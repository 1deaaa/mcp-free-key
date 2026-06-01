# -*- coding: utf-8 -*-
"""Context7 平台特化逻辑。"""
from __future__ import annotations

from .base import ProbeSpec, ServiceProvider


class Context7Provider(ServiceProvider):
    """Context7 提供方。"""

    service_name = "context7"

    def get_probe(self) -> ProbeSpec:
        """返回最省调用次数的默认探针。"""
        return ProbeSpec(
            tool="query-docs",
            arguments={"libraryId": "/vercel/next.js", "query": "routing"},
        )
