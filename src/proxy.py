# -*- coding: utf-8 -*-
"""透明反向代理引擎：网关核心。

职责：
- 将进站的 MCP 请求按服务转发到对应上游 remote URL。
- 启用密钥鉴权的服务：从密钥池轮询选密钥并注入（header 或 query）。
- 有状态上游（返回 Mcp-Session-Id）：将会话绑定到所用密钥，
  后续同会话请求复用同一密钥，保证一致性。
- 无状态上游（如 Tavily）：每个请求独立轮询，可在 tools/call 上故障转移。
- 失败检测：缓冲上游响应，复用 failure 检测器判断密钥是否失效；
  失效则标记冷却并换下一把密钥重试，直到成功或达到最大重试次数。

设计为与 Web 框架解耦的引擎，便于注入 httpx 客户端做单元/端到端测试。

注意：为支持故障转移，需要读取完整上游响应再决定是否重试。
实测 Context7 / Tavily 的响应均为「一次性单事件」，缓冲不会破坏可用性。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import AppConfig, ServiceConfig
from .failure import detect_failure
from .keypool import KeyPool
from .key_state import KeyStateStore
from .sessions import SessionStore

logger = logging.getLogger("mcp_gateway.proxy")


# 转发时需要剔除的逐跳（hop-by-hop）头与会自动重算的头
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "content-length", "content-encoding", "host",
}
# 进站时由网关消费、不应原样转发给上游的头
# - authorization: 用于网关统一密钥鉴权，不应泄露到上游
# - mcp-session-id: 由网关单独读取并以规范大小写重新注入，避免重复头导致值被逗号拼接
_CONSUMED_BY_GATEWAY = {"authorization", "mcp-session-id"}


@dataclass
class ProxyResponse:
    """代理返回给客户端的响应封装。"""

    status_code: int
    headers: dict[str, str]
    body: bytes
    used_key_tail: str = ""          # 实际使用的密钥尾部（调试用）
    attempts: int = 1               # 实际尝试次数（含故障转移）


@dataclass
class ServiceRuntime:
    """单个服务的运行时状态。"""

    config: ServiceConfig
    pool: KeyPool | None             # 启用密钥鉴权时存在
    sessions: SessionStore


class ProxyError(Exception):
    """代理层错误（如服务未找到、上游不可达）。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class ProxyEngine:
    """透明反向代理引擎。"""

    def __init__(
        self,
        config: AppConfig,
        client: httpx.AsyncClient,
        *,
        state_store: KeyStateStore | None = None,
    ) -> None:
        """初始化。

        Args:
            config: 应用配置。
            client: 注入的 httpx 异步客户端（生产用真实，测试用 MockTransport）。
        """
        self._config = config
        self._client = client
        self._state_store = state_store
        self._runtimes: dict[str, ServiceRuntime] = {}
        for svc in config.services:
            pool = None
            if svc.key_auth.enabled and svc.keys:
                pool = KeyPool(
                    svc.keys,
                    cooldown_seconds=config.gateway.key_cooldown_seconds,
                    service_name=svc.name,
                    state_store=state_store,
                )
            self._runtimes[svc.name] = ServiceRuntime(
                config=svc,
                pool=pool,
                sessions=SessionStore(ttl_seconds=config.gateway.session_ttl_seconds),
            )

    def get_runtime(self, service_name: str) -> ServiceRuntime | None:
        """按名获取服务运行时。"""
        return self._runtimes.get(service_name)

    async def forward(
        self,
        service_name: str,
        method: str,
        headers: dict[str, str],
        body: bytes,
        client_session_id: str | None = None,
        extra_path: str = "",
    ) -> ProxyResponse:
        """转发一次请求到上游，按需轮询密钥并故障转移。

        Args:
            service_name: 目标服务名（路由路径）。
            method: HTTP 方法（POST / GET / DELETE）。
            headers: 进站请求头。
            body: 进站请求体。
            client_session_id: 客户端携带的 Mcp-Session-Id（若有）。

        Returns:
            ProxyResponse。

        Raises:
            ProxyError: 服务不存在/被禁用，或上游不可达。
        """
        runtime = self._runtimes.get(service_name)
        if runtime is None or not runtime.config.enabled:
            raise ProxyError(404, f"服务不存在或已禁用：{service_name}")

        svc = runtime.config
        base_headers = self._build_forward_headers(headers)

        # 无需密钥鉴权：直接单次转发
        if runtime.pool is None:
            return await self._single_forward(
                runtime, method, base_headers, body, key=None,
                client_session_id=client_session_id,
                extra_path=extra_path,
            )

        # 会话粘连：若客户端带的会话已绑定密钥，复用该密钥，不做故障转移
        if client_session_id:
            bound_key = await runtime.sessions.get_key(client_session_id)
            if bound_key is not None:
                resp = await self._single_forward(
                    runtime, method, base_headers, body, key=bound_key,
                    client_session_id=client_session_id,
                    extra_path=extra_path,
                )
                # DELETE 会话终止：解绑
                if method.upper() == "DELETE":
                    await runtime.sessions.unbind(client_session_id)
                return resp

        # 无绑定会话（initialize 或无状态上游）：轮询 + 故障转移
        return await self._forward_with_failover(
            runtime, method, base_headers, body, client_session_id, extra_path,
        )

    async def _forward_with_failover(
        self,
        runtime: ServiceRuntime,
        method: str,
        base_headers: dict[str, str],
        body: bytes,
        client_session_id: str | None,
        extra_path: str,
    ) -> ProxyResponse:
        """带故障转移的转发：依次尝试不同密钥，直到成功或达上限。"""
        svc = runtime.config
        pool = runtime.pool
        assert pool is not None

        max_attempts = max(1, min(self._config.gateway.max_failover_retries, pool.size))
        tried: set[str] = set()
        last_resp: ProxyResponse | None = None

        for attempt in range(1, max_attempts + 1):
            # 选密钥：优先按轮询顺序选择“未尝试且当前可用”的密钥，
            # 这样即便在故障转移场景下也能保持 round-robin 负载均衡。
            key = await pool.next_key_excluding(exclude=tried)
            if key is None:
                key = await pool.next_key()
                if key in tried:
                    # 没有新密钥可试，终止
                    break
            tried.add(key)

            resp, failure, is_key = await self._forward_once_detect(
                runtime, method, base_headers, body, key, client_session_id, extra_path,
            )
            resp.attempts = attempt
            last_resp = resp

            if failure is None:
                # 成功：标记密钥健康，绑定会话（有状态上游）
                await pool.mark_success(key)
                new_sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
                if new_sid:
                    await runtime.sessions.bind(new_sid, key)
                return resp

            # 失败：仅密钥层面失效才标记冷却，5xx 等上游临时故障不惩罚密钥
            if is_key:
                await pool.mark_failed(key)
                logger.warning(
                    "服务 [%s] 密钥 ...%s 被判定为密钥失败（尝试 %d/%d）：%s",
                    svc.name, key[-6:], attempt, max_attempts, failure,
                )
            else:
                logger.warning(
                    "服务 [%s] 上游临时失败，保留密钥 ...%s（尝试 %d/%d）：%s",
                    svc.name, key[-6:], attempt, max_attempts, failure,
                )

        # 全部尝试失败：返回最后一次上游响应（让客户端看到真实错误）
        if last_resp is not None:
            return last_resp
        raise ProxyError(502, f"服务 [{svc.name}] 所有密钥均失败")

    async def _forward_once_detect(
        self,
        runtime: ServiceRuntime,
        method: str,
        base_headers: dict[str, str],
        body: bytes,
        key: str | None,
        client_session_id: str | None,
        extra_path: str,
    ) -> tuple[ProxyResponse, str | None, bool]:
        """转发一次并做失败检测。

        Returns:
            (ProxyResponse, failure_reason, is_key_failure)；
            failure_reason 为 None 表示成功；
            is_key_failure 为 True 表示密钥本身失效（需标记冷却）。
        """
        svc = runtime.config
        url, req_headers = self._inject_key(svc, base_headers, key, extra_path)
        if client_session_id:
            req_headers["Mcp-Session-Id"] = client_session_id

        try:
            upstream = await self._client.request(
                method, url, headers=req_headers, content=body,
            )
        except httpx.HTTPError as e:
            # 网络层错误也视为该密钥本次失败，允许故障转移
            resp = ProxyResponse(
                status_code=502,
                headers={"content-type": "application/json"},
                body=b'{"error":"upstream unreachable"}',
                used_key_tail=key[-6:] if key else "",
            )
            return resp, f"网络错误：{type(e).__name__}: {e}", False

        body_bytes = upstream.content
        body_text = self._safe_text(upstream)
        ctype = upstream.headers.get("content-type")

        fr = detect_failure(
            upstream.status_code, ctype, body_text, svc.normalized_failure_patterns,
        )

        resp = ProxyResponse(
            status_code=upstream.status_code,
            headers=self._build_response_headers(upstream.headers),
            body=body_bytes,
            used_key_tail=key[-6:] if key else "",
        )
        return resp, (fr.reason if fr.is_failure else None), fr.is_key_failure

    async def _single_forward(
        self,
        runtime: ServiceRuntime,
        method: str,
        base_headers: dict[str, str],
        body: bytes,
        key: str | None,
        client_session_id: str | None,
        extra_path: str,
    ) -> ProxyResponse:
        """单次转发（无故障转移），用于会话粘连或无密钥服务。"""
        resp, _failure, _is_key = await self._forward_once_detect(
            runtime, method, base_headers, body, key, client_session_id, extra_path,
        )
        # 单次转发也尝试绑定新会话（无密钥服务 key 为 None 时不绑定）
        if key is not None:
            new_sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
            if new_sid and not client_session_id:
                await runtime.sessions.bind(new_sid, key)
        return resp

    def _inject_key(
        self, svc: ServiceConfig, base_headers: dict[str, str], key: str | None, extra_path: str = "",
    ) -> tuple[str, dict[str, str]]:
        """根据服务鉴权方式注入密钥，返回 (url, headers)。"""
        url = svc.upstream_url
        if extra_path:
            # 针对 Streamable HTTP 协议，客户端（如 Cursor/VS Code）在连接时，
            # 可能会自动在配置的 URL 后面追加标准端点路径 "/mcp"（例如请求 /context7/mcp）。
            # 如果上游服务的 URL 本身就已经以 "/mcp" 结尾（例如 https://mcp.context7.com/mcp），
            # 此时追加 extra_path 会导致请求变成 "/mcp/mcp" 从而触发 404 错误。
            # 因此，如果 extra_path 是 "/mcp" 且上游 URL 已经以 "/mcp" 结尾，我们不重复追加。
            normalized_extra = extra_path.strip("/")
            if normalized_extra == "mcp" and url.rstrip("/").endswith("/mcp"):
                pass
            else:
                if url.endswith("/") and extra_path.startswith("/"):
                    url = url.rstrip("/") + extra_path
                elif not url.endswith("/") and not extra_path.startswith("/"):
                    url = url + "/" + extra_path
                else:
                    url = url + extra_path
        headers = dict(base_headers)
        if key is not None and svc.key_auth.enabled:
            if svc.key_auth.type == "header":
                headers[svc.key_auth.param] = key
            elif svc.key_auth.type == "query":
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}{svc.key_auth.param}={key}"
        return url, headers

    @staticmethod
    def _build_forward_headers(incoming: dict[str, str]) -> dict[str, str]:
        """从进站头构造转发给上游的基础头（剔除逐跳头与网关消费的头）。"""
        result: dict[str, str] = {}
        for name, value in incoming.items():
            low = name.lower()
            if low in _HOP_BY_HOP or low in _CONSUMED_BY_GATEWAY:
                continue
            result[name] = value
        return result

    @staticmethod
    def _build_response_headers(upstream_headers: httpx.Headers) -> dict[str, str]:
        """构造返回给客户端的响应头（剔除逐跳头，保留 Mcp-Session-Id 等）。"""
        result: dict[str, str] = {}
        for name, value in upstream_headers.items():
            if name.lower() in _HOP_BY_HOP:
                continue
            result[name] = value
        return result

    @staticmethod
    def _safe_text(resp: httpx.Response) -> str:
        """安全地将响应解码为文本用于失败检测。"""
        try:
            return resp.text
        except Exception:  # noqa: BLE001
            try:
                return resp.content.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return ""

    async def stats(self) -> dict[str, Any]:
        """汇总各服务的密钥池与会话统计（供监控端点）。"""
        out: dict[str, Any] = {}
        for name, rt in self._runtimes.items():
            entry: dict[str, Any] = {
                "enabled": rt.config.enabled,
                "upstream_url": rt.config.upstream_url,
                "key_auth": rt.config.key_auth.enabled,
                "sessions": await rt.sessions.size(),
            }
            if rt.pool is not None:
                ps = await rt.pool.stats()
                entry["keys"] = {
                    "total": ps.total,
                    "available": ps.available,
                    "cooling": ps.cooling,
                    "details": ps.details,
                }
            out[name] = entry
        return out
