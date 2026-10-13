from __future__ import annotations

import ctypes
import time

import win32api
import win32con

from .logging_config import _log
from .timing import _cfg

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
_MOUSEEVENTF_VIRTUALDESK = getattr(win32con, "MOUSEEVENTF_VIRTUALDESK", 0x4000)
# PERF: Precompute the four hot-path SendInput flag combinations once.
_MOUSE_MOVE_FLAGS = (
    _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK
)
_MOUSE_MOVE_NC_FLAGS = _MOUSE_MOVE_FLAGS | _MOUSEEVENTF_MOVE_NOCOALESCE
_MOUSE_MOVE_LEFTDOWN_FLAGS = _MOUSE_MOVE_FLAGS | _MOUSEEVENTF_LEFTDOWN
_MOUSE_MOVE_LEFTDOWN_NC_FLAGS = (
    _MOUSE_MOVE_LEFTDOWN_FLAGS | _MOUSEEVENTF_MOVE_NOCOALESCE
)
_FAILSAFE_MARGIN_PX = 0


class MouseFailsafeError(RuntimeError):
    """Raised when the cursor is moved to a screen corner to abort input."""


def _request_stop_from_failsafe() -> None:
    try:
        from .hotkeys import _pause_event, _stop_event
    except Exception:
        return
    _stop_event.set()
    _pause_event.clear()


def _raise_if_failsafe_corner() -> None:
    px, py = win32api.GetCursorPos()
    corners = (
        (_norm_ox, _norm_oy),
        (_norm_xmax, _norm_oy),
        (_norm_ox, _norm_ymax),
        (_norm_xmax, _norm_ymax),
    )
    for cx, cy in corners:
        if (
                abs(px - cx) <= _FAILSAFE_MARGIN_PX
                and abs(py - cy) <= _FAILSAFE_MARGIN_PX):
            msg = "Mouse moved to a screen corner; aborting input."
            _log.warning(msg)
            _request_stop_from_failsafe()
            raise MouseFailsafeError(msg)

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
        _raise_if_failsafe_corner()
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
        _raise_if_failsafe_corner()
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
