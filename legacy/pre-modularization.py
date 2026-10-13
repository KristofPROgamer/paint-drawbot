#!/usr/bin/env python3
"""
Paint DrawBot  v5.0
─────────────────────────────────────────────────────────────────────────────
Draws any image inside MS Paint using horizontal drag-strokes grouped by
colour (run-length encoding).

Requirements:
    pip install pillow pyautogui keyboard pywin32

Usage:
    1. Open MS Paint.  Select the PENCIL tool.  Note your brush size in px.
    2. Run this script.
    3. Click [ CALIBRATE ] and follow on-screen prompts (press SPACE).
    4. Set BRUSH SIZE to match your actual pencil size in Paint.
    5. Select an image, pick detail level, click Start.
    6. Open [ DEBUG TIMING ] to tune every delay live, even mid-draw.
    7. Press ESC to pause/resume.  Emergency abort: mouse to top-left corner.

v5.0 additions over v4.0:
    • Live Timing Debug Panel — Toplevel popup with sliders for every delay
      in the pipeline (14 params).  Adjustments take effect immediately,
      even while drawing is in progress.
    • ⚡ TURBO PRESET — one click sets all delays to minimum safe values.
      Biggest win: Skip Pencil Re-arm (saves ~350 ms per colour change).
    • Brush-size awareness — set your actual pencil size (1–8 px).  Step is
      auto-clamped so rows never have gaps.  Sanity check warns you if your
      detail level would leave visible gaps.
    • All formerly hard-coded delay constants replaced by tk.IntVar fields
      read in real time by the mouse/stroke functions via a module-level
      DelayConfig singleton.
    • _focus_paint delay and re-focus interval now also live-tunable.
    • skip_pencil_rearm toggle — disables the expensive pencil re-arm click
      after every palette swatch selection.  Safe on Win10/11 Paint; test
      with [ TEST STROKE ] first.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

# ── DPI awareness FIRST ────────────────────────────────────────────────────────
import ctypes
import ctypes.wintypes as _wintypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ── Stdlib ─────────────────────────────────────────────────────────────────────
import argparse
import json
import logging
import os
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from enum import Enum
from logging.handlers import RotatingFileHandler
from tkinter import filedialog, ttk

# ── Third-party ────────────────────────────────────────────────────────────────
import keyboard
import numpy as np
import win32api
import win32con
import win32gui
from PIL import Image, ImageTk

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "paint_drawbot.log")
_DEBUG_LOG = os.environ.get("DRAWBOT_DEBUG", "").strip().lower() in {
    "1", "true", "yes", "on",
}

_log = logging.getLogger("drawbot")
_log.setLevel(logging.DEBUG if _DEBUG_LOG else logging.INFO)
_fmt = logging.Formatter(
    "%(asctime)s.%(msecs)03d  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"
)
_fh = RotatingFileHandler(_LOG_FILE, maxBytes=2 * 1024 * 1024,
                           backupCount=3, encoding="utf-8")
_fh.setLevel(logging.DEBUG if _DEBUG_LOG else logging.INFO)
_fh.setFormatter(_fmt)
_log.addHandler(_fh)
_ch = logging.StreamHandler(sys.stdout)
_ch.setLevel(logging.INFO)
_ch.setFormatter(_fmt)
_log.addHandler(_ch)
_log.info("=" * 70)
_log.info("Paint DrawBot v5.0 starting")
_log.info("Log file: %s", _LOG_FILE)

# ── Paths ──────────────────────────────────────────────────────────────────────

CAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "paint_drawbot_cal.json")
CAL_SCHEMA_VERSION = 3


class DitherPreset(Enum):
    SMART = "Smart ordered"
    PHOTO = "Photo exact"
    CLEAN = "Clean mono"
    FLAT = "Flat"


class PlannerMode(Enum):
    AUTO = "Auto"
    FAST = "Fast"
    EXACT = "Exact"


class AutoBrushMode(Enum):
    OFF = "Off"
    SAFE = "Safe regions"
    AGGRESSIVE = "Aggressive"


BRUSH_SIZE_STOPS = (
    tuple(range(1, 25))
    + tuple(range(26, 105, 2))
    + tuple(range(108, 173, 4))
    + tuple(range(180, 245, 8))
    + (248,)
)
BRUSH_LADDER_TEST_SIZES = (1, 8, 24, 48, 104, 172, 248)


def _nearest_brush_size(px: int) -> int:
    return min(BRUSH_SIZE_STOPS, key=lambda stop: (abs(stop - px), stop))


def _brush_stop_index(px: int) -> int:
    snapped = _nearest_brush_size(px)
    return BRUSH_SIZE_STOPS.index(snapped)


@dataclass(frozen=True)
class BrushSizeCalibration:
    size_button: tuple[int, int]
    slider_bottom: tuple[int, int]
    slider_top: tuple[int, int]
    min_px: int = 1
    max_px: int = 248

    def slider_pos_for_size(self, px: int) -> tuple[int, int]:
        snapped = _nearest_brush_size(px)
        denom = max(1, len(BRUSH_SIZE_STOPS) - 1)
        t = _brush_stop_index(snapped) / denom
        bx, by = self.slider_bottom
        tx, ty = self.slider_top
        return (
            int(round(bx + (tx - bx) * t)),
            int(round(by + (ty - by) * t)),
        )


@dataclass(frozen=True)
class DrawSettings:
    dither: DitherPreset
    planner: PlannerMode
    auto_brush: AutoBrushMode
    max_auto_brush: int
    base_brush: int
    step: int
    grayscale_only: bool


@dataclass(frozen=True)
class BrushAction:
    brush_px: int
    colour_idx: int
    gy: int
    gx_start: int
    gx_end: int
    cover_cells: int


@dataclass
class IndexedImageResult:
    pixel_indices: bytes
    dither: DitherPreset
    fast_used: bool = False
    stats: dict[str, float | int | bool | str] = field(default_factory=dict)


@dataclass
class PlanStats:
    auto_brush_actions: int = 0
    auto_brush_pixels: int = 0
    auto_brush_sizes: list[int] = field(default_factory=list)
    planner_mode: str = PlannerMode.AUTO.value

# ── Base Palette ───────────────────────────────────────────────────────────────

BASE_PALETTE = [
    # Row 1
    {"name": "Black",           "rgb": (  0,   0,   0), "grid": (1,  1)},
    {"name": "Dark Grey",       "rgb": ( 64,  64,  64), "grid": (1,  2)},
    {"name": "Dark Red",        "rgb": (128,   0,   0), "grid": (1,  3)},
    {"name": "Red",             "rgb": (255,   0,   0), "grid": (1,  4)},
    {"name": "Orange",          "rgb": (255, 128,   0), "grid": (1,  5)},
    {"name": "Yellow",          "rgb": (255, 255,   0), "grid": (1,  6)},
    {"name": "Green",           "rgb": (  0, 128,   0), "grid": (1,  7)},
    {"name": "Turquoise",       "rgb": (  0, 255, 255), "grid": (1,  8)},
    {"name": "Indigo",          "rgb": ( 75,   0, 130), "grid": (1,  9)},
    {"name": "Purple",          "rgb": (128,   0, 128), "grid": (1, 10)},
    # Row 2
    {"name": "White",           "rgb": (255, 255, 255), "grid": (2,  1)},
    {"name": "Light Grey",      "rgb": (192, 192, 192), "grid": (2,  2)},
    {"name": "Brown",           "rgb": (139,  69,  19), "grid": (2,  3)},
    {"name": "Pink",            "rgb": (255, 105, 180), "grid": (2,  4)},
    {"name": "Gold",            "rgb": (255, 215,   0), "grid": (2,  5)},
    {"name": "Light Yellow",    "rgb": (255, 255, 128), "grid": (2,  6)},
    {"name": "Lime Green",      "rgb": (  0, 255,   0), "grid": (2,  7)},
    {"name": "Light Turquoise", "rgb": (128, 255, 255), "grid": (2,  8)},
    {"name": "Blue-Grey",       "rgb": ( 96, 128, 160), "grid": (2,  9)},
    {"name": "Lavender",        "rgb": (192, 128, 255), "grid": (2, 10)},
]

# ── Global control ─────────────────────────────────────────────────────────────

_pause_event = threading.Event()
_stop_event  = threading.Event()
_hotkeys_registered = False


def _request_stop() -> None:
    _log.info("HOTKEY: CTRL+ESC -> stop requested")
    _stop_event.set()
    _pause_event.clear()


def _toggle_pause() -> None:
    if _pause_event.is_set():
        _log.info("HOTKEY: ESC → resuming")
        _pause_event.clear()
    else:
        _log.info("HOTKEY: ESC → pausing")
        _pause_event.set()


def _register_hotkeys() -> None:
    global _hotkeys_registered
    if _hotkeys_registered:
        return
    keyboard.add_hotkey("esc", _toggle_pause)
    keyboard.add_hotkey("ctrl+esc", _request_stop)
    _hotkeys_registered = True

# ─────────────────────────────────────────────────────────────────────────────
# DELAY CONFIG  — module-level singleton; all timing params live here.
# Mouse/stroke functions read these in real time so the Debug Panel can
# adjust them while drawing is in progress.
# ─────────────────────────────────────────────────────────────────────────────

class DelayConfig:
    """
    All timing parameters backed by tk.IntVar so the Debug Panel can
    bind sliders directly and changes propagate instantly.

    Created once in DrawBotApp.__init__() and stored as the module-level
    _cfg global so the module-level mouse/stroke functions can read it.
    """

    # ── defaults ───────────────────────────────────────────────────────────────
    DEFAULTS = {
        "move_settle_ms":     0,   # cursor settle after _mk_move, before LEFTDOWN
        "press_settle_ms":    0,   # after LEFTDOWN before drag starts
        "post_step_ms":       0,   # after last drag step, before LEFTUP
        "release_settle_ms":  0,   # after LEFTUP
        "min_stroke_ms":      0,   # floor for total stroke duration
        "drag_steps":         1,   # intermediate SendInput moves during drag

        "click_pre_down_ms":  0,   # move-to-position → LEFTDOWN
        "click_pre_up_ms":    1,   # LEFTDOWN → LEFTUP
        "click_post_ms":      3,   # after LEFTUP (default; callers can override)

        "pal_delay_ms":       4,   # after palette swatch click
        "pencil_rearm_ms":   20,   # after pencil re-arm click
        # skip_pencil_rearm is a BooleanVar (not int) — handled separately

        "focus_delay_ms":    60,   # general Paint focus delay
        "focus_interval":  1000,   # re-focus check every N rows/strokes
    }

    # Turbo: minimum safe values that still produce visible strokes on most machines
    TURBO = {
        "move_settle_ms":     0,
        "press_settle_ms":    1,
        "post_step_ms":       0,
        "release_settle_ms":  0,
        "min_stroke_ms":      0,
        "drag_steps":         1,
        "click_pre_down_ms":  0,
        "click_pre_up_ms":    1,
        "click_post_ms":      3,
        "pal_delay_ms":       4,
        "pencil_rearm_ms":   20,
        "focus_delay_ms":    60,
        "focus_interval":  1000,
    }

    def __init__(self) -> None:
        self.move_settle_ms    = tk.IntVar(value=self.DEFAULTS["move_settle_ms"])
        self.press_settle_ms   = tk.IntVar(value=self.DEFAULTS["press_settle_ms"])
        self.post_step_ms      = tk.IntVar(value=self.DEFAULTS["post_step_ms"])
        self.release_settle_ms = tk.IntVar(value=self.DEFAULTS["release_settle_ms"])
        self.min_stroke_ms     = tk.IntVar(value=self.DEFAULTS["min_stroke_ms"])
        self.drag_steps        = tk.IntVar(value=self.DEFAULTS["drag_steps"])

        self.click_pre_down_ms = tk.IntVar(value=self.DEFAULTS["click_pre_down_ms"])
        self.click_pre_up_ms   = tk.IntVar(value=self.DEFAULTS["click_pre_up_ms"])
        self.click_post_ms     = tk.IntVar(value=self.DEFAULTS["click_post_ms"])

        self.pal_delay_ms      = tk.IntVar(value=self.DEFAULTS["pal_delay_ms"])
        self.pencil_rearm_ms   = tk.IntVar(value=self.DEFAULTS["pencil_rearm_ms"])
        self.skip_pencil_rearm = tk.BooleanVar(value=True)

        self.focus_delay_ms    = tk.IntVar(value=self.DEFAULTS["focus_delay_ms"])
        self.focus_interval    = tk.IntVar(value=self.DEFAULTS["focus_interval"])

    def apply(self, preset: dict) -> None:
        mapping = {
            "move_settle_ms":    self.move_settle_ms,
            "press_settle_ms":   self.press_settle_ms,
            "post_step_ms":      self.post_step_ms,
            "release_settle_ms": self.release_settle_ms,
            "min_stroke_ms":     self.min_stroke_ms,
            "drag_steps":        self.drag_steps,
            "click_pre_down_ms": self.click_pre_down_ms,
            "click_pre_up_ms":   self.click_pre_up_ms,
            "click_post_ms":     self.click_post_ms,
            "pal_delay_ms":      self.pal_delay_ms,
            "pencil_rearm_ms":   self.pencil_rearm_ms,
            "focus_delay_ms":    self.focus_delay_ms,
            "focus_interval":    self.focus_interval,
        }
        for k, v in preset.items():
            if k in mapping:
                mapping[k].set(v)


# Module-level singleton — assigned in DrawBotApp.__init__()
_cfg: DelayConfig  # type: ignore[assignment]

# ─────────────────────────────────────────────────────────────────────────────
# LOW-LEVEL MOUSE INPUT  (pure SendInput pipeline)
# ─────────────────────────────────────────────────────────────────────────────

_INPUT_MOUSE = 0


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx",          ctypes.c_long),
        ("dy",          ctypes.c_long),
        ("mouseData",   ctypes.c_ulong),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("_input", _INPUT_UNION)]


_SM_XVIRTUALSCREEN  = 76
_SM_YVIRTUALSCREEN  = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

_virt_x = win32api.GetSystemMetrics(_SM_XVIRTUALSCREEN)
_virt_y = win32api.GetSystemMetrics(_SM_YVIRTUALSCREEN)
_virt_w = win32api.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
_virt_h = win32api.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
_norm_sx = 65535.0 / max(1, _virt_w - 1)
_norm_sy = 65535.0 / max(1, _virt_h - 1)
_norm_ox = _virt_x
_norm_oy = _virt_y
_norm_xmax = _virt_x + _virt_w - 1
_norm_ymax = _virt_y + _virt_h - 1
_NULL_EXTRA = ctypes.POINTER(ctypes.c_ulong)()
_SEND_INPUT_CHUNK = 4096
_MOUSEEVENTF_MOVE = win32con.MOUSEEVENTF_MOVE
_MOUSEEVENTF_ABSOLUTE = win32con.MOUSEEVENTF_ABSOLUTE
_MOUSEEVENTF_LEFTDOWN = win32con.MOUSEEVENTF_LEFTDOWN
_MOUSEEVENTF_LEFTUP = win32con.MOUSEEVENTF_LEFTUP
_MOUSEEVENTF_MOVE_NOCOALESCE = getattr(
    win32con, "MOUSEEVENTF_MOVE_NOCOALESCE", 0x2000
)
# PERF: Precompute the four hot-path SendInput flag combinations once.
_MOUSE_MOVE_FLAGS = _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE
_MOUSE_MOVE_NC_FLAGS = _MOUSE_MOVE_FLAGS | _MOUSEEVENTF_MOVE_NOCOALESCE
_MOUSE_MOVE_LEFTDOWN_FLAGS = _MOUSE_MOVE_FLAGS | _MOUSEEVENTF_LEFTDOWN
_MOUSE_MOVE_LEFTDOWN_NC_FLAGS = (
    _MOUSE_MOVE_LEFTDOWN_FLAGS | _MOUSEEVENTF_MOVE_NOCOALESCE
)

_log.info("Virtual screen: origin=(%d,%d)  size=%dx%d",
          _virt_x, _virt_y, _virt_w, _virt_h)


def _norm(px: int, py: int) -> tuple[int, int]:
    # PERF: Virtual screen scale never changes, so use multiply-by-reciprocal.
    nx = int((px - _norm_ox) * _norm_sx)
    ny = int((py - _norm_oy) * _norm_sy)
    if _norm_ox <= px <= _norm_xmax and _norm_oy <= py <= _norm_ymax:
        return nx, ny
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def _send_input_events(events) -> None:
    total = len(events)
    sent_total = 0
    while sent_total < total:
        chunk = events[sent_total:sent_total + _SEND_INPUT_CHUNK]
        arr = (_INPUT * len(chunk))(*chunk)
        sent = ctypes.windll.user32.SendInput(
            len(chunk), arr, ctypes.sizeof(_INPUT)
        )
        if sent <= 0:
            _log.warning(
                "SendInput: requested %d, sent %d before failure",
                total, sent_total,
            )
            return
        if sent != len(chunk):
            _log.warning("SendInput: partial chunk requested %d, sent %d",
                         len(chunk), sent)
        sent_total += sent


def _send_input(*events: _INPUT) -> None:
    _send_input_events(events)


def _nap(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


def _mk_move(px: int, py: int, no_coalesce: bool = False) -> _INPUT:
    nx, ny = _norm(px, py)
    flags = _MOUSE_MOVE_NC_FLAGS if no_coalesce else _MOUSE_MOVE_FLAGS
    mi     = _MOUSEINPUT(nx, ny, 0,
                         flags, 0, _NULL_EXTRA)
    return _INPUT(_INPUT_MOUSE, _INPUT_UNION(mi=mi))


def _mk_move_button(
        px: int,
        py: int,
        button_flags: int,
        no_coalesce: bool = False) -> _INPUT:
    nx, ny = _norm(px, py)
    if button_flags == _MOUSEEVENTF_LEFTDOWN:
        flags = (
            _MOUSE_MOVE_LEFTDOWN_NC_FLAGS
            if no_coalesce else _MOUSE_MOVE_LEFTDOWN_FLAGS
        )
    elif button_flags == 0:
        flags = _MOUSE_MOVE_NC_FLAGS if no_coalesce else _MOUSE_MOVE_FLAGS
    else:
        flags = _MOUSE_MOVE_FLAGS | button_flags
        if no_coalesce:
            flags |= _MOUSEEVENTF_MOVE_NOCOALESCE
    mi = _MOUSEINPUT(nx, ny, 0, flags, 0, _NULL_EXTRA)
    return _INPUT(_INPUT_MOUSE, _INPUT_UNION(mi=mi))


def _mk_button(flags: int) -> _INPUT:
    mi    = _MOUSEINPUT(0, 0, 0, flags, 0, _NULL_EXTRA)
    return _INPUT(_INPUT_MOUSE, _INPUT_UNION(mi=mi))


_LEFT_DOWN_INPUT = _mk_button(_MOUSEEVENTF_LEFTDOWN)
_LEFT_UP_INPUT = _mk_button(_MOUSEEVENTF_LEFTUP)


def _mouse_click(
        px: int,
        py: int,
        delay: float | None = None,
        pre_down: float | None = None,
        pre_up: float | None = None) -> None:
    """Move → LEFTDOWN → LEFTUP, all timings from _cfg."""
    d = delay if delay is not None else _cfg.click_post_ms.get() / 1000.0
    pre_down_s = (
        pre_down if pre_down is not None else _cfg.click_pre_down_ms.get() / 1000.0
    )
    pre_up_s = pre_up if pre_up is not None else _cfg.click_pre_up_ms.get() / 1000.0
    _log.debug("  click → (%d,%d)  post=%.3fs", px, py, d)

    if pre_down_s <= 0 and pre_up_s <= 0 and d <= 0:
        _send_input(
            _mk_move_button(px, py, _MOUSEEVENTF_LEFTDOWN),
            _LEFT_UP_INPUT,
        )
        return

    _send_input(_mk_move(px, py))
    _nap(pre_down_s)
    _send_input(_LEFT_DOWN_INPUT)
    _nap(pre_up_s)
    _send_input(_LEFT_UP_INPUT)
    _nap(d)


def _stroke_line(x1: int, y1: int, x2: int, y2: int, duration: float) -> None:
    """
    Draw a straight stroke from (x1,y1) to (x2,y2).
    All timings read live from _cfg so the Debug Panel can tune mid-draw.
    """
    dur   = max(_cfg.min_stroke_ms.get() / 1000.0, duration)
    steps = max(1, _cfg.drag_steps.get())
    move_s = _cfg.move_settle_ms.get() / 1000.0
    press_s = _cfg.press_settle_ms.get() / 1000.0
    post_s = _cfg.post_step_ms.get() / 1000.0
    release_s = _cfg.release_settle_ms.get() / 1000.0
    _log.debug("  stroke (%d,%d)->(%d,%d)  dur=%.3fs steps=%d",
               x1, y1, x2, y2, dur, steps)

    if (steps == 1 and dur <= 0 and move_s <= 0 and press_s <= 0
            and post_s <= 0 and release_s <= 0):
        _send_input_events(_stroke_events(x1, y1, x2, y2))
        return

    _send_input(_mk_move(x1, y1))
    _nap(move_s)

    _send_input(_LEFT_DOWN_INPUT)
    _nap(press_s)

    if x2 != x1 or y2 != y1:
        step_dur = dur / steps
        for i in range(1, steps + 1):
            xi = x1 + (x2 - x1) * i // steps
            yi = y1 + (y2 - y1) * i // steps
            _send_input(_mk_move(xi, yi))
            _nap(step_dur)
    elif dur > 0:
        _nap(dur)

    _nap(post_s)
    _send_input(_LEFT_UP_INPUT)
    _nap(release_s)


def _stroke(x1: int, y: int, x2: int, duration: float) -> None:
    """Backward-compatible horizontal stroke wrapper."""
    _stroke_line(x1, y, x2, y, duration)


def _stroke_path(points: list[tuple[int, int]], duration: float) -> None:
    if not points:
        return
    if len(points) == 1:
        x, y = points[0]
        _stroke_line(x, y, x, y, duration)
        return

    dur = max(_cfg.min_stroke_ms.get() / 1000.0, duration)
    move_s = _cfg.move_settle_ms.get() / 1000.0
    press_s = _cfg.press_settle_ms.get() / 1000.0
    post_s = _cfg.post_step_ms.get() / 1000.0
    release_s = _cfg.release_settle_ms.get() / 1000.0
    step_dur = dur / max(1, len(points) - 1)

    x0, y0 = points[0]
    _send_input(_mk_move(x0, y0, no_coalesce=True))
    _nap(move_s)
    _send_input(_LEFT_DOWN_INPUT)
    _nap(press_s)
    for x, y in points[1:]:
        _send_input(_mk_move(x, y, no_coalesce=True))
        _nap(step_dur)
    _nap(post_s)
    _send_input(_LEFT_UP_INPUT)
    _nap(release_s)


# ── Colour math ────────────────────────────────────────────────────────────────

def _can_batch_strokes(duration: float) -> bool:
    return (
        max(_cfg.min_stroke_ms.get() / 1000.0, duration) <= 0
        and max(1, _cfg.drag_steps.get()) == 1
        and _cfg.move_settle_ms.get() <= 0
        and _cfg.press_settle_ms.get() <= 0
        and _cfg.post_step_ms.get() <= 0
        and _cfg.release_settle_ms.get() <= 0
    )


def _stroke_events(x1: int, y1: int, x2: int,
                   y2: int | None = None) -> tuple[_INPUT, ...]:
    y2 = y1 if y2 is None else y2
    if x2 == x1 and y2 == y1:
        return (
            _mk_move_button(x1, y1, _MOUSEEVENTF_LEFTDOWN),
            _LEFT_UP_INPUT,
        )
    return (
        _mk_move_button(x1, y1, _MOUSEEVENTF_LEFTDOWN),
        _mk_move(x2, y2),
        _LEFT_UP_INPUT,
    )


def _append_stroke_events(
    out: list[_INPUT],
        x1: int,
        y1: int,
        x2: int,
        y2: int | None = None) -> None:
    y2 = y1 if y2 is None else y2
    out.append(_mk_move_button(x1, y1, _MOUSEEVENTF_LEFTDOWN))
    if x2 != x1 or y2 != y1:
        out.append(_mk_move(x2, y2))
    out.append(_LEFT_UP_INPUT)


def _stroke_path_events(points: list[tuple[int, int]]) -> tuple[_INPUT, ...]:
    if not points:
        return ()
    first_x, first_y = points[0]
    events: list[_INPUT] = [
        _mk_move_button(
            first_x, first_y, _MOUSEEVENTF_LEFTDOWN, no_coalesce=True
        ),
    ]
    for x, y in points[1:]:
        events.append(_mk_move(x, y, no_coalesce=True))
    events.append(_LEFT_UP_INPUT)
    return tuple(events)


def _append_stroke_path_events(
        out: list[_INPUT],
        points: list[tuple[int, int]]) -> None:
    if not points:
        return
    first_x, first_y = points[0]
    out.append(_mk_move_button(
        first_x, first_y, _MOUSEEVENTF_LEFTDOWN, no_coalesce=True
    ))
    for x, y in points[1:]:
        out.append(_mk_move(x, y, no_coalesce=True))
    out.append(_LEFT_UP_INPUT)


def _send_stroke_batch(events: list[_INPUT]) -> None:
    if events:
        _send_input_events(events)


class _InputBatch:
    __slots__ = ("_events", "_capacity", "count", "strokes")

    def __init__(self, capacity: int = _SEND_INPUT_CHUNK) -> None:
        self._events = (_INPUT * capacity)()
        self._capacity = capacity
        self.count = 0
        self.strokes = 0

    def _ensure(self, needed: int) -> int:
        if self.count + needed > self._capacity:
            return self.flush()
        return 0

    def _write_raw(
            self,
            nx: int,
            ny: int,
            flags: int,
            input_mouse: int = _INPUT_MOUSE,
            null_extra = _NULL_EXTRA) -> None:
        # Reuse one contiguous ctypes array to avoid per-event heap churn.
        count = self.count
        slot = self._events[count]
        slot.type = input_mouse
        mi = slot._input.mi
        mi.dx = nx
        mi.dy = ny
        mi.mouseData = 0
        mi.dwFlags = flags
        mi.time = 0
        mi.dwExtraInfo = null_extra
        self.count = count + 1

    def _write_move(
            self,
            px: int,
            py: int,
            button_flags: int = 0,
            no_coalesce: bool = False,
            norm = _norm,
            leftdown: int = _MOUSEEVENTF_LEFTDOWN) -> None:
        nx, ny = norm(px, py)
        # PERF: Four if/elif branches beat hashing a dict for this tiny flag set.
        if button_flags == 0:
            flags = _MOUSE_MOVE_NC_FLAGS if no_coalesce else _MOUSE_MOVE_FLAGS
        elif button_flags == leftdown:
            flags = (
                _MOUSE_MOVE_LEFTDOWN_NC_FLAGS
                if no_coalesce else _MOUSE_MOVE_LEFTDOWN_FLAGS
            )
        else:
            flags = _MOUSE_MOVE_FLAGS | button_flags
            if no_coalesce:
                flags |= _MOUSEEVENTF_MOVE_NOCOALESCE
        write_raw = self._write_raw
        write_raw(nx, ny, flags)

    def _append_stroke_nonnull(
            self,
            x1: int,
            y1: int,
            x2: int,
            y2: int) -> int:
        flushed = self._ensure(2 if (x1 == x2 and y1 == y2) else 3)
        # PERF: Local bindings avoid repeated method/global lookups per stroke.
        write_move = self._write_move
        write_raw = self._write_raw
        leftdown = _MOUSEEVENTF_LEFTDOWN
        leftup = _MOUSEEVENTF_LEFTUP
        write_move(x1, y1, leftdown)
        if x2 != x1 or y2 != y1:
            write_move(x2, y2)
        write_raw(0, 0, leftup)
        self.strokes += 1
        return flushed

    def append_stroke(
            self,
            x1: int,
            y1: int,
            x2: int,
            y2: int | None = None) -> int:
        y2 = y1 if y2 is None else y2
        return self._append_stroke_nonnull(x1, y1, x2, y2)

    def append_path(self, points: list[tuple[int, int]]) -> int:
        if not points:
            return 0
        needed = len(points) + 1
        if needed > self._capacity:
            flushed = self.flush()
            _send_input_events(_stroke_path_events(points))
            return flushed + 1
        flushed = self._ensure(needed)
        first_x, first_y = points[0]
        # PERF: Local bindings avoid repeated method/global lookups in path loops.
        write_move = self._write_move
        write_raw = self._write_raw
        leftdown = _MOUSEEVENTF_LEFTDOWN
        leftup = _MOUSEEVENTF_LEFTUP
        write_move(first_x, first_y, leftdown, no_coalesce=True)
        for x, y in points[1:]:
            write_move(x, y, no_coalesce=True)
        write_raw(0, 0, leftup)
        self.strokes += 1
        return flushed

    def flush(self) -> int:
        if self.count <= 0:
            return 0
        sent = ctypes.windll.user32.SendInput(
            self.count, self._events, ctypes.sizeof(_INPUT)
        )
        if sent <= 0:
            _log.warning(
                "SendInput: requested %d batched events before failure",
                self.count,
            )
        elif sent != self.count:
            _log.warning("SendInput: partial fixed batch requested %d, sent %d",
                         self.count, sent)
        done = self.strokes
        self.count = 0
        self.strokes = 0
        return done


def _colour_dist_sq(r1: int, g1: int, b1: int,
                    r2: int, g2: int, b2: int) -> float:
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return 0.299 * dr * dr + 0.587 * dg * dg + 0.114 * db * db


# ── Win32 helpers ──────────────────────────────────────────────────────────────

def _looks_like_paint_title(title: str) -> bool:
    text = title.strip()
    low = text.lower()
    if not text or "drawbot" in low or "timing debug" in low:
        return False
    return low == "paint" or low.endswith(" - paint")


def _find_paint_hwnd() -> int | None:
    found: list[tuple[int, str]] = []

    def _cb(hwnd: int, _: object) -> bool:
        if win32gui.IsWindowVisible(hwnd):
            t = win32gui.GetWindowText(hwnd)
            if _looks_like_paint_title(t):
                found.append((hwnd, t))
        return True

    win32gui.EnumWindows(_cb, None)
    if found:
        _log.info("Found Paint windows: %s", found)
        found.sort(key=lambda item: item[1].strip().lower() != "paint")
        return found[0][0]
    _log.warning("No Paint window found")
    return None


def _focus_paint(hwnd: int, delay: float | None = None) -> None:
    d = delay if delay is not None else _cfg.focus_delay_ms.get() / 1000.0
    _log.debug("Focusing Paint hwnd=%d  delay=%.3fs", hwnd, d)
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as exc:
        _log.warning("SetForegroundWindow failed: %s", exc)
    _nap(d)
    fg = win32gui.GetForegroundWindow()
    _log.debug("Foreground after focus: %d (wanted %d) match=%s", fg, hwnd, fg == hwnd)


def _paint_has_focus(hwnd: int) -> bool:
    return win32gui.GetForegroundWindow() == hwnd


def _log_paint_rect(hwnd: int) -> None:
    try:
        rect  = win32gui.GetWindowRect(hwnd)
        crect = win32gui.GetClientRect(hwnd)
        pt    = win32gui.ClientToScreen(hwnd, (0, 0))
        _log.info("Paint window rect: %s", rect)
        _log.info("Paint client rect: %s  client-origin-screen: %s", crect, pt)
    except Exception as exc:
        _log.warning("Could not read Paint rect: %s", exc)


# ── Colour lookup ──────────────────────────────────────────────────────────────

def _closest_colour(palette: list[dict], pal_rgb: list[tuple],
                    r: int, g: int, b: int) -> dict:
    return palette[_closest_colour_idx(pal_rgb, r, g, b)]


def _closest_colour_idx(pal_rgb: list[tuple], r: int, g: int, b: int) -> int:
    best_idx, best_d = 0, 1e18
    for i, (pr, pg, pb) in enumerate(pal_rgb):
        d = _colour_dist_sq(r, g, b, pr, pg, pb)
        if d < best_d:
            best_d, best_idx = d, i
            if d == 0:
                break
    return best_idx


# ─────────────────────────────────────────────────────────────────────────────
def _palette_index_data(img: Image.Image, pal_rgb: list[tuple],
                        dither: bool = False) -> bytes:
    pal_img = Image.new("P", (1, 1))
    flat_palette: list[int] = []
    for r, g, b in pal_rgb:
        flat_palette.extend([int(r), int(g), int(b)])
    flat_palette.extend([0, 0, 0] * (256 - len(pal_rgb)))
    pal_img.putpalette(flat_palette)
    dither_mode = Image.Dither.FLOYDSTEINBERG if dither else Image.Dither.NONE
    indexed = img.quantize(palette=pal_img, dither=dither_mode)
    return indexed.tobytes()


_NP_RGB_WEIGHTS = np.array([0.299, 0.587, 0.114], dtype=np.float32)
_BAYER4 = (np.array(
    [[0, 8, 2, 10],
     [12, 4, 14, 6],
     [3, 11, 1, 9],
     [15, 7, 13, 5]],
    dtype=np.float32,
) + 0.5) / 16.0 - 0.5


def _nearest_palette_indices_np(
        rgb_arr: np.ndarray,
        pal_arr: np.ndarray) -> np.ndarray:
    flat = rgb_arr.reshape(-1, 3)
    out = np.empty(flat.shape[0], dtype=np.uint8)
    weights = _NP_RGB_WEIGHTS
    chunk_size = 65536
    # PERF: Chunked NumPy distance keeps the fast dither path vectorized without
    # building one huge pixels x palette temporary for large canvases.
    for start in range(0, flat.shape[0], chunk_size):
        end = min(flat.shape[0], start + chunk_size)
        diff = flat[start:end, None, :] - pal_arr[None, :, :]
        dist = (diff * diff * weights).sum(axis=2)
        out[start:end] = np.argmin(dist, axis=1).astype(np.uint8)
    return out.reshape(rgb_arr.shape[:2])


def _weighted_rgb_mse_np(
        rgb_arr: np.ndarray,
        pixel_indices: bytes | np.ndarray,
        pal_arr: np.ndarray) -> float:
    if isinstance(pixel_indices, bytes):
        idx = np.frombuffer(pixel_indices, dtype=np.uint8).reshape(rgb_arr.shape[:2])
    else:
        idx = pixel_indices
    mapped = pal_arr[idx.astype(np.intp, copy=False)]
    diff = rgb_arr - mapped
    return float((diff * diff * _NP_RGB_WEIGHTS).sum(axis=2).mean())


def _ordered_dither_indices_np(
        rgb_arr: np.ndarray,
        pal_arr: np.ndarray) -> np.ndarray:
    h, w = rgb_arr.shape[:2]
    tile = np.tile(_BAYER4, ((h + 3) // 4, (w + 3) // 4))[:h, :w]
    yy, xx = np.indices((h, w), dtype=np.uint32)
    # PERF: Deterministic hash noise breaks Bayer blocks while staying vectorized.
    blueish = (((xx * 1103515245) ^ (yy * 12345)) & 255).astype(np.float32)
    noise = ((tile * 0.75) + ((blueish / 255.0) - 0.5) * 0.25) * 48.0
    dithered = np.clip(rgb_arr + noise[:, :, None], 0.0, 255.0)
    return _nearest_palette_indices_np(dithered, pal_arr)


def _compact_dither_indices_np(
        rgb_arr: np.ndarray,
        indices: np.ndarray,
        pal_arr: np.ndarray) -> tuple[np.ndarray, int]:
    h, w = indices.shape
    if h < 3 or w < 3:
        return indices, 0

    pal_count = pal_arr.shape[0]
    counts = np.zeros((pal_count, h, w), dtype=np.uint8)
    padded = np.pad(indices, 1, mode="edge")
    for dy, dx in (
            (-1, -1), (-1, 0), (-1, 1),
            (0, -1),           (0, 1),
            (1, -1),  (1, 0),  (1, 1)):
        neighbour = padded[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
        for colour_idx in range(pal_count):
            counts[colour_idx] += (neighbour == colour_idx)

    candidate = np.argmax(counts, axis=0).astype(np.uint8)
    best_count = np.max(counts, axis=0)
    current_count = np.take_along_axis(
        counts,
        indices.astype(np.intp, copy=False)[None, :, :],
        axis=0,
    )[0]
    cur_err = (
        (rgb_arr - pal_arr[indices.astype(np.intp, copy=False)]) ** 2
        * _NP_RGB_WEIGHTS
    ).sum(axis=2)
    cand_err = (
        (rgb_arr - pal_arr[candidate.astype(np.intp, copy=False)]) ** 2
        * _NP_RGB_WEIGHTS
    ).sum(axis=2)
    # PERF: Absorb only isolated speckles into a local majority colour; the
    # global MSE guard below rejects images where this is visually too costly.
    change = (
        (candidate != indices)
        & (best_count >= 4)
        & (current_count <= 2)
        & (cand_err <= cur_err * 1.12 + 96.0)
    )
    changed = int(np.count_nonzero(change))
    if not changed:
        return indices, 0

    compacted = indices.copy()
    compacted[change] = candidate[change]
    return compacted, changed


def _fast_palette_index_data(
        img: Image.Image,
        pal_rgb: list[tuple],
        exact_pixel_indices: bytes) -> tuple[bytes, bool, dict[str, float | int | bool]]:
    if len(pal_rgb) > 255:
        return exact_pixel_indices, False, {"fallback": True}

    rgb_arr = np.asarray(img, dtype=np.float32)
    pal_arr = np.asarray(pal_rgb, dtype=np.float32)
    ordered = _ordered_dither_indices_np(rgb_arr, pal_arr)
    compacted, compacted_pixels = _compact_dither_indices_np(
        rgb_arr, ordered, pal_arr
    )
    exact_mse = _weighted_rgb_mse_np(rgb_arr, exact_pixel_indices, pal_arr)
    fast_mse = _weighted_rgb_mse_np(rgb_arr, compacted, pal_arr)
    allowed_mse = exact_mse * 1.08 + 1e-6
    fallback = fast_mse > allowed_mse
    stats: dict[str, float | int | bool] = {
        "exact_mse": exact_mse,
        "fast_mse": fast_mse,
        "allowed_mse": allowed_mse,
        "compacted_pixels": compacted_pixels,
        "fallback": fallback,
    }
    if fallback:
        return exact_pixel_indices, False, stats
    return compacted.astype(np.uint8, copy=False).tobytes(), True, stats


def _atkinson_dither_indices_np(
        img: Image.Image,
        pal_rgb: list[tuple]) -> bytes:
    rgb_arr = np.asarray(img, dtype=np.float32).copy()
    pal_arr = np.asarray(pal_rgb, dtype=np.float32)
    h, w = rgb_arr.shape[:2]
    out = np.empty((h, w), dtype=np.uint8)
    weights = _NP_RGB_WEIGHTS
    offsets = (
        (1, 0), (2, 0),
        (-1, 1), (0, 1), (1, 1),
        (0, 2),
    )

    for y in range(h):
        row = rgb_arr[y]
        for x in range(w):
            old = row[x].copy()
            diff = pal_arr - old[None, :]
            dist = (diff * diff * weights).sum(axis=1)
            idx = int(np.argmin(dist))
            out[y, x] = idx
            new = pal_arr[idx]
            err = (old - new) * 0.125
            for dx, dy in offsets:
                nx = x + dx
                ny = y + dy
                if 0 <= nx < w and 0 <= ny < h:
                    rgb_arr[ny, nx] = np.clip(
                        rgb_arr[ny, nx] + err, 0.0, 255.0
                    )

    return out.tobytes()


def _index_image_data(
        img: Image.Image,
        pal_rgb: list[tuple],
        dither: DitherPreset,
        planner: PlannerMode = PlannerMode.AUTO) -> IndexedImageResult:
    t0 = time.monotonic()
    if dither == DitherPreset.FLAT:
        data = _palette_index_data(img, pal_rgb, False)
        return IndexedImageResult(
            data,
            dither,
            stats={"mode": dither.value, "seconds": time.monotonic() - t0},
        )

    if dither == DitherPreset.PHOTO:
        data = _palette_index_data(img, pal_rgb, True)
        return IndexedImageResult(
            data,
            dither,
            stats={"mode": dither.value, "seconds": time.monotonic() - t0},
        )

    if dither == DitherPreset.CLEAN:
        data = _atkinson_dither_indices_np(img, pal_rgb)
        return IndexedImageResult(
            data,
            dither,
            stats={"mode": dither.value, "seconds": time.monotonic() - t0},
        )

    exact = _palette_index_data(img, pal_rgb, True)
    if planner == PlannerMode.EXACT:
        return IndexedImageResult(
            exact,
            dither,
            False,
            {"mode": "Photo exact guard", "seconds": time.monotonic() - t0},
        )

    data, fast_used, stats = _fast_palette_index_data(img, pal_rgb, exact)
    stats["mode"] = dither.value
    stats["seconds"] = time.monotonic() - t0
    return IndexedImageResult(data, dither, fast_used, stats)


def _coalesce_runs(runs: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    merged: list[tuple[int, int, int]] = []
    for idx, start, end in runs:
        if merged and merged[-1][0] == idx and merged[-1][2] + 1 >= start:
            prev_idx, prev_start, _ = merged[-1]
            merged[-1] = (prev_idx, prev_start, end)
        else:
            merged.append((idx, start, end))
    return merged


def _merge_tiny_noise(
        runs: list[tuple[int, int, int]],
        skip_indices: set[int]) -> list[tuple[int, int, int]]:
    if len(runs) < 3:
        return runs

    smoothed = list(runs)
    for i in range(1, len(runs) - 1):
        idx, start, end = runs[i]
        if idx in skip_indices or end > start:
            continue
        left_idx = runs[i - 1][0]
        right_idx = runs[i + 1][0]
        if left_idx == right_idx:
            smoothed[i] = (left_idx, start, end)

    return _coalesce_runs(smoothed)


PaletteRun = tuple[int, int, int]              # colour_idx, gx_start, gx_end
GridRun = tuple[int, int, int]                 # gy, gx_start, gx_end
GridLine = tuple[int, int, int, int]           # gy1, gx1, gy2, gx2
GridPath = tuple[tuple[int, int], ...]         # (gy, gx), ...
GridFill = tuple[str, int, int, GridPath]      # "fill", seed_gy, seed_gx, boundary
GridAction = GridLine | GridPath | GridFill
_GRID_PATH_VERTEX_LIMIT = 1024
_OVERPAINT_MAX_DEPTH = 8
_OVERPAINT_CANDIDATES = 4
_FILL_MIN_SIDE = 8
_FILL_MIN_AREA = 256


def _overpaint_cost(layers: list[list[PaletteRun]]) -> int:
    return sum(len(layer) for layer in layers)


def _merge_overpaint_layers(
        target: list[list[PaletteRun]],
        source: list[list[PaletteRun]],
        offset: int = 0) -> None:
    while len(target) < offset + len(source):
        target.append([])
    for idx, layer in enumerate(source):
        target[offset + idx].extend(layer)


def _overpaint_segment_layers(
        segment: list[PaletteRun],
        depth: int = 0) -> list[list[PaletteRun]]:
    """
    Build exact horizontal paint layers for one non-skipped row segment.

    A longer underpaint span may temporarily cover other colours, then later
    layers restore those inner runs. This preserves the final pixels while
    replacing repeated patterns such as A B C B A with fewer strokes.
    """
    def _solve(start: int, end: int, cur_depth: int) -> list[list[PaletteRun]]:
        count = end - start
        if count <= 0:
            return []
        if count < 3 or cur_depth >= _OVERPAINT_MAX_DEPTH:
            return [[segment[i] for i in range(start, end)]]

        positions: dict[int, list[int]] = {}
        pixel_counts: dict[int, int] = {}
        for idx in range(start, end):
            colour_idx, gx_start, gx_end = segment[idx]
            positions.setdefault(colour_idx, []).append(idx)
            pixel_counts[colour_idx] = (
                pixel_counts.get(colour_idx, 0) + gx_end - gx_start + 1
            )

        candidates = [
            colour_idx for colour_idx, pos in positions.items()
            if len(pos) >= 2
        ]
        if not candidates:
            return [[segment[i] for i in range(start, end)]]

        candidates.sort(
            key=lambda colour_idx: (
                len(positions[colour_idx]), pixel_counts[colour_idx]
            ),
            reverse=True,
        )

        best_layers: list[list[PaletteRun]] | None = None
        best_cost = count

        for colour_idx in candidates[:_OVERPAINT_CANDIDATES]:
            pos = positions[colour_idx]
            first = pos[0]
            last = pos[-1]
            layers: list[list[PaletteRun]] = [[(
                colour_idx, segment[first][1], segment[last][2]
            )]]

            # Recurse on index ranges to avoid allocating sliced run lists.
            if first > start:
                _merge_overpaint_layers(
                    layers,
                    _solve(start, first, cur_depth + 1),
                    0,
                )

            prev = first
            for nxt in pos[1:]:
                if nxt > prev + 1:
                    _merge_overpaint_layers(
                        layers,
                        _solve(prev + 1, nxt, cur_depth + 1),
                        1,
                    )
                prev = nxt

            if last + 1 < end:
                _merge_overpaint_layers(
                    layers,
                    _solve(last + 1, end, cur_depth + 1),
                    0,
                )

            cost = _overpaint_cost(layers)
            if cost < best_cost:
                best_layers = layers
                best_cost = cost

        return (
            best_layers if best_layers is not None
            else [[segment[i] for i in range(start, end)]]
        )

    return _solve(0, len(segment), depth)


def _as_line_strokes(runs: list[GridRun]) -> list[GridLine]:
    return [(gy, gx_start, gy, gx_end) for gy, gx_start, gx_end in runs]


def _is_grid_path(action: GridAction) -> bool:
    return bool(action) and isinstance(action[0], tuple)


def _is_grid_fill(action: GridAction) -> bool:
    return bool(action) and action[0] == "fill"


def _action_event_cost(actions: list[GridAction]) -> int:
    total = 0
    for action in actions:
        if _is_grid_fill(action):
            _, _, _, boundary = action
            total += len(boundary) + 4
        elif _is_grid_path(action):
            total += len(action) + 1
        else:
            gy1, gx1, gy2, gx2 = action
            total += 2 if gy1 == gy2 and gx1 == gx2 else 3
    return total


def _planned_action_count(action: GridAction) -> int:
    return 2 if _is_grid_fill(action) else 1


def _split_grid_path(points: list[tuple[int, int]]) -> list[GridPath]:
    if len(points) <= _GRID_PATH_VERTEX_LIMIT:
        return [tuple(points)]

    paths: list[GridPath] = []
    start = 0
    while start < len(points):
        end = min(len(points), start + _GRID_PATH_VERTEX_LIMIT)
        chunk = points[start:end] if start == 0 else points[start - 1:end]
        paths.append(tuple(chunk))
        start = end
    return paths


def _component_walk_path(
        component: set[int], cols: int, rows: int) -> list[tuple[int, int]]:
    def _point(cell: int) -> tuple[int, int]:
        gy, gx = divmod(cell, cols)
        return gy, gx

    def _neighbours(cell: int) -> list[int]:
        gy, gx = divmod(cell, cols)
        out: list[int] = []
        for dy, dx in (
                (0, 1), (1, 0), (0, -1), (-1, 0),
                (1, 1), (1, -1), (-1, 1), (-1, -1)):
            ngy, ngx = gy + dy, gx + dx
            if not (0 <= ngx < cols and 0 <= ngy < rows):
                continue
            n = ngy * cols + ngx
            if n in component:
                out.append(n)
        return out

    start = min(component)
    visited = {start}
    path = [_point(start)]
    stack: list[tuple[int, list[int], int]] = [(start, _neighbours(start), 0)]

    while stack:
        cell, neighbours, i = stack[-1]
        while i < len(neighbours) and neighbours[i] in visited:
            i += 1
        if i >= len(neighbours):
            stack.pop()
            if stack:
                path.append(_point(stack[-1][0]))
            continue

        nxt = neighbours[i]
        stack[-1] = (cell, neighbours, i + 1)
        visited.add(nxt)
        path.append(_point(nxt))
        stack.append((nxt, _neighbours(nxt), 0))

    return path


def _grid_components(
        cells: set[int],
        cols: int,
        rows: int,
        diagonal: bool = True) -> list[set[int]]:
    remaining = set(cells)
    components: list[set[int]] = []
    neighbour_offsets = [(0, 1), (1, 0), (0, -1), (-1, 0)]
    if diagonal:
        neighbour_offsets.extend([(1, 1), (1, -1), (-1, 1), (-1, -1)])
    while remaining:
        start = remaining.pop()
        component = {start}
        stack = [start]
        while stack:
            cell = stack.pop()
            gy, gx = divmod(cell, cols)
            for dy, dx in neighbour_offsets:
                ngy, ngx = gy + dy, gx + dx
                if not (0 <= ngx < cols and 0 <= ngy < rows):
                    continue
                n = ngy * cols + ngx
                if n in remaining:
                    remaining.remove(n)
                    component.add(n)
                    stack.append(n)
        components.append(component)
    return components


def _optimize_cells_as_lines(
        source_cells: set[int],
        cols: int,
        rows: int,
        diagonal: bool = True) -> list[GridLine]:
    cells = set(source_cells)
    if not cells:
        return []

    directions = [(1, 0), (0, 1)]
    if diagonal:
        directions.extend([(1, 1), (1, -1)])

    def _inside(gx: int, gy: int) -> bool:
        return 0 <= gx < cols and 0 <= gy < rows

    strokes: list[GridLine] = []
    while cells:
        first = next(iter(cells))
        gy, gx = divmod(first, cols)

        best_len = 1
        best = (gx, gy, gx, gy, 1, 0)
        for dx, dy in directions:
            sx, sy = gx, gy
            while True:
                nx, ny = sx - dx, sy - dy
                if not _inside(nx, ny) or ny * cols + nx not in cells:
                    break
                sx, sy = nx, ny

            ex, ey = gx, gy
            while True:
                nx, ny = ex + dx, ey + dy
                if not _inside(nx, ny) or ny * cols + nx not in cells:
                    break
                ex, ey = nx, ny

            length = max(abs(ex - sx), abs(ey - sy)) + 1
            if length > best_len:
                best_len = length
                best = (sx, sy, ex, ey, dx, dy)

        sx, sy, ex, ey, dx, dy = best
        strokes.append((sy, sx, ey, ex))

        cx, cy = sx, sy
        for _ in range(best_len):
            cells.discard(cy * cols + cx)
            cx += dx
            cy += dy

    return strokes


def _optimize_grid_strokes(
        runs: list[GridRun],
        cols: int,
        rows: int,
        diagonal: bool = True,
        paths: bool = True) -> list[GridAction]:
    """
    Cover the same 1px grid cells with cheaper exact actions.

    First we try straight horizontal/vertical/diagonal strokes. For grainy
    connected clusters that do not form long lines, we may instead trace a
    one-colour path through the component. The path can revisit pixels, but
    every touched pixel is already supposed to be that colour.
    """
    if len(runs) < 2:
        return _as_line_strokes(runs)

    cells: set[int] = set()
    for gy, gx_start, gx_end in runs:
        base = gy * cols
        cells.update(range(base + gx_start, base + gx_end + 1))
    if not cells:
        return []

    actions: list[GridAction] = []
    for component in _grid_components(cells, cols, rows, diagonal):
        line_actions = _optimize_cells_as_lines(component, cols, rows, diagonal)
        path_actions: list[GridAction] = []
        if paths and len(component) > 1:
            path_actions = list(_split_grid_path(
                _component_walk_path(component, cols, rows)
            ))

        if path_actions and _action_event_cost(path_actions) < _action_event_cost(line_actions):
            actions.extend(path_actions)
        else:
            actions.extend(line_actions)

    return actions


def _runs_to_mask(runs: list[GridRun], cols: int, rows: int) -> np.ndarray:
    mask = np.zeros((rows, cols), dtype=bool)
    for gy, gx_start, gx_end in runs:
        mask[gy, gx_start:gx_end + 1] = True
    return mask


def _row_mask_runs(mask: np.ndarray) -> list[list[list[int]]]:
    row_runs: list[list[list[int]]] = []
    for gy in range(mask.shape[0]):
        xs = np.flatnonzero(mask[gy])
        if xs.size == 0:
            row_runs.append([])
            continue
        gaps = np.flatnonzero(np.diff(xs) > 1)
        starts = np.concatenate((xs[:1], xs[gaps + 1]))
        ends = np.concatenate((xs[gaps], xs[-1:]))
        row_runs.append([[int(start), int(end), 0] for start, end in zip(starts, ends)])
    return row_runs


def _chain_to_grid_actions(chain: list[GridRun]) -> list[GridAction]:
    if len(chain) < 2:
        return _as_line_strokes(chain)

    points: list[tuple[int, int]] = []
    gy0, gx_start0, _ = chain[0]
    cur_x = gx_start0
    points.append((gy0, cur_x))

    for i, (gy, gx_start, gx_end) in enumerate(chain):
        if points[-1] != (gy, cur_x):
            points.append((gy, cur_x))
        if i + 1 < len(chain):
            _, next_start, next_end = chain[i + 1]
            overlap_start = max(gx_start, next_start)
            overlap_end = min(gx_end, next_end)
            exit_x = (overlap_start + overlap_end) // 2
        else:
            exit_x = gx_end if cur_x != gx_end else gx_start

        for gx in (gx_start, gx_end, exit_x):
            if points[-1] != (gy, gx):
                points.append((gy, gx))
        cur_x = exit_x
        if i + 1 < len(chain):
            next_gy = chain[i + 1][0]
            if points[-1] != (next_gy, cur_x):
                points.append((next_gy, cur_x))

    path_actions = list(_split_grid_path(points))
    line_actions = _as_line_strokes(chain)
    return (
        path_actions
        if _action_event_cost(path_actions) < _action_event_cost(line_actions)
        else line_actions
    )


def _action_start_end(action: GridAction) -> tuple[tuple[int, int], tuple[int, int]]:
    if _is_grid_fill(action):
        _, seed_gy, seed_gx, _ = action
        point = (seed_gy, seed_gx)
        return point, point
    if _is_grid_path(action):
        return action[0], action[-1]
    gy1, gx1, gy2, gx2 = action
    return (gy1, gx1), (gy2, gx2)


def _order_actions_nearest(
        actions: list[GridAction],
        radius: int = 64) -> list[GridAction]:
    if len(actions) < 3:
        return actions

    remaining = list(actions)
    ordered: list[GridAction] = []
    cur = (0, 0)
    radius_sq = radius * radius
    while remaining:
        best_i = 0
        best_d = 1 << 60
        best_reverse = False
        for i, action in enumerate(remaining):
            start, end = _action_start_end(action)
            d = (start[0] - cur[0]) ** 2 + (start[1] - cur[1]) ** 2
            reverse = False
            if _is_grid_path(action):
                rev_d = (end[0] - cur[0]) ** 2 + (end[1] - cur[1]) ** 2
                if rev_d < d:
                    d = rev_d
                    reverse = True
            if d < best_d:
                best_i = i
                best_d = d
                best_reverse = reverse
                if d <= radius_sq:
                    break

        action = remaining.pop(best_i)
        if best_reverse and _is_grid_path(action):
            action = tuple(reversed(action))
        ordered.append(action)
        _, cur = _action_start_end(action)

    return ordered


def _mask_serpentine_actions(mask: np.ndarray) -> list[GridAction]:
    rows, _ = mask.shape
    row_runs = _row_mask_runs(mask)
    actions: list[GridAction] = []

    for gy in range(rows):
        for run in row_runs[gy]:
            if run[2]:
                continue
            chain: list[GridRun] = []
            cur_gy = gy
            cur_run = run
            while True:
                gx_start, gx_end, _ = cur_run
                cur_run[2] = 1
                chain.append((cur_gy, gx_start, gx_end))
                if cur_gy + 1 >= rows:
                    break

                best_run: list[int] | None = None
                best_overlap = 0
                for next_run in row_runs[cur_gy + 1]:
                    if next_run[2]:
                        continue
                    overlap = min(gx_end, next_run[1]) - max(gx_start, next_run[0]) + 1
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_run = next_run
                if best_run is None or best_overlap <= 0:
                    break
                cur_gy += 1
                cur_run = best_run

            actions.extend(_chain_to_grid_actions(chain))

    return _order_actions_nearest(actions)


def _optimize_grid_strokes_fast(
        runs: list[GridRun],
        cols: int,
        rows: int) -> list[GridAction]:
    if len(runs) < 2:
        return _as_line_strokes(runs)

    horizontal = _as_line_strokes(runs)
    mask = _runs_to_mask(runs, cols, rows)
    # PERF: NumPy row masks avoid Python set components for the max-speed path.
    serpentine = _mask_serpentine_actions(mask)
    return (
        serpentine
        if _action_event_cost(serpentine) < _action_event_cost(horizontal)
        else horizontal
    )


def _extract_rect_fill_actions(
        runs: list[GridRun]) -> tuple[list[GridRun], list[GridAction]]:
    if len(runs) < _FILL_MIN_SIDE:
        return runs, []

    groups: dict[tuple[int, int], list[list[int]]] = {}
    for gy, gx_start, gx_end in sorted(runs):
        group_runs = groups.setdefault((gx_start, gx_end), [])
        if group_runs and group_runs[-1][1] + 1 == gy:
            group_runs[-1][1] = gy
        else:
            group_runs.append([gy, gy])

    covered: set[GridRun] = set()
    fill_actions: list[GridAction] = []
    for (gx_start, gx_end), gy_ranges in groups.items():
        width = gx_end - gx_start + 1
        if width < _FILL_MIN_SIDE:
            continue
        for gy_start, gy_end in gy_ranges:
            height = gy_end - gy_start + 1
            area = width * height
            if height < _FILL_MIN_SIDE or area < _FILL_MIN_AREA:
                continue
            boundary: GridPath = (
                (gy_start, gx_start),
                (gy_start, gx_end),
                (gy_end, gx_end),
                (gy_end, gx_start),
                (gy_start, gx_start),
            )
            seed_gy = (gy_start + gy_end) // 2
            seed_gx = (gx_start + gx_end) // 2
            fill_action: GridFill = ("fill", seed_gy, seed_gx, boundary)
            old_cost = height * 3
            # PERF: Fill is used only for exact solid rectangles with a clear
            # 25% event-cost win, so the tool switch cannot make it slower.
            if _action_event_cost([fill_action]) > old_cost * 0.75:
                continue
            fill_actions.append(fill_action)
            for gy in range(gy_start, gy_end + 1):
                covered.add((gy, gx_start, gx_end))

    if not fill_actions:
        return runs, []

    remaining = [run for run in runs if run not in covered]
    return remaining, fill_actions


def _auto_brush_candidate_sizes(
        max_px: int,
        step: int,
        mode: AutoBrushMode) -> list[int]:
    if mode == AutoBrushMode.OFF:
        return []
    max_px = _nearest_brush_size(max_px)
    minimum = max(8, step * 4)
    if mode == AutoBrushMode.AGGRESSIVE:
        candidates = [
            px for px in BRUSH_SIZE_STOPS
            if minimum <= px <= max_px
        ]
    else:
        anchors = (248, 244, 220, 196, 172, 140, 104, 72, 48, 32, 24, 16, 12, 8)
        candidates = [
            _nearest_brush_size(px)
            for px in anchors
            if minimum <= px <= max_px
        ]
    return sorted(set(candidates), reverse=True)


def _erode_square_mask(mask: np.ndarray, window: int) -> np.ndarray:
    if window <= 1:
        return mask.copy()
    h, w = mask.shape
    out = np.zeros((h, w), dtype=bool)
    if h < window or w < window:
        return out

    integral = np.pad(
        mask.astype(np.int32).cumsum(axis=0).cumsum(axis=1),
        ((1, 0), (1, 0)),
    )
    sums = (
        integral[window:, window:]
        - integral[:-window, window:]
        - integral[window:, :-window]
        + integral[:-window, :-window]
    )
    good = sums == window * window
    off = window // 2
    out[off:off + good.shape[0], off:off + good.shape[1]] = good
    return out


def _brush_coverage_slices(
        shape: tuple[int, int],
        action: BrushAction) -> tuple[slice, slice]:
    cover = max(1, action.cover_cells)
    half = cover // 2
    extra = cover - half - 1
    y0 = max(0, action.gy - half)
    y1 = min(shape[0], action.gy + extra + 1)
    x0 = max(0, action.gx_start - half)
    x1 = min(shape[1], action.gx_end + extra + 1)
    return slice(y0, y1), slice(x0, x1)


def _count_new_brush_coverage(
        covered: np.ndarray,
        action: BrushAction) -> int:
    ys, xs = _brush_coverage_slices(covered.shape, action)
    return int(np.count_nonzero(~covered[ys, xs]))


def _mark_brush_coverage(
        covered: np.ndarray,
        action: BrushAction) -> None:
    ys, xs = _brush_coverage_slices(covered.shape, action)
    covered[ys, xs] = True


def _extract_safe_brush_runs(
        safe_centers: np.ndarray,
        brush_px: int,
        colour_idx: int,
        cover_cells: int,
        mode: AutoBrushMode) -> list[BrushAction]:
    actions: list[BrushAction] = []
    min_run = max(cover_cells, 6 if mode == AutoBrushMode.SAFE else 3)
    for gy in range(safe_centers.shape[0]):
        xs = np.flatnonzero(safe_centers[gy])
        if xs.size == 0:
            continue
        gaps = np.flatnonzero(np.diff(xs) > 1)
        starts = np.concatenate((xs[:1], xs[gaps + 1]))
        ends = np.concatenate((xs[gaps], xs[-1:]))
        for start, end in zip(starts, ends):
            run_len = int(end - start + 1)
            if run_len < min_run:
                continue
            actions.append(
                BrushAction(
                    brush_px=brush_px,
                    colour_idx=colour_idx,
                    gy=gy,
                    gx_start=int(start),
                    gx_end=int(end),
                    cover_cells=cover_cells,
                )
            )
    return actions


def _plan_auto_brush_actions(
        pixel_indices: np.ndarray,
        palette_count: int,
        skip_indices: set[int],
        candidate_sizes: list[int],
        step: int,
        mode: AutoBrushMode) -> tuple[list[BrushAction], np.ndarray, PlanStats]:
    covered = np.zeros(pixel_indices.shape, dtype=bool)
    stats = PlanStats(planner_mode=mode.value)
    if mode == AutoBrushMode.OFF or not candidate_sizes:
        return [], covered, stats

    all_actions: list[BrushAction] = []
    safe_pad = 2 if mode == AutoBrushMode.SAFE else 1
    cover_shrink = 3 if mode == AutoBrushMode.SAFE else 1
    min_area = 96 if mode == AutoBrushMode.SAFE else 32

    for colour_idx in range(palette_count):
        if colour_idx in skip_indices:
            continue
        colour_mask = pixel_indices == colour_idx
        if int(np.count_nonzero(colour_mask)) < min_area:
            continue

        colour_covered = np.zeros(pixel_indices.shape, dtype=bool)
        for brush_px in candidate_sizes:
            cells = max(1, int(round(brush_px / max(1, step))))
            if cells < 4:
                continue
            safe_window = cells + safe_pad
            cover_cells = max(1, cells - cover_shrink)
            remaining = colour_mask & ~colour_covered
            if int(np.count_nonzero(remaining)) < max(min_area, safe_window * safe_window):
                continue

            safe_centers = _erode_square_mask(remaining, safe_window)
            if not np.any(safe_centers):
                continue

            actions = _extract_safe_brush_runs(
                safe_centers, brush_px, colour_idx, cover_cells, mode
            )
            if not actions:
                continue

            accepted = 0
            for action in actions:
                new_pixels = _count_new_brush_coverage(colour_covered, action)
                if new_pixels <= 0:
                    continue
                # One large stroke should replace a meaningful chunk of the
                # small-stroke residual plan after accounting for size changes.
                if new_pixels < max(cover_cells * 2, 32):
                    continue
                _mark_brush_coverage(colour_covered, action)
                all_actions.append(action)
                accepted += 1
                stats.auto_brush_pixels += new_pixels

            if accepted:
                stats.auto_brush_sizes.append(brush_px)

        covered |= colour_covered

    stats.auto_brush_actions = len(all_actions)
    stats.auto_brush_sizes = sorted(set(stats.auto_brush_sizes), reverse=True)
    return all_actions, covered, stats


# DEBUG PANEL
# ─────────────────────────────────────────────────────────────────────────────

class DebugPanel(tk.Toplevel):
    """
    Live-tuning popup for every delay in the pipeline.
    Sliders bind directly to the tk.IntVar fields in DelayConfig, so any
    movement is reflected immediately — even during an active draw.
    """

    BG   = "#0d0d0f"
    CARD = "#161618"
    ACC  = "#00e5a0"
    WARN = "#f5a623"
    ERR  = "#ff4c4c"
    MUT  = "#555"
    FONT = ("Consolas", 9)
    BFONT= ("Consolas", 9, "bold")

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.title("⚙  Timing Debug Panel  —  v5.0")
        self.configure(bg=self.BG)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self._build()

    # ── Build ──────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        tk.Label(self, text="TIMING  DEBUG  PANEL",
                 font=("Consolas", 13, "bold"), bg=self.BG, fg=self.ACC
                 ).pack(pady=(14, 2), padx=24)
        tk.Label(self,
                 text="All changes are live — effective immediately, even mid-draw.",
                 font=("Consolas", 8, "italic"), bg=self.BG, fg=self.MUT
                 ).pack(pady=(0, 8))

        # ── Preset buttons ─────────────────────────────────────────────────────
        row = tk.Frame(self, bg=self.BG)
        row.pack(fill="x", padx=24, pady=(0, 10))
        tk.Button(
            row, text="⚡  TURBO PRESET", command=self._apply_turbo,
            font=self.BFONT, bg=self.WARN, fg="#000",
            activebackground="#d4901f", activeforeground="#000",
            relief="flat", cursor="hand2", padx=10, pady=5,
        ).pack(side="left", padx=(0, 8))
        tk.Button(
            row, text="↺  RESTORE DEFAULTS", command=self._apply_defaults,
            font=self.FONT, bg="#202022", fg=self.ACC,
            activebackground="#2c2c2e", activeforeground=self.ACC,
            relief="flat", cursor="hand2", padx=10, pady=5,
        ).pack(side="left")
        tk.Label(row,
                 text="Turbo tip: enable 'Skip Pencil Re-arm'\nfor the biggest single speedup.",
                 font=("Consolas", 7, "italic"), bg=self.BG, fg=self.MUT,
                 justify="left",
                 ).pack(side="left", padx=(16, 0))

        # ── Sections ───────────────────────────────────────────────────────────
        left  = tk.Frame(self, bg=self.BG)
        right = tk.Frame(self, bg=self.BG)
        left.pack(side="left",  fill="both", padx=(24, 8), pady=(0, 16))
        right.pack(side="left", fill="both", padx=(8, 24), pady=(0, 16))

        self._section(left, "STROKE INTERNALS", [
            ("Move settle",        _cfg.move_settle_ms,    "ms",  0, 100),
            ("Press settle",       _cfg.press_settle_ms,   "ms",  0, 100),
            ("Post-step settle",   _cfg.post_step_ms,      "ms",  0,  60),
            ("Release settle",     _cfg.release_settle_ms, "ms",  0,  60),
            ("Min stroke floor",   _cfg.min_stroke_ms,     "ms",  0, 150),
            ("Drag interp. steps", _cfg.drag_steps,        " #",  1,  20),
        ])
        self._section(left, "CLICK TIMING", [
            ("Pre-LEFTDOWN",       _cfg.click_pre_down_ms, "ms",  0, 100),
            ("Pre-LEFTUP",         _cfg.click_pre_up_ms,   "ms",  0, 100),
            ("Post-click default", _cfg.click_post_ms,     "ms",  0, 250),
        ])

        self._section(right, "PALETTE & PENCIL", [
            ("Palette click delay", _cfg.pal_delay_ms,    "ms",  0, 250),
            ("Pencil re-arm delay", _cfg.pencil_rearm_ms, "ms",  0, 700),
        ])

        # Skip pencil re-arm toggle
        skip_card = tk.Frame(right, bg=self.CARD, padx=12, pady=8)
        skip_card.pack(fill="x", pady=(0, 6))
        tk.Label(skip_card, text="PENCIL RE-ARM",
                 font=self.BFONT, bg=self.CARD, fg=self.ACC).pack(anchor="w", pady=(0, 4))
        tk.Checkbutton(
            skip_card,
            text="Skip pencil re-arm after palette click",
            variable=_cfg.skip_pencil_rearm,
            font=self.FONT, bg=self.CARD, fg=self.WARN,
            selectcolor="#1a1a1c", activebackground=self.CARD,
            cursor="hand2",
        ).pack(anchor="w")
        tk.Label(skip_card,
                  text="Default ON for speed.\n"
                       "Turn off only if Paint drops the pencil.",
                 font=("Consolas", 8, "italic"), bg=self.CARD, fg=self.MUT,
                 justify="left").pack(anchor="w", pady=(2, 0))

        self._section(right, "FOCUS", [
            ("Focus delay",        _cfg.focus_delay_ms, "ms",  0, 2000),
            ("Re-focus every N rows/strokes", _cfg.focus_interval, " #", 10, 1000),
        ])

    def _section(self, parent: tk.Widget, title: str,
                 rows: list[tuple]) -> None:
        card = tk.Frame(parent, bg=self.CARD, padx=12, pady=8)
        card.pack(fill="x", pady=(0, 6))
        tk.Label(card, text=title, font=self.BFONT,
                 bg=self.CARD, fg=self.ACC).pack(anchor="w", pady=(0, 6))
        for label, var, unit, lo, hi in rows:
            self._slider_row(card, label, var, unit, lo, hi)

    def _slider_row(self, parent: tk.Widget, label: str,
                    var: tk.IntVar, unit: str, lo: int, hi: int) -> None:
        row = tk.Frame(parent, bg=self.CARD)
        row.pack(fill="x", pady=2)

        tk.Label(row, text=label, font=self.FONT, bg=self.CARD, fg="#bbb",
                 width=24, anchor="w").pack(side="left")

        val_lbl = tk.Label(row, text=f"{var.get():4d}{unit}",
                           font=("Consolas", 9, "bold"),
                           bg=self.CARD, fg=self.ACC, width=7, anchor="e")
        val_lbl.pack(side="right")

        def _upd(*_, lbl=val_lbl, v=var, u=unit):
            lbl.config(text=f"{v.get():4d}{u}")

        var.trace_add("write", _upd)
        ttk.Scale(row, from_=lo, to=hi, orient="horizontal",
                  variable=var, length=190, command=_upd).pack(side="left", padx=6)

    # ── Presets ────────────────────────────────────────────────────────────────

    def _apply_turbo(self) -> None:
        _cfg.apply(DelayConfig.TURBO)
        _cfg.skip_pencil_rearm.set(True)
        _log.info("TURBO preset applied")

    def _apply_defaults(self) -> None:
        _cfg.apply(DelayConfig.DEFAULTS)
        _cfg.skip_pencil_rearm.set(True)
        _log.info("Default delays restored")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────────────────────────────────────

class DrawBotApp:
    DETAIL_MIN = 1
    DETAIL_MAX = 20

    # ── Init ───────────────────────────────────────────────────────────────────

    def __init__(self, root: tk.Tk) -> None:
        global _cfg
        _register_hotkeys()
        self.root = root
        root.title("DrawBot  v5.0")
        root.resizable(False, False)
        root.configure(bg="#0d0d0f")

        self.bg    = "#0d0d0f"
        self.card  = "#161618"
        self.acc   = "#00e5a0"
        self.warn  = "#f5a623"
        self.err   = "#ff4c4c"
        self.muted = "#555"
        self.font  = ("Consolas", 10)
        self.hfont = ("Consolas", 14, "bold")

        self._image_path: str | None = None
        self._is_calibrated = False
        self._debug_panel: DebugPanel | None = None

        self.canvas_x = self.canvas_y = self.canvas_w = self.canvas_h = 0
        self.pencil_tool: tuple[int, int] = (0, 0)
        self.fill_tool: tuple[int, int] | None = None
        self.eraser_tool: tuple[int, int] | None = None
        self.brush_cal: BrushSizeCalibration | None = None
        self.palette:  list[dict] = []
        self._pal_rgb: list[tuple] = []
        self._hwnd: int | None = None
        self._draw_ui_hidden = False
        self._current_paint_brush: int | None = None
        self._preview_img_ref = None

        # Initialise global config BEFORE building UI (widgets bind to its vars)
        _cfg = DelayConfig()

        self._brush_px = tk.IntVar(value=1)
        self._detail   = tk.DoubleVar(value=10)
        self._speed_ms = tk.IntVar(value=0)
        self._batch_colours = tk.BooleanVar(value=True)
        self._skip_white = tk.BooleanVar(value=True)
        self._smart_underpaint = tk.BooleanVar(value=True)
        self._merge_tiny_runs = tk.BooleanVar(value=True)
        self._dither_style = tk.StringVar(value=DitherPreset.SMART.value)
        self._planner_mode = tk.StringVar(value=PlannerMode.AUTO.value)
        self._auto_brush_mode = tk.StringVar(value=AutoBrushMode.OFF.value)
        self._max_auto_brush = tk.IntVar(value=48)
        self._paint_fill_accel = tk.BooleanVar(value=True)
        self._grayscale_only = tk.BooleanVar(value=False)
        self._hide_ui_while_drawing = tk.BooleanVar(value=True)
        for var in (
                self._dither_style, self._planner_mode,
                self._auto_brush_mode, self._max_auto_brush):
            var.trace_add("write", lambda *_: self._refresh_estimate())

        self._build_ui()
        self._try_load_calibration()
        self._refresh_estimate()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header
        hdr = tk.Frame(self.root, bg=self.bg)
        hdr.pack(fill="x", padx=20, pady=(16, 2))
        tk.Label(hdr, text="PAINT  DRAWBOT  v5.0",
                 font=self.hfont, bg=self.bg, fg=self.acc).pack(side="left")
        self._cal_dot = tk.Label(hdr, text="●", font=("Consolas", 14),
                                  bg=self.bg, fg="#333")
        self._cal_dot.pack(side="right", padx=(0, 2))
        tk.Label(hdr, text="cal", font=("Consolas", 8),
                 bg=self.bg, fg=self.muted).pack(side="right")

        card = tk.Frame(self.root, bg=self.card, padx=18, pady=14)
        card.pack(fill="x", padx=20, pady=8)

        # Top button row
        top = tk.Frame(card, bg=self.card)
        top.pack(fill="x", pady=(0, 4))
        self._cal_btn  = self._mk_btn(top, "[ CALIBRATE ]",  self._start_calibration, self.warn, "left")
        self._sel_btn  = self._mk_btn(top, "[ SELECT IMAGE ]", self._select_image,    self.acc,  "right")

        top2 = tk.Frame(card, bg=self.card)
        top2.pack(fill="x", pady=(0, 4))
        self._test_btn  = self._mk_btn(top2, "[ TEST STROKE ]",  self._test_stroke, "#5bc4ff", "left")
        self._debug_btn = self._mk_btn(top2, "[ DEBUG TIMING ]", self._open_debug,  "#c084fc", "right")

        top3 = tk.Frame(card, bg=self.card)
        top3.pack(fill="x", pady=(0, 4))
        self._log_btn = self._mk_btn(top3, "[ OPEN LOG ]", self._open_log, self.muted, "left")
        self._tools_btn = self._mk_btn(top3, "[ CAL TOOLS ]", self._start_tools_calibration, "#7dd3fc", "right")

        top4 = tk.Frame(card, bg=self.card)
        top4.pack(fill="x", pady=(0, 8))
        self._preview_btn = self._mk_btn(top4, "[ PREVIEW ]", self._preview_conversion, "#34d399", "left")
        self._brush_test_btn = self._mk_btn(top4, "[ TEST BRUSHES ]", self._test_brush_ladder, "#f59e0b", "right")

        self._img_lbl = tk.Label(card, text="  no image selected",
                                  font=("Consolas", 9, "italic"),
                                  bg=self.card, fg=self.muted)
        self._img_lbl.pack(anchor="w", pady=(0, 6))

        self._sep(card)

        # ── Detail level ───────────────────────────────────────────────────────
        tk.Label(card, text="DETAIL LEVEL",
                 font=("Consolas", 9), bg=self.card, fg=self.muted).pack(anchor="w")
        sl1 = tk.Frame(card, bg=self.card)
        sl1.pack(fill="x", pady=(2, 0))
        tk.Label(sl1, text="1 (fast)", font=("Consolas", 9),
                 bg=self.card, fg=self.muted).pack(side="left")
        ttk.Scale(sl1, from_=self.DETAIL_MIN, to=self.DETAIL_MAX,
                  orient="horizontal",
                  variable=self._detail, length=220,
                  command=lambda _: self._refresh_estimate()).pack(side="left", padx=6)
        tk.Label(sl1, text=f"{self.DETAIL_MAX} (1px ultra)",
                 font=("Consolas", 9),
                 bg=self.card, fg=self.muted).pack(side="left")
        self._detail_lbl = tk.Label(card, text="",
                                     font=("Consolas", 9),
                                     bg=self.card, fg=self.muted)
        self._detail_lbl.pack(anchor="w", pady=(2, 0))

        self._sep(card)

        # ── Stroke speed ───────────────────────────────────────────────────────
        tk.Label(card,
                 text="STROKE SPEED  (ms per stroke — lower=faster, may miss)",
                 font=("Consolas", 9), bg=self.card, fg=self.muted).pack(anchor="w")
        sl2 = tk.Frame(card, bg=self.card)
        sl2.pack(fill="x", pady=(2, 0))
        tk.Label(sl2, text="0 ms", font=("Consolas", 9),
                 bg=self.card, fg=self.muted).pack(side="left")
        ttk.Scale(sl2, from_=0, to=80, orient="horizontal",
                  variable=self._speed_ms, length=220,
                  command=lambda _: self._refresh_estimate()).pack(side="left", padx=6)
        tk.Label(sl2, text="80 ms", font=("Consolas", 9),
                 bg=self.card, fg=self.muted).pack(side="left")
        self._speed_lbl = tk.Label(card, text="",
                                    font=("Consolas", 9), bg=self.card, fg=self.muted)
        self._speed_lbl.pack(anchor="w", pady=(2, 0))

        self._sep(card)

        # ── Brush size ─────────────────────────────────────────────────────────
        brow = tk.Frame(card, bg=self.card)
        brow.pack(fill="x", pady=(0, 4))
        tk.Label(brow, text="BRUSH SIZE (px):",
                 font=("Consolas", 9), bg=self.card, fg=self.muted).pack(side="left")
        for px in [1, 2, 3, 4, 5, 6, 8]:
            tk.Radiobutton(
                brow, text=str(px), variable=self._brush_px, value=px,
                bg=self.card, fg=self.acc, selectcolor="#1a1a1c",
                activebackground=self.card, font=("Consolas", 9),
                command=self._refresh_estimate,
            ).pack(side="left", padx=3)

        self._sanity_lbl = tk.Label(
            card, text="", font=("Consolas", 8),
            bg=self.card, fg=self.warn, anchor="w", justify="left"
        )
        self._sanity_lbl.pack(fill="x", pady=(0, 4))

        fast_row = tk.Frame(card, bg=self.card)
        fast_row.pack(fill="x", pady=(0, 4))
        tk.Checkbutton(
            fast_row,
            text="Batch colours",
            variable=self._batch_colours,
            bg=self.card, fg=self.warn, selectcolor="#1a1a1c",
            activebackground=self.card, font=("Consolas", 9),
            command=self._refresh_estimate,
        ).pack(side="left", padx=(0, 10))
        tk.Checkbutton(
            fast_row,
            text="Skip white (blank canvas)",
            variable=self._skip_white,
            bg=self.card, fg=self.warn, selectcolor="#1a1a1c",
            activebackground=self.card, font=("Consolas", 9),
            command=self._refresh_estimate,
        ).pack(side="left")
        fast_row2 = tk.Frame(card, bg=self.card)
        fast_row2.pack(fill="x", pady=(0, 4))
        tk.Checkbutton(
            fast_row2,
            text="Smart underpaint",
            variable=self._smart_underpaint,
            bg=self.card, fg=self.warn, selectcolor="#1a1a1c",
            activebackground=self.card, font=("Consolas", 9),
            command=self._refresh_estimate,
        ).pack(side="left", padx=(0, 10))
        tk.Checkbutton(
            fast_row2,
            text="Merge tiny noise",
            variable=self._merge_tiny_runs,
            bg=self.card, fg=self.warn, selectcolor="#1a1a1c",
            activebackground=self.card, font=("Consolas", 9),
            command=self._refresh_estimate,
        ).pack(side="left")
        fast_row3 = tk.Frame(card, bg=self.card)
        fast_row3.pack(fill="x", pady=(0, 4))
        tk.Label(fast_row3, text="Dither style:",
                 font=("Consolas", 9), bg=self.card, fg=self.muted
                 ).pack(side="left", padx=(0, 4))
        ttk.Combobox(
            fast_row3,
            textvariable=self._dither_style,
            values=[item.value for item in DitherPreset],
            width=14,
            state="readonly",
        ).pack(side="left", padx=(0, 10))
        tk.Checkbutton(
            fast_row3,
            text="Hide UI while drawing",
            variable=self._hide_ui_while_drawing,
            bg=self.card, fg=self.warn, selectcolor="#1a1a1c",
            activebackground=self.card, font=("Consolas", 9),
        ).pack(side="left")
        fast_row4 = tk.Frame(card, bg=self.card)
        fast_row4.pack(fill="x", pady=(0, 4))
        tk.Checkbutton(
            fast_row4,
            text="Grayscale only",
            variable=self._grayscale_only,
            bg=self.card, fg=self.warn, selectcolor="#1a1a1c",
            activebackground=self.card, font=("Consolas", 9),
            command=self._refresh_estimate,
        ).pack(side="left", padx=(0, 10))
        tk.Label(fast_row4, text="Planner:",
                 font=("Consolas", 9), bg=self.card, fg=self.muted
                 ).pack(side="left", padx=(0, 4))
        ttk.Combobox(
            fast_row4,
            textvariable=self._planner_mode,
            values=[item.value for item in PlannerMode],
            width=8,
            state="readonly",
        ).pack(side="left")
        fast_row5 = tk.Frame(card, bg=self.card)
        fast_row5.pack(fill="x", pady=(0, 4))
        tk.Checkbutton(
            fast_row5,
            text="Paint fill accelerator",
            variable=self._paint_fill_accel,
            bg=self.card, fg=self.warn, selectcolor="#1a1a1c",
            activebackground=self.card, font=("Consolas", 9),
            command=self._refresh_estimate,
        ).pack(side="left", padx=(0, 10))
        tk.Label(fast_row5, text="Auto brush:",
                 font=("Consolas", 9), bg=self.card, fg=self.muted
                 ).pack(side="left", padx=(0, 4))
        ttk.Combobox(
            fast_row5,
            textvariable=self._auto_brush_mode,
            values=[item.value for item in AutoBrushMode],
            width=12,
            state="readonly",
        ).pack(side="left")

        fast_row6 = tk.Frame(card, bg=self.card)
        fast_row6.pack(fill="x", pady=(0, 4))
        tk.Label(fast_row6, text="Max auto brush:",
                 font=("Consolas", 9), bg=self.card, fg=self.muted
                 ).pack(side="left", padx=(0, 4))
        ttk.Combobox(
            fast_row6,
            textvariable=self._max_auto_brush,
            values=BRUSH_SIZE_STOPS,
            width=6,
            state="readonly",
        ).pack(side="left", padx=(0, 8))
        tk.Label(
            fast_row6,
            text="uses calibrated Paint size slider",
            font=("Consolas", 8),
            bg=self.card,
            fg=self.muted,
        ).pack(side="left")

        self._est_lbl = tk.Label(card, text="Calibrate first to see estimate.",
                                  font=("Consolas", 9), bg=self.card, fg=self.muted)
        self._est_lbl.pack(anchor="w", pady=(2, 6))

        self._sep(card)

        # ── Start / Stop ───────────────────────────────────────────────────────
        btn_row = tk.Frame(card, bg=self.card)
        btn_row.pack(fill="x", pady=(4, 0))
        self._start_btn = tk.Button(
            btn_row, text="▶  START", command=self._start_drawing,
            font=("Consolas", 11, "bold"), bg=self.acc, fg="#000",
            activebackground="#00c080", activeforeground="#000",
            relief="flat", cursor="hand2", pady=8, state="disabled"
        )
        self._start_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._stop_btn = tk.Button(
            btn_row, text="⏹  STOP", command=self._stop_action,
            font=("Consolas", 11, "bold"), bg=self.err, fg="#000",
            activebackground="#cc0000", activeforeground="#000",
            relief="flat", cursor="hand2", pady=8, state="disabled"
        )
        self._stop_btn.pack(side="right", fill="x", expand=True, padx=(4, 0))

        # ── Status bar ─────────────────────────────────────────────────────────
        sbar = tk.Frame(self.root, bg=self.bg)
        sbar.pack(fill="x", padx=20, pady=(0, 4))
        self._status_lbl = tk.Label(
            sbar, text="Step 1: Click CALIBRATE (or load saved cal).",
            font=("Consolas", 9), bg=self.bg, fg="#5bc4ff",
            anchor="w", wraplength=480, justify="left"
        )
        self._status_lbl.pack(fill="x")

        self._pbar_cv = tk.Canvas(self.root, height=4, bg="#1c1c1e",
                                   highlightthickness=0)
        self._pbar_cv.pack(fill="x", padx=20, pady=(0, 4))
        self._pbar_rect = self._pbar_cv.create_rectangle(0, 0, 0, 4,
                                                          fill=self.acc, width=0)

        tk.Label(
            self.root,
            text="ESC → pause/resume  |  mouse corner → abort  "
                 "|  [ DEBUG TIMING ] → live tune all delays",
            font=("Consolas", 8), bg=self.bg, fg="#2a2a2a"
        ).pack(pady=(0, 12))

    # ── UI helpers ─────────────────────────────────────────────────────────────

    def _mk_btn(self, parent, text, cmd, fg, side) -> tk.Button:
        b = tk.Button(parent, text=text, command=cmd,
                      font=self.font, bg="#202022", fg=fg,
                      activebackground="#2c2c2e", activeforeground=fg,
                      relief="flat", cursor="hand2", padx=10, pady=5)
        padx = (0, 4) if side == "left" else (4, 0)
        b.pack(side=side, fill="x", expand=True, padx=padx)
        return b

    def _sep(self, parent) -> None:
        tk.Frame(parent, bg="#2a2a2c", height=1).pack(fill="x", pady=6)

    def _set_status(self, text: str, colour: str = "#5bc4ff") -> None:
        flat = text.replace("\n", " ")
        transient = flat.startswith(("Starting draw", "Quantising", "Planning", "Drawing"))
        if transient:
            _log.debug("STATUS: %s", flat)
        else:
            _log.info("STATUS: %s", flat)
        if self._draw_ui_hidden and transient:
            return
        self.root.after(0, lambda: self._status_lbl.config(text=text, fg=colour))

    def _set_progress(self, pct: float) -> None:
        if self._draw_ui_hidden and pct < 100:
            return

        def _up() -> None:
            w = self._pbar_cv.winfo_width()
            self._pbar_cv.coords(self._pbar_rect, 0, 0, int(w * pct / 100), 4)
        self.root.after(0, _up)

    def _set_cal_dot(self, ok: bool) -> None:
        col = self.acc if ok else "#333"
        self.root.after(0, lambda: self._cal_dot.config(fg=col))

    def _hide_ui_for_draw(self) -> None:
        if not self._hide_ui_while_drawing.get():
            return
        self._draw_ui_hidden = True
        if self._debug_panel is not None and self._debug_panel.winfo_exists():
            self._debug_panel.withdraw()
        self.root.withdraw()

    def _show_ui_after_draw(self) -> None:
        if not self._draw_ui_hidden:
            return

        def _show() -> None:
            self.root.deiconify()
            if self._debug_panel is not None and self._debug_panel.winfo_exists():
                self._debug_panel.deiconify()
            self._draw_ui_hidden = False

        self.root.after(0, _show)

    def _re_enable(self) -> None:
        self._show_ui_after_draw()
        self.root.after(0, self._check_start_ready)
        self.root.after(0, lambda: self._sel_btn.config(state="normal"))
        self.root.after(0, lambda: self._cal_btn.config(state="normal"))
        self.root.after(0, lambda: self._tools_btn.config(state="normal"))
        self.root.after(0, lambda: self._test_btn.config(state="normal"))
        self.root.after(0, lambda: self._preview_btn.config(state="normal"))
        self.root.after(0, lambda: self._brush_test_btn.config(state="normal"))
        self.root.after(0, lambda: self._stop_btn.config(state="disabled"))

    def _check_start_ready(self) -> None:
        ok = self._is_calibrated and bool(self._image_path)
        self._start_btn.config(state="normal" if ok else "disabled")

    def _open_log(self) -> None:
        try:
            os.startfile(_LOG_FILE)
        except Exception as exc:
            self._set_status(f"Cannot open log: {exc}", self.err)

    def _open_debug(self) -> None:
        """Open (or raise) the live Timing Debug Panel."""
        if self._debug_panel is None or not self._debug_panel.winfo_exists():
            self._debug_panel = DebugPanel(self.root)
        else:
            self._debug_panel.lift()
            self._debug_panel.focus_force()

    # ── Step / brush sanity ────────────────────────────────────────────────────

    def _step(self) -> int:
        """Pixel step between sampled rows/columns; high detail reaches 1 px."""
        detail = max(
            self.DETAIL_MIN,
            min(self.DETAIL_MAX, int(round(float(self._detail.get())))),
        )
        return max(1, int(round(24 / detail)))

    def _grid_dims(self, step: int | None = None) -> tuple[int, int]:
        step = step or self._step()
        cols = max(1, (self.canvas_w + step - 1) // step)
        rows = max(1, (self.canvas_h + step - 1) // step)
        return cols, rows

    def _stroke_duration(self) -> float:
        return self._speed_ms.get() / 1000.0

    def _dither_preset(self) -> DitherPreset:
        try:
            return DitherPreset(self._dither_style.get())
        except ValueError:
            return DitherPreset.SMART

    def _planner(self) -> PlannerMode:
        try:
            return PlannerMode(self._planner_mode.get())
        except ValueError:
            return PlannerMode.AUTO

    def _auto_brush(self) -> AutoBrushMode:
        try:
            return AutoBrushMode(self._auto_brush_mode.get())
        except ValueError:
            return AutoBrushMode.OFF

    def _use_dither(self, step: int | None = None) -> bool:
        step = step or self._step()
        return self._dither_preset() != DitherPreset.FLAT and step <= 2

    def _active_palette_indices(self) -> list[int]:
        force_gray = self._dither_preset() == DitherPreset.CLEAN
        if not self._grayscale_only.get() and not force_gray:
            return list(range(len(self.palette)))

        indices: list[int] = []
        gray_names = {"black", "dark grey", "dark gray", "light grey",
                      "light gray", "white"}
        for idx, colour in enumerate(self.palette):
            r, g, b = colour["rgb"]
            name = str(colour["name"]).strip().lower()
            if name in gray_names or (r == g == b):
                indices.append(idx)

        return indices or list(range(len(self.palette)))

    def _brush_sanity(self) -> tuple[bool, str]:
        """
        Returns (ok, message).
        ok=False means gaps between rows are expected.
        """
        if not self._is_calibrated:
            return True, ""
        step = self._step()
        brush = self._brush_px.get()
        cols, rows = self._grid_dims(step)

        if step > brush:
            gap = step - brush
            msg = (f"Gap warning: step={step}px > brush={brush}px "
                   f"({gap}px gap). Increase detail or use a larger brush.")
            return False, msg
        elif step < brush:
            ov = brush - step
            msg = (f"Ultra overlap: brush={brush}px > step={step}px "
                   f"({ov}px overlap; more detail, more strokes).")
            return True, msg
        else:
            msg = f"Perfect fit: brush={brush}px = step={step}px, {cols}x{rows} grid."
            return True, msg

    def _refresh_estimate(self, *_) -> None:
        spd = self._speed_ms.get()
        self._speed_lbl.config(text=f"  current: {spd} ms/stroke")

        detail = max(
            self.DETAIL_MIN,
            min(self.DETAIL_MAX, int(round(float(self._detail.get())))),
        )
        step = self._step()
        if hasattr(self, "_detail_lbl"):
            self._detail_lbl.config(
                text=f"  current: {detail}/{self.DETAIL_MAX}  step={step}px"
            )

        ok, sanity_msg = self._brush_sanity()
        fg = self.warn if not ok else (
            self.muted if "overlap" in sanity_msg.lower() else self.acc
        )
        self._sanity_lbl.config(text=sanity_msg, fg=fg)

        if not self._is_calibrated:
            self._est_lbl.config(text="Calibrate first to see time estimate.")
            return

        cols, rows = self._grid_dims(step)

        # Overhead per colour change: pal click + optional re-arm
        pal_oh = (_cfg.pal_delay_ms.get() + _cfg.click_pre_down_ms.get()
                  + _cfg.click_pre_up_ms.get()) / 1000.0
        if not _cfg.skip_pencil_rearm.get():
            pal_oh += _cfg.pencil_rearm_ms.get() / 1000.0

        approx_strokes = rows * max(1, cols // 3)
        if self._skip_white.get():
            approx_strokes = max(1, int(approx_strokes * 0.85))
        if self._batch_colours.get() and self._smart_underpaint.get():
            approx_strokes = max(1, int(approx_strokes * 0.75))
        if self._merge_tiny_runs.get():
            approx_strokes = max(1, int(approx_strokes * 0.85))
        planner = self._planner()
        fast_planner = planner != PlannerMode.EXACT
        if fast_planner:
            approx_strokes = max(1, int(approx_strokes * 0.55))
        fill_ready = self.fill_tool is not None and self.eraser_tool is not None
        if self._paint_fill_accel.get() and fast_planner and fill_ready:
            approx_strokes = max(1, int(approx_strokes * 0.85))
        auto_brush_ready = (
            self.brush_cal is not None
            and self._auto_brush() != AutoBrushMode.OFF
        )
        if auto_brush_ready:
            approx_strokes = max(1, int(approx_strokes * 0.7))
        if self._batch_colours.get():
            approx_colour_changes = min(
                len(self._active_palette_indices()), approx_strokes
            )
            mode = "smart" if self._smart_underpaint.get() else "batched"
        else:
            approx_colour_changes = rows * 2   # rough estimate
            mode = "row"
        if self._grayscale_only.get() or self._dither_preset() == DitherPreset.CLEAN:
            mode += "+gray"
        if self._use_dither(step):
            mode += "+" + self._dither_preset().value.lower().split()[0]
            if self._batch_colours.get() and step == 1 and self._brush_px.get() == 1:
                mode += "+lines"
                approx_strokes = max(1, int(approx_strokes * 0.65))
        mode += f"+{planner.value.lower()}"
        if auto_brush_ready:
            mode += f"+brush{self._max_auto_brush.get()}"
        if self._paint_fill_accel.get() and fast_planner and fill_ready:
            mode += "+fill"
        secs = approx_strokes * self._stroke_duration() + approx_colour_changes * pal_oh
        mins = secs / 60

        self._est_lbl.config(
            text=(f"step={step}px  grid={cols}×{rows}  "
                  f"~{rows*cols:,} dots  mode={mode}  ≈{mins:.1f} min")
        )

    # ── Calibration persistence ────────────────────────────────────────────────

    def _save_calibration(self) -> None:
        data = {
            "schema_version": CAL_SCHEMA_VERSION,
            "pencil_tool": list(self.pencil_tool),
            "canvas_x":    self.canvas_x,
            "canvas_y":    self.canvas_y,
            "canvas_w":    self.canvas_w,
            "canvas_h":    self.canvas_h,
            "palette": [
                {"name": c["name"], "rgb": list(c["rgb"]), "pos": list(c["pos"])}
                for c in self.palette
            ],
        }
        tools = {}
        if self.fill_tool is not None:
            tools["fill_tool"] = list(self.fill_tool)
        if self.eraser_tool is not None:
            tools["eraser_tool"] = list(self.eraser_tool)
        if tools:
            data["tools"] = tools
        if self.brush_cal is not None:
            data["brush_size"] = {
                "size_button": list(self.brush_cal.size_button),
                "slider_bottom": list(self.brush_cal.slider_bottom),
                "slider_top": list(self.brush_cal.slider_top),
                "min_px": self.brush_cal.min_px,
                "max_px": self.brush_cal.max_px,
            }
        with open(CAL_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        _log.info("Calibration saved to %s", CAL_FILE)

    def _try_load_calibration(self) -> None:
        if not os.path.exists(CAL_FILE):
            _log.info("No saved calibration at %s", CAL_FILE)
            return
        try:
            with open(CAL_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            schema = int(data.get("schema_version", 1))
            if schema not in (1, 2, 3):
                raise ValueError(f"unsupported calibration schema {schema}")

            def _coord(value, name: str) -> tuple[int, int]:
                if (not isinstance(value, (list, tuple)) or len(value) != 2):
                    raise ValueError(f"{name} must be a coordinate pair")
                return int(value[0]), int(value[1])

            self.pencil_tool = _coord(data["pencil_tool"], "pencil_tool")
            self.canvas_x    = int(data["canvas_x"])
            self.canvas_y    = int(data["canvas_y"])
            self.canvas_w    = int(data["canvas_w"])
            self.canvas_h    = int(data["canvas_h"])
            if self.canvas_w <= 0 or self.canvas_h <= 0:
                raise ValueError("canvas dimensions must be positive")
            if len(data.get("palette", [])) != len(BASE_PALETTE):
                raise ValueError("palette must contain 20 entries")
            self.palette     = [
                {
                    "name": c["name"],
                    "rgb": tuple(int(v) for v in c["rgb"]),
                    "pos": _coord(c["pos"], f"palette {c.get('name', '?')}"),
                }
                for c in data["palette"]
            ]
            tools = data.get("tools", {})
            self.fill_tool = (
                _coord(tools["fill_tool"], "fill_tool")
                if tools.get("fill_tool") else None
            )
            self.eraser_tool = (
                _coord(tools["eraser_tool"], "eraser_tool")
                if tools.get("eraser_tool") else None
            )
            brush_data = data.get("brush_size")
            self.brush_cal = None
            if brush_data:
                self.brush_cal = BrushSizeCalibration(
                    size_button=_coord(brush_data["size_button"], "size_button"),
                    slider_bottom=_coord(
                        brush_data["slider_bottom"], "slider_bottom"
                    ),
                    slider_top=_coord(brush_data["slider_top"], "slider_top"),
                    min_px=int(brush_data.get("min_px", 1)),
                    max_px=int(brush_data.get("max_px", 248)),
                )
                if self._auto_brush_mode.get() == AutoBrushMode.OFF.value:
                    self._auto_brush_mode.set(AutoBrushMode.SAFE.value)
            self._pal_rgb       = [tuple(c["rgb"]) for c in self.palette]
            self._is_calibrated = True
            self._set_cal_dot(True)
            self._refresh_estimate()
            self._set_status("Loaded saved calibration. Select an image to begin.",
                             self.acc)
            self._check_start_ready()
            _log.info("Calibration loaded: pencil=%s canvas=(%d,%d)+%dx%d palette=%d tools=%s",
                      self.pencil_tool, self.canvas_x, self.canvas_y,
                      self.canvas_w, self.canvas_h, len(self.palette),
                      bool(self.fill_tool and self.eraser_tool))
        except Exception as exc:
            _log.exception("Failed to load calibration: %s", exc)
            self._set_status(f"Could not load saved calibration: {exc}", self.warn)

    # ── Calibration flow ───────────────────────────────────────────────────────

    def _start_calibration(self) -> None:
        _stop_event.set()
        _pause_event.clear()
        time.sleep(0.1)
        _stop_event.clear()

        for btn in (self._cal_btn, self._sel_btn, self._test_btn,
                    self._tools_btn, self._preview_btn,
                    self._brush_test_btn, self._start_btn):
            btn.config(state="disabled")
        self._stop_btn.config(state="normal")

        hwnd = _find_paint_hwnd()
        if not hwnd:
            self._set_status("MS Paint not found. Open it, then try again.", self.err)
            self._re_enable()
            return

        _log_paint_rect(hwnd)
        _focus_paint(hwnd)
        threading.Thread(target=self._calibration_thread, daemon=True).start()

    def _calibration_thread(self) -> None:
        _log.info("Calibration thread started")
        steps = [
            ("PENCIL TOOL",
             "Hover over the Pencil icon → press SPACE"),
            ("CANVAS  TOP-LEFT",
             "Hover over the TOP-LEFT corner of the white canvas → press SPACE"),
            ("CANVAS  BOTTOM-RIGHT",
             "Hover over the BOTTOM-RIGHT corner of the canvas → press SPACE"),
            ("PALETTE: BLACK",
             "Hover over the first swatch (Black, top-left of palette) → press SPACE"),
            ("PALETTE: PURPLE",
             "Hover over the LAST swatch on row 1 (Purple, top-right) → press SPACE"),
            ("PALETTE: WHITE",
             "Hover over the FIRST swatch on row 2 (White, bottom-left) → press SPACE"),
        ]
        coords: list[tuple] = []

        for name, prompt in steps:
            self._set_status(f"▸ [{name}]\n{prompt}", self.warn)
            while keyboard.is_pressed("space"):
                time.sleep(0.05)
            while not keyboard.is_pressed("space"):
                if _stop_event.is_set():
                    _log.info("Calibration aborted at: %s", name)
                    self._set_status("Calibration aborted.", self.err)
                    self._re_enable()
                    return
                time.sleep(0.05)

            pt = win32api.GetCursorPos()
            coords.append(pt)
            _log.info("CAL step [%s] → %s", name, pt)
            self._set_status(f"✓  {name}  @  {pt}", self.acc)
            time.sleep(0.4)

        try:
            self.pencil_tool = coords[0]
            self.canvas_x    = coords[1][0]
            self.canvas_y    = coords[1][1]
            self.canvas_w    = coords[2][0] - coords[1][0]
            self.canvas_h    = coords[2][1] - coords[1][1]

            _log.info("Canvas: x=%d y=%d w=%d h=%d",
                      self.canvas_x, self.canvas_y, self.canvas_w, self.canvas_h)

            if self.canvas_w <= 0 or self.canvas_h <= 0:
                raise ValueError(
                    f"Canvas size invalid: {self.canvas_w}×{self.canvas_h}. "
                    "Bottom-right must be right of and below top-left."
                )

            p_black, p_purple, p_white = coords[3], coords[4], coords[5]
            dx   = (p_purple[0] - p_black[0]) / 9.0
            y_r1 = p_black[1]
            y_r2 = p_white[1]

            _log.info("Palette: black=%s purple=%s white=%s  dx=%.1f",
                      p_black, p_purple, p_white, dx)

            self.palette = []
            for base in BASE_PALETTE:
                row, col = base["grid"]
                x = int(p_black[0] + (col - 1) * dx)
                y = y_r1 if row == 1 else y_r2
                self.palette.append({
                    "name": base["name"], "rgb": base["rgb"], "pos": (x, y)
                })

            self._pal_rgb       = [tuple(c["rgb"]) for c in self.palette]
            self._is_calibrated = True
            self._set_cal_dot(True)
            self._save_calibration()
            self.root.after(0, self._refresh_estimate)
            self._set_status("🎉 Calibration complete!  Select an image and set BRUSH SIZE.",
                             self.acc)

        except Exception as exc:
            _log.exception("Calibration computation failed: %s", exc)
            self._set_status(f"Calibration failed: {exc}", self.err)
            self._is_calibrated = False
            self._set_cal_dot(False)

        self._re_enable()

    # ── Tool calibration ───────────────────────────────────────────────────────

    def _start_tools_calibration(self) -> None:
        if not self._is_calibrated:
            self._set_status("Calibrate canvas/palette first, then calibrate tools.", self.warn)
            return

        _stop_event.set()
        _pause_event.clear()
        time.sleep(0.1)
        _stop_event.clear()

        for btn in (self._cal_btn, self._sel_btn, self._test_btn,
                    self._tools_btn, self._preview_btn,
                    self._brush_test_btn, self._start_btn):
            btn.config(state="disabled")
        self._stop_btn.config(state="normal")

        hwnd = _find_paint_hwnd()
        if not hwnd:
            self._set_status("MS Paint not found. Open it, then try again.", self.err)
            self._re_enable()
            return

        _log_paint_rect(hwnd)
        _focus_paint(hwnd)
        threading.Thread(target=self._tools_calibration_thread, daemon=True).start()

    def _tools_calibration_thread(self) -> None:
        _log.info("Tool calibration thread started")
        steps = [
            ("FILL BUCKET TOOL",
             "Hover over the Fill Bucket icon → press SPACE"),
            ("ERASER TOOL",
             "Hover over the Eraser icon → press SPACE"),
            ("SIZE BUTTON",
             "Hover over the Paint Size button/menu -> press SPACE"),
            ("SIZE SLIDER BOTTOM",
             "Open Size, hover the slider bottom (1px) -> press SPACE"),
            ("SIZE SLIDER TOP",
             "Hover the slider top (248px) -> press SPACE"),
        ]
        coords: list[tuple[int, int]] = []

        for name, prompt in steps:
            self._set_status(f"▸ [{name}]\n{prompt}", self.warn)
            while keyboard.is_pressed("space"):
                time.sleep(0.05)
            while not keyboard.is_pressed("space"):
                if _stop_event.is_set():
                    _log.info("Tool calibration aborted at: %s", name)
                    self._set_status("Tool calibration aborted.", self.err)
                    self._re_enable()
                    return
                time.sleep(0.05)

            pt = win32api.GetCursorPos()
            coords.append(pt)
            _log.info("TOOL CAL step [%s] → %s", name, pt)
            self._set_status(f"✓  {name}  @  {pt}", self.acc)
            time.sleep(0.4)

        self.fill_tool = coords[0]
        self.eraser_tool = coords[1]
        self.brush_cal = BrushSizeCalibration(
            size_button=coords[2],
            slider_bottom=coords[3],
            slider_top=coords[4],
        )
        self._current_paint_brush = None
        self._auto_brush_mode.set(AutoBrushMode.SAFE.value)
        self._save_calibration()
        self.root.after(0, self._refresh_estimate)
        self._set_status(
            "Tool calibration complete. Fill and auto brush are available.",
            self.acc,
        )
        self._re_enable()

    # ── Image selection ────────────────────────────────────────────────────────

    def _select_image(self) -> None:
        path = filedialog.askopenfilename(
            title="Select image",
            filetypes=[("Images",
                        "*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tiff")]
        )
        if not path:
            return
        self._image_path = path
        _log.info("Image selected: %s", path)
        name = path.replace("\\", "/").split("/")[-1]
        self._img_lbl.config(text=f"  {name}", fg="#c8c8c8")
        self._set_status("Image loaded. Set BRUSH SIZE, then press START.")
        self._check_start_ready()

    # ── Stop ───────────────────────────────────────────────────────────────────

    def _stop_action(self) -> None:
        _log.info("STOP clicked")
        _stop_event.set()
        _pause_event.clear()
        self._set_status("Stopping…", self.err)

    # ── Test stroke ────────────────────────────────────────────────────────────

    def _test_stroke(self) -> None:
        if not self._is_calibrated:
            self._set_status("Calibrate first before testing.", self.warn)
            return
        self._test_btn.config(state="disabled")
        threading.Thread(target=self._test_stroke_thread, daemon=True).start()

    def _test_stroke_thread(self) -> None:
        _log.info("=== TEST STROKE ===")
        hwnd = _find_paint_hwnd()
        if not hwnd:
            self._set_status("Paint not found for test stroke.", self.err)
            self.root.after(0, lambda: self._test_btn.config(state="normal"))
            return

        _log_paint_rect(hwnd)
        self._set_status("Test stroke: focusing Paint now…", self.warn)
        _nap(0.4)
        _focus_paint(hwnd)

        _mouse_click(*self.pencil_tool, delay=0.08)
        black = next((c for c in self.palette if c["name"] == "Black"), self.palette[0])
        _mouse_click(*black["pos"], delay=0.05)
        _mouse_click(*self.pencil_tool, delay=0.08)

        for i, frac in enumerate([0.1, 0.3, 0.5]):
            ty  = self.canvas_y + int(self.canvas_h * frac)
            tx1 = self.canvas_x + 2
            tx2 = self.canvas_x + min(100, self.canvas_w - 4)
            _log.info("Test stroke %d: (%d,%d)→(%d,%d)", i + 1, tx1, ty, tx2, ty)
            self._set_status(
                f"Test stroke {i+1}/3 at y={ty}  "
                f"(speed: {self._speed_ms.get()} ms)  Watch Paint!",
                "#5bc4ff"
            )
            _stroke(tx1, ty, tx2, self._stroke_duration())
            _nap(0.05)

        _log.info("Test stroke complete.")
        self._set_status(
            f"Test done. Check Paint for 3 lines.  "
            f"If missing: increase delays in [ DEBUG TIMING ].",
            self.acc
        )
        self.root.after(0, lambda: self._test_btn.config(state="normal"))

    def _set_brush_size(
            self,
            px: int,
            hwnd: int | None = None,
            click_pre_down: float | None = None,
            click_pre_up: float | None = None,
            post_delay: float | None = None) -> int:
        snapped = _nearest_brush_size(px)
        if self.brush_cal is None:
            return snapped
        if self._current_paint_brush == snapped:
            return snapped

        post = (
            post_delay if post_delay is not None
            else max(0.02, _cfg.pal_delay_ms.get() / 1000.0)
        )
        _log.info("Brush size -> %dpx", snapped)
        if hwnd is not None and not _paint_has_focus(hwnd):
            _focus_paint(hwnd, delay=0.08)
        _mouse_click(
            *self.brush_cal.size_button,
            delay=post,
            pre_down=click_pre_down,
            pre_up=click_pre_up,
        )
        _mouse_click(
            *self.brush_cal.slider_pos_for_size(snapped),
            delay=post,
            pre_down=click_pre_down,
            pre_up=click_pre_up,
        )
        _mouse_click(
            *self.pencil_tool,
            delay=post,
            pre_down=click_pre_down,
            pre_up=click_pre_up,
        )
        self._current_paint_brush = snapped
        return snapped

    def _test_brush_ladder(self) -> None:
        if not self._is_calibrated or self.brush_cal is None:
            self._set_status(
                "Calibrate tools + size slider before brush test.", self.warn
            )
            return
        self._brush_test_btn.config(state="disabled")
        threading.Thread(target=self._test_brush_ladder_thread, daemon=True).start()

    def _test_brush_ladder_thread(self) -> None:
        _log.info("=== TEST BRUSH LADDER ===")
        hwnd = _find_paint_hwnd()
        if not hwnd:
            self._set_status("Paint not found for brush ladder.", self.err)
            self.root.after(0, lambda: self._brush_test_btn.config(state="normal"))
            return

        _focus_paint(hwnd)
        black = next((c for c in self.palette if c["name"] == "Black"), self.palette[0])
        self._pick_colour(black, hwnd, skip_pencil_rearm=False)
        x1 = self.canvas_x + 8
        x2 = self.canvas_x + min(220, self.canvas_w - 12)
        usable_h = max(1, self.canvas_h - 40)
        for i, size in enumerate(BRUSH_LADDER_TEST_SIZES):
            if _stop_event.is_set():
                break
            y = self.canvas_y + 20 + int(
                usable_h * i / max(1, len(BRUSH_LADDER_TEST_SIZES))
            )
            self._set_status(f"Brush ladder: {size}px", "#5bc4ff")
            self._set_brush_size(size, hwnd)
            _stroke(x1, y, x2, self._stroke_duration())
            _nap(0.05)

        self._set_brush_size(self._brush_px.get(), hwnd)
        self._set_status("Brush ladder done. Check Paint for size samples.", self.acc)
        self.root.after(0, lambda: self._brush_test_btn.config(state="normal"))

    def _preview_conversion(self) -> None:
        if not self._is_calibrated or not self._image_path:
            self._set_status("Select an image after calibration to preview.", self.warn)
            return
        self._preview_btn.config(state="disabled")
        threading.Thread(target=self._preview_conversion_thread, daemon=True).start()

    def _preview_conversion_thread(self) -> None:
        try:
            step = self._step()
            cols, rows = self._grid_dims(step)
            img = Image.open(self._image_path).convert("RGB")
            img = img.resize((cols, rows), Image.Resampling.LANCZOS)
            active_indices = self._active_palette_indices()
            active_pal_rgb = [tuple(self.palette[idx]["rgb"]) for idx in active_indices]
            result = _index_image_data(
                img, active_pal_rgb, self._dither_preset(), self._planner()
            )
            pixel_indices = result.pixel_indices
            if active_indices != list(range(len(self.palette))):
                index_map = bytes(
                    active_indices[i] if i < len(active_indices) else 0
                    for i in range(256)
                )
                pixel_indices = pixel_indices.translate(index_map)
            arr = np.frombuffer(pixel_indices, dtype=np.uint8).reshape((rows, cols))
            pal_arr = np.asarray([c["rgb"] for c in self.palette], dtype=np.uint8)
            preview = Image.fromarray(pal_arr[arr], "RGB")
            preview.thumbnail((460, 360), Image.Resampling.NEAREST)
            used = len(set(pixel_indices))
            approx_runs = sum(
                1 for gy in range(rows)
                for gx in range(cols)
                if gx == 0 or arr[gy, gx] != arr[gy, gx - 1]
            )

            def _show() -> None:
                top = tk.Toplevel(self.root)
                top.title("DrawBot Preview")
                top.configure(bg=self.bg)
                photo = ImageTk.PhotoImage(preview)
                self._preview_img_ref = photo
                tk.Label(top, image=photo, bg=self.bg).pack(padx=12, pady=(12, 6))
                stats = (
                    f"{cols}x{rows} grid | {used} colours | "
                    f"~{approx_runs:,} row runs | {self._dither_preset().value} | "
                    f"{result.stats.get('seconds', 0.0):.2f}s"
                )
                tk.Label(
                    top, text=stats, font=("Consolas", 9),
                    bg=self.bg, fg=self.acc,
                ).pack(padx=12, pady=(0, 12))
                self._preview_btn.config(state="normal")

            self.root.after(0, _show)
        except Exception as exc:
            _log.exception("Preview failed: %s", exc)
            self._set_status(f"Preview failed: {exc}", self.err)
            self.root.after(0, lambda: self._preview_btn.config(state="normal"))

    # ── Pick colour ────────────────────────────────────────────────────────────

    def _pick_colour(
            self,
            colour: dict,
            hwnd: int,
            pal_d: float | None = None,
            skip_pencil_rearm: bool | None = None,
            rearm_d: float | None = None,
            click_pre_down: float | None = None,
            click_pre_up: float | None = None,
            rearm_focus_delay: float | None = None) -> None:
        """
        Click palette swatch.  Optionally re-arm pencil (controlled by
        skip_pencil_rearm toggle in the Debug Panel).

        skip_pencil_rearm=True saves ~350 ms per colour change — biggest
        single speedup available.  Safe on Windows 10/11 Paint (palette
        clicks do not deselect the pencil), but test first.
        """
        pal_d = pal_d if pal_d is not None else _cfg.pal_delay_ms.get() / 1000.0
        skip_pencil_rearm = (
            skip_pencil_rearm
            if skip_pencil_rearm is not None else _cfg.skip_pencil_rearm.get()
        )
        _log.debug("pick_colour: %s @ %s", colour["name"], colour["pos"])
        _mouse_click(
            *colour["pos"],
            delay=pal_d,
            pre_down=click_pre_down,
            pre_up=click_pre_up,
        )

        if skip_pencil_rearm:
            # Still re-focus if Paint dropped focus (rare but possible)
            if not _paint_has_focus(hwnd):
                _log.debug("Focus lost after palette (no-rearm path) — re-focusing")
                _focus_paint(hwnd, delay=0.08)
        else:
            if not _paint_has_focus(hwnd):
                _log.debug("Focus lost after palette — re-focusing before re-arm")
                focus_d = (
                    rearm_focus_delay if rearm_focus_delay is not None
                    else max(0.08, _cfg.focus_delay_ms.get() / 5000.0)
                )
                _focus_paint(hwnd, delay=focus_d)
            rearm_d = (
                rearm_d if rearm_d is not None
                else _cfg.pencil_rearm_ms.get() / 1000.0
            )
            _log.debug("re-arm pencil @ %s  delay=%.3fs", self.pencil_tool, rearm_d)
            _mouse_click(
                *self.pencil_tool,
                delay=rearm_d,
                pre_down=click_pre_down,
                pre_up=click_pre_up,
            )

    # ── Drawing ────────────────────────────────────────────────────────────────

    def _start_drawing(self) -> None:
        _log.info("START clicked")

        # Sanity check brush before starting
        ok, msg = self._brush_sanity()
        if not ok:
            self._set_status(f"Brush/detail warning: {msg}", self.warn)
            # Don't block — user may still want to proceed
        else:
            _log.info("Brush sanity OK: %s", msg)

        self._start_btn.config(state="disabled")
        self._sel_btn.config(state="disabled")
        self._cal_btn.config(state="disabled")
        self._tools_btn.config(state="disabled")
        self._test_btn.config(state="disabled")
        self._preview_btn.config(state="disabled")
        self._brush_test_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._hide_ui_for_draw()
        threading.Thread(target=self._safe_draw_thread, daemon=True).start()

    # ── Draw thread ────────────────────────────────────────────────────────────

    def _safe_draw_thread(self) -> None:
        try:
            self._draw_thread()
        except Exception as exc:
            _log.exception("Draw thread crashed: %s", exc)
            self._set_status(f"Draw failed: {exc}", self.err)
            self._re_enable()

    def _draw_thread(self) -> None:
        _log.info("=== DRAW THREAD START ===")
        draw_t0 = time.monotonic()
        _pause_event.clear()
        _stop_event.clear()

        step = self._step()
        spd  = self._stroke_duration()
        cols, rows = self._grid_dims(step)
        brush_px = self._brush_px.get()

        dither_preset = self._dither_preset()
        planner_mode = self._planner()
        auto_brush_mode = self._auto_brush()
        if self.brush_cal is None:
            auto_brush_mode = AutoBrushMode.OFF
        max_auto_brush = _nearest_brush_size(self._max_auto_brush.get())
        use_dither = self._use_dither(step)
        effective_dither = dither_preset if use_dither else DitherPreset.FLAT
        grayscale_only = self._grayscale_only.get()
        active_palette_indices = self._active_palette_indices()
        active_pal_rgb = [
            tuple(self.palette[idx]["rgb"]) for idx in active_palette_indices
        ]
        # PERF: Tk variable reads cross into Tcl; snapshot draw-loop timings once.
        move_settle_ms = _cfg.move_settle_ms.get()
        press_settle_ms = _cfg.press_settle_ms.get()
        post_step_ms = _cfg.post_step_ms.get()
        release_settle_ms = _cfg.release_settle_ms.get()
        min_stroke_s = _cfg.min_stroke_ms.get() / 1000.0
        drag_steps = max(1, _cfg.drag_steps.get())
        click_pre_down_s = _cfg.click_pre_down_ms.get() / 1000.0
        click_pre_up_s = _cfg.click_pre_up_ms.get() / 1000.0
        pal_delay_s = _cfg.pal_delay_ms.get() / 1000.0
        pencil_rearm_s = _cfg.pencil_rearm_ms.get() / 1000.0
        skip_pencil_rearm = _cfg.skip_pencil_rearm.get()
        focus_delay_s = _cfg.focus_delay_ms.get() / 1000.0
        focus_interval = _cfg.focus_interval.get()
        focus_every = max(10, focus_interval)
        row_focus_interval = max(1, focus_interval)
        resume_focus_delay_s = min(0.12, focus_delay_s)
        rearm_focus_delay_s = max(0.08, focus_delay_s / 5.0)
        batch_colours = self._batch_colours.get()
        skip_white = self._skip_white.get()
        smart_underpaint_setting = self._smart_underpaint.get()
        merge_tiny_setting = self._merge_tiny_runs.get()
        max_speed_planner = planner_mode != PlannerMode.EXACT
        fill_tool_pos = self.fill_tool
        eraser_tool_pos = self.eraser_tool
        paint_fill_accel = (
            self._paint_fill_accel.get()
            and max_speed_planner
            and fill_tool_pos is not None
            and eraser_tool_pos is not None
        )
        _log.info("Draw params: step=%d  spd=%.3fs  grid=%dx%d  brush=%dpx  skip_rearm=%s  batch=%s  skip_white=%s  underpaint=%s  tiny_merge=%s  dither=%s  planner=%s  auto_brush=%s/%d  grayscale=%s  active_colours=%d  max_speed=%s  fill_accel=%s",
                  step, spd, cols, rows, brush_px,
                  skip_pencil_rearm, batch_colours,
                  skip_white, smart_underpaint_setting,
                  merge_tiny_setting, effective_dither.value,
                  planner_mode.value, auto_brush_mode.value, max_auto_brush,
                  grayscale_only,
                  len(active_palette_indices), max_speed_planner,
                  paint_fill_accel)

        if cols < 1 or rows < 1:
            self._set_status("Canvas too small for this detail level.", self.err)
            self._re_enable()
            return

        # ── Load & resize ──────────────────────────────────────────────────────
        try:
            img = Image.open(self._image_path).convert("RGB")
            _log.info("Opened image: %s  size=%s", self._image_path, img.size)
        except Exception as exc:
            _log.exception("Cannot open image: %s", exc)
            self._set_status(f"Cannot open image: {exc}", self.err)
            self._re_enable()
            return

        # ── Find Paint ─────────────────────────────────────────────────────────
        hwnd = _find_paint_hwnd()
        if not hwnd:
            self._set_status("MS Paint not found.", self.err)
            self._re_enable()
            return
        self._hwnd = hwnd
        _log_paint_rect(hwnd)

        img = img.resize((cols, rows), Image.Resampling.LANCZOS)
        index_result = _index_image_data(
            img,
            active_pal_rgb,
            effective_dither,
            planner_mode,
        )
        pixel_indices = index_result.pixel_indices
        fast_planner_used = index_result.fast_used
        _log.info("Dither stats: %s", index_result.stats)
        if active_palette_indices != list(range(len(self.palette))):
            index_map = bytes(
                active_palette_indices[i] if i < len(active_palette_indices) else 0
                for i in range(256)
            )
            pixel_indices = pixel_indices.translate(index_map)
        _log.info("Resized and palette-indexed to %dx%d  dither=%s  palette=%s  fast=%s",
                  cols, rows, effective_dither.value,
                  "grayscale" if grayscale_only or effective_dither == DitherPreset.CLEAN else "full",
                  fast_planner_used)

        if _stop_event.is_set():
            self._re_enable()
            return

        merge_tiny_runs = merge_tiny_setting and not use_dither
        white_idx = next(
            (i for i, colour in enumerate(self.palette) if colour["name"] == "White"),
            -1,
        )

        self._set_status("Starting draw...", self.warn)
        if not self._draw_ui_hidden:
            _nap(0.1)
        _focus_paint(hwnd, delay=focus_delay_s)

        # ── Arm pencil ─────────────────────────────────────────────────────────
        _log.info("Arming pencil @ %s", self.pencil_tool)
        _mouse_click(
            *self.pencil_tool,
            delay=0.01,
            pre_down=click_pre_down_s,
            pre_up=click_pre_up_s,
        )

        # ── Plan all rows ──────────────────────────────────────────────────────
        self._set_status("Planning strokes…", "#5bc4ff")
        _log.info("Planning %d rows…", rows)

        smart_underpaint = batch_colours and smart_underpaint_setting
        skip_indices = {white_idx} if skip_white and white_idx >= 0 else set()
        auto_brush_actions: list[BrushAction] = []
        auto_covered = np.zeros((rows, cols), dtype=bool)
        auto_stats = PlanStats(planner_mode=auto_brush_mode.value)
        if auto_brush_mode != AutoBrushMode.OFF:
            auto_t0 = time.monotonic()
            candidates = _auto_brush_candidate_sizes(
                max_auto_brush, step, auto_brush_mode
            )
            pixel_index_arr = np.frombuffer(
                pixel_indices, dtype=np.uint8
            ).reshape((rows, cols))
            auto_brush_actions, auto_covered, auto_stats = _plan_auto_brush_actions(
                pixel_index_arr,
                len(self.palette),
                skip_indices,
                candidates,
                step,
                auto_brush_mode,
            )
            _log.info(
                "Auto brush plan: actions=%d pixels=%d sizes=%s candidates=%s seconds=%.3f",
                auto_stats.auto_brush_actions,
                auto_stats.auto_brush_pixels,
                auto_stats.auto_brush_sizes,
                candidates,
                time.monotonic() - auto_t0,
            )
        residual_skip_indices = set(skip_indices)
        if auto_brush_actions:
            residual_skip_indices.add(-1)
        rle_rows: list[list[tuple[int, int, int]]] = []
        layered_strokes_by_colour: list[list[list[GridRun]]] = []
        skipped_white_runs = 0
        skipped_auto_runs = 0
        overpaint_saved_runs = 0
        max_overpaint_layers = 0
        # Exact 1px optimization can swap horizontal runs for cheaper
        # horizontal/vertical/diagonal coverage without changing target pixels.
        line_optimizer_possible = batch_colours and step == 1 and brush_px == 1
        line_optimizer_enabled = line_optimizer_possible
        line_optimizer_diagonal = use_dither
        line_optimizer_paths = use_dither
        line_optimizer_fast = (
            line_optimizer_possible
            and (
                planner_mode == PlannerMode.FAST
                or (planner_mode == PlannerMode.AUTO and (fast_planner_used or use_dither))
            )
        )
        fill_accel_enabled = paint_fill_accel and line_optimizer_possible
        line_opt_before = 0
        line_opt_after = 0
        fill_actions_used = 0
        fill_rows_replaced = 0
        raw_runs_buf: list[tuple[int, int, int]] = []
        segment_buf: list[PaletteRun] = []
        # PERF: Row memoryview slices avoid per-pixel row_offset arithmetic.
        pixel_rows = memoryview(pixel_indices)

        def _ensure_stroke_layer(layer_idx: int) -> None:
            while len(layered_strokes_by_colour) <= layer_idx:
                layered_strokes_by_colour.append([[] for _ in self.palette])

        def _add_layer_run(
                layer_idx: int,
                colour_idx: int,
                gy: int,
                gx_start: int,
                gx_end: int) -> None:
            _ensure_stroke_layer(layer_idx)
            layered_strokes_by_colour[layer_idx][colour_idx].append(
                (gy, gx_start, gx_end)
            )

        def _plan_segment(gy: int, segment: list[PaletteRun]) -> None:
            nonlocal overpaint_saved_runs, max_overpaint_layers
            if not segment:
                return
            layers = (
                _overpaint_segment_layers(segment)
                if smart_underpaint else [segment]
            )
            if smart_underpaint:
                original_count = len(segment)
                layered_count = _overpaint_cost(layers)
                overpaint_saved_runs += max(0, original_count - layered_count)
                max_overpaint_layers = max(max_overpaint_layers, len(layers))
            for layer_idx, layer in enumerate(layers):
                for colour_idx, gx_start, gx_end in layer:
                    _add_layer_run(layer_idx, colour_idx, gy, gx_start, gx_end)

        for gy in range(rows):
            row = pixel_rows[gy * cols:(gy + 1) * cols]
            covered_row = auto_covered[gy] if auto_brush_actions else None
            raw_runs_buf.clear()
            cur_idx = -1 if covered_row is not None and covered_row[0] else row[0]
            run_start = 0

            for gx in range(1, cols):
                idx = -1 if covered_row is not None and covered_row[gx] else row[gx]
                if idx != cur_idx:
                    raw_runs_buf.append((cur_idx, run_start, gx - 1))
                    cur_idx, run_start = idx, gx

            raw_runs_buf.append((cur_idx, run_start, cols - 1))
            raw_runs = (
                _merge_tiny_noise(raw_runs_buf, residual_skip_indices)
                if merge_tiny_runs else raw_runs_buf
            )

            if batch_colours:
                segment_buf.clear()
                for colour_idx, gx_start, gx_end in raw_runs:
                    if colour_idx in residual_skip_indices:
                        if colour_idx == -1:
                            skipped_auto_runs += 1
                        else:
                            skipped_white_runs += 1
                        _plan_segment(gy, segment_buf)
                        segment_buf.clear()
                    else:
                        segment_buf.append((colour_idx, gx_start, gx_end))
                _plan_segment(gy, segment_buf)
            else:
                runs: list[tuple[int, int, int]] = []
                for colour_idx, gx_start, gx_end in raw_runs:
                    if colour_idx in residual_skip_indices:
                        if colour_idx == -1:
                            skipped_auto_runs += 1
                        else:
                            skipped_white_runs += 1
                    else:
                        runs.append((colour_idx, gx_start, gx_end))
                rle_rows.append(runs)

        line_optimizer_enabled = (
            line_optimizer_possible
            and planner_mode != PlannerMode.EXACT
            and (use_dither or not smart_underpaint or overpaint_saved_runs < 1024)
        )
        if planner_mode == PlannerMode.EXACT:
            line_optimizer_enabled = line_optimizer_possible

        if batch_colours:
            def _make_line_plan(
                    strokes_by_colour: list[list[GridRun]],
                    optimize_lines: bool) -> list[tuple[int, list[GridAction]]]:
                nonlocal line_opt_before, line_opt_after
                nonlocal fill_actions_used, fill_rows_replaced
                plan: list[tuple[int, list[GridAction]]] = []
                for idx, runs in enumerate(strokes_by_colour):
                    if not runs:
                        continue

                    original_count = len(runs)
                    fill_actions: list[GridAction] = []
                    plan_runs = runs
                    if fill_accel_enabled:
                        plan_runs, fill_actions = _extract_rect_fill_actions(runs)
                        if fill_actions:
                            fill_actions_used += len(fill_actions)
                            fill_rows_replaced += original_count - len(plan_runs)

                    horizontal = _as_line_strokes(plan_runs)
                    strokes = horizontal
                    if optimize_lines:
                        if line_optimizer_fast:
                            optimized = _optimize_grid_strokes_fast(
                                plan_runs,
                                cols,
                                rows,
                            )
                        else:
                            optimized = _optimize_grid_strokes(
                                plan_runs,
                                cols,
                                rows,
                                diagonal=line_optimizer_diagonal,
                                paths=line_optimizer_paths,
                            )
                        if _action_event_cost(optimized) < _action_event_cost(horizontal):
                            strokes = optimized
                        line_opt_before += original_count
                        line_opt_after += sum(_planned_action_count(action) for action in strokes)

                    if fill_actions:
                        strokes = fill_actions + strokes
                    plan.append((idx, strokes))
                return plan

            draw_layers = []
            for layer_idx, strokes_by_colour in enumerate(layered_strokes_by_colour):
                layer_plan = _make_line_plan(
                    strokes_by_colour, line_optimizer_enabled
                )
                if not layer_plan:
                    continue
                layer_plan.sort(key=lambda item: len(item[1]), reverse=True)
                if smart_underpaint and max_overpaint_layers > 1:
                    layer_name = f"overpaint {layer_idx + 1}/{max_overpaint_layers}"
                else:
                    layer_name = "detail"
                draw_layers.append((layer_name, layer_plan))
            total_strokes = sum(
                _planned_action_count(action)
                for _, plan in draw_layers for _, runs in plan for action in runs
            )
            used_colours = len({
                idx for _, plan in draw_layers for idx, _ in plan
            })
        else:
            draw_layers = []
            total_strokes = sum(len(runs) for runs in rle_rows)
            used_colours = len({idx for runs in rle_rows for idx, _, _ in runs})

        auto_brush_layers: list[tuple[int, list[tuple[int, list[BrushAction]]]]] = []
        if auto_brush_actions:
            by_size_colour: dict[int, dict[int, list[BrushAction]]] = {}
            for action in auto_brush_actions:
                by_size_colour.setdefault(action.brush_px, {}).setdefault(
                    action.colour_idx, []
                ).append(action)
            for size in sorted(by_size_colour, reverse=True):
                colour_plan = sorted(
                    by_size_colour[size].items(),
                    key=lambda item: len(item[1]),
                    reverse=True,
                )
                auto_brush_layers.append((size, colour_plan))
            total_strokes += len(auto_brush_actions)
            used_colours = len({
                *({idx for _, plan in draw_layers for idx, _ in plan} if batch_colours else set()),
                *({idx for _, plan in auto_brush_layers for idx, _ in plan}),
                *({idx for runs in rle_rows for idx, _, _ in runs} if not batch_colours else set()),
            })

        _log.info(
            "Quantisation done. Total strokes=%d colours=%d skipped_white=%d skipped_auto=%d "
            "overpaint_saved=%d layers=%d mode=%s line_opt=%s saved=%d "
            "fill_actions=%d fill_rows=%d auto_actions=%d auto_pixels=%d",
            total_strokes, used_colours, skipped_white_runs, skipped_auto_runs,
            overpaint_saved_runs,
            max(1, max_overpaint_layers),
            "smart-batched" if smart_underpaint else (
                "batched" if batch_colours else "row"
            ),
            (
                f"{line_opt_before}->{line_opt_after}"
                if line_opt_before else "off"
            ),
            max(0, line_opt_before - line_opt_after),
            fill_actions_used,
            fill_rows_replaced,
            len(auto_brush_actions),
            auto_stats.auto_brush_pixels,
        )

        if total_strokes < 1:
            self._set_progress(100)
            self._set_status("Nothing to draw after white-skip.", self.acc)
            self._re_enable()
            return

        drawn_strokes = 0
        last_colour_idx: int | None = None
        last_status_t = 0.0
        batchable = (
            max(min_stroke_s, spd) <= 0
            and drag_steps == 1
            and move_settle_ms <= 0
            and press_settle_ms <= 0
            and post_step_ms <= 0
            and release_settle_ms <= 0
        )
        input_batch = _InputBatch() if batchable else None
        batch_stroke_limit = 1024
        scheduled_strokes = 0
        pause_poll_interval = 64
        status_poll_interval = 32
        _log.info("Stroke event batching: %s", batchable)

        def _flush_batch() -> None:
            nonlocal drawn_strokes
            if input_batch is None:
                return
            drawn_strokes += input_batch.flush()

        def _draw_planned_stroke(
                x_start: int, y_start: int, x_end: int, y_end: int) -> None:
            nonlocal drawn_strokes, scheduled_strokes
            if input_batch is not None:
                drawn_strokes += input_batch._append_stroke_nonnull(
                    x_start, y_start, x_end, y_end
                )
                if (input_batch.strokes >= batch_stroke_limit
                        or input_batch.count >= _SEND_INPUT_CHUNK - 3):
                    _flush_batch()
            else:
                _stroke_line(x_start, y_start, x_end, y_end, spd)
                drawn_strokes += 1
            scheduled_strokes += 1

        def _draw_planned_path(points: list[tuple[int, int]]) -> None:
            nonlocal drawn_strokes, scheduled_strokes
            if input_batch is not None:
                drawn_strokes += input_batch.append_path(points)
                if (input_batch.strokes >= batch_stroke_limit
                        or input_batch.count >= _SEND_INPUT_CHUNK - 3):
                    _flush_batch()
            else:
                _stroke_path(points, spd)
                drawn_strokes += 1
            scheduled_strokes += 1

        def _pause_if_needed(where: str) -> bool:
            nonlocal last_colour_idx
            if not _pause_event.is_set():
                return _stop_event.is_set()
            _flush_batch()
            self._set_status("Paused - press ESC to resume", self.warn)
            _log.info("Paused at %s", where)
            while _pause_event.is_set() and not _stop_event.is_set():
                _nap(0.03)
            if _stop_event.is_set():
                return True
            _log.info("Resumed at %s - re-focusing Paint", where)
            _focus_paint(hwnd, delay=resume_focus_delay_s)
            _mouse_click(
                *self.pencil_tool,
                delay=0.05,
                pre_down=click_pre_down_s,
                pre_up=click_pre_up_s,
            )
            last_colour_idx = None
            return False

        def _stop_now(where: str) -> None:
            _log.info(
                "Stop event: %s  strokes %d/%d",
                where, drawn_strokes, total_strokes,
            )
            self._set_status("Stopped.", self.err)
            self._re_enable()

        canvas_x = self.canvas_x
        canvas_y = self.canvas_y
        canvas_x_max = self.canvas_x + self.canvas_w - 1
        canvas_y_max = self.canvas_y + self.canvas_h - 1
        half_step = step // 2
        row_y_by_gy = [
            min(canvas_y_max, canvas_y + gy * step + half_step)
            for gy in range(rows)
        ]
        cell_x_by_gx = [
            min(canvas_x_max, canvas_x + gx * step + half_step)
            for gx in range(cols)
        ]
        run_x_start_by_gx = [
            canvas_x + gx * step
            for gx in range(cols)
        ]
        run_x_end_by_gx = [
            canvas_x_max if gx == cols - 1 else canvas_x + (gx + 1) * step - 1
            for gx in range(cols)
        ]

        def _stroke_coords(gx_start: int, gx_end: int) -> tuple[int, int]:
            x_start = run_x_start_by_gx[gx_start]
            x_end = run_x_end_by_gx[gx_end]
            return x_start, max(x_start, x_end)

        def _row_y(gy: int) -> int:
            return row_y_by_gy[gy]

        def _cell_center(gx: int, gy: int) -> tuple[int, int]:
            return cell_x_by_gx[gx], row_y_by_gy[gy]

        def _stroke_line_coords(stroke: GridLine) -> tuple[int, int, int, int]:
            gy1, gx1, gy2, gx2 = stroke
            if gy1 == gy2:
                x_start, x_end = _stroke_coords(gx1, gx2)
                y_screen = _row_y(gy1)
                return x_start, y_screen, x_end, y_screen

            x_start, y_start = _cell_center(gx1, gy1)
            x_end, y_end = _cell_center(gx2, gy2)
            return x_start, y_start, x_end, y_end

        def _path_coords(path: GridPath) -> list[tuple[int, int]]:
            return [_cell_center(gx, gy) for gy, gx in path]

        def _draw_fill_action(action: GridFill) -> None:
            nonlocal drawn_strokes, scheduled_strokes
            _, seed_gy, seed_gx, boundary = action
            boundary_points = _path_coords(boundary)
            if input_batch is not None:
                drawn_strokes += input_batch.append_path(boundary_points)
                _flush_batch()
            else:
                _stroke_path(boundary_points, spd)
                drawn_strokes += 1
            scheduled_strokes += 1

            if fill_tool_pos is None:
                return
            seed_x, seed_y = _cell_center(seed_gx, seed_gy)
            _mouse_click(
                *fill_tool_pos,
                delay=pal_delay_s,
                pre_down=click_pre_down_s,
                pre_up=click_pre_up_s,
            )
            _mouse_click(
                seed_x,
                seed_y,
                delay=pal_delay_s,
                pre_down=click_pre_down_s,
                pre_up=click_pre_up_s,
            )
            _mouse_click(
                *self.pencil_tool,
                delay=pencil_rearm_s,
                pre_down=click_pre_down_s,
                pre_up=click_pre_up_s,
            )
            drawn_strokes += 1
            scheduled_strokes += 1

        # ── Main draw loop ─────────────────────────────────────────────────────
        _log.info("=== MAIN DRAW LOOP (%s) ===",
                  "SMART BATCHED" if smart_underpaint
                  else ("COLOUR BATCHED" if batch_colours else "ROW ORDER"))

        if batch_colours:
            layer_count = len(draw_layers)
            for layer_no, (layer_name, draw_plan) in enumerate(draw_layers, 1):
                layer_colours = len(draw_plan)
                for colour_no, (colour_idx, strokes) in enumerate(draw_plan, 1):
                    colour = self.palette[colour_idx]
                    where = (
                        f"{layer_name} layer {layer_no}/{layer_count} "
                        f"colour {colour_no}/{layer_colours}"
                    )

                    if _pause_if_needed(where):
                        _stop_now(where)
                        return

                    for stroke in strokes:
                        if colour_idx != last_colour_idx:
                            _flush_batch()
                            _log.debug("%s -> %s (%d strokes)",
                                       where, colour["name"], len(strokes))
                            self._pick_colour(
                                colour,
                                hwnd,
                                pal_d=pal_delay_s,
                                skip_pencil_rearm=skip_pencil_rearm,
                                rearm_d=pencil_rearm_s,
                                click_pre_down=click_pre_down_s,
                                click_pre_up=click_pre_up_s,
                                rearm_focus_delay=rearm_focus_delay_s,
                            )
                            last_colour_idx = colour_idx

                        if _is_grid_fill(stroke):
                            _draw_fill_action(stroke)
                        elif _is_grid_path(stroke):
                            _draw_planned_path(_path_coords(stroke))
                        else:
                            x_start, y_start, x_end, y_end = _stroke_line_coords(stroke)
                            _draw_planned_stroke(x_start, y_start, x_end, y_end)

                        if scheduled_strokes % pause_poll_interval == 0:
                            if _pause_if_needed(
                                    f"stroke {scheduled_strokes}/{total_strokes}"):
                                _stop_now(
                                    f"stroke {scheduled_strokes}/{total_strokes}")
                                return

                        if scheduled_strokes > 0 and scheduled_strokes % focus_every == 0:
                            _flush_batch()
                            if not _paint_has_focus(hwnd):
                                _log.info("Stroke %d: focus lost - re-focusing",
                                          drawn_strokes)
                                _focus_paint(hwnd, delay=0.08)
                                _mouse_click(
                                    *self.pencil_tool,
                                    delay=0.05,
                                    pre_down=click_pre_down_s,
                                    pre_up=click_pre_up_s,
                                )
                                last_colour_idx = None

                        if (not self._draw_ui_hidden
                                and scheduled_strokes % status_poll_interval == 0):
                            now = time.monotonic()
                            if now - last_status_t >= 0.2:
                                _flush_batch()
                                pct = 100.0 * drawn_strokes / total_strokes
                                self._set_status(
                                    f"Drawing...  {layer_name} "
                                    f"{colour_no}/{layer_colours} {colour['name']}  "
                                    f"strokes {drawn_strokes:,}/{total_strokes:,}  "
                                    f"({pct:.0f}%)",
                                    "#5bc4ff",
                                )
                                self._set_progress(pct)
                                last_status_t = now
        else:
            for gy, runs in enumerate(rle_rows):
                if _pause_if_needed(f"row {gy}/{rows}"):
                    _stop_now(f"row {gy}/{rows}")
                    return

                if gy > 0 and gy % row_focus_interval == 0:
                    if not _paint_has_focus(hwnd):
                        _log.info("Row %d: focus lost - re-focusing", gy)
                        _focus_paint(hwnd, delay=0.08)
                        _mouse_click(
                            *self.pencil_tool,
                            delay=0.05,
                            pre_down=click_pre_down_s,
                            pre_up=click_pre_up_s,
                        )
                        last_colour_idx = None

                y_screen = _row_y(gy)

                for colour_idx, gx_start, gx_end in runs:
                    colour = self.palette[colour_idx]
                    if colour_idx != last_colour_idx:
                        _flush_batch()
                        _log.debug("Row %d: colour -> %s", gy, colour["name"])
                        self._pick_colour(
                            colour,
                            hwnd,
                            pal_d=pal_delay_s,
                            skip_pencil_rearm=skip_pencil_rearm,
                            rearm_d=pencil_rearm_s,
                            click_pre_down=click_pre_down_s,
                            click_pre_up=click_pre_up_s,
                            rearm_focus_delay=rearm_focus_delay_s,
                        )
                        last_colour_idx = colour_idx

                    x_start, x_end = _stroke_coords(gx_start, gx_end)
                    _draw_planned_stroke(x_start, y_screen, x_end, y_screen)

                    if scheduled_strokes % pause_poll_interval == 0:
                        if _pause_if_needed(
                                f"stroke {scheduled_strokes}/{total_strokes}"):
                            _stop_now(
                                f"stroke {scheduled_strokes}/{total_strokes}")
                            return

                if (not self._draw_ui_hidden
                        and scheduled_strokes > 0
                        and scheduled_strokes % status_poll_interval == 0):
                    now = time.monotonic()
                    if now - last_status_t >= 0.2:
                        _flush_batch()
                        pct = 100.0 * drawn_strokes / total_strokes
                        self._set_status(
                            f"Drawing...  row {gy+1}/{rows}  "
                            f"strokes {drawn_strokes:,}/{total_strokes:,}  ({pct:.0f}%)",
                            "#5bc4ff",
                        )
                        self._set_progress(pct)
                        last_status_t = now

        _flush_batch()
        _log.info("=== DRAW COMPLETE: %d strokes ===", drawn_strokes)
        self._set_progress(100)
        self._set_status("✅  Done!", self.acc)
        self._re_enable()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pyautogui
    pyautogui.FAILSAFE = True
    _log.info("pyautogui FAILSAFE enabled")

    root = tk.Tk()
    root.geometry("520x785")
    app = DrawBotApp(root)
    root.mainloop()
    _log.info("GUI closed — exiting")
