# -*- coding: utf-8 -*-
"""本地密钥状态持久化。

职责：
- 将“已废弃/按月恢复”的密钥状态写入本地 JSON 文件；
- 供网关进程与 GUI 进程共享读取；
- 在自然月初自动清理已到期的废弃状态。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


DEFAULT_KEY_STATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".key_state.json"
)


@dataclass
class PersistedKeyState:
    """单把密钥的持久化状态。"""

    is_disabled: bool = False
    disabled_until_epoch: float = 0.0
    reason: str = ""
    fail_count: int = 0
    consecutive_fails: int = 0
    updated_at: str = ""
    raw_error: str = ""
    raw_status_code: int | None = None
    retest_pending: bool = False
    monthly_period: str = ""
    monthly_success_count: int = 0

    @property
    def is_active(self) -> bool:
        """当前是否仍处于废弃状态。"""
        return self.is_disabled and (
            self.disabled_until_epoch <= 0 or self.disabled_until_epoch > time.time()
        )

    @property
    def is_retest_due(self) -> bool:
        """是否已结束冷却并等待自动复测。"""
        return bool(
            self.is_disabled
            and self.retest_pending
            and self.disabled_until_epoch > 0
            and self.disabled_until_epoch <= time.time()
        )


def current_utc_period(now: datetime | None = None) -> str:
    """返回当前 UTC 自然月标识。"""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).strftime("%Y-%m")


def next_month_start_epoch(now: datetime | None = None) -> float:
    """返回本地时区下个自然月 1 号 00:00 的时间戳。"""
    now = now or datetime.now().astimezone()
    if now.month == 12:
        next_month = now.replace(
            year=now.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    else:
        next_month = now.replace(
            month=now.month + 1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return next_month.timestamp()


class KeyStateStore:
    """基于本地 JSON 文件的密钥状态存储。"""

    def __init__(self, path: str = DEFAULT_KEY_STATE_PATH) -> None:
        self.path = path
        self._cache: dict[str, Any] = {"version": 1, "services": {}}
        self._mtime_ns: int | None = None

    def get_service_states(self, service_name: str, keys: list[str] | None = None) -> dict[str, PersistedKeyState]:
        """读取某个服务下的持久化状态。"""
        data = self._load()
        self._cleanup_expired(data)
        service_keys = (((data.get("services") or {}).get(service_name) or {}).get("keys") or {})

        if keys is not None:
            keep = set(keys)
            stale = [key for key in service_keys if key not in keep]
            if stale:
                for key in stale:
                    service_keys.pop(key, None)
                self._save(data)

        result: dict[str, PersistedKeyState] = {}
        for key, raw in service_keys.items():
            state = self._parse_state(raw)
            if state.is_active:
                result[key] = state
        return result

    def set_key_disabled(
        self,
        service_name: str,
        key: str,
        *,
        disabled_until_epoch: float,
        reason: str,
        fail_count: int,
        consecutive_fails: int,
        raw_error: str = "",
        raw_status_code: int | None = None,
        retest_pending: bool = False,
    ) -> None:
        """写入某把密钥的废弃状态。"""
        data = self._load()
        self._cleanup_expired(data)
        services = data.setdefault("services", {})
        service = services.setdefault(service_name, {})
        keys = service.setdefault("keys", {})
        old_state = self._parse_state(keys.get(key))
        period, monthly_count = self._normalized_monthly_values(old_state)
        keys[key] = {
            "is_disabled": True,
            "disabled_until_epoch": float(disabled_until_epoch),
            "reason": reason,
            "fail_count": int(fail_count),
            "consecutive_fails": int(consecutive_fails),
            "updated_at": datetime.now().astimezone().isoformat(),
            "raw_error": str(raw_error or ""),
            "raw_status_code": raw_status_code,
            "retest_pending": bool(retest_pending),
            "monthly_period": period,
            "monthly_success_count": monthly_count,
        }
        self._save(data)

    def disable_key_temporarily(
        self,
        service_name: str,
        key: str,
        *,
        cooldown_seconds: float,
        reason: str,
        fail_count: int,
        consecutive_fails: int,
        raw_error: str = "",
        raw_status_code: int | None = None,
    ) -> None:
        """暂时禁用密钥，冷却结束后等待自动复测。"""
        self.set_key_disabled(
            service_name,
            key,
            disabled_until_epoch=time.time() + max(0.0, float(cooldown_seconds)),
            reason=reason,
            fail_count=fail_count,
            consecutive_fails=consecutive_fails,
            raw_error=raw_error,
            raw_status_code=raw_status_code,
            retest_pending=True,
        )

    def disable_key_permanently(
        self,
        service_name: str,
        key: str,
        *,
        reason: str,
        raw_error: str = "",
        raw_status_code: int | None = None,
        fail_count: int = 1,
        consecutive_fails: int = 1,
    ) -> None:
        """将密钥永久失效，直到用户手动恢复。"""
        self.set_key_disabled(
            service_name,
            key,
            disabled_until_epoch=0.0,
            reason=reason,
            fail_count=fail_count,
            consecutive_fails=consecutive_fails,
            raw_error=raw_error,
            raw_status_code=raw_status_code,
            retest_pending=False,
        )

    def disable_key_until_next_month(
        self,
        service_name: str,
        key: str,
        *,
        reason: str,
        raw_error: str = "",
        raw_status_code: int | None = None,
    ) -> None:
        """将密钥禁用到下个自然月，供手动测试等明确失败场景使用。"""
        self.set_key_disabled(
            service_name,
            key,
            disabled_until_epoch=next_month_start_epoch(),
            reason=reason,
            fail_count=1,
            consecutive_fails=1,
            raw_error=raw_error,
            raw_status_code=raw_status_code,
            retest_pending=False,
        )

    def record_success(self, service_name: str, key: str) -> int:
        """记录一笔成功请求并返回该 key 本月成功次数。"""
        data = self._load()
        self._cleanup_expired(data)
        services = data.setdefault("services", {})
        service = services.setdefault(service_name, {})
        keys = service.setdefault("keys", {})
        state = self._parse_state(keys.get(key))
        period, monthly_count = self._normalized_monthly_values(state)
        monthly_count += 1
        keys[key] = self._state_payload(
            state,
            is_disabled=state.is_disabled,
            disabled_until_epoch=state.disabled_until_epoch,
            reason=state.reason,
            fail_count=state.fail_count,
            consecutive_fails=state.consecutive_fails,
            raw_error=state.raw_error,
            raw_status_code=state.raw_status_code,
            retest_pending=state.retest_pending,
            monthly_period=period,
            monthly_success_count=monthly_count,
        )
        self._save(data)
        return monthly_count

    def clear_key_failure_and_record_success(self, service_name: str, key: str) -> int:
        """清除失效状态并记录一笔成功请求。"""
        data = self._load()
        self._cleanup_expired(data)
        services = data.setdefault("services", {})
        service = services.setdefault(service_name, {})
        keys = service.setdefault("keys", {})
        state = self._parse_state(keys.get(key))
        period, monthly_count = self._normalized_monthly_values(state)
        monthly_count += 1
        keys[key] = self._state_payload(
            state,
            is_disabled=False,
            disabled_until_epoch=0.0,
            reason="",
            fail_count=0,
            consecutive_fails=0,
            updated_at="",
            raw_error="",
            raw_status_code=None,
            retest_pending=False,
            monthly_period=period,
            monthly_success_count=monthly_count,
        )
        self._save(data)
        return monthly_count

    def get_key_records(self, service_name: str, keys: list[str] | None = None) -> dict[str, PersistedKeyState]:
        """读取密钥的完整状态，包括月度成功次数和最近原始错误。"""
        data = self._load()
        self._cleanup_expired(data)
        service_keys = (((data.get("services") or {}).get(service_name) or {}).get("keys") or {})
        selected = keys if keys is not None else list(service_keys)
        result: dict[str, PersistedKeyState] = {}
        changed = False
        for key in selected:
            state = self._parse_state(service_keys.get(key))
            period, monthly_count = self._normalized_monthly_values(state)
            if state.monthly_period != period or state.monthly_success_count != monthly_count:
                if key in service_keys:
                    service_keys[key] = self._state_payload(
                        state,
                        monthly_period=period,
                        monthly_success_count=monthly_count,
                    )
                    changed = True
                state.monthly_period = period
                state.monthly_success_count = monthly_count
            result[key] = state
        if changed:
            self._save(data)
        return result

    def get_retest_candidates(
        self,
        service_name: str,
        keys: list[str] | None = None,
    ) -> dict[str, PersistedKeyState]:
        """读取已结束冷却、等待自动复测的密钥。"""
        records = self.get_key_records(service_name, keys)
        return {key: state for key, state in records.items() if state.is_retest_due}

    def reset_key(self, service_name: str, key: str) -> None:
        """清除某把密钥的失效状态，但保留本月成功次数。"""
        data = self._load()
        services = data.get("services") or {}
        service = services.get(service_name) or {}
        keys = service.get("keys") or {}
        if key in keys:
            state = self._parse_state(keys[key])
            period, monthly_count = self._normalized_monthly_values(state)
            if monthly_count:
                keys[key] = self._state_payload(
                    state,
                    is_disabled=False,
                    disabled_until_epoch=0.0,
                    reason="",
                    fail_count=0,
                    consecutive_fails=0,
                    updated_at="",
                    raw_error="",
                    raw_status_code=None,
                    retest_pending=False,
                    monthly_period=period,
                    monthly_success_count=monthly_count,
                )
            else:
                keys.pop(key, None)
                if not keys:
                    service.pop("keys", None)
                if not service:
                    services.pop(service_name, None)
            self._save(data)

    def build_key_map(self, service_name: str, keys: list[str]) -> dict[str, dict[str, Any]]:
        """返回适合 GUI 展示的状态映射。"""
        states = self.get_key_records(service_name, keys)
        now = time.time()
        return {
            key: {
                "is_disabled": state.is_active,
                "disabled_until_epoch": state.disabled_until_epoch,
                "disabled_remaining": max(0.0, round(state.disabled_until_epoch - now, 1)),
                "reason": state.reason,
                "fail_count": state.fail_count,
                "consecutive_fails": state.consecutive_fails,
                "updated_at": state.updated_at,
                "raw_error": state.raw_error,
                "raw_status_code": state.raw_status_code,
                "retest_pending": state.retest_pending,
                "retest_due": state.is_retest_due,
                "monthly_period": state.monthly_period,
                "monthly_success_count": state.monthly_success_count,
            }
            for key, state in states.items()
        }

    def _load(self) -> dict[str, Any]:
        """按需读取文件，并复用缓存。"""
        try:
            stat = os.stat(self.path)
            mtime_ns = stat.st_mtime_ns
        except FileNotFoundError:
            self._cache = {"version": 1, "services": {}}
            self._mtime_ns = None
            return self._cache

        if self._mtime_ns == mtime_ns:
            return self._cache

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            data = {}

        if not isinstance(data, dict):
            data = {}
        data.setdefault("version", 1)
        data.setdefault("services", {})

        self._cache = data
        self._mtime_ns = mtime_ns
        return data

    def _save(self, data: dict[str, Any]) -> None:
        """原子写回文件。"""
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".key_state.", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(temp_path, self.path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

        self._cache = data
        self._mtime_ns = os.stat(self.path).st_mtime_ns

    def _cleanup_expired(self, data: dict[str, Any]) -> None:
        """清理已到期的非复测状态，保留待自动复测记录。"""
        changed = False
        now = time.time()
        services = data.get("services") or {}
        for service_name in list(services.keys()):
            service = services.get(service_name) or {}
            keys = service.get("keys") or {}
            for key in list(keys.keys()):
                state = self._parse_state(keys[key])
                if not state.is_disabled:
                    continue
                if state.disabled_until_epoch <= 0 or state.disabled_until_epoch > now:
                    continue
                if state.retest_pending:
                    # 冷却结束的临时禁用必须保留，供网关自动复测任务领取。
                    continue
                period, monthly_count = self._normalized_monthly_values(state)
                if monthly_count:
                    keys[key] = self._state_payload(
                        state,
                        is_disabled=False,
                        disabled_until_epoch=0.0,
                        reason="",
                        fail_count=0,
                        consecutive_fails=0,
                        updated_at="",
                        raw_error="",
                        raw_status_code=None,
                        retest_pending=False,
                        monthly_period=period,
                        monthly_success_count=monthly_count,
                    )
                else:
                    keys.pop(key, None)
                changed = True
            if not keys:
                services.pop(service_name, None)
                changed = True
        if changed:
            self._save(data)

    @staticmethod
    def _parse_state(raw: Any) -> PersistedKeyState:
        """将原始 JSON 节点解析为状态对象。"""
        raw = raw if isinstance(raw, dict) else {}
        raw_status_code = raw.get("raw_status_code")
        try:
            parsed_status_code = int(raw_status_code) if raw_status_code is not None else None
        except (TypeError, ValueError):
            parsed_status_code = None
        return PersistedKeyState(
            is_disabled=bool(raw.get("is_disabled", False)),
            disabled_until_epoch=float(raw.get("disabled_until_epoch", 0.0) or 0.0),
            reason=str(raw.get("reason", "")),
            fail_count=int(raw.get("fail_count", 0) or 0),
            consecutive_fails=int(raw.get("consecutive_fails", 0) or 0),
            updated_at=str(raw.get("updated_at", "")),
            raw_error=str(raw.get("raw_error", "")),
            raw_status_code=parsed_status_code,
            retest_pending=bool(raw.get("retest_pending", False)),
            monthly_period=str(raw.get("monthly_period", "")),
            monthly_success_count=int(raw.get("monthly_success_count", 0) or 0),
        )

    @staticmethod
    def _state_payload(
        state: PersistedKeyState,
        **overrides: Any,
    ) -> dict[str, Any]:
        """将状态对象转换为可持久化字典。"""
        values = {
            "is_disabled": state.is_disabled,
            "disabled_until_epoch": state.disabled_until_epoch,
            "reason": state.reason,
            "fail_count": state.fail_count,
            "consecutive_fails": state.consecutive_fails,
            "updated_at": state.updated_at,
            "raw_error": state.raw_error,
            "raw_status_code": state.raw_status_code,
            "retest_pending": state.retest_pending,
            "monthly_period": state.monthly_period,
            "monthly_success_count": state.monthly_success_count,
        }
        values.update(overrides)
        return values

    @staticmethod
    def _normalized_monthly_values(state: PersistedKeyState) -> tuple[str, int]:
        """按 UTC 自然月归一化月度统计。"""
        period = current_utc_period()
        if state.monthly_period != period:
            return period, 0
        return period, max(0, state.monthly_success_count)
