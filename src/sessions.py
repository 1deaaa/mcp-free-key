# -*- coding: utf-8 -*-
"""会话映射：将上游返回的 Mcp-Session-Id 绑定到所用密钥。

背景（实测得出）：
- Context7 是有状态上游，initialize 后返回 Mcp-Session-Id，
  后续请求必须带相同 session-id，且应使用同一把密钥
  （否则上游会因会话与账户不匹配而拒绝）。
- Tavily 是无状态上游，不返回 session-id，无需会话粘连，可请求级轮询。

本模块维护 session_id -> key 的映射，并带 TTL 空闲淘汰，
每次访问刷新最后活跃时间，过期条目惰性清理。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass
class _Entry:
    """单条会话映射。"""

    key: str           # 该会话绑定的上游密钥
    last_active: float  # 最后活跃时间戳（monotonic）


class SessionStore:
    """协程安全的会话-密钥映射表，带 TTL 空闲淘汰。"""

    def __init__(self, ttl_seconds: int = 1800) -> None:
        """初始化。

        Args:
            ttl_seconds: 会话空闲多久后被淘汰（秒）。
        """
        self._ttl = ttl_seconds
        self._map: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    async def bind(self, session_id: str, key: str) -> None:
        """绑定会话到密钥（initialize 成功后调用）。

        Args:
            session_id: 上游返回的 Mcp-Session-Id。
            key: 本次会话所用的上游密钥。
        """
        if not session_id:
            return
        async with self._lock:
            self._map[session_id] = _Entry(key=key, last_active=time.monotonic())

    async def get_key(self, session_id: str) -> str | None:
        """获取会话绑定的密钥，并刷新活跃时间。

        Args:
            session_id: 客户端请求携带的 Mcp-Session-Id。

        Returns:
            绑定的密钥；不存在或已过期则返回 None。
        """
        if not session_id:
            return None
        async with self._lock:
            self._evict_expired_locked()
            entry = self._map.get(session_id)
            if entry is None:
                return None
            entry.last_active = time.monotonic()
            return entry.key

    async def unbind(self, session_id: str) -> None:
        """解除会话绑定（会话结束 / DELETE 时调用）。

        Args:
            session_id: 要解绑的会话 id。
        """
        if not session_id:
            return
        async with self._lock:
            self._map.pop(session_id, None)

    async def size(self) -> int:
        """返回当前有效会话数（会顺便清理过期）。"""
        async with self._lock:
            self._evict_expired_locked()
            return len(self._map)

    def _evict_expired_locked(self) -> None:
        """清理过期会话（调用方需已持锁）。"""
        now = time.monotonic()
        expired = [sid for sid, e in self._map.items() if now - e.last_active > self._ttl]
        for sid in expired:
            del self._map[sid]
