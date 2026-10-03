"""guardd.py —— 常驻守护进程：全局热键 + 定时清理。

它做三件事：
  1. 监听「允许/禁止占用鼠标」的切换热键
  2. 监听急停热键 —— 立即释放鼠标按键并转为禁止
  3. 定期清理过期截图

用 RegisterHotKey 而不是键盘钩子：不需要管理员权限，也不会拦截按键。
注册的是消息热键，系统会把 WM_HOTKEY 投递到本线程的消息队列。

    python guardd.py          前台运行（Ctrl+C 退出）
    pythonw guardd.py         后台静默运行
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import guard
import winput

WM_HOTKEY = 0x0312
WM_TIMER = 0x0113
WM_QUIT = 0x0012

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

HOTKEY_TOGGLE = 1
HOTKEY_PANIC = 2
TIMER_PRUNE = 3
PRUNE_INTERVAL_MS = 10 * 60 * 1000

PID_PATH = guard.BASE / "daemon.pid"

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# --------------------------------------------------------------------------
# 热键解析
# --------------------------------------------------------------------------

_MODIFIERS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """把 "ctrl+alt+space" 解析成 (modifiers, virtual_key)。"""
    mods = 0
    key = None
    for part in spec.split("+"):
        part = part.strip().lower()
        if not part:
            continue
        if part in _MODIFIERS:
            mods |= _MODIFIERS[part]
        elif key is None:
            key = part
        else:
            raise ValueError(f"热键 {spec!r} 里有多个主键。")
    if key is None:
        raise ValueError(f"热键 {spec!r} 没有主键，例如 ctrl+alt+space。")
    return mods | MOD_NOREPEAT, winput.resolve_vk(key)


# --------------------------------------------------------------------------
# 动作
# --------------------------------------------------------------------------

def do_toggle() -> None:
    armed = guard.is_armed()
    guard.set_armed(not armed)
    state = "允许占用鼠标" if not armed else "禁止占用鼠标"
    speak(f"[{time.strftime('%H:%M:%S')}] 切换 -> {state}")


def do_panic() -> None:
    winput.release_all()
    guard.set_armed(False)
    speak(f"[{time.strftime('%H:%M:%S')}] 急停！鼠标已释放，占用已禁止。"
          "按切换热键或运行 ctl arm 可恢复。")


def speak(message: str) -> None:
    """打日志。pythonw 下没有控制台，写文件兜底。"""
    try:
        print(message, flush=True)
    except Exception:
        pass
    try:
        with (guard.BASE / "log" / "daemon.log").open("a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 热键可用性扫描
# --------------------------------------------------------------------------

CANDIDATES = [
    "ctrl+alt+f9", "ctrl+alt+q", "ctrl+alt+z", "ctrl+alt+x", "ctrl+alt+p",
    "ctrl+alt+0", "ctrl+alt+f12", "ctrl+shift+f9", "ctrl+shift+f12",
    "ctrl+alt+shift+q", "ctrl+alt+shift+z", "ctrl+shift+alt+f12",
    "ctrl+alt+space", "ctrl+alt+a", "ctrl+alt+s",
]


def scan_hotkeys() -> int:
    """列出候选组合里哪些还空闲。

    全局热键是独占的：注册成功了别的程序就用不了，所以挑之前先扫一遍。
    """
    free, taken = [], []
    for i, spec in enumerate(CANDIDATES, start=100):
        try:
            mods, vk = parse_hotkey(spec)
        except ValueError:
            continue
        if user32.RegisterHotKey(None, i, mods, vk):
            free.append(spec)
            user32.UnregisterHotKey(None, i)
        else:
            taken.append((spec, ctypes.get_last_error()))
    free.sort(key=lambda s: (len(s), s))
    print("空闲可用：")
    for s in free:
        print(f"    {s}")
    if taken:
        print("\n已被别的程序占用：")
        for s, err in taken:
            print(f"    {s}    (错误码 {err})")
    print("\n把选中的组合填进 config.json 的 hotkeys，然后重启守护进程。")
    return 0


# --------------------------------------------------------------------------
# 单实例
# --------------------------------------------------------------------------

def already_running() -> int | None:
    try:
        pid = int(PID_PATH.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if winput.process_alive(pid) else None


def write_pid() -> None:
    PID_PATH.write_text(str(kernel32.GetCurrentProcessId()), encoding="utf-8")


def clear_pid() -> None:
    try:
        PID_PATH.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------

def main() -> int:
    if "--scan" in sys.argv or "--list-free" in sys.argv:
        return scan_hotkeys()

    running = already_running()
    if running:
        print(f"守护进程已在运行（pid={running}）。")
        return 0

    cfg = guard.load_config()["hotkeys"]
    try:
        toggle_mods, toggle_key = parse_hotkey(cfg["toggle"])
        panic_mods, panic_key = parse_hotkey(cfg["panic"])
    except ValueError as e:
        print(f"热键配置有误：{e}")
        return 1

    if not user32.RegisterHotKey(None, HOTKEY_TOGGLE, toggle_mods, toggle_key):
        print(f"无法注册热键 {cfg['toggle']!r}（错误码 {ctypes.get_last_error()}）。"
              "多半是被别的程序占用了 —— 运行 `python guardd.py --scan` "
              "看看哪些组合空闲，再改 config.json 里的 hotkeys。")
        return 1
    if not user32.RegisterHotKey(None, HOTKEY_PANIC, panic_mods, panic_key):
        user32.UnregisterHotKey(None, HOTKEY_TOGGLE)
        print(f"无法注册热键 {cfg['panic']!r}（错误码 {ctypes.get_last_error()}）。"
              "同样用 `python guardd.py --scan` 找空闲组合。")
        return 1

    write_pid()
    guard._write_state(guard._read_state())  # 确保 state.json 存在
    user32.SetTimer(None, TIMER_PRUNE, PRUNE_INTERVAL_MS, None)

    speak(f"守护进程已启动（pid={kernel32.GetCurrentProcessId()}）。")
    speak(f"  {cfg['toggle']}  切换允许/禁止占用鼠标  （当前：{'允许' if guard.is_armed() else '禁止'}）")
    speak(f"  {cfg['panic']}  急停")

    msg = wintypes.MSG()
    try:
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            if msg.message == WM_HOTKEY:
                if msg.wParam == HOTKEY_TOGGLE:
                    do_toggle()
                elif msg.wParam == HOTKEY_PANIC:
                    do_panic()
            elif msg.message == WM_TIMER and msg.wParam == TIMER_PRUNE:
                removed = guard.prune_screenshots()
                if removed:
                    speak(f"清理了 {removed} 张过期截图。")
            else:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
    except KeyboardInterrupt:
        pass
    finally:
        user32.UnregisterHotKey(None, HOTKEY_TOGGLE)
        user32.UnregisterHotKey(None, HOTKEY_PANIC)
        user32.KillTimer(None, TIMER_PRUNE)
        clear_pid()
        speak("守护进程已退出。热键不再生效。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
