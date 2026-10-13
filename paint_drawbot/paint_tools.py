from __future__ import annotations

import threading
import time

from .hotkeys import _pause_event, _stop_event
from .logging_config import _log
from .models import BRUSH_LADDER_TEST_SIZES, _nearest_brush_size
from .mouse_input import MouseFailsafeError, _mouse_click, _nap, _stroke
from .paint_window import (
    _find_paint_hwnd,
    _focus_paint,
    _log_paint_rect,
    _paint_has_focus,
)
from .timing import _cfg


class PaintToolsMixin:
    def _stop_action(self) -> None:
        _log.info("STOP clicked")
        _stop_event.set()
        _pause_event.clear()
        self._set_status("Stopping…", self.err)


    def _test_stroke(self) -> None:
        if not self._is_calibrated:
            self._set_status("Calibrate first before testing.", self.warn)
            return
        self._test_btn.config(state="disabled")
        threading.Thread(target=self._test_stroke_thread, daemon=True).start()


    def _test_stroke_thread(self) -> None:
        try:
            _log.info("=== TEST STROKE ===")
            hwnd = _find_paint_hwnd()
            if not hwnd:
                self._set_status("Paint not found for test stroke.", self.err)
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
        except MouseFailsafeError as exc:
            _log.warning("Test stroke aborted by mouse failsafe: %s", exc)
            self._set_status(str(exc), self.err)
        finally:
            self.root.after(0, lambda: self._test_btn.config(state="normal"))


    def _set_brush_size(
            self,
            px: int,
            hwnd: int | None = None,
            click_pre_down: float | None = None,
            click_pre_up: float | None = None,
            post_delay: float | None = None,
            force: bool = False) -> int:
        snapped = _nearest_brush_size(px)
        if self.brush_cal is None:
            return snapped
        if not force and self._current_paint_brush == snapped:
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
        try:
            _log.info("=== TEST BRUSH LADDER ===")
            hwnd = _find_paint_hwnd()
            if not hwnd:
                self._set_status("Paint not found for brush ladder.", self.err)
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
        except MouseFailsafeError as exc:
            _log.warning("Brush ladder aborted by mouse failsafe: %s", exc)
            self._set_status(str(exc), self.err)
        finally:
            self.root.after(0, lambda: self._brush_test_btn.config(state="normal"))


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
