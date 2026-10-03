"""感知层 —— 让模型"看见"浏览器窗口里有什么。

三条获取信息的途径，按性价比排序：

  1. UIA 无障碍树 —— 精确、便宜（200 个元素约 30ms）、带像素坐标
  2. UIA TextPattern —— 直接取页面正文，不碰剪贴板
  3. 截图 —— 通用但昂贵，UIA 给不出答案时才用

已知代价：查询 Chrome 的 UIA 会让它启用无障碍树，而"无障碍被启用"本身
是一种可被部分网站检测到的指纹。对高敏感站点应改用纯截图模式。
"""

from __future__ import annotations

import time
from pathlib import Path

import win32con
import win32gui

import humanize
import winput

# --------------------------------------------------------------------------
# UIA 初始化
# --------------------------------------------------------------------------

import comtypes
import comtypes.client

try:
    comtypes.CoInitialize()
except OSError:
    pass  # 本线程已初始化过，忽略

comtypes.client.GetModule("UIAutomationCore.dll")
from comtypes.gen import UIAutomationClient as UIA  # noqa: E402

_uia = None

# 控件类型 ID -> 可读名字
CONTROL_TYPES: dict[int, str] = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
}

# 元素类型的「可操作性」优先级，数字越小越可能真的是要操作的目标。
#
# 这个排序不是锦上添花，是必需的：页面上经常有一段说明文字和一个输入框
# 名字完全相同（比如 GitHub 表单里的 "Description"），只有靠类型才分得清
# 该点哪个。实测踩过这个坑 —— 点到了标签，没点到输入框。
#
#   0  直接可操作，优先命中
#   1  列表项，通常可点
#   2  滚动条一类的辅助控件
#   3  未知类型，兜底
#   4  结构性容器，基本不该被当作操作目标
#   5  文档本体
#   6  纯展示内容，排最后
TYPE_PRIORITY = {
    "Button": 0, "Hyperlink": 0, "Edit": 0, "ComboBox": 0,
    "CheckBox": 0, "RadioButton": 0, "SplitButton": 0, "MenuItem": 0,
    "TabItem": 1, "ListItem": 1, "TreeItem": 1, "DataItem": 1, "Slider": 1,
    "Thumb": 2, "ScrollBar": 2,
    "ToolBar": 4, "TitleBar": 4, "Group": 4, "Pane": 4, "Header": 4,
    "Document": 5,
    "Text": 6, "Image": 6, "Separator": 6, "ProgressBar": 6,
}
_DEFAULT_PRIORITY = 3

# 只有这些类型才适合作为「输入文本」的目标
TEXT_INPUT_TYPES = {"Edit", "ComboBox", "Document"}

MAX_TREE_DEPTH = 20


def uia():
    """惰性创建 UIA 根对象。"""
    global _uia
    if _uia is None:
        _uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
    return _uia


# --------------------------------------------------------------------------
# 窗口
# --------------------------------------------------------------------------

def list_browser_windows() -> list[dict]:
    """列出所有可见的浏览器窗口，按 z 序（最上面的排最前）。"""
    import guard

    cfg = guard.load_config()["whitelist"]
    classes = set(cfg["window_classes"])
    procs = set(cfg["browser_processes"])
    out: list[dict] = []

    def cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.GetParent(hwnd) != 0:
                return True
            if win32gui.GetClassName(hwnd) not in classes:
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
                return True

            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            # 最小化到托盘时窗口会被挪到 (-32000, -32000) 附近
            if left < -10000 or top < -10000:
                return True
            if right - left < 200 or bottom - top < 200:
                return True

            pid = winput.window_pid(hwnd)
            proc = winput.process_name(pid)
            if proc not in procs:
                return True

            out.append({
                "hwnd": int(hwnd),
                "title": title,
                "class": win32gui.GetClassName(hwnd),
                "process": proc,
                "pid": pid,
                "rect": (left, top, right, bottom),
                "foreground": int(win32gui.GetForegroundWindow()) == int(hwnd),
            })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def pick_window(hwnd: int | None = None) -> dict:
    """决定要操作哪个窗口。

    优先级：显式指定 > 当前前台浏览器 > z 序最靠前的浏览器窗口
    """
    import guard

    windows = list_browser_windows()
    if not windows:
        raise guard.GuardError(
            "没有找到可用的浏览器窗口。请先打开 Chrome 或 Edge，"
            "并确认窗口没有最小化。"
        )

    if hwnd:
        for w in windows:
            if w["hwnd"] == int(hwnd):
                return w
        raise guard.GuardError(f"hwnd={hwnd} 不是一个可见的浏览器窗口。")

    for w in windows:
        if w["foreground"]:
            return w
    return windows[0]


