# -*- coding: utf-8 -*-
"""平台提供方注册表。"""
from __future__ import annotations

from .base import ProbeSpec, ServiceProvider, UsageSnapshot
from .context7 import Context7Provider
from .serper import SerperProvider
from .tavily import TavilyProvider


_PROVIDERS: dict[str, ServiceProvider] = {
    Context7Provider.service_name: Context7Provider(),
    TavilyProvider.service_name: TavilyProvider(),
    SerperProvider.service_name: SerperProvider(),
}


def get_provider(service_name: str) -> ServiceProvider | None:
    """按服务名获取平台提供方。"""
    return _PROVIDERS.get(service_name)


def get_native_provider(service_name: str) -> ServiceProvider | None:
    """按服务名获取原生 MCP 提供方（无可用远端 MCP 上游的服务）。"""
    provider = _PROVIDERS.get(service_name)
    if provider is not None and provider.native:
        return provider
    return None


__all__ = [
    "ProbeSpec",
    "ServiceProvider",
    "UsageSnapshot",
    "get_native_provider",
    "get_provider",
]
