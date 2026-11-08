"""视觉层 —— 低分辨率抓帧做"扫视"，回答「画面刚才变了哪里」。

定位：这一层对应人眼的周边视觉。低分辨率、覆盖全屏、只看"有没有变"，
不看"是什么"。看懂变化是什么，仍然交给 UIA 和调用方。

三条设计约束，都是实测出来的：

1. 窗口 DC 抓不到内容。Chrome 走 GPU 合成，BitBlt(GetDC(hwnd)) 出来是全黑
   （实测 0/576 非黑像素）。必须从屏幕 DC 抓 DWM 合成后的画面。
   代价是窗口被别的程序遮住时，抓到的是遮挡物。

2. 抓一帧的硬成本约 20ms，瓶颈是读 DWM 表面的像素，跟缩放模式、采样分辨率
   都无关（实测 BitBlt 全尺寸 21ms、StretchBlt 降到 32x18 也是 20ms）。
   所以这一层**按需调用**，不做常驻高频轮询：10Hz 会吃掉 20% 单核。

3. 输出只有区域和分数，没有结论。"这个变化是不是我要的"由调用方判断 ——
   底层猜业务，误报率会高得没法用。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

import winput  # 只为了复用已设置好的 DPI 感知

user32 = winput.user32
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020

DEFAULT_COLS = 32
DEFAULT_ROWS = 18

# 灰度差低于这个值就当没变。页面上的抗锯齿、次像素渲染会在同一块上
# 产生 1~5 的抖动，门槛放到 12 以上才干净。
DEFAULT_THRESHOLD = 12

# 3x3 核，权重和 16。中格最重，四邻次之，角最轻 —— 单格噪声被邻域稀释，
# 成片的变化反而被加权放大。
_KERNEL = ((1, 2, 1), (2, 4, 2), (1, 2, 1))


class _BIH(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BMI(ctypes.Structure):
    _fields_ = [("bmiHeader", _BIH), ("bmiColors", wintypes.DWORD * 3)]


class Frame:
    """一帧低清画面。gray 是行优先的灰度网格。"""

    __slots__ = ("cols", "rows", "gray", "rect", "ts")

    def __init__(self, cols, rows, gray, rect, ts):
        self.cols = cols
        self.rows = rows
        self.gray = gray
        self.rect = rect          # 采样覆盖的屏幕矩形 (l, t, r, b)
        self.ts = ts

    def cell_rect(self, row: int, col: int) -> tuple[int, int, int, int]:
        """第 (row, col) 格在屏幕上对应的矩形。"""
        l, t, r, b = self.rect
        cw, ch = (r - l) / self.cols, (b - t) / self.rows
        return (int(l + col * cw), int(t + row * ch),
                int(l + (col + 1) * cw), int(t + (row + 1) * ch))


# --------------------------------------------------------------------------
# 抓帧设备：全屏大小的 DIB section，建一次反复用
# --------------------------------------------------------------------------

class _Grabber:
    def __init__(self):
        self._screen = None
        self._mem = None
        self._dib = None
        self._bits = None
        self._w = self._h = 0

    def _ensure(self):
        sw = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        sh = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
        if self._dib and (sw, sh) == (self._w, self._h):
            return
        self.close()
        self._screen = user32.GetDC(0)     # 屏幕 DC —— 窗口 DC 抓不到 GPU 内容
        if not self._screen:
            raise OSError("拿不到屏幕 DC。")
        self._mem = gdi32.CreateCompatibleDC(self._screen)
        bi = _BMI()
        h = bi.bmiHeader
        h.biSize = ctypes.sizeof(_BIH)
        h.biWidth, h.biHeight = sw, -sh      # 负高度 = 自上而下
        h.biPlanes, h.biBitCount = 1, 32
        bits = ctypes.c_void_p()
        self._dib = gdi32.CreateDIBSection(
            self._mem, ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        if not self._dib or not bits.value:
            raise OSError("创建 DIB 失败。")
        gdi32.SelectObject(self._mem, self._dib)
        self._bits = bits.value
        self._w, self._h = sw, sh
        # 直接映射这块内存，省掉 GetDIBits 那一次 16MB 拷贝
        self._raw = (ctypes.c_ubyte * (sw * sh * 4)).from_address(bits.value)

    def grab(self) -> tuple[int, int, int, int]:
        """抓一帧到内存，返回虚拟屏幕矩形。"""
        self._ensure()
        ox = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
        oy = user32.GetSystemMetrics(77)
        if not gdi32.BitBlt(self._mem, 0, 0, self._w, self._h,
                            self._screen, 0, 0, SRCCOPY):
            raise OSError("抓屏失败。")
        return (ox, oy, ox + self._w, oy + self._h)

    def sample(self, rect, cols, rows) -> list[int]:
        """在已抓好的帧里按网格取平均灰度（只取中心点，够用且快）。"""
        l, t, r, b = rect
        ox = user32.GetSystemMetrics(76)
        oy = user32.GetSystemMetrics(77)
        cw, ch = (r - l) / cols, (b - t) / rows
        stride = self._w * 4
        raw = self._raw
        out = []
        for row in range(rows):
            y = int(t - oy + (row + 0.5) * ch)
            if y < 0 or y >= self._h:
                out.extend([0] * cols)
                continue
            base = y * stride
            for col in range(cols):
                x = int(l - ox + (col + 0.5) * cw)
                if x < 0 or x >= self._w:
                    out.append(0)
                    continue
                i = base + x * 4
                out.append((raw[i] + raw[i + 1] + raw[i + 2]) // 3)
        return out

    def close(self):
        try:
            if self._dib:
                gdi32.DeleteObject(self._dib)
        except Exception:
            pass
        try:
            if self._mem:
                gdi32.DeleteDC(self._mem)
        except Exception:
            pass
        try:
            if self._screen:
                user32.ReleaseDC(0, self._screen)
        except Exception:
            pass
        self._screen = self._mem = self._dib = self._bits = None
        self._w = self._h = 0


_grabber = None


def _device() -> _Grabber:
    global _grabber
    if _grabber is None:
        _grabber = _Grabber()
    return _grabber


def capture(rect=None, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS) -> Frame:
    """抓一帧低清画面。rect 不给就覆盖整个虚拟屏幕。

    一次调用约 20ms，其中绝大部分花在读屏幕像素上。
    """
    dev = _device()
    screen = dev.grab()
    if rect is None:
        rect = screen
    gray = dev.sample(rect, cols, rows)
    return Frame(cols, rows, gray, tuple(rect), time.time())


# --------------------------------------------------------------------------
# 差分与形态
# --------------------------------------------------------------------------

def compare(a: Frame, b: Frame, threshold: int = DEFAULT_THRESHOLD) -> dict[int, int]:
    """逐格灰度差。返回 {格子索引: 差值}，只保留超过门槛的。"""
    if (a.cols, a.rows) != (b.cols, b.rows):
        raise ValueError("两帧的网格尺寸不一致，不能比较。")
    out = {}
    for i, (x, y) in enumerate(zip(a.gray, b.gray)):
        d = x - y
        if d < 0:
            d = -d
        if d >= threshold:
            out[i] = d
    return out


def smooth(cells: dict[int, int], cols: int, rows: int) -> dict[int, int]:
    """对差分图做一次 3x3 加权卷积。

    作用是把孤立的单格噪声压下去、把成片的变化抬起来 —— 判断"这一块算不算
    真的变了"时参考的是邻域，不是某个像素自己说了算。
    """
    if not cells:
        return {}
    out = {}
    for idx in cells:
        r, c = divmod(idx, cols)
        acc = 0
        for dr in (-1, 0, 1):
            rr = r + dr
            if rr < 0 or rr >= rows:
                continue
            for dc in (-1, 0, 1):
                cc = c + dc
                if cc < 0 or cc >= cols:
                    continue
                v = cells.get(rr * cols + cc)
                if v:
                    acc += v * _KERNEL[dr + 1][dc + 1]
        if acc:
            out[idx] = acc // 16
    return out


def regions(cells: dict[int, int], frame: Frame, *, min_score: int = 8,
            gap: int = 1) -> list[dict]:
    """把零散的格子合并成矩形区域，按显著度排序。

    gap 允许相邻 but 不相接的格子并成一块 —— 页面上的一次变化（比如弹出一个
    提示框）在 32x18 的网格里往往被切成几片，不合起来就看不出是一件事。
    """
    keep = {i: v for i, v in cells.items() if v >= min_score}
    if not keep:
        return []

    cols, rows = frame.cols, frame.rows
    seen = set()
    out = []
    for start in sorted(keep, key=lambda i: -keep[i]):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        cells_in = []
        while stack:
            i = stack.pop()
            cells_in.append(i)
            r, c = divmod(i, cols)
            for dr in range(-gap, gap + 1):
                for dc in range(-gap, gap + 1):
                    rr, cc = r + dr, c + dc
                    if rr < 0 or rr >= rows or cc < 0 or cc >= cols:
                        continue
                    j = rr * cols + cc
                    if j in keep and j not in seen:
                        seen.add(j)
                        stack.append(j)
        rs = [divmod(i, cols) for i in cells_in]
        r0 = min(x[0] for x in rs); r1 = max(x[0] for x in rs)
        c0 = min(x[1] for x in rs); c1 = max(x[1] for x in rs)
        l = frame.cell_rect(r0, c0)[0]
        t = frame.cell_rect(r0, c0)[1]
        r = frame.cell_rect(r1, c1)[2]
        b = frame.cell_rect(r1, c1)[3]
        out.append({
            "rect": (l, t, r, b),
            "cells": len(cells_in),
            "score": sum(keep[i] for i in cells_in) // len(cells_in),
        })
    out.sort(key=lambda d: -(d["cells"] * d["score"]))
    return out


def change_map(before: Frame, after: Frame, *, threshold: int = DEFAULT_THRESHOLD,
               min_score: int = 8) -> list[dict]:
    """一步到位：两帧之间变了哪些区域，按显著度排序。"""
    cells = smooth(compare(before, after, threshold), after.cols, after.rows)
    return regions(cells, after, min_score=min_score)
