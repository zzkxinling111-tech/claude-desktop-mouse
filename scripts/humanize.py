"""手势层 —— 把机械原语组合成"像人做出来的"操作。

设计原则：
  - 轨迹用三次贝塞尔 + 最小 jerk 速度剖面，不是匀速直线
  - 起点取光标的真实当前位置，不伪造
  - 所有时间间隔都从分布里采样，绝不使用固定值
  - 每个采样点之间检查中止信号

诚实说明：这一层让操作"看起来自然"，但网站判定机器人主要靠的是
浏览器指纹和自动化框架特征，不是鼠标曲线。别指望它单独解决问题。
"""

from __future__ import annotations

import math
import random
import time
from typing import Callable

import winput
from winput import Aborted

# --------------------------------------------------------------------------
# 随机数工具
# --------------------------------------------------------------------------

def u(lo: float, hi: float) -> float:
    """区间内均匀采样。"""
    return random.uniform(lo, hi)


def g(mu: float, sigma: float, lo: float, hi: float) -> float:
    """正态采样并夹到区间内 —— 比均匀分布更像人的反应时间。"""
    return max(lo, min(hi, random.gauss(mu, sigma)))


def sleep(seconds: float) -> None:
    """可被中止的等待。

    短于 2 毫秒时改用忙等 —— Windows 上 time.sleep 的最小粒度可能到 15 毫秒，
    用它去「睡 0.5 毫秒」会变成睡 15 毫秒，高速档反而被拖慢。
    """
    end = time.perf_counter() + max(0.0, seconds)
    while True:
        winput.check_abort()
        remaining = end - time.perf_counter()
        if remaining <= 0:
            return
        if remaining > 0.002:
            time.sleep(min(remaining - 0.001, 0.02))
        # 剩余不足 2 毫秒，继续忙等（此时循环里只剩 check_abort）


# --------------------------------------------------------------------------
# 速度档位
# --------------------------------------------------------------------------
#
# 1.0 是「人类正常速度」。调大就是拿真实性换速度：
#
#     1.0   真人速度。1800 像素的移动约 0.8 秒
#     3.0   明显偏快，但还在人类范围边缘
#    10.0   光标基本等于瞬移 —— 真人不可能 80 毫秒移动 1800 像素
#
# 由 ctl 从 config.json 读出后写进 DEFAULT_SPEED（单进程 CLI，够用）。

DEFAULT_SPEED = 1.0

# 单步延迟低于这个值就干脆不 sleep —— Windows 上 time.sleep 的最小粒度是
# 1~15 毫秒，硬睡反而会让「提速」变得比不提速还慢。
_SLEEP_FLOOR = 0.006


def speed_factor() -> float:
    return max(1.0, float(DEFAULT_SPEED))


def compress(seconds: float, floor: float = 0.0) -> float:
    """按倍速压缩一段时长。用于移动耗时、打字间隔这类可以整体缩放的量。"""
    return max(floor, seconds / speed_factor())


def compress_gently(seconds: float, floor: float = 0.0) -> float:
    """温和压缩停顿类时长（悬停、按下、落点停顿）。

    用平方根而不是全倍率：这些停顿压得太短会让点击变得不可靠 ——
    按下到抬起不足 20 毫秒，有些界面根本不认，会被当成没点。
    """
    return max(floor, seconds / math.sqrt(speed_factor()))


# --------------------------------------------------------------------------
# 轨迹生成
# --------------------------------------------------------------------------

def _min_jerk(t: float) -> float:
    """最小 jerk 进度曲线：起步和收尾都平滑。"""
    return 10 * t ** 3 - 15 * t ** 4 + 6 * t ** 5


def _cubic(t: float, p0, p1, p2, p3) -> tuple[float, float]:
    mt = 1.0 - t
    a, b = mt * mt * mt, 3 * mt * mt * t
    c, d = 3 * mt * t * t, t * t * t
    return (
        a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
        a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1],
    )


def _target_span(distance: float) -> float:
    """菲茨定律：移动耗时随距离对数增长，不是线性。"""
    width = 24.0  # 假设的目标宽度，像素
    return 0.09 + 0.11 * math.log2(distance / width + 1.0)