# --------------------------------------------------------------------------
# UIA 树
# --------------------------------------------------------------------------

def _node(el, depth: int) -> dict | None:
    """把一个 UIA 元素压成普通 dict。任何一步失败都返回 None。"""
    try:
        r = el.CurrentBoundingRectangle
        return {
            "depth": depth,
            "name": el.CurrentName or "",
            "type_id": el.CurrentControlType,
            "type": CONTROL_TYPES.get(el.CurrentControlType, str(el.CurrentControlType)),
            "rect": (r.left, r.top, r.right, r.bottom),
            "offscreen": bool(el.CurrentIsOffscreen),
            "enabled": bool(el.CurrentIsEnabled),
            "password": bool(el.CurrentIsPassword),
            "_el": el,
        }
    except Exception:
        return None


def collect_nodes(hwnd: int, *, max_nodes: int = 4000) -> list[dict]:
    """把整个 UIA 树压平成节点列表。

    用逐层 FindAll(Children) 而不是 ControlViewWalker —— 后者在 comtypes 下
    递归到第 3~4 层就会抛"无效指针"，是元素生命周期的问题。
    """
    root = uia().ElementFromHandle(hwnd)
    nodes: list[dict] = []
    cond = uia().ControlViewCondition

    def rec(el, depth: int) -> None:
        if depth > MAX_TREE_DEPTH or len(nodes) >= max_nodes:
            return
        try:
            kids = el.FindAll(UIA.TreeScope_Children, cond)
        except Exception:
            return
        for i in range(kids.Length):
            if len(nodes) >= max_nodes:
                return
            try:
                child = kids.GetElement(i)
            except Exception:
                continue
            n = _node(child, depth)
            if n is not None:
                nodes.append(n)
            rec(child, depth + 1)

    rec(root, 0)
    return nodes


