# -*- coding: utf-8 -*-
"""原生 MCP 服务适配层。

背景：
- 并非所有搜索商都提供官方托管的远端 MCP 端点（实测 serper.dev
  没有任何官方 MCP 入口，官方交互面只有 REST API）。
- 本模块让网关自身为这类服务实现标准的 MCP 端点
 （initialize / tools/list / tools/call），并把工具调用翻译成对应平台的
  REST 请求；密钥注入、失败检测、故障转移、额度查询等通用能力
  全部复用现有的 ProxyEngine 管线，无需为每个原生服务写分支逻辑。

设计：
- 服务的 upstream_url 使用 "native://<provider_name>" 前缀标记为原生服务。
- 原生 ASGI 应用内嵌在网关进程中，ProxyEngine 通过 httpx 的
  ASGITransport 在进程内转发，避免回环 HTTP。
- 密钥从进站请求直接读取：Header 的 X-API-KEY / Authorization(Bearer)，
  或 Query 的 apiKey/api_key/key。
- 平台 REST 返回的错误状态码（403/429 等）会原样映射为 HTTP 响应状态码，
  使得 failure.detect_failure 能够逐层触发密钥冷却与故障转移。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("mcp_gateway.native")

NATIVE_URL_PREFIX = "native://"

_SERVER_VERSION = "1.0.0"

# MCP 协议版本：与代理层保持一致。
_PROTOCOL_VERSION = "2025-06-18"


def is_native_url(url: str) -> bool:
    """判断上游 URL 是否为原生服务标记。"""
    return str(url or "").startswith(NATIVE_URL_PREFIX)


def native_provider_from_url(url: str) -> str:
    """从原生服务标记中解析提供方名称（native://serper/mcp -> serper）。"""
    rest = str(url or "")[len(NATIVE_URL_PREFIX):]
    return rest.split("/", 1)[0].strip()


def native_path_from_url(url: str) -> str:
    """把原生服务标记映射为内嵌应用的请求路径（native://serper/mcp -> /native/serper/mcp）。"""
    rest = str(url or "")[len(NATIVE_URL_PREFIX):].strip("/")
    return f"/native/{rest}" if rest else "/native"


def extract_native_key(headers: dict[str, str], query: dict[str, str]) -> str | None:
    """从原生请求中提取上游密钥（大小写不敏感）。"""
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    key = lowered.get("x-api-key")
    if key:
        return str(key).strip() or None
    auth = lowered.get("authorization", "")
    if isinstance(auth, str) and auth.lower().startswith("bearer "):
        candidate = auth[7:].strip()
        if candidate:
            return candidate
    q_lowered = {str(k).lower(): v for k, v in (query or {}).items()}
    for name in ("apikey", "api_key", "key"):
        candidate = q_lowered.get(name)
        if candidate and str(candidate).strip():
            return str(candidate).strip()
    return None


