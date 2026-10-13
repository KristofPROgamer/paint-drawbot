from __future__ import annotations

import win32con
import win32gui

from .logging_config import _log
from .mouse_input import _nap
from .timing import _cfg

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
