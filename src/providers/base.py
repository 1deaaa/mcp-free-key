# -*- coding: utf-8 -*-
"""平台提供方基础设施。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProbeSpec:
    """深度校验时使用的轻量探针定义。"""

    tool: str
    arguments: dict[str, Any]


@dataclass
class UsageSnapshot:
    """单把密钥的额度快照。"""

    status: str
    detail: str = ""
    key_usage: int | None = None
    key_limit: int | None = None
    account_plan: str = ""
    account_usage: int | None = None
    account_limit: int | None = None
    paygo_usage: int | None = None
    paygo_limit: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """是否成功取回额度。"""
        return self.status == "ok"

    @property
    def key_remaining(self) -> int | None:
        """当前密钥剩余额度。"""
        if self.key_limit is None or self.key_usage is None:
            return None
        return self.key_limit - self.key_usage

    @property
    def account_remaining(self) -> int | None:
        """当前套餐剩余额度。"""
        if self.account_limit is None or self.account_usage is None:
            return None
        return self.account_limit - self.account_usage


class ServiceProvider:
    """平台提供方基类。"""

    service_name: str = ""
    supports_usage: bool = False

    def get_probe(self) -> ProbeSpec | None:
        """返回默认探针。"""
        return None

    async def fetch_usage(self, key: str, timeout: float = 15.0) -> UsageSnapshot | None:
        """查询单把密钥的额度。默认不支持。"""
        _ = (key, timeout)
        return None
