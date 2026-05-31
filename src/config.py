# -*- coding: utf-8 -*-
"""配置加载与数据模型。

负责：
- 定义网关、服务、密钥鉴权的数据结构。
- 从 config.yaml 读取并校验配置。
- 解析旧版 .key 文件（供 GUI 迁移密钥使用）。
- 将配置写回 config.yaml（供 GUI 保存）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Any

import yaml


# 默认配置文件路径（相对项目根目录）
DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml"
)


@dataclass
class KeyAuthConfig:
    """单个服务的密钥注入方式配置。"""

    enabled: bool = False  # 是否启用密钥池轮询逻辑
    type: str = "header"   # 注入方式：header 或 query
    param: str = ""        # 注入字段名（如 CONTEXT7_API_KEY、tavilyApiKey）

    def validate(self, service_name: str) -> None:
        """校验配置合法性。"""
        if not self.enabled:
            return
        if self.type not in ("header", "query"):
            raise ValueError(
                f"服务 [{service_name}] 的 key_auth.type 必须是 'header' 或 'query'，当前为 '{self.type}'"
            )
        if not self.param:
            raise ValueError(f"服务 [{service_name}] 启用了密钥鉴权但未设置 key_auth.param 字段名")


@dataclass
class ServiceConfig:
    """单个被聚合的 MCP 服务配置。"""

    name: str                                   # 服务名，也是路由路径，如 context7
    upstream_url: str                           # 上游 remote URL
    enabled: bool = True                        # 是否启用
    key_auth: KeyAuthConfig = field(default_factory=KeyAuthConfig)
    keys: list[str] = field(default_factory=list)            # 上游密钥池
    failure_patterns: list[str] = field(default_factory=list)  # 服务专属失败特征（小写匹配）

    def validate(self) -> None:
        """校验服务配置。"""
        if not self.name:
            raise ValueError("服务缺少 name 字段")
        if not self.upstream_url:
            raise ValueError(f"服务 [{self.name}] 缺少 upstream_url 字段")
        self.key_auth.validate(self.name)
        if self.key_auth.enabled and not self.keys:
            raise ValueError(f"服务 [{self.name}] 启用了密钥鉴权但密钥池为空")

    def validate_basic(self) -> None:
        """基础校验：不检查密钥池是否为空（供 GUI 编辑时使用）。"""
        if not self.name:
            raise ValueError("服务缺少 name 字段")
        if not self.upstream_url:
            raise ValueError(f"服务 [{self.name}] 缺少 upstream_url 字段")
        self.key_auth.validate(self.name)

    @property
    def normalized_failure_patterns(self) -> list[str]:
        """返回小写化的失败特征，用于大小写不敏感匹配。"""
        return [p.lower() for p in self.failure_patterns]


@dataclass
class GatewayConfig:
    """网关全局配置。"""

    port: int = 8080
    access_keys: list[str] = field(default_factory=list)
    key_cooldown_seconds: int = 60
    session_ttl_seconds: int = 1800
    max_failover_retries: int = 3
    upstream_timeout_seconds: int = 120

    def validate(self) -> None:
        """校验网关配置。"""
        if not (0 < self.port < 65536):
            raise ValueError(f"端口非法：{self.port}")
        if not self.access_keys:
            raise ValueError("gateway.access_keys 不能为空，否则网关将无鉴权暴露")


@dataclass
class AppConfig:
    """整个应用的配置聚合。"""

    gateway: GatewayConfig
    services: list[ServiceConfig]

    def validate(self) -> None:
        """整体校验。"""
        self.gateway.validate()
        names = set()
        for svc in self.services:
            svc.validate()
            if svc.name in names:
                raise ValueError(f"存在重复的服务名：{svc.name}")
            names.add(svc.name)

    def get_service(self, name: str) -> ServiceConfig | None:
        """按名查找服务。"""
        for svc in self.services:
            if svc.name == name:
                return svc
        return None


def _parse_key_auth(raw: dict[str, Any] | None) -> KeyAuthConfig:
    """解析单个服务的 key_auth 子配置。"""
    raw = raw or {}
    return KeyAuthConfig(
        enabled=bool(raw.get("enabled", False)),
        type=str(raw.get("type", "header")),
        param=str(raw.get("param", "")),
    )


def _parse_service(raw: dict[str, Any]) -> ServiceConfig:
    """解析单个服务配置。"""
    return ServiceConfig(
        name=str(raw.get("name", "")),
        upstream_url=str(raw.get("upstream_url", "")),
        enabled=bool(raw.get("enabled", True)),
        key_auth=_parse_key_auth(raw.get("key_auth")),
        keys=list(raw.get("keys", []) or []),
        failure_patterns=list(raw.get("failure_patterns", []) or []),
    )


def _load_env_keys() -> dict[str, list[str]]:
    """从本地 .env 文件加载密钥。
    
    格式为：
    GATEWAY_ACCESS_KEYS=key1,key2
    SERVICE_KEYS_context7=key1,key2
    SERVICE_KEYS_tavily=key1,key2
    """
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    keys_map = {}
    if not os.path.exists(env_path):
        return keys_map
        
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if v:
                keys_map[k] = [item.strip() for item in v.split(",") if item.strip()]
    return keys_map


def _save_env_keys(gateway_keys: list[str], services_keys: dict[str, list[str]], gateway_settings: dict | None = None) -> None:
    """将密钥和网关设置保存到本地 .env 文件。"""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    lines = []
    
    lines.append("# MCP 聚合网关密钥存储")
    
    # 写入网关设置
    if gateway_settings:
        lines.append("")
        lines.append("# 网关设置")
        for k, v in gateway_settings.items():
            lines.append(f"{k}={v}")
    
    # 写入网关统一密钥
    lines.append("")
    lines.append("# 网关访问密钥")
    lines.append(f"GATEWAY_ACCESS_KEYS={','.join(gateway_keys)}")
    
    # 写入各服务的密钥
    lines.append("")
    lines.append("# 各服务上游密钥池")
    for svc_name, keys in services_keys.items():
        lines.append(f"SERVICE_KEYS_{svc_name}={','.join(keys)}")
        
    with open(env_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def load_config(path: str | None = None, *, strict: bool = True) -> AppConfig:
    """从 YAML 文件加载并校验配置，同时从 .env 文件加载密钥。

    Args:
        path: 配置文件路径，默认 项目根/config.yaml。
        strict: 是否执行校验。GUI 编辑器传 False 以允许加载不完整配置。

    Returns:
        校验通过的 AppConfig 实例。
    """
    path = path or DEFAULT_CONFIG_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(f"配置文件不存在：{path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # 从 .env 加载密钥
    env_keys = _load_env_keys()

    gw_raw = raw.get("gateway", {}) or {}
    # 优先使用 .env 中的网关密钥，若无则使用 config.yaml 中的（向下兼容）
    gw_keys_raw = env_keys.get("GATEWAY_ACCESS_KEYS", [])
    if gw_keys_raw:
        gw_keys = [k.strip() for k in gw_keys_raw if k.strip()]
    else:
        gw_keys = list(gw_raw.get("access_keys", []) or [])

    def _env_int(key: str, default: int) -> int:
        vals = env_keys.get(key, [])
        return int(vals[0]) if vals else default

    gateway = GatewayConfig(
        port=_env_int("GATEWAY_PORT", int(gw_raw.get("port", 8080))),
        access_keys=gw_keys,
        key_cooldown_seconds=_env_int("GATEWAY_KEY_COOLDOWN_SECONDS", int(gw_raw.get("key_cooldown_seconds", 1800))),
        session_ttl_seconds=_env_int("GATEWAY_SESSION_TTL_SECONDS", int(gw_raw.get("session_ttl_seconds", 1800))),
        max_failover_retries=_env_int("GATEWAY_MAX_FAILOVER_RETRIES", int(gw_raw.get("max_failover_retries", 3))),
        upstream_timeout_seconds=_env_int("GATEWAY_UPSTREAM_TIMEOUT_SECONDS", int(gw_raw.get("upstream_timeout_seconds", 120))),
    )

    services = []
    for s in (raw.get("services", []) or []):
        svc = _parse_service(s)
        # 优先使用 .env 中的服务密钥，若无则使用 config.yaml 中的（向下兼容）
        env_svc_key = f"SERVICE_KEYS_{svc.name}"
        if env_svc_key in env_keys:
            svc.keys = env_keys[env_svc_key]
        services.append(svc)

    config = AppConfig(gateway=gateway, services=services)
    if strict:
        config.validate()
    return config


def dump_config(config: AppConfig, path: str | None = None) -> None:
    """将配置写回 YAML 文件，同时将密钥和网关设置保存到本地 .env 文件。

    Args:
        config: 要保存的配置。
        path: 目标路径，默认 项目根/config.yaml。
    """
    path = path or DEFAULT_CONFIG_PATH
    
    # 1. 保存密钥和网关设置到 .env
    services_keys = {svc.name: svc.keys for svc in config.services}
    gateway_settings = {
        "GATEWAY_PORT": config.gateway.port,
        "GATEWAY_KEY_COOLDOWN_SECONDS": config.gateway.key_cooldown_seconds,
        "GATEWAY_SESSION_TTL_SECONDS": config.gateway.session_ttl_seconds,
        "GATEWAY_MAX_FAILOVER_RETRIES": config.gateway.max_failover_retries,
        "GATEWAY_UPSTREAM_TIMEOUT_SECONDS": config.gateway.upstream_timeout_seconds,
    }
    _save_env_keys(config.gateway.access_keys, services_keys, gateway_settings)
    
    # 2. 构造 config.yaml 数据（保留默认结构，密钥置空）
    clean_services = []
    for svc in config.services:
        svc_dict = asdict(svc)
        svc_dict["keys"] = []
        clean_services.append(svc_dict)
        
    gw_dict = asdict(config.gateway)
    gw_dict["access_keys"] = []
    
    data = {
        "gateway": gw_dict,
        "services": clean_services,
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, indent=2)


def parse_legacy_key_file(path: str) -> dict[str, list[str]]:
    """解析旧版 .key 文件，返回 {分组名: [密钥列表]}。

    文件格式为：分组名独占一行，随后若干行是该分组的密钥，空行分隔分组。
    例如：
        taivy

        tvly-dev-xxx
        tvly-dev-yyy

        context7

        ctx7sk-xxx

    Returns:
        分组名到密钥列表的映射；自动去重并保持出现顺序。
    """
    groups: dict[str, list[str]] = {}
    current: str | None = None

    def looks_like_key(line: str) -> bool:
        # 密钥通常较长且包含连字符/前缀；分组名一般是简短单词
        return len(line) > 16 or "-" in line or line.startswith(("tvly", "ctx7", "sk-"))

    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            if not looks_like_key(line):
                # 视为分组名
                current = line
                groups.setdefault(current, [])
            else:
                if current is None:
                    current = "default"
                    groups.setdefault(current, [])
                if line not in groups[current]:
                    groups[current].append(line)
    return groups
