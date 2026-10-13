from __future__ import annotations

import queue
import threading

from .estimation import EstimateRequest, EstimateResult, estimate_draw
from .models import AutoBrushMode, DitherPreset, PlannerMode
from .timing import _cfg


class SettingsMixin:
    def _step(self) -> int:
        """Pixel step between sampled rows/columns; high detail reaches 1 px."""
        detail = max(
            self.DETAIL_MIN,
            min(self.DETAIL_MAX, int(round(float(self._detail.get())))),
        )
        return max(1, int(round(24 / detail)))


    def _grid_dims(self, step: int | None = None) -> tuple[int, int]:
        step = step or self._step()
        cols = max(1, (self.canvas_w + step - 1) // step)
        rows = max(1, (self.canvas_h + step - 1) // step)
        return cols, rows


    def _stroke_duration(self) -> float:
        return self._speed_ms.get() / 1000.0


    def _dither_preset(self) -> DitherPreset:
        try:
            return DitherPreset(self._dither_style.get())
        except ValueError:
            return DitherPreset.SMART


    def _planner(self) -> PlannerMode:
        try:
            return PlannerMode(self._planner_mode.get())
        except ValueError:
            return PlannerMode.AUTO


    def _auto_brush(self) -> AutoBrushMode:
        try:
            return AutoBrushMode(self._auto_brush_mode.get())
        except ValueError:
            return AutoBrushMode.OFF


    def _use_dither(self, step: int | None = None) -> bool:
        step = step or self._step()
        return self._dither_preset() != DitherPreset.FLAT and step <= 2


    def _active_palette_indices(self) -> list[int]:
        force_gray = self._dither_preset() == DitherPreset.CLEAN
        if not self._grayscale_only.get() and not force_gray:
            return list(range(len(self.palette)))

        indices: list[int] = []
        gray_names = {"black", "dark grey", "dark gray", "light grey",
                      "light gray", "white"}
        for idx, colour in enumerate(self.palette):
            r, g, b = colour["rgb"]
            name = str(colour["name"]).strip().lower()
            if name in gray_names or (r == g == b):
                indices.append(idx)

        return indices or list(range(len(self.palette)))


    def _brush_sanity(self) -> tuple[bool, str]:
        """
        Returns (ok, message).
        ok=False means gaps between rows are expected.
        """
        if not self._is_calibrated:
            return True, ""
        step = self._step()
        brush = self._brush_px.get()
        cols, rows = self._grid_dims(step)

        if step > brush:
            gap = step - brush
            msg = (f"Gap warning: step={step}px > brush={brush}px "
                   f"({gap}px gap). Increase detail or use a larger brush.")
            return False, msg
        elif step < brush:
            ov = brush - step
            msg = (f"Ultra overlap: brush={brush}px > step={step}px "
                   f"({ov}px overlap; more detail, more strokes).")
            return True, msg
        else:
            msg = f"Perfect fit: brush={brush}px = step={step}px, {cols}x{rows} grid."
            return True, msg


    def _refresh_estimate(self, *_) -> None:
        spd = self._speed_ms.get()
        self._speed_lbl.config(text=f"  current: {spd} ms/stroke")

        detail = max(
            self.DETAIL_MIN,
            min(self.DETAIL_MAX, int(round(float(self._detail.get())))),
        )
        step = self._step()
        if hasattr(self, "_detail_lbl"):
            self._detail_lbl.config(
                text=f"  current: {detail}/{self.DETAIL_MAX}  step={step}px"
            )

        ok, sanity_msg = self._brush_sanity()
        fg = self.warn if not ok else (
            self.muted if "overlap" in sanity_msg.lower() else self.acc
        )
        self._sanity_lbl.config(text=sanity_msg, fg=fg)

        if not self._is_calibrated:
            self._est_lbl.config(text="Calibrate first to see time estimate.")
            return

        cols, rows = self._grid_dims(step)
        if not self._image_path:
            self._est_lbl.config(
                text=(
                    f"step={step}px  grid={cols}x{rows}  "
                    f"{rows * cols:,} dots. Select an image for actual estimate."
                )
            )
            return

        request = self._estimate_request(step, cols, rows)
        if request is None:
            self._est_lbl.config(text="Select an image for actual estimate.")
            return
        cached_request = getattr(self, "_estimate_cache_request", None)
        cached_result = getattr(self, "_estimate_cache_result", None)
        if cached_request == request and cached_result is not None:
            self._display_estimate(cached_result, step, cols, rows)
            return

        self._est_lbl.config(
            text=(
                f"step={step}px  grid={cols}x{rows}  {rows * cols:,} dots. "
                "Estimating actual plan..."
            )
        )
        self._schedule_estimate(request)


    def _estimate_request(
            self,
            step: int,
            cols: int,
            rows: int) -> EstimateRequest | None:
        if not self._image_path or not self.palette:
            return None
        auto_brush = self._auto_brush()
        if self.brush_cal is None:
            auto_brush = AutoBrushMode.OFF
        palette = tuple(
            (
                str(colour["name"]),
                tuple(int(value) for value in colour["rgb"]),
            )
            for colour in self.palette
        )
        fill_ready = self.fill_tool is not None and self.eraser_tool is not None
        return EstimateRequest(
            image_path=self._image_path,
            cols=cols,
            rows=rows,
            step=step,
            brush_px=self._brush_px.get(),
            palette=palette,
            active_palette_indices=tuple(self._active_palette_indices()),
            dither=self._dither_preset(),
            planner=self._planner(),
            use_dither=self._use_dither(step),
            batch_colours=self._batch_colours.get(),
            skip_white=self._skip_white.get(),
            smart_underpaint=self._smart_underpaint.get(),
            merge_tiny_runs=self._merge_tiny_runs.get(),
            paint_fill_accel=self._paint_fill_accel.get(),
            fill_ready=fill_ready,
            auto_brush=auto_brush,
            max_auto_brush=self._max_auto_brush.get(),
            hide_ui_while_drawing=self._hide_ui_while_drawing.get(),
            stroke_duration_s=self._stroke_duration(),
            move_settle_s=_cfg.move_settle_ms.get() / 1000.0,
            press_settle_s=_cfg.press_settle_ms.get() / 1000.0,
            post_step_s=_cfg.post_step_ms.get() / 1000.0,
            release_settle_s=_cfg.release_settle_ms.get() / 1000.0,
            min_stroke_s=_cfg.min_stroke_ms.get() / 1000.0,
            drag_steps=max(1, _cfg.drag_steps.get()),
            click_pre_down_s=_cfg.click_pre_down_ms.get() / 1000.0,
            click_pre_up_s=_cfg.click_pre_up_ms.get() / 1000.0,
            pal_delay_s=_cfg.pal_delay_ms.get() / 1000.0,
            pencil_rearm_s=_cfg.pencil_rearm_ms.get() / 1000.0,
            skip_pencil_rearm=_cfg.skip_pencil_rearm.get(),
            focus_delay_s=_cfg.focus_delay_ms.get() / 1000.0,
        )


    def _schedule_estimate(self, request: EstimateRequest) -> None:
        self._estimate_pending_request = request
        self._estimate_token = getattr(self, "_estimate_token", 0) + 1
        after_id = getattr(self, "_estimate_after_id", None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._estimate_after_id = self.root.after(350, self._start_pending_estimate)


    def _start_pending_estimate(self) -> None:
        self._estimate_after_id = None
        if getattr(self, "_estimate_inflight", False):
            return
        request = getattr(self, "_estimate_pending_request", None)
        if request is None:
            return
        self._estimate_pending_request = None
        self._estimate_inflight = True
        token = getattr(self, "_estimate_token", 0)
        self._estimate_queue = queue.Queue()
        threading.Thread(
            target=self._estimate_thread,
            args=(request, token, self._estimate_queue),
            daemon=True,
        ).start()
        self.root.after(50, self._poll_estimate_queue)


    def _estimate_thread(
            self,
            request: EstimateRequest,
            token: int,
            result_queue: queue.Queue) -> None:
        result: EstimateResult | None = None
        error: Exception | None = None
        try:
            result = estimate_draw(request)
        except Exception as exc:
            error = exc
        result_queue.put((request, token, result, error))


    def _poll_estimate_queue(self) -> None:
        result_queue = getattr(self, "_estimate_queue", None)
        if result_queue is None:
            return
        try:
            request, token, result, error = result_queue.get_nowait()
        except queue.Empty:
            if getattr(self, "_estimate_inflight", False):
                self.root.after(50, self._poll_estimate_queue)
            return
        self._finish_estimate(request, token, result, error)


    def _finish_estimate(
            self,
            request: EstimateRequest,
            token: int,
            result: EstimateResult | None,
            error: Exception | None) -> None:
        self._estimate_inflight = False
        if result is not None:
            self._estimate_cache_request = request
            self._estimate_cache_result = result
        if token == getattr(self, "_estimate_token", 0):
            if result is not None:
                self._display_estimate(result, request.step, request.cols, request.rows)
            elif error is not None:
                self._est_lbl.config(text=f"Estimate unavailable: {error}")

        if getattr(self, "_estimate_pending_request", None) is not None:
            self.root.after(50, self._start_pending_estimate)


    def _display_estimate(
            self,
            result: EstimateResult,
            step: int,
            cols: int,
            rows: int) -> None:
        duration = self._format_duration(result.seconds)
        pieces = [
            f"actual {duration}",
            f"{result.total_units:,} actions",
            f"{result.colour_changes:,} colour picks",
            f"{result.used_colours} colours",
        ]
        if result.auto_actions:
            pieces.append(f"{result.auto_actions:,} auto-brush")
        if result.fill_actions:
            pieces.append(f"{result.fill_actions:,} fills")
        if result.line_opt_before:
            saved = max(0, result.line_opt_before - result.line_opt_after)
            pieces.append(f"{saved:,} line actions saved")
        self._est_lbl.config(
            text=(
                f"step={step}px  grid={cols}x{rows}  "
                + "  |  ".join(pieces)
                + f"\nmode={result.mode}  prep={result.prep_seconds:.2f}s"
            )
        )


    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = max(0.0, seconds)
        if seconds < 60:
            return f"{seconds:.0f}s"
        if seconds < 3600:
            return f"{seconds / 60:.1f} min"
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes:02d}m"
