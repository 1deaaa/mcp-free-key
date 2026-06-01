# -*- coding: utf-8 -*-
"""Tavily 平台特化逻辑。"""
from __future__ import annotations

import httpx

from .base import ProbeSpec, ServiceProvider, UsageSnapshot


class TavilyProvider(ServiceProvider):
    """Tavily 提供方。"""

    service_name = "tavily"
    usage_url = "https://api.tavily.com/usage"
    supports_usage = True

    def get_probe(self) -> ProbeSpec:
        """返回最低成本的连通性探针。"""
        return ProbeSpec(
            tool="tavily_search",
            arguments={
                "query": "ping",
                "topic": "general",
                "search_depth": "basic",
                "max_results": 1,
                "include_raw_content": False,
                "include_images": False,
                "include_image_descriptions": False,
            },
        )

    async def fetch_usage(self, key: str, timeout: float = 15.0) -> UsageSnapshot:
        """查询 Tavily 当前密钥与账户套餐额度。"""
        headers = {"Authorization": f"Bearer {key}"}
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(self.usage_url, headers=headers)
        except httpx.HTTPError as exc:
            return UsageSnapshot(status="error", detail=f"网络错误：{type(exc).__name__}: {exc}")

        if resp.status_code in (401, 403):
            return UsageSnapshot(status="error", detail=f"鉴权失败：HTTP {resp.status_code}")
        if resp.status_code >= 400:
            return UsageSnapshot(status="error", detail=f"查询失败：HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as exc:
            return UsageSnapshot(status="error", detail=f"响应解析失败：{exc}")

        key_info = data.get("key") or {}
        account_info = data.get("account") or {}
        return UsageSnapshot(
            status="ok",
            key_usage=_to_int(key_info.get("usage")),
            key_limit=_to_int(key_info.get("limit")),
            account_plan=str(account_info.get("plan") or ""),
            account_usage=_to_int(account_info.get("plan_usage")),
            account_limit=_to_int(account_info.get("plan_limit")),
            paygo_usage=_to_int(account_info.get("payg_usage")),
            paygo_limit=_to_int(account_info.get("payg_limit")),
            raw=data,
        )


def _to_int(value) -> int | None:
    """将接口数值安全转为整数。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
