from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .logging_config import _log
from .timing import DelayConfig, _cfg

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
        self._trace_handles: list[tuple[tk.Variable, str]] = []
        self.title("⚙  Timing Debug Panel  —  v5.0")
        self.configure(bg=self.BG)
        self.resizable(False, False)
        self.attributes("-topmost", True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
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

        trace_id = var.trace_add("write", _upd)
        self._trace_handles.append((var, trace_id))
        ttk.Scale(row, from_=lo, to=hi, orient="horizontal",
                  variable=var, length=190, command=_upd).pack(side="left", padx=6)

    def destroy(self) -> None:
        for var, trace_id in self._trace_handles:
            try:
                var.trace_remove("write", trace_id)
            except tk.TclError:
                pass
        self._trace_handles.clear()
        super().destroy()

    # ── Presets ────────────────────────────────────────────────────────────────

    def _apply_turbo(self) -> None:
        _cfg.apply(DelayConfig.TURBO)
        _cfg.skip_pencil_rearm.set(True)
        _log.info("TURBO preset applied")

    def _apply_defaults(self) -> None:
        _cfg.apply(DelayConfig.DEFAULTS)
        _cfg.skip_pencil_rearm.set(True)
        _log.info("Default delays restored")
