from __future__ import annotations

import threading
import time

import numpy as np
from PIL import Image

from .hotkeys import _pause_event, _stop_event
from .image_indexing import _index_image_data
from .logging_config import _log
from .models import (
    AutoBrushMode,
    BrushAction,
    DitherPreset,
    PlanStats,
    PlannerMode,
    _nearest_brush_size,
)
from .mouse_input import (
    _InputBatch,
    MouseFailsafeError,
    _mouse_click,
    _nap,
    _SEND_INPUT_CHUNK,
    _stroke_line,
    _stroke_path,
)
from .paint_window import (
    _find_paint_hwnd,
    _focus_paint,
    _log_paint_rect,
    _paint_has_focus,
)
from .planner import (
    GridAction,
    GridFill,
    GridLine,
    GridPath,
    GridRun,
    PaletteRun,
    _action_event_cost,
    _as_line_strokes,
    _auto_brush_candidate_sizes,
    _extract_rect_fill_actions,
    _is_grid_fill,
    _is_grid_path,
    _merge_tiny_noise,
    _optimize_grid_strokes,
    _optimize_grid_strokes_fast,
    _overpaint_cost,
    _overpaint_segment_layers,
    _plan_auto_brush_actions,
    _planned_action_count,
)
from .timing import _cfg


