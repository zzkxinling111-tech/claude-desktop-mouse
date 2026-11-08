"""显著性层 —— 对变化的区域排序，决定「先看哪里」。

这一层把三类信号合成一个分数：

  1. 变化幅度（时间显著性）—— 这块刚才变了多少
  2. 局部对比度（空间显著性）—— 这块和周围差多少。这是人眼最底层的机制：
     视网膜神经元测的不是"这里多亮"，而是"这里和旁边差多少"（中心-周边）
  3. 语义显著性 —— 这块里有没有可交互元素（按钮、输入框），
     或者有没有当前正在找的东西

前两个纯视觉，不需要 UIA；后两个要查元素，每次 1.7ms 左右，
所以默认只对排在前面的少数区域探点。

它只回答"先看哪个"，不回答"这是不是我要的"。后者必须由调用方判断 ——
底层猜业务，误报率会高得没法用。
"""

from __future__ import annotations

import observe

# 权重。改这里就是改"什么样的变化更值得看"。
W_CHANGE = 0.40      # 变了多少
W_CONTRAST = 0.25    # 和周围的反差
W_INTERACTIVE = 0.25  # 里面有没有能点的东西
W_TASK = 0.10        # 是不是我正在找的

# 能被点击/输入的元素类型。这些动起来通常意味着"有东西可做"。
INTERACTIVE_TYPES = {
    "Button", "Hyperlink", "Edit", "CheckBox", "RadioButton",
    "ComboBox", "ListItem", "MenuItem", "TabItem", "Slider",
    "Spinner", "SplitButton", "TreeItem", "DataItem",
}


def _clamp(v: float) -> float:
    return 0.0 if v < 0 else (1.0 if v > 1 else v)


def _cells_in(region, frame) -> list[int]:
    """区域矩形覆盖到的格子索引。"""
    l, t, r, b = region["rect"]
    out = []
    for row in range(frame.rows):
        cl, ct, cr, cb = frame.cell_rect(row, 0)
        cy = (ct + cb) // 2
        if not (t <= cy <= b):
            continue
        for col in range(frame.cols):
            cl, ct, cr, cb = frame.cell_rect(row, col)
            cx = (cl + cr) // 2
            if l <= cx <= r:
                out.append(row * frame.cols + col)
    return out


def change_signal(region) -> float:
    """这一块变了多少。regions() 给的平均灰度差，60 以上就算很强烈。"""
    return _clamp(region.get("score", 0) / 60.0)


def contrast_signal(region, frame) -> float:
    """这一块和周围的差异 —— 中心-周边对比。

    两种都算：区域内部自己的灰度离散度（内容有多"花"），
    以及区域均值与外围一圈的差（它有多"跳出来"）。
    """
    inside = _cells_in(region, frame)
    if not inside:
        return 0.0
    vals = [frame.gray[i] for i in inside]
    mean_in = sum(vals) / len(vals)
    spread = (max(vals) - min(vals)) / 255.0

    # 外圈：整帧里不在区域内的格子取均值
    others = [frame.gray[i] for i in range(len(frame.gray)) if i not in set(inside)]
    if others:
        mean_out = sum(others) / len(others)
        jump = abs(mean_in - mean_out) / 128.0
    else:
        jump = 0.0

    return _clamp(0.5 * spread + 0.5 * jump)


def _probe_points(region) -> list[tuple[int, int]]:
    """在区域里挑几个点去问 UIA：中心优先，再取四角各向内收一点。"""
    l, t, r, b = region["rect"]
    cx, cy = (l + r) // 2, (t + b) // 2
    dx = max(4, (r - l) // 6)
    dy = max(4, (b - t) // 6)
    return [(cx, cy), (cx - dx, cy - dy), (cx + dx, cy - dy),
            (cx - dx, cy + dy), (cx + dx, cy + dy)]


def semantic_signal(region, hwnd, focus: str | None, *, probes: int = 1):
    """问 UIA：这块里能不能点、是不是我要找的。

    probes 是查几个点。1 个点（中心）通常够；查得多更准但更慢。
    只查 hwnd 之外的点会被忽略 —— 区域可能压在浏览器边框上。
    """
    interactive = 0.0
    task = 0.0
    names: list[str] = []
    types: list[str] = []

    for x, y in _probe_points(region)[:probes]:
        try:
            el = observe.element_from_point(x, y)
        except Exception:
            el = None
        if not el:
            continue
        t = el.get("type") or ""
        n = (el.get("name") or "").strip()
        types.append(t)
        if n:
            names.append(n)
        if t in INTERACTIVE_TYPES:
            interactive = 1.0
        if t == "Window" or t == "Pane":
            interactive = min(interactive, 0.0)
        if focus and focus.lower() in n.lower():
            task = 1.0

    return interactive, task, types, names


def rank(regions: list[dict], frame, hwnd=None, focus: str | None = None,
         *, probes: int = 1, semantic_top: int = 5) -> list[dict]:
    """给每个区域算总分并排序。

    hwnd 为 None 时不做语义部分（纯视觉打分），用于不值得查 UIA 的场合。
    semantic_top 限制最多对前几个区域查 UIA —— 每个点约 1.7ms，别让它拖慢扫视。
    """
    ranked = []
    for i, r in enumerate(regions):
        sig = {
            "change": change_signal(r),
            "contrast": contrast_signal(r, frame),
            "interactive": 0.0,
            "task": 0.0,
        }
        item = dict(r)
        item["signal"] = sig
        ranked.append(item)

    # 先按纯视觉分排一次，只给最靠前的几个查语义
    for item in ranked:
        s = item["signal"]
        item["_v"] = W_CHANGE * s["change"] + W_CONTRAST * s["contrast"]
    ranked.sort(key=lambda d: -d["_v"])

    if hwnd is not None:
        for item in ranked[:semantic_top]:
            try:
                inter, task, types, names = semantic_signal(
                    item, hwnd, focus, probes=probes)
            except Exception:
                inter = task = 0.0
                types, names = [], []
            item["signal"]["interactive"] = inter
            item["signal"]["task"] = task
            if types:
                item["element_types"] = types
            if names:
                item["elements"] = names[:3]

    for item in ranked:
        s = item["signal"]
        item["visual_score"] = item.pop("_v", 0.0)
        item["salience"] = round(
            W_CHANGE * s["change"] + W_CONTRAST * s["contrast"]
            + W_INTERACTIVE * s["interactive"] + W_TASK * s["task"], 3)

    ranked.sort(key=lambda d: -d["salience"])
    return ranked
