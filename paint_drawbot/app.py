from __future__ import annotations

from .dpi import set_dpi_awareness

set_dpi_awareness()

import tkinter as tk

from .calibration import CalibrationMixin
from .debug_panel import DebugPanel
from .drawing import DrawingMixin
from .hotkeys import _register_hotkeys
from .image_workflow import ImageWorkflowMixin
from .models import (
    AutoBrushMode,
    BrushSizeCalibration,
    DitherPreset,
    PlannerMode,
)
from .paint_tools import PaintToolsMixin
from .settings import SettingsMixin
from .timing import DelayConfig, set_delay_config
from .ui import UiMixin


class DrawBotApp(UiMixin, SettingsMixin, CalibrationMixin, ImageWorkflowMixin, PaintToolsMixin, DrawingMixin):
    DETAIL_MIN = 1
    DETAIL_MAX = 20

    # ── Init ───────────────────────────────────────────────────────────────────

    def __init__(self, root: tk.Tk) -> None:
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

        # Initialise shared config BEFORE building UI (widgets bind to its vars)
        set_delay_config(DelayConfig())

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
