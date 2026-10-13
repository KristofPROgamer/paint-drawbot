from __future__ import annotations

import ctypes
import os
import time
import tkinter as tk
from tkinter import ttk

from .debug_panel import DebugPanel
from .logging_config import _LOG_FILE, _log
from .models import AutoBrushMode, BRUSH_SIZE_STOPS, DitherPreset, PlannerMode


class UiMixin:
    def _build_ui(self) -> None:
        # ── Header ──────────────────────────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=self.bg)
        hdr.pack(fill="x", padx=20, pady=(16, 4))
        tk.Label(hdr, text="PAINT  DRAWBOT  v5.0",
                 font=self.hfont, bg=self.bg, fg=self.acc).pack(side="left")
        self._cal_dot = tk.Label(hdr, text="●", font=("Consolas", 14),
                                  bg=self.bg, fg="#333")
        self._cal_dot.pack(side="right", padx=(4, 0))
        tk.Label(hdr, text="cal", font=("Consolas", 8),
                 bg=self.bg, fg=self.muted).pack(side="right")

        # ── Main card ───────────────────────────────────────────────────────────
        card = tk.Frame(self.root, bg=self.card, padx=18, pady=14)
        card.pack(fill="x", padx=20, pady=(0, 8))

        self._build_actions_section(card)
        self._sep(card)
        self._build_tuning_section(card)
        self._sep(card)
        self._build_options_section(card)

        # ── Estimate label ───────────────────────────────────────────────────────
        self._est_lbl = tk.Label(card, text="Calibrate first to see estimate.",
                                  font=("Consolas", 9), bg=self.card,
                                  fg=self.muted, wraplength=460, justify="left")
        self._est_lbl.pack(anchor="w", pady=(4, 6))

        self._sep(card)

        # ── Start / Stop ────────────────────────────────────────────────────────
        btn_row = tk.Frame(card, bg=self.card)
        btn_row.pack(fill="x", pady=(4, 0))
        self._start_btn = tk.Button(
            btn_row, text="▶  START", command=self._start_drawing,
            font=("Consolas", 11, "bold"), bg=self.acc, fg="#000",
            activebackground="#00c080", activeforeground="#000",
            relief="flat", cursor="hand2", pady=8, state="disabled",
        )
        self._start_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._stop_btn = tk.Button(
            btn_row, text="⏹  STOP", command=self._stop_action,
            font=("Consolas", 11, "bold"), bg=self.err, fg="#000",
            activebackground="#cc0000", activeforeground="#000",
            relief="flat", cursor="hand2", pady=8, state="disabled",
        )
        self._stop_btn.pack(side="right", fill="x", expand=True, padx=(4, 0))

        # ── Status bar ──────────────────────────────────────────────────────────
        sbar = tk.Frame(self.root, bg=self.bg)
        sbar.pack(fill="x", padx=20, pady=(4, 2))
        self._status_lbl = tk.Label(
            sbar, text="Step 1: Click CALIBRATE (or load saved cal).",
            font=("Consolas", 9), bg=self.bg, fg="#5bc4ff",
            anchor="w", wraplength=480, justify="left",
        )
        self._status_lbl.pack(fill="x")

        self._pbar_cv = tk.Canvas(self.root, height=6, bg="#1c1c1e",
                                   highlightthickness=0)
        self._pbar_cv.pack(fill="x", padx=20, pady=(2, 4))
        self._pbar_rect = self._pbar_cv.create_rectangle(0, 0, 0, 6,
                                                          fill=self.acc, width=0)

        tk.Label(
            self.root,
            text="ESC → pause/resume  |  mouse corner → abort  "
                 "|  [ DEBUG TIMING ] → live tune all delays",
            font=("Consolas", 8), bg=self.bg, fg="#333",
        ).pack(pady=(0, 12))

    # ── Section builders ─────────────────────────────────────────────────────────

    def _build_actions_section(self, parent: tk.Frame) -> None:
        """Action buttons in a uniform 2-column grid."""
        self._sec_label(parent, "ACTIONS")

        grid = tk.Frame(parent, bg=self.card)
        grid.pack(fill="x", pady=(2, 4))
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        # (label, command, foreground, active-background)
        _buttons = [
            ("[ CALIBRATE ]",    self._start_calibration,        self.warn,   "#7a4a00"),
            ("[ SELECT IMAGE ]", self._select_image,              self.acc,    "#005a3a"),
            ("[ TEST STROKE ]",  self._test_stroke,               "#5bc4ff",   "#005880"),
            ("[ DEBUG TIMING ]", self._open_debug,                "#c084fc",   "#5b219e"),
            ("[ OPEN LOG ]",     self._open_log,                  self.muted,  "#2c2c2e"),
            ("[ CAL TOOLS ]",    self._start_tools_calibration,   "#7dd3fc",   "#164e63"),
            ("[ PREVIEW ]",      self._preview_conversion,        "#34d399",   "#065f46"),
            ("[ TEST BRUSHES ]", self._test_brush_ladder,         "#f59e0b",   "#78350f"),
        ]

        btn_refs: list[tk.Button] = []
        for i, (text, cmd, fg, abg) in enumerate(_buttons):
            row, col = divmod(i, 2)
            b = tk.Button(
                grid, text=text, command=cmd,
                font=self.font, bg="#202022", fg=fg,
                activebackground=abg, activeforeground=fg,
                relief="flat", cursor="hand2", padx=8, pady=5,
            )
            padx = (0, 2) if col == 0 else (2, 0)
            b.grid(row=row, column=col, sticky="ew", padx=padx, pady=2)
            btn_refs.append(b)

        (self._cal_btn, self._sel_btn,
         self._test_btn, self._debug_btn,
         self._log_btn, self._tools_btn,
         self._preview_btn, self._brush_test_btn) = btn_refs

        self._img_lbl = tk.Label(parent, text="  no image selected",
                                  font=("Consolas", 9, "italic"),
                                  bg=self.card, fg=self.muted)
        self._img_lbl.pack(anchor="w", pady=(0, 4))

    def _build_tuning_section(self, parent: tk.Frame) -> None:
        """Detail level, stroke speed, and brush-size controls."""
        self._sec_label(parent, "TUNING")

        # Detail level
        tk.Label(parent, text="DETAIL LEVEL",
                 font=("Consolas", 9), bg=self.card, fg=self.muted).pack(anchor="w")
        sl1 = tk.Frame(parent, bg=self.card)
        sl1.pack(fill="x", pady=(2, 0))
        tk.Label(sl1, text="1 (fast)", font=("Consolas", 9),
                 bg=self.card, fg=self.muted).pack(side="left")
        ttk.Scale(sl1, from_=self.DETAIL_MIN, to=self.DETAIL_MAX,
                  orient="horizontal", variable=self._detail, length=220,
                  command=lambda _: self._refresh_estimate()).pack(side="left", padx=6)
        tk.Label(sl1, text=f"{self.DETAIL_MAX} (1px ultra)",
                 font=("Consolas", 9), bg=self.card, fg=self.muted).pack(side="left")
        self._detail_lbl = tk.Label(parent, text="", font=("Consolas", 9),
                                     bg=self.card, fg=self.muted)
        self._detail_lbl.pack(anchor="w", pady=(2, 6))

        # Stroke speed
        tk.Label(parent,
                 text="STROKE SPEED  (ms per stroke — lower=faster, may miss)",
                 font=("Consolas", 9), bg=self.card, fg=self.muted).pack(anchor="w")
        sl2 = tk.Frame(parent, bg=self.card)
        sl2.pack(fill="x", pady=(2, 0))
        tk.Label(sl2, text="0 ms", font=("Consolas", 9),
                 bg=self.card, fg=self.muted).pack(side="left")
        ttk.Scale(sl2, from_=0, to=80, orient="horizontal",
                  variable=self._speed_ms, length=220,
                  command=lambda _: self._refresh_estimate()).pack(side="left", padx=6)
        tk.Label(sl2, text="80 ms", font=("Consolas", 9),
                 bg=self.card, fg=self.muted).pack(side="left")
        self._speed_lbl = tk.Label(parent, text="", font=("Consolas", 9),
                                    bg=self.card, fg=self.muted)
        self._speed_lbl.pack(anchor="w", pady=(2, 6))

        # Brush size
        brow = tk.Frame(parent, bg=self.card)
        brow.pack(fill="x", pady=(0, 2))
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
            parent, text="", font=("Consolas", 8),
            bg=self.card, fg=self.warn, anchor="w", justify="left",
        )
        self._sanity_lbl.pack(fill="x", pady=(0, 2))

    def _build_options_section(self, parent: tk.Frame) -> None:
        """Checkboxes in a 2-column grid, then dropdown rows."""
        self._sec_label(parent, "OPTIONS")

        # Checkbox grid
        cgrid = tk.Frame(parent, bg=self.card)
        cgrid.pack(fill="x", pady=(2, 4))
        cgrid.columnconfigure(0, weight=1)
        cgrid.columnconfigure(1, weight=1)

        # (row, col, label, variable, calls _refresh_estimate)
        _checks = [
            (0, 0, "Batch colours",           self._batch_colours,         True),
            (0, 1, "Skip white (blank canvas)", self._skip_white,           True),
            (1, 0, "Smart underpaint",         self._smart_underpaint,      True),
            (1, 1, "Merge tiny noise",         self._merge_tiny_runs,       True),
            (2, 0, "Grayscale only",           self._grayscale_only,        True),
            (2, 1, "Paint fill accelerator",   self._paint_fill_accel,      True),
            (3, 0, "Hide UI while drawing",    self._hide_ui_while_drawing, False),
        ]
        for row, col, label, var, refresh in _checks:
            cb = tk.Checkbutton(
                cgrid, text=label, variable=var,
                bg=self.card, fg=self.warn, selectcolor="#1a1a1c",
                activebackground=self.card, font=("Consolas", 9),
                command=self._refresh_estimate if refresh else None,
            )
            padx = (0, 6) if col == 0 else (6, 0)
            cb.grid(row=row, column=col, sticky="w", padx=padx, pady=1)

        # Dropdown rows
        row1 = tk.Frame(parent, bg=self.card)
        row1.pack(fill="x", pady=(2, 2))
        self._lbl_combo(row1, "Dither:",
                        self._dither_style, [e.value for e in DitherPreset], 14)
        tk.Frame(row1, bg=self.card, width=16).pack(side="left")  # spacer
        self._lbl_combo(row1, "Planner:",
                        self._planner_mode, [e.value for e in PlannerMode], 8)

        row2 = tk.Frame(parent, bg=self.card)
        row2.pack(fill="x", pady=(2, 4))
        self._lbl_combo(row2, "Auto brush:",
                        self._auto_brush_mode, [e.value for e in AutoBrushMode], 12)
        tk.Frame(row2, bg=self.card, width=16).pack(side="left")  # spacer
        self._lbl_combo(row2, "Max auto brush:",
                        self._max_auto_brush, BRUSH_SIZE_STOPS, 6)
        tk.Label(row2, text="(uses calibrated Paint size slider)",
                 font=("Consolas", 8), bg=self.card, fg=self.muted).pack(
                     side="left", padx=(8, 0))

    # ── UI helpers ───────────────────────────────────────────────────────────────

    def _sec_label(self, parent: tk.Frame, text: str) -> None:
        """Small uppercase section heading."""
        tk.Label(parent, text=text, font=("Consolas", 8, "bold"),
                 bg=self.card, fg=self.muted).pack(anchor="w", pady=(2, 0))

    def _lbl_combo(self, parent: tk.Frame, label: str,
                   var, values: list, width: int) -> None:
        """Inline label + read-only combobox pair."""
        tk.Label(parent, text=label, font=("Consolas", 9),
                 bg=self.card, fg=self.muted).pack(side="left", padx=(0, 4))
        ttk.Combobox(parent, textvariable=var, values=values,
                     width=width, state="readonly").pack(side="left")

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

    # ── Runtime helpers ──────────────────────────────────────────────────────────

    def _set_status(self, text: str, colour: str = "#5bc4ff") -> None:
        flat = text.replace("\n", " ")
        transient = flat.startswith(("Starting draw", "Quantising", "Planning", "Drawing"))
        if transient:
            _log.debug("STATUS: %s", flat)
        else:
            _log.info("STATUS: %s", flat)
        if self._draw_ui_hidden:
            self._update_draw_hud(status=flat, force=not transient)
            if transient:
                return
        self.root.after(0, lambda: self._status_lbl.config(text=text, fg=colour))

    def _set_progress(self, pct: float) -> None:
        if self._draw_ui_hidden:
            self._update_draw_hud(pct=pct, force=pct >= 100)
            if pct < 100:
                return

        def _up() -> None:
            w = self._pbar_cv.winfo_width()
            self._pbar_cv.coords(self._pbar_rect, 0, 0, int(w * pct / 100), 6)

        self.root.after(0, _up)

    def _set_cal_dot(self, ok: bool) -> None:
        col = self.acc if ok else "#333"
        self.root.after(0, lambda: self._cal_dot.config(fg=col))

    def _show_draw_hud(self) -> None:
        def _show() -> None:
            if (
                    getattr(self, "_draw_hud", None) is None
                    or not self._draw_hud.winfo_exists()):
                self._create_draw_hud()
            self._draw_hud.deiconify()
            self._draw_hud.lift()
            self._update_draw_hud(
                pct=0,
                status="Preparing draw...",
                detail="Planning exact stroke count",
                fun="Tiny HUD is click-through and staying out of the way.",
                force=True,
            )

        self.root.after(0, _show)

    def _create_draw_hud(self) -> None:
        width = 316
        height = 102
        hud = tk.Toplevel(self.root)
        self._draw_hud = hud
        hud.title("DrawBot HUD")
        hud.overrideredirect(True)
        hud.resizable(False, False)
        hud.configure(bg="#101114")
        hud.attributes("-topmost", True)
        try:
            hud.attributes("-alpha", 0.88)
        except tk.TclError:
            pass

        body = tk.Frame(hud, bg="#101114", padx=12, pady=9)
        body.pack(fill="both", expand=True)
        top = tk.Frame(body, bg="#101114")
        top.pack(fill="x")
        self._draw_hud_status = tk.Label(
            top, text="Preparing draw...", font=("Consolas", 9, "bold"),
            bg="#101114", fg="#e9fff6", anchor="w",
        )
        self._draw_hud_status.pack(side="left", fill="x", expand=True)
        self._draw_hud_pct = tk.Label(
            top, text="0.00%", font=("Consolas", 10, "bold"),
            bg="#101114", fg=self.acc, anchor="e", width=8,
        )
        self._draw_hud_pct.pack(side="right")

        self._draw_hud_canvas = tk.Canvas(
            body, height=8, bg="#27272a", highlightthickness=0,
        )
        self._draw_hud_canvas.pack(fill="x", pady=(7, 5))
        self._draw_hud_bar = self._draw_hud_canvas.create_rectangle(
            0, 0, 0, 8, fill=self.acc, width=0,
        )

        self._draw_hud_detail = tk.Label(
            body, text="Planning exact stroke count",
            font=("Consolas", 8), bg="#101114", fg="#a1a1aa",
            anchor="w",
        )
        self._draw_hud_detail.pack(fill="x")
        self._draw_hud_fun = tk.Label(
            body, text="Tiny HUD is click-through and staying out of the way.",
            font=("Consolas", 8, "italic"), bg="#101114", fg="#7dd3fc",
            anchor="w",
        )
        self._draw_hud_fun.pack(fill="x", pady=(2, 0))

        self._place_draw_hud(width, height)
        hud.update_idletasks()
        self._make_hud_clickthrough(hud)

    def _place_draw_hud(self, width: int, height: int) -> None:
        margin = 14
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        if self.canvas_w > 0 and self.canvas_h > 0:
            x = self.canvas_x + self.canvas_w - width - margin
            y = self.canvas_y - height - margin
            if y < margin:
                y = margin
        else:
            x = screen_w - width - margin
            y = margin
        x = min(max(margin, x), max(margin, screen_w - width - margin))
        y = min(max(margin, y), max(margin, screen_h - height - margin))
        self._draw_hud.geometry(f"{width}x{height}+{x}+{y}")

    def _make_hud_clickthrough(self, hud: tk.Toplevel) -> None:
        try:
            hwnd = hud.winfo_id()
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, -20)
            style |= 0x00080000  # WS_EX_LAYERED
            style |= 0x00000020  # WS_EX_TRANSPARENT
            style |= 0x00000080  # WS_EX_TOOLWINDOW
            style |= 0x08000000  # WS_EX_NOACTIVATE
            user32.SetWindowLongW(hwnd, -20, style)
        except Exception as exc:
            _log.debug("Could not make HUD click-through: %s", exc)

    def _update_draw_hud(
            self,
            pct: float | None = None,
            status: str | None = None,
            detail: str | None = None,
            fun: str | None = None,
            force: bool = False) -> None:
        now = time.monotonic()
        if not force and now < getattr(self, "_draw_hud_next_update", 0.0):
            return
        self._draw_hud_next_update = now + 0.15

        def _up() -> None:
            if (
                    getattr(self, "_draw_hud", None) is None
                    or not self._draw_hud.winfo_exists()):
                if not self._draw_ui_hidden:
                    return
                self._create_draw_hud()
            if pct is not None:
                bounded = max(0.0, min(100.0, pct))
                w = self._draw_hud_canvas.winfo_width()
                self._draw_hud_canvas.coords(
                    self._draw_hud_bar, 0, 0, int(w * bounded / 100), 8
                )
                self._draw_hud_pct.config(text=f"{bounded:5.2f}%")
            if status is not None:
                self._draw_hud_status.config(text=status[:42])
            if detail is not None:
                self._draw_hud_detail.config(text=detail[:54])
            if fun is not None:
                self._draw_hud_fun.config(text=fun[:58])

        self.root.after(0, _up)

    def _update_draw_hud_stats(
            self,
            done: int,
            total: int,
            started_at: float,
            phase: str,
            extra: str = "",
            force: bool = False) -> None:
        total = max(1, total)
        done = max(0, min(done, total))
        elapsed = max(0.001, time.monotonic() - started_at)
        pct = 100.0 * done / total
        rate_per_min = done * 60.0 / elapsed if done else 0.0
        remaining = (total - done) / (done / elapsed) if done else 0.0
        eta = self._hud_duration(remaining) if done else "--"
        detail = (
            f"{done:,}/{total:,} actions  eta {eta}  "
            f"{rate_per_min:,.0f}/min"
        )
        if extra:
            detail = f"{detail}  {extra}"
        fun = self._hud_fun_line(pct, done, total)
        self._update_draw_hud(
            pct=pct,
            status=phase,
            detail=detail,
            fun=fun,
            force=force,
        )

    @staticmethod
    def _hud_duration(seconds: float) -> str:
        seconds = max(0.0, seconds)
        if seconds < 60:
            return f"{seconds:04.1f}s"
        if seconds < 3600:
            minutes = int(seconds // 60)
            sec = int(seconds % 60)
            return f"{minutes:02d}:{sec:02d}"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h{minutes:02d}m"

    @staticmethod
    def _hud_fun_line(pct: float, done: int, total: int) -> str:
        if done <= 0:
            return "Preflight complete. Waiting for the first confident stroke."
        if pct >= 99.9:
            return "Final polish pass. The finish line has excellent manners."
        lines = (
            "Palette hops counted, cursor mileage silently ignored.",
            "Stroke batching is carrying the tiny repetitive parts.",
            "Pixels are becoming rectangles with surprisingly good discipline.",
            "Exact progress, low drama, steady hands.",
        )
        return lines[int(done / max(1, total) * len(lines)) % len(lines)]

    def _hide_draw_hud(self) -> None:
        hud = getattr(self, "_draw_hud", None)
        if hud is None:
            return

        def _hide() -> None:
            if hud.winfo_exists():
                hud.withdraw()

        self.root.after(0, _hide)

    def _hide_ui_for_draw(self) -> None:
        if not self._hide_ui_while_drawing.get():
            return
        self._draw_ui_hidden = True
        self._show_draw_hud()
        if self._debug_panel is not None and self._debug_panel.winfo_exists():
            self._debug_panel.withdraw()
        self.root.withdraw()

    def _show_ui_after_draw(self) -> None:
        if not self._draw_ui_hidden:
            return

        def _show() -> None:
            self._hide_draw_hud()
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