def build_path(
    x0: float, y0: float, x1: float, y1: float, *, deliberate: bool = False
) -> tuple[list[tuple[int, int]], list[float]]:
    """生成一条人类化轨迹。

    返回 (点列表, 相邻点之间的延迟秒数列表)。
    `deliberate=True` 用于拖拽 —— 更慢、更稳、抖动更小。
    """
    dx, dy = x1 - x0, y1 - y0
    distance = math.hypot(dx, dy)

    if distance < 1.0:
        return [(int(round(x1)), int(round(y1)))], []

    # 总时长
    duration = _target_span(distance)
    if deliberate:
        duration *= u(1.6, 2.2)
    duration *= u(0.85, 1.20)

    # 过冲：约 20% 的概率先到目标外一点，再修正回来
    overshoot_px = 0.0
    if not deliberate and random.random() < 0.20:
        overshoot_px = u(2.0, 3.5) * min(1.0, distance / 200.0)

    ux, uy = dx / distance, dy / distance
    px, py = -uy, ux  # 垂直于移动方向

    end_x = x1 + ux * overshoot_px
    end_y = y1 + uy * overshoot_px

    # 两个随机控制点：偏移量与距离成正比，垂直于主线
    spread = distance * (0.05 if deliberate else 0.08)
    b1 = g(0.0, spread, -distance * 0.25, distance * 0.25)
    b2 = g(0.0, spread, -distance * 0.25, distance * 0.25)

    p0 = (x0, y0)
    p1 = (x0 + dx * u(0.18, 0.32) + px * b1, y0 + dy * u(0.18, 0.32) + py * b1)
    p2 = (x0 + dx * u(0.65, 0.82) + px * b2, y0 + dy * u(0.65, 0.82) + py * b2)
    p3 = (end_x, end_y)

    # 采样点数：始终以「不提速时」的时长来算，这样轨迹形状不受速度影响 ——
    # 提速只压缩时间轴，不改曲线。约每 11 ms 一个点。
    n = int(max(10, min(60, round(duration * 1000 / 11.0))))

    points: list[tuple[int, int]] = []
    for i in range(1, n + 1):
        progress = _min_jerk(i / n)

        # 手抖：越接近终点越收敛（人在瞄准时会稳住）
        settle = 1.0 - progress ** 3
        amp = (1.2 if deliberate else 2.0) * settle
        jx = g(0.0, amp, -3.0, 3.0) if amp > 0.2 else 0.0
        jy = g(0.0, amp, -3.0, 3.0) if amp > 0.2 else 0.0

        bx, by = _cubic(progress, p0, p1, p2, p3)
        points.append((int(round(bx + jx)), int(round(by + jy))))

    # 过冲修正：补 2~3 个短点回到真正的目标
    if overshoot_px > 0.5:
        fix_n = random.randint(2, 3)
        for i in range(1, fix_n + 1):
            r = i / fix_n
            points.append((
                int(round(end_x + (x1 - end_x) * r)),
                int(round(end_y + (y1 - end_y) * r)),
            ))

    points[-1] = (int(round(x1)), int(round(y1)))

    # 到这一步才压缩时间轴 —— 曲线已经生成好了
    duration = compress(duration)

    # 时间分配：基础匀速 + 每步抖动，保证永不出现完全相同的间隔
    base = duration / len(points)
    if base < 0.0015:
        # 单步太短，sleep 的精度开销会盖过延迟本身，干脆一口气发完。
        # 结果是光标沿着同一条曲线「瞬移」而过。
        delays = [0.0] * len(points)
    else:
        delays = [max(0.0005, base * u(0.6, 1.5)) for _ in points]
        scale = duration / sum(delays)
        delays = [d * scale for d in delays]

    return points, delays


def move_to(x: int, y: int, *, deliberate: bool = False) -> None:
    """沿人类化轨迹把光标移到目标位置。"""
    x0, y0 = winput.cursor_pos()
    points, delays = build_path(x0, y0, x, y, deliberate=deliberate)
    for (px, py), delay in zip(points, delays):
        winput.check_abort()
        winput.send_move(px, py)
        if delay > 0.002:
            time.sleep(delay)
    winput.check_abort()

    # 校验：如果系统拒绝了这次移动（例如目标窗口提权），立刻发现
    ax, ay = winput.cursor_pos()
    if abs(ax - x) > 4 or abs(ay - y) > 4:
        raise OSError(
            f"光标未到达目标：期望 ({x}, {y})，实际 ({ax}, {ay})。"
            "通常是目标窗口以管理员权限运行，普通进程无法向其注入输入。"
        )


