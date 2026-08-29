# -*- coding: utf-8 -*-
"""Starlette 应用与路由层。

对外暴露：
- POST/GET/DELETE /{service}        转发到对应上游 MCP（如 /context7、/tavily）
- GET  /healthz                     健康检查
- GET  /stats                       密钥池与会话统计（需网关密钥）
- POST /admin/reset-key             重置单把密钥状态（需网关密钥）
- POST /admin/reload                重新读取配置并立即应用（需网关密钥）
- POST /admin/restart               请求网关优雅退出（需网关密钥）

进站鉴权：
- 所有 /{service} 请求必须携带有效的网关访问密钥，方式二选一：
    * Authorization: <access_key> 或 Authorization: Bearer <access_key>
    * 查询参数 ?key=<access_key>
- 校验失败返回 401。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import logging

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import AppConfig, load_config
from .key_state import KeyStateStore
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


def create_app(
    config: AppConfig,
    client: httpx.AsyncClient | None = None,
    *,
    state_store: KeyStateStore | None = None,
    config_path: str | None = None,
) -> Starlette:
    """构造 Starlette 应用。"""
    state: dict = {
        "config": config,
        "config_path": config_path,
        "engine": None,
        "client": client,
        "owns_client": client is None,
        "key_state_store": state_store or KeyStateStore(),
        "retest_task": None,
        "reload_lock": asyncio.Lock(),
        "access_keys": set(config.gateway.access_keys),
    }

    async def on_startup() -> None:
        runtime_config: AppConfig = state["config"]
        if state["client"] is None:
            state["client"] = httpx.AsyncClient(
                timeout=runtime_config.gateway.upstream_timeout_seconds,
                follow_redirects=True,
            )
        if state["engine"] is None:
            state["engine"] = ProxyEngine(
                runtime_config,
                state["client"],
                state_store=state["key_state_store"],
            )

        async def retest_loop() -> None:
            """周期性领取冷却到期密钥并执行自动复测。"""
            while True:
                try:
                    engine: ProxyEngine | None = state.get("engine")
                    if engine is not None:
                        await engine.retest_expired_keys()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("自动复测任务执行失败")
                await asyncio.sleep(5.0)

        state["retest_task"] = asyncio.create_task(retest_loop())
        logger.info("网关已启动，聚合服务：%s，鉴权密钥数：%d",
                     [s.name for s in runtime_config.services],
                     len(state["access_keys"]))

    async def on_shutdown() -> None:
        task = state.get("retest_task")
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            state["retest_task"] = None
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
        if provided is None or provided not in state["access_keys"]:
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
        runtime_config: AppConfig = state["config"]
        return JSONResponse({"status": "ok", "services": [s.name for s in runtime_config.services]})

    async def handle_stats(request: Request) -> JSONResponse:
        """统计端点（需网关密钥）。"""
        denied = _check_access(request)
        if denied is not None:
            return denied
        engine: ProxyEngine = state["engine"]
        return JSONResponse(await engine.stats())

    async def handle_reset_key(request: Request) -> JSONResponse:
        """重置单把密钥状态（需网关密钥）。"""
        denied = _check_access(request)
        if denied is not None:
            return denied

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "bad_request", "message": "请求体必须是 JSON"}, status_code=400)

        service_name = str((payload or {}).get("service", "")).strip()
        key = str((payload or {}).get("key", "")).strip()
        if not service_name or not key:
            return JSONResponse(
                {"error": "bad_request", "message": "缺少 service 或 key"},
                status_code=400,
            )

        engine: ProxyEngine = state["engine"]
        ok = await engine.reset_key_state(service_name, key)
        if not ok:
            return JSONResponse(
                {"error": "not_found", "message": "服务不存在、未启用密钥池或密钥不存在"},
                status_code=404,
            )
        return JSONResponse({"status": "ok", "service": service_name, "key_tail": key[-6:]})

    async def handle_reload(request: Request) -> JSONResponse:
        """从配置文件重新加载网关配置，并在当前进程中立即替换运行时。"""
        denied = _check_access(request)
        if denied is not None:
            return denied

        path = state.get("config_path")
        if not path:
            return JSONResponse(
                {"error": "unavailable", "message": "当前运行方式未提供配置文件路径"},
                status_code=503,
            )

        async with state["reload_lock"]:
            current_config: AppConfig = state["config"]
            try:
                new_config = load_config(path)
            except Exception as exc:  # noqa: BLE001 - 将配置错误返回给 GUI
                logger.warning("重新加载配置失败：%s", exc)
                return JSONResponse(
                    {"error": "bad_config", "message": f"配置无效：{exc}"},
                    status_code=400,
                )

            if new_config.gateway.port != current_config.gateway.port:
                return JSONResponse(
                    {
                        "error": "restart_required",
                        "message": "端口发生变化，请重启网关后应用",
                        "current_port": current_config.gateway.port,
                        "requested_port": new_config.gateway.port,
                    },
                    status_code=409,
                )

            new_engine = ProxyEngine(
                new_config,
                state["client"],
                state_store=state["key_state_store"],
            )
            state["config"] = new_config
            state["engine"] = new_engine
            state["access_keys"] = set(new_config.gateway.access_keys)
            request.app.state.config = new_config

        logger.info("配置已热加载，服务：%s", [s.name for s in new_config.services])
        return JSONResponse(
            {
                "status": "reloaded",
                "port": new_config.gateway.port,
                "services": [s.name for s in new_config.services],
            }
        )

    async def handle_restart(request: Request) -> JSONResponse:
        """请求当前 Uvicorn 网关优雅退出，供 GUI 随后重新启动。"""
        denied = _check_access(request)
        if denied is not None:
            return denied

        server = getattr(request.app.state, "server", None)
        if server is None:
            return JSONResponse(
                {"error": "unavailable", "message": "当前运行方式不支持重启接口"},
                status_code=503,
            )
        server.should_exit = True
        return JSONResponse({"status": "restarting"})

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
        Route("/admin/reset-key", handle_reset_key, methods=["POST"]),
        Route("/admin/reload", handle_reload, methods=["POST"]),
        Route("/admin/restart", handle_restart, methods=["POST"]),
        Route("/{service}", handle_service, methods=["GET", "POST", "DELETE"]),
        Route("/{service}/{rest:path}", handle_service, methods=["GET", "POST", "DELETE"]),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.config = config
    app.state.runtime = state
    if client is not None:
        state["engine"] = ProxyEngine(
            config,
            client,
            state_store=state["key_state_store"],
        )
    return app
