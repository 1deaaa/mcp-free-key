# -*- coding: utf-8 -*-
"""Serper 平台特化逻辑与原生 MCP 服务实现。

背景（多渠道交叉验证的实测结论）：
- serper.dev 没有官方托管的远端 MCP 端点（mcp.serper.dev 不解析，
  serper.dev/api.serper.dev/google.serper.dev 下无任何 /mcp 路径，
  官方站点构建清单中也无 MCP 路由，官方整合均为本地 stdio 桥接）。
- 官方交互面是 REST API：POST https://google.serper.dev/<type>，
  鉴权头为 X-API-KEY；剩余额度接口为 GET https://google.serper.dev/account。
- 因此网关选择「原生 MCP 服务」形态直接代理为 serper REST：
  向客户端暴露 /serper 的标准 MCP 端点（initialize/tools/list/tools/call），
  用密钥池中的密钥发起 REST 调用并把状态码/正文原样带回，
  使得密钥轮询、故障转移、冷却、额度查询等通用功能全部复用现有管线。
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import ProbeSpec, ServiceProvider, UsageSnapshot


API_BASE = "https://google.serper.dev"
SCRAPE_BASE = "https://scrape.serper.dev"

# REST 端点类型与其面向 MCP 客户端的工具映射。
_TOOL_ENDPOINTS: dict[str, str] = {
    "web_search": "/search",
    "news": "/news",
    "images": "/images",
    "videos": "/videos",
    "places": "/places",
    "shopping": "/shopping",
    "scholar": "/scholar",
    "patents": "/patents",
    "autocomplete": "/autocomplete",
    "maps": "/maps",
    "lens": "/lens",
    "scrape": "/scrape",
}


def _tool_spec(
    name: str,
    title: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    """构造一条 MCP tools/list 风格的工具定义（逐行紧凑，方便聚合透传）。"""
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


def _string_prop(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


def _integer_prop(desc: str, default: int | None = None) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": "integer", "description": desc}
    if default is not None:
        prop["default"] = default
    return prop


# 搜索类工具的公共参数（与官方 REST 字段一一对应）。
_SEARCH_PROPS: dict[str, Any] = {
    "q": _string_prop("搜索关键词（必填）"),
    "num": _integer_prop("返回条数，默认 10（news 上限 10）", 10),
    "page": _integer_prop("页码，从 1 开始", 1),
    "gl": _string_prop("国家代码，如 us、cn、gb"),
    "hl": _string_prop("界面语言代码，如 en、zh-cn"),
    "location": _string_prop("位置限定，如 San Francisco, California, United States"),
    "tbs": _string_prop("时间/类型筛选，如 qdr:d（近一天）、qdr:w（近一周）"),
    "autocorrect": {"type": "boolean", "description": "是否允许自动纠错（默认 true）"},
}


def build_serper_tools() -> list[dict[str, Any]]:
    """返回网关暴露的 serper MCP 工具清单（说明原文逐字透传）。"""
    search_props = dict(_SEARCH_PROPS)
    image_props = dict(_SEARCH_PROPS)
    return [
        _tool_spec(
            "web_search",
            "Serper Web Search",
            "使用 Serper 检索 Google 网页搜索结果。返回 organic 自然结果、"
            "knowledgeGraph 知识图谱、answerBox 直接答案、peopleAlsoAsk 与 relatedSearches。",
            search_props,
            ["q"],
        ),
        _tool_spec(
            "news",
            "Serper News Search",
            "使用 Serper 检索 Google 新闻。返回每条新闻的标题、链接、摘要、来源与日期。每页最多 10 条。",
            dict(_SEARCH_PROPS),
            ["q"],
        ),
        _tool_spec(
            "images",
            "Serper Image Search",
            "使用 Serper 检索 Google 图片。返回图片标题、页面链接、图片直链 imageUrl 与来源。",
            image_props,
            ["q"],
        ),
        _tool_spec(
            "videos",
            "Serper Video Search",
            "使用 Serper 检索 Google 视频。返回标题、链接、摘要、时长与来源（如 YouTube）。",
            dict(_SEARCH_PROPS),
            ["q"],
        ),
        _tool_spec(
            "places",
            "Serper Local Search",
            "使用 Serper 检索本地商户（Google Places/Maps）。返回名称、地址、经纬度、评分与评价数。"
            "location 参数可以直接指定城市或具体地名。",
            dict(_SEARCH_PROPS),
            ["q"],
        ),
        _tool_spec(
            "shopping",
            "Serper Shopping Search",
            "使用 Serper 检索 Google 购物。返回商品标题、来源商家、价格、运费与商品链接。",
            dict(_SEARCH_PROPS),
            ["q"],
        ),
        _tool_spec(
            "scholar",
            "Serper Scholar Search",
            "使用 Serper 检索 Google Scholar 学术文献。返回标题、链接、摘要、引用数与出版信息。",
            dict(_SEARCH_PROPS),
            ["q"],
        ),
        _tool_spec(
            "patents",
            "Serper Patent Search",
            "使用 Serper 检索 Google Patents 专利。返回专利标题、编号、摘要、发明人与申请信息。",
            dict(_SEARCH_PROPS),
            ["q"],
        ),
        _tool_spec(
            "autocomplete",
            "Serper Autocomplete",
            "使用 Serper 获取 Google 搜索联想建议。返回按热度排序的建议词列表。",
            {"q": _string_prop("输入前缀（必填）")},
            ["q"],
        ),
        _tool_spec(
            "maps",
            "Serper Maps Search",
            "使用 Serper 检索 Google 地图。返回地点列表、坐标与局部区域 ll 锚点。"
            "location 参数建议写详细地址以精确定位。",
            dict(_SEARCH_PROPS),
            ["q"],
        ),
        _tool_spec(
            "lens",
            "Serper Lens Search",
            "使用 Serper 以图搜图（Google Lens）。传入图片 URL，返回视觉相似的结果列表。",
            {"url": _string_prop("图片 URL（必填）。使用 num 控制条数。"),
             **{k: v for k, v in _SEARCH_PROPS.items() if k == "num"}},
            ["url"],
        ),
        _tool_spec(
            "scrape",
            "Serper Web Scrape",
            "使用 Serper 抓取任意网页正文。返回 text 纯文本、markdown（可选）、"
            "links（可选）与 metadata（含标题）。",
            {
                "url": _string_prop("待抓取的网页 URL（必填）"),
                "includeMarkdown": {
                    "type": "boolean",
                    "description": "是否返回 Markdown 正文（默认 false）",
                },
                "includeLinks": {
                    "type": "boolean",
                    "description": "是否返回页面外链列表（默认 false）",
                },
            },
            ["url"],
        ),
        _tool_spec(
            "credits",
            "Serper Credits Balance",
            "查询当前 Serper 密钥的剩余积分与每秒限速（不消费积分）。"
            "返回 balance（剩余积分）与 rateLimit（每秒请求上限）。",
            {},
            [],
        ),
    ]


async def execute_serper_tool(
    tool: str, arguments: dict[str, Any], key: str, timeout: float = 30.0
) -> tuple[int, str, dict[str, Any] | None]:
    """执行一次 serper 工具调用，返回 (HTTP 状态码, 正文文本, 结构化数据)。"""
    arguments = dict(arguments or {})
    if tool == "credits":
        return await _account(key, timeout)

    endpoint = _TOOL_ENDPOINTS.get(tool)
    if endpoint is None:
        err = {"message": f"未知工具: {tool}"}
        return 404, _json_text(err), err

    if endpoint == "/scrape":
        base, args = SCRAPE_BASE, arguments
    else:
        base, args = API_BASE, arguments
        if tool in ("autocomplete", "lens"):
            args = dict(args)

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(
                f"{base}{endpoint}",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json=args,
            )
    except httpx.HTTPError as exc:
        err = {"message": f"上游网络错误：{type(exc).__name__}: {exc}"}
        return 502, _json_text(err), err

    try:
        data: dict[str, Any] | None = resp.json()
    except ValueError:
        data = None
    return resp.status_code, resp.text, data


async def _account(key: str, timeout: float) -> tuple[int, str, dict[str, Any] | None]:
    """查询剩余额度。"""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(f"{API_BASE}/account", headers={"X-API-KEY": key})
    except httpx.HTTPError as exc:
        err = {"message": f"上游网络错误：{type(exc).__name__}: {exc}"}
        return 502, _json_text(err), err
    try:
        data: dict[str, Any] | None = resp.json()
    except ValueError:
        data = None
    return resp.status_code, resp.text, data


def _json_text(payload: dict[str, Any]) -> str:
    """构造紧凑的错误 JSON 文本。"""
    import json as _json

    return _json.dumps(payload, ensure_ascii=False)


class SerperProvider(ServiceProvider):
    """Serper 提供方（原生 MCP 服务）。"""

    service_name = "serper"
    supports_usage = True
    usage_url = f"{API_BASE}/account"
    native = True

    def get_probe(self) -> ProbeSpec:
        """返回最低成本的连通性探针（单条网页搜索，仅消费 1 积分）。"""
        return ProbeSpec(
            tool="web_search",
            arguments={"q": "ping", "num": 1},
        )

    async def fetch_usage(self, key: str, timeout: float = 15.0) -> UsageSnapshot:
        """查询 Serper 当前密钥的剩余积分与限速。"""
        status_code, text, data = await _account(key, timeout)
        if status_code == 502:
            return UsageSnapshot(status="error", detail=text or "上游网络错误")
        if status_code in (401, 403):
            return UsageSnapshot(status="error", detail=f"鉴权失败：HTTP {status_code}")
        if status_code >= 400 or not isinstance(data, dict):
            detail = text[:200] if text else f"查询失败：HTTP {status_code}"
            return UsageSnapshot(status="error", detail=detail)

        def _to_int(value: Any) -> int | None:
            if value is None or value == "":
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return UsageSnapshot(
            status="ok",
            balance=_to_int(data.get("balance")),
            rate_limit=_to_int(data.get("rateLimit")),
            account_plan="credits",
            raw=data,
        )

    def list_tools(self) -> list[dict[str, Any]]:
        """返回网关对外暴露的 serper MCP 工具。"""
        return build_serper_tools()

    async def call_tool(
        self, tool: str, arguments: dict[str, Any], key: str, timeout: float = 30.0
    ) -> tuple[int, Any]:
        """执行工具调用并返回 (HTTP 状态码, 供失败检测的结构化负载)。"""
        status_code, text, data = await execute_serper_tool(tool, arguments, key, timeout)
        if isinstance(data, dict):
            return status_code, data
        return status_code, {"message": text[:500] if text else f"HTTP {status_code}"}
