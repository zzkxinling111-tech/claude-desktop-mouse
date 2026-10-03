"""安全层 —— 状态门、白名单、审批门、审计日志。

这一层的原则：**判断由代码做，不靠模型自觉。**

  - 状态门：被禁止时直接抛错，任何动作都不执行
  - 白名单：只允许浏览器进程和指定窗口类，其余一律拒绝
  - 审批门：危险词命中时由脚本拒绝，而不是"提醒模型注意"
  - 审计日志：只记动作，绝不记输入文本、剪贴板内容、截图内容
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime
from pathlib import Path

import winput

BASE = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
STOP_PATH = BASE / "STOP"
CONSENT_PATH = BASE / "consent.json"
LOG_DIR = BASE / "log"


class GuardError(RuntimeError):
    """被安全规则拒绝。调用方应把这个信息原样报告给用户。"""


class NeedsConsent(RuntimeError):
    """命中了需要用户同意的操作，但未获得同意。"""


class Refused(RuntimeError):
    """命中了绝不允许自动执行的操作（例如涉及付款）。"""


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

_config_cache: dict = {"mtime": None, "data": None}


def load_config() -> dict:
    """读取 config.json。按 mtime 缓存，改文件后立即生效。"""
    try:
        mtime = CONFIG_PATH.stat().st_mtime_ns
    except FileNotFoundError:
        raise GuardError(f"配置文件不存在：{CONFIG_PATH}")

    if _config_cache["mtime"] != mtime:
        with CONFIG_PATH.open(encoding="utf-8") as fh:
            _config_cache["data"] = json.load(fh)
        _config_cache["mtime"] = mtime
    return _config_cache["data"]


# --------------------------------------------------------------------------
# 状态门
# --------------------------------------------------------------------------
#
# 两种"别动"信号，用同一个 STOP 文件表达：
#   - disarm（长期禁止）：创建 STOP，直到重新 arm
#   - panic（急停）：创建 STOP 并中止当前动作，同样需要重新 arm 才能恢复
#
# 用文件存在性做判断，是因为它能在轨迹的每个采样点之间被廉价地反复检查。

def _read_state() -> dict:
    try:
        with STATE_PATH.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        # 首次运行：默认为允许。守护进程和 CLI 都会在启动时把它写出来。
        return {"armed": True, "since": None, "actions_this_session": 0}


def _write_state(state: dict) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_armed() -> bool:
    if STOP_PATH.exists():
        return False
    return bool(_read_state().get("armed", True))


def set_armed(armed: bool) -> dict:
    """切换允许/禁止。禁止时同时创建 STOP 文件，让正在跑的动作立刻中断。"""
    state = _read_state()
    state["armed"] = bool(armed)
    state["since"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if armed:
        state["actions_this_session"] = 0
        clear_stop()
    else:
        request_stop()
    _write_state(state)

    # 状态变了就撤掉 armed 缓存，让 abort_check 立刻看到
    _arm_cache["mtime"] = None
    return state


def stop_requested() -> bool:
    return STOP_PATH.exists()


def request_stop() -> None:
    STOP_PATH.touch(exist_ok=True)


def clear_stop() -> None:
    try:
        STOP_PATH.unlink()
    except FileNotFoundError:
        pass


def bump_action_count() -> int:
    state = _read_state()
    state["actions_this_session"] = int(state.get("actions_this_session", 0)) + 1
    _write_state(state)
    return state["actions_this_session"]


# --------------------------------------------------------------------------
# 中止检查（会被 humanize 在每个采样点之间调用，必须廉价）
# --------------------------------------------------------------------------

_arm_cache: dict = {"mtime": None, "armed": True}
_deadline: float | None = None
_corner_since: float | None = None

CORNER_PX = 3
CORNER_DWELL = 0.35


def set_deadline(seconds: float) -> None:
    """给当前动作设一个硬性时间上限，防止跑飞。"""
    global _deadline
    _deadline = time.monotonic() + max(1.0, seconds)


def deadline_exceeded() -> bool:
    return _deadline is not None and time.monotonic() > _deadline


def corner_failsafe() -> bool:
    """把鼠标甩到屏幕左上角即中止 —— 这是"事情不对我要停下来"的本能动作。

    要求光标在极左上角**停留**一小会儿才算数，这样正常移动路径偶尔掠过那里
    不会误触发。代价是真的想点左上角那个像素时会被拦住，
    需要的话把 config 里的 safety.failsafe_corner 关掉。
    """
    global _corner_since

    if not load_config()["safety"].get("failsafe_corner", True):
        return False

    try:
        x, y = winput.cursor_pos()
    except OSError:
        return False

    if x <= CORNER_PX and y <= CORNER_PX:
        now = time.monotonic()
        if _corner_since is None:
            _corner_since = now
            return False
        return (now - _corner_since) >= CORNER_DWELL

    _corner_since = None
    return False


def abort_check() -> bool:
    """返回 True 表示应立即停止。

    每个轨迹采样点都会调一次，所以只做文件 stat 和一次 GetCursorPos，
    不做 JSON 解析。
    """
    if STOP_PATH.exists():
        return True
    if deadline_exceeded():
        return True
    if corner_failsafe():
        return True
    try:
        mtime = STATE_PATH.stat().st_mtime_ns
    except FileNotFoundError:
        return False
    if _arm_cache["mtime"] != mtime:
        _arm_cache["mtime"] = mtime
        _arm_cache["armed"] = is_armed()
    return not _arm_cache["armed"]


def install() -> None:
    """把中止检查装进 winput。所有入口脚本都应先调用它。"""
    winput.set_abort_check(abort_check)


# --------------------------------------------------------------------------
# 动作前置检查
# --------------------------------------------------------------------------

def assert_armed() -> None:
    if stop_requested():
        raise GuardError(
            "已急停或被禁止占用鼠标。请按 Ctrl+Alt+Space 恢复，或运行 `ctl arm`。"
        )
    if not _read_state().get("armed", True):
        raise GuardError("当前为禁止占用状态。运行 `ctl arm` 或按 Ctrl+Alt+Space 可恢复。")


def assert_user_idle(force: bool = False) -> None:
    """检查用户是否正在用电脑。

    默认**不做**管控，因为系统的"最后输入时间"会把脚本自己发出的输入也算进去 ——
    一旦动作跑起来，下一步就永远满足不了这个条件。真正的控制权交给
    Ctrl+Alt+Space 的允许/禁止开关，那个是用户显式表达的意图。

    传 force=True 可强制执行（命令行 --wait-idle），适合一次会话的第一步。
    """
    cfg = load_config()["safety"]
    if not force and not cfg.get("enforce_user_idle", False):
        return
    threshold = float(cfg["min_user_idle_seconds"])
    idle = winput.user_idle_seconds()
    if idle < threshold:
        raise GuardError(
            f"检测到键鼠活动（{idle:.2f} 秒前）—— 你可能正在使用电脑。"
            "请停止操作鼠标键盘，稍后重试。"
        )


def assert_window_allowed(hwnd: int) -> dict:
    """确认目标窗口在白名单内，返回它的身份信息。"""
    if not hwnd or not winput.is_window(hwnd):
        raise GuardError("目标窗口不存在或已关闭。")

    cfg = load_config()["whitelist"]

    cls = winput.window_class(hwnd)
    if cls not in cfg["window_classes"]:
        raise GuardError(
            f"拒绝操作：窗口类 `{cls}` 不在白名单内。本 skill 只允许操作浏览器窗口。"
        )

    pid = winput.window_pid(hwnd)
    proc = winput.process_name(pid)
    if proc not in cfg["browser_processes"]:
        raise GuardError(
            f"拒绝操作：进程 `{proc}` 不在白名单内"
            f"（允许：{'、'.join(cfg['browser_processes'])}）。"
        )

    if winput.is_elevated(pid) is True:
        raise GuardError(
            "拒绝操作：该浏览器以管理员权限运行，普通进程无法向其注入输入。"
            "请用普通权限重新打开浏览器。"
        )

    return {
        "hwnd": int(hwnd),
        "class": cls,
        "pid": pid,
        "process": proc,
        "title": winput.window_title(hwnd),
    }


def assert_focused(hwnd: int) -> None:
    """要求目标窗口当前在前台，避免把点击发到别的窗口上。"""
    if not load_config()["safety"].get("require_focus", True):
        return
    fg = winput.foreground_window()
    if fg != hwnd:
        raise GuardError(
            f"目标窗口不在前台（前台是 {winput.window_title(fg) or fg}）。"
            "先运行 `ctl focus` 把浏览器切到前台。"
        )


def assert_domain_allowed(url: str | None) -> None:
    cfg = load_config()["whitelist"]
    if not cfg.get("domain_allowlist_enabled") or not url:
        return
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return

    for blocked in cfg.get("domain_blocklist", []):
        if host == blocked or host.endswith("." + blocked):
            raise GuardError(f"拒绝操作：域名 `{host}` 在禁止名单内。")

    allow = cfg.get("domain_allowlist", [])
    if allow and not any(host == d or host.endswith("." + d) for d in allow):
        raise GuardError(f"拒绝操作：域名 `{host}` 不在允许名单内。")


# --------------------------------------------------------------------------
# 审批门
# --------------------------------------------------------------------------

def check_approval(element_name: str | None) -> dict:
    """按危险词判定操作档位。

    返回 {"tier": "ok" | "confirm" | "refuse", "word": 命中的词 或 None}
    """
    if not element_name:
        return {"tier": "ok", "word": None}

    cfg = load_config()["approval"]
    name = element_name.strip()
    if not name:
        return {"tier": "ok", "word": None}
    lowered = name.lower()

    for word in cfg["tier_refuse"]:
        if word.lower() in lowered:
            return {"tier": "refuse", "word": word}

    for word in cfg["tier_confirm"]:
        if word.lower() in lowered:
            return {"tier": "confirm", "word": word}

    return {"tier": "ok", "word": None}


def prompt_user_code(element_name: str, *, ttl_seconds: int = 150) -> None:
    """弹一个系统对话框，把一次性确认码**只显示给用户**。

    这个码绝不能写进模型能读到的任何地方 —— 一旦模型也能看到，这道门就白设了。
    所以它只出现在对话框里，以及一个仅用于比对的临时文件里。
    """
    code = f"{secrets.randbelow(10000):04d}"
    CONSENT_PATH.write_text(
        json.dumps({"code": code, "target": (element_name or "")[:80],
                    "expires": time.time() + ttl_seconds}),
        encoding="utf-8",
    )

    MB_OKCANCEL = 0x00000001
    MB_ICONWARNING = 0x00000030
    MB_TOPMOST = 0x00040000
    MB_SETFOREGROUND = 0x00010000

    message = (
        f"Claude 请求执行一个有副作用的操作：\n\n"
        f"        {(element_name or '(未命名元素)')[:80]}\n\n"
        f"如果你同意，请把这个确认码告诉它：\n\n"
        f"        {code}\n\n"
        f"如果你没有要求过这个操作，请点「取消」。\n"
        f"（确认码 {ttl_seconds} 秒内有效，只能用一次）"
    )
    result = winput.user32.MessageBoxW(
        None, message, "desktop-mouse 操作确认",
        MB_OKCANCEL | MB_ICONWARNING | MB_TOPMOST | MB_SETFOREGROUND,
    )
    if result != 1:  # IDOK
        try:
            CONSENT_PATH.unlink()
        except FileNotFoundError:
            pass
        raise Refused("用户在确认对话框里点了「取消」，操作已放弃。")


def verify_user_code(supplied) -> bool:
    """核对用户报回来的确认码。一次性，用掉即失效。"""
    try:
        data = json.loads(CONSENT_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False

    try:
        CONSENT_PATH.unlink()
    except FileNotFoundError:
        pass

    if time.time() > float(data.get("expires", 0)):
        return False
    return secrets.compare_digest(str(supplied).strip(), str(data.get("code", "")))


def assert_approved(
    element_name: str | None,
    user_consent: bool,
    consent_code=None,
) -> dict:
    """在点击前调用。三层判定：放行 / 要求用户同意 / 直接拒绝。

    诚实说明这个门能挡什么、挡不住什么：
      - 挡得住：误触、模型判断失误
      - `tier_refuse` 挡得住注入：代码直接拒绝，没有任何开关能绕过
      - `tier_confirm` 只靠 `--i-have-user-consent` 的话**挡不住**被注入的模型，
        因为它可以自己加上这个参数。想要真正的门，把 config.json 里的
        `approval.require_user_code` 打开 —— 那时必须由用户在系统弹窗里读到
        一次性确认码再报回来，模型无法自行完成。
    """
    verdict = check_approval(element_name)
    tier = verdict["tier"]

    if tier == "refuse":
        raise Refused(
            f"拒绝执行：目标 `{element_name}` 命中付款类关键词「{verdict['word']}」。\n"
            "这类操作本 skill 一律不做，请你亲自完成。"
        )

    if tier == "confirm":
        if not user_consent:
            raise NeedsConsent(
                f"需要用户确认：目标 `{element_name}` 命中关键词「{verdict['word']}」，"
                "这属于有副作用的操作。\n"
                "请先向用户说明你要点的是什么、会产生什么后果，"
                "得到明确同意后，再加 `--i-have-user-consent` 重新执行。"
            )

        if load_config()["approval"].get("require_user_code", False):
            if not consent_code:
                prompt_user_code(element_name or "")
                raise NeedsConsent(
                    "已经弹出一个系统确认对话框，里面有一个 4 位确认码。\n"
                    "请让用户把对话框里的数字告诉你，然后加上 "
                    "`--consent-code <数字>` 重新执行。"
                )
            if not verify_user_code(consent_code):
                raise NeedsConsent(
                    "确认码不正确或已经过期。请重新征求用户同意 —— "
                    "去掉 --consent-code 再执行一次会重新弹窗。"
                )

    return verdict


# --------------------------------------------------------------------------
# 审计日志
# --------------------------------------------------------------------------

def log_event(
    action: str,
    *,
    window: dict | None = None,
    element: str | None = None,
    xy: tuple[int, int] | None = None,
    url: str | None = None,
    result: str = "ok",
    note: str = "",
    chars: int | None = None,
) -> None:
    """追加一条审计记录。

    刻意不写：输入的文本内容、剪贴板内容、截图内容。
    `chars` 只记长度，用于事后核对，不含原文。
    """
    cfg = load_config()["logging"]
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    record: dict = {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "action": action,
        "result": result,
    }

    if window:
        record["window"] = {
            "hwnd": window.get("hwnd"),
            "process": window.get("process"),
            "title": (window.get("title") or "")[:120],
        }
    if url:
        record["url"] = url[:300]

    if element and cfg.get("log_element_text", True):
        limit = int(cfg.get("element_text_max_chars", 80))
        record["element"] = element[:limit]

    if xy:
        record["xy"] = [int(xy[0]), int(xy[1])]
    if chars is not None:
        record["chars"] = chars
    if note:
        record["note"] = note[:200]

    path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def prune_screenshots() -> int:
    """删掉超过保留时限的截图。截图可能包含隐私，不能无限留。"""
    cfg = load_config()["logging"]
    shot_dir = BASE / cfg["screenshot_dir"]
    if not shot_dir.is_dir():
        return 0

    cutoff = time.time() - float(cfg.get("keep_screenshots_hours", 24)) * 3600
    removed = 0
    for f in shot_dir.glob("*.png"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            pass
    return removed


if __name__ == "__main__":
    print("配置:", CONFIG_PATH)
    print("状态:", STATE_PATH)
    print("已允许占用:", is_armed())
    print("急停标志:", stop_requested())
    print("用户空闲:", round(winput.user_idle_seconds(), 2), "秒")
    print("截图清理:", prune_screenshots(), "个文件")