class DrawingMixin:
    def _start_drawing(self) -> None:
        _log.info("START clicked")
        if not self._is_calibrated:
            self._set_status("Calibrate first before drawing.", self.warn)
            self._check_start_ready()
            return
        if not self._image_path:
            self._set_status("Select an image before drawing.", self.warn)
            self._check_start_ready()
            return

        # Sanity check brush before starting
        ok, msg = self._brush_sanity()
        if not ok:
            self._set_status(f"Brush/detail warning: {msg}", self.warn)
            # Don't block — user may still want to proceed
        else:
            _log.info("Brush sanity OK: %s", msg)

        self._start_btn.config(state="disabled")
        self._sel_btn.config(state="disabled")
        self._cal_btn.config(state="disabled")
        self._tools_btn.config(state="disabled")
        self._test_btn.config(state="disabled")
        self._preview_btn.config(state="disabled")
        self._brush_test_btn.config(state="disabled")
        self._stop_btn.config(state="normal")
        self._hide_ui_for_draw()
        threading.Thread(target=self._safe_draw_thread, daemon=True).start()


    def _safe_draw_thread(self) -> None:
        try:
            self._draw_thread()
        except MouseFailsafeError as exc:
            _log.warning("Draw aborted by mouse failsafe: %s", exc)
            self._set_status(str(exc), self.err)
            self._re_enable()
        except Exception as exc:
            _log.exception("Draw thread crashed: %s", exc)
            self._set_status(f"Draw failed: {exc}", self.err)
            self._re_enable()


    def _draw_thread(self) -> None:
        _log.info("=== DRAW THREAD START ===")
        draw_t0 = time.monotonic()
        _pause_event.clear()
        _stop_event.clear()

        step = self._step()
        spd  = self._stroke_duration()
        cols, rows = self._grid_dims(step)
        brush_px = self._brush_px.get()

        dither_preset = self._dither_preset()
        planner_mode = self._planner()
        auto_brush_mode = self._auto_brush()
        if self.brush_cal is None:
            auto_brush_mode = AutoBrushMode.OFF
        max_auto_brush = _nearest_brush_size(self._max_auto_brush.get())
        use_dither = self._use_dither(step)
        effective_dither = dither_preset if use_dither else DitherPreset.FLAT
        grayscale_only = self._grayscale_only.get()
        active_palette_indices = self._active_palette_indices()
        active_pal_rgb = [
            tuple(self.palette[idx]["rgb"]) for idx in active_palette_indices
        ]
        # PERF: Tk variable reads cross into Tcl; snapshot draw-loop timings once.
        move_settle_ms = _cfg.move_settle_ms.get()
        press_settle_ms = _cfg.press_settle_ms.get()
        post_step_ms = _cfg.post_step_ms.get()
        release_settle_ms = _cfg.release_settle_ms.get()
        min_stroke_s = _cfg.min_stroke_ms.get() / 1000.0
        drag_steps = max(1, _cfg.drag_steps.get())
        click_pre_down_s = _cfg.click_pre_down_ms.get() / 1000.0
        click_pre_up_s = _cfg.click_pre_up_ms.get() / 1000.0
        pal_delay_s = _cfg.pal_delay_ms.get() / 1000.0
        pencil_rearm_s = _cfg.pencil_rearm_ms.get() / 1000.0
        skip_pencil_rearm = _cfg.skip_pencil_rearm.get()
        focus_delay_s = _cfg.focus_delay_ms.get() / 1000.0
        focus_interval = _cfg.focus_interval.get()
        focus_every = max(10, focus_interval)
        row_focus_interval = max(1, focus_interval)
        resume_focus_delay_s = min(0.12, focus_delay_s)
        rearm_focus_delay_s = max(0.08, focus_delay_s / 5.0)
        batch_colours = self._batch_colours.get()
        skip_white = self._skip_white.get()
        smart_underpaint_setting = self._smart_underpaint.get()
        merge_tiny_setting = self._merge_tiny_runs.get()
        max_speed_planner = planner_mode != PlannerMode.EXACT
        fill_tool_pos = self.fill_tool
        eraser_tool_pos = self.eraser_tool
        paint_fill_accel = (
            self._paint_fill_accel.get()
            and max_speed_planner
            and fill_tool_pos is not None
            and eraser_tool_pos is not None
        )
        _log.info("Draw params: step=%d  spd=%.3fs  grid=%dx%d  brush=%dpx  skip_rearm=%s  batch=%s  skip_white=%s  underpaint=%s  tiny_merge=%s  dither=%s  planner=%s  auto_brush=%s/%d  grayscale=%s  active_colours=%d  max_speed=%s  fill_accel=%s",
                  step, spd, cols, rows, brush_px,
                  skip_pencil_rearm, batch_colours,
                  skip_white, smart_underpaint_setting,
                  merge_tiny_setting, effective_dither.value,
                  planner_mode.value, auto_brush_mode.value, max_auto_brush,
                  grayscale_only,
                  len(active_palette_indices), max_speed_planner,
                  paint_fill_accel)

        if cols < 1 or rows < 1:
            self._set_status("Canvas too small for this detail level.", self.err)
            self._re_enable()
            return

        # ── Load & resize ──────────────────────────────────────────────────────
        try:
            img = Image.open(self._image_path).convert("RGB")
            _log.info("Opened image: %s  size=%s", self._image_path, img.size)
        except Exception as exc:
            _log.exception("Cannot open image: %s", exc)
            self._set_status(f"Cannot open image: {exc}", self.err)
            self._re_enable()
            return

        # ── Find Paint ─────────────────────────────────────────────────────────
        hwnd = _find_paint_hwnd()
        if not hwnd:
            self._set_status("MS Paint not found.", self.err)
            self._re_enable()
            return
        self._hwnd = hwnd
        _log_paint_rect(hwnd)

        img = img.resize((cols, rows), Image.Resampling.LANCZOS)
        index_result = _index_image_data(
            img,
            active_pal_rgb,
            effective_dither,
            planner_mode,
        )
        pixel_indices = index_result.pixel_indices
        fast_planner_used = index_result.fast_used
        _log.info("Dither stats: %s", index_result.stats)
        if active_palette_indices != list(range(len(self.palette))):
            index_map = bytes(
                active_palette_indices[i] if i < len(active_palette_indices) else 0
                for i in range(256)
            )
            pixel_indices = pixel_indices.translate(index_map)
        _log.info("Resized and palette-indexed to %dx%d  dither=%s  palette=%s  fast=%s",
                  cols, rows, effective_dither.value,
                  "grayscale" if grayscale_only or effective_dither == DitherPreset.CLEAN else "full",
                  fast_planner_used)

        if _stop_event.is_set():
            self._re_enable()
            return

        merge_tiny_runs = merge_tiny_setting and not use_dither
        white_idx = next(
            (i for i, colour in enumerate(self.palette) if colour["name"] == "White"),
            -1,
        )

        self._set_status("Starting draw...", self.warn)
        if not self._draw_ui_hidden:
            _nap(0.1)
        _focus_paint(hwnd, delay=focus_delay_s)

        # ── Arm pencil ─────────────────────────────────────────────────────────
        _log.info("Arming pencil @ %s", self.pencil_tool)
        _mouse_click(
            *self.pencil_tool,
            delay=0.01,
            pre_down=click_pre_down_s,
            pre_up=click_pre_up_s,
        )
        self._current_paint_brush = None

        # ── Plan all rows ──────────────────────────────────────────────────────
        self._set_status("Planning strokes…", "#5bc4ff")
        _log.info("Planning %d rows…", rows)

        smart_underpaint = batch_colours and smart_underpaint_setting
        skip_indices = {white_idx} if skip_white and white_idx >= 0 else set()
        auto_brush_actions: list[BrushAction] = []
        auto_covered = np.zeros((rows, cols), dtype=bool)
        auto_stats = PlanStats(planner_mode=auto_brush_mode.value)
        if auto_brush_mode != AutoBrushMode.OFF:
            auto_t0 = time.monotonic()
            candidates = _auto_brush_candidate_sizes(
                max_auto_brush, step, auto_brush_mode
            )
            pixel_index_arr = np.frombuffer(
                pixel_indices, dtype=np.uint8
            ).reshape((rows, cols))
            auto_brush_actions, auto_covered, auto_stats = _plan_auto_brush_actions(
                pixel_index_arr,
                len(self.palette),
                skip_indices,
                candidates,
                step,
                auto_brush_mode,
            )
            _log.info(
                "Auto brush plan: actions=%d pixels=%d sizes=%s candidates=%s seconds=%.3f",
                auto_stats.auto_brush_actions,
                auto_stats.auto_brush_pixels,
                auto_stats.auto_brush_sizes,
                candidates,
                time.monotonic() - auto_t0,
            )
        residual_skip_indices = set(skip_indices)
        if auto_brush_actions:
            residual_skip_indices.add(-1)
        rle_rows: list[list[tuple[int, int, int]]] = []
        layered_strokes_by_colour: list[list[list[GridRun]]] = []
        skipped_white_runs = 0
        skipped_auto_runs = 0
        overpaint_saved_runs = 0
        max_overpaint_layers = 0
        # Exact 1px optimization can swap horizontal runs for cheaper
        # horizontal/vertical/diagonal coverage without changing target pixels.
        line_optimizer_possible = batch_colours and step == 1 and brush_px == 1
        line_optimizer_enabled = line_optimizer_possible
        line_optimizer_diagonal = use_dither
        line_optimizer_paths = use_dither
        line_optimizer_fast = (
            line_optimizer_possible
            and (
                planner_mode == PlannerMode.FAST
                or (planner_mode == PlannerMode.AUTO and (fast_planner_used or use_dither))
            )
        )
        fill_accel_enabled = paint_fill_accel and line_optimizer_possible
        line_opt_before = 0
        line_opt_after = 0
        fill_actions_used = 0
        fill_rows_replaced = 0
        raw_runs_buf: list[tuple[int, int, int]] = []
        segment_buf: list[PaletteRun] = []
        # PERF: Row memoryview slices avoid per-pixel row_offset arithmetic.
        pixel_rows = memoryview(pixel_indices)

        def _ensure_stroke_layer(layer_idx: int) -> None:
            while len(layered_strokes_by_colour) <= layer_idx:
                layered_strokes_by_colour.append([[] for _ in self.palette])

        def _add_layer_run(
                layer_idx: int,
                colour_idx: int,
                gy: int,
                gx_start: int,
                gx_end: int) -> None:
            _ensure_stroke_layer(layer_idx)
            layered_strokes_by_colour[layer_idx][colour_idx].append(
                (gy, gx_start, gx_end)
            )

        def _plan_segment(gy: int, segment: list[PaletteRun]) -> None:
            nonlocal overpaint_saved_runs, max_overpaint_layers
            if not segment:
                return
            layers = (
                _overpaint_segment_layers(segment)
                if smart_underpaint else [segment]
            )
            if smart_underpaint:
                original_count = len(segment)
                layered_count = _overpaint_cost(layers)
                overpaint_saved_runs += max(0, original_count - layered_count)
                max_overpaint_layers = max(max_overpaint_layers, len(layers))
            for layer_idx, layer in enumerate(layers):
                for colour_idx, gx_start, gx_end in layer:
                    _add_layer_run(layer_idx, colour_idx, gy, gx_start, gx_end)

        for gy in range(rows):
            row = pixel_rows[gy * cols:(gy + 1) * cols]
            covered_row = auto_covered[gy] if auto_brush_actions else None
            raw_runs_buf.clear()
            cur_idx = -1 if covered_row is not None and covered_row[0] else row[0]
            run_start = 0

            for gx in range(1, cols):
                idx = -1 if covered_row is not None and covered_row[gx] else row[gx]
                if idx != cur_idx:
                    raw_runs_buf.append((cur_idx, run_start, gx - 1))
                    cur_idx, run_start = idx, gx

            raw_runs_buf.append((cur_idx, run_start, cols - 1))
            raw_runs = (
                _merge_tiny_noise(raw_runs_buf, residual_skip_indices)
                if merge_tiny_runs else raw_runs_buf
            )

            if batch_colours:
                segment_buf.clear()
                for colour_idx, gx_start, gx_end in raw_runs:
                    if colour_idx in residual_skip_indices:
                        if colour_idx == -1:
                            skipped_auto_runs += 1
                        else:
                            skipped_white_runs += 1
                        _plan_segment(gy, segment_buf)
                        segment_buf.clear()
                    else:
                        segment_buf.append((colour_idx, gx_start, gx_end))
                _plan_segment(gy, segment_buf)
            else:
                runs: list[tuple[int, int, int]] = []
                for colour_idx, gx_start, gx_end in raw_runs:
                    if colour_idx in residual_skip_indices:
                        if colour_idx == -1:
                            skipped_auto_runs += 1
                        else:
                            skipped_white_runs += 1
                    else:
                        runs.append((colour_idx, gx_start, gx_end))
                rle_rows.append(runs)

        line_optimizer_enabled = (
            line_optimizer_possible
            and planner_mode != PlannerMode.EXACT
            and (use_dither or not smart_underpaint or overpaint_saved_runs < 1024)
        )
        if planner_mode == PlannerMode.EXACT:
            line_optimizer_enabled = line_optimizer_possible

        if batch_colours:
            def _make_line_plan(
                    strokes_by_colour: list[list[GridRun]],
                    optimize_lines: bool) -> list[tuple[int, list[GridAction]]]:
                nonlocal line_opt_before, line_opt_after
                nonlocal fill_actions_used, fill_rows_replaced
                plan: list[tuple[int, list[GridAction]]] = []
                for idx, runs in enumerate(strokes_by_colour):
                    if not runs:
                        continue

                    original_count = len(runs)
                    fill_actions: list[GridAction] = []
                    plan_runs = runs
                    if fill_accel_enabled:
                        plan_runs, fill_actions = _extract_rect_fill_actions(runs)
                        if fill_actions:
                            fill_actions_used += len(fill_actions)
                            fill_rows_replaced += original_count - len(plan_runs)

                    horizontal = _as_line_strokes(plan_runs)
                    strokes = horizontal
                    if optimize_lines:
                        if line_optimizer_fast:
                            optimized = _optimize_grid_strokes_fast(
                                plan_runs,
                                cols,
                                rows,
                            )
                        else:
                            optimized = _optimize_grid_strokes(
                                plan_runs,
                                cols,
                                rows,
                                diagonal=line_optimizer_diagonal,
                                paths=line_optimizer_paths,
                            )
                        if _action_event_cost(optimized) < _action_event_cost(horizontal):
                            strokes = optimized
                        line_opt_before += original_count
                        line_opt_after += sum(_planned_action_count(action) for action in strokes)

                    if fill_actions:
                        strokes = fill_actions + strokes
                    plan.append((idx, strokes))
                return plan

            draw_layers = []
            for layer_idx, strokes_by_colour in enumerate(layered_strokes_by_colour):
                layer_plan = _make_line_plan(
                    strokes_by_colour, line_optimizer_enabled
                )
                if not layer_plan:
                    continue
                layer_plan.sort(key=lambda item: len(item[1]), reverse=True)
                if smart_underpaint and max_overpaint_layers > 1:
                    layer_name = f"overpaint {layer_idx + 1}/{max_overpaint_layers}"
                else:
                    layer_name = "detail"
                draw_layers.append((layer_name, layer_plan))
            total_strokes = sum(
                _planned_action_count(action)
                for _, plan in draw_layers for _, runs in plan for action in runs
            )
            used_colours = len({
                idx for _, plan in draw_layers for idx, _ in plan
            })
        else:
            draw_layers = []
            total_strokes = sum(len(runs) for runs in rle_rows)
            used_colours = len({idx for runs in rle_rows for idx, _, _ in runs})

        auto_brush_layers: list[tuple[int, list[tuple[int, list[BrushAction]]]]] = []
        if auto_brush_actions:
            by_size_colour: dict[int, dict[int, list[BrushAction]]] = {}
            for action in auto_brush_actions:
                by_size_colour.setdefault(action.brush_px, {}).setdefault(
                    action.colour_idx, []
                ).append(action)
            for size in sorted(by_size_colour, reverse=True):
                colour_plan = sorted(
                    by_size_colour[size].items(),
                    key=lambda item: len(item[1]),
                    reverse=True,
                )
                auto_brush_layers.append((size, colour_plan))
            total_strokes += len(auto_brush_actions)
            used_colours = len({
                *({idx for _, plan in draw_layers for idx, _ in plan} if batch_colours else set()),
                *({idx for _, plan in auto_brush_layers for idx, _ in plan}),
                *({idx for runs in rle_rows for idx, _, _ in runs} if not batch_colours else set()),
            })

        _log.info(
            "Quantisation done. Total strokes=%d colours=%d skipped_white=%d skipped_auto=%d "
            "overpaint_saved=%d layers=%d mode=%s line_opt=%s saved=%d "
            "fill_actions=%d fill_rows=%d auto_actions=%d auto_pixels=%d",
            total_strokes, used_colours, skipped_white_runs, skipped_auto_runs,
            overpaint_saved_runs,
            max(1, max_overpaint_layers),
            "smart-batched" if smart_underpaint else (
                "batched" if batch_colours else "row"
            ),
            (
                f"{line_opt_before}->{line_opt_after}"
                if line_opt_before else "off"
            ),
            max(0, line_opt_before - line_opt_after),
            fill_actions_used,
            fill_rows_replaced,
            len(auto_brush_actions),
            auto_stats.auto_brush_pixels,
        )

        if total_strokes < 1:
            self._set_progress(100)
            self._set_status("Nothing to draw after white-skip.", self.acc)
            self._re_enable()
            return

        drawn_strokes = 0
        last_colour_idx: int | None = None
        last_status_t = 0.0
        batchable = (
            max(min_stroke_s, spd) <= 0
            and drag_steps == 1
            and move_settle_ms <= 0
            and press_settle_ms <= 0
            and post_step_ms <= 0
            and release_settle_ms <= 0
        )
        input_batch = _InputBatch() if batchable else None
        batch_stroke_limit = 1024
        scheduled_strokes = 0
        pause_poll_interval = 64
        status_poll_interval = 32
        _log.info("Stroke event batching: %s", batchable)

        def _flush_batch() -> None:
            nonlocal drawn_strokes
            if input_batch is None:
                return
            drawn_strokes += input_batch.flush()

        def _draw_planned_stroke(
                x_start: int, y_start: int, x_end: int, y_end: int) -> None:
            nonlocal drawn_strokes, scheduled_strokes
            if input_batch is not None:
                drawn_strokes += input_batch._append_stroke_nonnull(
                    x_start, y_start, x_end, y_end
                )
                if (input_batch.strokes >= batch_stroke_limit
                        or input_batch.count >= _SEND_INPUT_CHUNK - 3):
                    _flush_batch()
            else:
                _stroke_line(x_start, y_start, x_end, y_end, spd)
                drawn_strokes += 1
            scheduled_strokes += 1

        def _draw_planned_path(points: list[tuple[int, int]]) -> None:
            nonlocal drawn_strokes, scheduled_strokes
            if input_batch is not None:
                drawn_strokes += input_batch.append_path(points)
                if (input_batch.strokes >= batch_stroke_limit
                        or input_batch.count >= _SEND_INPUT_CHUNK - 3):
                    _flush_batch()
            else:
                _stroke_path(points, spd)
                drawn_strokes += 1
            scheduled_strokes += 1

        def _pause_if_needed(where: str) -> bool:
            nonlocal last_colour_idx
            if not _pause_event.is_set():
                return _stop_event.is_set()
            _flush_batch()
            self._set_status("Paused - press ESC to resume", self.warn)
            _log.info("Paused at %s", where)
            while _pause_event.is_set() and not _stop_event.is_set():
                _nap(0.03)
            if _stop_event.is_set():
                return True
            _log.info("Resumed at %s - re-focusing Paint", where)
            _focus_paint(hwnd, delay=resume_focus_delay_s)
            _mouse_click(
                *self.pencil_tool,
                delay=0.05,
                pre_down=click_pre_down_s,
                pre_up=click_pre_up_s,
            )
            last_colour_idx = None
            return False

        def _stop_now(where: str) -> None:
            _log.info(
                "Stop event: %s  strokes %d/%d",
                where, drawn_strokes, total_strokes,
            )
            self._set_status("Stopped.", self.err)
            self._re_enable()

        canvas_x = self.canvas_x
        canvas_y = self.canvas_y
        canvas_x_max = self.canvas_x + self.canvas_w - 1
        canvas_y_max = self.canvas_y + self.canvas_h - 1
        half_step = step // 2
        row_y_by_gy = [
            min(canvas_y_max, canvas_y + gy * step + half_step)
            for gy in range(rows)
        ]
        cell_x_by_gx = [
            min(canvas_x_max, canvas_x + gx * step + half_step)
            for gx in range(cols)
        ]
        run_x_start_by_gx = [
            canvas_x + gx * step
            for gx in range(cols)
        ]
        run_x_end_by_gx = [
            canvas_x_max if gx == cols - 1 else canvas_x + (gx + 1) * step - 1
            for gx in range(cols)
        ]

        def _stroke_coords(gx_start: int, gx_end: int) -> tuple[int, int]:
            x_start = run_x_start_by_gx[gx_start]
            x_end = run_x_end_by_gx[gx_end]
            return x_start, max(x_start, x_end)

        def _row_y(gy: int) -> int:
            return row_y_by_gy[gy]

        def _cell_center(gx: int, gy: int) -> tuple[int, int]:
            return cell_x_by_gx[gx], row_y_by_gy[gy]

        def _stroke_line_coords(stroke: GridLine) -> tuple[int, int, int, int]:
            gy1, gx1, gy2, gx2 = stroke
            if gy1 == gy2:
                x_start, x_end = _stroke_coords(gx1, gx2)
                y_screen = _row_y(gy1)
                return x_start, y_screen, x_end, y_screen

            x_start, y_start = _cell_center(gx1, gy1)
            x_end, y_end = _cell_center(gx2, gy2)
            return x_start, y_start, x_end, y_end

        def _path_coords(path: GridPath) -> list[tuple[int, int]]:
            return [_cell_center(gx, gy) for gy, gx in path]

        def _draw_fill_action(action: GridFill) -> None:
            nonlocal drawn_strokes, scheduled_strokes
            _, seed_gy, seed_gx, boundary = action
            boundary_points = _path_coords(boundary)
            if input_batch is not None:
                drawn_strokes += input_batch.append_path(boundary_points)
                _flush_batch()
            else:
                _stroke_path(boundary_points, spd)
                drawn_strokes += 1
            scheduled_strokes += 1

            if fill_tool_pos is None:
                return
            seed_x, seed_y = _cell_center(seed_gx, seed_gy)
            _mouse_click(
                *fill_tool_pos,
                delay=pal_delay_s,
                pre_down=click_pre_down_s,
                pre_up=click_pre_up_s,
            )
            _mouse_click(
                seed_x,
                seed_y,
                delay=pal_delay_s,
                pre_down=click_pre_down_s,
                pre_up=click_pre_up_s,
            )
            _mouse_click(
                *self.pencil_tool,
                delay=pencil_rearm_s,
                pre_down=click_pre_down_s,
                pre_up=click_pre_up_s,
            )
            drawn_strokes += 1
            scheduled_strokes += 1

        def _draw_brush_action(action: BrushAction) -> None:
            y_screen = _row_y(action.gy)
            x_start = cell_x_by_gx[action.gx_start]
            x_end = cell_x_by_gx[action.gx_end]
            _draw_planned_stroke(x_start, y_screen, x_end, y_screen)

        hud_started_at = time.monotonic()
        self._update_draw_hud_stats(
            0,
            total_strokes,
            hud_started_at,
            "Plan ready",
            extra=f"{used_colours} colours",
            force=True,
        )

        def _report_progress(phase: str, extra: str = "") -> None:
            nonlocal last_status_t
            if scheduled_strokes <= 0:
                return
            now = time.monotonic()
            if now - last_status_t < 0.2:
                return
            pct = 100.0 * scheduled_strokes / total_strokes
            if self._draw_ui_hidden:
                self._update_draw_hud_stats(
                    scheduled_strokes,
                    total_strokes,
                    hud_started_at,
                    phase,
                    extra=extra,
                )
            else:
                _flush_batch()
                self._set_status(
                    f"Drawing...  {phase}  "
                    f"actions {scheduled_strokes:,}/{total_strokes:,}  "
                    f"({pct:.0f}%)",
                    "#5bc4ff",
                )
                self._set_progress(pct)
            last_status_t = now

        # ── Main draw loop ─────────────────────────────────────────────────────
        _log.info("=== MAIN DRAW LOOP (%s) ===",
                  "SMART BATCHED" if smart_underpaint
                  else ("COLOUR BATCHED" if batch_colours else "ROW ORDER"))

        if auto_brush_layers:
            _log.info("=== AUTO BRUSH PASS (%d sizes) ===", len(auto_brush_layers))
            for size_no, (brush_size, colour_plan) in enumerate(auto_brush_layers, 1):
                where = f"auto brush {size_no}/{len(auto_brush_layers)}"
                if _pause_if_needed(where):
                    _stop_now(where)
                    return

                _flush_batch()
                self._set_brush_size(
                    brush_size,
                    hwnd,
                    click_pre_down=click_pre_down_s,
                    click_pre_up=click_pre_up_s,
                    post_delay=max(0.02, pal_delay_s),
                    force=size_no == 1,
                )
                last_colour_idx = None

                for colour_no, (colour_idx, actions) in enumerate(colour_plan, 1):
                    colour = self.palette[colour_idx]
                    where = (
                        f"brush {brush_size}px {size_no}/{len(auto_brush_layers)} "
                        f"colour {colour_no}/{len(colour_plan)} {colour['name']}"
                    )
                    if _pause_if_needed(where):
                        _stop_now(where)
                        return

                    if colour_idx != last_colour_idx:
                        _flush_batch()
                        self._pick_colour(
                            colour,
                            hwnd,
                            pal_d=pal_delay_s,
                            skip_pencil_rearm=skip_pencil_rearm,
                            rearm_d=pencil_rearm_s,
                            click_pre_down=click_pre_down_s,
                            click_pre_up=click_pre_up_s,
                            rearm_focus_delay=rearm_focus_delay_s,
                        )
                        last_colour_idx = colour_idx

                    for action in actions:
                        _draw_brush_action(action)

                        if scheduled_strokes % pause_poll_interval == 0:
                            if _pause_if_needed(
                                    f"stroke {scheduled_strokes}/{total_strokes}"):
                                _stop_now(
                                    f"stroke {scheduled_strokes}/{total_strokes}")
                                return

                        if scheduled_strokes > 0 and scheduled_strokes % focus_every == 0:
                            _flush_batch()
                            if not _paint_has_focus(hwnd):
                                _log.info("Stroke %d: focus lost - re-focusing",
                                          drawn_strokes)
                                _focus_paint(hwnd, delay=0.08)
                                _mouse_click(
                                    *self.pencil_tool,
                                    delay=0.05,
                                    pre_down=click_pre_down_s,
                                    pre_up=click_pre_up_s,
                                )
                                last_colour_idx = None

                        if scheduled_strokes % status_poll_interval == 0:
                            _report_progress(where, f"{used_colours} colours")

            _flush_batch()
            self._set_brush_size(
                brush_px,
                hwnd,
                click_pre_down=click_pre_down_s,
                click_pre_up=click_pre_up_s,
                post_delay=max(0.02, pal_delay_s),
                force=True,
            )
            last_colour_idx = None

        if batch_colours:
            layer_count = len(draw_layers)
            for layer_no, (layer_name, draw_plan) in enumerate(draw_layers, 1):
                layer_colours = len(draw_plan)
                for colour_no, (colour_idx, strokes) in enumerate(draw_plan, 1):
                    colour = self.palette[colour_idx]
                    where = (
                        f"{layer_name} layer {layer_no}/{layer_count} "
                        f"colour {colour_no}/{layer_colours}"
                    )

                    if _pause_if_needed(where):
                        _stop_now(where)
                        return

                    for stroke in strokes:
                        if colour_idx != last_colour_idx:
                            _flush_batch()
                            _log.debug("%s -> %s (%d strokes)",
                                       where, colour["name"], len(strokes))
                            self._pick_colour(
                                colour,
                                hwnd,
                                pal_d=pal_delay_s,
                                skip_pencil_rearm=skip_pencil_rearm,
                                rearm_d=pencil_rearm_s,
                                click_pre_down=click_pre_down_s,
                                click_pre_up=click_pre_up_s,
                                rearm_focus_delay=rearm_focus_delay_s,
                            )
                            last_colour_idx = colour_idx

                        if _is_grid_fill(stroke):
                            _draw_fill_action(stroke)
                        elif _is_grid_path(stroke):
                            _draw_planned_path(_path_coords(stroke))
                        else:
                            x_start, y_start, x_end, y_end = _stroke_line_coords(stroke)
                            _draw_planned_stroke(x_start, y_start, x_end, y_end)

                        if scheduled_strokes % pause_poll_interval == 0:
                            if _pause_if_needed(
                                    f"stroke {scheduled_strokes}/{total_strokes}"):
                                _stop_now(
                                    f"stroke {scheduled_strokes}/{total_strokes}")
                                return

                        if scheduled_strokes > 0 and scheduled_strokes % focus_every == 0:
                            _flush_batch()
                            if not _paint_has_focus(hwnd):
                                _log.info("Stroke %d: focus lost - re-focusing",
                                          drawn_strokes)
                                _focus_paint(hwnd, delay=0.08)
                                _mouse_click(
                                    *self.pencil_tool,
                                    delay=0.05,
                                    pre_down=click_pre_down_s,
                                    pre_up=click_pre_up_s,
                                )
                                last_colour_idx = None

                        if scheduled_strokes % status_poll_interval == 0:
                            _report_progress(
                                f"{layer_name} {colour_no}/{layer_colours} "
                                f"{colour['name']}",
                                f"{used_colours} colours",
                            )
        else:
            for gy, runs in enumerate(rle_rows):
                if _pause_if_needed(f"row {gy}/{rows}"):
                    _stop_now(f"row {gy}/{rows}")
                    return

                if gy > 0 and gy % row_focus_interval == 0:
                    if not _paint_has_focus(hwnd):
                        _log.info("Row %d: focus lost - re-focusing", gy)
                        _focus_paint(hwnd, delay=0.08)
                        _mouse_click(
                            *self.pencil_tool,
                            delay=0.05,
                            pre_down=click_pre_down_s,
                            pre_up=click_pre_up_s,
                        )
                        last_colour_idx = None

                y_screen = _row_y(gy)

                for colour_idx, gx_start, gx_end in runs:
                    colour = self.palette[colour_idx]
                    if colour_idx != last_colour_idx:
                        _flush_batch()
                        _log.debug("Row %d: colour -> %s", gy, colour["name"])
                        self._pick_colour(
                            colour,
                            hwnd,
                            pal_d=pal_delay_s,
                            skip_pencil_rearm=skip_pencil_rearm,
                            rearm_d=pencil_rearm_s,
                            click_pre_down=click_pre_down_s,
                            click_pre_up=click_pre_up_s,
                            rearm_focus_delay=rearm_focus_delay_s,
                        )
                        last_colour_idx = colour_idx

                    x_start, x_end = _stroke_coords(gx_start, gx_end)
                    _draw_planned_stroke(x_start, y_screen, x_end, y_screen)

                    if scheduled_strokes % pause_poll_interval == 0:
                        if _pause_if_needed(
                                f"stroke {scheduled_strokes}/{total_strokes}"):
                            _stop_now(
                                f"stroke {scheduled_strokes}/{total_strokes}")
                            return

                if (
                        scheduled_strokes > 0
                        and scheduled_strokes % status_poll_interval == 0):
                    _report_progress(
                        f"row {gy + 1}/{rows}",
                        f"{used_colours} colours",
                    )

        _flush_batch()
        _log.info("=== DRAW COMPLETE: %d strokes ===", drawn_strokes)
        self._update_draw_hud_stats(
            total_strokes,
            total_strokes,
            hud_started_at,
            "Done",
            extra=f"{used_colours} colours",
            force=True,
        )
        self._set_progress(100)
        self._set_status("✅  Done!", self.acc)
        self._re_enable()