def _jsonrpc_error(request_id: Any, code: int, message: str, status_code: int) -> tuple[int, dict[str, Any]]:
    """构造 JSON-RPC 错误响应。"""
    return status_code, {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _jsonrpc_result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """构造 JSON-RPC 成功响应。"""
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def create_native_app(providers: dict[str, Any]) -> Starlette:
    """构造承载原生 MCP 服务的内嵌 ASGI 应用。

    Args:
        providers: 提供方名称到 ServiceProvider 实例的映射
            （仅需 native=True 的条目）。
    """
    name_map = {str(name).lower(): provider for name, provider in providers.items()}

    async def handle_native(request: Request) -> Response:
        provider_name = str(request.path_params.get("provider", "") or "").lower()
        provider = name_map.get(provider_name)
        if provider is None:
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32601, "message": f"未知原生服务：{provider_name}"}},
                status_code=404,
            )

        try:
            body = await request.body()
            payload = json.loads(body.decode("utf-8") or "{}") if body else {}
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32700, "message": "请求体不是合法 JSON"}},
                status_code=400,
            )

        method = payload.get("method", "")
        request_id = payload.get("id")
        headers = {k: v for k, v in request.headers.items()}
        query = dict(request.query_params)

        if method == "notifications/initialized":
            # 通知无需响应体，返回 202 即可。
            return Response(status_code=202)

        if method == "initialize":
            key = extract_native_key(headers, query)
            if not key:
                status, err = _jsonrpc_error(
                    request_id, -32001, "缺少上游密钥：请配置密钥池后再连接", 401
                )
                return JSONResponse(err, status_code=status)
            return JSONResponse(
                _jsonrpc_result(
                    request_id,
                    {
                        "protocolVersion": _PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": f"gateway-native-{provider_name}",
                            "version": _SERVER_VERSION,
                        },
                        "instructions": (
                            f"这是网关内置的 {provider_name} 原生 MCP 服务，"
                            "工具调用会使用密钥池轮询注入上游密钥。"
                        ),
                    },
                )
            )

        if method == "tools/list":
            tools = provider.list_tools()
            return JSONResponse(_jsonrpc_result(request_id, {"tools": tools}))

        if method == "tools/call":
            params = payload.get("params") or {}
            tool = str(params.get("name", ""))
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                status, err = _jsonrpc_error(request_id, -32602, "arguments 必须是对象", 400)
                return JSONResponse(err, status_code=status)
            key = extract_native_key(headers, query)
            if not key:
                status, err = _jsonrpc_error(request_id, -32001, "缺少上游密钥：请配置密钥池后再调用", 401)
                return JSONResponse(err, status_code=status)
            upstream_timeout = float(headers.pop("x-gateway-upstream-timeout", "") or 0) or 60.0
            try:
                status_code, structured = await provider.call_tool(tool, arguments, key, timeout=upstream_timeout)
            except Exception as exc:  # noqa: BLE001 - 原生服务也必须返回合法 JSON-RPC
                logger.exception("原生服务 %s 工具调用异常", provider_name)
                status, err = _jsonrpc_error(request_id, -32603, f"工具执行异常：{exc}", 500)
                return JSONResponse(err, status_code=status)
            if status_code >= 400:
                # 平台错误直接映射为 HTTP 状态码 + JSON-RPC error，
                # 令 failure.detect_failure 判定为密钥失效并执行故障转移。
                message = ""
                if isinstance(structured, dict):
                    message = str(structured.get("message", ""))[:300]
                status, err = _jsonrpc_error(
                    request_id, -32001, message or f"上游错误 HTTP {status_code}", status_code
                )
                return JSONResponse(err, status_code=status)
            text = json.dumps(structured, ensure_ascii=False)
            return JSONResponse(
                _jsonrpc_result(
                    request_id,
                    {
                        "content": [{"type": "text", "text": text}],
                        "structuredContent": structured if isinstance(structured, dict) else {"result": structured},
                        "isError": False,
                    },
                )
            )

        if method == "ping":
            return JSONResponse(_jsonrpc_result(request_id, {}))

        status, err = _jsonrpc_error(request_id, -32601, f"不支持的方法：{method}", 405)
        return JSONResponse(err, status_code=status)

    routes = [
        Route("/native/{provider}/mcp", handle_native, methods=["POST"]),
        Route("/native/{provider}", handle_native, methods=["POST"]),
    ]
    return Starlette(routes=routes)


class NativeClient:
    """原生服务的进程内调用客户端（无需回环 HTTP，开销与本地函数相当）。"""

    def __init__(self, providers: dict[str, Any]) -> None:
        self._app = create_native_app(providers)
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self._app),
            base_url="http://native",
        )

    async def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        timeout: float = 60.0,
    ) -> httpx.Response:
        """向内嵌原生应用发起请求（url 形如 native://serper/mcp）。"""
        path = native_path_from_url(url)
        extra_headers = dict(headers or {})
        extra_headers.setdefault("x-gateway-upstream-timeout", str(timeout))
        return await self._client.request(method, path, headers=extra_headers, content=content or b"")

    async def aclose(self) -> None:
        await self._client.aclose()
