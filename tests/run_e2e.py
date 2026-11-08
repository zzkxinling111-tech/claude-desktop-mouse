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
import json
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


def reset_page(hwnd: int, *, scroll: int = 0) -> None:
    """刷新页面回到初始状态，并把视图滚回顶部。

    两个坑都在这里：
      - Chrome 刷新后会**恢复原来的滚动位置**，不回到顶部；不显式 ctrl+home，
        页面顶部的元素就还在视口外面。
      - 屏幕高 1600、任务栏占了底部一截，而拖拽区靠下：「空格2」的中心正好落在
        任务栏上。UIA 说它在页面里，但那一点其实属于任务栏 —— 松手事件根本
        到不了网页。所以拖拽前要 scroll 一下，把它抬到任务栏上面。
    """
    run("press", "--combo", "ctrl+shift+r", "--window", hwnd)
    time.sleep(2.5)
    warm_up(hwnd)
    run("press", "--combo", "ctrl+home", "--window", hwnd)
    time.sleep(0.6)
    if scroll:
        run("scroll", "--dy", scroll, "--window", hwnd)
        time.sleep(0.6)


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
    reset_page(hwnd)
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

    print("\n=== 3. 危险操作：一律弹窗问用户 ===")
    # 测试里不能真的弹系统对话框 —— 没人点，脚本会一直挂着。所以把弹窗
    # 换成一个只做记录的桩。这里验证的是「代码有没有走到要问用户这一步」，
    # 以及「没拿到正确确认码时，一个字都不许动」。
    prompts: list[str] = []
    real_prompt = guard.prompt_user_code

    def stub_prompt(name, **_kw):
        prompts.append(name)
        raise guard.NeedsConsent(f"（测试桩）已对 {name!r} 弹出确认框")

    def seed_code(code: str) -> None:
        guard.CONSENT_PATH.write_text(json.dumps({
            "hash": guard._code_hash(code),
            "target": "删除全部内容",
            "expires": time.time() + 60,
            "attempts": 3,
        }), encoding="utf-8")

    guard.prompt_user_code = stub_prompt
    try:
        code, out = run("click", "--text", "删除全部内容", "--window", hwnd)
        time.sleep(0.6)
        check(code == 2, "「删除」类操作要求用户确认（退出码 2）", f"实际 {code}")
        check(bool(prompts), "确实弹出了确认框", f"弹了 {len(prompts)} 次")
        check("审批门失效" not in page_text(hwnd), "危险按钮确实没被点到")

        code, _ = run("click", "--text", "立即支付", "--window", hwnd)
        time.sleep(0.6)
        check(code == 2, "「支付」类操作同样是询问，不是直接拒绝", f"实际 {code}")
        check("审批门失效" not in page_text(hwnd), "支付按钮确实没被点到")

        seed_code("4271")
        code, _ = run("click", "--text", "删除全部内容", "--consent-code", "0000",
                      "--window", hwnd)
        time.sleep(0.6)
        check(code == 2, "确认码不对时不执行", f"实际 {code}")
        check("审批门失效" not in page_text(hwnd), "用了错码也没碰到危险按钮")

        seed_code("4271")
        code, _ = run("click", "--text", "删除全部内容", "--consent-code", "4271",
                      "--window", hwnd)
        time.sleep(0.8)
        check(code == 0, "正确的确认码放行", f"实际 {code}")
        check("删除按钮被点击" in page_text(hwnd), "拿到用户授权后确实执行了")
    finally:
        guard.prompt_user_code = real_prompt
        try:
            guard.CONSENT_PATH.unlink()
        except FileNotFoundError:
            pass

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
    run("press", "--combo", "ctrl+home", "--window", hwnd)
    time.sleep(0.6)
    code, _ = run("find", "--text", "页面底部的锚点", "--window", hwnd)
    visible_before = code == 0

    # 一次滚不够就再滚一次，直到看见锚点或滚到头。页面长度会随测试内容变化，
    # 写死一个距离迟早会失效。
    moved = False
    code2 = 1
    err = ""
    for _ in range(6):
        code, out = run("scroll", "--dy", 900, "--window", hwnd)
        time.sleep(0.9)
        if code != 0:
            err = f"滚动失败（退出码 {code}）：{out.strip()[:200]}"
            break
        moved = True
        code2, out2 = run("find", "--text", "页面底部的锚点", "--window", hwnd)
        if code2 == 0:
            break
        err = out2.strip()[:200]

    check(moved, "滚动命令返回成功", "" if moved else err)
    check(code2 == 0, "滚动后底部锚点可见，滚动真的生效"
                      + ("（滚动前已可见）" if visible_before else ""),
          "" if code2 == 0 else err)

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

    print("\n=== 9. 闭环拖拽（drag-to）===")
    reset_page(hwnd, scroll=260)

    code, out = run("drag-to", "--from", "it", "--to", "空格1", "--window", hwnd)
    time.sleep(0.6)
    text = page_text(hwnd)
    check(code == 0, "普通拖拽成功", f"退出码 {code}")
    check("把 it 放进了 s1" in text, "词确实落进了第 1 个空格")
    check(not winput.left_button_down(), "拖拽结束后鼠标按键已抬起")

    # 打开「拖拽途中自动重排」，再拖一次 —— 这正是真实场景里失手的那种情况
    reset_page(hwnd, scroll=260)  # 上一次拖进去的词会让 --from 匹配到格子本身，先清干净
    code, _ = run("click", "--text", "拖拽途中自动重排", "--window", hwnd)
    time.sleep(0.5)
    code, out = run("drag-to", "--from", "is", "--to", "空格2", "--window", hwnd)
    time.sleep(0.6)
    text = page_text(hwnd)
    check("页面重排了" in text, "拖拽途中页面确实重排了")
    check(code == 0, "重排之后仍然拖拽成功", f"退出码 {code}")
    check("把 is 放进了 s2" in text, "重排之后仍然落进了正确的空格")
    check(not winput.left_button_down(), "重排场景下鼠标按键也已抬起")

    reset_page(hwnd, scroll=260)
    code, _ = run("drag-to", "--from", "hot", "--to", "不存在的空格", "--window", hwnd)
    check(code == 4, "落点不存在时明确报错（退出码 4）", f"实际 {code}")
    check(not winput.left_button_down(), "失败之后鼠标按键也是抬起的")

    print("\n=== 10. 批量执行 ===")
    reset_page(hwnd)

    ok_steps = [
        {"action": "click", "text": "普通按钮"},
        {"action": "drag-to", "from": "it", "to": "空格1"},
        {"action": "page-text"},
    ]
    ok_path = HERE / "steps-ok.json"
    ok_path.write_text(json.dumps(ok_steps, ensure_ascii=False), encoding="utf-8")

    code, out = run("batch", "--file", ok_path, "--window", hwnd)
    time.sleep(0.6)
    text = page_text(hwnd)
    check(code == 0, "三步批量全部成功", f"退出码 {code}")
    check("普通按钮被点击" in text and "把 it 放进了 s1" in text,
          "批量里每一步都真的生效了，不是一个来回才做一步")
    check("全部 3 步成功" in out, "给出了批量汇总")

    bad_steps = [
        {"action": "click", "text": "普通按钮"},
        {"action": "click", "text": "这个元素根本不存在"},
        {"action": "drag-to", "from": "hot", "to": "空格3"},
    ]
    bad_path = HERE / "steps-bad.json"
    bad_path.write_text(json.dumps(bad_steps, ensure_ascii=False), encoding="utf-8")

    code, out = run("batch", "--file", bad_path, "--window", hwnd)
    time.sleep(0.4)
    check(code == 4, "第二步找不到元素时整批停下（退出码 4）", f"实际 {code}")
    check("停在第 2 步" in out, "明确报告停在哪一步")
    check("把 hot 放进了 s3" not in page_text(hwnd), "失败之后没有继续跑第三步")

    for p in (ok_path, bad_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    print("\n" + "=" * 58)
    passed = sum(1 for ok, _, _ in results if ok)
    print(f"结果: {passed}/{len(results)} 通过")
    for ok, name, detail in results:
        if not ok:
            print(f"  失败 → {name}   {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
