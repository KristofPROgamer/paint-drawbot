from __future__ import annotations

import tkinter as tk

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


class _DelayConfigProxy:
    def __init__(self) -> None:
        self._config: DelayConfig | None = None

    def set(self, config: DelayConfig) -> None:
        self._config = config

    def get(self) -> DelayConfig:
        if self._config is None:
            raise RuntimeError("DelayConfig has not been initialised")
        return self._config

    def __getattr__(self, name: str):
        return getattr(self.get(), name)


_cfg = _DelayConfigProxy()


def set_delay_config(config: DelayConfig) -> None:
    _cfg.set(config)


def get_delay_config() -> DelayConfig:
    return _cfg.get()
