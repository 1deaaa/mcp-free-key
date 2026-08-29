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
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

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
from src.validator import validate_keys

# ── 主题 ──────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

APP_TITLE = "MCP 聚合网关"
CONFIG_PATH = Path(DEFAULT_CONFIG_PATH)
PROJECT_ROOT = CONFIG_PATH.parent
START_SCRIPT = PROJECT_ROOT / "start.py"
ROUTING_MODE_LABELS = {
    ROUTING_MODE_ROUND_ROBIN: "轮询",
    ROUTING_MODE_PRIMARY_BACKUP: "主备",
}
ROUTING_LABEL_MODES = {label: mode for mode, label in ROUTING_MODE_LABELS.items()}


def _gateway_python_candidates() -> tuple[Path, ...]:
    """按项目虚拟环境、当前解释器的顺序返回网关启动解释器。"""
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

# 优先选择包含中文字符的字体，避免 Linux 回退到不完整或过于模糊的字体。
UI_FONT_CANDIDATES = (
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "WenQuanYi Zen Hei",
    "FangSong Ti",
    "Song Ti",
    "Mincho",
    "SimHei",
    "Droid Sans Fallback",
    "DejaVu Sans",
)
MONO_FONT_CANDIDATES = (
    "Noto Sans Mono CJK SC",
    "Sarasa Mono SC",
    "Source Han Mono SC",
    "WenQuanYi Zen Hei Mono",
    "FangSong Ti",
    "Song Ti",
    "Mincho",
    "Cascadia Mono",
    "Consolas",
    "DejaVu Sans Mono",
)
FONT_SCALE = 1.6


def select_font_family(
    available_families: tuple[str, ...],
    candidates: tuple[str, ...],
    fallback: str,
) -> str:
    """从当前系统字体中选择第一个匹配的字体族。"""
    available = {family.casefold(): family for family in available_families}
    for candidate in candidates:
        selected = available.get(candidate.casefold())
        if selected:
            return selected
    return fallback


