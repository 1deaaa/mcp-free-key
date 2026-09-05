# Flet Linux Wayland 适配指南

本文档记录本项目在 Kubuntu 26.04、KDE Plasma 6、Wayland 会话下，对 Flet 原生桌面窗口进行实际验证后保留下来的做法，供其他 Flet 项目的 AI 开发助手参考。

## 结论先行

本项目使用 Flet `0.28.3` 时遇到的启动黑屏、最大化弹回和关闭阶段崩溃风险，主要是旧版 Linux GTK 客户端的窗口时序与窗口状态同步问题，不是 Wayland 必然无法运行 Flet。

当前机器同时提供 `WAYLAND_DISPLAY` 和 `DISPLAY`（Xwayland），因此最稳定的方案是：

1. 保持 Python 端、桌面客户端和 `flet-desktop` 版本一致。
2. 在 Wayland 且存在 Xwayland 时，把 Flet GTK 客户端强制放到 X11 后端。
3. 在用户目录处理旧版客户端的 `libmpv.so.1` 动态库名称兼容问题。
4. 用用户级 `LD_PRELOAD` 垫片阻止 Flet 0.28.3 在已经最大化的窗口上再次执行尺寸还原。
5. 所有页面更新和关闭清理操作串行化。
6. 对旧版 Linux 客户端做启动遮罩：窗口先保持透明，待目标尺寸稳定且首帧有足够时间完成绘制后再恢复不透明。

纯 Wayland 且没有 Xwayland 时，不能使用 X11 的 `_NET_WM_WINDOW_OPACITY` 属性，因此本指南中的透明遮罩不能生效。此时应优先升级完整的 Flet Python 包和桌面客户端；若必须继续使用 0.28.3，只能接受启动黑帧仍可能出现，或自行维护 Wayland 原生客户端补丁。

## 调查依据

### Flet 0.28.3 的启动顺序

官方 `v0.28.3` 源码 `client/linux/my_application.cc` 的顺序是：

```cpp
gtk_window_set_default_size(window, 1280, 720);
gtk_widget_show(GTK_WIDGET(window));
```

顶层 GTK 窗口先以默认 `1280x720` 映射，然后 Flutter/Flet Python 页面才发送目标尺寸和控件树。于是会出现默认尺寸空窗、黑色 Flutter 表面或底部黑区。

### 新版 Flet 的改进范围

截至 2026-09-06，官方 PyPI/GitHub 最新稳定版为 `0.86.5`，发布于 2026-08-01。其 Linux 客户端已经改为：

```cpp
gtk_window_set_default_size(window, 1280, 720);
// 顶层窗口先只实现，不显示，由 Dart 决定何时显示
gtk_widget_realize(GTK_WIDGET(window));
gtk_widget_realize(GTK_WIDGET(view));
```

这项源码改动直接针对旧版“先显示、后初始化”的启动时序。`0.85.3` 的变更记录还明确修复了隐藏启动模式的窗口闪现和 Linux 窗口显示前定位问题。

但这不等于 `0.86.5` 已经替本项目解决所有问题：

- 发布说明没有把本项目的 GTK 最大化弹回和关闭握手问题列为已修复项。
- Flet `0.86.0` 已改变流式传输协议，旧 Python 端不能与新桌面客户端随意混用。
- 因此不能只把 `0.86.5` 的可执行文件替换到 `0.28.3` Python 项目中。
- 若升级，应让 `flet`、`flet-desktop`、CLI 和打包客户端一起升级，并按变更记录迁移后重新测试。

官方参考：