# --------------------------------------------------------------------------
# 四种手势
# --------------------------------------------------------------------------

def click(x: int, y: int, *, button: str = "left", hover: bool = True) -> None:
    move_to(x, y)
    if hover:
        # 人不会一到就立刻按。这个停顿只按平方根压缩 —— 压得太短，
        # 界面可能来不及处理悬停，点击会落空。
        sleep(compress_gently(u(0.08, 0.25), 0.012))
    winput.send_button(button, True)
    sleep(compress_gently(u(0.06, 0.14), 0.018))   # 按下到抬起
    winput.send_button(button, False)


def double_click(x: int, y: int, *, button: str = "left") -> None:
    move_to(x, y)
    sleep(compress_gently(u(0.08, 0.25), 0.012))

    # 两次点击的间隔**不按速度压缩**：它有硬性上限，超过系统的双击阈值
    # 就会被拆成两次单击。
    threshold = winput.double_click_time_ms() / 1000.0
    gap = threshold * u(0.40, 0.70)

    for i in range(2):
        winput.check_abort()
        winput.send_button(button, True)
        sleep(compress_gently(u(0.04, 0.09), 0.015))
        winput.send_button(button, False)
        if i == 0:
            sleep(gap * u(0.85, 1.15))


def long_press(x: int, y: int, ms: int = 900, *, button: str = "left") -> None:
    """按下不动，保持 ms 毫秒后抬起。

    用于"按住以确认"、长按菜单、以及网页上的移动端模拟长按。
    按住时长是语义性的，不按倍速压缩 —— 它就是要按那么久。
    """
    hold = max(0.3, min(10.0, ms / 1000.0))
    move_to(x, y)
    sleep(compress_gently(u(0.10, 0.28), 0.015))
    winput.send_button(button, True)
    try:
        sleep(hold)
    finally:
        # 无论正常结束还是被急停打断，都要抬起，不能留下按住的鼠标
        winput.send_button(button, False)


def drag(
    x0: int, y0: int, x1: int, y1: int, *, button: str = "left", hold_end: bool = True
) -> None:
    """从 A 拖到 B。"""
    move_to(x0, y0)
    sleep(compress_gently(u(0.10, 0.26), 0.015))
    winput.send_button(button, True)
    try:
        sleep(compress_gently(u(0.08, 0.18), 0.015))

        # 拖拽阈值：多数界面要求先移动几像素才认定这是拖拽而非点击
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy) or 1.0
        thresh = u(3.0, 6.0)
        winput.send_move(int(round(x0 + dx / length * thresh)),
                         int(round(y0 + dy / length * thresh)))
        sleep(compress_gently(u(0.02, 0.05), 0.008))

        # 用 deliberate 模式走完剩下的路程
        cx, cy = winput.cursor_pos()
        points, delays = build_path(cx, cy, x1, y1, deliberate=True)
        for (px, py), delay in zip(points, delays):
            winput.check_abort()
            winput.send_move(px, py)
            if delay > 0.002:
                time.sleep(delay)

        if hold_end:
            # 落点略作停顿，像人在确认位置
            sleep(compress_gently(u(0.12, 0.30), 0.02))
    finally:
        winput.send_button(button, False)


def scroll(dy: int, *, dx: int = 0) -> None:
    """滚动页面。

    `dy` 与鼠标移动语义一致：正数 = 向下看页面下方，负数 = 向上。
    100 像素约等于一格滚轮。
    """
    notches_y = int(round(-dy / 100.0))  # 滚轮向下是负值，与屏幕坐标相反
    notches_x = int(round(dx / 100.0))

    def emit(notches: int, horizontal: bool) -> None:
        if notches == 0:
            return
        step = 1 if notches > 0 else -1
        remaining = abs(notches)
        while remaining > 0:
            # 人不会一次滚固定格数：3~6 格一组，组间有停顿
            burst = min(remaining, random.randint(3, 6))
            for _ in range(burst):
                winput.check_abort()
                winput.send_wheel(step * 120, horizontal=horizontal)
                sleep(compress_gently(u(0.02, 0.06), 0.006))
            remaining -= burst
            if remaining > 0:
                sleep(compress_gently(u(0.12, 0.35), 0.02))

    emit(notches_y, False)
    if notches_x:
        emit(notches_x, True)


