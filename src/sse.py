# -*- coding: utf-8 -*-
"""SSE（Server-Sent Events）解析工具。

MCP 的 Streamable HTTP 传输在响应工具调用时通常返回 text/event-stream，
但实测 Context7 与 Tavily 的响应均为「一次性单事件」（单条 message 后即关闭）。

本模块负责从 SSE 原始文本中提取出 data 字段拼接的 JSON 文本，
用于故障检测时分析上游真实返回内容。注意：检测逻辑只读取内容，
不改变最终透传给客户端的原始字节，避免破坏协议。
"""
from __future__ import annotations

import json
from typing import Any


def extract_data_payloads(raw_text: str) -> list[str]:
    """从 SSE 原始文本中提取所有事件的 data 内容。

    SSE 格式中，每个事件由若干 `data:` 行组成，多行 data 用换行拼接，
    事件之间用空行分隔。

    Args:
        raw_text: 上游返回的 SSE 原始文本。

    Returns:
        每个事件对应的 data 字符串列表。
    """
    events: list[str] = []
    current: list[str] = []

    for line in raw_text.splitlines():
        if line.startswith("data:"):
            # 去掉 "data:" 前缀，保留一个可选空格后的内容
            current.append(line[5:].lstrip(" "))
        elif line.strip() == "":
            # 空行表示一个事件结束
            if current:
                events.append("\n".join(current))
                current = []
        # 其它字段（event:, id:, retry:）对内容检测无影响，忽略

    if current:
        events.append("\n".join(current))

    return events


def parse_json_messages(raw_text: str, content_type: str | None) -> list[dict[str, Any]]:
    """将上游响应正文解析为 JSON-RPC 消息列表。

    同时兼容两种情况：
    - content_type 为 application/json：整个正文是一个 JSON 对象。
    - content_type 为 text/event-stream：从各事件的 data 中解析 JSON。

    解析失败的片段会被静默跳过（返回能成功解析的部分）。

    Args:
        raw_text: 响应正文文本。
        content_type: 响应的 Content-Type 头。

    Returns:
        成功解析出的 JSON 对象列表。
    """
    messages: list[dict[str, Any]] = []
    ctype = (content_type or "").lower()

    if "text/event-stream" in ctype:
        payloads = extract_data_payloads(raw_text)
    else:
        # 当作单个 JSON 处理
        payloads = [raw_text.strip()] if raw_text.strip() else []

    for payload in payloads:
        if not payload:
            continue
        try:
            obj = json.loads(payload)
            if isinstance(obj, dict):
                messages.append(obj)
        except (json.JSONDecodeError, ValueError):
            continue

    return messages
