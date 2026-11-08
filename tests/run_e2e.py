"""End-to-end verification — drives a real browser window through the full flow.

Checks that clicks actually land, that the approval gate really blocks, that
typing reaches the field, and that scrolling really moves the page.

This script **starts nothing**. Open tests/fixture.html in a browser first, then
run it; it finds that page and takes over the mouse for about 30 seconds.

Do not touch the keyboard or mouse while it runs — you will fight it for the cursor.

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

TITLE_MARK = "desktop-mouse test page"
FIXTURE = HERE / "fixture.html"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(f"  {'✓' if ok else '✗'} {name}" + (f"   {detail}" if detail else ""))
    return ok


def run(*args) -> tuple[int, str]:
    """Run one ctl command, swallow its output, keep only the exit code."""
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
    """Chrome builds its accessibility tree gradually; wait for it to populate."""
    count = 0
    for _ in range(8):
        nodes = observe.collect_nodes(hwnd)
        count = sum(1 for n in nodes if n["name"])
        if count > 12:
            break
        time.sleep(0.6)
    return count


def reset_page(hwnd: int, *, scroll: int = 0) -> None:
    """Reload to the initial state and scroll back to the top.

    Two traps live here:
      - Chrome **restores the previous scroll position** after a reload instead of
        jumping to the top; without an explicit ctrl+home the top of the page stays
        outside the viewport.
      - The screen is 1600 px tall and the taskbar covers the bottom strip, while
        the drag area sits low: the centre of "Slot 2" lands exactly on the taskbar.
        UIA says it is in the page, but that pixel belongs to the taskbar — the
        mouseup never reaches the document. So scroll first and lift it clear.
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

    print("=== Setup ===")
    win = find_fixture_window()
    if win is None:
        print(f"\nNo browser window with {TITLE_MARK!r} in its title.\n")
        print("Open this file in a browser first:")
        print(f"    {FIXTURE}")
        print("\nThen run this script again.")
        return 2

    hwnd = win["hwnd"]
    print(f"  target hwnd={hwnd}  {win['title'][:60]}")
    print(f"  UIA warm-up: {warm_up(hwnd)} named elements")

    # Reload so the page starts clean — the suite must be repeatable
    reset_page(hwnd)
    print(f"  after reload: {warm_up(hwnd)} named elements")
    time.sleep(0.8)

    print("\n=== 1. Finding elements ===")
    code, _ = run("find", "--text", "Normal button", "--window", hwnd)
    check(code == 0, 'finds "Normal button" by its text', f"exit {code}")

    code, _ = run("find", "--text", "this button does not exist", "--window", hwnd)
    check(code == 4, "missing element exits with code 4", f"got {code}")

    print("\n=== 2. Plain click ===")
    check("waiting" in page_text(hwnd), "page is in its initial state")

    code, _ = run("click", "--text", "Normal button", "--window", hwnd)
    time.sleep(1.0)
    check(code == 0, "click command returns success", f"exit {code}")
    check("normal button clicked" in page_text(hwnd), "the click really reached the page")

    print("\n=== 3. Dangerous actions: always ask the user ===")
    # The suite must not open real system dialogs — nobody is there to answer and
    # the script would hang. The prompt is stubbed with a recorder instead. What is
    # under test is: does the code reach the "ask the user" step at all, and does it
    # keep its hands off when the consent code is wrong.
    prompts: list[str] = []
    real_prompt = guard.prompt_user_code

    def stub_prompt(name, **_kw):
        prompts.append(name)
        raise guard.NeedsConsent(f"(test stub) consent dialog for {name!r}")

    def seed_code(code: str) -> None:
        guard.CONSENT_PATH.write_text(json.dumps({
            "hash": guard._code_hash(code),
            "target": "Delete everything",
            "expires": time.time() + 60,
            "attempts": 3,
        }), encoding="utf-8")

    guard.prompt_user_code = stub_prompt
    try:
        code, out = run("click", "--text", "Delete everything", "--window", hwnd)
        time.sleep(0.6)
        check(code == 2, "a delete-class action asks the user (exit 2)", f"got {code}")
        check(bool(prompts), "the consent prompt was actually raised", f"{len(prompts)} time(s)")
        check("approval gate FAILED" not in page_text(hwnd), "the dangerous button was never hit")

        code, _ = run("click", "--text", "Pay now", "--window", hwnd)
        time.sleep(0.6)
        check(code == 2, "a pay-class action also asks rather than refusing outright", f"got {code}")
        check("approval gate FAILED" not in page_text(hwnd), "the pay button was never hit")

        seed_code("4271")
        code, _ = run("click", "--text", "Delete everything", "--consent-code", "0000",
                      "--window", hwnd)
        time.sleep(0.6)
        check(code == 2, "a wrong consent code does not execute", f"got {code}")
        check("approval gate FAILED" not in page_text(hwnd), "the wrong code touched nothing")

        seed_code("4271")
        code, _ = run("click", "--text", "Delete everything", "--consent-code", "4271",
                      "--window", hwnd)
        time.sleep(0.8)
        check(code == 0, "the correct consent code is let through", f"got {code}")
        check("delete button clicked" in page_text(hwnd), "it really ran once the user approved")
    finally:
        guard.prompt_user_code = real_prompt
        try:
            guard.CONSENT_PATH.unlink()
        except FileNotFoundError:
            pass

    print("\n=== 4. Keyboard input ===")
    code, _ = run("click", "--text", "Normal input", "--window", hwnd)
    time.sleep(0.8)
    check(code == 0, "clicks the normal input", f"exit {code}")

    code, _ = run("type", "--text", "hello world", "--window", hwnd)
    time.sleep(1.0)
    check(code == 0, "type command returns success", f"exit {code}")
    check("hello world" in page_text(hwnd), "the text really landed in the input")

    print("\n=== 5. Password field ===")
    code, _ = run("click", "--text", "Password", "--window", hwnd)
    time.sleep(0.8)

    code, out = run("type", "--text", "supersecret", "--window", hwnd)
    time.sleep(0.8)
    check(code == 3, "typing into a password field is refused (exit 3)", f"got {code}")
    check("blocking FAILED" not in page_text(hwnd), "the password field really received nothing")

    print("\n=== 6. Scrolling ===")
    run("press", "--combo", "ctrl+home", "--window", hwnd)
    time.sleep(0.6)
    code, _ = run("find", "--text", "Anchor at the bottom", "--window", hwnd)
    visible_before = code == 0

    # One scroll is not always enough — keep going until the anchor shows up or the
    # page bottoms out. Hard-coding a distance breaks as soon as the content changes.
    moved = False
    code2 = 1
    err = ""
    for _ in range(6):
        code, out = run("scroll", "--dy", 900, "--window", hwnd)
        time.sleep(0.9)
        if code != 0:
            err = f"scroll failed (exit {code}): {out.strip()[:200]}"
            break
        moved = True
        code2, out2 = run("find", "--text", "Anchor at the bottom", "--window", hwnd)
        if code2 == 0:
            break
        err = out2.strip()[:200]

    check(moved, "scroll command returns success", "" if moved else err)
    check(code2 == 0, "the bottom anchor is visible after scrolling — scrolling worked"
                      + (" (already visible before scrolling)" if visible_before else ""),
          "" if code2 == 0 else err)

    print("\n=== 7. Screenshot ===")
    code, _ = run("shot", "--window", hwnd, "--tag", "e2e")
    pngs = sorted((guard.BASE / "log" / "shots").glob("e2e-*.png"))
    check(code == 0 and bool(pngs), f"screenshot written to disk ({len(pngs)} file(s))")

    print("\n=== 8. Audit log ===")
    logs = sorted((guard.BASE / "log").glob("*.jsonl"))
    check(bool(logs), "audit log was written")
    if logs:
        lines = logs[-1].read_text(encoding="utf-8").strip().splitlines()
        check(len(lines) > 0, f"log holds {len(lines)} entries")
        dirty = [ln for ln in lines if "supersecret" in ln or "hello world" in ln]
        check(not dirty, "no typed text leaked into the log")

    print("\n=== 9. Closed-loop drag (drag-to) ===")
    reset_page(hwnd, scroll=260)

    code, out = run("drag-to", "--from", "it", "--to", "Slot 1", "--window", hwnd)
    time.sleep(0.6)
    text = page_text(hwnd)
    check(code == 0, "plain drag succeeds", f"exit {code}")
    check("dropped it into s1" in text, "the word really landed in the first slot")
    check(not winput.left_button_down(), "the mouse button is released afterwards")

    # Tick "reflow mid-drag" and drag again — the failure mode from real sites
    reset_page(hwnd, scroll=260)  # the previous drop makes --from match the slot itself; reset first
    code, _ = run("click", "--text", "Reflow mid-drag", "--window", hwnd)
    time.sleep(0.5)
    code, out = run("drag-to", "--from", "is", "--to", "Slot 2", "--window", hwnd)
    time.sleep(0.6)
    text = page_text(hwnd)
    check("layout reflowed" in text, "the layout really reflowed mid-drag")
    check(code == 0, "the drag still succeeds after the reflow", f"exit {code}")
    check("dropped is into s2" in text, "it still lands in the right slot")
    check(not winput.left_button_down(), "the button is released in the reflow case too")

    reset_page(hwnd, scroll=260)
    code, _ = run("drag-to", "--from", "hot", "--to", "No such slot", "--window", hwnd)
    check(code == 4, "a missing drop target fails loudly (exit 4)", f"got {code}")
    check(not winput.left_button_down(), "the button is released after a failure too")

    print("\n=== 10. Batch ===")
    reset_page(hwnd)

    ok_steps = [
        {"action": "click", "text": "Normal button"},
        {"action": "drag-to", "from": "it", "to": "Slot 1"},
        {"action": "page-text"},
    ]
    ok_path = HERE / "steps-ok.json"
    ok_path.write_text(json.dumps(ok_steps, ensure_ascii=False), encoding="utf-8")

    code, out = run("batch", "--file", ok_path, "--window", hwnd)
    time.sleep(0.6)
    text = page_text(hwnd)
    check(code == 0, "all three batch steps succeed", f"exit {code}")
    check("normal button clicked" in text and "dropped it into s1" in text,
          "every step really took effect — not one round-trip per action")
    check("All 3 steps succeeded" in out, "a batch summary is returned")

    bad_steps = [
        {"action": "click", "text": "Normal button"},
        {"action": "click", "text": "this element does not exist"},
        {"action": "drag-to", "from": "hot", "to": "Slot 3"},
    ]
    bad_path = HERE / "steps-bad.json"
    bad_path.write_text(json.dumps(bad_steps, ensure_ascii=False), encoding="utf-8")

    code, out = run("batch", "--file", bad_path, "--window", hwnd)
    time.sleep(0.4)
    check(code == 4, "the batch stops at the missing element (exit 4)", f"got {code}")
    check("Stopped at step 2" in out, "it reports which step it stopped on")
    check("dropped hot into s3" not in page_text(hwnd), "step three never ran")

    for p in (ok_path, bad_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    print("\n" + "=" * 58)
    passed = sum(1 for ok, _, _ in results if ok)
    print(f"Result: {passed}/{len(results)} passed")
    for ok, name, detail in results:
        if not ok:
            print(f"  FAILED → {name}   {detail}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
