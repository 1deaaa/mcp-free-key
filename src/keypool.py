# -*- coding: utf-8 -*-
"""上游密钥池：轮询、主备切换、冷却与自动复测。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .config import (
    DEFAULT_KEY_COOLDOWN_SECONDS,
    ROUTING_MODE_PRIMARY_BACKUP,
    ROUTING_MODE_ROUND_ROBIN,
    ROUTING_MODES,
)
from .key_state import KeyStateStore


class NoAvailableKeyError(ValueError):
    """当前没有可以立即使用的密钥。"""


@dataclass
class _KeyState:
    """单把密钥的运行时状态。"""

    value: str
    cooldown_until: float = 0.0
    fail_count: int = 0
    success_count: int = 0
    consecutive_fails: int = 0
    is_disabled: bool = False
    retest_pending: bool = False

    def is_available(self, now: float) -> bool:
        """当前时刻是否可用。"""
        # 持久化禁用和等待复测都不能直接回到请求路径。
        return not self.is_disabled and now >= self.cooldown_until


@dataclass
class KeyPoolStats:
    """密钥池统计快照（供监控和 GUI 使用）。"""

    total: int
    available: int
    cooling: int
    details: list[dict] = field(default_factory=list)


class KeyPool:
    """协程安全的密钥池。"""

    def __init__(
        self,
        keys: list[str],
        cooldown_seconds: int = DEFAULT_KEY_COOLDOWN_SECONDS,
        *,
        service_name: str = "",
        state_store: KeyStateStore | None = None,
        routing_mode: str = ROUTING_MODE_ROUND_ROBIN,
    ) -> None:
        """初始化密钥池。"""
        if not keys:
            raise ValueError("密钥池不能为空")
        if routing_mode not in ROUTING_MODES:
            raise ValueError(f"不支持的路由模式：{routing_mode}")
        self._states = [_KeyState(value=key) for key in keys]
        self._cooldown = max(0.0, float(cooldown_seconds))
        self._service_name = service_name
        self._state_store = state_store
        self._routing_mode = routing_mode
        self._cursor = 0
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        """密钥总数。"""
        return len(self._states)

    @property
    def routing_mode(self) -> str:
        """当前路由模式。"""
        return self._routing_mode

    async def next_key(self) -> str:
        """返回一把当前可用的密钥。"""
        async with self._lock:
            self._sync_persisted_states_locked()
            now = time.monotonic()
            for idx in self._ordered_indices_locked():
                state = self._states[idx]
                if state.is_available(now):
                    self._advance_cursor_locked(idx)
                    return state.value
            raise NoAvailableKeyError("没有可立即使用的密钥，等待冷却结束后将自动复测")

    async def next_key_excluding(self, exclude: set[str] | None = None) -> str | None:
        """返回一把当前可用且不在排除集合中的密钥。"""
        excluded = exclude or set()
        async with self._lock:
            self._sync_persisted_states_locked()
            now = time.monotonic()
            for idx in self._ordered_indices_locked():
                state = self._states[idx]
                if state.value in excluded or not state.is_available(now):
                    continue
                self._advance_cursor_locked(idx)
                return state.value
            return None

    async def mark_failed(
        self,
        key: str,
        *,
        reason: str = "key_failure",
        raw_error: str = "",
        raw_status_code: int | None = None,
    ) -> None:
        """标记密钥失败并进入冷却；冷却结束后只允许自动复测。"""
        async with self._lock:
            self._sync_persisted_states_locked()
            state = self._find_state(key)
            if state is None:
                return

            state.fail_count += 1
            state.consecutive_fails += 1
            state.cooldown_until = time.monotonic() + self._cooldown
            state.is_disabled = True
            state.retest_pending = True

            if self._state_store and self._service_name:
                self._state_store.disable_key_temporarily(
                    self._service_name,
                    key,
                    cooldown_seconds=self._cooldown,
                    reason=reason,
                    fail_count=state.fail_count,
                    consecutive_fails=state.consecutive_fails,
                    raw_error=raw_error,
                    raw_status_code=raw_status_code,
                )

    async def mark_success(self, key: str) -> None:
        """标记正常请求成功，恢复密钥并记录本月成功次数。"""
        async with self._lock:
            self._sync_persisted_states_locked()
            state = self._find_state(key)
            if state is None:
                return
            state.cooldown_until = 0.0
            state.success_count += 1
            state.consecutive_fails = 0
            state.is_disabled = False
            state.retest_pending = False
            if self._state_store and self._service_name:
                self._state_store.clear_key_failure_and_record_success(
                    self._service_name,
                    key,
                )

    async def mark_retest_success(self, key: str) -> None:
        """自动复测成功后恢复密钥，但不把复测请求计入业务成功次数。"""
        async with self._lock:
            self._sync_persisted_states_locked()
            state = self._find_state(key)
            if state is None:
                return
            state.cooldown_until = 0.0
            state.consecutive_fails = 0
            state.is_disabled = False
            state.retest_pending = False
            if self._state_store and self._service_name:
                self._state_store.reset_key(self._service_name, key)

    async def mark_retest_failed(
        self,
        key: str,
        *,
        reason: str,
        raw_error: str = "",
        raw_status_code: int | None = None,
    ) -> None:
        """自动复测失败后永久禁用密钥并保存原始错误。"""
        async with self._lock:
            self._sync_persisted_states_locked()
            state = self._find_state(key)
            if state is None:
                return
            state.fail_count += 1
            state.consecutive_fails += 1
            state.cooldown_until = 0.0
            state.is_disabled = True
            state.retest_pending = False
            if self._state_store and self._service_name:
                self._state_store.disable_key_permanently(
                    self._service_name,
                    key,
                    reason=reason,
                    raw_error=raw_error,
                    raw_status_code=raw_status_code,
                    fail_count=state.fail_count,
                    consecutive_fails=state.consecutive_fails,
                )

    async def reset_key_state(self, key: str) -> bool:
        """手动恢复指定密钥，解除禁用和冷却。"""
        async with self._lock:
            self._sync_persisted_states_locked()
            state = self._find_state(key)
            if state is None:
                return False
            state.cooldown_until = 0.0
            state.consecutive_fails = 0
            state.is_disabled = False
            state.retest_pending = False
            if self._state_store and self._service_name:
                self._state_store.reset_key(self._service_name, key)
            return True

    async def available_keys(self, exclude: set[str] | None = None) -> list[str]:
        """返回当前可用密钥，顺序遵循当前路由模式。"""
        excluded = exclude or set()
        async with self._lock:
            self._sync_persisted_states_locked()
            now = time.monotonic()
            return [
                self._states[idx].value
                for idx in self._ordered_indices_locked()
                if self._states[idx].value not in excluded
                and self._states[idx].is_available(now)
            ]

    async def pending_retest_keys(self) -> list[str]:
        """返回已结束冷却、等待自动复测的密钥。"""
        if not self._state_store or not self._service_name:
            now = time.monotonic()
            async with self._lock:
                return [
                    state.value
                    for state in self._states
                    if state.is_disabled
                    and state.retest_pending
                    and state.cooldown_until <= now
                ]
        async with self._lock:
            self._sync_persisted_states_locked()
            candidates = self._state_store.get_retest_candidates(
                self._service_name,
                [state.value for state in self._states],
            )
            return [state.value for state in self._states if state.value in candidates]

    async def primary_key_tail(self) -> str:
        """返回当前主密钥尾部；没有可用主密钥时返回空字符串。"""
        async with self._lock:
            self._sync_persisted_states_locked()
            now = time.monotonic()
            for idx in self._ordered_indices_locked():
                if self._states[idx].is_available(now):
                    return self._states[idx].value[-6:]
            return ""

    async def stats(self) -> KeyPoolStats:
        """返回密钥池统计快照。"""
        async with self._lock:
            self._sync_persisted_states_locked()
            now = time.monotonic()
            records = {}
            if self._state_store and self._service_name:
                records = self._state_store.get_key_records(
                    self._service_name,
                    [state.value for state in self._states],
                )

            details: list[dict] = []
            for state in self._states:
                stored = records.get(state.value)
                cooldown_remaining = (
                    max(0.0, round(state.cooldown_until - now, 1))
                    if state.cooldown_until != float("inf")
                    else 0.0
                )
                details.append(
                    {
                        "tail": state.value[-6:],
                        "available": state.is_available(now),
                        "cooldown_remaining": cooldown_remaining,
                        "fail_count": state.fail_count,
                        "success_count": state.success_count,
                        "consecutive_fails": state.consecutive_fails,
                        "is_disabled": state.is_disabled,
                        "retest_pending": state.retest_pending,
                        "retest_due": (
                            stored.is_retest_due
                            if stored is not None
                            else state.is_disabled
                            and state.retest_pending
                            and state.cooldown_until <= now
                        ),
                        "monthly_success_count": stored.monthly_success_count if stored else 0,
                        "raw_error": stored.raw_error if stored else "",
                        "raw_status_code": stored.raw_status_code if stored else None,
                        # GUI 的重置接口需要精确定位密钥；统计接口本身已受网关密钥保护。
                        "key": state.value,
                    }
                )

            available = sum(1 for state in self._states if state.is_available(now))
            return KeyPoolStats(
                total=len(self._states),
                available=available,
                cooling=len(self._states) - available,
                details=details,
            )

    def _find_state(self, key: str) -> _KeyState | None:
        """按明文查找运行时状态。"""
        return next((state for state in self._states if state.value == key), None)

    def _ordered_indices_locked(self) -> list[int]:
        """生成当前模式下的扫描顺序，调用方需已持锁。"""
        n = len(self._states)
        if self._routing_mode == ROUTING_MODE_PRIMARY_BACKUP:
            return list(range(n))
        return [(self._cursor + offset) % n for offset in range(n)]

    def _advance_cursor_locked(self, index: int) -> None:
        """推进轮询游标；主备模式不改变主备顺序。"""
        if self._routing_mode == ROUTING_MODE_ROUND_ROBIN:
            self._cursor = (index + 1) % len(self._states)

    def _sync_persisted_states_locked(self) -> None:
        """将本地持久化状态同步到内存。"""
        if not self._state_store or not self._service_name:
            return

        records = self._state_store.get_key_records(
            self._service_name,
            [state.value for state in self._states],
        )
        now_mono = time.monotonic()
        now_wall = time.time()
        for state in self._states:
            stored = records.get(state.value)
            if stored and stored.is_disabled:
                state.is_disabled = True
                state.retest_pending = stored.retest_pending
                if stored.disabled_until_epoch > now_wall:
                    state.cooldown_until = now_mono + stored.disabled_until_epoch - now_wall
                elif stored.retest_pending:
                    # 已到期但等待复测：使用无穷远，防止误回池。
                    state.cooldown_until = float("inf")
                else:
                    state.cooldown_until = 0.0
                state.fail_count = max(state.fail_count, stored.fail_count)
                state.consecutive_fails = max(state.consecutive_fails, stored.consecutive_fails)
            else:
                state.is_disabled = False
                state.retest_pending = False
                state.cooldown_until = 0.0
                state.consecutive_fails = 0
