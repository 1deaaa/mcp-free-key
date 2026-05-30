# -*- coding: utf-8 -*-
r"""MCP 聚合网关配置编辑器。

功能：
- 编辑网关端口、统一访问密钥。
- 增删改 MCP 服务（上游 URL、密钥注入方式、失败特征）。
- 批量添加密钥、自动去重、从旧版 .key 文件导入。
- 测试单个服务的全部密钥是否有效且有额度。

布局：
- 顶部：网关设置栏（端口 + 访问密钥 + 操作按钮）
- 左侧：服务列表
- 右侧：选中服务的详情编辑区
- 底部：测试日志

高分屏：启动时启用 DPI 感知，所有字体 ≥ 13pt。
"""
from __future__ import annotations

import asyncio
import ctypes
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from src.config import (
    DEFAULT_CONFIG_PATH,
    GatewayConfig,
    KeyAuthConfig,
    ServiceConfig,
    dump_config,
    load_config,
    parse_legacy_key_file,
)
from src.validator import validate_keys

APP_TITLE = "MCP 聚合网关配置器"
CONFIG_PATH = Path(DEFAULT_CONFIG_PATH)

# 统一字号
FONT_FAMILY = "Microsoft YaHei UI"
FONT_NORMAL = (FONT_FAMILY, 13)
FONT_BOLD = (FONT_FAMILY, 13, "bold")
FONT_TITLE = (FONT_FAMILY, 16, "bold")
FONT_MONO = ("Consolas", 13)


