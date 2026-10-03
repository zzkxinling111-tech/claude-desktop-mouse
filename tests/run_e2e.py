"""端到端功能验证 —— 在真实浏览器窗口里跑完整流程。

验证：点击是否真的生效、审批门是否真的拦得住、输入是否真的落到输入框、
滚动是否真的动了页面。

这个脚本**不启动任何程序**。请先用浏览器打开 tests/fixture.html，
然后运行它；脚本会自动找到那个页面并接管鼠标约 30 秒。

运行期间请不要动键鼠 —— 你一动手就会和脚本抢光标。

    python run_e2e.py
"""

from __future__ import annotations

import contextlib
import io
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "scripts"))

import ctl  # noqa: E402
import guard  # noqa: E402
import observe  # noqa: E402
import winput  # noqa: E402

TITLE_MARK = "desktop-mouse 测试页"
FIXTURE = HERE / "fixture.html"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""))
    return ok


def run(*args) -> tuple[int, str]:
    """跑一条 ctl 命令，把它的输出收起来，只看退出码。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = ctl.main([str(a) for a in args])
    return code, buf.getvalue()


def page_text(hwnd: int) -> str:
    return observe.read_page_text_uia(hwnd) or ""


def find_fixture_window(timeout: float = 25.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for w in observe.list_browser_windows():
            if TITLE_MARK in w["title"]:
                if not w["foreground"]:
                    winput.focus_window(w["hwnd"])
                    time.sleep(0.4)
                return w
        time.sleep(0.5)
    return None


def warm_up(hwnd: int) -> int:
    """Chrome 的无障碍树是渐进的，等它把页面元素吐出来。"""
    count = 0
    for _ in range(8):
        nodes = observe.collect_nodes(hwnd)
        count = sum(1 for n in nodes if n["name"])
        if count > 12:
            break
        time.sleep(0.6)
    return count


def main() -> int:
    guard.install()
    guard.set_armed(True)

    print("=== 准备 ===")
    win = find_fixture_window()
    if win is None:
        print(f"\n没找到标题含 {TITLE_MARK!r} 的浏览器窗口。\n")
        print("请先在浏览器里打开这个文件：")
        print(f"    {FIXTURE}")
        print("\n打开后重新运行本脚本。")
        return 2

    hwnd = win["hwnd"]
    print(f"  目标窗口 hwnd={hwnd}  {win['title'][:60]}")
    print(f"  UIA 预热：{warm_up(hwnd)} 个有名字的元素")

    # 刷新一下，让页面回到初始状态（测试可以反复跑）
    run("press", "--combo", "ctrl+r", "--window", hwnd)
    time.sleep(2.5)
    print(f"  刷新后预热：{warm_up(hwnd)} 个有名字的元素")
    time.sleep(0.8)

    print("\n=== 1. 查找元素 ===")
    code, _ = run("find", "--text", "普通按钮", "--window", hwnd)
    check(code == 0, "能按文字找到「普通按钮」", f"退出码 {code}")

    code, _ = run("find", "--text", "这个按钮根本不存在", "--window", hwnd)
    check(code == 4, "查找不存在的元素返回退出码 4", f"实际 {code}")

    print("\n=== 2. 普通点击 ===")
    check("等待操作" in page_text(hwnd), "页面初始状态正确")

    code, _ = run("click", "--text", "普通按钮", "--window", hwnd)
    time.sleep(1.0)
    check(code == 0, "点击命令返回成功", f"退出码 {code}")
    check("普通按钮被点击" in page_text(hwnd), "点击真的落到了网页上")

    print("\n=== 3. 审批门拦截 ===")
    code, out = run("click", "--text", "删除全部内容", "--window", hwnd)
    time.sleep(0.8)
    check(code == 2, "「删除」类操作返回退出码 2（需确认）", f"实际 {code}")
    check("审批门失效" not in page_text(hwnd), "危险按钮确实没被点到")
    check("需要用户确认" in out or "已向用户说明" in out, "给出了明确的确认指引")

    code, out = run("click", "--text", "立即支付", "--window", hwnd)
    time.sleep(0.8)
    check(code == 3, "「支付」类操作返回退出码 3（拒绝）", f"实际 {code}")
    check("审批门失效" not in page_text(hwnd), "支付按钮确实没被点到")

    print("\n=== 4. 键盘输入 ===")
    code, _ = run("click", "--text", "普通输入框", "--window", hwnd)
    time.sleep(0.8)
    check(code == 0, "能点中普通输入框", f"退出码 {code}")

    code, _ = run("type", "--text", "hello 世界", "--window", hwnd)
    time.sleep(1.0)
    check(code == 0, "输入命令返回成功", f"退出码 {code}")
    check("hello 世界" in page_text(hwnd), "输入真的落到了正常输入框")

    print("\n=== 5. 密码框拦截 ===")
    code, _ = run("click", "--text", "密码框", "--window", hwnd)
    time.sleep(0.8)

    code, out = run("type", "--text", "supersecret", "--window", hwnd)
    time.sleep(0.8)
    check(code == 3, "向密码框输入被拒绝（退出码 3）", f"实际 {code}")
    check("拦截失效" not in page_text(hwnd), "密码框确实没被输入")

    print("\n=== 6. 滚动 ===")
    code, _ = run("find", "--text", "页面底部的锚点", "--window", hwnd)
    visible_before = code == 0

    code, _ = run("scroll", "--dy", 1500, "--window", hwnd)
    time.sleep(1.4)
    code2, _ = run("find", "--text", "页面底部的锚点", "--window", hwnd)

    check(code == 0, "滚动命令返回成功", f"退出码 {code}")
    if visible_before:
        check(code2 == 0, "滚动后底部锚点可见")
    else:
        check(code2 == 0, "滚动前不可见 → 滚动后可见，滚动真的生效")

    print("\n=== 7. 截图 ===")
    code, _ = run("shot", "--window", hwnd, "--tag", "e2e")
    pngs = sorted((guard.BASE / "log" / "shots").glob("e2e-*.png"))
    check(code == 0 and bool(pngs), f"截图成功并已落盘（{len(pngs)} 个文件）")

    print("\n=== 8. 审计日志 ===")
    logs = sorted((guard.BASE / "log").glob("*.jsonl"))
    check(bool(logs), "审计日志已写入")
    if logs:
        lines = logs[-1].read_text(encoding="utf-8").strip().splitlines()
        check(len(lines) > 0, f"日志有 {len(lines)} 条记录")
        dirty = [ln for ln in lines if "supersecret" in ln or "hello 世界" in ln]
        check(not dirty, "日志里没有出现任何输入文本内容")

    print("\n" + "=" * 58)
    passed = sum(1 for ok, _, _ in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    for ok, name, detail in results:
        if not ok:
            print(f"  失败 → {name}   {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
