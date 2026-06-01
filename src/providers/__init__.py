# -*- coding: utf-8 -*-
"""平台提供方注册表。"""
from __future__ import annotations

from .base import ProbeSpec, ServiceProvider, UsageSnapshot
from .context7 import Context7Provider
from .tavily import TavilyProvider


_PROVIDERS: dict[str, ServiceProvider] = {
    Context7Provider.service_name: Context7Provider(),
    TavilyProvider.service_name: TavilyProvider(),
}


def get_provider(service_name: str) -> ServiceProvider | None:
    """按服务名获取平台提供方。"""
    return _PROVIDERS.get(service_name)


__all__ = [
    "ProbeSpec",
    "ServiceProvider",
    "UsageSnapshot",
    "get_provider",
]
