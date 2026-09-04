# -*- coding: utf-8 -*-
"""MCP 聚合网关配置管理器 - Flet 0.28.3 现代重构版。

功能特性：
1. 架构彻底解耦：界面由 GatewayAppUI 负责，纯业务逻辑封装在 src.gateway_manager.GatewayManager。
2. 现代响应式工作台布局：
   - 顶部：网关全局参数、实时运行健康状态灯、快捷重启/应用
   - 左侧：服务列表导航卡片，附带启用状态、密钥总数、失效角标
   - 右侧：多标签页设计（密钥池管理、上游服务配置、多客户端 MCP JSON 生成、实时控制台日志）
3. 丰富交互体验：
   - 密钥列表支持搜索筛选、全选/反选、卡片化状态标记（正常/冷却/禁用/复测）、单键快捷测试/恢复/复制/删除
   - 批量导入、并发测试结果、额度快照、原始错误报文统一采用现代模态弹窗（AlertDialog）
   - 统一使用 page.open() / page.close()，完全兼容 Flet 0.28.x API
4. 保持双击启动：直接双击 gui.pyw 即可静默启动无黑框窗口。
"""
from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable

import flet as ft

from src.config import (
    ROUTING_MODE_PRIMARY_BACKUP,
    ROUTING_MODE_ROUND_ROBIN,
    ServiceConfig,
)
from src.gateway_manager import (
    APP_TITLE,
    ROUTING_LABEL_MODES,
    ROUTING_MODE_LABELS,
    GatewayManager,
    KeyDisplayItem,
    split_lines,
)
from src.providers import UsageSnapshot
from src.validator import ValidationResult

# ── 现代暗色调色板 ───────────────────────────────────────────────────────────
CLR_BG = "#0f172a"          # Slate 900
CLR_SIDEBAR = "#1e293b"     # Slate 800
CLR_CARD = "#1e293b"        # Slate 800
CLR_CARD_ACTIVE = "#283548" # Slate 750
CLR_BORDER = "#334155"      # Slate 700
CLR_ACCENT = "#3b82f6"      # Blue 500
CLR_SUCCESS = "#10b981"     # Emerald 500
CLR_WARN = "#f59e0b"        # Amber 500
CLR_ERROR = "#ef4444"       # Red 500
CLR_TEXT = "#f8fafc"        # Slate 50
CLR_MUTED = "#94a3b8"       # Slate 400

# ── 原生窗口尺寸 ──────────────────────────────────────────────────────────────
WINDOW_WIDTH = 1200
WINDOW_HEIGHT = 800
WINDOW_MIN_WIDTH = 880
WINDOW_MIN_HEIGHT = 580

# ── 跨平台字体兼容处理 ─────────────────────────────────────────────────────────
def resolve_ui_font_family() -> str:
    """解析系统首选 UI 界面字体：
    - Windows：使用系统标配高质量中文字体“微软雅黑” (Microsoft YaHei)。
    - Ubuntu Desktop：优先使用官方推荐预装的“Noto Sans CJK SC”及“Ubuntu”字体，并提供文泉驿正黑等安全回退。
    """
    if sys.platform == "win32":
        return "Microsoft YaHei, Segoe UI, sans-serif"
    elif sys.platform.startswith("linux"):
        return "Noto Sans CJK SC, Ubuntu, Source Han Sans SC, WenQuanYi Zen Hei, sans-serif"
    elif sys.platform == "darwin":
        return "PingFang SC, -apple-system, sans-serif"
    return "sans-serif"


def resolve_mono_font_family() -> str:
    """解析系统首选等宽代码字体：
    - Windows：使用 Consolas / Cascadia Mono。
    - Ubuntu Desktop：使用 Ubuntu 标配的 Ubuntu Mono / Noto Sans Mono CJK SC。
    """
    if sys.platform == "win32":
        return "Consolas, Cascadia Mono, monospace"
    elif sys.platform.startswith("linux"):
        return "Ubuntu Mono, Noto Sans Mono CJK SC, DejaVu Sans Mono, monospace"
    return "monospace"


# ── Linux 原生桌面兼容性与自愈垫片 ──────────────────────────────────────────────
_LINUX_COMPAT_INITIALIZED = False

_GTK_WINDOW_GUARD_SOURCE = r"""
#define _GNU_SOURCE
#include <dlfcn.h>

typedef struct _GtkWindow GtkWindow;
typedef int (*gtk_window_is_maximized_fn)(GtkWindow *window);
typedef void (*gtk_window_resize_fn)(GtkWindow *window, int width, int height);

int gtk_window_is_maximized(GtkWindow *window) {
    if (window == 0) {
        return 0;
    }

    gtk_window_is_maximized_fn real_fn =
        (gtk_window_is_maximized_fn)dlsym(RTLD_NEXT, "gtk_window_is_maximized");
    return real_fn == 0 ? 0 : real_fn(window);
}

void gtk_window_resize(GtkWindow *window, int width, int height) {
    // Flet 0.28.3 会把最大化后的实际尺寸再次传给 gtk_window_resize，GTK 因此会还原窗口。
    if (window != 0 && gtk_window_is_maximized(window)) {
        return;
    }

    gtk_window_resize_fn real_fn =
        (gtk_window_resize_fn)dlsym(RTLD_NEXT, "gtk_window_resize");
    if (real_fn != 0) {
        real_fn(window, width, height);
    }
}
"""
_GTK_WINDOW_GUARD_DIGEST = hashlib.sha256(
    _GTK_WINDOW_GUARD_SOURCE.encode("utf-8")
).hexdigest()


def _ensure_gtk_window_guard(compat_dir: str) -> None:
    """生成并注入 GTK 空指针保护垫片，避免 Flet 关闭窗口时访问悬垂对象。"""
    shim_path = os.path.join(compat_dir, "libmcp_gtk_guard.so")
    stamp_path = os.path.join(compat_dir, "libmcp_gtk_guard.sha256")

    try:
        compiler = next(
            (
                candidate
                for candidate in ("cc", "gcc", "clang")
                if shutil.which(candidate)
            ),
            None,
        )
        current_digest = ""
        try:
            with open(stamp_path, encoding="ascii") as stamp_file:
                current_digest = stamp_file.read().strip()
        except OSError:
            pass

        needs_build = not os.path.isfile(shim_path) or current_digest != _GTK_WINDOW_GUARD_DIGEST
        if needs_build and compiler:
            temp_path: str | None = None
            try:
                fd, temp_path = tempfile.mkstemp(
                    prefix=".libmcp_gtk_guard.",
                    suffix=".so",
                    dir=compat_dir,
                )
                os.close(fd)
                result = subprocess.run(
                    [
                        compiler,
                        "-shared",
                        "-fPIC",
                        "-O2",
                        "-x",
                        "c",
                        "-",
                        "-o",
                        temp_path,
                        "-ldl",
                    ],
                    input=_GTK_WINDOW_GUARD_SOURCE,
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8.0,
                    check=False,
                )
                if result.returncode != 0:
                    return
                os.replace(temp_path, shim_path)
                try:
                    with open(stamp_path, "w", encoding="ascii") as stamp_file:
                        stamp_file.write(_GTK_WINDOW_GUARD_DIGEST)
                except OSError:
                    pass
            finally:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

        if not os.path.isfile(shim_path):
            return

        preload_entries = os.environ.get("LD_PRELOAD", "").split()
        if shim_path not in preload_entries:
            os.environ["LD_PRELOAD"] = " ".join([shim_path, *preload_entries])
    except Exception:
        # 系统没有编译器或用户目录不可写时，保留 Flet 的默认运行方式。
        pass


