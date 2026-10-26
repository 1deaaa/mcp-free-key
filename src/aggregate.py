# -*- coding: utf-8 -*-
"""一键聚合端点（/mcp）：把全部启用服务聚为一个 MCP 服务。

进入 /mcp 的请求仍是标准 MCP JSON-RPC：
- initialize → 网关直接应答，列出已聚合的服务与工具数。
- notifications/initialized → 202 Accepted。
- tools/list → 汇总每个启用服务的 tools/list，工具名统一加
  "<service>_" 前缀并原样保留上游的 description / inputSchema，
  保证 AI 客户端看到的工具说明与各服务原生一致。
- tools/call → 按工具前缀路由到对应服务，沿用 ProxyEngine 的
  密钥轮询、故障转移与会话粘连，然后把上游响应 id 改回客户端请求 id。

与按服务后缀（/context7、/tavily 等）完全正交，可随时并存。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from .proxy import ProxyEngine
from .sse import parse_json_messages

logger = logging.getLogger("mcp_gateway.aggregate")

_PROTOCOL_VERSION = "2025-06-18"
_CATALOG_TTL_SECONDS = 60.0
_UPSTREAM_SESSION_TTL = 1500.0


@dataclass
class ToolRoute:
    """聚合工具名到归属服务与原始工具名的映射。"""

    service: str
    original: str


class AggregateGateway:
    """一键聚合逻辑（无状态上聚展示，有状态上游会话复用）。"""

    def __init__(self, engine: ProxyEngine) -> None:
        self._engine = engine
        self._upstream_sessions: dict[str, tuple[str, float]] = {}
        self._catalog: dict[str, ToolRoute] = {}
        self._catalog_tools: list[dict] = []
        self._catalog_time: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """聚合开关是否开启，并至少存在一个启用服务。"""
        cfg = self._engine_state_config()
        return bool(cfg and cfg.gateway.aggregate_enabled)

    def _engine_state_config(self):
        return getattr(self._engine, "_config", None)

    def _active_services(self) -> list:
        cfg = self._engine_state_config()
        if cfg is None:
            return []
        return [s for s in cfg.services if s.enabled and (s.keys or not s.key_auth.enabled)]

    # ── 上游会话管理 ──────────────────────────────────────────────────────────
    async def _ensure_upstream_session(self, service_name: str, headers: dict[str, str]) -> str | None:
        """为有状态上游完成 initialize 握手，返回 Mcp-Session-Id（无状态返回 None）。"""
        cached = self._upstream_sessions.get(service_name)
        if cached and time.monotonic() - cached[1] < _UPSTREAM_SESSION_TTL:
            return cached[0]

        init_body = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "gateway-aggregate", "version": "1.0.0"},
            },
        }).encode("utf-8")
        try:
            result = await self._engine.forward(
                service_name=service_name,
                method="POST",
                headers=dict(headers),
                body=init_body,
            )
        except Exception as exc:  # noqa: BLE001 - 单个服务握手失败只跳过该服务
            logger.warning("聚合握手 [%s] 失败：%s", service_name, exc)
            return None
        if result.status_code != 200:
            logger.warning("聚合握手 [%s] 返回 HTTP %s", service_name, result.status_code)
            return None
        sid = result.headers.get("mcp-session-id") or result.headers.get("Mcp-Session-Id")
        if sid:
            note = json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            ).encode("utf-8")
            note_headers = dict(headers)
            note_headers["Mcp-Session-Id"] = sid
            try:
                await self._engine.forward(
                    service_name=service_name,
                    method="POST",
                    headers=note_headers,
                    body=note,
                    client_session_id=sid,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("聚合 initialized 通知 [%s] 失败：%s", service_name, exc)
            self._upstream_sessions[service_name] = (sid, time.monotonic())
            return sid
        return None

    def _invalidate_upstream_session(self, service_name: str) -> None:
        self._upstream_sessions.pop(service_name, None)

    def _invalidate_catalog(self) -> None:
        self._catalog_time = 0.0

    # ── 工具目录 ──────────────────────────────────────────────────────────────
    async def _fetch_service_tools(
        self, service_name: str, headers: dict[str, str], sid: str | None
    ) -> list[dict]:
        """拉取单个服务的工具清单（原始 dict，含 description / inputSchema）。"""
        list_body = json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        ).encode("utf-8")
        fwd_headers = dict(headers)
        if sid:
            fwd_headers["Mcp-Session-Id"] = sid
        result = await self._engine.forward(
            service_name=service_name,
            method="POST",
            headers=fwd_headers,
            body=list_body,
            client_session_id=sid,
        )
        ctype = (result.headers.get("content-type") or "")
        messages = parse_json_messages(result.body.decode("utf-8", errors="replace"), ctype)
        tools: list[dict] = []
        for msg in messages:
            res = msg.get("result") if isinstance(msg, dict) else None
            if isinstance(res, dict) and isinstance(res.get("tools"), list):
                tools.extend(t for t in res["tools"] if isinstance(t, dict))
        return tools

    def _build_tool_entry(self, service_name: str, tool: dict, used: set[str]) -> tuple[str, dict]:
        """生成聚合工具名（service_ 前缀），正文原样透传。"""
        original = str(tool.get("name", "") or "tool")
        base = f"{service_name}_{original}"
        name = base
        idx = 2
        while name in used:
            name = f"{base}_{idx}"
            idx += 1
        used.add(name)
        entry = dict(tool)
        entry["name"] = name
        meta = dict(entry.get("_meta") or {})
        meta["gateway"] = {"service": service_name, "original_tool": original}
        entry["_meta"] = meta
        return name, entry

    async def build_catalog(self, headers: dict[str, str]) -> tuple[list[dict], dict[str, ToolRoute]]:
        """构建（并缓存）聚合工具目录。"""
        async with self._lock:
            if time.monotonic() - self._catalog_time < _CATALOG_TTL_SECONDS and self._catalog:
                return self._catalog_tools, self._catalog
            tools_out: list[dict] = []
            routes: dict[str, ToolRoute] = {}
            used: set[str] = set()
            for svc in self._active_services():
                try:
                    sid = await self._ensure_upstream_session(svc.name, headers)
                    upstream_tools = await self._fetch_service_tools(svc.name, headers, sid)
                except Exception as exc:  # noqa: BLE001 - 单个服务异常不影响其他服务
                    logger.warning("聚合拉取工具 [%s] 失败：%s", svc.name, exc)
                    self._invalidate_upstream_session(svc.name)
                    continue
                for tool in upstream_tools:
                    name, entry = self._build_tool_entry(svc.name, tool, used)
                    tools_out.append(entry)
                    routes[name] = ToolRoute(service=svc.name, original=str(tool.get("name", "")))
            self._catalog_tools = tools_out
            self._catalog = routes
            self._catalog_time = time.monotonic()
            return tools_out, routes

    async def refresh_catalog(self) -> tuple[list[dict], dict[str, ToolRoute]]:
        """强制刷新聚合工具目录（配置热加载后调用）。"""
        self._invalidate_catalog()
        return await self.build_catalog({})

    # ── 工具调用 ──────────────────────────────────────────────────────────────
    async def call_tool(
        self, tool_name: str, arguments: dict, request_id, headers: dict[str, str]
    ) -> tuple[int, dict]:
        """把聚合工具调用路由到归属服务，返回 (HTTP 状态码, JSON-RPC 响应体)。"""
        _tools, routes = await self.build_catalog(headers)
        route = routes.get(tool_name)
        if route is None:
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"聚合目录中不存在工具：{tool_name}"},
            }
        call_body = json.dumps({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": route.original, "arguments": arguments or {}},
        }).encode("utf-8")
        sid = await self._ensure_upstream_session(route.service, headers)
        fwd_headers = dict(headers)
        if sid:
            fwd_headers["Mcp-Session-Id"] = sid
        try:
            result = await self._engine.forward(
                service_name=route.service,
                method="POST",
                headers=fwd_headers,
                body=call_body,
                client_session_id=sid,
            )
        except Exception as exc:  # noqa: BLE001
            self._invalidate_upstream_session(route.service)
            self._invalidate_catalog()
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": f"服务 [{route.service}] 调用失败：{exc}"}],
                    "isError": True,
                },
            }

        if result.status_code != 200:
            # 上游整把密钥池都不可用时，把错误转成工具级 isError，
            # 让 AI 能看到原因而不是面对传输层报错。
            reason = result.body.decode("utf-8", errors="replace")[:500]
            self._invalidate_upstream_session(route.service)
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{
                        "type": "text",
                        "text": f"服务 [{route.service}] 上游错误（HTTP {result.status_code}）：{reason}",
                    }],
                    "isError": True,
                },
            }

        ctype = result.headers.get("content-type") or ""
        messages = parse_json_messages(result.body.decode("utf-8", errors="replace"), ctype)
        payload = None
        for msg in messages:
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                payload = msg
                break
        if payload is None:
            return 200, {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "上游返回无法解析的响应"},
            }
        payload = dict(payload)
        payload["id"] = request_id
        return 200, payload

    def describe(self) -> dict:
        """返回聚合端点的自描述信息（用于 initialize 指令）。"""
        cfg = self._engine_state_config()
        services = [s.name for s in self._active_services()] if cfg else []
        return {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mcp-gateway-aggregate", "version": "1.0.0"},
            "instructions": (
                "一键聚合网关：一次连接即可调用全部上游 MCP 服务。工具命名为"
                " <服务>_<原工具名>（如 serper_web_search、tavily_tavily_search、"
                "context7_query-docs），说明与参数与各服务原生一致，"
                "按说明直接调用即可。当前已聚合服务："
                + ("、".join(services) if services else "（暂无）")
            ),
        }