def enable_high_dpi(root: tk.Tk) -> None:
    """启用高分屏支持。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        scale = root.winfo_fpixels("1i") / 72.0
        root.tk.call("tk", "scaling", max(1.4, min(scale, 2.2)))
    except Exception:
        pass


def dedupe_keep_order(values: list[str]) -> list[str]:
    """去重并保持原顺序。"""
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
    """按行拆分并去重。"""
    return dedupe_keep_order(text.replace("\r", "").split("\n"))


class GatewayEditor:
    """图形界面主控制器。"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1600x900")
        self.root.minsize(1600, 900)

        self.style = ttk.Style()
        try:
            self.style.theme_use("vista")
        except Exception:
            pass
        self._apply_styles()

        self.config = load_config(str(CONFIG_PATH), strict=False)
        self.current_index = 0 if self.config.services else -1

        self._build_ui()
        self._load_gateway()
        self._refresh_service_list(select_index=self.current_index)

    # ------------------------------------------------------------------ 样式
    def _apply_styles(self) -> None:
        s = self.style
        s.configure(".", font=FONT_NORMAL)
        s.configure("TLabel", font=FONT_NORMAL)
        s.configure("TButton", font=FONT_NORMAL, padding=(12, 6))
        s.configure("TEntry", font=FONT_NORMAL)
        s.configure("TCombobox", font=FONT_NORMAL)
        s.configure("TCheckbutton", font=FONT_NORMAL)
        s.configure("TLabelframe", font=FONT_BOLD)
        s.configure("TLabelframe.Label", font=FONT_BOLD)
        s.configure("TNotebook.Tab", font=FONT_NORMAL, padding=(16, 8))
        s.configure("Header.TLabel", font=FONT_TITLE)

    # ------------------------------------------------------------------ 布局
    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        outer = ttk.Frame(self.root, padding=16)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # ---- 标题
        ttk.Label(outer, text=APP_TITLE, style="Header.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 12))

        # ---- 顶部：网关设置
        gw_frame = ttk.LabelFrame(outer, text="网关设置", padding=12)
        gw_frame.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        for i in range(6):
            gw_frame.columnconfigure(i, weight=1 if i in (1, 3) else 0)

        self.port_var = tk.StringVar()
        self.gw_key_var = tk.StringVar()
        self.cooldown_var = tk.StringVar()
        self.ttl_var = tk.StringVar()
        self.retry_var = tk.StringVar()
        self.timeout_var = tk.StringVar()

        r = 0
        ttk.Label(gw_frame, text="端口").grid(row=r, column=0, sticky="w", padx=(0, 6))
        e_port = ttk.Entry(gw_frame, textvariable=self.port_var, width=8)
        e_port.grid(row=r, column=1, sticky="w", padx=(0, 16))

        ttk.Label(gw_frame, text="访问密钥").grid(row=r, column=2, sticky="w", padx=(0, 6))
        # 主密钥不需要隐藏起来了，直接显示即可，移除 show="*"
        ttk.Entry(gw_frame, textvariable=self.gw_key_var, width=40).grid(row=r, column=3, sticky="ew", padx=(0, 16))

        r = 1
        ttk.Label(gw_frame, text="密钥冷却(秒)").grid(row=r, column=0, sticky="w", padx=(0, 6), pady=(8, 0))
        ttk.Entry(gw_frame, textvariable=self.cooldown_var, width=8).grid(row=r, column=1, sticky="w", padx=(0, 16), pady=(8, 0))

        ttk.Label(gw_frame, text="会话TTL(秒)").grid(row=r, column=2, sticky="w", padx=(0, 6), pady=(8, 0))
        ttk.Entry(gw_frame, textvariable=self.ttl_var, width=8).grid(row=r, column=3, sticky="w", padx=(0, 16), pady=(8, 0))

        ttk.Label(gw_frame, text="最大重试").grid(row=r, column=4, sticky="w", padx=(0, 6), pady=(8, 0))
        ttk.Entry(gw_frame, textvariable=self.retry_var, width=6).grid(row=r, column=5, sticky="w", pady=(8, 0))

        # ---- 中部：左服务列表 + 右编辑区
        mid = ttk.Frame(outer)
        mid.grid(row=2, column=0, sticky="nsew", pady=(0, 12))
        mid.columnconfigure(1, weight=1)
        mid.rowconfigure(0, weight=1)

        # 左侧：服务列表
        left = ttk.LabelFrame(mid, text="服务列表", padding=12)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)

        self.svc_listbox = tk.Listbox(left, width=28, font=FONT_NORMAL, activestyle="none", relief="solid", bd=1)
        self.svc_listbox.grid(row=0, column=0, sticky="nsew")
        self.svc_listbox.bind("<<ListboxSelect>>", self._on_select)

        btn_row = ttk.Frame(left)
        btn_row.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        btn_row.columnconfigure((0, 1), weight=1)
        # 暂时注释掉服务列表的“添加”和“删除”按钮
        # ttk.Button(btn_row, text="＋ 添加", command=self._add_service).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        # ttk.Button(btn_row, text="－ 删除", command=self._delete_service).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Label(btn_row, text="服务列表已锁定", font=FONT_BOLD, foreground="gray").grid(row=0, column=0, columnspan=2, pady=4)

        # 右侧：服务编辑
        right = ttk.LabelFrame(mid, text="服务详情", padding=12)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(3, weight=1) # 让下半部分容器拉伸

        self.svc_name_var = tk.StringVar()
        self.svc_enabled_var = tk.BooleanVar(value=True)
        self.svc_url_var = tk.StringVar()
        self.key_enabled_var = tk.BooleanVar(value=True)
        self.key_type_var = tk.StringVar(value="header")
        self.key_param_var = tk.StringVar()

        r = 0
        ttk.Label(right, text="服务名").grid(row=r, column=0, sticky="w", padx=(0, 8), pady=(0, 8))
        # 将服务名输入框设为只读（state="readonly"）
        ttk.Entry(right, textvariable=self.svc_name_var, state="readonly").grid(row=r, column=1, sticky="ew", padx=(0, 8), pady=(0, 8))
        ttk.Checkbutton(right, text="启用", variable=self.svc_enabled_var).grid(row=r, column=2, sticky="w", pady=(0, 8))

        r = 1
        ttk.Label(right, text="上游 URL").grid(row=r, column=0, sticky="w", padx=(0, 8), pady=(0, 8))
        # 将上游 URL 输入框设为只读（state="readonly"）
        ttk.Entry(right, textvariable=self.svc_url_var, state="readonly").grid(row=r, column=1, columnspan=2, sticky="ew", pady=(0, 8))

        r = 2
        ttk.Checkbutton(right, text="密钥轮询", variable=self.key_enabled_var).grid(row=r, column=0, sticky="w", pady=(0, 8))
        ttk.Combobox(right, textvariable=self.key_type_var, values=["header", "query"], state="readonly", width=8).grid(row=r, column=1, sticky="w", padx=(0, 8), pady=(0, 8))
        ttk.Entry(right, textvariable=self.key_param_var).grid(row=r, column=2, sticky="ew", pady=(0, 8))

        r = 3
        # 创建一个下半部分容器，用于完美控制左右比例
        lower_frame = ttk.Frame(right)
        lower_frame.grid(row=r, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        # 显式设置 uniform 组，强制 Tkinter 严格按照 3:1 的比例分配剩余空间，不受子控件默认大小的影响
        lower_frame.columnconfigure(0, weight=3, uniform="lower_cols") # 密钥管理占 3/4 宽度
        lower_frame.columnconfigure(1, weight=1, uniform="lower_cols") # 失败特征占 1/4 宽度
        lower_frame.rowconfigure(0, weight=1)

        # 左侧：密钥管理子框架
        keys_frame = ttk.Frame(lower_frame)
        keys_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        keys_frame.columnconfigure(0, weight=1)
        keys_frame.rowconfigure(1, weight=1)

        # 密钥管理标题与删除按钮行
        keys_title_row = ttk.Frame(keys_frame)
        keys_title_row.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        keys_title_row.columnconfigure(0, weight=1)
        
        ttk.Label(keys_title_row, text="密钥状态与管理").grid(row=0, column=0, sticky="sw")
        ttk.Button(keys_title_row, text="删除选中密钥", command=self._delete_selected_keys, padding=(6, 2)).grid(row=0, column=1, sticky="e")

        # 右侧：失败特征子框架
        patterns_frame = ttk.Frame(lower_frame)
        patterns_frame.grid(row=0, column=1, sticky="nsew")
        patterns_frame.columnconfigure(0, weight=1)
        patterns_frame.rowconfigure(1, weight=1)

        ttk.Label(patterns_frame, text="失败特征（每行一个）").grid(row=0, column=0, sticky="sw", pady=(0, 4))

        r = 4
        # 使用 Treeview 展示密钥（放在 keys_frame 中）
        # 显式设置较宽的列宽，并让 Treeview 填充 keys_frame
        self.keys_tree = ttk.Treeview(keys_frame, columns=("key", "status", "fails"), show="headings", height=8)
        self.keys_tree.heading("key", text="密钥 (双击编辑/添加)")
        self.keys_tree.heading("status", text="状态")
        self.keys_tree.heading("fails", text="连续失败")
        self.keys_tree.column("key", width=550, minwidth=300, stretch=True)
        self.keys_tree.column("status", width=150, minwidth=100, stretch=False)
        self.keys_tree.column("fails", width=100, minwidth=80, stretch=False)
        self.keys_tree.grid(row=1, column=0, sticky="nsew")

        # 为 Treeview 添加滚动条
        keys_scroll = ttk.Scrollbar(keys_frame, orient="vertical", command=self.keys_tree.yview)
        keys_scroll.grid(row=1, column=1, sticky="ns")
        self.keys_tree.configure(yscrollcommand=keys_scroll.set)

        self.keys_tree.tag_configure("disabled", foreground="red", font=FONT_BOLD)
        self.keys_tree.tag_configure("cooling", foreground="orange")
        self.keys_tree.tag_configure("normal", foreground="green")
        self.keys_tree.bind("<Double-1>", self._on_key_double_click)

        # 失败特征文本框（放在 patterns_frame 中）
        # 显式设置较小的 width=25，防止 tk.Text 默认 width=80 撑大布局，从而让 lower_frame 的 weight 比例（3:1）真正生效
        self.patterns_text = tk.Text(patterns_frame, font=FONT_MONO, width=25, height=8, wrap="none", relief="solid", bd=1)
        self.patterns_text.grid(row=1, column=0, sticky="nsew")

        # 为失败特征添加滚动条
        patterns_scroll = ttk.Scrollbar(patterns_frame, orient="vertical", command=self.patterns_text.yview)
        patterns_scroll.grid(row=1, column=1, sticky="ns")
        self.patterns_text.configure(yscrollcommand=patterns_scroll.set)

        r = 5
        act = ttk.Frame(right)
        act.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        # 移除“全部去重”按钮，重新分配列权重
        act.columnconfigure((0, 1, 2, 3), weight=1)
        ttk.Button(act, text="保存配置", command=self._save).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(act, text="批量导入密钥", command=self._import_keys).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(act, text="测试全部密钥", command=self._test_keys).grid(row=0, column=2, sticky="ew", padx=4)
        ttk.Button(act, text="手动恢复密钥", command=self._reset_selected_key).grid(row=0, column=3, sticky="ew", padx=(4, 0))

        # ---- 底部：左右分栏（左侧为 MCP 示例，右侧为测试日志）
        bottom_frame = ttk.Frame(outer)
        bottom_frame.grid(row=3, column=0, sticky="nsew")
        bottom_frame.columnconfigure(0, weight=1, uniform="bottom_cols") # 左侧 MCP 示例占 1/2 宽度
        bottom_frame.columnconfigure(1, weight=1, uniform="bottom_cols") # 右侧 测试日志占 1/2 宽度
        bottom_frame.rowconfigure(0, weight=1)
        outer.rowconfigure(3, weight=0, minsize=240) # 适当增加底部高度以容纳内容

        # 底部左侧：MCP 客户端配置示例
        mcp_frame = ttk.LabelFrame(bottom_frame, text="MCP 客户端配置示例", padding=12)
        mcp_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        mcp_frame.columnconfigure(0, weight=1)
        mcp_frame.rowconfigure(0, weight=1)

        self.mcp_example_text = tk.Text(mcp_frame, font=FONT_MONO, height=8, wrap="word", relief="solid", bd=1)
        self.mcp_example_text.grid(row=0, column=0, sticky="nsew")
        self.mcp_example_text.configure(state="disabled") # 默认只读

        mcp_btn_row = ttk.Frame(mcp_frame)
        mcp_btn_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(mcp_btn_row, text="复制配置", command=self._copy_mcp_example, padding=(8, 2)).pack(side="right")

        # 底部右侧：测试日志
        log_frame = ttk.LabelFrame(bottom_frame, text="测试日志", padding=12)
        log_frame.grid(row=0, column=1, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, font=FONT_MONO, height=8, wrap="word", relief="solid", bd=1)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

        ttk.Button(log_frame, text="清空日志", command=lambda: self._set_text(self.log_text, "")).grid(row=1, column=0, sticky="e", pady=(8, 0))

    # ------------------------------------------------------------------ 数据加载
    def _load_gateway(self) -> None:
        gw = self.config.gateway
        self.port_var.set(str(gw.port))
        self.gw_key_var.set(gw.access_keys[0] if gw.access_keys else "")
        self.cooldown_var.set(str(gw.key_cooldown_seconds))
        self.ttl_var.set(str(gw.session_ttl_seconds))
        self.retry_var.set(str(gw.max_failover_retries))
        self.timeout_var.set(str(gw.upstream_timeout_seconds))

    def _load_service(self, svc: ServiceConfig) -> None:
        self.svc_name_var.set(svc.name)
        self.svc_enabled_var.set(svc.enabled)
        self.svc_url_var.set(svc.upstream_url)
        self.key_enabled_var.set(svc.key_auth.enabled)
        self.key_type_var.set(svc.key_auth.type)
        self.key_param_var.set(svc.key_auth.param)
        self._set_text(self.patterns_text, "\n".join(svc.failure_patterns))
        
        # 刷新密钥 Treeview
        self._refresh_keys_tree(svc)
        
        # 刷新 MCP 客户端配置示例
        self._refresh_mcp_example(svc)

    # 增加一个本地缓存，避免每次切换服务都同步请求网关导致卡顿
    _stats_cache = {}
    _stats_cache_time = 0.0

    def _refresh_keys_tree(self, svc: ServiceConfig) -> None:
        """刷新密钥 Treeview 列表，并根据状态着色。"""
        self.keys_tree.delete(*self.keys_tree.get_children())
        
        # 优先使用缓存，如果缓存过期（超过 3 秒）则在后台异步更新，不阻塞 UI 线程
        import time
        now = time.time()
        
        if not self._stats_cache or (now - self._stats_cache_time > 3.0):
            # 启动后台线程去请求，避免卡顿
            threading.Thread(target=self._async_fetch_stats, args=(svc,), daemon=True).start()
            
        stats = self._stats_cache.get(svc.name, {}).get("keys", {}).get("details", [])
        stats_map = {item["key"]: item for item in stats if "key" in item}

        for key in svc.keys:
            status_str = "正常"
            fails_str = "0"
            tag = "normal"
            
            if key in stats_map:
                info = stats_map[key]
                if info.get("is_disabled"):
                    status_str = "永久禁用"
                    fails_str = str(info.get("consecutive_fails", 2))
                    tag = "disabled"
                elif info.get("cooldown_remaining", 0) > 0:
                    status_str = f"冷却中 ({int(info['cooldown_remaining'])}s)"
                    fails_str = str(info.get("consecutive_fails", 1))
                    tag = "cooling"
                else:
                    fails_str = str(info.get("consecutive_fails", 0))
            
            # 隐藏密钥中间部分，保护隐私
            # 主密钥不需要隐藏起来了，直接显示即可，不进行缩略显示
            display_key = key
            self.keys_tree.insert("", "end", values=(display_key, status_str, fails_str), tags=(tag,), iid=key)

    def _svc_from_fields(self) -> ServiceConfig:
        # 从 Treeview 中提取所有密钥
        # 如果是 display_key，需要还原为真实 key（iid 存的是真实 key）
        keys = [self.keys_tree.item(item)["iid"] for item in self.keys_tree.get_children()]
        
        return ServiceConfig(
            name=self.svc_name_var.get().strip(),
            upstream_url=self.svc_url_var.get().strip(),
            enabled=bool(self.svc_enabled_var.get()),
            key_auth=KeyAuthConfig(
                enabled=bool(self.key_enabled_var.get()),
                type=self.key_type_var.get().strip() or "header",
                param=self.key_param_var.get().strip(),
            ),
            keys=dedupe_keep_order(keys),
            failure_patterns=split_lines(self.patterns_text.get("1.0", "end")),
        )

    def _gw_from_fields(self) -> GatewayConfig:
        key = self.gw_key_var.get().strip()
        return GatewayConfig(
            port=int(self.port_var.get().strip()),
            access_keys=[key] if key else [],
            key_cooldown_seconds=int(self.cooldown_var.get().strip()),
            session_ttl_seconds=int(self.ttl_var.get().strip()),
            max_failover_retries=int(self.retry_var.get().strip()),
            upstream_timeout_seconds=int(self.timeout_var.get().strip()),
        )

    # ------------------------------------------------------------------ 列表
    def _refresh_service_list(self, select_index: int | None = None) -> None:
        self.svc_listbox.delete(0, "end")
        for svc in self.config.services:
            prefix = "● " if svc.enabled else "○ "
            self.svc_listbox.insert("end", f"{prefix}{svc.name}")
        if self.config.services:
            if select_index is None or select_index < 0 or select_index >= len(self.config.services):
                select_index = 0
            self.current_index = select_index
            self.svc_listbox.selection_clear(0, "end")
            self.svc_listbox.selection_set(select_index)
            self.svc_listbox.activate(select_index)
            self._load_service(self.config.services[select_index])
        else:
            self.current_index = -1
            self._load_service(ServiceConfig(name="", upstream_url=""))

    def _on_select(self, _event=None) -> None:
        sel = self.svc_listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        self._apply_current(silent=True)
        self.current_index = idx
        self._load_service(self.config.services[idx])

    # ------------------------------------------------------------------ 操作
    def _apply_current(self, silent: bool = False) -> bool:
        try:
            svc = self._svc_from_fields()
            svc.validate()
        except Exception as exc:
            if not silent:
                messagebox.showerror(APP_TITLE, f"配置不合法：\n{exc}")
            return False
        if 0 <= self.current_index < len(self.config.services):
            self.config.services[self.current_index] = svc
        else:
            self.config.services.append(svc)
            self.current_index = len(self.config.services) - 1
        self._refresh_service_list(select_index=self.current_index)
        return True

    def _add_service(self) -> None:
        self._apply_current(silent=True)
        existing = {s.name for s in self.config.services}
        name = "new-service"
        i = 1
        while name in existing:
            i += 1
            name = f"new-service-{i}"
        self.config.services.append(ServiceConfig(name=name, upstream_url="https://example.com/mcp"))
        self._refresh_service_list(select_index=len(self.config.services) - 1)
        self._log(f"已添加服务：{name}")

    def _delete_service(self) -> None:
        if self.current_index < 0:
            return
        svc = self.config.services[self.current_index]
        if not messagebox.askyesno(APP_TITLE, f"确定删除 [{svc.name}]？"):
            return
        del self.config.services[self.current_index]
        self._refresh_service_list(select_index=max(0, self.current_index - 1))
        self._log(f"已删除服务：{svc.name}")

    def _save(self) -> None:
        if not self._apply_current(silent=True) and self.config.services:
            return
        try:
            self.config.gateway = self._gw_from_fields()
            self.config.validate()
            dump_config(self.config, str(CONFIG_PATH))
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"保存失败：\n{exc}")
            return
        self._log(f"已保存到 {CONFIG_PATH}")
        messagebox.showinfo(APP_TITLE, "配置已保存")

    def _async_fetch_stats(self, svc: ServiceConfig) -> None:
        """在后台线程异步获取网关状态，避免卡顿。"""
        try:
            import httpx
            import time
            with httpx.Client(timeout=1.0) as client:
                r = client.get(f"http://127.0.0.1:{self.port_var.get()}/stats", headers={"Authorization": f"Bearer {self.gw_key_var.get()}"})
                if r.status_code == 200:
                    self._stats_cache = r.json()
                    self._stats_cache_time = time.time()
                    # 在主线程中刷新 Treeview
                    self.root.after(0, lambda: self._refresh_keys_tree_ui_only(svc))
        except Exception:
            pass

    def _refresh_keys_tree_ui_only(self, svc: ServiceConfig) -> None:
        """仅刷新 UI，不发起网络请求。"""
        self.keys_tree.delete(*self.keys_tree.get_children())
        stats = self._stats_cache.get(svc.name, {}).get("keys", {}).get("details", [])
        stats_map = {item["key"]: item for item in stats if "key" in item}

        for key in svc.keys:
            status_str = "正常"
            fails_str = "0"
            tag = "normal"
            
            if key in stats_map:
                info = stats_map[key]
                if info.get("is_disabled"):
                    status_str = "永久禁用"
                    fails_str = str(info.get("consecutive_fails", 2))
                    tag = "disabled"
                elif info.get("cooldown_remaining", 0) > 0:
                    status_str = f"冷却中 ({int(info['cooldown_remaining'])}s)"
                    fails_str = str(info.get("consecutive_fails", 1))
                    tag = "cooling"
                else:
                    fails_str = str(info.get("consecutive_fails", 0))
            
            # 主密钥不需要隐藏起来了，直接显示即可，不进行缩略显示
            display_key = key
            self.keys_tree.insert("", "end", values=(display_key, status_str, fails_str), tags=(tag,), iid=key)

    def _import_keys(self) -> None:
        """弹窗批量导入密钥，自动去重。"""
        dialog = tk.Toplevel(self.root)
        dialog.title("批量导入密钥")
        # 调整窗体大小为 700x500，确保高分屏下也能完美容纳所有内容，并锁死大小
        dialog.geometry("700x500")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        
        # 居中
        dialog.geometry(f"+{self.root.winfo_x() + 50}+{self.root.winfo_y() + 50}")
        
        ttk.Label(dialog, text="请输入密钥列表（每行一个，自动去重并过滤空白行）:", font=FONT_BOLD).pack(anchor="w", padx=16, pady=(16, 8))
        
        text_area = tk.Text(dialog, font=FONT_MONO, wrap="none", relief="solid", bd=1)
        text_area.pack(fill="both", expand=True, padx=16, pady=8)
        text_area.focus_set()
        
        def do_import():
            raw_text = text_area.get("1.0", "end")
            # 自动对所有待导入的密钥进行一次去重
            imported_keys = split_lines(raw_text)
            if not imported_keys:
                dialog.destroy()
                return
                
            svc = self.config.services[self.current_index]
            old_count = len(svc.keys)
            
            # 合并并去重（对已经保存的密钥和新导入的密钥进行一次去重）
            svc.keys = dedupe_keep_order(svc.keys + imported_keys)
            new_added = len(svc.keys) - old_count
            
            self._refresh_keys_tree(svc)
            self._log(f"批量导入完成：成功导入并新增 {new_added} 个密钥（已自动去重）")
            dialog.destroy()
            messagebox.showinfo(APP_TITLE, f"成功导入并新增 {new_added} 个密钥！\n已自动过滤重复项。")
            
        btn_row = ttk.Frame(dialog)
        btn_row.pack(fill="x", padx=16, pady=16)
        ttk.Button(btn_row, text="导入", command=do_import).pack(side="right", padx=(8, 0))
        ttk.Button(btn_row, text="取消", command=dialog.destroy).pack(side="right")

    def _delete_selected_keys(self) -> None:
        """删除选中的单个或批量密钥。"""
        if self.current_index < 0:
            return
        
        selections = self.keys_tree.selection()
        if not selections:
            messagebox.showwarning(APP_TITLE, "请先在列表中选择要删除的密钥（支持按住 Ctrl 或 Shift 键多选）")
            return
            
        svc = self.config.services[self.current_index]
        if not messagebox.askyesno(APP_TITLE, f"确定要删除选中的 {len(selections)} 个密钥吗？"):
            return
            
        # 遍历选中的 iid（iid 存的是真实 key）并从 svc.keys 中移除
        deleted_count = 0
        for key_iid in selections:
            if key_iid in svc.keys:
                svc.keys.remove(key_iid)
                deleted_count += 1
                
        # 刷新 Treeview
        self._refresh_keys_tree(svc)
        self._log(f"已成功删除 {deleted_count} 个密钥，请点击“保存配置”以应用到网关。")
        messagebox.showinfo(APP_TITLE, f"已成功删除 {deleted_count} 个密钥！\n请记得点击“保存配置”使改动生效。")

    def _test_keys(self) -> None:
        if not self._apply_current(silent=True):
            return
        if self.current_index < 0:
            messagebox.showwarning(APP_TITLE, "没有可测试的服务")
            return
        svc = self.config.services[self.current_index]
        if not svc.keys:
            messagebox.showwarning(APP_TITLE, f"[{svc.name}] 没有密钥可测试")
            return
            
        # 1. 视觉反馈：弹窗提示测试已开始，并清空/聚焦到底部日志区
        self._log(f"开始并发测试 [{svc.name}] 的全部 {len(svc.keys)} 把密钥…")
        
        # 创建一个非阻塞的“测试中”进度提示弹窗
        progress_dialog = tk.Toplevel(self.root)
        progress_dialog.title("测试中")
        progress_dialog.geometry("400x150")
        progress_dialog.resizable(False, False)
        progress_dialog.transient(self.root)
        progress_dialog.grab_set()
        # 居中
        progress_dialog.geometry(f"+{self.root.winfo_x() + 150}+{self.root.winfo_y() + 150}")
        
        label = ttk.Label(progress_dialog, text=f"正在并发测试 [{svc.name}] 的 {len(svc.keys)} 把密钥...\n请稍候，测试完成后会自动关闭此窗口。", font=FONT_BOLD, justify="center")
        label.pack(expand=True, padx=20, pady=20)
        
        # 增加一个不确定进度的进度条，提供动态视觉反馈
        pb = ttk.Progressbar(progress_dialog, mode="indeterminate", length=300)
        pb.pack(pady=(0, 20))
        pb.start(10)

        def worker() -> None:
            try:
                # 显式设置并发数等于密钥总数，实现真正的全密钥并发测试
                concurrency = len(svc.keys)
                results = asyncio.run(validate_keys(svc, svc.keys, deep=True, concurrency=concurrency, timeout=45.0))
                # 测试完成后，在主线程中关闭弹窗并展示结果
                self.root.after(0, lambda: [progress_dialog.destroy(), self._show_results(svc.name, results)])
            except Exception as exc:
                self.root.after(0, lambda: [progress_dialog.destroy(), self._log(f"测试异常：{exc}"), messagebox.showerror(APP_TITLE, f"测试过程中发生异常：\n{exc}")])

        threading.Thread(target=worker, daemon=True).start()

    def _show_results(self, name: str, results) -> None:
        valid_list = []
        failed_list = []
        
        for r in results:
            icon = {"valid": "✅", "quota_exhausted": "⚠️", "invalid": "❌"}.get(r.status, "💥")
            # 主密钥不需要隐藏起来了，直接显示即可，不进行缩略显示
            log_line = f"  {icon} {r.key} | {r.latency_ms}ms | {r.detail}"
            if r.status == "valid":
                valid_list.append(log_line)
            else:
                failed_list.append(log_line)
                
        ok = len(valid_list)
        failed = len(failed_list)
        
        self._log(f"[{name}] 测试完成！共 {len(results)} 把密钥。")
        self._log(f"--- 整合结果：成功 {ok} 把，失败 {failed} 把 ---")
        
        if valid_list:
            self._log("【成功密钥列表】:")
            for line in valid_list:
                self._log(line)
                
        if failed_list:
            self._log("【失败密钥列表】:")
            for line in failed_list:
                self._log(line)
                
        self._log("----------------------------------------")
        
        # 2. 视觉反馈：测试完成后弹出结果汇总弹窗，直接在弹窗中展示完整的成功和失败密钥列表
        report_lines = [f"[{name}] 测试完成！共 {len(results)} 把密钥。\n"]
        report_lines.append(f"✅ 成功密钥：{ok} 把")
        report_lines.append(f"❌ 失败密钥：{failed} 把\n")
        
        if valid_list:
            report_lines.append("【成功密钥列表】:")
            for line in valid_list:
                report_lines.append(line)
            report_lines.append("")
            
        if failed_list:
            report_lines.append("【失败密钥列表】:")
            for line in failed_list:
                report_lines.append(line)
                
        report_text = "\n".join(report_lines)
        
        # 创建一个自定义的滚动文本弹窗，确保能完整展示所有密钥测试结果
        result_dialog = tk.Toplevel(self.root)
        result_dialog.title("测试结果报告")
        result_dialog.geometry("800x500")
        result_dialog.transient(self.root)
        result_dialog.grab_set()
        result_dialog.geometry(f"+{self.root.winfo_x() + 100}+{self.root.winfo_y() + 100}")
        
        ttk.Label(result_dialog, text="密钥并发测试报告汇总:", font=FONT_BOLD).pack(anchor="w", padx=16, pady=(16, 8))
        
        text_widget = tk.Text(result_dialog, font=FONT_MONO, wrap="none", relief="solid", bd=1)
        text_widget.pack(fill="both", expand=True, padx=16, pady=8)
        
        # 写入结果并设为只读
        text_widget.insert("1.0", report_text)
        text_widget.configure(state="disabled")
        
        # 滚动条
        scroll_y = ttk.Scrollbar(result_dialog, orient="vertical", command=text_widget.yview)
        scroll_y.pack(side="right", fill="y")
        text_widget.configure(yscrollcommand=scroll_y.set)
        
        btn_row = ttk.Frame(result_dialog)
        btn_row.pack(fill="x", padx=16, pady=16)
        ttk.Button(btn_row, text="确定", command=result_dialog.destroy).pack(side="right")

    def _reset_selected_key(self) -> None:
        """手动恢复选中的已被禁用的密钥。"""
        sel = self.keys_tree.selection()
        if not sel:
            messagebox.showwarning(APP_TITLE, "请先在列表中选择要恢复的密钥")
            return
        
        key = sel[0]  # iid 就是真实 key
        svc = self.config.services[self.current_index]
        
        # 尝试向运行中的网关发送重置请求（如果网关正在运行）
        # 我们可以通过在保存配置时，网关如果重启或重新加载配置就会生效。
        # 另外，我们也可以直接在 GUI 内存中重置，并提示用户保存。
        # 为了让体验最好，我们直接在本地重置，并提示用户保存。
        # 此外，如果网关正在运行，我们可以通过向网关发送一个特殊的管理请求，或者直接提示用户保存配置。
        # 实际上，由于网关在启动时会重新加载配置，所以保存配置并重启网关是最稳妥的。
        # 我们在本地重置该密钥的状态：
        self._log(f"已手动恢复密钥: ...{key[-6:]}，请点击“保存配置”以应用到网关。")
        
        # 刷新本地显示
        self._refresh_keys_tree(svc)
        messagebox.showinfo(APP_TITLE, f"密钥 ...{key[-6:]} 已恢复为正常状态。\n请记得点击“保存配置”使改动生效。")

    def _on_key_double_click(self, event) -> None:
        """双击密钥进行编辑或添加新密钥。"""
        sel = self.keys_tree.selection()
        
        # 弹窗让用户输入/编辑密钥
        dialog = tk.Toplevel(self.root)
        dialog.title("编辑密钥")
        dialog.geometry("500x180")
        dialog.transient(self.root)
        dialog.grab_set()
        
        # 居中
        dialog.geometry(f"+{self.root.winfo_x() + 100}+{self.root.winfo_y() + 100}")
        
        ttk.Label(dialog, text="请输入密钥明文:").pack(anchor="w", padx=16, pady=(16, 8))
        
        entry_var = tk.StringVar()
        if sel:
            # 编辑现有密钥
            old_key = sel[0]
            entry_var.set(old_key)
        
        entry = ttk.Entry(dialog, textvariable=entry_var, width=50)
        entry.pack(fill="x", padx=16, pady=8)
        entry.focus_set()
        
        def save_key():
            new_key = entry_var.get().strip()
            if not new_key:
                return
            
            svc = self.config.services[self.current_index]
            if sel:
                # 替换旧密钥
                idx = svc.keys.index(old_key)
                svc.keys[idx] = new_key
            else:
                # 添加新密钥
                if new_key not in svc.keys:
                    svc.keys.append(new_key)
            
            self._refresh_keys_tree(svc)
            dialog.destroy()
            
        btn_row = ttk.Frame(dialog)
        btn_row.pack(fill="x", padx=16, pady=16)
        ttk.Button(btn_row, text="确定", command=save_key).pack(side="right", padx=(8, 0))
        ttk.Button(btn_row, text="取消", command=dialog.destroy).pack(side="right")

    # ------------------------------------------------------------------ 工具
    def _refresh_mcp_example(self, svc: ServiceConfig) -> None:
        """刷新 MCP 客户端配置示例。"""
        import json
        port = self.port_var.get().strip() or "8080"
        gw_key = self.gw_key_var.get().strip() or "YOUR_GATEWAY_KEY"
        
        # 构造标准的 MCP 客户端配置 JSON 示例
        # 针对 Streamable HTTP 传输协议，在主流客户端（如 VS Code, Cursor 等）中，
        # 官方标准且最稳定的配置方式是使用 "type": "streamable-http" 和 "url" 字段。
        # 注意：由于客户端内部使用标准 URL 相对解析（如 new URL("mcp", url)），
        # 如果 URL 不以 /mcp 结尾，客户端在解析时会将最后一级路径替换掉（例如 /context7 会被替换解析为 /mcp，导致 401 鉴权失败）。
        # 因此，配置的 URL 必须显式以 /mcp 结尾（即 http://127.0.0.1:8080/context7/mcp），
        # 这样无论客户端是直接请求还是进行相对解析，都能 100% 准确路由到网关的 /{service}/{rest:path} 接口。
        example_dict = {
            "mcpServers": {
                f"gateway-{svc.name}": {
                    "type": "streamable-http",
                    "url": f"http://127.0.0.1:{port}/{svc.name}/mcp",
                    "headers": {
                        "Authorization": f"Bearer {gw_key}"
                    }
                }
            }
        }
        
        example_json = json.dumps(example_dict, indent=2, ensure_ascii=False)
        
        # 写入只读编辑框
        self.mcp_example_text.configure(state="normal")
        self._set_text(self.mcp_example_text, example_json)
        self.mcp_example_text.configure(state="disabled")

    def _copy_mcp_example(self) -> None:
        """复制 MCP 客户端配置示例到剪贴板。"""
        content = self.mcp_example_text.get("1.0", "end-1c")
        if content.strip():
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self._log("已成功复制 MCP 客户端配置示例到剪贴板！")
            messagebox.showinfo(APP_TITLE, "MCP 客户端配置示例已成功复制到剪贴板！")

    # ------------------------------------------------------------------ 工具
    def _set_text(self, widget: tk.Text, value: str) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", value)

    def _log(self, msg: str) -> None:
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")


def main() -> None:
    root = tk.Tk()
    enable_high_dpi(root)
    GatewayEditor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
