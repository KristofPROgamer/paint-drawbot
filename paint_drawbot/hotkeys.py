from __future__ import annotations

import threading

import keyboard

from .logging_config import _log

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
    try:
        keyboard.add_hotkey("esc", _toggle_pause)
        keyboard.add_hotkey("ctrl+esc", _request_stop)
    except Exception as exc:
        _log.warning("Could not register global hotkeys: %s", exc)
        return
    _hotkeys_registered = True
