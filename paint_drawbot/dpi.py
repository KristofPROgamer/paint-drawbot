from __future__ import annotations

import ctypes

_dpi_awareness_set = False


def set_dpi_awareness() -> None:
    """Enable per-monitor DPI awareness before Tk/Win32 UI work starts."""
    global _dpi_awareness_set
    if _dpi_awareness_set:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    _dpi_awareness_set = True