def format_nodes(nodes: list[dict], *, include_offscreen: bool = False) -> str:
    """把节点列表格式化成给模型看的紧凑文本。

    只保留有名字且在屏幕内的元素 —— 这才是"人眼能看见的东西"。
    """
    lines = []
    for n in nodes:
        if not include_offscreen and n["offscreen"]:
            continue
        if not n["name"].strip():
            continue
        l, t, r, b = n["rect"]
        if r - l <= 0 or b - t <= 0:
            continue
        cx, cy = (l + r) // 2, (t + b) // 2
        flags = "" if n["enabled"] else " [禁用]"
        if n["password"]:
            flags += " [密码框]"
        lines.append(
            f"{'  ' * n['depth']}[{n['type']}] {n['name'][:60]!r} "
            f"中心({cx},{cy}) 尺寸{r-l}×{b-t}{flags}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 查找元素
# --------------------------------------------------------------------------

def find_elements(
    hwnd: int,
    text: str,
    *,
    max_nodes: int = 4000,
    onscreen_only: bool = True,
    intent: str = "click",
) -> list[dict]:
    """按可见文字查找元素，最匹配的排在最前。

    排序依据（从重要到次要）：
      1. 完全匹配 > 包含匹配
      2. 元素类型是否真的可操作 —— 见 TYPE_PRIORITY 的说明
      3. 名字更短（更具体）
      4. 面积更小（更精确）

    `intent` 决定第 2 步怎么排：
      - "click"：按钮、链接这类可点控件优先
      - "type" ：只有输入框、下拉框、文档本体才可能入选，其余全部降级
    """
    query = text.strip().lower()
    if not query:
        return []

    scored = []
    for n in collect_nodes(hwnd, max_nodes=max_nodes):
        if onscreen_only and (n["offscreen"] or not n["enabled"]):
            continue
        name = n["name"].strip()
        if not name:
            continue
        l, t, r, b = n["rect"]
        if r - l <= 0 or b - t <= 0:
            continue

        lowered = name.lower()
        if lowered == query:
            tier = 0
        elif query in lowered:
            tier = 1
        else:
            continue

        priority = TYPE_PRIORITY.get(n["type"], _DEFAULT_PRIORITY)
        if intent == "type" and n["type"] not in TEXT_INPUT_TYPES:
            priority = max(priority, 5)

        area = (r - l) * (b - t)
        scored.append((
            (tier, priority, len(name), area),
            {**{k: v for k, v in n.items() if k != "_el"},
             "center": ((l + r) // 2, (t + b) // 2)},
        ))

    scored.sort(key=lambda pair: pair[0])
    return [item for _, item in scored]


def element_from_point(x: int, y: int) -> dict | None:
    """查光标位置下面压着哪个元素。用于点击前后的校验。"""
    try:
        pt = winput.wintypes.POINT(int(x), int(y))
        el = uia().ElementFromPoint(pt)
        return _node(el, 0)
    except Exception:
        return None


def focused_element() -> dict | None:
    """查当前拥有键盘焦点的元素。

    `type` 命令靠它判断目标是不是密码框。
    """
    try:
        return _node(uia().GetFocusedElement(), 0)
    except Exception:
        return None


# --------------------------------------------------------------------------
# 地址栏
# --------------------------------------------------------------------------

def read_url(hwnd: int, *, search_depth: int = 900) -> str | None:
    """读地址栏里的网址。

    做法：找位于窗口顶部、控件类型是 Edit 的元素，用 ValuePattern 取它的值。
    """
    try:
        win_top = win32gui.GetWindowRect(hwnd)[1]
        win_height = win32gui.GetWindowRect(hwnd)[3] - win_top
        band = min(160, max(60, win_height // 10))
    except Exception:
        return None

    for n in collect_nodes(hwnd, max_nodes=search_depth):
        if n["type"] != "Edit":
            continue
        if n["rect"][1] - win_top > band:
            continue
        el = n.get("_el")
        if el is None:
            continue
        try:
            raw = el.GetCurrentPattern(UIA.UIA_ValuePatternId)
            vp = raw.QueryInterface(UIA.IUIAutomationValuePattern)
            value = (vp.CurrentValue or "").strip()
            if value:
                return value
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------
# 页面正文
# --------------------------------------------------------------------------

def _document_node(nodes: list[dict]) -> dict | None:
    for n in nodes:
        if n["type"] == "Document":
            return n
    return None


def read_page_text_uia(hwnd: int, *, max_nodes: int = 4000) -> str | None:
    """用 UIA TextPattern 直接读正文。不碰剪贴板，也不改变页面状态。"""
    doc = _document_node(collect_nodes(hwnd, max_nodes=max_nodes))
    if doc is None or doc.get("_el") is None:
        return None
    try:
        raw = doc["_el"].GetCurrentPattern(UIA.UIA_TextPatternId)
        tp = raw.QueryInterface(UIA.IUIAutomationTextPattern)
        return tp.DocumentRange.GetText(-1) or None
    except Exception:
        return None


# 剪贴板原语已经下沉到 winput —— 手势层的「粘贴输入」也要用它，
# 留在这一层会绕成循环依赖（humanize 反过来被 observe 导入）。
_clipboard_get_text = winput.clipboard_get_text
_clipboard_snapshot = winput.clipboard_snapshot
_clipboard_restore = winput.clipboard_restore


def read_page_text_clipboard(hwnd: int, *, timeout: float = 2.5) -> tuple[str, bool]:
    """退路：全选 + 复制，读剪贴板，再还原。

    返回 (正文, 剪贴板是否有无法还原的内容)。

    注意两个副作用：
      - 会短暂覆盖剪贴板（之后尽力还原，但非文本格式无法还原）
      - 会在页面上留下全选状态，通常再点一下就恢复
    """
    snap = _clipboard_snapshot()
    try:
        humanize.press("ctrl+a")
        time.sleep(0.08)
        humanize.press("ctrl+c")

        deadline = time.time() + timeout
        text = ""
        while time.time() < deadline:
            text = _clipboard_get_text()
            if text:
                break
            time.sleep(0.06)
        return text, bool(snap.get("has_other"))
    finally:
        _clipboard_restore(snap)


def read_page_text(hwnd: int, *, method: str = "auto") -> dict:
    """取页面正文。

    `method`: auto（先 UIA 后剪贴板）/ uia / clipboard
    """
    if method in ("auto", "uia"):
        text = read_page_text_uia(hwnd)
        if text and len(text.strip()) > 40:
            return {"text": text, "method": "uia", "clipboard_touched": False}

    if method == "uia":
        return {"text": "", "method": "uia", "clipboard_touched": False,
                "note": "UIA TextPattern 拿不到正文。"}

    text, lost = read_page_text_clipboard(hwnd)
    note = ""
    if lost:
        note = "剪贴板里有非文本内容，已被覆盖且无法还原。"
    return {"text": text, "method": "clipboard", "clipboard_touched": True, "note": note}


# --------------------------------------------------------------------------
# 截图
# --------------------------------------------------------------------------

def capture_region(left: int, top: int, right: int, bottom: int):
    """只抓指定屏幕区域。

    用 BitBlt 逐区域抓，而不是 PIL.ImageGrab —— 后者会先把整个屏幕抓进内存
    再裁剪。这里从源头上就不去读目标区域以外的像素。

    前提：窗口必须真的显示在屏幕上。被遮挡的部分会拍到遮挡它的东西。
    """
    import win32ui
    from PIL import Image

    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        raise ValueError(f"截图区域无效：{width}×{height}")

    desktop = win32gui.GetDesktopWindow()
    desktop_dc = win32gui.GetWindowDC(desktop)
    src_dc = None
    mem_dc = None
    bmp = None
    try:
        src_dc = win32ui.CreateDCFromHandle(desktop_dc)
        mem_dc = src_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(src_dc, width, height)
        mem_dc.SelectObject(bmp)
        # CAPTUREBLT 让分层窗口（部分浏览器 UI）也能被正确抓到
        mem_dc.BitBlt((0, 0), (width, height), src_dc, (left, top),
                      win32con.SRCCOPY | 0x40000000)
        info = bmp.GetInfo()
        data = bmp.GetBitmapBits(True)
        return Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), data, "raw", "BGRX", 0, 1
        )
    finally:
        if bmp is not None:
            try:
                win32gui.DeleteObject(bmp.GetHandle())
            except Exception:
                pass
        if mem_dc is not None:
            try:
                mem_dc.DeleteDC()
            except Exception:
                pass
        if src_dc is not None:
            try:
                src_dc.DeleteDC()
            except Exception:
                pass
        win32gui.ReleaseDC(desktop, desktop_dc)


def screenshot(hwnd: int, *, out_dir: Path, tag: str = "shot") -> dict:
    """截取窗口客户区并存成 PNG。"""
    from datetime import datetime

    import guard

    winput.focus_window(hwnd)
    time.sleep(0.12)

    left, top, right, bottom = winput.client_rect(hwnd)
    vx, vy, vw, vh = winput.virtual_screen()
    # 夹到可见范围内，否则 BitBlt 会越界
    left, top = max(left, vx), max(top, vy)
    right, bottom = min(right, vx + vw), min(bottom, vy + vh)

    img = capture_region(left, top, right, bottom)

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{tag}-{datetime.now():%H%M%S-%f}.png"
    img.save(path, "PNG")

    return {
        "path": str(path),
        "size": img.size,
        "region": [left, top, right, bottom],
        "note": "只截取了目标窗口的客户区。被其他窗口遮挡的部分拍到的是遮挡物。",
    }


# --------------------------------------------------------------------------
# 汇总
# --------------------------------------------------------------------------

def observe(hwnd: int | None = None, *, max_nodes: int = 4000) -> dict:
    """一次拿齐：窗口信息、网址、UIA 树文本。"""
    import guard

    win = pick_window(hwnd)
    guard.assert_window_allowed(win["hwnd"])

    nodes = collect_nodes(win["hwnd"], max_nodes=max_nodes)
    return {
        "window": {k: v for k, v in win.items() if k != "rect"},
        "rect": win["rect"],
        "url": read_url(win["hwnd"]),
        "node_count": len(nodes),
        "tree": format_nodes(nodes),
    }


if __name__ == "__main__":
    import json

    wins = list_browser_windows()
    print(f"找到 {len(wins)} 个浏览器窗口")
    for w in wins:
        print(f"  hwnd={w['hwnd']} {w['process']} {w['title'][:60]!r}")

    if wins:
        hwnd = wins[0]["hwnd"]
        t0 = time.perf_counter()
        nodes = collect_nodes(hwnd)
        print(f"\n采集 {len(nodes)} 个节点，耗时 {time.perf_counter()-t0:.3f}s")
        print("地址栏:", read_url(hwnd))
        print("\n树的前 25 行：")
        for ln in format_nodes(nodes).splitlines()[:25]:
            print(" ", ln)
