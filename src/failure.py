# -*- coding: utf-8 -*-
"""上游响应失败检测器。

由实测得出的事实驱动设计：
- Context7：密钥有效时 HTTP 200；无效/超限通常体现在 HTTP 状态码或 JSON-RPC error。
- Tavily：额度耗尽时 HTTP 仍为 200，且 JSON-RPC 无 error、isError 为 false，
  真正的错误（status 432 + "exceeds your plan"）被埋在 result.content[].text 的嵌套 JSON 字符串里。

因此判断「当前密钥是否失效」需要多层检测，任一层命中即判定失败并触发故障转移：
  1. HTTP 鉴权/限流状态码（401 / 403 / 429）。
  2. JSON-RPC 顶层 error（尤其是鉴权类错误码）。
  3. 响应正文（包含被透传文本）命中服务专属的 failure_patterns 关键词。

为避免误伤（例如用户搜索内容本身包含 "quota" 字样），关键词匹配应配置得足够特定，
这一点由 config.yaml 中各服务的 failure_patterns 控制。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .sse import parse_json_messages


# 明确指示「密钥层面」失败的 HTTP 状态码（值得换密钥重试）
KEY_LEVEL_HTTP_STATUS = {401, 402, 403, 429}


@dataclass
class FailureResult:
    """检测结果。"""

    is_failure: bool          # 是否判定为当前请求失败（需换密钥重试）
    is_key_failure: bool      # 是否密钥层面失效（需标记冷却/禁用）
    reason: str = ""          # 失败原因描述（用于日志）


def detect_failure(
    status_code: int,
    content_type: str | None,
    body_text: str,
    failure_patterns: list[str],
) -> FailureResult:
    """综合判断一次上游响应是否意味着当前密钥失效。

    Args:
        status_code: 上游 HTTP 状态码。
        content_type: 上游响应 Content-Type。
        body_text: 上游响应正文（已解码为文本）。
        failure_patterns: 该服务配置的失败特征关键词（建议已小写）。

    Returns:
        FailureResult，is_failure 为 True 表示应触发故障转移。
    """
    # 第 1 层：HTTP 鉴权/限流状态码 → 密钥失效
    if status_code in KEY_LEVEL_HTTP_STATUS:
        return FailureResult(True, True, f"HTTP 状态码 {status_code} 指示密钥失效/超限")

    # 5xx → 上游临时故障，触发重试但不惩罚密钥
    if status_code >= 500:
        return FailureResult(True, False, f"HTTP 状态码 {status_code} 上游临时故障")

    # 第 2 层：JSON-RPC 顶层 error
    messages = parse_json_messages(body_text, content_type)
    for msg in messages:
        err = msg.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            emsg = str(err.get("message", "")).lower()
            # 鉴权/限流相关的 JSON-RPC 错误
            if _looks_like_key_error(emsg):
                return FailureResult(True, True, f"JSON-RPC error: {err.get('message')}")
            # 其它带 error 的情况通常是协议/参数错误，不应换密钥（避免无意义重试）
            # 这里不判定为密钥失败，交由正文关键词层兜底
            _ = code

        result = msg.get("result")
        retryable = _extract_retryable_upstream_error(result)
        if retryable:
            return FailureResult(True, False, retryable)

    # 第 3 层：服务专属失败特征（针对 Tavily 这类 HTTP200 埋错的情况）
    if failure_patterns:
        haystack = body_text.lower()
        for pattern in failure_patterns:
            if pattern and pattern in haystack:
                return FailureResult(True, True, f"命中失败特征关键词: '{pattern}'")

    return FailureResult(False, False)


def _looks_like_key_error(message_lower: str) -> bool:
    """判断 JSON-RPC error message 是否属于密钥层面的错误。"""
    indicators = (
        "unauthorized",
        "api key",
        "apikey",
        "invalid key",
        "rate limit",
        "quota",
        "forbidden",
        "exceeds your plan",
        "usage limit",
    )
    return any(ind in message_lower for ind in indicators)


def _extract_retryable_upstream_error(result: object) -> str | None:
    """从工具结果中提取“上游临时错误”信号。"""
    if not isinstance(result, dict):
        return None

    candidates: list[tuple[int | None, str]] = []

    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        candidates.extend(_collect_status_and_text(sc))

    for item in result.get("content", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        candidates.extend(_collect_status_and_text(text))

    for status, text in candidates:
        if status is not None and status >= 500:
            return f"上游内部错误（状态 {status}）"
        if _looks_like_retryable_upstream_error(text.lower()):
            return f"上游临时错误：{text[:120]}"
    return None


def _collect_status_and_text(value: object) -> list[tuple[int | None, str]]:
    """从任意节点中递归提取状态码与文本。"""
    if isinstance(value, dict):
        status = value.get("status")
        detail = value.get("detail")
        error = value.get("error")
        text = json.dumps(value, ensure_ascii=False)
        items = [(status if isinstance(status, int) else None, text)]
        if detail is not None:
            items.extend(_collect_status_and_text(detail))
        if error is not None:
            items.extend(_collect_status_and_text(error))
        return items

    if isinstance(value, list):
        items: list[tuple[int | None, str]] = []
        for item in value:
            items.extend(_collect_status_and_text(item))
        return items

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:
            return [(None, text)]
        return _collect_status_and_text(parsed) + [(None, text)]

    return []


def _looks_like_retryable_upstream_error(message_lower: str) -> bool:
    """判断文本是否更像上游临时错误而不是密钥问题。"""
    indicators = (
        "internal error",
        "internal server error",
        "server error",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "temporarily unavailable",
        "upstream error",
        "try again later",
        "timeout",
    )
    return any(ind in message_lower for ind in indicators)
