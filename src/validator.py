# -*- coding: utf-8 -*-
"""密钥校验器：测试单个/批量密钥是否有效且有额度。

供 GUI 的「测试密钥」功能使用。

关键设计（基于实测事实）：
- 仅做 initialize 只能验证密钥被「接受」，无法发现额度耗尽
  （Tavily 额度耗尽时 initialize 仍返回 200）。
- 真正判断「可用且有额度」必须发起一次真实的 tools/call。
- 因此校验分两级：
    * 基础校验（auth）：initialize + tools/list，验证密钥被接受。
    * 深度校验（quota）：再发一次轻量探针工具调用，复用 failure 检测器
      判断是否额度耗尽 / 失效。
- 各服务的探针工具不同，内置 context7 / tavily 预设，
  未知服务可由调用方传入 ProbeSpec，否则只做基础校验。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .config import ServiceConfig
from .failure import detect_failure
from .sse import parse_json_messages


# 校验结果状态
STATUS_VALID = "valid"            # 有效且有额度
STATUS_QUOTA = "quota_exhausted"  # 被接受但额度耗尽/受限
STATUS_INVALID = "invalid"        # 密钥被拒绝（鉴权失败）
STATUS_ERROR = "error"            # 网络/解析等其它错误


@dataclass
class ProbeSpec:
    """深度校验时使用的探针工具调用定义。"""

    tool: str                    # 工具名
    arguments: dict[str, Any]    # 调用参数


# 内置探针预设：使用尽量轻量、参数最简的工具调用
# 注意：Context7 的 resolve-library-id 实测必填参数是 "query"（而非 libraryName）
DEFAULT_PROBES: dict[str, ProbeSpec] = {
    "context7": ProbeSpec(tool="resolve-library-id", arguments={"query": "react"}),
    "tavily": ProbeSpec(tool="tavily_search", arguments={"query": "ping", "max_results": 1}),
}


@dataclass
class ValidationResult:
    """单把密钥的校验结果。"""

    key: str                  # 被测密钥（明文）
    status: str               # 上述 STATUS_* 之一
    detail: str = ""          # 详情/错误信息
    latency_ms: int = 0       # 往返耗时（毫秒）

    @property
    def ok(self) -> bool:
        """是否可正常使用。"""
        return self.status == STATUS_VALID

    @property
    def key_tail(self) -> str:
        """密钥尾部（用于展示，避免泄露完整密钥）。"""
        return self.key[-6:] if len(self.key) > 6 else self.key


_COMMON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}


def _build_request_target(service: ServiceConfig, key: str) -> tuple[str, dict[str, str]]:
    """根据服务的鉴权方式，构造请求 URL 与请求头（注入密钥）。

    Returns:
        (url, headers) 二元组。
    """
    url = service.upstream_url
    headers = dict(_COMMON_HEADERS)
    if service.key_auth.enabled:
        if service.key_auth.type == "header":
            headers[service.key_auth.param] = key
        elif service.key_auth.type == "query":
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{service.key_auth.param}={key}"
    return url, headers


async def validate_key(
    service: ServiceConfig,
    key: str,
    probe: ProbeSpec | None = None,
    deep: bool = True,
    timeout: float = 30.0,
) -> ValidationResult:
    """校验单把密钥。

    Args:
        service: 服务配置（提供 URL 与鉴权方式、失败特征）。
        key: 待测密钥。
        probe: 深度校验使用的探针工具；为 None 时按服务名取内置预设。
        deep: 是否进行深度（额度）校验。False 时只验证鉴权是否通过。
        timeout: 单次请求超时（秒）。

    Returns:
        ValidationResult。
    """
    loop = asyncio.get_event_loop()
    start = loop.time()
    url, headers = _build_request_target(service, key)

    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "gateway-validator", "version": "1.0.0"},
        },
    }
    initialized_note = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}

    patterns = service.normalized_failure_patterns

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            # 1) initialize
            r_init = await client.post(url, headers=headers, json=init_body)
            if r_init.status_code in (401, 402, 403):
                return _result(key, STATUS_INVALID, f"initialize 返回 {r_init.status_code}（鉴权失败）", loop, start)

            fr_init = detect_failure(r_init.status_code, r_init.headers.get("content-type"), r_init.text, patterns)
            if fr_init.is_failure:
                # initialize 阶段就失败，多半是鉴权问题
                return _result(key, STATUS_INVALID, f"initialize 阶段失败：{fr_init.reason}", loop, start)

            # 取上游会话 id（有状态上游需要）
            sid = r_init.headers.get("mcp-session-id")
            sess_headers = dict(headers)
            if sid:
                sess_headers["Mcp-Session-Id"] = sid

            # 发送 initialized 通知（部分服务需要）
            try:
                await client.post(url, headers=sess_headers, json=initialized_note)
            except httpx.HTTPError:
                pass  # 通知失败不影响校验主流程

            # 2) 基础校验：tools/list
            tools_body = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            r_tools = await client.post(url, headers=sess_headers, json=tools_body)
            fr_tools = detect_failure(r_tools.status_code, r_tools.headers.get("content-type"), r_tools.text, patterns)
            if fr_tools.is_failure:
                return _result(key, STATUS_INVALID, f"tools/list 失败：{fr_tools.reason}", loop, start)

            if not deep:
                return _result(key, STATUS_VALID, "鉴权通过（未做额度校验）", loop, start)

            # 3) 深度校验：发起真实探针工具调用以检测额度
            probe = probe or DEFAULT_PROBES.get(service.name)
            if probe is None:
                # 无可用探针，退化为基础校验结果
                return _result(key, STATUS_VALID, "鉴权通过（无探针，未做额度校验）", loop, start)

            call_body = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": probe.tool, "arguments": probe.arguments},
            }
            r_call = await client.post(url, headers=sess_headers, json=call_body)
            fr_call = detect_failure(r_call.status_code, r_call.headers.get("content-type"), r_call.text, patterns)
            if fr_call.is_failure:
                return _result(key, STATUS_QUOTA, f"额度耗尽/受限：{fr_call.reason}", loop, start)

            # 深度检查：即便没命中 failure_patterns，也扫一遍 content 文本中的常见超限提示
            extra = _scan_tool_result_for_quota(r_call.text, r_call.headers.get("content-type"))
            if extra:
                return _result(key, STATUS_QUOTA, f"额度耗尽/受限：{extra}", loop, start)

            return _result(key, STATUS_VALID, "有效且有额度", loop, start)

    except httpx.HTTPError as e:
        return _result(key, STATUS_ERROR, f"网络错误：{type(e).__name__}: {e}", loop, start)
    except Exception as e:  # noqa: BLE001 - 校验器需对任何异常稳健
        return _result(key, STATUS_ERROR, f"未知错误：{type(e).__name__}: {e}", loop, start)


def _scan_tool_result_for_quota(body_text: str, content_type: str | None) -> str | None:
    """扫描工具调用结果文本中是否含超限信号（兜底，针对嵌套 JSON 的情况）。

    Returns:
        命中时返回简短描述，否则 None。
    """
    messages = parse_json_messages(body_text, content_type)
    indicators = ("exceeds your plan", "usage limit", "upgrade your plan", "rate limit", "quota")
    for msg in messages:
        result = msg.get("result")
        if not isinstance(result, dict):
            continue
        # 检查 structuredContent
        sc = result.get("structuredContent")
        text_blob = json.dumps(sc, ensure_ascii=False).lower() if sc else ""
        # 检查 content[].text
        for item in result.get("content", []) or []:
            if isinstance(item, dict) and item.get("type") == "text":
                text_blob += " " + str(item.get("text", "")).lower()
        for ind in indicators:
            if ind in text_blob:
                return f"响应含超限提示 '{ind}'"
    return None


def _result(key: str, status: str, detail: str, loop, start) -> ValidationResult:
    """构造结果并计算耗时。"""
    latency = int((loop.time() - start) * 1000)
    return ValidationResult(key=key, status=status, detail=detail, latency_ms=latency)


async def validate_keys(
    service: ServiceConfig,
    keys: list[str],
    deep: bool = True,
    concurrency: int = 5,
    timeout: float = 30.0,
) -> list[ValidationResult]:
    """批量并发校验多把密钥。

    Args:
        service: 服务配置。
        keys: 待测密钥列表。
        deep: 是否深度校验额度。
        concurrency: 最大并发数。
        timeout: 单次请求超时。

    Returns:
        与 keys 顺序一致的结果列表。
    """
    sem = asyncio.Semaphore(concurrency)

    async def _one(k: str) -> ValidationResult:
        async with sem:
            return await validate_key(service, k, deep=deep, timeout=timeout)

    return await asyncio.gather(*[_one(k) for k in keys])