# 词边界字符：打完这些之后人通常会顿一下，像在想下一个词
_WORD_BOUNDARY = " ，。！？、；：,.!?;:\n\t"

# 这个额外停顿按**基础字符间隔的倍数**算，不用绝对值。
# 用绝对值的话，高速档下每个标点都要停固定的 0.17 秒，
# 这些小停顿会反过来变成主要耗时 —— 把 WPM 调到 200 也快不起来。
_BOUNDARY_EXTRA = (0.6, 1.0)
_BOUNDARY_MEAN = 0.8


def _base_char_delay(wpm: float) -> float:
    """平均每字符间隔。45 词/分 ≈ 每字符 0.267 秒，200 词/分 ≈ 0.06 秒。"""
    return 60.0 / (max(1.0, wpm) * 5.0)


def estimate_type_duration(text: str, *, wpm: float = 45.0) -> float:
    """估算逐字符打完这段文本要多久（秒）。

    用途是给动作设一个**和它自身规模匹配**的超时上限。固定的上限会把
    "打一段长文字"这种完全正常的操作当成失控掐掉 —— 实测踩过这个坑：
    106 个字符要 31.7 秒，而默认上限只有 25 秒。
    """
    if not text:
        return 0.0
    base = _base_char_delay(wpm)
    boundaries = sum(1 for ch in text if ch in _WORD_BOUNDARY)
    return len(text) * base + boundaries * base * _BOUNDARY_MEAN


def type_text(text: str, *, wpm: float = 45.0) -> None:
    """逐字符输入文本。

    密码框的拦截在 guard 层，不在这里。
    注意：WPM 是**唯一的**打字速度控制项，不叠加 mouse_speed。
    """
    if not text:
        return

    base_delay = _base_char_delay(wpm)

    for i, ch in enumerate(text):
        winput.check_abort()
        winput.send_text_unicode(ch)
        if i == len(text) - 1:
            break

        delay = g(base_delay, base_delay * 0.45, base_delay * 0.35, base_delay * 2.2)
        # 词边界（空格、标点、换行）前停顿更久，像人在想下一个词
        if ch in _WORD_BOUNDARY:
            delay += base_delay * u(*_BOUNDARY_EXTRA)
        sleep(delay)


def paste_text(text: str, *, settle: float | None = None) -> bool:
    """用剪贴板粘贴文本 —— 比逐字符打快一百倍以上。

    真人填长文本时本来就这么干，所以这一步并不突兀。但有两个代价要知道：

      1. 剪贴板会被短暂覆盖（之后尽力还原，非文本格式还原不了）
      2. 整块文字瞬间出现，这本身是一个可被识别为自动化的特征

    返回：剪贴板里原本有无法还原的内容时为 True。
    """
    if not text:
        return False

    snap = winput.clipboard_snapshot()
    try:
        winput.clipboard_set_text(text)
    except OSError as e:
        raise OSError(f"粘贴失败：{e}")

    try:
        winput.check_abort()
        winput.send_combo("ctrl+v")
        sleep(settle if settle is not None else compress_gently(0.18, 0.05))
    finally:
        winput.clipboard_restore(snap)

    return bool(snap.get("has_other"))


def press(combo: str) -> None:
    """发送组合键，例如 ctrl+l / enter / ctrl+shift+t。"""
    winput.check_abort()
    winput.send_combo(combo)
    sleep(compress_gently(u(0.05, 0.14), 0.008))


# --------------------------------------------------------------------------
# 自检
# --------------------------------------------------------------------------

if __name__ == "__main__":
    print("3 秒后开始轨迹自检 —— 只移动光标，不点击任何东西。")
    print("把鼠标甩到屏幕左上角可以中止。")
    time.sleep(3)

    start = winput.cursor_pos()
    print("起点:", start)

    for name, tx, ty in (
        ("短距", start[0] + 120, start[1] + 40),
        ("中距", start[0] - 400, start[1] + 260),
        ("长距", start[0] + 800, start[1] - 500),
    ):
        t0 = time.perf_counter()
        move_to(tx, ty)
        dt = time.perf_counter() - t0
        end = winput.cursor_pos()
        dist = math.hypot(tx - start[0], ty - start[1])
        print(f"{name}: {dist:6.0f}px 用时 {dt:.3f}s  落点 {end}")

    move_to(*start)
    print("自检完成，光标已复位。")
