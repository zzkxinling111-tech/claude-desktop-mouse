"""机械层 —— Win32 输入与窗口操作的 ctypes 封装。

只做"把指令发给系统"这件事，不含任何人类化逻辑（那在 humanize.py）。

必须处理的 Windows 陷阱：
  1. DPI 感知必须在本模块导入时立刻设置，早于任何窗口 API 调用
  2. 绝对坐标用 VIRTUALDESK 归一化，否则副屏（负坐标）会算错
  3. SetForegroundWindow 有抢焦点限制，需要 AttachThreadInput 配合
  4. 提权进程无法被非提权进程注入输入（UIPI），必须提前检测并拒绝
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

IS_WINDOWS = os.name == "nt"

if not IS_WINDOWS:
    raise RuntimeError("本 skill 只支持 Windows。")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

# 鼠标事件标志
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

# 键盘事件标志
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# 虚拟屏幕尺寸（多显示器合计）
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# 窗口状态
SW_RESTORE = 9

# 令牌信息
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class Aborted(RuntimeError):
    """用户主动中止（急停热键或 disarm）。"""


# --------------------------------------------------------------------------
# 第一步：DPI 感知 —— 必须最先执行
# --------------------------------------------------------------------------

def _enable_dpi_awareness() -> str:
    """把本进程标记为 DPI 感知。

    不做这件事的话，系统会对坐标做缩放补偿，125%/150% 缩放下点哪都偏。
    """
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        # PER_MONITOR_AWARE_V2 == (DPI_AWARENESS_CONTEXT)-4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass

    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return "per-monitor"
    except (AttributeError, OSError):
        pass

    try:
        user32.SetProcessDPIAware()
        return "system"
    except (AttributeError, OSError):
        return "none"


DPI_MODE = _enable_dpi_awareness()


# --------------------------------------------------------------------------
# 结构体
# --------------------------------------------------------------------------

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


# --------------------------------------------------------------------------
# 中止机制
# --------------------------------------------------------------------------

_abort_check = None  # 由 guard 注入；返回 True 表示应立即停止


def set_abort_check(fn) -> None:
    """注入中止检查函数。humanize 会在每个轨迹采样点之间调用它。"""
    global _abort_check
    _abort_check = fn


def check_abort() -> None:
    if _abort_check is not None and _abort_check():
        release_all()
        raise Aborted("已收到急停信号，动作中止。")


def release_all() -> None:
    """无条件释放所有鼠标按键。

    急停时调用。即使按键本来就没按下，多发一次抬起也是安全的。
    """
    for flag in (MOUSEEVENTF_LEFTUP, MOUSEEVENTF_RIGHTUP, MOUSEEVENTF_MIDDLEUP):
        try:
            _send(_mouse_input(0, 0, flag))
        except Exception:
            pass


# --------------------------------------------------------------------------
# 底层发送
# --------------------------------------------------------------------------

def _mouse_input(dx: int, dy: int, flags: int, data: int = 0) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, 0)
    return inp


def _key_input(vk: int, scan: int, flags: int) -> INPUT:
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return inp


def _send(*inputs: INPUT) -> None:
    n = len(inputs)
    if n == 0:
        return
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        err = ctypes.get_last_error()
        raise OSError(
            f"SendInput 只成功 {sent}/{n}（错误码 {err}）。"
            "常见原因：桌面被锁、目标窗口以管理员权限运行、或前台是 UAC 安全桌面。"
        )


# --------------------------------------------------------------------------
# 坐标换算
# --------------------------------------------------------------------------

def virtual_screen() -> tuple[int, int, int, int]:
    """返回虚拟桌面（全部显示器）的 (left, top, width, height)。

    副屏在主屏左侧或上方时，left/top 为负。
    """
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def to_absolute(x: int, y: int) -> tuple[int, int]:
    """屏幕坐标 → SendInput 要求的 0..65535 归一化坐标。"""
    vx, vy, vw, vh = virtual_screen()
    if vw <= 1 or vh <= 1:
        raise OSError("虚拟桌面尺寸异常，无法换算坐标。")
    nx = int(round((x - vx) * 65535 / (vw - 1)))
    ny = int(round((y - vy) * 65535 / (vh - 1)))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(pt)):
        raise OSError("GetCursorPos 失败。")
    return pt.x, pt.y


# --------------------------------------------------------------------------
# 鼠标原语
# --------------------------------------------------------------------------

def send_move(x: int, y: int) -> None:
    """把光标瞬间移到 (x, y)。人类化由 humanize 负责。"""
    nx, ny = to_absolute(x, y)
    _send(_mouse_input(nx, ny, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK))


_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def send_button(button: str, down: bool) -> None:
    try:
        down_flag, up_flag = _BUTTON_FLAGS[button]
    except KeyError:
        raise ValueError(f"不支持的鼠标键：{button}（可用 left / right / middle）")
    _send(_mouse_input(0, 0, down_flag if down else up_flag))


def send_wheel(delta: int, horizontal: bool = False) -> None:
    """delta > 0 向上/向左滚，负值反向。一格 = 120。"""
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    _send(_mouse_input(0, 0, flag, delta & 0xFFFFFFFF))


def double_click_time_ms() -> int:
    """系统双击判定阈值。两次点击间隔必须小于它。"""
    return int(user32.GetDoubleClickTime()) or 500


# --------------------------------------------------------------------------
# 键盘原语
# --------------------------------------------------------------------------

VK_CODES = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "pause": 0x13, "capslock": 0x14,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "win": 0x5B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def _resolve_vk(name: str) -> int:
    key = name.strip().lower()
    if key in VK_CODES:
        return VK_CODES[key]
    if len(key) == 1:
        # VkKeyScanW 会把字符映射到当前键盘布局的虚拟键
        res = user32.VkKeyScanW(ord(key))
        if res != -1:
            return res & 0xFF
    raise ValueError(f"无法识别的按键：{name}")


# 公开别名：守护进程注册热键时要用
resolve_vk = _resolve_vk


def send_combo(combo: str) -> None:
    """发送组合键，例如 "ctrl+l"、"ctrl+shift+t"、"enter"。"""
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError("组合键为空。")

    vks = [_resolve_vk(p) for p in parts]
    # 先全部按下，再逆序抬起 —— 和真实键盘行为一致
    for vk in vks:
        _send(_key_input(vk, 0, 0))
    for vk in reversed(vks):
        _send(_key_input(vk, 0, KEYEVENTF_KEYUP))


def send_text_unicode(text: str) -> None:
    """逐字符发送 Unicode 文本。

    用 KEYEVENTF_UNICODE 绕开键盘布局，中文和符号都能正确输入。
    逐字符之间的间隔由 humanize 控制，这里只负责发一个字符。
    """
    data = text.encode("utf-16-le")
    for i in range(0, len(data), 2):
        unit = int.from_bytes(data[i:i + 2], "little")
        _send(
            _key_input(0, unit, KEYEVENTF_UNICODE),
            _key_input(0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        )


# --------------------------------------------------------------------------
# 窗口操作
# --------------------------------------------------------------------------

def window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def process_name(pid: int) -> str:
    """取进程的可执行文件名（小写）。

    比窗口标题可靠得多 —— 标题会被网页改，进程名不会。
    """
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        kernel32.CloseHandle(h)


def process_alive(pid: int) -> bool:
    """进程是否还活着。用于判断守护进程是否在跑。"""
    if not pid or pid <= 0:
        return False
    SYNCHRONIZE = 0x00100000
    h = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not h:
        return False
    try:
        return kernel32.WaitForSingleObject(h, 0) == 0x00000102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(h)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """窗口外框在屏幕上的 (left, top, right, bottom)。"""
    r = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        raise OSError("GetWindowRect 失败，窗口可能已关闭。")
    return r.left, r.top, r.right, r.bottom


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """客户区（去掉标题栏和边框）在屏幕上的 (left, top, right, bottom)。"""
    r = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(r)):
        raise OSError("GetClientRect 失败。")
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, pt.x + r.right, pt.y + r.bottom


def dpi_for_window(hwnd: int) -> int:
    try:
        return int(user32.GetDpiForWindow(hwnd)) or 96
    except (AttributeError, OSError):
        return 96


def focus_window(hwnd: int) -> bool:
    """把窗口切到前台并确保真的抢到了焦点。

    单独调用 SetForegroundWindow 通常会被系统拒绝（后台进程不允许抢焦点），
    所以先用 AttachThreadInput 把当前线程和目标线程的输入队列接在一起。
    """
    if not is_window(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)

    if user32.GetForegroundWindow() == hwnd:
        return True

    cur_thread = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tgt_thread = user32.GetWindowThreadProcessId(hwnd, None)

    attached = []
    try:
        for t in (fg_thread, tgt_thread):
            if t and t != cur_thread and user32.AttachThreadInput(cur_thread, t, True):
                attached.append(t)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for t in attached:
            user32.AttachThreadInput(cur_thread, t, False)

    return user32.GetForegroundWindow() == hwnd


def foreground_window() -> int:
    return int(user32.GetForegroundWindow())


def is_elevated(pid: int) -> bool | None:
    """目标进程是否以管理员权限运行。

    返回 None 表示查不到（无权限打开进程）。提权进程无法被普通进程注入输入，
    所以查到 True 就必须拒绝操作，而不是硬试。
    """
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            class TOKEN_ELEVATION_INFO(ctypes.Structure):
                _fields_ = [("TokenIsElevated", wintypes.DWORD)]

            info = TOKEN_ELEVATION_INFO()
            size = wintypes.DWORD()
            ok = advapi32.GetTokenInformation(
                token, TOKEN_ELEVATION, ctypes.byref(info),
                ctypes.sizeof(info), ctypes.byref(size),
            )
            return bool(info.TokenIsElevated) if ok else None
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(h)


def user_idle_seconds() -> float:
    """用户距上次操作键鼠过了多少秒。

    用来检测"用户正在用电脑"，此时应当暂停自动化。
    """
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0
    now = kernel32.GetTickCount64()
    return max(0.0, (now - int(lii.dwTime)) / 1000.0)


# --------------------------------------------------------------------------
# 剪贴板
# --------------------------------------------------------------------------
#
# 放在这一层是因为它属于操作系统原语，感知层（取页面正文）和手势层
# （粘贴输入）都要用，放在任何一边都会绕成循环依赖。

def clipboard_get_text() -> str:
    import win32clipboard as cb
    import win32con

    for _ in range(3):
        try:
            cb.OpenClipboard()
            break
        except Exception:
            time.sleep(0.1)
    else:
        return ""
    try:
        if cb.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return cb.GetClipboardData(win32con.CF_UNICODETEXT) or ""
        return ""
    except Exception:
        return ""
    finally:
        try:
            cb.CloseClipboard()
        except Exception:
            pass


def clipboard_snapshot() -> dict:
    """备份剪贴板的文本内容。

    只备份文本格式 —— 如果剪贴板里是图片等其它格式就还原不了。这种情况会被
    标在返回值里，由调用方决定要不要继续。
    """
    import win32clipboard as cb
    import win32con

    snap: dict = {"text": None, "has_other": False}
    try:
        cb.OpenClipboard()
    except Exception:
        return snap
    try:
        formats = []
        fmt = 0
        while True:
            fmt = cb.EnumClipboardFormats(fmt)
            if fmt == 0:
                break
            formats.append(fmt)
        snap["has_other"] = any(
            f not in (win32con.CF_UNICODETEXT, win32con.CF_TEXT) for f in formats
        )
        if cb.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            snap["text"] = cb.GetClipboardData(win32con.CF_UNICODETEXT)
    except Exception:
        pass
    finally:
        try:
            cb.CloseClipboard()
        except Exception:
            pass
    return snap


def clipboard_set_text(text: str) -> None:
    import win32clipboard as cb
    import win32con

    for _ in range(3):
        try:
            cb.OpenClipboard()
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise OSError("打不开剪贴板（可能被别的程序占着）。")
    try:
        cb.EmptyClipboard()
        cb.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        try:
            cb.CloseClipboard()
        except Exception:
            pass


def clipboard_restore(snap: dict) -> None:
    """把剪贴板还原成快照的样子。非文本格式还原不了。"""
    import win32clipboard as cb
    import win32con

    try:
        cb.OpenClipboard()
    except Exception:
        return
    try:
        cb.EmptyClipboard()
        if snap.get("text"):
            cb.SetClipboardData(win32con.CF_UNICODETEXT, snap["text"])
    except Exception:
        pass
    finally:
        try:
            cb.CloseClipboard()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 诊断
# --------------------------------------------------------------------------

def diagnostics() -> dict:
    """给排查用的一份环境快照。"""
    vx, vy, vw, vh = virtual_screen()
    return {
        "dpi_mode": DPI_MODE,
        "virtual_screen": {"left": vx, "top": vy, "width": vw, "height": vh},
        "cursor": cursor_pos(),
        "double_click_ms": double_click_time_ms(),
        "idle_seconds": round(user_idle_seconds(), 2),
        "self_elevated": is_elevated(kernel32.GetCurrentProcessId()),
    }


if __name__ == "__main__":
    import json

    print(json.dumps(diagnostics(), ensure_ascii=False, indent=2))
    print("\n3 秒后做一次原地小幅移动测试（不会点击任何东西）…")
    time.sleep(3)
    x0, y0 = cursor_pos()
    for dx in (20, 40, 60, 40, 20, 0):
        send_move(x0 + dx, y0)
        time.sleep(0.02)
    print("移动测试完成，光标已复位。")
