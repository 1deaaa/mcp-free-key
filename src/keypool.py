# -*- coding: utf-8 -*-
"""上游密钥池：轮询负载均衡 + 故障转移 + 冷却恢复。

设计要点：
- 每个启用了密钥鉴权的服务持有一个 KeyPool。
- next_key() 以 round-robin 方式返回当前可用密钥，实现多账户负载均衡。
- mark_failed() 将某把密钥标记为冷却状态，冷却期内不再被选用。
- 冷却到期后密钥自动恢复可用，无需人工干预。
- 当所有密钥都处于冷却时，返回冷却结束最早的一把作为兜底
  （宁可一试也不直接断流，避免完全不可用）。
- 使用 asyncio.Lock 保证并发请求下选择与状态更新的一致性。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class _KeyState:
    """单把密钥的运行时状态。"""

    value: str                      # 密钥明文
    cooldown_until: float = 0.0     # 冷却截止时间戳（0 表示可用）
    fail_count: int = 0             # 累计失败次数（用于观测）
    success_count: int = 0          # 累计成功次数（用于观测）
    consecutive_fails: int = 0      # 连续失败次数
    is_disabled: bool = False       # 是否已被永久禁用（连续失败2次）

    def is_available(self, now: float) -> bool:
        """当前时刻是否可用。"""
        if self.is_disabled:
            # 禁用也有 cooldown_until（下月恢复），到期自动解除
            if now >= self.cooldown_until:
                self.is_disabled = False
                self.consecutive_fails = 0
                return True
            return False
        return now >= self.cooldown_until


@dataclass
class KeyPoolStats:
    """密钥池统计快照（供监控/调试）。"""

    total: int
    available: int
    cooling: int
    details: list[dict] = field(default_factory=list)


class KeyPool:
    """协程安全的密钥池。"""

    def __init__(self, keys: list[str], cooldown_seconds: int = 60) -> None:
        """初始化密钥池。

        Args:
            keys: 密钥明文列表（顺序即初始轮询顺序）。
            cooldown_seconds: 单把密钥失效后的冷却时长（秒）。
        """
        if not keys:
            raise ValueError("密钥池不能为空")
        self._states = [_KeyState(value=k) for k in keys]
        self._cooldown = cooldown_seconds
        self._cursor = 0  # round-robin 游标
        self._lock = asyncio.Lock()

    @property
    def size(self) -> int:
        """密钥总数。"""
        return len(self._states)

    async def next_key(self) -> str:
        """以轮询方式取下一把可用密钥。

        从当前游标开始扫描一圈，返回第一把可用密钥并推进游标。
        若全部在冷却中，则返回冷却结束最早的一把（兜底，不阻断服务）。
        注意：已被永久禁用的密钥绝不会被轮询到。

        Returns:
            选中的密钥明文。
        """
        async with self._lock:
            now = time.monotonic()
            n = len(self._states)
            # 从游标处开始扫描一整圈，寻找可用密钥
            for offset in range(n):
                idx = (self._cursor + offset) % n
                state = self._states[idx]
                if state.is_available(now):
                    # 推进游标到下一把，实现负载均衡
                    self._cursor = (idx + 1) % n
                    return state.value

            # 过滤掉已被永久禁用的密钥，只在冷却中的密钥里选最早恢复的
            active_states = [s for s in self._states if not s.is_disabled]
            if active_states:
                earliest = min(active_states, key=lambda s: s.cooldown_until)
                return earliest.value
            
            # 如果全部都被禁用了，抛出异常
            raise ValueError("所有密钥均已被永久禁用")

    async def next_key_excluding(self, exclude: set[str] | None = None) -> str | None:
        """以轮询方式取下一把可用且不在排除集合中的密钥。

        这个方法与 [`KeyPool.next_key()`](src/keypool.py:66) 的区别在于：
        - 会跳过本次请求已经尝试过的密钥；
        - 一旦选中，仍会推进轮询游标，确保故障转移路径也保持负载均衡。

        Args:
            exclude: 需要跳过的密钥集合。

        Returns:
            选中的密钥；若没有符合条件的密钥则返回 None。
        """
        exclude = exclude or set()
        async with self._lock:
            now = time.monotonic()
            n = len(self._states)
            for offset in range(n):
                idx = (self._cursor + offset) % n
                state = self._states[idx]
                if state.value in exclude:
                    continue
                if state.is_available(now):
                    self._cursor = (idx + 1) % n
                    return state.value
            return None

    @staticmethod
    def _next_month_utc_timestamp() -> float:
        """返回下个自然月 00:00 UTC 的 monotonic 时间戳。

        用 wall-clock 算出下月 1 号 00:00 UTC，再换算为 monotonic。
        """
        now_wall = datetime.now(timezone.utc)
        if now_wall.month == 12:
            next_month = now_wall.replace(year=now_wall.year + 1, month=1, day=1,
                                          hour=0, minute=0, second=0, microsecond=0)
        else:
            next_month = now_wall.replace(month=now_wall.month + 1, day=1,
                                          hour=0, minute=0, second=0, microsecond=0)
        delta = (next_month - now_wall).total_seconds()
        return time.monotonic() + delta

    async def mark_failed(self, key: str) -> None:
        """将指定密钥标记为失效。

        策略：
        - 首次失败：冷却 60 秒。
        - 连续第 2 次失败：禁用到下个自然月 00:00 UTC（额度刷新）。

        Args:
            key: 失效的密钥明文。
        """
        async with self._lock:
            now = time.monotonic()
            for state in self._states:
                if state.value == key:
                    state.fail_count += 1
                    state.consecutive_fails += 1
                    if state.consecutive_fails >= 2:
                        state.is_disabled = True
                        state.cooldown_until = self._next_month_utc_timestamp()
                    else:
                        state.cooldown_until = now + self._cooldown
                    break

    async def mark_success(self, key: str) -> None:
        """将指定密钥标记为成功，立即解除冷却（若有）。
        同时重置连续失败计数。

        Args:
            key: 成功的密钥明文。
        """
        async with self._lock:
            for state in self._states:
                if state.value == key:
                    state.cooldown_until = 0.0
                    state.success_count += 1
                    state.consecutive_fails = 0
                    break

    async def reset_key_state(self, key: str) -> None:
        """手动重置指定密钥的状态，解除禁用和冷却。

        Args:
            key: 密钥明文。
        """
        async with self._lock:
            for state in self._states:
                if state.value == key:
                    state.cooldown_until = 0.0
                    state.consecutive_fails = 0
                    state.is_disabled = False
                    break

    async def available_keys(self, exclude: set[str] | None = None) -> list[str]:
        """返回当前可用的密钥列表（用于故障转移时挑选未尝试过的密钥）。

        Args:
            exclude: 需要排除的密钥集合（如本次请求已尝试失败的）。

        Returns:
            可用且不在排除集合中的密钥列表，按轮询顺序排列。
        """
        exclude = exclude or set()
        async with self._lock:
            now = time.monotonic()
            n = len(self._states)
            result: list[str] = []
            for offset in range(n):
                idx = (self._cursor + offset) % n
                state = self._states[idx]
                if state.value in exclude:
                    continue
                if state.is_available(now):
                    result.append(state.value)
            return result

    async def stats(self) -> KeyPoolStats:
        """返回密钥池统计快照。"""
        async with self._lock:
            now = time.monotonic()
            available = sum(1 for s in self._states if s.is_available(now))
            details = [
                {
                    "tail": s.value[-6:],
                    "available": s.is_available(now),
                    "cooldown_remaining": max(0.0, round(s.cooldown_until - now, 1)),
                    "fail_count": s.fail_count,
                    "success_count": s.success_count,
                    "consecutive_fails": s.consecutive_fails,
                    "is_disabled": s.is_disabled,
                    "key": s.value,  # 传递完整 key 供 GUI 手动重置使用
                }
                for s in self._states
            ]
            return KeyPoolStats(
                total=len(self._states),
                available=available,
                cooling=len(self._states) - available,
                details=details,
            )
