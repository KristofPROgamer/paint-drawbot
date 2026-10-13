from __future__ import annotations

import json
import os
import threading
import time

import keyboard
import win32api

from .hotkeys import _pause_event, _stop_event
from .logging_config import _log
from .models import AutoBrushMode, BrushSizeCalibration
from .paint_window import _find_paint_hwnd, _focus_paint, _log_paint_rect
from .palette import BASE_PALETTE
from .paths import CAL_FILE, CAL_SCHEMA_VERSION


class CalibrationMixin:
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