def _find_system_libmpv() -> str | None:
    """探测 Linux 系统中实际存在的 libmpv 共享库路径（如 libmpv.so.2 等）。"""
    candidate_paths = [
        "/usr/lib/x86_64-linux-gnu/libmpv.so.2",
        "/usr/lib/aarch64-linux-gnu/libmpv.so.2",
        "/usr/lib64/libmpv.so.2",
        "/usr/lib/libmpv.so.2",
        "/usr/local/lib/libmpv.so.2",
        "/usr/local/lib64/libmpv.so.2",
        "/usr/lib/x86_64-linux-gnu/libmpv.so",
        "/usr/lib/aarch64-linux-gnu/libmpv.so",
        "/usr/lib64/libmpv.so",
        "/usr/lib/libmpv.so",
    ]
    for p in candidate_paths:
        if os.path.isfile(p):
            return p

    # 兜底：使用 ldconfig -p 扫描动态链接器缓存
    try:
        out = subprocess.check_output(
            ["/sbin/ldconfig", "-p"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
        for line in out.splitlines():
            if "libmpv.so" in line and "=>" in line:
                p = line.split("=>")[-1].strip()
                if os.path.isfile(p):
                    return p
    except Exception:
        pass
    return None


def _ensure_linux_native_compat() -> None:
    """面向现代 Linux（如 Ubuntu 26.04+）的 Flet 原生桌面运行时自愈垫片。

    背景与机理：
    1. Flet 0.28.x 原生桌面客户端写死链接旧版 libmpv.so.1。
    2. 而在 Ubuntu 26.04 等现代发行版中，系统仅标配 libmpv.so.2，直接启动会触发缺失动态库错误。
    3. 本垫片在用户空间建立安全的隔离符号链接，并注入 LD_LIBRARY_PATH，
       使 Flet 的原生子进程能够无感加载，无需修改系统文件，无需 root / patchelf。
    4. Flet 0.28.3 的 Linux 窗口插件在 KDE Wayland 下关闭握手不稳定；有 Xwayland 时使用 X11 兼容后端。
    """
    global _LINUX_COMPAT_INITIALIZED
    if _LINUX_COMPAT_INITIALIZED or not sys.platform.startswith("linux"):
        return

    _LINUX_COMPAT_INITIALIZED = True

    # Flet 0.28.3 的 GTK 窗口插件在 KDE Wayland 下可能无法正常完成关闭。
    # 有 Xwayland 时使用 X11 兼容层；纯 Wayland 环境仍保留系统默认后端。
    is_wayland_session = bool(os.environ.get("WAYLAND_DISPLAY")) or (
        os.environ.get("XDG_SESSION_TYPE") == "wayland"
    )
    if is_wayland_session and os.environ.get("DISPLAY"):
        os.environ["GDK_BACKEND"] = "x11"

    # 1. 准备用户级兼容目录，所有垫片均不修改系统目录。
    compat_dir = os.path.join(os.path.expanduser("~"), ".flet", "compat_libs")
    try:
        os.makedirs(compat_dir, exist_ok=True)
    except OSError:
        return

    # 2. 检查系统是否已直接拥有 libmpv.so.1
    has_mpv1 = False
    for p in [
        "/usr/lib/x86_64-linux-gnu/libmpv.so.1",
        "/usr/lib/aarch64-linux-gnu/libmpv.so.1",
        "/usr/lib64/libmpv.so.1",
        "/usr/lib/libmpv.so.1",
    ]:
        if os.path.isfile(p):
            has_mpv1 = True
            break

    # 3. 寻找系统中的新版 libmpv (如 libmpv.so.2)，仅在缺少旧名称时建立链接。
    if not has_mpv1:
        real_mpv = _find_system_libmpv()
        if real_mpv:
            try:
                link_target = os.path.join(compat_dir, "libmpv.so.1")
                if not os.path.lexists(link_target):
                    os.symlink(real_mpv, link_target)

                # 双保险：若 Flet 私有目录已存在，顺便在其中补齐软链接。
                flet_bin_root = os.path.join(os.path.expanduser("~"), ".flet", "bin")
                if os.path.isdir(flet_bin_root):
                    for entry in os.listdir(flet_bin_root):
                        lib_dir = os.path.join(flet_bin_root, entry, "flet", "lib")
                        if os.path.isdir(lib_dir):
                            flet_link = os.path.join(lib_dir, "libmpv.so.1")
                            if not os.path.lexists(flet_link):
                                try:
                                    os.symlink(real_mpv, flet_link)
                                except OSError:
                                    pass

                # 注入到 LD_LIBRARY_PATH，使 Flet 原生子进程能够解析兼容名称。
                curr_ld = os.environ.get("LD_LIBRARY_PATH", "")
                if compat_dir not in curr_ld.split(os.pathsep):
                    os.environ["LD_LIBRARY_PATH"] = (
                        f"{compat_dir}{os.pathsep}{curr_ld}" if curr_ld else compat_dir
                    )
            except OSError:
                pass

    # 4. 保护 Flet 0.28.3 window_manager 插件的 GTK 空指针调用。
    _ensure_gtk_window_guard(compat_dir)


# 模块级首次加载即预初始化垫片环境
_ensure_linux_native_compat()


# ── Flet 0.28.3 启动窗口兼容处理 ──────────────────────────────────────────────
class _X11WindowAttributes(ctypes.Structure):
    """X11 顶层窗口属性的最小 ctypes 映射。"""

    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("class_", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class _X11ClassHint(ctypes.Structure):
    """X11 窗口类别字符串的 ctypes 映射。"""

    _fields_ = [("res_name", ctypes.c_void_p), ("res_class", ctypes.c_void_p)]


def _x11_string(pointer: int | None) -> str:
    """读取 X11 分配的 C 字符串，不在此函数中释放内存。"""
    if not pointer:
        return ""
    value = ctypes.cast(pointer, ctypes.c_char_p).value
    return value.decode(errors="replace") if value else ""


def _x11_startup_cloak(native_pid: int) -> None:
    """暂时隐藏 Flet 默认启动页，等目标尺寸应用后再显示。"""
    if not sys.platform.startswith("linux") or not os.environ.get("DISPLAY"):
        return
    if os.environ.get("GDK_BACKEND", "").lower() == "wayland":
        return

    library_name = ctypes.util.find_library("X11")
    if not library_name:
        return

    try:
        x11 = ctypes.CDLL(library_name)
        display_type = ctypes.c_void_p
        window_type = ctypes.c_ulong
        atom_type = ctypes.c_ulong

        def bind(name: str, arguments: list[Any], result: Any) -> Any:
            function = getattr(x11, name)
            function.argtypes = arguments
            function.restype = result
            return function

        x_open_display = bind("XOpenDisplay", [ctypes.c_char_p], display_type)
        x_close_display = bind("XCloseDisplay", [display_type], ctypes.c_int)
        x_default_root_window = bind(
            "XDefaultRootWindow", [display_type], window_type
        )
        x_query_tree = bind(
            "XQueryTree",
            [
                display_type,
                window_type,
                ctypes.POINTER(window_type),
                ctypes.POINTER(window_type),
                ctypes.POINTER(ctypes.POINTER(window_type)),
                ctypes.POINTER(ctypes.c_uint),
            ],
            ctypes.c_int,
        )
        x_get_class_hint = bind(
            "XGetClassHint",
            [display_type, window_type, ctypes.POINTER(_X11ClassHint)],
            ctypes.c_int,
        )
        x_get_window_attributes = bind(
            "XGetWindowAttributes",
            [display_type, window_type, ctypes.POINTER(_X11WindowAttributes)],
            ctypes.c_int,
        )
        x_get_window_property = bind(
            "XGetWindowProperty",
            [
                display_type,
                window_type,
                atom_type,
                ctypes.c_long,
                ctypes.c_long,
                ctypes.c_int,
                atom_type,
                ctypes.POINTER(atom_type),
                ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_void_p),
            ],
            ctypes.c_int,
        )
        x_intern_atom = bind(
            "XInternAtom", [display_type, ctypes.c_char_p, ctypes.c_int], atom_type
        )
        x_change_property = bind(
            "XChangeProperty",
            [
                display_type,
                window_type,
                atom_type,
                atom_type,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_int,
            ],
            ctypes.c_int,
        )
        x_flush = bind("XFlush", [display_type], ctypes.c_int)
        x_free = bind("XFree", [ctypes.c_void_p], ctypes.c_int)

        display = x_open_display(os.environ["DISPLAY"].encode())
        if not display:
            return

        pid_atom = x_intern_atom(display, b"_NET_WM_PID", 0)
        opacity_atom = x_intern_atom(display, b"_NET_WM_WINDOW_OPACITY", 0)
        cardinal_atom = x_intern_atom(display, b"CARDINAL", 0)
        root_window = x_default_root_window(display)

        def get_window_pid(window: int) -> int | None:
            actual_type = atom_type()
            actual_format = ctypes.c_int()
            item_count = ctypes.c_ulong()
            bytes_after = ctypes.c_ulong()
            data = ctypes.c_void_p()
            status = x_get_window_property(
                display,
                window_type(window),
                pid_atom,
                0,
                1,
                0,
                0,
                ctypes.byref(actual_type),
                ctypes.byref(actual_format),
                ctypes.byref(item_count),
                ctypes.byref(bytes_after),
                ctypes.byref(data),
            )
            try:
                if status != 0 or not data.value or item_count.value == 0:
                    return None
                return int(ctypes.cast(data, ctypes.POINTER(ctypes.c_uint32))[0])
            finally:
                if data.value:
                    x_free(data)

        def find_window() -> tuple[int, int, int] | None:
            root_return = window_type()
            parent_return = window_type()
            children = ctypes.POINTER(window_type)()
            child_count = ctypes.c_uint()
            if not x_query_tree(
                display,
                root_window,
                ctypes.byref(root_return),
                ctypes.byref(parent_return),
                ctypes.byref(children),
                ctypes.byref(child_count),
            ):
                return None

            candidate = None
            try:
                for index in range(child_count.value):
                    window = int(children[index])
                    attributes = _X11WindowAttributes()
                    if not x_get_window_attributes(
                        display, window_type(window), ctypes.byref(attributes)
                    ):
                        continue
                    if attributes.width < 100 or attributes.height < 100:
                        continue
                    window_pid = get_window_pid(window)
                    if window_pid == native_pid:
                        return window, attributes.width, attributes.height
                    if window_pid is None:
                        hint = _X11ClassHint()
                        if x_get_class_hint(
                            display, window_type(window), ctypes.byref(hint)
                        ):
                            resource_class = _x11_string(hint.res_class)
                            if hint.res_name:
                                x_free(hint.res_name)
                            if hint.res_class:
                                x_free(hint.res_class)
                            if resource_class.lower() == "flet":
                                candidate = (window, attributes.width, attributes.height)
            finally:
                if bool(children):
                    x_free(children)
            return candidate

        def set_opacity(window: int, value: int) -> None:
            opacity = ctypes.c_uint32(value)
            x_change_property(
                display,
                window_type(window),
                opacity_atom,
                cardinal_atom,
                32,
                0,
                ctypes.byref(opacity),
                1,
            )
            x_flush(display)

        deadline = time.monotonic() + 12.0
        hidden_window: int | None = None
        try:
            while time.monotonic() < deadline:
                window_info = find_window()
                if window_info is None:
                    time.sleep(0.03)
                    continue

                window, width, height = window_info
                if hidden_window is None:
                    hidden_window = window
                    set_opacity(window, 0)

                if window == hidden_window and (width, height) == (
                    WINDOW_WIDTH,
                    WINDOW_HEIGHT,
                ):
                    set_opacity(window, 0xFFFFFFFF)
                    return

                time.sleep(0.03)
        finally:
            if hidden_window is not None:
                set_opacity(hidden_window, 0xFFFFFFFF)
            x_close_display(display)
    except Exception:
        # 没有 X11 开发库或窗口管理器不支持该属性时，保留 Flet 默认行为。
        return


def _install_flet_startup_cloak() -> None:
    """包装 Flet 原生子进程启动，在 Xwayland 下屏蔽默认启动页闪现。"""
    if not sys.platform.startswith("linux") or not os.environ.get("DISPLAY"):
        return
    if os.environ.get("GDK_BACKEND", "").lower() == "wayland":
        return

    try:
        import flet_desktop

        original_open = flet_desktop.open_flet_view_async
        if getattr(original_open, "_mcp_startup_cloak", False):
            return

        async def open_flet_view_with_cloak(*args: Any, **kwargs: Any) -> Any:
            result = await original_open(*args, **kwargs)
            process = result[0]
            threading.Thread(
                target=_x11_startup_cloak,
                args=(process.pid,),
                daemon=True,
                name="flet-startup-cloak",
            ).start()
            return result

        open_flet_view_with_cloak._mcp_startup_cloak = True
        flet_desktop.open_flet_view_async = open_flet_view_with_cloak
    except Exception:
        return


def _synchronized_ui_method(method: Callable[..., Any]) -> Callable[..., Any]:
    """串行执行会修改控件树的界面方法，并在关闭后直接忽略迟到调用。"""
    def wrapper(self, *args: Any, **kwargs: Any) -> Any:
        with self._page_command_lock:
            if self._closing:
                return None
            return method(self, *args, **kwargs)

    return wrapper


class GatewayAppUI:
    """Flet 界面控制器与组件构建器。"""

    def __init__(self, page: ft.Page, manager: GatewayManager | None = None) -> None:
        self.page = page
        self.manager = manager or GatewayManager()
        self.current_service_index = 0 if self.manager.config.services else -1
        self.selected_keys: set[str] = set()
        self.search_filter: str = ""
        self.status_filter: str = "all"  # "all", "normal", "disabled"

        self._auto_apply_timer: threading.Timer | None = None
        self._key_state_mtime: int | None = None
        self._poll_running = True
        self._poll_stop = threading.Event()
        # 所有 Flet 页面命令与控件树修改共用一把可重入锁，关窗时可等待当前命令完成。
        self._page_command_lock = threading.RLock()
        self._closing = False

        # 构建所有控件实例
        self._init_controls()
        # 清理页面中的默认控件后再挂载完整工作台。
        self._page_call(self.page.clean)
        self._build_layout()
        self._load_data()
        self._start_background_polling()

    def _page_call(self, callback: Callable[[], Any]) -> Any | None:
        """在页面生命周期锁内执行 Flet 命令，关闭后拒绝新的原生调用。"""
        with self._page_command_lock:
            if self._closing:
                return None
            try:
                return callback()
            except Exception:
                # 页面断开或原生窗口正在退出时，忽略迟到的 UI 命令。
                return None

    def _page_update(self) -> None:
        """安全更新页面。"""
        self._page_call(self.page.update)

    def _page_open(self, control: ft.Control) -> None:
        """安全打开页面浮层。"""
        self._page_call(lambda: self.page.open(control))

    def _page_close(self, control: ft.Control) -> None:
        """安全关闭页面浮层。"""
        self._page_call(lambda: self.page.close(control))

    # ── 控件初始化 ────────────────────────────────────────────────────────────
    def _init_controls(self) -> None:
        """初始化输入、选择与状态控件。"""
        # 顶部网关配置控件
        self.port_input = ft.TextField(
            label="网关端口",
            dense=True,
            width=90,
            text_size=12,
            border_color=CLR_BORDER,
            on_change=self._on_gateway_setting_change,
        )
        self.access_key_input = ft.TextField(
            label="统一访问密钥",
            dense=True,
            width=180,
            password=True,
            can_reveal_password=True,
            text_size=12,
            border_color=CLR_BORDER,
            on_change=self._on_gateway_setting_change,
        )
        self.routing_mode_dropdown = ft.Dropdown(
            label="路由模式",
            dense=True,
            width=110,
            text_size=12,
            border_color=CLR_BORDER,
            options=[
                ft.dropdown.Option("轮询", "轮询均衡"),
                ft.dropdown.Option("主备", "主备优先"),
            ],
            on_change=self._on_gateway_setting_change,
        )
        self.cooldown_input = ft.TextField(
            label="冷却(s)",
            dense=True,
            width=80,
            text_size=12,
            border_color=CLR_BORDER,
            on_change=self._on_gateway_setting_change,
        )
        self.retries_input = ft.TextField(
            label="重试次数",
            dense=True,
            width=80,
            text_size=12,
            border_color=CLR_BORDER,
            on_change=self._on_gateway_setting_change,
        )
        self.timeout_input = ft.TextField(
            label="超时(s)",
            dense=True,
            width=80,
            text_size=12,
            border_color=CLR_BORDER,
            on_change=self._on_gateway_setting_change,
        )
        self.auto_apply_switch = ft.Switch(
            label="自动保存应用",
            value=True,
            label_position=ft.LabelPosition.LEFT,
        )

        # 状态指示灯与文字
        self.status_indicator = ft.Container(
            width=10,
            height=10,
            border_radius=5,
            bgcolor=ft.Colors.GREY_500,
        )
        self.status_text = ft.Text(
            "检测中...",
            size=12,
            color=CLR_MUTED,
            weight=ft.FontWeight.W_500,
        )

        # 左侧服务列表容器
        self.services_listview = ft.ListView(
            spacing=8,
            padding=ft.padding.only(top=4, bottom=8),
            expand=True,
        )

        # 统计看板紧凑文字
        self.stat_total_val = ft.Text("0", size=11, weight=ft.FontWeight.BOLD, color=CLR_TEXT)
        self.stat_healthy_val = ft.Text("0", size=11, weight=ft.FontWeight.BOLD, color=CLR_SUCCESS)
        self.stat_disabled_val = ft.Text("0", size=11, weight=ft.FontWeight.BOLD, color=CLR_ERROR)
        self.stat_success_val = ft.Text("0", size=11, weight=ft.FontWeight.BOLD, color=CLR_ACCENT)

        # 密钥池筛选与操作栏控件
        self.search_input = ft.TextField(
            prefix_icon=ft.Icons.SEARCH,
            hint_text="搜索密钥前缀/尾号...",
            dense=True,
            width=200,
            text_size=12,
            border_color=CLR_BORDER,
            on_change=self.handle_search_change,
        )
        self.filter_dropdown = ft.Dropdown(
            dense=True,
            width=110,
            text_size=12,
            value="all",
            border_color=CLR_BORDER,
            options=[
                ft.dropdown.Option("all", "全部状态"),
                ft.dropdown.Option("normal", "仅正常"),
                ft.dropdown.Option("disabled", "仅失效/冷却"),
            ],
            on_change=self.handle_filter_change,
        )
        self.select_all_cb = ft.Checkbox(
            label="全选",
            value=False,
            on_change=self.handle_select_all_toggle,
        )

        # 密钥列表容器
        self.keys_listview = ft.ListView(
            spacing=6,
            padding=ft.padding.all(6),
            expand=True,
        )

        # 服务参数配置控件
        self.svc_name_input = ft.TextField(
            label="服务唯一标识 (Name)",
            dense=True,
            text_size=12,
            read_only=True,
            border_color=CLR_BORDER,
        )
        self.svc_enabled_switch = ft.Switch(
            label="启用该 MCP 服务",
            value=True,
            on_change=self._on_service_field_change,
        )
        self.svc_url_input = ft.TextField(
            label="上游目标 URL",
            dense=True,
            text_size=12,
            border_color=CLR_BORDER,
            on_change=self._on_service_field_change,
        )
        self.key_auth_enabled_switch = ft.Switch(
            label="启用密钥自动轮询注入",
            value=True,
            on_change=self._on_service_field_change,
        )
        self.key_type_dropdown = ft.Dropdown(
            label="注入位置",
            dense=True,
            width=120,
            text_size=12,
            border_color=CLR_BORDER,
            options=[
                ft.dropdown.Option("header", "Header (请求头)"),
                ft.dropdown.Option("query", "Query (URL 参数)"),
            ],
            on_change=self._on_service_field_change,
        )
        self.key_param_input = ft.TextField(
            label="认证参数名 (如 Authorization 或 key)",
            dense=True,
            text_size=12,
            border_color=CLR_BORDER,
            on_change=self._on_service_field_change,
        )
        self.patterns_input = ft.TextField(
            label="上游失败特征列表 (每行一条关键字或正则)",
            multiline=True,
            min_lines=5,
            max_lines=9,
            text_size=12,
            text_style=ft.TextStyle(font_family=resolve_mono_font_family()),
            border_color=CLR_BORDER,
            on_change=self._on_service_field_change,
        )

        # 客户端配置与日志控件
        self.mcp_client_type = "claude"
        self.mcp_config_display = ft.TextField(
            multiline=True,
            min_lines=10,
            max_lines=16,
            read_only=True,
            text_size=11,
            text_style=ft.TextStyle(font_family=resolve_mono_font_family()),
            border_color=CLR_BORDER,
        )
        self.log_listview = ft.ListView(
            spacing=4,
            padding=ft.padding.all(8),
            auto_scroll=True,
            expand=True,
        )

    # ── 界面布局搭建 ──────────────────────────────────────────────────────────
    def _build_layout(self) -> None:
        """组装整体 Flet 界面结构。"""
        # 顶部品牌与控制栏
        top_bar = self._build_top_bar()

        # 左侧服务列表侧边栏
        sidebar = self._build_sidebar()

        # 右侧主工作区
        workspace = self._build_workspace()

        # 组装到页面根容器
        body_row = ft.Row(
            controls=[sidebar, ft.VerticalDivider(width=1, color=CLR_BORDER), workspace],
            expand=True,
            spacing=0,
        )

        root = ft.Container(
            content=ft.Column(
                controls=[top_bar, ft.Divider(height=1, color=CLR_BORDER), body_row],
                spacing=0,
                expand=True,
            ),
            expand=True,
            bgcolor=CLR_BG,
        )
        self._page_call(lambda: self.page.add(root))

    def _build_top_bar(self) -> ft.Container:
        """构建现代顶部导航栏与网关快捷设置。"""
        brand = ft.Row(
            controls=[
                ft.Icon(ft.Icons.HUB_ROUNDED, color=CLR_ACCENT, size=26),
                ft.Column(
                    controls=[
                        ft.Text(APP_TITLE, size=15, weight=ft.FontWeight.BOLD, color=CLR_TEXT),
                        ft.Row(
                            controls=[
                                self.status_indicator,
                                self.status_text,
                            ],
                            spacing=6,
                        ),
                    ],
                    spacing=2,
                ),
            ],
            spacing=10,
        )

        settings_row = ft.Row(
            controls=[
                self.port_input,
                self.access_key_input,
                self.routing_mode_dropdown,
                self.cooldown_input,
                self.retries_input,
                self.timeout_input,
            ],
            spacing=8,
            scroll=ft.ScrollMode.AUTO,
        )

        actions_row = ft.Row(
            controls=[
                self.auto_apply_switch,
                ft.ElevatedButton(
                    "重启服务",
                    icon=ft.Icons.REFRESH_ROUNDED,
                    style=ft.ButtonStyle(
                        bgcolor=ft.Colors.AMBER_800,
                        color=ft.Colors.WHITE,
                    ),
                    on_click=self.handle_restart_gateway,
                ),
                ft.FilledButton(
                    "立即应用",
                    icon=ft.Icons.CHECK_ROUNDED,
                    style=ft.ButtonStyle(bgcolor=CLR_ACCENT),
                    on_click=self.handle_save_config,
                ),
            ],
            spacing=8,
        )

        return ft.Container(
            content=ft.Row(
                controls=[brand, settings_row, actions_row],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(horizontal=16, vertical=10),
            bgcolor=CLR_SIDEBAR,
        )

    def _build_sidebar(self) -> ft.Container:
        """构建左侧服务导航栏（含添加/删除第三方 MCP 操作）。"""
        header = ft.Row(
            controls=[
                ft.Row(
                    controls=[
                        ft.Icon(ft.Icons.LIST_ALT_ROUNDED, color=CLR_ACCENT, size=18),
                        ft.Text("服务列表", size=13, weight=ft.FontWeight.BOLD, color=CLR_TEXT),
                    ],
                    spacing=6,
                ),
                ft.Row(
                    controls=[
                        ft.IconButton(
                            icon=ft.Icons.ADD_CIRCLE_OUTLINE_ROUNDED,
                            icon_color=CLR_SUCCESS,
                            tooltip="添加第三方 MCP 服务",
                            icon_size=18,
                            on_click=self.handle_add_service,
                        ),
                        ft.IconButton(
                            icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                            icon_color=CLR_ERROR,
                            tooltip="删除当前选中的服务",
                            icon_size=18,
                            on_click=self.handle_delete_service,
                        ),
                    ],
                    spacing=2,
                ),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )

        return ft.Container(
            content=ft.Column(
                controls=[header, ft.Divider(height=1, color=CLR_BORDER), self.services_listview],
                spacing=8,
                expand=True,
            ),
            width=260,
            padding=ft.padding.all(12),
            bgcolor=CLR_SIDEBAR,
        )

    def _build_workspace(self) -> ft.Container:
        """构建右侧多标签页工作区。"""
        # Tab 1: 密钥池管理
        keys_tab_content = self._build_keys_tab()

        # Tab 2: 服务配置
        service_tab_content = self._build_service_tab()

        # Tab 3: MCP 客户端配置
        mcp_tab_content = self._build_mcp_tab()

        # Tab 4: 实时控制台
        log_tab_content = self._build_log_tab()

        self.tabs = ft.Tabs(
            selected_index=0,
            animation_duration=200,
            tabs=[
                ft.Tab(
                    text="密钥池管理",
                    icon=ft.Icons.VPN_KEY_ROUNDED,
                    content=keys_tab_content,
                ),
                ft.Tab(
                    text="上游服务参数",
                    icon=ft.Icons.TUNE_ROUNDED,
                    content=service_tab_content,
                ),
                ft.Tab(
                    text="客户端配置代码",
                    icon=ft.Icons.CODE_ROUNDED,
                    content=mcp_tab_content,
                ),
                ft.Tab(
                    text="运行与测试日志",
                    icon=ft.Icons.TERMINAL_ROUNDED,
                    content=log_tab_content,
                ),
            ],
            expand=True,
        )

        tab_header_trailing = ft.Container(
            content=ft.Row(
                controls=[
                    ft.IconButton(
                        icon=ft.Icons.REFRESH_ROUNDED,
                        icon_size=18,
                        tooltip="快速刷新全部服务与密钥",
                        on_click=lambda e: self._load_data(),
                    ),
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            top=4,
            right=8,
        )

        return ft.Container(
            content=ft.Stack(
                controls=[
                    self.tabs,
                    tab_header_trailing,
                ],
                expand=True,
            ),
            padding=ft.padding.only(left=12, right=12, top=4, bottom=4),
            expand=True,
        )

    def _build_keys_tab(self) -> ft.Container:
        """构建密钥池标签页（高纵向利用率排版：压缩状态卡片为徽标，按键融入工具栏右侧）。"""
        def make_stat_badge(icon: str, label: str, val_widget: ft.Text, color: str) -> ft.Container:
            return ft.Container(
                content=ft.Row(
                    controls=[
                        ft.Icon(icon, size=13, color=color),
                        ft.Text(label, size=11, color=CLR_MUTED),
                        val_widget,
                    ],
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                bgcolor=CLR_CARD,
                border=ft.border.all(1, CLR_BORDER),
                border_radius=6,
                padding=ft.padding.symmetric(horizontal=8, vertical=4),
            )

        stats_badges = ft.Row(
            controls=[
                make_stat_badge(ft.Icons.KEY_ROUNDED, "总数", self.stat_total_val, CLR_TEXT),
                make_stat_badge(ft.Icons.CHECK_CIRCLE_ROUNDED, "正常", self.stat_healthy_val, CLR_SUCCESS),
                make_stat_badge(ft.Icons.CANCEL_ROUNDED, "禁用", self.stat_disabled_val, CLR_ERROR),
                make_stat_badge(ft.Icons.TRENDING_UP_ROUNDED, "本月成功", self.stat_success_val, CLR_ACCENT),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # 工具栏第 1 行：左侧搜索与状态筛选，右侧 4 个高频操作按钮
        toolbar_row_1 = ft.Row(
            controls=[
                ft.Row(
                    controls=[
                        self.select_all_cb,
                        self.search_input,
                        self.filter_dropdown,
                    ],
                    spacing=8,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Row(
                    controls=[
                        ft.ElevatedButton(
                            "导入密钥",
                            icon=ft.Icons.ADD_ROUNDED,
                            height=32,
                            style=ft.ButtonStyle(
                                bgcolor="#0d9488",
                                color=ft.Colors.WHITE,
                                padding=ft.padding.symmetric(horizontal=10),
                            ),
                            on_click=self.handle_import_keys,
                        ),
                        ft.ElevatedButton(
                            "测试选中",
                            icon=ft.Icons.PLAY_ARROW_ROUNDED,
                            height=32,
                            style=ft.ButtonStyle(
                                bgcolor="#6366f1",
                                color=ft.Colors.WHITE,
                                padding=ft.padding.symmetric(horizontal=10),
                            ),
                            on_click=self.handle_test_selected,
                        ),
                        ft.ElevatedButton(
                            "测试全部",
                            icon=ft.Icons.PLAY_CIRCLE_FILLED_ROUNDED,
                            height=32,
                            style=ft.ButtonStyle(
                                bgcolor="#0284c7",
                                color=ft.Colors.WHITE,
                                padding=ft.padding.symmetric(horizontal=10),
                            ),
                            on_click=self.handle_test_all,
                        ),
                        ft.ElevatedButton(
                            "查询额度",
                            icon=ft.Icons.ASSESSMENT_ROUNDED,
                            height=32,
                            style=ft.ButtonStyle(
                                bgcolor="#0f766e",
                                color=ft.Colors.WHITE,
                                padding=ft.padding.symmetric(horizontal=10),
                            ),
                            on_click=self.handle_query_usage,
                        ),
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # 工具栏第 2 行：左侧 4 个紧凑统计徽标，右侧 4 个状态维护按钮
        toolbar_row_2 = ft.Row(
            controls=[
                stats_badges,
                ft.Row(
                    controls=[
                        ft.ElevatedButton(
                            "恢复选中",
                            icon=ft.Icons.RESTORE_ROUNDED,
                            height=32,
                            style=ft.ButtonStyle(
                                bgcolor="#166534",
                                color=ft.Colors.WHITE,
                                padding=ft.padding.symmetric(horizontal=10),
                            ),
                            on_click=self.handle_reset_selected,
                        ),
                        ft.ElevatedButton(
                            "全部恢复",
                            icon=ft.Icons.RESTORE_PAGE_ROUNDED,
                            height=32,
                            style=ft.ButtonStyle(
                                bgcolor="#15803d",
                                color=ft.Colors.WHITE,
                                padding=ft.padding.symmetric(horizontal=10),
                            ),
                            on_click=self.handle_reset_all_disabled,
                        ),
                        ft.OutlinedButton(
                            "查看错误",
                            icon=ft.Icons.ERROR_OUTLINE_ROUNDED,
                            height=32,
                            style=ft.ButtonStyle(
                                padding=ft.padding.symmetric(horizontal=10),
                            ),
                            on_click=self.handle_show_raw_errors,
                        ),
                        ft.ElevatedButton(
                            "删除选中",
                            icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                            height=32,
                            style=ft.ButtonStyle(
                                bgcolor="#991b1b",
                                color=ft.Colors.WHITE,
                                padding=ft.padding.symmetric(horizontal=10),
                            ),
                            on_click=self.handle_delete_selected,
                        ),
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        return ft.Container(
            content=ft.Column(
                controls=[
                    toolbar_row_1,
                    toolbar_row_2,
                    ft.Divider(height=1, color=CLR_BORDER),
                    self.keys_listview,
                ],
                spacing=6,
                expand=True,
            ),
            padding=ft.padding.symmetric(vertical=4),
            expand=True,
        )

    def _build_service_tab(self) -> ft.Container:
        """构建上游服务参数配置标签页。"""
        row1 = ft.Row(
            controls=[
                ft.Container(content=self.svc_name_input, expand=1),
                self.svc_enabled_switch,
                ft.Container(content=self.svc_url_input, expand=3),
            ],
            spacing=16,
        )
        row2 = ft.Row(
            controls=[
                self.key_auth_enabled_switch,
                self.key_type_dropdown,
                ft.Container(content=self.key_param_input, expand=2),
            ],
            spacing=16,
        )

        card = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text("基础与认证配置", size=14, weight=ft.FontWeight.BOLD, color=CLR_TEXT),
                    row1,
                    row2,
                    ft.Divider(height=1, color=CLR_BORDER),
                    ft.Text("故障判定特征 (Failure Patterns)", size=14, weight=ft.FontWeight.BOLD, color=CLR_TEXT),
                    self.patterns_input,
                ],
                spacing=12,
            ),
            bgcolor=CLR_CARD,
            border=ft.border.all(1, CLR_BORDER),
            border_radius=8,
            padding=ft.padding.all(16),
        )

        return ft.Container(
            content=card,
            padding=ft.padding.symmetric(vertical=12),
            expand=True,
        )

    def _build_mcp_tab(self) -> ft.Container:
        """构建客户端配置代码生成标签页。"""
        tabs = ft.Row(
            controls=[
                ft.ElevatedButton(
                    "Claude Desktop",
                    icon=ft.Icons.INTEGRATION_INSTRUCTIONS_ROUNDED,
                    on_click=lambda e: self._switch_mcp_client("claude"),
                ),
                ft.ElevatedButton(
                    "Cursor / Windsurf",
                    icon=ft.Icons.TERMINAL_ROUNDED,
                    on_click=lambda e: self._switch_mcp_client("cursor"),
                ),
                ft.FilledButton(
                    "复制配置 JSON",
                    icon=ft.Icons.CONTENT_COPY_ROUNDED,
                    style=ft.ButtonStyle(bgcolor=CLR_ACCENT),
                    on_click=self.handle_copy_mcp_config,
                ),
            ],
            spacing=10,
        )

        card = ft.Container(
            content=ft.Column(
                controls=[
                    tabs,
                    ft.Divider(height=1, color=CLR_BORDER),
                    self.mcp_config_display,
                ],
                spacing=10,
                expand=True,
            ),
            bgcolor=CLR_CARD,
            border=ft.border.all(1, CLR_BORDER),
            border_radius=8,
            padding=ft.padding.all(16),
            expand=True,
        )

        return ft.Container(
            content=card,
            padding=ft.padding.symmetric(vertical=12),
            expand=True,
        )

    def _build_log_tab(self) -> ft.Container:
        """构建实时日志与测试控制台标签页。"""
        toolbar = ft.Row(
            controls=[
                ft.Text("运行与测试日志输出", size=13, weight=ft.FontWeight.BOLD, color=CLR_TEXT),
                ft.OutlinedButton(
                    "清空日志",
                    icon=ft.Icons.DELETE_SWEEP_ROUNDED,
                    on_click=self.handle_clear_logs,
                ),
            ],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )

        card = ft.Container(
            content=ft.Column(
                controls=[toolbar, ft.Divider(height=1, color=CLR_BORDER), self.log_listview],
                spacing=8,
                expand=True,
            ),
            bgcolor=CLR_CARD,
            border=ft.border.all(1, CLR_BORDER),
            border_radius=8,
            padding=ft.padding.all(12),
            expand=True,
        )

        return ft.Container(
            content=card,
            padding=ft.padding.symmetric(vertical=12),
            expand=True,
        )

    # ── 数据绑定与刷新 ────────────────────────────────────────────────────────
    def _load_data(self) -> None:
        """初始化加载配置与当前服务。"""
        gw = self.manager.config.gateway
        self.port_input.value = str(gw.port)
        self.access_key_input.value = gw.access_keys[0] if gw.access_keys else ""
        self.routing_mode_dropdown.value = ROUTING_MODE_LABELS.get(gw.routing_mode, "轮询")
        self.cooldown_input.value = str(gw.key_cooldown_seconds)
        self.retries_input.value = str(gw.max_failover_retries)
        self.timeout_input.value = str(gw.upstream_timeout_seconds)

        self.refresh_service_list()
        if 0 <= self.current_service_index < len(self.manager.config.services):
            self.load_service(self.current_service_index)

    @_synchronized_ui_method
    def load_service(self, index: int) -> None:
        """将指定服务的配置加载到工作区。"""
        if not (0 <= index < len(self.manager.config.services)):
            return
        self.current_service_index = index
        svc = self.manager.config.services[index]

        self.svc_name_input.value = svc.name
        self.svc_enabled_switch.value = svc.enabled
        self.svc_url_input.value = svc.upstream_url
        self.key_auth_enabled_switch.value = svc.key_auth.enabled
        self.key_type_dropdown.value = svc.key_auth.type
        self.key_param_input.value = svc.key_auth.param
        self.patterns_input.value = "\n".join(svc.failure_patterns)

        self.selected_keys.clear()
        self.select_all_cb.value = False
        self.refresh_keys_list()
        self._refresh_mcp_config()
        self.refresh_service_list()
        self._page_update()

    @_synchronized_ui_method
    def refresh_service_list(self) -> None:
        """刷新左侧服务卡片列表。"""
        self.services_listview.controls.clear()
        for i, svc in enumerate(self.manager.config.services):
            is_active = i == self.current_service_index
            keys_count = len(svc.keys)

            # 统计失效键
            items = self.manager.get_key_display_items(i)
            disabled_count = sum(1 for it in items if it.status_type != "normal")

            badge_color = CLR_SUCCESS if disabled_count == 0 else CLR_ERROR
            badge_text = f"{keys_count} 键" if disabled_count == 0 else f"{keys_count} 键 ({disabled_count} 失效)"

            def make_click_handler(idx: int):
                return lambda e: self.load_service(idx)

            card = ft.Container(
                content=ft.Row(
                    controls=[
                        ft.Icon(
                            ft.Icons.RADIO_BUTTON_CHECKED_ROUNDED if is_active else ft.Icons.CIRCLE_OUTLINED,
                            color=CLR_ACCENT if is_active else CLR_MUTED,
                            size=16,
                        ),
                        ft.Column(
                            controls=[
                                ft.Text(
                                    svc.name,
                                    size=13,
                                    weight=ft.FontWeight.BOLD if is_active else ft.FontWeight.NORMAL,
                                    color=CLR_TEXT,
                                ),
                                ft.Text(
                                    badge_text,
                                    size=10,
                                    color=badge_color,
                                ),
                            ],
                            spacing=1,
                            expand=True,
                        ),
                        ft.Switch(
                            value=svc.enabled,
                            scale=0.7,
                            on_change=lambda e, idx=i: self._on_service_toggle(idx, e.control.value),
                        ),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                ),
                padding=ft.padding.symmetric(horizontal=10, vertical=8),
                bgcolor=CLR_CARD_ACTIVE if is_active else ft.Colors.TRANSPARENT,
                border=ft.border.all(1, CLR_ACCENT if is_active else CLR_BORDER),
                border_radius=8,
                on_click=make_click_handler(i),
            )
            self.services_listview.controls.append(card)

    @_synchronized_ui_method
    def refresh_keys_list(self) -> None:
        """根据搜索与状态过滤器渲染当前服务的密钥列表。"""
        self.keys_listview.controls.clear()
        if not (0 <= self.current_service_index < len(self.manager.config.services)):
            return

        items = self.manager.get_key_display_items(self.current_service_index)

        # 更新看板数据
        total = len(items)
        healthy = sum(1 for it in items if it.status_type == "normal")
        disabled = sum(1 for it in items if it.status_type != "normal")
        total_success = sum(it.monthly_success_count for it in items)

        self.stat_total_val.value = str(total)
        self.stat_healthy_val.value = str(healthy)
        self.stat_disabled_val.value = str(disabled)
        self.stat_success_val.value = str(total_success)

        # 过滤数据
        filtered_items: list[KeyDisplayItem] = []
        for it in items:
            if self.search_filter:
                sf = self.search_filter.lower()
                if sf not in it.key.lower():
                    continue
            if self.status_filter == "normal" and it.status_type != "normal":
                continue
            if self.status_filter == "disabled" and it.status_type == "normal":
                continue
            filtered_items.append(it)

        if not filtered_items:
            self.keys_listview.controls.append(
                ft.Container(
                    content=ft.Text("暂无匹配的密钥记录", color=CLR_MUTED, size=12),
                    alignment=ft.alignment.center,
                    padding=ft.padding.all(30),
                )
            )
            return

        for it in filtered_items:
            k = it.key
            is_checked = k in self.selected_keys

            # 状态芯片颜色与标签
            chip_color = CLR_SUCCESS
            if it.status_type == "cooldown":
                chip_color = CLR_WARN
            elif it.status_type == "disabled":
                chip_color = CLR_ERROR
            elif it.status_type == "retest":
                chip_color = "#f97316"

            def make_checkbox_handler(key_val: str):
                return lambda e: self.toggle_key_selection(key_val, e.control.value)

            def make_copy_handler(key_val: str):
                return lambda e: self._copy_text(key_val, "密钥已复制到剪贴板")

            def make_single_test_handler(key_val: str):
                return lambda e: self.handle_single_key_test(key_val)

            def make_single_restore_handler(key_val: str):
                return lambda e: self.handle_single_key_restore(key_val)

            def make_single_delete_handler(key_val: str):
                return lambda e: self.handle_single_key_delete(key_val)

            card_row = ft.Row(
                controls=[
                    ft.Checkbox(
                        value=is_checked,
                        on_change=make_checkbox_handler(k),
                    ),
                    ft.Container(
                        content=ft.Row(
                            controls=[
                                ft.Text(it.display_key, size=12, weight=ft.FontWeight.W_500, color=CLR_TEXT),
                                ft.IconButton(
                                    icon=ft.Icons.COPY_ROUNDED,
                                    icon_size=14,
                                    tooltip="复制完整密钥",
                                    on_click=make_copy_handler(k),
                                ),
                            ],
                            spacing=2,
                        ),
                        width=220,
                    ),
                    ft.Container(
                        content=ft.Text(it.status_str, size=10, color=chip_color, weight=ft.FontWeight.BOLD),
                        padding=ft.padding.symmetric(horizontal=8, vertical=4),
                        border=ft.border.all(1, chip_color),
                        border_radius=6,
                    ),
                    ft.Text(f"本月成功: {it.monthly_success_count}", size=11, color=CLR_MUTED),
                    ft.Text(it.quota_info or "", size=11, color=CLR_ACCENT) if it.quota_info else ft.Container(),
                    ft.Row(
                        controls=[
                            ft.IconButton(
                                icon=ft.Icons.PLAY_ARROW_ROUNDED,
                                icon_color=CLR_ACCENT,
                                tooltip="测试此密钥",
                                on_click=make_single_test_handler(k),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.RESTORE_ROUNDED,
                                icon_color=CLR_SUCCESS,
                                tooltip="恢复此密钥",
                                on_click=make_single_restore_handler(k),
                            ),
                            ft.IconButton(
                                icon=ft.Icons.DELETE_ROUNDED,
                                icon_color=CLR_ERROR,
                                tooltip="删除此密钥",
                                on_click=make_single_delete_handler(k),
                            ),
                        ],
                        spacing=2,
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            )

            item_card = ft.Container(
                content=card_row,
                padding=ft.padding.symmetric(horizontal=8, vertical=4),
                bgcolor=CLR_CARD,
                border=ft.border.all(1, CLR_BORDER),
                border_radius=8,
            )
            self.keys_listview.controls.append(item_card)

    def _refresh_mcp_config(self) -> None:
        """刷新 MCP 配置展示。"""
        if not (0 <= self.current_service_index < len(self.manager.config.services)):
            self.mcp_config_display.value = "{}"
            return
        svc = self.manager.config.services[self.current_service_index]
        self.mcp_config_display.value = self.manager.generate_mcp_config(
            svc.name, self.mcp_client_type
        )

    @_synchronized_ui_method
    def _switch_mcp_client(self, client_type: str) -> None:
        self.mcp_client_type = client_type
        self._refresh_mcp_config()
        self._page_update()

    # ── 交互事件处理 ──────────────────────────────────────────────────────────
    def _on_gateway_setting_change(self, e=None) -> None:
        """网关配置变化时同步并调度防抖自动应用。"""
        self._schedule_auto_apply()

    def _on_service_field_change(self, e=None) -> None:
        """当前服务参数变化时同步。"""
        if not (0 <= self.current_service_index < len(self.manager.config.services)):
            return
        try:
            self.manager.update_service_config(
                service_index=self.current_service_index,
                name=self.svc_name_input.value or "",
                upstream_url=self.svc_url_input.value or "",
                enabled=bool(self.svc_enabled_switch.value),
                key_auth_enabled=bool(self.key_auth_enabled_switch.value),
                key_type=self.key_type_dropdown.value or "header",
                key_param=self.key_param_input.value or "",
                failure_patterns=split_lines(self.patterns_input.value or ""),
            )
            self._schedule_auto_apply()
        except Exception as exc:
            self.log(f"⚠️ 配置校验警告: {exc}")

    @_synchronized_ui_method
    def _on_service_toggle(self, index: int, enabled: bool) -> None:
        """服务启用开关切换。"""
        if 0 <= index < len(self.manager.config.services):
            self.manager.config.services[index].enabled = enabled
            if index == self.current_service_index:
                self.svc_enabled_switch.value = enabled
            self._schedule_auto_apply()
            self.refresh_service_list()
            self._page_update()

    def _schedule_auto_apply(self) -> None:
        """防抖自动应用。"""
        with self._page_command_lock:
            if self._closing or not self.auto_apply_switch.value:
                return
            if self._auto_apply_timer:
                self._auto_apply_timer.cancel()
            self._auto_apply_timer = threading.Timer(0.8, self._auto_apply_worker)
            self._auto_apply_timer.start()

    def _auto_apply_worker(self) -> None:
        with self._page_command_lock:
            if self._closing:
                return
            self._auto_apply_timer = None
        try:
            self._sync_gateway_from_fields()
            self.manager.write_config_to_disk()
            self.manager.apply_runtime_sync(
                self.manager.config.gateway.port,
                self.manager.config.gateway.access_keys[0]
                if self.manager.config.gateway.access_keys
                else "",
            )
            self.log("✅ 配置修改已自动保存并热重载")
        except Exception as exc:
            self.log(f"⚠️ 自动应用未完成: {exc}")

    def _sync_gateway_from_fields(self) -> None:
        """将界面上的输入同步到 Manager 对象。"""
        port = int(self.port_input.value.strip() or "8080")
        gw_key = self.access_key_input.value.strip()
        cooldown = int(self.cooldown_input.value.strip() or "60")
        retries = int(self.retries_input.value.strip() or "1")
        timeout = int(self.timeout_input.value.strip() or "120")
        mode = self.routing_mode_dropdown.value or "轮询"

        self.manager.update_gateway_config(
            port=port,
            access_key=gw_key,
            cooldown=cooldown,
            ttl=1800,
            retries=retries,
            timeout=timeout,
            routing_mode=mode,
        )

    def handle_add_service(self, e=None) -> None:
        """打开添加第三方 MCP 服务弹窗。"""
        name_input = ft.TextField(
            label="服务唯一标识 (英文标识，如 brave、exa、deepseek)",
            hint_text="用于访问路由，如 /brave/mcp",
            dense=True,
            text_size=12,
            border_color=CLR_BORDER,
        )
        url_input = ft.TextField(
            label="上游目标 URL (如 https://mcp.brave.com/mcp)",
            hint_text="必须以 http:// 或 https:// 开头",
            dense=True,
            text_size=12,
            border_color=CLR_BORDER,
        )
        auth_type_dd = ft.Dropdown(
            label="密钥注入位置",
            dense=True,
            width=140,
            value="header",
            text_size=12,
            border_color=CLR_BORDER,
            options=[
                ft.dropdown.Option("header", "Header (请求头)"),
                ft.dropdown.Option("query", "Query (URL 参数)"),
            ],
        )
        auth_param_input = ft.TextField(
            label="参数名 (如 Authorization 或 key)",
            value="Authorization",
            dense=True,
            text_size=12,
            border_color=CLR_BORDER,
        )
        patterns_input = ft.TextField(
            label="故障判定过滤词 (每行一条，命中则故障转移)",
            value="rate limit\nquota\nunauthorized\n401\n429",
            multiline=True,
            min_lines=3,
            max_lines=5,
            text_size=12,
            text_style=ft.TextStyle(font_family=resolve_mono_font_family()),
            border_color=CLR_BORDER,
        )

        def do_confirm(ev):
            name_val = name_input.value.strip()
            url_val = url_input.value.strip()
            if not name_val:
                self.show_snack("服务标识不能为空", CLR_WARN)
                return
            if not url_val:
                self.show_snack("上游目标 URL 不能为空", CLR_WARN)
                return

            try:
                patterns = split_lines(patterns_input.value or "")
                self.manager.add_service(
                    name=name_val,
                    upstream_url=url_val,
                    key_auth_type=auth_type_dd.value or "header",
                    key_param=auth_param_input.value.strip() or "Authorization",
                    failure_patterns=patterns,
                )
                self._page_close(dlg)
                self.manager.write_config_to_disk()
                new_index = len(self.manager.config.services) - 1
                self.load_service(new_index)
                self.refresh_service_list()
                self._page_update()
                self.log(f"➕ 成功添加第三方 MCP 服务: [{name_val}] -> {url_val}")
                self.show_snack(f"成功添加服务 [{name_val}]！", CLR_SUCCESS)
            except Exception as exc:
                self.show_snack(f"添加服务失败: {exc}", CLR_ERROR)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("➕ 添加第三方 MCP 服务"),
            content=ft.Container(
                content=ft.Column(
                    controls=[
                        name_input,
                        url_input,
                        ft.Row(controls=[auth_type_dd, ft.Container(content=auth_param_input, expand=True)], spacing=10),
                        patterns_input,
                    ],
                    spacing=10,
                ),
                width=540,
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda ev: self._page_close(dlg)),
                ft.FilledButton("确认添加", on_click=do_confirm),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page_open(dlg)

    def handle_delete_service(self, e=None) -> None:
        """删除当前选中的服务。"""
        if not (0 <= self.current_service_index < len(self.manager.config.services)):
            self.show_snack("当前没有可删除的服务", CLR_WARN)
            return

        svc = self.manager.config.services[self.current_service_index]
        svc_name = svc.name

        def do_delete(ev):
            self._page_close(dlg)
            self.manager.delete_service(self.current_service_index)
            self.manager.write_config_to_disk()
            self.current_service_index = 0 if self.manager.config.services else -1
            if self.current_service_index >= 0:
                self.load_service(self.current_service_index)
            else:
                self.keys_listview.controls.clear()
                self.services_listview.controls.clear()
                self._page_update()
            self.refresh_service_list()
            self.log(f"🗑️ 已删除服务: [{svc_name}]")
            self.show_snack(f"服务 [{svc_name}] 已删除", CLR_SUCCESS)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认删除该服务？"),
            content=ft.Text(f"即将删除服务 [{svc_name}] 及其全部 {len(svc.keys)} 把密钥。\n此操作不可撤销。"),
            actions=[
                ft.TextButton("取消", on_click=lambda ev: self._page_close(dlg)),
                ft.FilledButton("确认删除", style=ft.ButtonStyle(bgcolor=CLR_ERROR), on_click=do_delete),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page_open(dlg)

    def handle_save_config(self, e=None) -> None:
        """手动点击立即应用配置。"""
        try:
            self._sync_gateway_from_fields()
            self.manager.write_config_to_disk()
            self.manager.apply_runtime_sync(
                self.manager.config.gateway.port,
                self.manager.config.gateway.access_keys[0]
                if self.manager.config.gateway.access_keys
                else "",
            )
            self.show_snack("✅ 配置已立即保存并应用成功！", CLR_SUCCESS)
            self.log("✅ 手动应用配置完成")
        except Exception as exc:
            self.show_snack(f"❌ 配置应用失败: {exc}", CLR_ERROR)
            self.log(f"❌ 配置应用失败: {exc}")

    def handle_restart_gateway(self, e=None) -> None:
        """重启本地网关服务。"""
        self.show_snack("🔄 正在重启本地网关服务，请稍候...", CLR_WARN)
        self.log("🔄 开始重启网关服务...")

        def _worker():
            try:
                self._sync_gateway_from_fields()
                self.manager.write_config_to_disk()
                target_port = self.manager.config.gateway.port
                target_key = (
                    self.manager.config.gateway.access_keys[0]
                    if self.manager.config.gateway.access_keys
                    else ""
                )
                self.manager.stop_running_gateway(self.manager.runtime_port, self.manager.runtime_access_key)
                self.manager.start_and_wait_gateway(target_port)
                self.manager.runtime_port = target_port
                self.manager.runtime_access_key = target_key
                self.log("✅ 网关服务已成功重启并恢复健康")
                self.show_snack("✅ 网关服务已成功重启并正常运行！", CLR_SUCCESS)
            except Exception as exc:
                self.log(f"❌ 网关重启失败: {exc}")
                self.show_snack(f"❌ 网关重启失败: {exc}", CLR_ERROR)

        threading.Thread(target=_worker, daemon=True).start()

    # ── 密钥操作动作 ──────────────────────────────────────────────────────────
    def toggle_key_selection(self, key: str, selected: bool) -> None:
        if selected:
            self.selected_keys.add(key)
        else:
            self.selected_keys.discard(key)
        self.refresh_keys_list()
        self._page_update()

    def handle_select_all_toggle(self, e=None) -> None:
        if not (0 <= self.current_service_index < len(self.manager.config.services)):
            return
        svc = self.manager.config.services[self.current_service_index]
        if self.select_all_cb.value:
            self.selected_keys = set(svc.keys)
        else:
            self.selected_keys.clear()
        self.refresh_keys_list()
        self._page_update()

    def handle_search_change(self, e=None) -> None:
        self.search_filter = self.search_input.value.strip()
        self.refresh_keys_list()
        self._page_update()

    def handle_filter_change(self, e=None) -> None:
        self.status_filter = self.filter_dropdown.value
        self.refresh_keys_list()
        self._page_update()

    def handle_import_keys(self, e=None) -> None:
        """打开批量导入密钥弹窗。"""
        if not (0 <= self.current_service_index < len(self.manager.config.services)):
            self.show_snack("请先选择一个有效服务", CLR_WARN)
            return

        text_input = ft.TextField(
            label="请输入密钥列表",
            hint_text="每行一把密钥，自动去重...",
            multiline=True,
            min_lines=8,
            max_lines=12,
            text_size=12,
            border_color=CLR_BORDER,
        )

        def do_confirm(ev):
            raw = text_input.value or ""
            new_count, all_keys = self.manager.import_keys(self.current_service_index, raw)
            self._page_close(dlg)
            self.manager.write_config_to_disk()
            self.refresh_keys_list()
            self.refresh_service_list()
            self._page_update()
            self.log(f"📥 批量导入完成: 新增 {new_count} 把密钥 (当前总数: {len(all_keys)})")
            self.show_snack(f"成功导入 {new_count} 把新密钥！", CLR_SUCCESS)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("📥 批量导入密钥"),
            content=ft.Container(content=text_input, width=540),
            actions=[
                ft.TextButton("取消", on_click=lambda ev: self._page_close(dlg)),
                ft.FilledButton("导入", on_click=do_confirm),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page_open(dlg)

    def handle_test_selected(self, e=None) -> None:
        """测试勾选的密钥。"""
        if not self.selected_keys:
            self.show_snack("请先勾选需要测试的密钥", CLR_WARN)
            return
        keys = list(self.selected_keys)
        self._run_keys_validation(keys)

    def handle_test_all(self, e=None) -> None:
        """测试当前服务的全部密钥。"""
        if not (0 <= self.current_service_index < len(self.manager.config.services)):
            return
        keys = list(self.manager.config.services[self.current_service_index].keys)
        if not keys:
            self.show_snack("当前服务没有可测试的密钥", CLR_WARN)
            return
        self._run_keys_validation(keys)

    def handle_single_key_test(self, key: str) -> None:
        self._run_keys_validation([key])

    def _run_keys_validation(self, keys: list[str]) -> None:
        """执行后台并发测试并弹窗汇报。"""
        svc = self.manager.config.services[self.current_service_index]
        self.log(f"🧪 开始并发测试 [{svc.name}] 的 {len(keys)} 把密钥...")
        self.show_snack(f"开始测试 {len(keys)} 把密钥，请稍候...", CLR_ACCENT)

        def _worker():
            try:
                results = asyncio.run(
                    self.manager.run_key_validation(
                        self.current_service_index, keys, concurrency=5, timeout=45.0
                    )
                )
                self.manager.write_config_to_disk()
                self._show_validation_results_dialog(svc.name, results)
            except Exception as exc:
                self.log(f"❌ 测试过程异常: {exc}")
                self.show_snack(f"测试失败: {exc}", CLR_ERROR)

        threading.Thread(target=_worker, daemon=True).start()

    @_synchronized_ui_method
    def _show_validation_results_dialog(self, svc_name: str, results: list[ValidationResult]) -> None:
        """展示测试结果弹窗。"""
        valid_count = sum(1 for r in results if r.status == "valid")
        failed_count = len(results) - valid_count

        result_items: list[ft.Control] = []
        for r in results:
            icon = ft.Icons.CHECK_CIRCLE_ROUNDED if r.status == "valid" else ft.Icons.CANCEL_ROUNDED
            color = CLR_SUCCESS if r.status == "valid" else CLR_ERROR
            tail = r.key[-8:] if len(r.key) >= 8 else r.key

            row = ft.Row(
                controls=[
                    ft.Icon(icon, color=color, size=16),
                    ft.Text(f"...{tail}", size=12, weight=ft.FontWeight.W_500, color=CLR_TEXT),
                    ft.Text(f"{r.latency_ms}ms", size=11, color=CLR_MUTED),
                    ft.Text(r.detail, size=11, color=color, expand=True),
                ],
                spacing=8,
            )
            result_items.append(
                ft.Container(
                    content=row,
                    padding=ft.padding.symmetric(horizontal=8, vertical=6),
                    bgcolor=CLR_CARD,
                    border_radius=6,
                )
            )

        dlg = ft.AlertDialog(
            title=ft.Text(f"[{svc_name}] 测试完成：✅ {valid_count} 成功 / ❌ {failed_count} 失败"),
            content=ft.Container(
                content=ft.ListView(controls=result_items, spacing=4),
                width=640,
                height=380,
            ),
            actions=[ft.TextButton("关闭", on_click=lambda ev: self._page_close(dlg))],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page_open(dlg)
        self.refresh_keys_list()
        self.refresh_service_list()
        self._page_update()

    def handle_query_usage(self, e=None) -> None:
        """查询选中密钥额度。"""
        if not self.selected_keys:
            self.show_snack("请先勾选需要查询额度的密钥", CLR_WARN)
            return
        keys = list(self.selected_keys)
        svc = self.manager.config.services[self.current_service_index]

        self.log(f"📊 开始查询 [{svc.name}] 的 {len(keys)} 把密钥额度...")
        self.show_snack(f"开始查询 {len(keys)} 把密钥额度...", CLR_ACCENT)

        def _worker():
            try:
                results = asyncio.run(
                    self.manager.run_usage_query(self.current_service_index, keys, timeout=20.0)
                )
                self._show_usage_results_dialog(svc.name, results)
            except Exception as exc:
                self.log(f"❌ 额度查询异常: {exc}")
                self.show_snack(f"额度查询失败: {exc}", CLR_ERROR)

        threading.Thread(target=_worker, daemon=True).start()

    @_synchronized_ui_method
    def _show_usage_results_dialog(
        self, svc_name: str, results: list[tuple[str, UsageSnapshot]]
    ) -> None:
        items: list[ft.Control] = []
        for key, snap in results:
            line = self.manager.format_usage_line(key, snap)
            items.append(
                ft.Container(
                    content=ft.Text(line, size=11, color=CLR_SUCCESS if snap.ok else CLR_ERROR),
                    padding=ft.padding.all(6),
                    bgcolor=CLR_CARD,
                    border_radius=6,
                )
            )

        dlg = ft.AlertDialog(
            title=ft.Text(f"[{svc_name}] 额度查询结果 (共 {len(results)} 把)"),
            content=ft.Container(
                content=ft.ListView(controls=items, spacing=4),
                width=640,
                height=360,
            ),
            actions=[ft.TextButton("关闭", on_click=lambda ev: self._page_close(dlg))],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page_open(dlg)
        self.refresh_keys_list()
        self._page_update()

    def handle_reset_selected(self, e=None) -> None:
        """恢复勾选的失效密钥。"""
        if not self.selected_keys:
            self.show_snack("请先勾选需要恢复的密钥", CLR_WARN)
            return
        keys = list(self.selected_keys)
        count = self.manager.reset_selected_keys(self.current_service_index, keys)
        self.refresh_keys_list()
        self.refresh_service_list()
        self._page_update()
        self.show_snack(f"已恢复 {count} 把密钥为可用状态！", CLR_SUCCESS)
        self.log(f"♻️ 已恢复 {count} 把密钥可用状态")

    def handle_reset_all_disabled(self, e=None) -> None:
        """恢复当前服务所有失效密钥。"""
        count = self.manager.reset_all_disabled_keys(self.current_service_index)
        self.refresh_keys_list()
        self.refresh_service_list()
        self._page_update()
        self.show_snack(f"已恢复全部 {count} 把失效密钥为可用状态！", CLR_SUCCESS)
        self.log(f"♻️ 全部恢复操作：已恢复 {count} 把密钥可用状态")

    def handle_single_key_restore(self, key: str) -> None:
        svc = self.manager.config.services[self.current_service_index]
        self.manager.restore_key_available(svc.name, key)
        self.refresh_keys_list()
        self.refresh_service_list()
        self._page_update()
        self.show_snack(f"密钥 ...{key[-6:]} 已恢复可用！", CLR_SUCCESS)

    def handle_delete_selected(self, e=None) -> None:
        """删除勾选的密钥。"""
        if not self.selected_keys:
            self.show_snack("请先勾选需要删除的密钥", CLR_WARN)
            return

        def do_delete(ev):
            self._page_close(dlg)
            keys = list(self.selected_keys)
            count = self.manager.delete_selected_keys(self.current_service_index, keys)
            self.selected_keys.clear()
            self.manager.write_config_to_disk()
            self.refresh_keys_list()
            self.refresh_service_list()
            self._page_update()
            self.show_snack(f"已删除 {count} 把密钥", CLR_SUCCESS)
            self.log(f"🗑️ 已删除选中的 {count} 把密钥")

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认删除选中的密钥？"),
            content=ft.Text(f"即将删除 {len(self.selected_keys)} 把密钥，此操作不可撤销。"),
            actions=[
                ft.TextButton("取消", on_click=lambda ev: self._page_close(dlg)),
                ft.FilledButton("确认删除", style=ft.ButtonStyle(bgcolor=CLR_ERROR), on_click=do_delete),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page_open(dlg)

    def handle_single_key_delete(self, key: str) -> None:
        def do_delete(ev):
            self._page_close(dlg)
            self.manager.delete_selected_keys(self.current_service_index, [key])
            self.selected_keys.discard(key)
            self.manager.write_config_to_disk()
            self.refresh_keys_list()
            self.refresh_service_list()
            self._page_update()
            self.show_snack("密钥已删除", CLR_SUCCESS)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("确认删除该密钥？"),
            content=ft.Text(f"密钥: ...{key[-8:]}\n此操作不可撤销。"),
            actions=[
                ft.TextButton("取消", on_click=lambda ev: self._page_close(dlg)),
                ft.FilledButton("确认删除", style=ft.ButtonStyle(bgcolor=CLR_ERROR), on_click=do_delete),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page_open(dlg)

    def handle_show_raw_errors(self, e=None) -> None:
        """查看选中密钥的原始失败报文。"""
        if not self.selected_keys:
            self.show_snack("请先勾选要查看报错的密钥", CLR_WARN)
            return
        keys = list(self.selected_keys)
        reports = self.manager.get_key_raw_errors(self.current_service_index, keys)

        text_content = "\n\n" + ("=" * 40 + "\n\n").join(reports)
        dlg = ft.AlertDialog(
            title=ft.Text("原始错误报文详情"),
            content=ft.Container(
                content=ft.TextField(
                    value=text_content,
                    read_only=True,
                    multiline=True,
                    min_lines=12,
                    text_size=11,
                ),
                width=680,
                height=400,
            ),
            actions=[ft.TextButton("关闭", on_click=lambda ev: self._page_close(dlg))],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._page_open(dlg)

    def handle_copy_mcp_config(self, e=None) -> None:
        self._copy_text(self.mcp_config_display.value or "", "MCP 客户端配置已复制到剪贴板！")

    def handle_clear_logs(self, e=None) -> None:
        self.log_listview.controls.clear()
        self._page_update()

    # ── 辅助与后台轮询 ────────────────────────────────────────────────────────
    def _copy_text(self, text: str, toast: str) -> None:
        self._page_call(lambda: self.page.set_clipboard(text))
        self.show_snack(toast, CLR_ACCENT)

    @_synchronized_ui_method
    def log(self, message: str) -> None:
        """向日志标签页输出一行日志。"""
        t_str = time.strftime("%H:%M:%S")
        self.log_listview.controls.append(
            ft.Text(f"[{t_str}] {message}", size=11, color=CLR_TEXT, font_family=resolve_mono_font_family())
        )
        self._page_update()

    def show_snack(self, message: str, color: str = CLR_ACCENT) -> None:
        """弹出底部轻量通知。"""
        sb = ft.SnackBar(
            content=ft.Text(message, color=ft.Colors.WHITE, size=12),
            bgcolor=color,
            show_close_icon=True,
        )
        self._page_open(sb)

    def _start_background_polling(self) -> None:
        """启动后台健康状态检查与密钥持久化文件变动检测。"""
        def _poll():
            while self._poll_running and not self._poll_stop.wait(2.0):
                if self._closing:
                    break
                # 检查网关健康
                is_up = self.manager.is_gateway_healthy()
                state_changed = False
                state_mtime: int | None = None
                try:
                    state_mtime = os.stat(self.manager.state_store.path).st_mtime_ns
                    state_changed = state_mtime != self._key_state_mtime
                except OSError:
                    pass

                # 网络请求必须在页面锁外执行，关闭窗口时不等待 HTTP 超时。
                if state_changed:
                    try:
                        self.manager.fetch_stats()
                    except Exception:
                        pass

                if self._closing or self._poll_stop.is_set():
                    break
                with self._page_command_lock:
                    if self._closing:
                        break
                    self.status_indicator.bgcolor = CLR_SUCCESS if is_up else ft.Colors.GREY_500
                    self.status_text.value = f"运行中 (端口 {self.manager.runtime_port})" if is_up else "已停止"

                    # 检查状态文件变动
                    if state_changed:
                        self._key_state_mtime = state_mtime
                        self.refresh_keys_list()
                        self.refresh_service_list()

                    self._page_update()

        threading.Thread(target=_poll, daemon=True).start()

    def cleanup(self) -> None:
        """停止后台任务并等待当前页面命令完成，不再向原生窗口发送命令。"""
        with self._page_command_lock:
            if self._closing:
                return
            self._closing = True
            self._poll_running = False
            self._poll_stop.set()
            timer = self._auto_apply_timer
            self._auto_apply_timer = None
        if timer:
            try:
                timer.cancel()
            except Exception:
                pass


def _configure_windows_dpi() -> None:
    """Windows 下启用高分屏 DPI 感知。"""
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


async def main(page: ft.Page) -> None:
    """Flet 客户端主入口。"""
    page.title = APP_TITLE
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = ft.Theme(font_family=resolve_ui_font_family())
    page.window.width = WINDOW_WIDTH
    page.window.height = WINDOW_HEIGHT
    page.window.min_width = WINDOW_MIN_WIDTH
    page.window.min_height = WINDOW_MIN_HEIGHT
    page.window.resizable = True
    page.window.maximizable = True
    page.window.minimizable = True
    # 配合 FLET_APP_HIDDEN，首个页面更新先保持隐藏，避免默认尺寸窗口抢先映射。
    page.window.visible = False
    page.padding = 0
    page.spacing = 0

    app_ui: GatewayAppUI | None = None

    # 先安装关闭处理器。异步入口在初始化期间不会让出事件循环，
    # 因此即使用户立即关闭窗口，也会等完整初始化结束后再处理关闭事件。
    async def on_close(e: ft.ControlEvent) -> None:
        if app_ui is not None:
            app_ui.cleanup()

    async def on_window_event(e: ft.WindowEvent) -> None:
        """在原生窗口发出关闭事件时尽早停止后台任务。"""
        if (
            getattr(e, "type", None) == ft.WindowEventType.CLOSE
            or getattr(e, "data", None) == "close"
        ):
            if app_ui is not None:
                app_ui.cleanup()

    async def on_disconnect(e: ft.ControlEvent) -> None:
        if app_ui is not None:
            app_ui.cleanup()

    page.on_close = on_close
    page.on_disconnect = on_disconnect
    page.window.on_event = on_window_event

    app_ui = GatewayAppUI(page)

    # 控件树、尺寸与最小尺寸均已提交后再显示窗口，避免启动时出现默认尺寸空窗。
    page.window.visible = True
    app_ui._page_update()


if __name__ == "__main__":
    _ensure_linux_native_compat()
    _configure_windows_dpi()
    _install_flet_startup_cloak()
    ft.app(target=main, view=ft.AppView.FLET_APP_HIDDEN)
