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
    balance: int | None = None       # 剩余额度（预付次数型平台，如 Serper 积分）
    rate_limit: int | None = None    # 平台限速（每秒请求数）
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

    @property
    def has_quota_info(self) -> bool:
        """是否成功取回任何可用于展示的额度信息。"""
        return any(
            value is not None
            for value in (
                self.key_limit,
                self.account_limit,
                self.paygo_limit,
                self.balance,
            )
        )


class ServiceProvider:
    """平台提供方基类。"""

    service_name: str = ""
    supports_usage: bool = False
    # 原生 MCP 服务：网关直接实现 MCP 协议并由其代理到该平台的 REST 接口。
    # 仅当服务未提供可用的远程 MCP 上游时才启用。
    native: bool = False

    def get_probe(self) -> ProbeSpec | None:
        """返回默认探针。"""
        return None

    async def fetch_usage(self, key: str, timeout: float = 15.0) -> UsageSnapshot | None:
        """查询单把密钥的额度。默认不支持。"""
        _ = (key, timeout)
        return None

    def list_tools(self) -> list[dict[str, Any]]:
        """返回原生 MCP 服务的工具清单（仅 native=True 时有效）。"""
        return []

    async def call_tool(
        self, tool: str, arguments: dict[str, Any], key: str, timeout: float = 30.0
    ) -> tuple[int, Any]:
        """执行原生 MCP 工具调用。

        Returns:
            (上游 HTTP 状态码, 结构化结果) 二元组。
        """
        _ = (tool, arguments, key, timeout)
        return 404, {"message": "该服务未实现原生工具调用"}
