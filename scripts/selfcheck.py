"""环境自检 —— 确认坐标系、UIA、安全层都正常工作。

只移动光标，不点击任何东西。出问题时先跑这个。

    python selfcheck.py
"""

from __future__ import annotations

import random
import time

import winput


def line(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    problems: list[str] = []

    line("1. 坐标空间")
    vx, vy, vw, vh = winput.virtual_screen()
    print(f"  DPI 模式    : {winput.DPI_MODE}")
    print(f"  虚拟屏幕    : 原点 ({vx},{vy})  尺寸 {vw}×{vh}")
    print(f"  双击阈值    : {winput.double_click_time_ms()} ms")
    print(f"  当前光标    : {winput.cursor_pos()}")
    print(f"  自身已提权  : {winput.is_elevated(winput.kernel32.GetCurrentProcessId())}")
    if winput.DPI_MODE == "none":
        problems.append("DPI 感知设置失败，高分屏下坐标会偏移。")

    line("2. 坐标精度（不涉及 UIA，纯 SendInput 往返）")
    original = winput.cursor_pos()
    deviations = []
    for _ in range(20):
        tx = random.randint(50, max(51, vw - 50))
        ty = random.randint(50, max(51, vh - 50))
        winput.send_move(tx, ty)
        ax, ay = winput.cursor_pos()
        deviations.append((abs(ax - tx), abs(ay - ty)))

    worst = max(max(d) for d in deviations)
    avg = sum(sum(d) for d in deviations) / (2 * len(deviations))
    print(f"  20 次随机移动：最大偏差 {worst} px，平均偏差 {avg:.2f} px")
    if worst > 2:
        problems.append(
            f"坐标精度异常（最大偏差 {worst}px）。"
            "常见原因：DPI 感知未生效、鼠标指针加速、或有人在动鼠标。"
        )
    winput.send_move(*original)

    line("3. 浏览器窗口与 UIA")
    try:
        import observe
        wins = observe.list_browser_windows()
        if not wins:
            print("  (没有找到浏览器窗口 —— 请打开 Chrome 或 Edge 后重跑)")
        for w in wins:
            print(f"  hwnd={w['hwnd']:<10} {w['process']:<10} {w['title'][:50]!r}")

        if wins:
            hwnd = wins[0]["hwnd"]
            t0 = time.perf_counter()
            nodes = observe.collect_nodes(hwnd, max_nodes=4000)
            dt = time.perf_counter() - t0
            named = sum(1 for n in nodes if n["name"])
            print(f"  UIA 采集：{len(nodes)} 个节点（{named} 个有名字），耗时 {dt:.3f}s")
            if named == 0:
                problems.append(
                    "UIA 树里没有带名字的元素 —— Chrome 的无障碍树可能还没启用，"
                    "重跑一次通常就好了。"
                )
            url = observe.read_url(hwnd)
            print(f"  地址栏读出  : {url!r}")
            if not url:
                problems.append("读不到地址栏，域名白名单和审计日志会缺 URL。")
    except ImportError:
        print("  observe.py 尚未就绪，跳过。")

    line("4. 安全层")
    try:
        import guard
        print(f"  已允许占用  : {guard.is_armed()}")
        print(f"  急停标志    : {guard.stop_requested()}")
        print(f"  用户空闲    : {winput.user_idle_seconds():.2f} s")
        for probe, expect in (("登录", "ok"), ("删除", "confirm"), ("立即支付", "refuse")):
            tier = guard.check_approval(probe)["tier"]
            mark = "✓" if tier == expect else "✗"
            print(f"  {mark} 审批 `{probe}` -> {tier}（应为 {expect}）")
            if tier != expect:
                problems.append(f"审批门异常：`{probe}` 判定为 {tier}，应为 {expect}。")
    except ImportError:
        print("  guard.py 尚未就绪，跳过。")

    line("结论")
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("  ✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
