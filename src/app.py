# -*- coding: utf-8 -*-
"""Starlette 应用与路由层。

对外暴露：
- POST/GET/DELETE /{service}        转发到对应上游 MCP（如 /context7、/tavily）
- GET  /healthz                     健康检查
- GET  /stats                       密钥池与会话统计（需网关密钥）

进站鉴权：
- 所有 /{service} 请求必须携带有效的网关访问密钥，方式二选一：
    * Authorization: <access_key> 或 Authorization: Bearer <access_key>
    * 查询参数 ?key=<access_key>
- 校验失败返回 401。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import logging

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import AppConfig
from .proxy import ProxyEngine, ProxyError

logger = logging.getLogger("mcp_gateway.app")


def _extract_access_key(request: Request) -> str | None:
    """从请求中提取网关访问密钥。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if auth:
        return auth.strip()
    key = request.query_params.get("key")
    if key:
        return key.strip()
    return None


def create_app(config: AppConfig, client: httpx.AsyncClient | None = None) -> Starlette:
    """构造 Starlette 应用。"""
    state: dict = {"engine": None, "client": client, "owns_client": client is None}
    access_keys = set(config.gateway.access_keys)

    async def on_startup() -> None:
        if state["client"] is None:
            state["client"] = httpx.AsyncClient(
                timeout=config.gateway.upstream_timeout_seconds,
                follow_redirects=True,
            )
        state["engine"] = ProxyEngine(config, state["client"])
        logger.info("网关已启动，聚合服务：%s，鉴权密钥数：%d",
                     [s.name for s in config.services], len(access_keys))

    async def on_shutdown() -> None:
        if state["owns_client"] and state["client"] is not None:
            await state["client"].aclose()

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        await on_startup()
        try:
            yield
        finally:
            await on_shutdown()

    def _check_access(request: Request) -> JSONResponse | None:
        """校验网关访问密钥，通过返回 None，否则返回 401 响应。"""
        provided = _extract_access_key(request)
        if provided is None or provided not in access_keys:
            return JSONResponse(
                {"error": "unauthorized", "message": "缺少或无效的网关访问密钥"},
                status_code=401,
            )
        return None

    async def handle_service(request: Request) -> Response:
        """处理 /{service} 的 MCP 转发请求。"""
        denied = _check_access(request)
        if denied is not None:
            return denied

        service_name = request.path_params["service"]
        rest = str(request.path_params.get("rest", "") or "")
        extra_path = f"/{rest}" if rest else ""
        engine: ProxyEngine = state["engine"]

        body = await request.body()
        client_session_id = request.headers.get("mcp-session-id")
        incoming_headers = {k: v for k, v in request.headers.items()}

        try:
            result = await engine.forward(
                service_name=service_name,
                method=request.method,
                headers=incoming_headers,
                body=body,
                client_session_id=client_session_id,
                extra_path=extra_path,
            )
        except ProxyError as e:
            return JSONResponse({"error": "proxy_error", "message": e.message}, status_code=e.status_code)

        return Response(
            content=result.body,
            status_code=result.status_code,
            headers=result.headers,
        )

    async def handle_health(request: Request) -> JSONResponse:
        """健康检查端点（无需鉴权）。"""
        return JSONResponse({"status": "ok", "services": [s.name for s in config.services]})

    async def handle_stats(request: Request) -> JSONResponse:
        """统计端点（需网关密钥）。"""
        denied = _check_access(request)
        if denied is not None:
            return denied
        engine: ProxyEngine = state["engine"]
        return JSONResponse(await engine.stats())

    async def handle_not_found(request: Request) -> JSONResponse:
        """非服务路径统一返回 404（避免被 /{service} 捕获后返回 401 触发 OAuth）。"""
        return JSONResponse({"error": "not_found"}, status_code=404)

    routes = [
        Route("/healthz", handle_health, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", handle_not_found, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", handle_not_found, methods=["GET"]),
        Route("/register", handle_not_found, methods=["POST"]),
        Route("/authorize", handle_not_found, methods=["GET", "POST"]),
        Route("/token", handle_not_found, methods=["POST"]),
        Route("/stats", handle_stats, methods=["GET"]),
        Route("/{service}", handle_service, methods=["GET", "POST", "DELETE"]),
        Route("/{service}/{rest:path}", handle_service, methods=["GET", "POST", "DELETE"]),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.config = config
    app.state.runtime = state
    return app
