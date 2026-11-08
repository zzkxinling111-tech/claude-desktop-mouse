"""双语支持。

代码里一律用中文写文案 —— 中文是主语言，也是维护时的可读版本。需要英文时，
在 EN 词典里登记对应翻译：

    tr("没有找到可见的浏览器窗口。")
    tr("等待 {n} 秒后仍未出现。", n=5)

没有登记翻译的条目会原样返回中文。宁可在英文环境里看到一句中文，也不要抛异常
或者显示一个 key。

语言选择顺序：
  1. 环境变量 CLAUDE_MOUSE_LANG=zh|en（单次生效，适合临时切换和测试）
  2. config.json 的 "language"（"zh" / "en" / "auto"）
  3. auto：系统界面语言是中文就用中文，否则英文

为什么不用 gettext：这一层要能在 CLI 进程、测试脚本和自检脚本里零依赖地用，
而且翻译表本身就该是一眼能读完的字典，不是编译产物。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

_CACHE: str | None = None

VALID = ("zh", "en")


def _config_language() -> str:
    try:
        path = Path(__file__).resolve().parent.parent / "config.json"
        return str(json.loads(path.read_text(encoding="utf-8")).get("language", "auto"))
    except Exception:
        return "auto"


def _system_language() -> str:
    """系统界面语言。中文（简繁）返回 zh，其余返回 en。"""
    try:
        import ctypes
        lid = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
        return "zh" if lid == 0x04 else "en"
    except Exception:
        return "zh"


def lang() -> str:
    global _CACHE
    if _CACHE in VALID:
        return _CACHE

    env = (os.environ.get("CLAUDE_MOUSE_LANG") or "").strip().lower()
    if env in VALID:
        _CACHE = env
        return env

    cfg = _config_language().strip().lower()
    _CACHE = cfg if cfg in VALID else _system_language()
    return _CACHE


def set_language(value: str | None) -> None:
    """强制切换（测试用）。传 None 恢复自动判断。"""
    global _CACHE
    v = (value or "").strip().lower()
    _CACHE = v if v in VALID else None


def tr(text: str, **kwargs) -> str:
    """把中文文案换成当前语言的版本；没有翻译就原样返回中文。"""
    out = text if lang() == "zh" else EN.get(text, text)
    if kwargs:
        try:
            return out.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return out
    return out


# --------------------------------------------------------------------------
# 翻译表
#
# 键是代码里的中文原文，逐字一致。新增文案时先在代码里写中文，再到这里补英文；
# 忘了补也不会出错，只是英文环境里会露出中文。
# --------------------------------------------------------------------------

EN: dict[str, str] = {
    # —— 通用 ——
    "没有找到可见的浏览器窗口。": "No visible browser window found.",
    "没有找到可用的浏览器窗口。请先打开 Chrome 或 Edge，并确认窗口没有最小化。":
        "No usable browser window. Open Chrome or Edge and make sure it is not minimized.",
    "窗口当前不可见（最小化或被隐藏），无法扫视。":
        "The window is not visible (minimized or hidden) — cannot look at it.",
    "已允许占用鼠标。": "Mouse control allowed.",
    "已禁止占用鼠标。正在执行的动作会立即中止。":
        "Mouse control disabled. Any action in progress is aborted.",

    # —— 审批门 ——
    ("AI 助手请求执行一个有副作用的操作：\n\n"
     "        {name}\n\n"
     "如果你同意，请把这个确认码告诉它：\n\n"
     "        {code}\n\n"
     "如果你没有要求过这个操作，请点「取消」。\n"
     "（确认码 {ttl} 秒内有效，只能用一次）"):
        "The AI assistant is asking to perform an action with side effects:\n\n"
        "        {name}\n\n"
        "If you approve, tell it this code:\n\n"
        "        {code}\n\n"
        "If you did not ask for this, click Cancel.\n"
        "(the code is valid for {ttl} seconds and can be used once)",
    "desktop-mouse 操作确认": "desktop-mouse — confirm action",
    "（未命名元素）": "(unnamed element)",

    # —— 扫视 ——
    "扫视基线已建立（{cols}×{rows} 网格，覆盖 {w}×{h}）。\n下一次 look 会报告和这一帧的差异。":
        "Baseline captured ({cols}×{rows} grid covering {w}×{h}).\n"
        "The next `look` will report what changed against this frame.",
    "画面没有变化。": "No change on screen.",
    "画面有 {n} 处变化（按显著度排序）：":
        "{n} change(s) on screen, ranked by salience:",
    "大范围变化：{n} 处碎片，整体覆盖 ({l},{t})-({r},{b})。\n通常是一次滚动、跳转或整页重绘，而不是某个控件动了。":
        "Large-area change: {n} fragments spanning ({l},{t})-({r},{b}).\n"
        "Usually a scroll, a navigation or a full repaint rather than one control moving.",

    # —— 批量 ——
    "批量执行：共 {n} 步": "Batch: {n} steps",
    "全部 {n} 步成功。": "All {n} steps succeeded.",
    "停在第 {n} 步：": "Stopped at step {n}:",
    "（没有更多信息）": "(no further detail)",
    "后面的步骤没有执行。修正这一步后重新提交整批即可。":
        "Later steps were skipped. Fix this one and re-submit the batch.",
    "这一步需要你本人确认：对话框里的 4 位码告诉助手后，带上 --consent-code 单独重跑这一步即可。":
        "This step needs the user: read the 4-digit code from the dialog, "
        "then re-run this step alone with --consent-code.",
    "失败": "FAIL",

    # —— 错误消息 ——
    "需要 --to（按文字找落点）或 --to-x/--to-y（按坐标）。":
        "Needs --to (drop target by text) or --to-x/--to-y (by coordinates).",
    "拖拽目标当前没有可用的位置信息。":
        "The drop target currently has no usable position.",
    "找不到页面区域，无法确定滚轮该发到哪里。":
        "Could not find the page area, so there is nowhere to send the wheel.",
    "需要 --text 或 --text-file。": "Needs --text or --text-file.",
    "已急停。": "Emergency stop is in effect.",
    "切换失败。请手动点一下浏览器窗口再重试。":
        "Could not bring the window to the front. Click the browser window yourself and retry.",
    "步骤文件读不出来。需要是一个 JSON 数组。":
        "The steps file could not be read. It must be a JSON array.",
    "[已中断] 用户按下了 Ctrl+C。": "[Interrupted] Ctrl+C was pressed.",
    "没有找到 {name}。": "No match for {name}.",
    "找不到要拖动的元素 {name}。": "Could not find the element to drag: {name}.",    "找不到拖拽目标 {name}。": "Could not find the drop target: {name}.",
    "({x}, {y}) 这一点上盖着别的窗口，不在浏览器里。":
        "({x}, {y}) is covered by another window — it is not over the browser.",
    "[拒绝执行]\n{err}": "[Refused]\n{err}",
    "[需要用户确认]\n{err}": "[Needs your confirmation]\n{err}",
    "[安全拦截] {err}": "[Blocked] {err}",
    "[已中止] {err}": "[Aborted] {err}",
    "[窗口已变化] {err}": "[Window changed] {err}",
    "[系统错误] {err}": "[System error] {err}",

    # —— 安全层 ——
    "配置文件不存在：{path}": "Config file not found: {path}",
    "当前为禁止占用状态。运行 `ctl arm` 或按 Ctrl+Alt+Space 可恢复。":
        "Mouse control is currently disabled. Run `ctl arm` or press Ctrl+Alt+Space to re-enable.",
    "目标窗口不存在或已关闭。": "The target window does not exist or has been closed.",
    "拒绝操作：域名 `{host}` 在禁止名单内。":
        "Refused: the host `{host}` is on the block list.",
    "拒绝操作：域名 `{host}` 不在允许名单内。":
        "Refused: the host `{host}` is not on the allow list.",
    "用户在确认对话框里点了「取消」，操作已放弃。":
        "The user clicked Cancel in the confirmation dialog — action abandoned.",
    "hwnd={hwnd} 不是一个可见的浏览器窗口。":
        "hwnd={hwnd} is not a visible browser window.",

    # —— 底层 ——
    "本 skill 只支持 Windows。": "This skill only supports Windows.",
    "已收到急停信号，动作中止。": "Emergency stop received — action aborted.",
    "虚拟桌面尺寸异常，无法换算坐标。":
        "The virtual desktop size looks wrong; cannot convert coordinates.",
    "GetCursorPos 失败。": "GetCursorPos failed.",
    "不支持的鼠标键：{button}（可用 left / right / middle）":
        "Unsupported mouse button: {button} (use left / right / middle)",
    "无法识别的按键：{name}": "Unknown key: {name}",
    "组合键为空。": "The key combination is empty.",
    "GetWindowRect 失败，窗口可能已关闭。":
        "GetWindowRect failed; the window may have been closed.",
    "GetClientRect 失败。": "GetClientRect failed.",
    "打不开剪贴板（可能被别的程序占着）。":
        "Could not open the clipboard (another program may be holding it).",
    "拖拽起点元素已经不在页面上，动作未开始。":
        "The drag source is no longer on the page — nothing was started.",
    "拖拽过程中目标元素从页面上消失了，已松手。":
        "The drag target disappeared mid-drag; the button was released.",
    "粘贴失败：{err}": "Paste failed: {err}",
    "拿不到屏幕 DC。": "Could not get a screen DC.",
    "创建 DIB 失败。": "Could not create the DIB.",
    "抓屏失败。": "Screen capture failed.",
    "两帧的网格尺寸不一致，不能比较。":
        "The two frames have different grid sizes and cannot be compared.",

    # —— status ——
    "占用鼠标   : {v}": "Mouse control : {v}",
    "允许": "allowed",
    "已禁止": "disabled",
    "急停标志   : {v}": "Stop flag     : {v}",
    "已触发（需 arm 才能恢复）": "triggered (needs arm to clear)",
    "无": "none",
    "浏览器窗口 : {n} 个": "Browser windows: {n}",
    "用户空闲   : {n} 秒": "User idle      : {n}s",
    "热键       : 切换占用 {t} ／ 急停 {p}": "Hotkeys        : toggle {t} / panic {p}",
    "危险操作   : 一律弹窗询问（{n} 个关键词，无法关闭）":
        "Dangerous ops  : always ask, via dialog ({n} keywords, cannot be disabled)",

    # —— 动作结果 ——
    "拖拽 ({x1},{y1}) → ({x2},{y2})": "Drag ({x1},{y1}) → ({x2},{y2})",
    "已把 [{t1}] {n1} 拖到 [{t2}] {n2}，落点 {xy}（伺服 {steps} 步）":
        "Dragged [{t1}] {n1} onto [{t2}] {n2}, landed at {xy} ({steps} steps)",
    "点": "point",
    "滚动 dy={dy} dx={dx}（光标位于 ({x},{y})）":
        "Scrolled dy={dy} dx={dx} (cursor at {x},{y})",
    "粘贴": "paste",
    "逐字符（{wpm:.0f} WPM，预计 {sec:.0f}s）":
        "keystroke ({wpm:.0f} WPM, about {sec:.0f}s)",
    "已{how}输入 {n} 个字符到 [{t}] {name}（内容不记入日志）{warn}":
        "Typed {n} characters into [{t}] {name} via {how} (content not logged){warn}",
    "未知": "unknown",
    "已发送按键 {combo}": "Sent keys {combo}",
    "已出现: [{t}] {name} 中心{center}": "Appeared: [{t}] {name} at {center}",
    "已切到前台: {title}": "Brought to front: {title}",
    " ←前台": " ←foreground",

    # —— 自检 ——
    "  DPI 模式    : {v}": "  DPI mode     : {v}",
    "  虚拟屏幕    : 原点 ({vx},{vy})  尺寸 {vw}×{vh}":
        "  Virtual screen: origin ({vx},{vy})  size {vw}×{vh}",
    "  双击阈值    : {v} ms": "  Double-click : {v} ms",
    "  当前光标    : {v}": "  Cursor       : {v}",
    "  自身已提权  : {v}": "  Elevated     : {v}",
    "  20 次随机移动：最大偏差 {w} px，平均偏差 {a:.2f} px":
        "  20 random moves: max error {w} px, mean error {a:.2f} px",
    "  ✓ 全部通过": "  ✓ all checks passed",
    "  已允许占用  : {v}": "  Armed        : {v}",
    "  急停标志    : {v}": "  Stop flag    : {v}",
    "  用户空闲    : {v:.2f} s": "  User idle    : {v:.2f} s",
    "  (没有找到浏览器窗口 —— 请打开 Chrome 或 Edge 后重跑)":
        "  (no browser window found — open Chrome or Edge and run again)",
    "  UIA 采集：{n} 个节点（{named} 个有名字），耗时 {dt:.3f}s":
        "  UIA: {n} nodes ({named} named) in {dt:.3f}s",
    "  地址栏读出  : {v}": "  Address bar  : {v}",
    "  observe.py 尚未就绪，跳过。": "  observe.py not ready, skipped.",
    "  {mark} 危险词判定 `{probe}` -> {tier}（应为 {expect}）":
        "  {mark} danger word `{probe}` -> {tier} (expected {expect})",
    "  ✓ 审批接口只认确认码（参数：{p}）":
        "  ✓ approval accepts nothing but a consent code (params: {p})",
    "  ✓ 危险操作确认没有关闭开关": "  ✓ dangerous actions cannot be switched off",
    "  guard.py 尚未就绪，跳过。": "  guard.py not ready, skipped.",
    "1. 坐标空间": "1. Coordinate space",
    "2. 坐标精度（不涉及 UIA，纯 SendInput 往返）":
        "2. Coordinate precision (SendInput round-trip, no UIA)",
    "3. 浏览器窗口与 UIA": "3. Browser window and UIA",
    "4. 安全层": "4. Safety layer",
    "结论": "Result",
}
