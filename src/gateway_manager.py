# -*- coding: utf-8 -*-
"""MCP 聚合网关 - 核心业务与网关管理层。

从原有 GUI 中完全解耦出来的纯方法与服务管理类，负责：
1. 配置读写、结构校验与持久化。
2. 本地网关子进程生命周期（启动、优雅退出、健康检查、热重载、端口轮询）。
3. 密钥池维护（导入去重、删除、状态恢复、标记失效）。
4. 异步并发测试验证与上游平台额度查询。
5. 多平台 MCP 客户端配置代码生成。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

from src.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_KEY_COOLDOWN_SECONDS,
    GatewayConfig,
    KeyAuthConfig,
    ROUTING_MODE_PRIMARY_BACKUP,
    ROUTING_MODE_ROUND_ROBIN,
    ServiceConfig,
    dump_config,
    load_config,
)
from src.key_state import KeyStateStore
from src.providers import UsageSnapshot, get_provider
from src.validator import ValidationResult, validate_keys

# ── 常量定义 ──────────────────────────────────────────────────────────────────
APP_TITLE = "MCP 聚合网关"
CONFIG_PATH = Path(DEFAULT_CONFIG_PATH)
PROJECT_ROOT = CONFIG_PATH.parent
START_SCRIPT = PROJECT_ROOT / "start.py"

ROUTING_MODE_LABELS = {
    ROUTING_MODE_ROUND_ROBIN: "轮询",
    ROUTING_MODE_PRIMARY_BACKUP: "主备",
}
ROUTING_LABEL_MODES = {label: mode for mode, label in ROUTING_MODE_LABELS.items()}


def gateway_python_candidates() -> tuple[Path, ...]:
    """按项目虚拟环境、当前解释器的顺序返回网关启动解释器候选。"""
    if sys.platform == "win32":
        venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"

    candidates = [venv_python, Path(sys.executable)]
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        absolute = str(candidate.absolute())
        if absolute not in seen:
            seen.add(absolute)
            unique.append(Path(absolute))
    return tuple(unique)


def dedupe_keep_order(values: list[str]) -> list[str]:
    """对列表去重并保留原始顺序。"""
    seen: set[str] = set()
    result: list[str] = []
    for v in values:
        item = v.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def split_lines(text: str) -> list[str]:
    """按行拆分文本并去重空行。"""
    return dedupe_keep_order(text.replace("\r", "").split("\n"))


@dataclass
class KeyDisplayItem:
    """供界面渲染的单把密钥状态数据。"""
    key: str
    display_key: str
    status_str: str
    status_type: str  # "normal", "cooldown", "disabled", "retest"
    monthly_success_count: int
    quota_info: str | None = None
    is_selected: bool = False


class GatewayManager:
    """网关与密钥池核心管理器。"""

    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or CONFIG_PATH
        self.state_store = KeyStateStore()
        self.config = load_config(str(self.config_path), strict=False)
        self.server_process: subprocess.Popen | None = None

        # 缓存状态
        self.stats_cache: dict[str, Any] = {}
        self.stats_cache_time: float = 0.0
        self.usage_cache: dict[tuple[str, str], UsageSnapshot] = {}
        self.restored_keys: set[tuple[str, str]] = set()  # (service_name, key)

        # 运行时状态缓存（避免端口/密钥改变时未及时匹配）
        self.runtime_port = self.config.gateway.port
        self.runtime_access_key = (
            self.config.gateway.access_keys[0] if self.config.gateway.access_keys else ""
        )

    # ── 配置持久化与提取 ────────────────────────────────────────────────────────
    def reload_config_from_disk(self) -> None:
        """从磁盘重新加载配置文件。"""
        self.config = load_config(str(self.config_path), strict=False)

    def write_config_to_disk(self) -> None:
        """将当前内存中的配置写入磁盘 YAML 文件。"""
        self.config.validate()
        dump_config(self.config, str(self.config_path))

    def update_gateway_config(
        self,
        port: int,
        access_key: str,
        cooldown: int,
        ttl: int,
        retries: int,
        timeout: int,
        routing_mode: str,
    ) -> None:
        """更新网关全局配置。"""
        mode = ROUTING_LABEL_MODES.get(routing_mode, routing_mode)
        self.config.gateway = GatewayConfig(
            port=port,
            access_keys=[access_key.strip()] if access_key.strip() else [],
            key_cooldown_seconds=cooldown,
            session_ttl_seconds=ttl,
            max_failover_retries=retries,
            upstream_timeout_seconds=timeout,
            routing_mode=mode,
        )
        self.config.gateway.validate()

    def update_service_config(
        self,
        service_index: int,
        name: str,
        upstream_url: str,
        enabled: bool,
        key_auth_enabled: bool,
        key_type: str,
        key_param: str,
        failure_patterns: list[str],
    ) -> ServiceConfig:
        """更新指定服务的配置（保留其原有密钥列表）。"""
        if not (0 <= service_index < len(self.config.services)):
            raise IndexError(f"无效的服务索引: {service_index}")

        current_keys = self.config.services[service_index].keys
        updated_svc = ServiceConfig(
            name=name.strip(),
            upstream_url=upstream_url.strip(),
            enabled=enabled,
            key_auth=KeyAuthConfig(
                enabled=key_auth_enabled,
                type=key_type.strip() or "header",
                param=key_param.strip(),
            ),
            keys=current_keys,
            failure_patterns=dedupe_keep_order(failure_patterns),
        )
        updated_svc.validate_basic()
        self.config.services[service_index] = updated_svc
        return updated_svc

    def add_service(
        self,
        name: str,
        upstream_url: str,
        key_auth_type: str = "header",
        key_param: str = "Authorization",
        failure_patterns: list[str] | None = None,
    ) -> ServiceConfig:
        """新增一个第三方 MCP 服务。"""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("服务标识不能为空")
        if any(s.name == clean_name for s in self.config.services):
            raise ValueError(f"服务名称 [{clean_name}] 已存在")

        patterns = failure_patterns if failure_patterns is not None else [
            "rate limit",
            "quota",
            "unauthorized",
            "invalid",
            "401",
            "429",
        ]

        new_svc = ServiceConfig(
            name=clean_name,
            upstream_url=upstream_url.strip(),
            enabled=True,
            key_auth=KeyAuthConfig(
                enabled=True,
                type=key_auth_type.strip() or "header",
                param=key_param.strip() or "Authorization",
            ),
            keys=[],
            failure_patterns=dedupe_keep_order(patterns),
        )
        new_svc.validate_basic()
        self.config.services.append(new_svc)
        return new_svc

    def delete_service(self, service_index: int) -> ServiceConfig:
        """删除指定索引的 MCP 服务，并清理其关联缓存与状态。"""
        if not (0 <= service_index < len(self.config.services)):
            raise IndexError(f"无效的服务索引: {service_index}")

        svc = self.config.services.pop(service_index)
        for k in svc.keys:
            self.state_store.reset_key(svc.name, k)
            self.usage_cache.pop((svc.name, k), None)
            self.restored_keys.discard((svc.name, k))
        self.stats_cache.pop(svc.name, None)
        return svc

    # ── 网关进程管理与网络交互 ──────────────────────────────────────────────────
    def resolve_gateway_python(self) -> Path:
        """查找具备网关运行依赖的 Python 解释器。"""
        probe = "import httpx, starlette, uvicorn, yaml"
        failures: list[str] = []
        for candidate in gateway_python_candidates():
            if not candidate.is_file():
                failures.append(f"{candidate}：文件不存在")
                continue
            try:
                result = subprocess.run(
                    [str(candidate), "-c", probe],
                    cwd=str(PROJECT_ROOT),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"{candidate}：{exc}")
                continue
            if result.returncode == 0:
                return candidate
            detail = (result.stderr or result.stdout).strip().splitlines()
            failures.append(f"{candidate}：{detail[-1] if detail else '网关依赖检查失败'}")

        requirements_path = PROJECT_ROOT / "requirements.txt"
        details = "\n".join(f"- {failure}" for failure in failures)
        raise RuntimeError(
            f"未找到可用的网关 Python 解释器：\n{details}\n"
            f"请先安装项目依赖：pip install -r {requirements_path}"
        )

    def is_gateway_healthy(self, port: int | None = None) -> bool:
        """检查指定或当前运行时端口的网关健康状态。"""
        target_port = port or self.runtime_port
        try:
            with httpx.Client(timeout=0.8) as client:
                res = client.get(f"http://127.0.0.1:{target_port}/healthz")
                return res.status_code == 200
        except Exception:
            return False

    def wait_for_gateway_down(self, port: int, timeout: float = 6.0) -> bool:
        """等待端口释放。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_gateway_healthy(port):
                return True
            time.sleep(0.15)
        return False

    def wait_for_gateway_up(
        self, port: int, process: subprocess.Popen, timeout: float = 12.0
    ) -> bool:
        """等待网关通过健康检查。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            if self.is_gateway_healthy(port):
                return True
            time.sleep(0.2)
        return False

    def start_gateway_process(self) -> subprocess.Popen:
        """启动网关子进程。"""
        if not START_SCRIPT.exists():
            raise FileNotFoundError(f"启动脚本不存在：{START_SCRIPT}")

        python_bin = self.resolve_gateway_python()
        kwargs: dict[str, Any] = {
            "cwd": str(PROJECT_ROOT),
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        return subprocess.Popen(
            [str(python_bin), str(START_SCRIPT), "--config", str(self.config_path)],
            **kwargs,
        )

    def stop_owned_server_process(self) -> None:
        """停止由当前对象管理的子进程。"""
        if self.server_process is None or self.server_process.poll() is not None:
            return
        self.server_process.terminate()
        try:
            self.server_process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self.server_process.kill()
            self.server_process.wait(timeout=2.0)
        self.server_process = None

    def request_gateway_reload(self, port: int, access_key: str) -> tuple[bool, str | None]:
        """向运行中的网关发送热重载请求。"""
        if not access_key:
            return False, "未设置网关访问密钥，无法热加载配置"
        try:
            with httpx.Client(timeout=2.0) as client:
                res = client.post(
                    f"http://127.0.0.1:{port}/admin/reload",
                    headers={"Authorization": f"Bearer {access_key}"},
                )
            if res.status_code == 200:
                return True, None
            if res.status_code == 401:
                return False, "网关访问密钥不正确，拒绝热加载"
            try:
                detail = res.json().get("message", res.text)
            except Exception:
                detail = res.text
            return False, f"网关拒绝热加载：HTTP {res.status_code} {detail}".strip()
        except Exception as exc:
            return False, f"无法连接网关进行热加载：{exc}"

    def request_gateway_restart(self, port: int, access_key: str) -> tuple[bool, str | None]:
        """向运行中的网关发送优雅重启请求。"""
        try:
            with httpx.Client(timeout=2.0) as client:
                res = client.post(
                    f"http://127.0.0.1:{port}/admin/restart",
                    headers={"Authorization": f"Bearer {access_key}"},
                )
            if res.status_code == 200:
                return True, None
            if res.status_code == 404:
                return False, "当前网关不支持优雅退出接口"
            if res.status_code == 401:
                return False, "网关访问密钥不正确，拒绝重启"
            try:
                detail = res.json().get("message", res.text)
            except Exception:
                detail = res.text
            return False, f"网关拒绝重启：HTTP {res.status_code} {detail}".strip()
        except Exception:
            return False, None

    def stop_running_gateway(self, port: int, access_key: str) -> None:
        """停止运行中的网关服务并等待释放。"""
        requested, error = self.request_gateway_restart(port, access_key)
        if error:
            raise RuntimeError(error)
        if requested:
            if not self.wait_for_gateway_down(port, timeout=6.0):
                raise RuntimeError("旧网关未能在 6 秒内停止释放端口")
            return
        if self.is_gateway_healthy(port):
            raise RuntimeError("网关健康检查通过，但无法请求其优雅退出")
        self.stop_owned_server_process()

    def start_and_wait_gateway(self, port: int) -> None:
        """拉起新网关并阻塞等待健康检查通过。"""
        proc = self.start_gateway_process()
        self.server_process = proc
        if self.wait_for_gateway_up(port, proc, timeout=12.0):
            return
        exit_code = proc.poll()
        if exit_code is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        self.server_process = None
        detail = f"进程已退出（退出码 {exit_code}）" if exit_code is not None else "健康检查超时"
        raise RuntimeError(f"新网关启动失败：{detail}")

    def apply_runtime_sync(self, target_port: int, target_key: str) -> None:
        """将修改热加载或重启生效到运行时。"""
        old_port = self.runtime_port
        old_key = self.runtime_access_key
        old_healthy = self.is_gateway_healthy(old_port)
        target_healthy = self.is_gateway_healthy(target_port)

        if old_healthy and target_port == old_port:
            ok, error = self.request_gateway_reload(old_port, old_key)
            if not ok:
                raise RuntimeError(error or "网关拒绝热加载配置")
        elif old_healthy and target_port != old_port:
            self.stop_running_gateway(old_port, old_key)
            self.start_and_wait_gateway(target_port)
        elif target_healthy:
            ok, error = self.request_gateway_reload(target_port, old_key)
            if not ok:
                raise RuntimeError(error or "目标端口网关拒绝热加载配置")
        else:
            self.stop_owned_server_process()
            self.start_and_wait_gateway(target_port)

        self.runtime_port = target_port
        self.runtime_access_key = target_key
        self.stats_cache_time = 0.0

    # ── 密钥池维护 ──────────────────────────────────────────────────────────────
    def import_keys(self, service_index: int, raw_text: str | list[str]) -> tuple[int, list[str]]:
        """批量导入密钥，去重并追加。返回 (新增数量, 最新密钥列表)。支持文本字符串或列表。"""
        if not (0 <= service_index < len(self.config.services)):
            raise IndexError(f"无效的服务索引: {service_index}")

        if isinstance(raw_text, list):
            imported = dedupe_keep_order([k.strip() for k in raw_text if k and k.strip()])
        else:
            imported = split_lines(raw_text)
        if not imported:
            return 0, self.config.services[service_index].keys

        svc = self.config.services[service_index]
        old_count = len(svc.keys)
        svc.keys = dedupe_keep_order(svc.keys + imported)
        new_count = len(svc.keys) - old_count
        return new_count, svc.keys

    def delete_selected_keys(self, service_index: int, keys_to_delete: list[str]) -> int:
        """删除指定密钥，并清除持久化状态与缓存。"""
        if not (0 <= service_index < len(self.config.services)):
            return 0

        svc = self.config.services[service_index]
        deleted_count = 0
        to_delete_set = set(keys_to_delete)

        new_keys: list[str] = []
        for k in svc.keys:
            if k in to_delete_set:
                self.state_store.reset_key(svc.name, k)
                self.usage_cache.pop((svc.name, k), None)
                self.restored_keys.discard((svc.name, k))
                deleted_count += 1
            else:
                new_keys.append(k)

        svc.keys = new_keys
        return deleted_count

    def delete_all_keys(self, service_index: int) -> int:
        """清空指定服务的全部密钥。"""
        if not (0 <= service_index < len(self.config.services)):
            return 0

        svc = self.config.services[service_index]
        count = len(svc.keys)
        for k in svc.keys:
            self.state_store.reset_key(svc.name, k)
            self.usage_cache.pop((svc.name, k), None)
            self.restored_keys.discard((svc.name, k))

        svc.keys = []
        self.stats_cache_time = 0.0
        return count

    def restore_key_available(self, service_name: str, key: str) -> None:
        """将单把密钥从冷却/禁用中恢复。"""
        self.state_store.reset_key(service_name, key)
        self.usage_cache.pop((service_name, key), None)
        self.stats_cache_time = 0.0
        self.restored_keys.add((service_name, key))

        # 尝试通知正在运行的网关实例
        try:
            with httpx.Client(timeout=1.5) as client:
                client.post(
                    f"http://127.0.0.1:{self.runtime_port}/admin/reset-key",
                    headers={"Authorization": f"Bearer {self.runtime_access_key}"},
                    json={"service": service_name, "key": key},
                )
        except Exception:
            pass

    def reset_selected_keys(self, service_index: int, keys: list[str]) -> int:
        """恢复选中的多把密钥。"""
        if not (0 <= service_index < len(self.config.services)):
            return 0
        svc = self.config.services[service_index]
        count = 0
        for k in keys:
            if k in svc.keys:
                self.restore_key_available(svc.name, k)
                count += 1
        return count

    def reset_all_disabled_keys(self, service_index: int) -> int:
        """恢复当前服务所有处于失效或冷却状态的密钥。"""
        if not (0 <= service_index < len(self.config.services)):
            return 0
        svc = self.config.services[service_index]
        items = self.get_key_display_items(service_index)
        disabled_keys = [item.key for item in items if item.status_type != "normal"]

        for k in disabled_keys:
            self.restore_key_available(svc.name, k)
        return len(disabled_keys)

    def disable_key_after_test(self, service_name: str, result: ValidationResult) -> None:
        """在测试失败后，将密钥禁用至下个月。"""
        raw_error = getattr(result, "raw_error", "") or result.detail
        raw_status_code = getattr(result, "raw_status_code", None)
        self.state_store.disable_key_until_next_month(
            service_name,
            result.key,
            reason=f"manual_test_failed:{result.status}",
            raw_error=raw_error,
            raw_status_code=raw_status_code,
        )
        self.restored_keys.discard((service_name, result.key))
        self.usage_cache.pop((service_name, result.key), None)
        self.stats_cache_time = 0.0

    def get_key_raw_errors(self, service_index: int, keys: list[str]) -> list[str]:
        """获取选中密钥最近的原始错误报文。"""
        if not (0 <= service_index < len(self.config.services)):
            return []
        svc = self.config.services[service_index]
        records = self.state_store.build_key_map(svc.name, svc.keys)
        stats = self.stats_cache.get(svc.name, {}).get("keys", {}).get("details", [])
        stats_map = {item["key"]: item for item in stats if "key" in item}

        reports: list[str] = []
        for key in keys:
            rec = records.get(key, {})
            info = stats_map.get(key, {})
            raw_error = rec.get("raw_error") or info.get("raw_error") or "暂无原始错误记录"
            status_code = rec.get("raw_status_code") or info.get("raw_status_code")
            status_text = f"HTTP {status_code}" if status_code else "无 HTTP 状态码"
            tail = key[-6:] if len(key) >= 6 else key
            reports.append(f"密钥 ...{tail} | {status_text}\n{raw_error}")
        return reports

    # ── 状态展示与统计 ──────────────────────────────────────────────────────────
    def fetch_stats(self) -> dict[str, Any]:
        """从网关拉取最新统计数据。"""
        if time.time() - self.stats_cache_time <= 2.5:
            return self.stats_cache
        try:
            with httpx.Client(timeout=1.0) as client:
                res = client.get(
                    f"http://127.0.0.1:{self.runtime_port}/stats",
                    headers={"Authorization": f"Bearer {self.runtime_access_key}"},
                )
                if res.status_code == 200:
                    self.stats_cache = res.json()
                    self.stats_cache_time = time.time()
        except Exception:
            pass
        return self.stats_cache

    def get_key_display_items(self, service_index: int) -> list[KeyDisplayItem]:
        """根据持久化数据与运行时统计，构建统一的密钥列表渲染模型。"""
        if not (0 <= service_index < len(self.config.services)):
            return []

        svc = self.config.services[service_index]
        stats = self.stats_cache.get(svc.name, {}).get("keys", {}).get("details", [])
        stats_map = {item["key"]: item for item in stats if "key" in item}
        persisted_map = self.state_store.build_key_map(svc.name, svc.keys)

        results: list[KeyDisplayItem] = []
        for key in svc.keys:
            status_str = "正常"
            status_type = "normal"

            if (svc.name, key) in self.restored_keys:
                if (
                    key in stats_map
                    and not stats_map[key].get("is_disabled")
                    and stats_map[key].get("cooldown_remaining", 0) <= 0
                ):
                    self.restored_keys.discard((svc.name, key))
            else:
                persisted = persisted_map.get(key)
                if persisted and (
                    persisted.get("is_disabled") or persisted.get("retest_pending")
                ):
                    if persisted.get("retest_pending"):
                        remaining = persisted.get("disabled_remaining", 0)
                        status_str = f"冷却中({int(remaining)}s)" if remaining > 0 else "等待复测"
                        status_type = "cooldown" if remaining > 0 else "retest"
                    elif persisted.get("disabled_until_epoch", 0) > 0:
                        status_str = "禁用至下月"
                        status_type = "disabled"
                    else:
                        status_str = "永久禁用"
                        status_type = "disabled"
                elif key in stats_map:
                    info = stats_map[key]
                    if info.get("is_disabled"):
                        if info.get("retest_pending"):
                            rem = info.get("cooldown_remaining", 0)
                            status_str = f"冷却中({int(rem)}s)" if rem > 0 else "等待复测"
                            status_type = "cooldown" if rem > 0 else "retest"
                        else:
                            status_str = "永久禁用"
                            status_type = "disabled"
                    elif info.get("cooldown_remaining", 0) > 0:
                        status_str = f"冷却中({int(info['cooldown_remaining'])}s)"
                        status_type = "cooldown"

            rec = persisted_map.get(key)
            monthly_success = (
                rec.get("monthly_success_count", 0)
                if rec
                else stats_map.get(key, {}).get("monthly_success_count", 0)
            )

            usage = self.usage_cache.get((svc.name, key))
            quota_info = None
            if usage and usage.ok and usage.key_remaining is not None and usage.key_limit is not None:
                quota_info = f"余 {usage.key_remaining}/{usage.key_limit}"

            # 脱敏展示
            if len(key) <= 24:
                display_key = key
            else:
                display_key = f"{key[:10]}...{key[-8:]}"

            results.append(
                KeyDisplayItem(
                    key=key,
                    display_key=display_key,
                    status_str=status_str,
                    status_type=status_type,
                    monthly_success_count=monthly_success,
                    quota_info=quota_info,
                )
            )
        return results

    # ── 并发测试与额度 ──────────────────────────────────────────────────────────
    async def run_key_validation(
        self,
        service_index: int,
        keys: list[str],
        concurrency: int = 5,
        timeout: float = 45.0,
    ) -> list[ValidationResult]:
        """并发测试指定服务的密钥。"""
        if not (0 <= service_index < len(self.config.services)):
            return []
        svc = self.config.services[service_index]
        actual_concurrency = min(len(keys), max(1, concurrency))
        results = await validate_keys(
            svc, keys, deep=True, concurrency=actual_concurrency, timeout=timeout
        )
        for r in results:
            if r.status == "valid":
                self.restore_key_available(svc.name, r.key)
            else:
                self.disable_key_after_test(svc.name, r)
        return results

    async def run_usage_query(
        self,
        service_index: int,
        keys: list[str],
        timeout: float = 20.0,
    ) -> list[tuple[str, UsageSnapshot]]:
        """并发查询密钥额度快照。"""
        if not (0 <= service_index < len(self.config.services)):
            return []
        svc = self.config.services[service_index]
        provider = get_provider(svc.name)
        if provider is None or not provider.supports_usage:
            raise ValueError(f"服务 [{svc.name}] 暂不支持额度查询")

        sem = asyncio.Semaphore(min(2, max(1, len(keys))))

        async def _fetch(key: str) -> tuple[str, UsageSnapshot]:
            async with sem:
                snapshot = await provider.fetch_usage(key, timeout=timeout)
                if snapshot is None:
                    snapshot = UsageSnapshot(status="error", detail="当前平台未返回额度信息")
                return key, snapshot

        results = await asyncio.gather(*[_fetch(k) for k in keys])
        for key, snapshot in results:
            self.usage_cache[(svc.name, key)] = snapshot
        return results

    def format_usage_line(self, key: str, snapshot: UsageSnapshot) -> str:
        """格式化单条额度输出。"""
        if not snapshot.ok:
            return f"❌ {key} | {snapshot.detail}"

        parts = [f"📊 {key}"]
        if (
            snapshot.key_remaining is not None
            and snapshot.key_limit is not None
            and snapshot.key_usage is not None
        ):
            parts.append(f"密钥剩余 {snapshot.key_remaining}/{snapshot.key_limit} (已用 {snapshot.key_usage})")
        if snapshot.account_plan:
            plan = f"套餐 {snapshot.account_plan}"
            if (
                snapshot.account_remaining is not None
                and snapshot.account_limit is not None
                and snapshot.account_usage is not None
            ):
                plan += f" 剩余 {snapshot.account_remaining}/{snapshot.account_limit} (已用 {snapshot.account_usage})"
            parts.append(plan)
        if snapshot.paygo_usage is not None:
            if snapshot.paygo_limit is not None:
                parts.append(f"按量剩余 {snapshot.paygo_limit - snapshot.paygo_usage}/{snapshot.paygo_limit}")
            else:
                parts.append(f"按量已用 {snapshot.paygo_usage}")
        return " | ".join(parts)

    # ── MCP 客户端配置代码生成 ──────────────────────────────────────────────────
    def generate_mcp_config(
        self,
        service_name: str,
        client_type: str = "claude",
    ) -> str:
        """生成支持 Claude Desktop / Cursor / Cline 的 MCP 配置 JSON。"""
        port = self.config.gateway.port
        gw_key = self.runtime_access_key or "YOUR_GATEWAY_ACCESS_KEY"

        if client_type.lower() == "cursor":
            cfg = {
                "mcpServers": {
                    f"mcp-{service_name}": {
                        "url": f"http://127.0.0.1:{port}/{service_name}/mcp",
                        "headers": {"Authorization": gw_key},
                    }
                }
            }
        else:  # Claude Desktop / Cline / 通用 streamable-http
            cfg = {
                "mcpServers": {
                    f"gateway-{service_name}": {
                        "type": "streamable-http",
                        "url": f"http://127.0.0.1:{port}/{service_name}/mcp",
                        "headers": {"Authorization": gw_key},
                    }
                }
            }
        return json.dumps(cfg, indent=2, ensure_ascii=False)