- [Flet v0.86.5 发布页](https://github.com/flet-dev/flet/releases/tag/v0.86.5)
- [Flet 0.86.5 Linux 客户端源码](https://github.com/flet-dev/flet/blob/v0.86.5/client/linux/my_application.cc)
- [Flet 0.28.3 Linux 客户端源码](https://github.com/flet-dev/flet/blob/v0.28.3/client/linux/my_application.cc)
- [Flet 0.86.5 变更记录](https://github.com/flet-dev/flet/blob/v0.86.5/CHANGELOG.md)

## 已验证有效的操作

### 1. 固定一组匹配的 Flet 版本

旧项目要锁定版本时，至少保持下面两项一致：

```text
flet==0.28.3
flet-desktop==0.28.3
```

不要让 Python 包是一个版本、`~/.flet` 中的桌面客户端是另一个版本。排查时使用实际运行环境检查：

```bash
/绝对路径/python -m pip show flet flet-desktop
```

### 2. Wayland 有 Xwayland 时选择 X11 GTK 后端

启动 Flet 前检测：

```python
is_wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or (
    os.environ.get("XDG_SESSION_TYPE") == "wayland"
)
if is_wayland and os.environ.get("DISPLAY"):
    os.environ["GDK_BACKEND"] = "x11"
```

应在 GTK/Flet 桌面客户端真正启动前完成。X11 后端在本项目中解决了 KDE Wayland 下的关闭握手不稳定，也使 X11 窗口属性遮罩成为可能。它不是把整个桌面会话切回 X11，而是让这个 GTK 子进程通过 Xwayland 运行。

不要在没有 `DISPLAY` 的纯 Wayland 环境中强行设置 `GDK_BACKEND=x11`，否则会把“可用但有缺陷”的窗口变成“无法创建窗口”。

### 3. 处理旧版客户端的 libmpv 名称兼容

Flet 0.28.x Linux 客户端可能链接 `libmpv.so.1`，而较新的发行版只提供 `libmpv.so.2`。本项目的有效做法是：

- 先确认系统中确实没有 `libmpv.so.1`。
- 找到系统实际的 `libmpv.so.2` 或兼容 `libmpv.so`。
- 在用户目录（例如 `~/.flet/compat_libs/`）建立 `libmpv.so.1` 符号链接。
- 把该目录加入子进程的 `LD_LIBRARY_PATH`。
- 不修改 `/usr/lib`，不要求 root，不使用未经确认的跨 ABI 链接。

这是启动依赖兼容，不是 Wayland 修复；只有旧客户端确实缺少动态库时才需要。

### 4. 保护 GTK 关闭阶段并串行化页面操作

本项目验证有效的关闭链路是：

- `page.on_close`、`page.window.on_event` 和 `page.on_disconnect` 都调用同一个幂等的 `cleanup()`。
- `cleanup()` 先设置关闭标志、停止轮询事件、取消延时保存任务。
- 页面更新、打开/关闭浮层和控件树修改共用一把可重入锁。
- 后台线程在执行网络请求时不持有页面锁，回到页面更新前再次检查关闭标志。
- Flet 0.28.3 的 Linux GTK 关闭阶段使用用户级 `LD_PRELOAD` 垫片，在 `gtk_window_is_maximized()` 为真时拦截有风险的后续操作，并对空指针场景直接放行。

这样可以避免后台线程在原生窗口已经注销后继续发送页面命令，也避免关闭时为了强制销毁窗口而制造新的 GTK 竞态。垫片应使用源代码摘要缓存，只有源代码变化时重新编译。

### 5. 修复 Flet 0.28.3 最大化后弹回

在页面初始化阶段明确设置：

```python
page.window.width = 1200
page.window.height = 800
page.window.min_width = 880
page.window.min_height = 580
page.window.resizable = True
page.window.maximizable = True
page.window.minimizable = True
```

本项目的实际问题是：窗口已经被窗口管理器最大化后，旧版 Flet 又把先前保存的普通窗口尺寸传给 `gtk_window_resize()`，GTK 随即把最大化状态还原。用户级 GTK 垫片只在窗口已经最大化时拦截这一笔 resize，普通调整大小仍然放行。

验证标准：最大化后连续观察窗口尺寸，不应自动回到 `1200x800`；点击还原后应恢复普通尺寸。

### 6. 旧版启动时隐藏默认尺寸空窗

在 Python 页面侧设置：

```python
page.window.visible = False
page.window.bgcolor = "#0f172a"
```

应用入口使用：

```python
ft.app(target=main, view=ft.AppView.FLET_APP_HIDDEN)
```

但在 Flet 0.28.3 上这两个设置单独不够，因为 GTK 客户端仍可能先映射顶层窗口。必须配合新版客户端，或在有 Xwayland 时使用外层遮罩。

当前项目在 Xwayland 下额外做了以下处理：

1. 包装 `flet_desktop.open_flet_view_async()`，取得桌面子进程 PID。
2. 通过 `_NET_WM_PID` 或 Flet 窗口类找到对应 X11 窗口。
3. 窗口出现后立即设置 `_NET_WM_WINDOW_OPACITY=0`。
4. 等窗口达到目标 `1200x800` 并保持稳定 `0.80` 秒。
5. 等 Python 端的首个完整页面更新已发送，再恢复不透明度 `0xFFFFFFFF`。
6. 最长等待时间到达后恢复不透明度，避免异常时窗口永久透明。

`0.80` 秒不是 Flet API 的神奇常数，而是本机实测值：`0.25` 秒仍会露出 Flutter 尚未完成的底部黑区，`0.80` 秒时最终截图已经完整。不同机器应重新采样，并把它当作可调的渲染稳定等待。

### 7. 启动遮罩只在 Xwayland 分支启用

遮罩代码必须同时满足：Linux、存在 `DISPLAY`、实际后端不是 `wayland`。纯 Wayland 没有可移植的 X11 窗口属性接口，不能把 X11 代码当作通用 Wayland 方案。当前项目的后端选择顺序是：

```text
Wayland + Xwayland -> GDK_BACKEND=x11 + X11 不透明度遮罩
纯 Wayland       -> 使用系统默认 GTK 后端，不宣称遮罩有效
非 Linux         -> 不执行 Linux 垫片
```

## Git 历史中哪些改动应保留

| 提交 | 实测有效部分 | 结论 |
| --- | --- | --- |
| `65f4842` | 锁定 Flet `0.28.3`，替换原 GUI 依赖 | 需要兼容当前代码时保留，并同步锁定桌面客户端 |
| `bb99cb8` | 用户目录 `libmpv.so.1` 兼容链接；关闭时停止后台任务 | 保留；前者解决旧客户端启动依赖，后者是关闭稳定性的基础 |
| `c98d58b` | Wayland 有 Xwayland 时选择 X11；页面命令串行化；关闭时拒绝迟到更新 | 保留，是当前环境的核心兼容层 |
| `4bc285a` | GTK 关闭阶段防护垫片及关闭事件早期清理 | 保留；与页面锁和幂等清理配套 |
| `25d235a` | 最大化状态下拦截重复 GTK resize；明确尺寸和窗口能力 | 保留，已实测解决最大化弹回 |
| 当前补丁 | Xwayland 启动透明遮罩；页面背景兜底；首帧更新后的渲染稳定等待 | 保留，已实测消除最终窗口底部黑区 |

`25d235a` 中的 `page.window.wait_until_ready_to_show = True` 单独没有解决 Flet 0.28.3 的黑屏时序，当前版本已移除。它不是必须保留的有效操作。`FLET_APP_HIDDEN` 和 `page.window.visible=False` 仍作为显示意图保留，但真正消除旧客户端早期映射问题的是 Xwayland 遮罩和首帧等待。

## 已验证无效或不应照搬的做法

### 只设置 `wait_until_ready_to_show`

它没有阻止 Flet 0.28.3 Linux 客户端在 GTK 层提前映射窗口，不能单独解决默认尺寸空窗和黑区。

### 只设置 `FLET_APP_HIDDEN` 或 `visible=False`

这是应用层的隐藏意图，旧版 Linux GTK 启动代码仍可能调用 `gtk_widget_show(window)`。必须配合新版客户端，或在有 Xwayland 时使用外层遮罩。

### 混用 Flet 0.28.3 Python 与 0.86.5 桌面二进制

不要把新客户端当作旧 Python 包的透明替换。协议、包结构和客户端行为均可能变化，至少应成套升级并执行完整回归。

### 在纯 Wayland 使用 X11 遮罩

Wayland 不提供允许任意客户端直接设置全局窗口不透明度的 X11 属性。没有 Xwayland 时，这段代码会主动跳过；这不是遗漏，而是能力边界。

## 适配其他项目的执行顺序

让 AI 开发助手按以下顺序处理，不要一开始就堆叠窗口参数：

1. 记录 Flet、`flet-desktop`、Python、GTK、桌面环境、会话类型和 `DISPLAY/WAYLAND_DISPLAY`。
2. 用官方对应版本源码确认 Linux 客户端是在 `show` 前还是 `realize` 前初始化窗口。
3. 分开验证动态库启动、后端选择、首帧显示、调整大小/最大化和关闭，不要把不同故障归为同一个“Wayland 问题”。
4. 先修复后台页面更新与关闭竞态，再修复最大化状态同步。
5. 对旧客户端增加 `visible=False`、明确尺寸和背景；有 Xwayland 时再增加窗口级透明遮罩。
6. 用时间线截图检查默认尺寸、目标尺寸、透明度和最终 Flutter 内容，而不是只看进程是否启动。
7. 在至少一次最大化、还原、普通关闭和启动期间关闭后，再决定是否提交兼容垫片。
8. 若纯 Wayland 仍有黑帧，明确记录为旧版客户端能力限制，并优先评估成套升级 Flet，而不是继续增加未经验证的 X11 调用。

## 本项目验证标准

在当前机器上，修复后的标准结果是：

- 启动过程中不会把默认 `1280x720` 黑色空窗作为可见应用展示。
- 透明遮罩解除时窗口为 `1200x800`，页面底部没有黑区。
- 最大化后保持屏幕工作区尺寸，不自动弹回普通尺寸。
- 还原后回到 `1200x800`。
- 标准关闭后 Python/Flet 进程均退出，无残留 GUI 进程。
- 关闭日志中的 Flet/Flutter 上游警告仍可能出现，但不能导致崩溃或残留进程。

最后一点很重要：Flet/Flutter 的上游关闭警告和应用故障不是同一个判据。应以进程是否残留、是否崩溃、下一次启动是否正常作为关闭回归标准。
