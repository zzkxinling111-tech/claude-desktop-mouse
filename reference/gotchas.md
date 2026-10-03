# Windows 踩坑笔记

按"踩到的概率 × 排查难度"排序。

---

## 1. DPI 缩放（第一大坑）

**症状**：点击位置整体偏移，屏幕缩放比例越高偏得越多。125% 缩放下可能差几十像素。

**原因**：进程不声明 DPI 感知时，Windows 会给它一套"虚拟化"的坐标。
截图是物理像素，坐标却是逻辑像素，两边对不上。

**处理**：`winput.py` 在模块导入时立刻调用
`SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`，早于任何窗口 API。
这一步必须在最前面——一旦调用过其他窗口 API，再设置就晚了。

**验证**：`python selfcheck.py` 第 1 项应显示 `per-monitor-v2`。

---

## 2. 多显示器与负坐标

**症状**：副屏在主屏左边或上边时，副屏上的点击全部落错。

**原因**：虚拟桌面的原点是主屏左上角，副屏在主屏左侧时坐标为负。

**处理**：绝对坐标必须带 `MOUSEEVENTF_VIRTUALDESK`，
归一化范围用 `SM_XVIRTUALSCREEN / SM_YVIRTUALSCREEN / SM_CXVIRTUALSCREEN / SM_CYVIRTUALSCREEN`
而不是 `SM_CXSCREEN`。见 `winput.to_absolute()`。

---

## 3. SetForegroundWindow 抢不到焦点

**症状**：调用了 `SetForegroundWindow`，但窗口没有真的到前台，点击打到了别的窗口上。

**原因**：Windows 限制后台进程抢焦点，单独的 `SetForegroundWindow` 经常被静默忽略。

**处理**：先用 `AttachThreadInput` 把当前线程和目标线程的输入队列接起来，
再 `BringWindowToTop` + `SetForegroundWindow`，最后**校验**
`GetForegroundWindow() == hwnd`，失败就报错而不是继续。见 `winput.focus_window()`。

---

## 4. 提权窗口收不到输入（UIPI）

**症状**：能移动光标，但点击/按键对目标窗口完全无效。

**原因**：Windows 的 UIPI 隔离禁止普通进程向高完整性级别（管理员）进程注入输入。

**处理**：`winput.is_elevated()` 提前检测，发现是提权进程就**拒绝操作并说明原因**。

**不要**为了自动化而提权运行 skill——那会让它能够操作 UAC 对话框和系统设置，
等于拆掉整个 Windows 的安全边界。

---

## 5. Chrome 无障碍树的懒加载

**症状**：第一次查询只拿到工具栏（约 50 个元素），看不到网页内容。

**原因**：Chrome 默认不构建无障碍树。有 UIA 客户端来查询时才启用，
而渲染进程跟上还需要一点时间。

**处理**：第一次 `observe` 之后等 1~2 秒再查一次。
`tests/run_e2e.py` 里的 `warm_up()` 就是干这个的。

**代价**：查询 UIA 会一直让 Chrome 的无障碍保持开启，而这**本身是一种可检测的指纹**。
对高敏感站点，改用纯截图模式（只 `shot`，不 `observe`）。

---

## 6. comtypes 遍历 UIA 树的崩溃

**症状**：`ControlViewWalker.GetFirstChildElement()` 递归到第 3~4 层时抛
`COMError: 无效指针 (-2147467261)`。

**原因**：comtypes 的元素生命周期管理问题，父元素被提前释放。

**处理**：改用逐层 `FindAll(TreeScope_Children, ControlViewCondition)`，
拿到 `IUIAutomationElementArray` 后再 `GetElement(i)`。
实测 313 个节点、0 失败、250ms。见 `observe.collect_nodes()`。

---

## 7. 双击被识别成两次单击

**症状**：双击不生效，变成了两次单击。

**处理**：两次按下之间的间隔必须小于系统阈值。
用 `GetDoubleClickTime()` 取值，乘以 0.4~0.7 作为实际间隔。
见 `humanize.double_click()`。

---

## 8. 拖拽被识别成点击

**症状**：想拖拽，结果只触发了一次点击。

**原因**：多数界面有"拖拽阈值"——按下后必须先移动几个像素才认定是拖拽。

**处理**：按下后先移动 3~6 像素，停顿一下，再走完整路径。见 `humanize.drag()`。

---

## 9. `GetLastInputInfo` 会被自己的输入污染

**症状**："用户正在使用电脑"的检查在第二步就永远不通过。

**原因**：`SendInput` 发出的输入同样会刷新系统的"最后输入时间"。

**处理**：`safety.enforce_user_idle` 默认关闭。
控制权交给 `Ctrl+Alt+F9` 的允许/禁止开关——那是用户显式表达的意图，不是猜测。

---

## 10. 控制台中文乱码

**症状**：中文元素名输出成 `����`。

**原因**：中文 Windows 的控制台默认用 GBK，而外部按 UTF-8 解码。

**处理**：所有入口脚本开头
`sys.stdout.reconfigure(encoding="utf-8")`。见 `ctl.py` 顶部。

---

## 11. 全局热键被占用

**症状**：`guardd.py` 启动失败，错误码 1409。

**原因**：`RegisterHotKey` 是独占的。中文 Windows 上 `Ctrl+Alt+Space`
通常已被输入法占用。

**处理**：`python guardd.py --scan` 列出空闲组合，填进 `config.json`，
重启守护进程。

---

## 12. 用户和脚本抢鼠标

**症状**：点击落空，或者鼠标被用户拽走。

**原因**：这是真实鼠标方案的固有代价——鼠标只有一个。

**处理**：

- 动手前明确告知用户"我要接管鼠标，按 Ctrl+Alt+Q 急停"
- 单次动作设时间上限（`safety.max_action_seconds`）
- `humanize.move_to()` 移动后会校验光标确实到位，没到位就报错而不是盲目点击

---

## 13. 截图拍到遮挡物

**症状**：截图里有别的窗口盖在上面。

**原因**：`BitBlt` 从屏幕 DC 抓的是**当前显示的内容**，被遮挡的部分拍到的是遮挡物。

**处理**：截图前先 `focus`。这是真实输入方案的固有限制——
必须让浏览器真的显示在屏幕上，而不能像无头浏览器那样在后台跑。
