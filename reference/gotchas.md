# Windows gotchas

Ordered by "how likely you are to hit this × how long it takes to diagnose."

---

## 1. DPI scaling (the biggest one)

**Symptom:** clicks are consistently offset. The higher the display scaling, the bigger the
offset — at 125% it can be tens of pixels.

**Cause:** a process that doesn't declare DPI awareness gets a *virtualized* coordinate
system from Windows. Screenshots are in physical pixels, coordinates are in logical pixels,
and the two no longer line up.

**Fix:** `winput.py` calls `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)` the moment
the module is imported, before any other window API. This has to happen first — once you've
called another window API, it's too late to set.

**Verify:** item 1 of `python selfcheck.py` should report `per-monitor-v2`.

---

## 2. Multi-monitor and negative coordinates

**Symptom:** every click on a secondary monitor lands in the wrong place when that monitor
sits to the left of or above the primary one.

**Cause:** the virtual desktop's origin is the primary monitor's top-left corner. A monitor
to the left has negative coordinates.

**Fix:** absolute coordinates must carry `MOUSEEVENTF_VIRTUALDESK`, and normalization must
use `SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN / SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN` —
not `SM_CXSCREEN`. See `winput.to_absolute()`.

---

## 3. `SetForegroundWindow` doesn't actually grab focus

**Symptom:** you call `SetForegroundWindow`, the window doesn't come forward, and your click
lands in whatever was in front.

**Cause:** Windows restricts background processes from stealing focus, and a bare
`SetForegroundWindow` is silently ignored.

**Fix:** `AttachThreadInput` to splice the current thread's input queue onto the target
thread's, then `BringWindowToTop` + `SetForegroundWindow`, then **verify** with
`GetForegroundWindow() == hwnd` and raise if it doesn't match rather than ploughing on.
See `winput.focus_window()`.

---

## 4. Elevated windows receive no input (UIPI)

**Symptom:** the cursor moves fine, but clicks and keystrokes have zero effect on the target
window.

**Cause:** Windows' UIPI isolation forbids a normal-integrity process from injecting input
into a high-integrity (administrator) process.

**Fix:** `winput.is_elevated()` checks up front and **refuses with an explanation** if the
target is elevated.

**Do not** run the skill elevated to work around this. That would let it drive UAC dialogs
and system settings — i.e. dismantle the Windows security boundary for a little automation
convenience.

---

## 5. Chrome's accessibility tree loads lazily

**Symptom:** the first query returns only the toolbar (~50 elements); no page content.

**Cause:** Chrome doesn't build its accessibility tree until a UIA client asks for it, and
the renderer processes take a moment to catch up.

**Fix:** after the first `observe`, wait 1–2 seconds and query again. `warm_up()` in
`tests/run_e2e.py` does exactly this.

**Cost:** querying UIA keeps Chrome's accessibility enabled, and **that is itself a
detectable fingerprint**. For highly sensitive sites, switch to screenshot-only mode
(`shot` without `observe`).

---

## 6. comtypes crashes when walking the UIA tree

**Symptom:** `ControlViewWalker.GetFirstChildElement()` throws
`COMError: invalid pointer (-2147467261)` around the third or fourth level of recursion.

**Cause:** a comtypes element-lifetime bug — the parent element gets released early.

**Fix:** use level-by-level `FindAll(TreeScope_Children, ControlViewCondition)` instead, then
`GetElement(i)` on the returned `IUIAutomationElementArray`. Measured: 313 nodes, 0 failures,
250 ms. See `observe.collect_nodes()`.

---

## 7. Double-click registers as two single clicks

**Symptom:** double-click doesn't work; you get two separate clicks.

**Fix:** the gap between the two presses must be shorter than the system threshold. Read it
with `GetDoubleClickTime()` and use 0.4–0.7× of it as the actual gap.
See `humanize.double_click()`.

---

## 8. Drag registers as a click

**Symptom:** you meant to drag; the UI saw a click.

**Cause:** most interfaces have a drag threshold — the pointer must move a few pixels after
the press before it counts as a drag.

**Fix:** after pressing, move 3–6 px first, pause briefly, then traverse the full path.
See `humanize.drag()`.

---

## 9. `GetLastInputInfo` gets polluted by your own input

**Symptom:** the "user is actively using the computer" check fails permanently from step two
onward.

**Cause:** input sent via `SendInput` also updates the system's "last input time."

**Fix:** `safety.enforce_user_idle` defaults to off. Control belongs to the `Ctrl+Alt+F9`
arm switch — an explicit statement of intent by the user, rather than a guess.

---

## 10. Non-ASCII text turns to mojibake on the console

**Symptom:** Chinese element names print as `����`.

**Cause:** on Chinese Windows the console defaults to GBK, while the reader decodes as UTF-8.

**Fix:** every entry-point script calls `sys.stdout.reconfigure(encoding="utf-8")` at the top.
See the head of `ctl.py`.

---

## 11. A global hotkey is already taken

**Symptom:** `guardd.py` fails to start with error 1409.

**Cause:** `RegisterHotKey` is exclusive. On Chinese Windows, `Ctrl+Alt+Space` is usually
already held by the IME.

**Fix:** `python guardd.py --scan` lists the free combinations. Put one in `config.json` and
restart the daemon.

---

## 12. The user and the script fight over the mouse

**Symptom:** clicks miss, or the cursor gets yanked away mid-action.

**Cause:** the built-in cost of the real-mouse approach — there is only one cursor.

**Fix:**

- Tell the user "I'm taking over the mouse, press Ctrl+Alt+Q to stop" before you start
- Bound each action with `safety.max_action_seconds`
- `humanize.move_to()` verifies the cursor actually arrived and raises instead of clicking
  blindly at the wrong spot

---

## 13. Screenshots capture whatever is on top

**Symptom:** the screenshot contains another window covering the browser.

**Cause:** `BitBlt` against the screen DC captures **what is currently displayed**. Occluded
regions show the occluding window.

**Fix:** `focus` before you `shot`. This is an inherent limitation of the real-input approach
— the browser has to genuinely be on screen, unlike a headless browser.

---

## 14. The page changes between two steps

**Symptom:** you fill half a form, then discover a hundred typed characters went into a
completely different page.

**Cause:** pages change under you — a tab gets switched, a login session expires and
redirects, the site redirects on its own. Each `ctl` invocation is a separate process, so
there is nothing holding a lock on "which page we're on."

**Fix:** pass `--expect-url <substring>` on every step of a multi-step flow. It's checked
before the action runs, and a mismatch aborts rather than typing into the wrong place.
This was a real failure observed during development: a GitHub form was half-filled, the page
changed, and 106 characters landed elsewhere.

**Related:** `ctl type` also refuses when the focused element is a page body rather than an
input, unless you pass `--allow-page-body`. Free-typing into a page can trigger keyboard
shortcuts.
