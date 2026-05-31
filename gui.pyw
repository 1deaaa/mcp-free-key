# -*- coding: utf-8 -*-
"""MCP 聚合网关配置编辑器 - CustomTkinter 重构版。

功能：
- 编辑网关端口、统一访问密钥。
- 增删改 MCP 服务（上游 URL、密钥注入方式、失败特征）。
- 批量添加密钥、自动去重。
- 测试选中/全部密钥是否有效且有额度。
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from src.config import (
    DEFAULT_CONFIG_PATH,
    GatewayConfig,
    KeyAuthConfig,
    ServiceConfig,
    dump_config,
    load_config,
)
from src.validator import validate_keys

# ── 主题 ──────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

APP_TITLE = "MCP 聚合网关"
CONFIG_PATH = Path(DEFAULT_CONFIG_PATH)

# 颜色常量
CLR_BG       = "#1a1a2e"
CLR_PANEL    = "#16213e"
CLR_CARD     = "#0f3460"
CLR_ACCENT   = "#4f8ef7"
CLR_ACCENT2  = "#7c3aed"
CLR_SUCCESS  = "#22c55e"
CLR_WARN     = "#f59e0b"
CLR_ERROR    = "#ef4444"
CLR_TEXT     = "#e2e8f0"
CLR_MUTED    = "#94a3b8"
CLR_BORDER   = "#334155"
CLR_ENTRY_BG = "#1e293b"
CLR_HOVER    = "#2d4a7a"


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


# ── 主窗口 ────────────────────────────────────────────────────────────────────
class GatewayEditor:
    """图形界面主控制器。"""

    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1280x800")
        self.root.minsize(1100, 700)

        # 实例级缓存（避免类变量在多实例间共享）
        self._stats_cache: dict = {}
        self._stats_cache_time: float = 0.0

        self.config = load_config(str(CONFIG_PATH), strict=False)
        self.current_index = 0 if self.config.services else -1

        self._build_ui()
        self._load_gateway()
        self._refresh_service_list(select_index=self.current_index)

    # ── 布局构建 ──────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        # 最外层容器
        outer = ctk.CTkFrame(self.root, fg_color=CLR_BG, corner_radius=0)
        outer.grid(row=0, column=0, sticky="nsew")
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(1, weight=1)

        self._build_header(outer)
        self._build_body(outer)

    def _build_header(self, parent) -> None:
        """顶部标题栏 + 网关设置（单行紧凑布局）。"""
        header = ctk.CTkFrame(parent, fg_color=CLR_PANEL, corner_radius=0, height=60)
        header.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        header.grid_columnconfigure(1, weight=1)
        header.grid_propagate(False)

        # 左侧品牌标题（紧凑单行）
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="ns", padx=(16, 20), pady=8)
        ctk.CTkLabel(title_box, text="⚡ MCP Gateway",
                     font=ctk.CTkFont(size=16, weight="bold"),
                     text_color=CLR_ACCENT).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(title_box, text="聚合网关配置管理器",
                     font=ctk.CTkFont(size=11),
                     text_color=CLR_MUTED).pack(side="left")

        # 右侧：所有网关参数一行排列
        gw_box = ctk.CTkFrame(header, fg_color="transparent")
        gw_box.grid(row=0, column=1, sticky="nsew", padx=(0, 16), pady=8)
        self._build_gateway_fields(gw_box)

    def _build_gateway_fields(self, parent) -> None:
        """网关参数输入区（单行）。"""
        self.port_var     = ctk.StringVar()
        self.gw_key_var   = ctk.StringVar()
        self.cooldown_var = ctk.StringVar()
        self.ttl_var      = ctk.StringVar()
        self.retry_var    = ctk.StringVar()
        self.timeout_var  = ctk.StringVar()

        # 全部参数一行
        self._lbl_entry(parent, "端口", self.port_var, width=70)
        self._lbl_entry(parent, "访问密钥", self.gw_key_var, width=260)
        self._lbl_entry(parent, "冷却(秒)", self.cooldown_var, width=70)
        self._lbl_entry(parent, "TTL(秒)", self.ttl_var, width=70)
        self._lbl_entry(parent, "重试", self.retry_var, width=50)
        self._lbl_entry(parent, "超时(秒)", self.timeout_var, width=70)

    def _lbl_entry(self, parent, label: str, var: ctk.StringVar, width: int = 120) -> None:
        """标签 + 输入框组合（横向排列）。"""
        ctk.CTkLabel(parent, text=label, font=ctk.CTkFont(size=12),
                     text_color=CLR_MUTED).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(parent, textvariable=var, width=width,
                     fg_color=CLR_ENTRY_BG, border_color=CLR_BORDER,
                     text_color=CLR_TEXT, font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 16))

    def _build_body(self, parent) -> None:
        """中部主体：左侧服务列表（固定宽）+ 右侧编辑区（拉伸）。"""
        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=16)
        body.grid_columnconfigure(0, weight=0, minsize=200)  # 左侧固定
        body.grid_columnconfigure(1, weight=1)               # 右侧拉伸
        body.grid_rowconfigure(0, weight=1)

        # 左侧：服务列表
        self._build_service_list(body)
        # 右侧：编辑区 + 日志
        self._build_right_panel(body)

    def _build_service_list(self, parent) -> None:
        """左侧服务列表面板。"""
        left = ctk.CTkFrame(parent, fg_color=CLR_CARD, corner_radius=8, width=200)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_propagate(False)
        left.grid_rowconfigure(1, weight=1)   # 列表行拉伸
        left.grid_columnconfigure(0, weight=1)

        # 标题
        ctk.CTkLabel(left, text="📋 服务列表", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=CLR_ACCENT).grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 6))

        # 列表框（不设 width，让 grid 控制宽度）
        self.svc_listbox = tk.Listbox(left, font=("Microsoft YaHei UI", 11),
                                      bg=CLR_ENTRY_BG, fg=CLR_TEXT, selectmode="single",
                                      activestyle="none", relief="flat", bd=0,
                                      highlightthickness=0,
                                      selectbackground=CLR_HOVER, selectforeground=CLR_TEXT)
        self.svc_listbox.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(0, 10))
        self.svc_listbox.bind("<<ListboxSelect>>", self._on_select)

        # 滚动条
        scrollbar = tk.Scrollbar(left, command=self.svc_listbox.yview,
                                 bg=CLR_BORDER, activebackground=CLR_HOVER,
                                 troughcolor=CLR_ENTRY_BG, width=6, relief="flat")
        scrollbar.grid(row=1, column=1, sticky="ns", padx=(0, 6), pady=(0, 10))
        self.svc_listbox.config(yscrollcommand=scrollbar.set)

    def _build_right_panel(self, parent) -> None:
        """右侧编辑区 + 日志。"""
        right = ctk.CTkFrame(parent, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=0)

        # 上半部分：编辑区
        self._build_service_editor(right)
        # 下半部分：日志
        self._build_log_panel(right)

    def _build_service_editor(self, parent) -> None:
        """服务编辑区。"""
        editor = ctk.CTkFrame(parent, fg_color=CLR_CARD, corner_radius=8)
        editor.grid(row=0, column=0, sticky="nsew", padx=0, pady=(0, 12))
        editor.grid_columnconfigure(0, weight=1)
        editor.grid_rowconfigure(2, weight=1)

        # ── 标题 + 配置一行
        hdr = ctk.CTkFrame(editor, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        ctk.CTkLabel(hdr, text="⚙️ 服务详情",
                    font=ctk.CTkFont(size=13, weight="bold"),
                    text_color=CLR_ACCENT).pack(side="left")

        # 变量声明
        self.svc_name_var    = ctk.StringVar()
        self.svc_enabled_var = ctk.BooleanVar(value=True)
        self.svc_url_var     = ctk.StringVar()
        self.key_enabled_var = ctk.BooleanVar(value=True)
        self.key_type_var    = ctk.StringVar(value="header")
        self.key_param_var   = ctk.StringVar()

        # ── 配置一行：服务名 | 启用 | 上游URL | 密钥轮询 | 注入方式 | 字段名
        cfg_row = ctk.CTkFrame(editor, fg_color="transparent")
        cfg_row.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))

        ctk.CTkLabel(cfg_row, text="服务名", text_color=CLR_MUTED,
                    font=ctk.CTkFont(size=10)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cfg_row, textvariable=self.svc_name_var, state="readonly",
                    fg_color=CLR_ENTRY_BG, border_color=CLR_BORDER,
                    text_color=CLR_TEXT, font=ctk.CTkFont(size=11),
                    width=110).pack(side="left", padx=(0, 6))
        ctk.CTkCheckBox(cfg_row, text="启用", variable=self.svc_enabled_var,
                       font=ctk.CTkFont(size=11), text_color=CLR_TEXT,
                       width=60).pack(side="left", padx=(0, 14))
        ctk.CTkLabel(cfg_row, text="|", text_color=CLR_BORDER,
                    font=ctk.CTkFont(size=13)).pack(side="left", padx=(0, 14))
        ctk.CTkLabel(cfg_row, text="上游URL", text_color=CLR_MUTED,
                    font=ctk.CTkFont(size=10)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cfg_row, textvariable=self.svc_url_var, state="readonly",
                    fg_color=CLR_ENTRY_BG, border_color=CLR_BORDER,
                    text_color=CLR_TEXT, font=ctk.CTkFont(size=11)).pack(
                    side="left", fill="x", expand=True, padx=(0, 14))
        ctk.CTkLabel(cfg_row, text="|", text_color=CLR_BORDER,
                    font=ctk.CTkFont(size=13)).pack(side="left", padx=(0, 14))
        ctk.CTkCheckBox(cfg_row, text="密钥轮询", variable=self.key_enabled_var,
                       font=ctk.CTkFont(size=11), text_color=CLR_TEXT,
                       width=80).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(cfg_row, text="注入方式", text_color=CLR_MUTED,
                    font=ctk.CTkFont(size=10)).pack(side="left", padx=(0, 4))
        type_combo = ctk.CTkComboBox(cfg_row, variable=self.key_type_var,
                       values=["header", "query"], state="readonly",
                       fg_color=CLR_ENTRY_BG, border_color=CLR_BORDER,
                       text_color=CLR_TEXT, font=ctk.CTkFont(size=10), width=90)
        type_combo.pack(side="left", padx=(0, 10))
        self._add_tooltip(type_combo,
                         "header：密钥通过 HTTP 请求头传递（如 Authorization）\n"
                         "query ：密钥通过 URL 查询参数传递（如 ?apiKey=xxx）")
        ctk.CTkLabel(cfg_row, text="字段名", text_color=CLR_MUTED,
                    font=ctk.CTkFont(size=10)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cfg_row, textvariable=self.key_param_var,
                    fg_color=CLR_ENTRY_BG, border_color=CLR_BORDER,
                    text_color=CLR_TEXT, font=ctk.CTkFont(size=10),
                    width=150).pack(side="left")

        # ── 密钥管理 + 失败特征（两列布局）
        lower = ctk.CTkFrame(editor, fg_color="transparent")
        lower.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        lower.grid_columnconfigure(0, weight=2)  # 密钥列表占 2/3
        lower.grid_columnconfigure(1, weight=1)  # 失败特征占 1/3
        lower.grid_rowconfigure(1, weight=1)

        # 左侧：密钥列表
        keys_lbl = ctk.CTkLabel(lower, text="🔑 密钥状态", font=ctk.CTkFont(size=11, weight="bold"),
                               text_color=CLR_ACCENT)
        keys_lbl.grid(row=0, column=0, sticky="w", pady=(0, 4))

        self.keys_tree = tk.Listbox(lower, height=6, font=("Consolas", 10),
                                   bg=CLR_ENTRY_BG, fg=CLR_TEXT, selectmode="extended",
                                   activestyle="none", relief="flat", bd=0,
                                   highlightthickness=0)
        self.keys_tree.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        keys_scroll = tk.Scrollbar(lower, command=self.keys_tree.yview,
                                  bg=CLR_BORDER, activebackground=CLR_HOVER, width=8)
        keys_scroll.grid(row=1, column=0, sticky="nse", padx=(0, 0))
        self.keys_tree.config(yscrollcommand=keys_scroll.set)

        # 右侧：失败特征
        patterns_lbl = ctk.CTkLabel(lower, text="⚠️ 失败特征", font=ctk.CTkFont(size=11, weight="bold"),
                                   text_color=CLR_ACCENT)
        patterns_lbl.grid(row=0, column=1, sticky="w", pady=(0, 4))

        self.patterns_text = tk.Text(lower, height=6, width=20, font=("Consolas", 10),
                                    bg=CLR_ENTRY_BG, fg=CLR_TEXT, relief="flat", bd=0,
                                    highlightthickness=0, wrap="none")
        self.patterns_text.grid(row=1, column=1, sticky="nsew")
        patterns_scroll = tk.Scrollbar(lower, command=self.patterns_text.yview,
                                      bg=CLR_BORDER, activebackground=CLR_HOVER, width=8)
        patterns_scroll.grid(row=1, column=1, sticky="nse")
        self.patterns_text.config(yscrollcommand=patterns_scroll.set)

        # 操作按钮行
        btn_row = ctk.CTkFrame(editor, fg_color="transparent")
        btn_row.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
        for i in range(5):
            btn_row.grid_columnconfigure(i, weight=1)

        ctk.CTkButton(btn_row, text="💾 保存配置", command=self._save,
                     fg_color=CLR_ACCENT, hover_color=CLR_HOVER,
                     font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkButton(btn_row, text="📥 批量导入密钥", command=self._import_keys,
                     fg_color="#0d9488", hover_color="#0f766e",
                     font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=1, sticky="ew", padx=4)
        ctk.CTkButton(btn_row, text="🧪 测试选中密钥", command=self._test_selected_keys,
                     fg_color=CLR_ACCENT2, hover_color="#6d28d9",
                     font=ctk.CTkFont(size=11)).grid(row=0, column=2, sticky="ew", padx=4)
        ctk.CTkButton(btn_row, text="🧪 测试全部密钥", command=self._test_all_keys,
                     fg_color="#0284c7", hover_color="#0369a1",
                     font=ctk.CTkFont(size=11)).grid(row=0, column=3, sticky="ew", padx=4)
        ctk.CTkButton(btn_row, text="🗑️ 删除选中", command=self._delete_selected_keys,
                     fg_color="#7f1d1d", hover_color="#991b1b",
                     font=ctk.CTkFont(size=11)).grid(row=0, column=4, sticky="ew", padx=(4, 0))

    def _build_log_panel(self, parent) -> None:
        """底部：MCP 示例 + 测试日志（左右分栏）。"""
        bottom = ctk.CTkFrame(parent, fg_color="transparent")
        bottom.grid(row=1, column=0, sticky="nsew")
        bottom.grid_columnconfigure(0, weight=1)
        bottom.grid_columnconfigure(1, weight=1)
        bottom.grid_rowconfigure(0, weight=1)
        parent.grid_rowconfigure(1, minsize=200)

        # 左：MCP 配置示例
        mcp_frame = ctk.CTkFrame(bottom, fg_color=CLR_CARD, corner_radius=8)
        mcp_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        mcp_frame.grid_columnconfigure(0, weight=1)
        mcp_frame.grid_rowconfigure(1, weight=1)

        mcp_hdr = ctk.CTkFrame(mcp_frame, fg_color="transparent")
        mcp_hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        ctk.CTkLabel(mcp_hdr, text="📋 MCP 客户端配置示例", font=ctk.CTkFont(size=12, weight="bold"),
                    text_color=CLR_ACCENT).pack(side="left")
        ctk.CTkButton(mcp_hdr, text="复制", command=self._copy_mcp_example,
                     width=60, height=24, fg_color=CLR_ACCENT, hover_color=CLR_HOVER,
                     font=ctk.CTkFont(size=10)).pack(side="right")

        self.mcp_example_text = tk.Text(mcp_frame, height=7, font=("Consolas", 10),
                                       bg=CLR_ENTRY_BG, fg=CLR_TEXT, relief="flat", bd=0,
                                       highlightthickness=0, wrap="word", state="disabled")
        self.mcp_example_text.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))

        # 右：测试日志
        log_frame = ctk.CTkFrame(bottom, fg_color=CLR_CARD, corner_radius=8)
        log_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)

        log_hdr = ctk.CTkFrame(log_frame, fg_color="transparent")
        log_hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        ctk.CTkLabel(log_hdr, text="📝 测试日志", font=ctk.CTkFont(size=12, weight="bold"),
                    text_color=CLR_ACCENT).pack(side="left")
        ctk.CTkButton(log_hdr, text="清空", command=lambda: self._set_text(self.log_text, ""),
                     width=60, height=24, fg_color=CLR_CARD, hover_color=CLR_HOVER,
                     border_width=1, border_color=CLR_BORDER,
                     font=ctk.CTkFont(size=10)).pack(side="right")

        self.log_text = tk.Text(log_frame, height=7, font=("Consolas", 10),
                               bg=CLR_ENTRY_BG, fg=CLR_TEXT, relief="flat", bd=0,
                               highlightthickness=0, wrap="word")
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 12))
        log_scroll = tk.Scrollbar(log_frame, command=self.log_text.yview,
                                 bg=CLR_BORDER, activebackground=CLR_HOVER, width=8)
        log_scroll.grid(row=1, column=1, sticky="ns", pady=(0, 12))
        self.log_text.config(yscrollcommand=log_scroll.set)

    # ── 数据加载 ──────────────────────────────────────────────────────────────
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
        self._refresh_keys_list(svc)
        self._refresh_mcp_example(svc)

    def _refresh_service_list(self, select_index: int | None = None) -> None:
        self.svc_listbox.delete(0, "end")
        for svc in self.config.services:
            prefix = "● " if svc.enabled else "○ "
            self.svc_listbox.insert("end", f"{prefix}{svc.name}")
            # 着色
            idx = self.svc_listbox.size() - 1
            self.svc_listbox.itemconfig(idx, fg=CLR_SUCCESS if svc.enabled else CLR_MUTED)
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

    def _refresh_keys_list(self, svc: ServiceConfig) -> None:
        """刷新密钥列表，根据状态着色。"""
        self.keys_tree.delete(0, "end")
        # 后台异步拉取状态（不阻塞 UI）
        if not self._stats_cache or (time.time() - self._stats_cache_time > 3.0):
            threading.Thread(target=self._async_fetch_stats, args=(svc,), daemon=True).start()
        stats = self._stats_cache.get(svc.name, {}).get("keys", {}).get("details", [])
        stats_map = {item["key"]: item for item in stats if "key" in item}

        for key in svc.keys:
            status_str = "正常"
            tag_color = CLR_SUCCESS
            if key in stats_map:
                info = stats_map[key]
                if info.get("is_disabled"):
                    status_str = "永久禁用"
                    tag_color = CLR_ERROR
                elif info.get("cooldown_remaining", 0) > 0:
                    status_str = f"冷却中({int(info['cooldown_remaining'])}s)"
                    tag_color = CLR_WARN
            # 截断显示：前8位...后8位
            display = key if len(key) <= 24 else f"{key[:12]}...{key[-8:]}"
            self.keys_tree.insert("end", f"  {display}  [{status_str}]")
            idx = self.keys_tree.size() - 1
            self.keys_tree.itemconfig(idx, fg=tag_color)

    def _on_select(self, _event=None) -> None:
        sel = self.svc_listbox.curselection()
        if not sel:
            return
        idx = int(sel[0])
        self._apply_current(silent=True)
        self.current_index = idx
        self._load_service(self.config.services[idx])

    # ── 数据提取 ──────────────────────────────────────────────────────────────
    def _svc_from_fields(self) -> ServiceConfig:
        """从界面字段提取当前服务配置。"""
        # 从 Listbox 的 iid 映射回真实密钥
        # keys_tree 存的是显示文本，真实密钥存在 config.services 中
        # 只有在用户通过导入/删除操作时才修改 svc.keys，这里直接读取
        if 0 <= self.current_index < len(self.config.services):
            keys = self.config.services[self.current_index].keys
        else:
            keys = []
        return ServiceConfig(
            name=self.svc_name_var.get().strip(),
            upstream_url=self.svc_url_var.get().strip(),
            enabled=bool(self.svc_enabled_var.get()),
            key_auth=KeyAuthConfig(
                enabled=bool(self.key_enabled_var.get()),
                type=self.key_type_var.get().strip() or "header",
                param=self.key_param_var.get().strip(),
            ),
            keys=keys,
            failure_patterns=split_lines(self.patterns_text.get("1.0", "end")),
        )

    def _gw_from_fields(self) -> GatewayConfig:
        key = self.gw_key_var.get().strip()
        return GatewayConfig(
            port=int(self.port_var.get().strip() or "8080"),
            access_keys=[key] if key else [],
            key_cooldown_seconds=int(self.cooldown_var.get().strip() or "1800"),
            session_ttl_seconds=int(self.ttl_var.get().strip() or "1800"),
            max_failover_retries=int(self.retry_var.get().strip() or "3"),
            upstream_timeout_seconds=int(self.timeout_var.get().strip() or "120"),
        )

    def _apply_current(self, silent: bool = False) -> bool:
        try:
            svc = self._svc_from_fields()
            svc.validate_basic()
        except Exception as exc:
            if not silent:
                messagebox.showerror(APP_TITLE, f"配置不合法：\n{exc}")
            return False
        if 0 <= self.current_index < len(self.config.services):
            self.config.services[self.current_index] = svc
        else:
            self.config.services.append(svc)
            self.current_index = len(self.config.services) - 1
        return True

    # ── 操作 ──────────────────────────────────────────────────────────────────
    def _save(self) -> None:
        # 1. 同步编辑区字段到 config（允许密钥为空不报错）
        if 0 <= self.current_index < len(self.config.services):
            svc = self.config.services[self.current_index]
            svc.enabled = bool(self.svc_enabled_var.get())
            svc.failure_patterns = split_lines(self.patterns_text.get("1.0", "end"))
            svc.key_auth.enabled = bool(self.key_enabled_var.get())
            svc.key_auth.type = self.key_type_var.get().strip() or "header"
            svc.key_auth.param = self.key_param_var.get().strip()
        # 2. 校验网关参数
        try:
            self.config.gateway = self._gw_from_fields()
            self.config.gateway.validate()
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"网关参数不合法：\n{exc}")
            self._log(f"❌ 保存失败：{exc}")
            return
        # 3. 写入文件
        try:
            dump_config(self.config, str(CONFIG_PATH))
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"保存失败：\n{exc}")
            self._log(f"❌ 保存失败：{exc}")
            return
        self._log(f"✅ 已保存到 {CONFIG_PATH}")

    def _import_keys(self) -> None:
        """弹窗批量导入密钥。"""
        if self.current_index < 0:
            messagebox.showwarning(APP_TITLE, "请先选择一个服务")
            return
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("批量导入密钥")
        dialog.geometry("700x500")
        dialog.minsize(500, 300)
        dialog.grab_set()
        dialog.configure(fg_color=CLR_BG)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(1, weight=1)

        # 标题
        ctk.CTkLabel(dialog, text="请输入密钥列表（每行一个，自动去重）:",
                    font=ctk.CTkFont(size=12, weight="bold"),
                    text_color=CLR_TEXT).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 8))

        # 文本框
        text_area = tk.Text(dialog, font=("Consolas", 11), bg=CLR_ENTRY_BG, fg=CLR_TEXT,
                           relief="flat", bd=0, highlightthickness=0, wrap="none",
                           insertbackground=CLR_TEXT)
        text_area.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))
        text_area.focus_set()

        def do_import():
            raw_text = text_area.get("1.0", "end")
            imported_keys = split_lines(raw_text)
            if not imported_keys:
                dialog.destroy()
                return
            svc = self.config.services[self.current_index]
            old_count = len(svc.keys)
            svc.keys = dedupe_keep_order(svc.keys + imported_keys)
            new_added = len(svc.keys) - old_count
            self._refresh_keys_list(svc)
            self._log(f"✅ 批量导入完成：新增 {new_added} 个密钥（已去重）")
            dialog.destroy()
            messagebox.showinfo(APP_TITLE, f"成功导入 {new_added} 个新密钥！")

        # 按钮
        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 16))
        ctk.CTkButton(btn_row, text="取消", command=dialog.destroy, width=80,
                     fg_color=CLR_CARD, hover_color=CLR_HOVER, border_width=1, border_color=CLR_BORDER).pack(side="right", padx=(4, 0))
        ctk.CTkButton(btn_row, text="导入", command=do_import, width=80,
                     fg_color=CLR_ACCENT, hover_color=CLR_HOVER).pack(side="right", padx=(0, 4))

    def _delete_selected_keys(self) -> None:
        """删除选中的密钥。"""
        if self.current_index < 0:
            return
        selections = self.keys_tree.curselection()
        if not selections:
            messagebox.showwarning(APP_TITLE, "请先选择要删除的密钥")
            return
        svc = self.config.services[self.current_index]
        if not messagebox.askyesno(APP_TITLE, f"确定删除选中的 {len(selections)} 个密钥吗？"):
            return
        # 按倒序删除（避免索引偏移）
        for idx in sorted(selections, reverse=True):
            if 0 <= idx < len(svc.keys):
                del svc.keys[idx]
        self._refresh_keys_list(svc)
        self._log(f"✅ 已删除 {len(selections)} 个密钥，请保存配置")
        messagebox.showinfo(APP_TITLE, f"已删除 {len(selections)} 个密钥")

    def _test_selected_keys(self) -> None:
        """测试选中的密钥。"""
        if not self._apply_current(silent=True):
            return
        if self.current_index < 0:
            messagebox.showwarning(APP_TITLE, "没有可测试的服务")
            return
        svc = self.config.services[self.current_index]
        selections = self.keys_tree.curselection()
        if not selections:
            messagebox.showwarning(APP_TITLE, "请先选择要测试的密钥")
            return
        # 提取选中的密钥
        selected_keys = [svc.keys[idx] for idx in selections if 0 <= idx < len(svc.keys)]
        if not selected_keys:
            messagebox.showwarning(APP_TITLE, "无有效的密钥可测试")
            return
        self._run_test(svc, selected_keys)

    def _test_all_keys(self) -> None:
        """测试全部密钥。"""
        if not self._apply_current(silent=True):
            return
        if self.current_index < 0:
            messagebox.showwarning(APP_TITLE, "没有可测试的服务")
            return
        svc = self.config.services[self.current_index]
        if not svc.keys:
            messagebox.showwarning(APP_TITLE, f"[{svc.name}] 没有密钥可测试")
            return
        self._run_test(svc, svc.keys)

    def _run_test(self, svc: ServiceConfig, keys: list[str]) -> None:
        """执行密钥测试（后台线程）。"""
        self._log(f"🔄 开始并发测试 [{svc.name}] 的 {len(keys)} 把密钥…")
        
        # 进度弹窗
        progress_dialog = ctk.CTkToplevel(self.root)
        progress_dialog.title("测试中")
        progress_dialog.geometry("400x120")
        progress_dialog.resizable(False, False)
        progress_dialog.configure(fg_color=CLR_BG)
        progress_dialog.grab_set()

        ctk.CTkLabel(progress_dialog, text=f"正在测试 {len(keys)} 把密钥...",
                    font=ctk.CTkFont(size=12, weight="bold"),
                    text_color=CLR_TEXT).pack(expand=True, padx=20, pady=20)

        def worker() -> None:
            try:
                concurrency = min(len(keys), 5)
                results = asyncio.run(validate_keys(svc, keys, deep=True, concurrency=concurrency, timeout=45.0))
                self.root.after(0, lambda: [progress_dialog.destroy(), self._show_results(svc.name, results)])
            except Exception as exc:
                self.root.after(0, lambda: [progress_dialog.destroy(), self._log(f"❌ 测试异常：{exc}")])

        threading.Thread(target=worker, daemon=True).start()

    def _show_results(self, name: str, results) -> None:
        valid_list, failed_list = [], []
        for r in results:
            icon = {"valid": "✅", "quota_exhausted": "⚠️", "invalid": "❌"}.get(r.status, "💥")
            line = f"  {icon} {r.key} | {r.latency_ms}ms | {r.detail}"
            (valid_list if r.status == "valid" else failed_list).append(line)

        ok, failed = len(valid_list), len(failed_list)
        self._log(f"[{name}] 测试完成：✅ {ok} 把有效，❌ {failed} 把失败")
        for line in valid_list + failed_list:
            self._log(line)
        self._log("─" * 60)

        # 结果弹窗
        result_dialog = ctk.CTkToplevel(self.root)
        result_dialog.title("测试结果")
        result_dialog.geometry("800x480")
        result_dialog.configure(fg_color=CLR_BG)
        result_dialog.grab_set()
        result_dialog.grid_columnconfigure(0, weight=1)
        result_dialog.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(result_dialog, text=f"[{name}] 测试完成：✅ {ok} 把有效  ❌ {failed} 把失败",
                    font=ctk.CTkFont(size=13, weight="bold"),
                    text_color=CLR_TEXT).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 8))

        report = "\n".join([f"✅ 有效密钥 ({ok} 把):"] + valid_list +
                           ["", f"❌ 失败密钥 ({failed} 把):"] + failed_list)
        text_widget = tk.Text(result_dialog, font=("Consolas", 10), bg=CLR_ENTRY_BG, fg=CLR_TEXT,
                             relief="flat", bd=0, highlightthickness=0, wrap="none")
        text_widget.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)
        text_widget.insert("1.0", report)
        text_widget.configure(state="disabled")

        ctk.CTkButton(result_dialog, text="确定", command=result_dialog.destroy,
                     fg_color=CLR_ACCENT, hover_color=CLR_HOVER).grid(row=2, column=0, sticky="e", padx=16, pady=16)

    def _async_fetch_stats(self, svc: ServiceConfig) -> None:
        """后台线程拉取网关状态。"""
        try:
            import httpx
            with httpx.Client(timeout=1.0) as client:
                r = client.get(
                    f"http://127.0.0.1:{self.port_var.get()}/stats",
                    headers={"Authorization": f"Bearer {self.gw_key_var.get()}"}
                )
                if r.status_code == 200:
                    self._stats_cache = r.json()
                    self._stats_cache_time = time.time()
                    self.root.after(0, lambda: self._refresh_keys_list(svc))
        except Exception:
            pass

    def _refresh_mcp_example(self, svc: ServiceConfig) -> None:
        """刷新 MCP 客户端配置示例。"""
        port = self.port_var.get().strip() or "8080"
        gw_key = self.gw_key_var.get().strip() or "YOUR_GATEWAY_KEY"
        example_dict = {
            "mcpServers": {
                f"gateway-{svc.name}": {
                    "type": "streamable-http",
                    "url": f"http://127.0.0.1:{port}/{svc.name}/mcp",
                    "headers": {"Authorization": f"Bearer {gw_key}"}
                }
            }
        }
        example_json = json.dumps(example_dict, indent=2, ensure_ascii=False)
        self.mcp_example_text.configure(state="normal")
        self._set_text(self.mcp_example_text, example_json)
        self.mcp_example_text.configure(state="disabled")

    def _copy_mcp_example(self) -> None:
        content = self.mcp_example_text.get("1.0", "end-1c")
        if content.strip():
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
            self._log("✅ 已复制 MCP 客户端配置到剪贴板")
            messagebox.showinfo(APP_TITLE, "已复制到剪贴板！")

    def _set_text(self, widget: tk.Text, value: str) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", value)

    def _log(self, msg: str) -> None:
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _add_tooltip(self, widget, text: str) -> None:
        """为组件添加 tooltip（悬停提示）。"""
        def on_enter(event):
            tooltip = tk.Toplevel(widget)
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{event.x_root + 10}+{event.y_root + 10}")
            label = tk.Label(tooltip, text=text, background=CLR_PANEL, foreground=CLR_TEXT,
                           font=("Microsoft YaHei UI", 9), padx=8, pady=4, relief="solid", bd=1)
            label.pack()
            widget.tooltip = tooltip

        def on_leave(event):
            if hasattr(widget, 'tooltip'):
                widget.tooltip.destroy()
                del widget.tooltip

        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)


# ── 入口 ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # 在创建窗口前先设置 DPI 感知（不需要 Tk 实例）
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    root = ctk.CTk()
    root.configure(fg_color=CLR_BG)
    GatewayEditor(root)
    root.mainloop()


if __name__ == "__main__":
    main()