def _resolve_font_families(root: tk.Misc) -> tuple[str, str]:
    """解析界面字体和等宽字体，兼容不同操作系统的字体安装情况。"""
    available = tuple(tkfont.families(root))
    ui_fallback = str(tkfont.nametofont("TkDefaultFont", root).actual("family"))
    mono_fallback = str(tkfont.nametofont("TkFixedFont", root).actual("family"))
    return (
        select_font_family(available, UI_FONT_CANDIDATES, ui_fallback),
        select_font_family(available, MONO_FONT_CANDIDATES, mono_fallback),
    )


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
        self.ui_font_family, self.mono_font_family = _resolve_font_families(root)
        self._server_process: subprocess.Popen | None = None
        self._suspend_auto_apply = True
        self._auto_apply_after_id: str | None = None
        self._auto_apply_generation = 0
        self._auto_apply_inflight = False

        # 实例级缓存（避免类变量在多实例间共享）
        self._stats_cache: dict = {}
        self._stats_cache_time: float = 0.0
        self._usage_cache: dict[tuple[str, str], UsageSnapshot] = {}
        self._restored_keys: set[tuple[str, str]] = set()  # (svc_name, key) 近期手动恢复的密钥
        self.state_store = KeyStateStore()

        self.config = load_config(str(CONFIG_PATH), strict=False)
        self.current_index = 0 if self.config.services else -1
        self._runtime_port = self.config.gateway.port
        self._runtime_access_key = self.config.gateway.access_keys[0] if self.config.gateway.access_keys else ""

        self._build_ui()
        self._load_gateway()
        self._refresh_service_list(select_index=self.current_index)
        self._suspend_auto_apply = False

    def _ui_font(self, size: int, weight: str = "normal") -> ctk.CTkFont:
        """创建使用系统中文字体的 CustomTkinter 字体。"""
        return ctk.CTkFont(
            family=self.ui_font_family,
            size=max(10, round(size * FONT_SCALE)),
            weight=weight,
        )

    def _tk_font(self, family: str, size: int) -> tuple[str, int]:
        """创建与界面字号统一的 Tk 原生字体。"""
        return family, max(10, round(size * FONT_SCALE))

    def _set_modal(self, dialog: ctk.CTkToplevel) -> None:
        """在弹窗可见后设置模态，兼容 Linux Tk 的映射时序。"""
        dialog.transient(self.root)
        try:
            dialog.update_idletasks()
            dialog.deiconify()
        except tk.TclError:
            return
        self._grab_when_viewable(dialog)

    def _grab_when_viewable(self, dialog: ctk.CTkToplevel) -> None:
        """等待窗口真正映射后再抢占输入焦点。"""
        try:
            if not dialog.winfo_exists():
                return
            if dialog.winfo_viewable():
                dialog.grab_set()
                dialog.focus_set()
                return
            dialog.after(30, lambda: self._grab_when_viewable(dialog))
        except tk.TclError:
            # 用户在窗口映射前关闭弹窗时无需再处理。
            return

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
        header = ctk.CTkFrame(parent, fg_color=CLR_PANEL, corner_radius=0, height=96)
        header.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        header.grid_columnconfigure(0, weight=1)
        header.grid_propagate(False)

        # 左侧品牌标题（紧凑单行）
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w", padx=(16, 20), pady=(8, 0))
        ctk.CTkLabel(title_box, text="⚡ MCP Gateway",
                     font=self._ui_font(16, "bold"),
                     text_color=CLR_ACCENT).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(title_box, text="聚合网关配置管理器",
                     font=self._ui_font(11),
                     text_color=CLR_MUTED).pack(side="left")

        # 右侧：所有网关参数一行排列
        gw_box = ctk.CTkFrame(header, fg_color="transparent")
        gw_box.grid(row=1, column=0, sticky="ew", padx=16, pady=(2, 8))
        self._build_gateway_fields(gw_box)

    def _build_gateway_fields(self, parent) -> None:
        """网关参数输入区（单行）。"""
        self.port_var     = ctk.StringVar()
        self.gw_key_var   = ctk.StringVar()
        self.cooldown_var = ctk.StringVar()
        self.ttl_var      = ctk.StringVar()
        self.retry_var    = ctk.StringVar()
        self.timeout_var  = ctk.StringVar()
        self.routing_mode_var = ctk.StringVar(value=ROUTING_MODE_LABELS[ROUTING_MODE_ROUND_ROBIN])

        # 全部参数一行
        self._lbl_entry(parent, "端口", self.port_var, width=70)
        self._lbl_entry(parent, "访问密钥", self.gw_key_var, width=260)
        self._lbl_entry(parent, "冷却(秒)", self.cooldown_var, width=70)
        self._lbl_entry(parent, "TTL(秒)", self.ttl_var, width=70)
        self._lbl_entry(parent, "转移次数", self.retry_var, width=70)
        self._lbl_entry(parent, "超时(秒)", self.timeout_var, width=70)
        ctk.CTkLabel(parent, text="路由模式", font=self._ui_font(12),
                     text_color=CLR_MUTED).pack(side="left", padx=(0, 4))
        mode_combo = ctk.CTkComboBox(
            parent,
            variable=self.routing_mode_var,
            values=list(ROUTING_LABEL_MODES),
            state="readonly",
            width=100,
            fg_color=CLR_ENTRY_BG,
            border_color=CLR_ACCENT,
            text_color=CLR_TEXT,
            font=self._ui_font(12),
        )
        mode_combo.pack(side="left", padx=(0, 0))
        self._add_tooltip(mode_combo, "轮询：各密钥均衡使用；主备：优先使用列表第一把可用密钥")
        for variable in (
            self.port_var,
            self.gw_key_var,
            self.cooldown_var,
            self.ttl_var,
            self.retry_var,
            self.timeout_var,
            self.routing_mode_var,
        ):
            self._watch_variable(variable)

    def _lbl_entry(self, parent, label: str, var: ctk.StringVar, width: int = 120) -> None:
        """标签 + 输入框组合（横向排列）。"""
        ctk.CTkLabel(parent, text=label, font=self._ui_font(12),
                     text_color=CLR_MUTED).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(parent, textvariable=var, width=width,
                     fg_color=CLR_ENTRY_BG, border_color=CLR_BORDER,
                     text_color=CLR_TEXT, font=self._ui_font(12)).pack(side="left", padx=(0, 16))

    def _watch_variable(self, variable) -> None:
        """监听配置变量，修改后通过统一防抖入口自动应用。"""
        variable.trace_add("write", self._on_setting_changed)

    def _on_setting_changed(self, *_args) -> None:
        """处理输入变量变化，并刷新会随网关设置变化的示例。"""
        if self._suspend_auto_apply:
            return
        self._schedule_auto_apply()
        if (
            hasattr(self, "mcp_example_text")
            and 0 <= self.current_index < len(self.config.services)
        ):
            self._refresh_mcp_example(self.config.services[self.current_index])

    def _on_text_setting_changed(self, _event=None):
        """处理失败特征文本变化。"""
        self._on_setting_changed()

    def _schedule_auto_apply(self) -> None:
        """防抖调度配置应用，避免连续输入时重复写文件和重载网关。"""
        if self._suspend_auto_apply:
            return
        self._auto_apply_generation += 1
        if self._auto_apply_inflight:
            return
        if self._auto_apply_after_id is not None:
            try:
                self.root.after_cancel(self._auto_apply_after_id)
            except tk.TclError:
                pass
        self._auto_apply_after_id = self.root.after(500, self._auto_apply)

    def _build_body(self, parent) -> None:
        """中部主体：上半区编辑，下半区全宽日志/示例。"""
        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=16)
        body.grid_columnconfigure(0, weight=0, minsize=200)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=3)
        body.grid_rowconfigure(1, weight=2, minsize=160)

        self._build_service_list(body)
        self._build_right_panel(body)
        self._build_log_panel(body)

    def _build_service_list(self, parent) -> None:
        """左侧服务列表面板。"""
        left = ctk.CTkFrame(parent, fg_color=CLR_CARD, corner_radius=8, width=200)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.grid_propagate(False)
        left.grid_rowconfigure(1, weight=1)   # 列表行拉伸
        left.grid_columnconfigure(0, weight=1)

        # 标题
        ctk.CTkLabel(left, text="📋 服务列表", font=self._ui_font(13, "bold"),
                     text_color=CLR_ACCENT).grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(12, 6))

        # 列表框（不设 width，让 grid 控制宽度）
        self.svc_listbox = tk.Listbox(left, font=self._tk_font(self.ui_font_family, 11),
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
        """右侧上半区：服务编辑区。"""
        right = ctk.CTkFrame(parent, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=1)

        self._build_service_editor(right)

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
                    font=self._ui_font(13, "bold"),
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
                    font=self._ui_font(10)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cfg_row, textvariable=self.svc_name_var, state="readonly",
                    fg_color=CLR_ENTRY_BG, border_color=CLR_BORDER,
                    text_color=CLR_TEXT, font=self._ui_font(11),
                    width=110).pack(side="left", padx=(0, 6))
        ctk.CTkCheckBox(cfg_row, text="启用", variable=self.svc_enabled_var,
                       font=self._ui_font(11), text_color=CLR_TEXT,
                       width=60).pack(side="left", padx=(0, 14))
        ctk.CTkLabel(cfg_row, text="|", text_color=CLR_BORDER,
                    font=self._ui_font(13)).pack(side="left", padx=(0, 14))
        ctk.CTkLabel(cfg_row, text="上游URL", text_color=CLR_MUTED,
                    font=self._ui_font(10)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cfg_row, textvariable=self.svc_url_var, state="readonly",
                    fg_color=CLR_ENTRY_BG, border_color=CLR_BORDER,
                    text_color=CLR_TEXT, font=self._ui_font(11)).pack(
                    side="left", fill="x", expand=True, padx=(0, 14))
        ctk.CTkLabel(cfg_row, text="|", text_color=CLR_BORDER,
                    font=self._ui_font(13)).pack(side="left", padx=(0, 14))
        ctk.CTkCheckBox(cfg_row, text="密钥轮询", variable=self.key_enabled_var,
                       font=self._ui_font(11), text_color=CLR_TEXT,
                       width=80).pack(side="left", padx=(0, 10))
        ctk.CTkLabel(cfg_row, text="注入方式", text_color=CLR_MUTED,
                    font=self._ui_font(10)).pack(side="left", padx=(0, 4))
        type_combo = ctk.CTkComboBox(cfg_row, variable=self.key_type_var,
                       values=["header", "query"], state="readonly",
                       fg_color=CLR_ENTRY_BG, border_color=CLR_BORDER,
                       text_color=CLR_TEXT, font=self._ui_font(10), width=90)
        type_combo.pack(side="left", padx=(0, 10))
        self._add_tooltip(type_combo,
                         "header：密钥通过 HTTP 请求头传递（如 Authorization）\n"
                         "query ：密钥通过 URL 查询参数传递（如 ?apiKey=xxx）")
        ctk.CTkLabel(cfg_row, text="字段名", text_color=CLR_MUTED,
                    font=self._ui_font(10)).pack(side="left", padx=(0, 4))
        ctk.CTkEntry(cfg_row, textvariable=self.key_param_var,
                    fg_color=CLR_ENTRY_BG, border_color=CLR_BORDER,
                    text_color=CLR_TEXT, font=self._ui_font(10),
                    width=150).pack(side="left")

        # ── 密钥管理 + 失败特征（两列布局）
        lower = ctk.CTkFrame(editor, fg_color="transparent")
        lower.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        lower.grid_columnconfigure(0, weight=2)  # 密钥列表占 2/3
        lower.grid_columnconfigure(1, weight=1)  # 失败特征占 1/3
        lower.grid_rowconfigure(1, weight=1)

        # 左侧：密钥列表
        keys_lbl = ctk.CTkLabel(lower, text="🔑 密钥状态", font=self._ui_font(11, "bold"),
                               text_color=CLR_ACCENT)
        keys_lbl.grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.key_status_label = ctk.CTkLabel(
            lower,
            text="",
            font=self._ui_font(10),
            text_color=CLR_MUTED,
        )
        self.key_status_label.grid(row=0, column=0, sticky="e", pady=(0, 4))

        self.keys_tree = tk.Listbox(lower, height=6, font=self._tk_font(self.mono_font_family, 10),
                                   bg=CLR_ENTRY_BG, fg=CLR_TEXT, selectmode="extended",
                                   activestyle="none", relief="flat", bd=0,
                                   highlightthickness=0)
        self.keys_tree.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        keys_scroll = tk.Scrollbar(lower, command=self.keys_tree.yview,
                                  bg=CLR_BORDER, activebackground=CLR_HOVER, width=8)
        keys_scroll.grid(row=1, column=0, sticky="nse", padx=(0, 0))
        self.keys_tree.config(yscrollcommand=keys_scroll.set)

        # 右侧：失败特征
        patterns_lbl = ctk.CTkLabel(lower, text="⚠️ 失败特征", font=self._ui_font(11, "bold"),
                                   text_color=CLR_ACCENT)
        patterns_lbl.grid(row=0, column=1, sticky="w", pady=(0, 4))

        self.patterns_text = tk.Text(lower, height=6, width=20, font=self._tk_font(self.mono_font_family, 10),
                                    bg=CLR_ENTRY_BG, fg=CLR_TEXT, relief="flat", bd=0,
                                    highlightthickness=0, wrap="none")
        self.patterns_text.grid(row=1, column=1, sticky="nsew")
        patterns_scroll = tk.Scrollbar(lower, command=self.patterns_text.yview,
                                      bg=CLR_BORDER, activebackground=CLR_HOVER, width=8)
        patterns_scroll.grid(row=1, column=1, sticky="nse")
        self.patterns_text.config(yscrollcommand=patterns_scroll.set)
        for variable in (
            self.svc_enabled_var,
            self.key_enabled_var,
            self.key_type_var,
            self.key_param_var,
        ):
            self._watch_variable(variable)
        self.patterns_text.bind("<KeyRelease>", self._on_text_setting_changed, add="+")
        self.patterns_text.bind("<FocusOut>", self._on_text_setting_changed, add="+")

        # 操作按钮行（所有编辑均自动应用，不提供单独的保存按钮）
        btn_row = ctk.CTkFrame(editor, fg_color="transparent")
        btn_row.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
        for i in range(5):
            btn_row.grid_columnconfigure(i, weight=1)

        ctk.CTkButton(btn_row, text="📥 批量导入密钥", command=self._import_keys,
                     fg_color="#0d9488", hover_color="#0f766e",
                     font=self._ui_font(11, "bold")).grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
        ctk.CTkButton(btn_row, text="🧪 测试选中密钥", command=self._test_selected_keys,
                     fg_color=CLR_ACCENT2, hover_color="#6d28d9",
                     font=self._ui_font(11)).grid(row=0, column=1, sticky="ew", padx=4, pady=(0, 4))
        ctk.CTkButton(btn_row, text="🧪 测试全部密钥", command=self._test_all_keys,
                     fg_color="#0284c7", hover_color="#0369a1",
                     font=self._ui_font(11)).grid(row=0, column=2, sticky="ew", padx=4, pady=(0, 4))
        ctk.CTkButton(btn_row, text="📊 查询额度", command=self._query_selected_usage,
                     fg_color="#0f766e", hover_color="#115e59",
                     font=self._ui_font(11)).grid(row=0, column=3, sticky="ew", padx=4, pady=(0, 4))
        self.restart_button = ctk.CTkButton(
            btn_row,
            text="🔄 重启服务",
            command=self._restart_service,
            fg_color="#b45309",
            hover_color="#92400e",
            font=self._ui_font(11, "bold"),
        )
        self.restart_button.grid(row=0, column=4, sticky="ew", padx=(4, 0), pady=(0, 4))
        ctk.CTkButton(btn_row, text="♻️ 恢复选中", command=self._reset_selected_key_states,
                     fg_color="#166534", hover_color="#15803d",
                     font=self._ui_font(11)).grid(row=1, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkButton(btn_row, text="♻️ 恢复全部失效密钥", command=self._reset_all_disabled_keys,
                     fg_color="#166534", hover_color="#15803d",
                     font=self._ui_font(11, "bold")).grid(row=1, column=1, sticky="ew", padx=4)
        ctk.CTkButton(btn_row, text="查看原始错误", command=self._show_raw_errors,
                     fg_color="#475569", hover_color="#334155",
                     font=self._ui_font(11)).grid(row=1, column=2, sticky="ew", padx=4)
        ctk.CTkButton(btn_row, text="🗑️ 删除选中", command=self._delete_selected_keys,
                     fg_color="#7f1d1d", hover_color="#991b1b",
                     font=self._ui_font(11)).grid(row=1, column=3, sticky="ew", padx=4)
        ctk.CTkButton(btn_row, text="🗑️ 删除全部密钥", command=self._delete_all_keys,
                     fg_color="#991b1b", hover_color="#b91c1c",
                     font=self._ui_font(11, "bold")).grid(row=1, column=4, sticky="ew", padx=(4, 0))

    def _build_log_panel(self, parent) -> None:
        """底部：全宽 MCP 示例 + 测试日志（40/60 分栏）。"""
        bottom = ctk.CTkFrame(parent, fg_color="transparent")
        bottom.grid(row=1, column=0, columnspan=2, sticky="nsew")
        bottom.grid_columnconfigure(0, weight=2)
        bottom.grid_columnconfigure(1, weight=3)
        bottom.grid_rowconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1, minsize=180)

        # 左：MCP 配置示例
        mcp_frame = ctk.CTkFrame(bottom, fg_color=CLR_CARD, corner_radius=8)
        mcp_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        mcp_frame.grid_columnconfigure(0, weight=1)
        mcp_frame.grid_rowconfigure(1, weight=1)

        mcp_hdr = ctk.CTkFrame(mcp_frame, fg_color="transparent")
        mcp_hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        ctk.CTkLabel(mcp_hdr, text="📋 MCP 客户端配置示例", font=self._ui_font(12, "bold"),
                    text_color=CLR_ACCENT).pack(side="left")
        ctk.CTkButton(mcp_hdr, text="复制", command=self._copy_mcp_example,
                     width=60, height=24, fg_color=CLR_ACCENT, hover_color=CLR_HOVER,
                     font=self._ui_font(10)).pack(side="right")

        self.mcp_example_text = tk.Text(mcp_frame, height=7, font=self._tk_font(self.mono_font_family, 10),
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
        ctk.CTkLabel(log_hdr, text="📝 测试日志", font=self._ui_font(12, "bold"),
                    text_color=CLR_ACCENT).pack(side="left")
        ctk.CTkButton(log_hdr, text="清空", command=lambda: self._set_text(self.log_text, ""),
                     width=60, height=24, fg_color=CLR_CARD, hover_color=CLR_HOVER,
                     border_width=1, border_color=CLR_BORDER,
                     font=self._ui_font(10)).pack(side="right")

        self.log_text = tk.Text(log_frame, height=7, font=self._tk_font(self.mono_font_family, 10),
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
        self.routing_mode_var.set(ROUTING_MODE_LABELS.get(gw.routing_mode, "轮询"))

    def _load_service(self, svc: ServiceConfig) -> None:
        was_suspended = self._suspend_auto_apply
        self._suspend_auto_apply = True
        try:
            self.svc_name_var.set(svc.name)
            self.svc_enabled_var.set(svc.enabled)
            self.svc_url_var.set(svc.upstream_url)
            self.key_enabled_var.set(svc.key_auth.enabled)
            self.key_type_var.set(svc.key_auth.type)
            self.key_param_var.set(svc.key_auth.param)
            self._set_text(self.patterns_text, "\n".join(svc.failure_patterns))
            self._refresh_keys_list(svc)
            self._refresh_mcp_example(svc)
        finally:
            self._suspend_auto_apply = was_suspended

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
        persisted_map = self.state_store.build_key_map(svc.name, svc.keys)
        service_stats = self._stats_cache.get(svc.name, {})
        mode = ROUTING_MODE_LABELS.get(
            service_stats.get("routing_mode") or ROUTING_LABEL_MODES.get(self.routing_mode_var.get()),
            "轮询",
        )
        primary_tail = service_stats.get("primary_key_tail") or "暂无"
        self.key_status_label.configure(
            text=f"{mode} | 当前主 ...{primary_tail}" if mode == "主备" else f"{mode}模式"
        )

        for key in svc.keys:
            status_str = "正常"
            tag_color = CLR_SUCCESS
            if (svc.name, key) in self._restored_keys:
                if key in stats_map and not stats_map[key].get("is_disabled") and stats_map[key].get("cooldown_remaining", 0) <= 0:
                    self._restored_keys.discard((svc.name, key))
            else:
                persisted = persisted_map.get(key)
                if persisted and (
                    persisted.get("is_disabled") or persisted.get("retest_pending")
                ):
                    if persisted.get("retest_pending"):
                        remaining = persisted.get("disabled_remaining", 0)
                        if remaining > 0:
                            status_str = f"冷却中({int(remaining)}s)"
                        else:
                            status_str = "等待复测"
                        tag_color = CLR_WARN
                    elif persisted.get("disabled_until_epoch", 0) > 0:
                        status_str = "禁用至下月"
                        tag_color = CLR_ERROR
                    else:
                        status_str = "永久禁用"
                        tag_color = CLR_ERROR
                elif key in stats_map:
                    info = stats_map[key]
                    if info.get("is_disabled"):
                        if info.get("retest_pending"):
                            if info.get("cooldown_remaining", 0) > 0:
                                status_str = f"冷却中({int(info['cooldown_remaining'])}s)"
                            else:
                                status_str = "等待复测"
                            tag_color = CLR_WARN
                        else:
                            status_str = "永久禁用"
                            tag_color = CLR_ERROR
                    elif info.get("cooldown_remaining", 0) > 0:
                        status_str = f"冷却中({int(info['cooldown_remaining'])}s)"
                        tag_color = CLR_WARN
            record = persisted_map.get(key)
            monthly_success_count = (
                record.get("monthly_success_count", 0) if record else stats_map.get(key, {}).get("monthly_success_count", 0)
            )
            status_str = f"{status_str} | 本月成功{monthly_success_count}"
            usage = self._usage_cache.get((svc.name, key))
            if usage and usage.ok and usage.key_remaining is not None and usage.key_limit is not None:
                status_str = f"{status_str} | 余{usage.key_remaining}/{usage.key_limit}"
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
        if not self._suspend_auto_apply:
            self._cancel_auto_apply_timer()
            self._apply_and_queue_runtime(reason="切换服务前的修改")
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
            key_cooldown_seconds=int(
                self.cooldown_var.get().strip() or str(DEFAULT_KEY_COOLDOWN_SECONDS)
            ),
            session_ttl_seconds=int(self.ttl_var.get().strip() or "1800"),
            max_failover_retries=int(self.retry_var.get().strip() or "1"),
            upstream_timeout_seconds=int(self.timeout_var.get().strip() or "120"),
            routing_mode=ROUTING_LABEL_MODES.get(
                self.routing_mode_var.get().strip(),
                ROUTING_MODE_ROUND_ROBIN,
            ),
        )

    def _apply_current(self, silent: bool = False) -> bool:
        if self.current_index < 0:
            return True
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
    def _cancel_auto_apply_timer(self) -> None:
        """取消尚未执行的自动应用定时器。"""
        if self._auto_apply_after_id is None:
            return
        try:
            self.root.after_cancel(self._auto_apply_after_id)
        except tk.TclError:
            pass
        self._auto_apply_after_id = None

    def _auto_apply(self) -> None:
        """执行一次防抖后的自动配置应用。"""
        self._auto_apply_after_id = None
        if self._auto_apply_inflight:
            return
        self._apply_and_queue_runtime(reason="配置修改", generation=self._auto_apply_generation)

    def _prepare_config(self, *, show_errors: bool) -> bool:
        """同步并校验界面配置，校验通过后才允许写入文件。"""
        if not self._apply_current(silent=True):
            if show_errors:
                messagebox.showerror(APP_TITLE, "当前服务配置不完整，无法应用")
            return False
        try:
            gateway = self._gw_from_fields()
            gateway.validate()
            self.config.gateway = gateway
            self.config.validate()
        except Exception as exc:
            if show_errors:
                messagebox.showerror(APP_TITLE, f"配置不合法：\n{exc}")
            self._log(f"❌ 配置未应用：{exc}")
            return False
        return True

    def _write_config(self, *, show_errors: bool) -> bool:
        """将已经校验通过的配置写入 YAML 和本地密钥文件。"""
        try:
            dump_config(self.config, str(CONFIG_PATH))
        except Exception as exc:
            if show_errors:
                messagebox.showerror(APP_TITLE, f"配置写入失败：\n{exc}")
            self._log(f"❌ 配置写入失败：{exc}")
            return False
        return True

    def _apply_and_queue_runtime(
        self,
        *,
        reason: str,
        generation: int | None = None,
        show_errors: bool = False,
    ) -> bool:
        """写入配置并异步通知运行中的网关立即应用。"""
        if not self._prepare_config(show_errors=show_errors):
            return False
        if not self._write_config(show_errors=show_errors):
            return False

        self._log(f"✅ {reason}已写入配置，正在立即应用…")
        if generation is None:
            self._auto_apply_generation += 1
            generation = self._auto_apply_generation
        if self._auto_apply_inflight:
            return True
        self._auto_apply_inflight = True
        target_port = self.config.gateway.port
        target_key = self.config.gateway.access_keys[0]
        old_port = self._runtime_port
        old_key = self._runtime_access_key
        threading.Thread(
            target=self._apply_runtime_worker,
            args=(generation, old_port, old_key, target_port, target_key),
            daemon=True,
        ).start()
        return True

    def _apply_runtime_worker(
        self,
        generation: int,
        old_port: int,
        old_key: str,
        target_port: int,
        target_key: str,
    ) -> None:
        """在后台热加载配置，必要时重启网关进程。"""
        try:
            old_healthy = self._gateway_is_healthy(old_port)
            target_healthy = self._gateway_is_healthy(target_port)

            if old_healthy and target_port == old_port:
                ok, error = self._request_gateway_reload(old_port, old_key)
                if not ok:
                    raise RuntimeError(error or "网关拒绝热加载配置")
            elif old_healthy and target_port != old_port:
                self._stop_running_gateway(old_port, old_key)
                self._start_and_wait_gateway(target_port)
            elif target_healthy:
                ok, error = self._request_gateway_reload(target_port, old_key)
                if not ok:
                    raise RuntimeError(error or "目标端口上的网关拒绝热加载配置")
            else:
                self._stop_owned_server_process()
                self._start_and_wait_gateway(target_port)

            self.root.after(0, lambda: self._finish_auto_apply(
                generation, target_port, target_key, None,
            ))
        except Exception as exc:
            message = str(exc)
            self.root.after(0, lambda: self._finish_auto_apply(
                generation, target_port, target_key, message,
            ))

    def _finish_auto_apply(
        self,
        generation: int,
        target_port: int,
        target_key: str,
        error: str | None,
    ) -> None:
        """在主线程收尾自动应用，并处理应用期间产生的新修改。"""
        self._auto_apply_inflight = False
        if error is None:
            self._runtime_port = target_port
            self._runtime_access_key = target_key
            self._stats_cache_time = 0.0
            self._log("✅ 配置已立即生效")
        else:
            self._log(f"❌ 配置立即应用失败：{error}")

        if generation != self._auto_apply_generation:
            self._auto_apply_after_id = self.root.after(100, self._auto_apply)

    def _restart_service(self) -> None:
        """立即写入当前配置并重启本地网关。"""
        if self._auto_apply_inflight:
            self._log("⚠️ 当前已有配置正在应用，请稍候再重启")
            return
        self._cancel_auto_apply_timer()
        self._auto_apply_generation += 1
        if not self._prepare_config(show_errors=True):
            return
        if not self._write_config(show_errors=True):
            return

        self.restart_button.configure(state="disabled")
        self._log("🔄 正在重启本地网关，请稍候…")
        self._auto_apply_inflight = True
        target_port = self.config.gateway.port
        target_key = self.config.gateway.access_keys[0]
        generation = self._auto_apply_generation
        threading.Thread(
            target=self._restart_service_worker,
            args=(generation, self._runtime_port, self._runtime_access_key, target_port, target_key),
            daemon=True,
        ).start()

    def _restart_service_worker(
        self,
        generation: int,
        old_port: int,
        old_key: str,
        target_port: int,
        target_key: str,
    ) -> None:
        """在后台完成网关退出、启动和健康检查。"""
        try:
            if self._gateway_is_healthy(old_port):
                self._stop_running_gateway(old_port, old_key)
            else:
                self._stop_owned_server_process()
            self._start_and_wait_gateway(target_port)
            self.root.after(0, lambda: self._finish_manual_restart(
                generation, target_port, target_key, None,
            ))
        except Exception as exc:
            message = str(exc)
            self.root.after(0, lambda: self._finish_manual_restart(
                generation, target_port, target_key, message,
            ))

    def _finish_manual_restart(
        self,
        generation: int,
        target_port: int,
        target_key: str,
        error: str | None,
    ) -> None:
        """在主线程完成手动重启状态更新。"""
        self._auto_apply_inflight = False
        self.restart_button.configure(state="normal")
        if error is None:
            self._runtime_port = target_port
            self._runtime_access_key = target_key
            self._stats_cache_time = 0.0
            self._log("✅ 网关已重启，配置已生效")
        else:
            self._log(f"❌ 重启服务失败：{error}")
            messagebox.showerror(APP_TITLE, f"重启服务失败：\n{error}")
        if generation != self._auto_apply_generation:
            self._auto_apply_after_id = self.root.after(100, self._auto_apply)

    def _stop_running_gateway(self, port: int, access_key: str) -> None:
        """请求运行中的新版本网关优雅退出，并等待端口释放。"""
        requested, error = self._request_gateway_restart(port, access_key)
        if error:
            raise RuntimeError(error)
        if requested:
            if not self._wait_for_gateway_down(port, timeout=6.0):
                raise RuntimeError("旧网关未能在 6 秒内退出")
            return
        if self._gateway_is_healthy(port):
            raise RuntimeError("网关健康检查通过，但无法请求其优雅退出")
        self._stop_owned_server_process()

    def _start_and_wait_gateway(self, port: int) -> None:
        """启动网关并等待健康检查通过。"""
        process = self._start_gateway_process()
        self._server_process = process
        if self._wait_for_gateway_up(port, process, timeout=12.0):
            return
        exit_code = process.poll()
        if exit_code is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        self._server_process = None
        detail = f"进程已退出（退出码 {exit_code}）" if exit_code is not None else "健康检查超时"
        raise RuntimeError(f"新网关启动失败：{detail}")

    def _request_gateway_reload(
        self,
        port: int,
        access_key: str,
    ) -> tuple[bool, str | None]:
        """请求运行中的网关重新读取配置。"""
        import httpx

        if not access_key:
            return False, "未设置网关访问密钥，无法热加载配置"
        try:
            with httpx.Client(timeout=2.0) as client:
                response = client.post(
                    f"http://127.0.0.1:{port}/admin/reload",
                    headers={"Authorization": f"Bearer {access_key}"},
                )
        except httpx.RequestError as exc:
            return False, f"无法连接网关：{exc}"

        if response.status_code == 200:
            return True, None
        if response.status_code == 401:
            return False, "网关访问密钥不正确，无法热加载配置"
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        return False, f"网关拒绝热加载：HTTP {response.status_code} {detail}".strip()

    def _request_gateway_restart(
        self,
        port: int,
        access_key: str,
    ) -> tuple[bool, str | None]:
        """请求正在运行的网关退出；未运行时返回 False 且不报错。"""
        import httpx

        try:
            with httpx.Client(timeout=2.0) as client:
                response = client.post(
                    f"http://127.0.0.1:{port}/admin/restart",
                    headers={"Authorization": f"Bearer {access_key}"},
                )
        except httpx.RequestError:
            return False, None

        if response.status_code == 200:
            return True, None
        if response.status_code == 404:
            return False, "当前网关不支持优雅重启接口，请先启动最新网关程序"
        if response.status_code == 401:
            return False, "网关访问密钥不正确，无法重启服务"
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text
        return False, f"网关拒绝重启请求：HTTP {response.status_code} {detail}".strip()

    def _start_gateway_process(self) -> subprocess.Popen:
        """启动新的网关进程并返回进程句柄。"""
        if not START_SCRIPT.exists():
            raise FileNotFoundError(f"启动脚本不存在：{START_SCRIPT}")

        gateway_python = self._resolve_gateway_python()

        kwargs = {
            "cwd": str(PROJECT_ROOT),
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return subprocess.Popen(
            [str(gateway_python), str(START_SCRIPT), "--config", str(CONFIG_PATH)],
            **kwargs,
        )

    def _resolve_gateway_python(self) -> Path:
        """选择能够导入网关运行依赖的 Python 解释器。"""
        probe = "import httpx, starlette, uvicorn, yaml"
        failures: list[str] = []
        for candidate in _gateway_python_candidates():
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
            "没有可用的网关 Python 解释器：\n"
            f"{details}\n"
            "请先安装项目依赖：\n"
            f"{_gateway_python_candidates()[0]} -m pip install -r {requirements_path}"
        )

    def _stop_owned_server_process(self) -> None:
        """仅停止由当前 GUI 启动且仍存活的网关进程。"""
        process = self._server_process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
        self._server_process = None

    def _gateway_is_healthy(self, port: int) -> bool:
        """检查本机网关健康状态。"""
        import httpx

        try:
            with httpx.Client(timeout=0.8) as client:
                return client.get(f"http://127.0.0.1:{port}/healthz").status_code == 200
        except httpx.RequestError:
            return False

    def _wait_for_gateway_down(self, port: int, timeout: float) -> bool:
        """等待旧网关释放端口。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._gateway_is_healthy(port):
                return True
            time.sleep(0.15)
        return False

    def _wait_for_gateway_up(self, port: int, process: subprocess.Popen, timeout: float) -> bool:
        """等待新网关通过健康检查。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            if self._gateway_is_healthy(port):
                return True
            time.sleep(0.2)
        return False

    def _import_keys(self) -> None:
        """弹窗批量导入密钥。"""
        if self.current_index < 0:
            messagebox.showwarning(APP_TITLE, "请先选择一个服务")
            return
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("批量导入密钥")
        dialog.geometry("700x500")
        dialog.minsize(500, 300)
        self._set_modal(dialog)
        dialog.configure(fg_color=CLR_BG)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(1, weight=1)

        # 标题
        ctk.CTkLabel(dialog, text="请输入密钥列表（每行一个，自动去重）:",
                    font=self._ui_font(12, "bold"),
                    text_color=CLR_TEXT).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 8))

        # 文本框
        text_area = tk.Text(dialog, font=self._tk_font(self.mono_font_family, 11), bg=CLR_ENTRY_BG, fg=CLR_TEXT,
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
            applied = True
            if new_added:
                applied = self._apply_and_queue_runtime(reason="导入密钥")
            self._log(f"✅ 批量导入完成：新增 {new_added} 个密钥（已去重）")
            dialog.destroy()
            if applied:
                messagebox.showinfo(APP_TITLE, f"成功导入 {new_added} 个新密钥！")
            else:
                messagebox.showwarning(APP_TITLE, "密钥已加入当前编辑区，但配置尚未应用，请先修正无效设置")

        # 按钮
        btn_row = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 16))
        ctk.CTkButton(btn_row, text="取消", command=dialog.destroy, width=80,
                     fg_color=CLR_CARD, hover_color=CLR_HOVER, border_width=1, border_color=CLR_BORDER,
                     font=self._ui_font(11)).pack(side="right", padx=(4, 0))
        ctk.CTkButton(btn_row, text="导入", command=do_import, width=80,
                     fg_color=CLR_ACCENT, hover_color=CLR_HOVER,
                     font=self._ui_font(11)).pack(side="right", padx=(0, 4))

    def _show_raw_errors(self) -> None:
        """显示选中密钥最近一次失败的原始响应。"""
        if self.current_index < 0:
            return
        selections = self.keys_tree.curselection()
        if not selections:
            messagebox.showwarning(APP_TITLE, "请先选择要查看的密钥")
            return

        svc = self.config.services[self.current_index]
        records = self.state_store.build_key_map(svc.name, svc.keys)
        stats = self._stats_cache.get(svc.name, {}).get("keys", {}).get("details", [])
        stats_map = {item["key"]: item for item in stats if "key" in item}
        reports: list[str] = []
        for index in selections:
            if not (0 <= index < len(svc.keys)):
                continue
            key = svc.keys[index]
            record = records.get(key, {})
            info = stats_map.get(key, {})
            raw_error = record.get("raw_error") or info.get("raw_error") or "暂无原始错误记录"
            status_code = record.get("raw_status_code") or info.get("raw_status_code")
            status_text = f"HTTP {status_code}" if status_code else "无 HTTP 状态码"
            reports.append(f"密钥 ...{key[-6:]} | {status_text}\n{raw_error}")

        dialog = ctk.CTkToplevel(self.root)
        dialog.title("原始错误详情")
        dialog.geometry("900x560")
        dialog.minsize(600, 360)
        dialog.configure(fg_color=CLR_BG)
        self._set_modal(dialog)
        dialog.grid_columnconfigure(0, weight=1)
        dialog.grid_rowconfigure(0, weight=1)

        text_widget = tk.Text(
            dialog,
            font=self._tk_font(self.mono_font_family, 10),
            bg=CLR_ENTRY_BG,
            fg=CLR_TEXT,
            insertbackground=CLR_TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            wrap="word",
        )
        text_widget.grid(row=0, column=0, sticky="nsew", padx=16, pady=(16, 8))
        text_widget.insert("1.0", "\n\n".join(reports))
        text_widget.configure(state="disabled")
        ctk.CTkButton(
            dialog,
            text="确定",
            command=dialog.destroy,
            fg_color=CLR_ACCENT,
            hover_color=CLR_HOVER,
            font=self._ui_font(11),
        ).grid(row=1, column=0, sticky="e", padx=16, pady=(0, 16))

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
                self.state_store.reset_key(svc.name, svc.keys[idx])
                self._usage_cache.pop((svc.name, svc.keys[idx]), None)
                self._restored_keys.discard((svc.name, svc.keys[idx]))
                del svc.keys[idx]
        self._refresh_keys_list(svc)
        applied = self._apply_and_queue_runtime(reason="删除选中密钥")
        self._log(f"✅ 已删除 {len(selections)} 个密钥")
        if applied:
            messagebox.showinfo(APP_TITLE, f"已删除 {len(selections)} 个密钥")
        else:
            messagebox.showwarning(APP_TITLE, "密钥已从当前编辑区删除，但配置尚未应用，请先修正无效设置")

    def _delete_all_keys(self) -> None:
        """删除当前服务的全部上游密钥，并立即应用配置。"""
        if self.current_index < 0:
            return
        svc = self.config.services[self.current_index]
        if not svc.keys:
            messagebox.showinfo(APP_TITLE, "当前服务没有可删除的密钥")
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"确定删除当前服务的全部 {len(svc.keys)} 把密钥吗？\n此操作会立即生效。",
        ):
            return

        for key in svc.keys:
            self.state_store.reset_key(svc.name, key)
            self._usage_cache.pop((svc.name, key), None)
            self._restored_keys.discard((svc.name, key))
        deleted = len(svc.keys)
        svc.keys = []
        self._stats_cache_time = 0.0
        self._refresh_keys_list(svc)
        applied = self._apply_and_queue_runtime(reason="删除全部密钥")
        self._log(f"✅ 已删除当前服务全部 {deleted} 个密钥")
        if applied:
            messagebox.showinfo(APP_TITLE, f"已删除全部 {deleted} 个密钥")
        else:
            messagebox.showwarning(APP_TITLE, "密钥已从当前编辑区删除，但配置尚未应用，请先修正无效设置")

    def _reset_selected_key_states(self) -> None:
        """清除选中密钥的本地废弃状态。"""
        if self.current_index < 0:
            return
        selections = self.keys_tree.curselection()
        if not selections:
            messagebox.showwarning(APP_TITLE, "请先选择要恢复状态的密钥")
            return
        svc = self.config.services[self.current_index]
        restored = 0
        for idx in selections:
            if 0 <= idx < len(svc.keys):
                self._restore_key_available(svc, svc.keys[idx])
                restored += 1
        self._refresh_keys_list(svc)
        self._log(f"✅ 已恢复 {restored} 个密钥为可用状态")
        messagebox.showinfo(APP_TITLE, f"已恢复 {restored} 个密钥为可用状态")

    def _reset_all_disabled_keys(self) -> None:
        """恢复当前服务所有失效（禁用/冷却中）密钥为可用状态。"""
        if self.current_index < 0:
            return
        svc = self.config.services[self.current_index]
        stats = self._stats_cache.get(svc.name, {}).get("keys", {}).get("details", [])
        stats_map = {item["key"]: item for item in stats if "key" in item}
        persisted_map = self.state_store.build_key_map(svc.name, svc.keys)

        disabled_keys: list[str] = []
        for key in svc.keys:
            is_disabled = False
            persisted = persisted_map.get(key)
            if persisted and (
                persisted.get("is_disabled")
                or persisted.get("retest_pending")
                or persisted.get("retest_due")
            ):
                is_disabled = True
            elif key in stats_map:
                info = stats_map[key]
                if info.get("is_disabled") or info.get("cooldown_remaining", 0) > 0:
                    is_disabled = True
            if is_disabled:
                disabled_keys.append(key)

        if not disabled_keys:
            messagebox.showinfo(APP_TITLE, "当前服务没有失效密钥")
            return

        if not messagebox.askyesno(
            APP_TITLE,
            f"确定恢复全部 {len(disabled_keys)} 把失效密钥为可用状态吗？\n"
            "（这会清除所有密钥的冷却和禁用状态）",
        ):
            return

        for key in disabled_keys:
            self._restore_key_available(svc, key)
        self._refresh_keys_list(svc)
        self._log(f"✅ 已恢复全部 {len(disabled_keys)} 把失效密钥为可用状态")
        messagebox.showinfo(APP_TITLE, f"已恢复 {len(disabled_keys)} 把失效密钥为可用状态")

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

    def _query_selected_usage(self) -> None:
        """查询选中密钥的额度。"""
        if not self._apply_current(silent=True):
            return
        if self.current_index < 0:
            messagebox.showwarning(APP_TITLE, "没有可查询的服务")
            return
        svc = self.config.services[self.current_index]
        provider = get_provider(svc.name)
        if provider is None or not provider.supports_usage:
            messagebox.showwarning(APP_TITLE, f"[{svc.name}] 当前不支持精确额度查询")
            return
        selections = self.keys_tree.curselection()
        if not selections:
            messagebox.showwarning(APP_TITLE, "请先选择要查询额度的密钥")
            return
        selected_keys = [svc.keys[idx] for idx in selections if 0 <= idx < len(svc.keys)]
        if not selected_keys:
            messagebox.showwarning(APP_TITLE, "无有效的密钥可查询额度")
            return
        self._run_usage_query(svc, provider, selected_keys)

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
        self._set_modal(progress_dialog)

        ctk.CTkLabel(progress_dialog, text=f"正在测试 {len(keys)} 把密钥...",
                    font=self._ui_font(12, "bold"),
                    text_color=CLR_TEXT).pack(expand=True, padx=20, pady=20)

        def worker() -> None:
            try:
                concurrency = min(len(keys), 5)
                results = asyncio.run(validate_keys(svc, keys, deep=True, concurrency=concurrency, timeout=45.0))
                self.root.after(0, lambda: [progress_dialog.destroy(), self._show_results(svc, results)])
            except Exception as exc:
                self.root.after(0, lambda: [progress_dialog.destroy(), self._log(f"❌ 测试异常：{exc}")])

        threading.Thread(target=worker, daemon=True).start()

    def _run_usage_query(self, svc: ServiceConfig, provider, keys: list[str]) -> None:
        """后台查询密钥额度。"""
        self._log(f"🔄 开始查询 [{svc.name}] 的 {len(keys)} 把密钥额度…")

        progress_dialog = ctk.CTkToplevel(self.root)
        progress_dialog.title("额度查询中")
        progress_dialog.geometry("420x120")
        progress_dialog.resizable(False, False)
        progress_dialog.configure(fg_color=CLR_BG)
        self._set_modal(progress_dialog)

        ctk.CTkLabel(progress_dialog, text=f"正在查询 {len(keys)} 把密钥额度...",
                    font=self._ui_font(12, "bold"),
                    text_color=CLR_TEXT).pack(expand=True, padx=20, pady=20)

        def worker() -> None:
            try:
                results = asyncio.run(self._fetch_usage_snapshots(provider, keys))
                self.root.after(
                    0,
                    lambda: [progress_dialog.destroy(), self._show_usage_results(svc, results)],
                )
            except Exception as exc:
                self.root.after(0, lambda: [progress_dialog.destroy(), self._log(f"❌ 额度查询异常：{exc}")])

        threading.Thread(target=worker, daemon=True).start()

    def _show_results(self, svc: ServiceConfig, results) -> None:
        """展示手动测试结果，并同步更新密钥状态。"""
        name = svc.name
        valid_list, failed_list = [], []
        disabled_count = 0
        for r in results:
            icon = {"valid": "✅", "quota_exhausted": "⚠️", "invalid": "❌"}.get(r.status, "💥")
            line = f"  {icon} {r.key} | {r.latency_ms}ms | {r.detail}"
            if r.status == "valid":
                self._restore_key_available(svc, r.key)
            else:
                self._disable_key_after_manual_test(svc, r)
                disabled_count += 1
            (valid_list if r.status == "valid" else failed_list).append(line)

        ok, failed = len(valid_list), len(failed_list)
        self._log(f"[{name}] 测试完成：✅ {ok} 把有效，❌ {failed} 把失败")
        for line in valid_list + failed_list:
            self._log(line)
        if disabled_count:
            self._log(f"⚠️ 已自动禁用 {disabled_count} 把测试失败的密钥，可手动恢复")
        self._log("─" * 60)
        if 0 <= self.current_index < len(self.config.services) and self.config.services[self.current_index] is svc:
            self._refresh_keys_list(svc)

        # 结果弹窗
        result_dialog = ctk.CTkToplevel(self.root)
        result_dialog.title("测试结果")
        result_dialog.geometry("800x480")
        result_dialog.configure(fg_color=CLR_BG)
        self._set_modal(result_dialog)
        result_dialog.grid_columnconfigure(0, weight=1)
        result_dialog.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(result_dialog, text=f"[{name}] 测试完成：✅ {ok} 把有效  ❌ {failed} 把失败",
                    font=self._ui_font(13, "bold"),
                    text_color=CLR_TEXT).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 8))

        report = "\n".join([f"✅ 有效密钥 ({ok} 把):"] + valid_list +
                           ["", f"❌ 失败密钥 ({failed} 把):"] + failed_list)
        text_widget = tk.Text(result_dialog, font=self._tk_font(self.mono_font_family, 10), bg=CLR_ENTRY_BG, fg=CLR_TEXT,
                             relief="flat", bd=0, highlightthickness=0, wrap="none")
        text_widget.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)
        text_widget.insert("1.0", report)
        text_widget.configure(state="disabled")

        ctk.CTkButton(result_dialog, text="确定", command=result_dialog.destroy,
                     fg_color=CLR_ACCENT, hover_color=CLR_HOVER,
                     font=self._ui_font(11)).grid(row=2, column=0, sticky="e", padx=16, pady=16)

    async def _fetch_usage_snapshots(self, provider, keys: list[str]) -> list[tuple[str, UsageSnapshot]]:
        """并发查询额度快照。"""
        sem = asyncio.Semaphore(min(2, max(1, len(keys))))

        async def _one(key: str) -> tuple[str, UsageSnapshot]:
            async with sem:
                snapshot = await provider.fetch_usage(key, timeout=20.0)
                if snapshot is None:
                    snapshot = UsageSnapshot(status="error", detail="当前平台未返回额度信息")
                return key, snapshot

        return await asyncio.gather(*[_one(key) for key in keys])

    def _show_usage_results(self, svc: ServiceConfig, results: list[tuple[str, UsageSnapshot]]) -> None:
        """展示额度查询结果。"""
        ok_lines: list[str] = []
        fail_lines: list[str] = []

        for key, snapshot in results:
            self._usage_cache[(svc.name, key)] = snapshot
            line = self._format_usage_line(key, snapshot)
            if snapshot.ok:
                ok_lines.append(line)
            else:
                fail_lines.append(line)
            self._log(line)

        self._refresh_keys_list(svc)
        self._log("─" * 60)

        ok = len(ok_lines)
        failed = len(fail_lines)
        title = f"[{svc.name}] 额度查询完成：✅ {ok} 把成功  ❌ {failed} 把失败"
        self._log(title)

        result_dialog = ctk.CTkToplevel(self.root)
        result_dialog.title("额度查询结果")
        result_dialog.geometry("880x480")
        result_dialog.configure(fg_color=CLR_BG)
        self._set_modal(result_dialog)
        result_dialog.grid_columnconfigure(0, weight=1)
        result_dialog.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(result_dialog, text=title,
                    font=self._ui_font(13, "bold"),
                    text_color=CLR_TEXT).grid(row=0, column=0, sticky="w", padx=16, pady=(16, 8))

        report = "\n".join(
            [f"✅ 查询成功 ({ok} 把):"] + ok_lines +
            ["", f"❌ 查询失败 ({failed} 把):"] + fail_lines
        )
        text_widget = tk.Text(result_dialog, font=self._tk_font(self.mono_font_family, 10), bg=CLR_ENTRY_BG, fg=CLR_TEXT,
                             relief="flat", bd=0, highlightthickness=0, wrap="none")
        text_widget.grid(row=1, column=0, sticky="nsew", padx=16, pady=8)
        text_widget.insert("1.0", report)
        text_widget.configure(state="disabled")

        ctk.CTkButton(result_dialog, text="确定", command=result_dialog.destroy,
                     fg_color=CLR_ACCENT, hover_color=CLR_HOVER,
                     font=self._ui_font(11)).grid(row=2, column=0, sticky="e", padx=16, pady=16)

    def _format_usage_line(self, key: str, snapshot: UsageSnapshot) -> str:
        """格式化额度查询输出。"""
        if not snapshot.ok:
            return f"  ❌ {key} | {snapshot.detail}"

        parts = [f"  📊 {key}"]
        if snapshot.key_remaining is not None and snapshot.key_limit is not None and snapshot.key_usage is not None:
            parts.append(f"密钥剩余 {snapshot.key_remaining}/{snapshot.key_limit}（已用 {snapshot.key_usage}）")
        if snapshot.account_plan:
            plan_part = f"套餐 {snapshot.account_plan}"
            if snapshot.account_remaining is not None and snapshot.account_limit is not None and snapshot.account_usage is not None:
                plan_part += f" 剩余 {snapshot.account_remaining}/{snapshot.account_limit}（已用 {snapshot.account_usage}）"
            parts.append(plan_part)
        if snapshot.paygo_usage is not None:
            if snapshot.paygo_limit is not None:
                parts.append(f"按量额度剩余 {snapshot.paygo_limit - snapshot.paygo_usage}/{snapshot.paygo_limit}")
            else:
                parts.append(f"按量已用 {snapshot.paygo_usage}")
        return " | ".join(parts)

    def _async_fetch_stats(self, svc: ServiceConfig) -> None:
        """后台线程拉取网关状态。"""
        try:
            import httpx
            with httpx.Client(timeout=1.0) as client:
                r = client.get(
                    f"http://127.0.0.1:{self._runtime_port}/stats",
                    headers={"Authorization": f"Bearer {self._runtime_access_key}"}
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
                    "headers": {"Authorization": gw_key}
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

    def _set_text(self, widget: tk.Text, value: str) -> None:
        widget.delete("1.0", "end")
        widget.insert("1.0", value)

    def _log(self, msg: str) -> None:
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _restore_key_available(self, svc: ServiceConfig, key: str) -> None:
        """将密钥恢复为可用：清本地状态，并尝试通知运行中网关。"""
        self.state_store.reset_key(svc.name, key)
        self._usage_cache.pop((svc.name, key), None)
        self._stats_cache_time = 0.0
        self._restored_keys.add((svc.name, key))

        try:
            import httpx
            with httpx.Client(timeout=1.5) as client:
                resp = client.post(
                    f"http://127.0.0.1:{self._runtime_port}/admin/reset-key",
                    headers={"Authorization": f"Bearer {self._runtime_access_key}"},
                    json={"service": svc.name, "key": key},
                )
                if resp.status_code == 200:
                    return
        except Exception:
            pass

    def _disable_key_after_manual_test(self, svc: ServiceConfig, result) -> None:
        """将手动测试失败的密钥禁用到下个自然月。"""
        self.state_store.disable_key_until_next_month(
            svc.name,
            result.key,
            reason=f"manual_test_failed:{result.status}",
            raw_error=getattr(result, "raw_error", "") or result.detail,
            raw_status_code=getattr(result, "raw_status_code", None),
        )
        self._restored_keys.discard((svc.name, result.key))
        self._usage_cache.pop((svc.name, result.key), None)
        self._stats_cache_time = 0.0

    def _add_tooltip(self, widget, text: str) -> None:
        """为组件添加 tooltip（悬停提示）。"""
        def on_enter(event):
            tooltip = tk.Toplevel(widget)
            tooltip.wm_overrideredirect(True)
            tooltip.wm_geometry(f"+{event.x_root + 10}+{event.y_root + 10}")
            label = tk.Label(tooltip, text=text, background=CLR_PANEL, foreground=CLR_TEXT,
                           font=self._ui_font(9), padx=8, pady=4, relief="solid", bd=1)
            label.pack()
            widget.tooltip = tooltip

        def on_leave(event):
            if hasattr(widget, 'tooltip'):
                widget.tooltip.destroy()
                del widget.tooltip

        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)


# ── 入口 ──────────────────────────────────────────────────────────────────────
def _configure_linux_display(root: tk.Misc) -> None:
    """修正 Linux Wayland/XWayland 下 Tk 默认缩放过小导致的字体发糊。"""
    if not sys.platform.startswith("linux"):
        return
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session_type != "wayland" and not os.environ.get("WAYLAND_DISPLAY"):
        return
    try:
        current = float(root.tk.call("tk", "scaling"))
        # Tk 8.6 在 XWayland 下常把 1.0 当作默认值，至少提升到 1.25。
        root.tk.call("tk", "scaling", max(current, 1.25))
    except (tk.TclError, TypeError, ValueError):
        pass


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
    _configure_linux_display(root)
    root.configure(fg_color=CLR_BG)
    GatewayEditor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
