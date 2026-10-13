from __future__ import annotations

from dataclasses import dataclass
import time

import numpy as np
from PIL import Image

from .image_indexing import _index_image_data
from .models import (
    AutoBrushMode,
    BrushAction,
    DitherPreset,
    PlannerMode,
    _nearest_brush_size,
)
from .planner import (
    GridAction,
    GridRun,
    PaletteRun,
    _action_event_cost,
    _as_line_strokes,
    _auto_brush_candidate_sizes,
    _extract_rect_fill_actions,
    _merge_tiny_noise,
    _optimize_grid_strokes,
    _optimize_grid_strokes_fast,
    _overpaint_cost,
    _overpaint_segment_layers,
    _plan_auto_brush_actions,
    _planned_action_count,
)


PaletteEntry = tuple[str, tuple[int, int, int]]


@dataclass(frozen=True)
class EstimateRequest:
    image_path: str
    cols: int
    rows: int
    step: int
    brush_px: int
    palette: tuple[PaletteEntry, ...]
    active_palette_indices: tuple[int, ...]
    dither: DitherPreset
    planner: PlannerMode
    use_dither: bool
    batch_colours: bool
    skip_white: bool
    smart_underpaint: bool
    merge_tiny_runs: bool
    paint_fill_accel: bool
    fill_ready: bool
    auto_brush: AutoBrushMode
    max_auto_brush: int
    hide_ui_while_drawing: bool
    stroke_duration_s: float
    move_settle_s: float
    press_settle_s: float
    post_step_s: float
    release_settle_s: float
    min_stroke_s: float
    drag_steps: int
    click_pre_down_s: float
    click_pre_up_s: float
    pal_delay_s: float
    pencil_rearm_s: float
    skip_pencil_rearm: bool
    focus_delay_s: float


@dataclass(frozen=True)
class EstimateResult:
    seconds: float
    prep_seconds: float
    total_units: int
    stroke_units: int
    colour_changes: int
    brush_size_changes: int
    fill_actions: int
    auto_actions: int
    auto_pixels: int
    skipped_white_runs: int
    skipped_auto_runs: int
    used_colours: int
    line_opt_before: int
    line_opt_after: int
    overpaint_saved_runs: int
    mode: str
    fast_dither_used: bool


def _click_seconds(post_delay: float, req: EstimateRequest) -> float:
    return req.click_pre_down_s + req.click_pre_up_s + post_delay


def _stroke_seconds(req: EstimateRequest) -> float:
    batchable = (
        max(req.min_stroke_s, req.stroke_duration_s) <= 0
        and req.drag_steps == 1
        and req.move_settle_s <= 0
        and req.press_settle_s <= 0
        and req.post_step_s <= 0
        and req.release_settle_s <= 0
    )
    if batchable:
        # SendInput batches still have a tiny kernel/user transition cost.
        return 0.00045
    return (
        req.move_settle_s
        + req.press_settle_s
        + max(req.min_stroke_s, req.stroke_duration_s)
        + req.post_step_s
        + req.release_settle_s
    )


def _count_colour_changes(colours: list[int]) -> int:
    changes = 0
    last: int | None = None
    for colour_idx in colours:
        if colour_idx != last:
            changes += 1
            last = colour_idx
    return changes


def estimate_draw(req: EstimateRequest) -> EstimateResult:
    t0 = time.monotonic()
    palette_rgb = [rgb for _, rgb in req.palette]
    palette_names = [name for name, _ in req.palette]
    active_indices = list(req.active_palette_indices)
    active_pal_rgb = [palette_rgb[idx] for idx in active_indices]
    effective_dither = req.dither if req.use_dither else DitherPreset.FLAT

    image = Image.open(req.image_path).convert("RGB")
    image = image.resize((req.cols, req.rows), Image.Resampling.LANCZOS)
    index_result = _index_image_data(
        image,
        active_pal_rgb,
        effective_dither,
        req.planner,
    )
    pixel_indices = index_result.pixel_indices
    if active_indices != list(range(len(palette_rgb))):
        index_map = bytes(
            active_indices[idx] if idx < len(active_indices) else 0
            for idx in range(256)
        )
        pixel_indices = pixel_indices.translate(index_map)

    white_idx = next(
        (idx for idx, name in enumerate(palette_names) if name == "White"),
        -1,
    )
    skip_indices = {white_idx} if req.skip_white and white_idx >= 0 else set()
    max_speed_planner = req.planner != PlannerMode.EXACT
    auto_brush_mode = req.auto_brush
    if auto_brush_mode == AutoBrushMode.OFF:
        auto_brush_actions: list[BrushAction] = []
        auto_covered = np.zeros((req.rows, req.cols), dtype=bool)
        auto_pixels = 0
    else:
        candidates = _auto_brush_candidate_sizes(
            _nearest_brush_size(req.max_auto_brush),
            req.step,
            auto_brush_mode,
        )
        pixel_index_arr = np.frombuffer(
            pixel_indices,
            dtype=np.uint8,
        ).reshape((req.rows, req.cols))
        auto_brush_actions, auto_covered, auto_stats = _plan_auto_brush_actions(
            pixel_index_arr,
            len(req.palette),
            skip_indices,
            candidates,
            req.step,
            auto_brush_mode,
        )
        auto_pixels = auto_stats.auto_brush_pixels

    residual_skip_indices = set(skip_indices)
    if auto_brush_actions:
        residual_skip_indices.add(-1)

    rle_rows: list[list[tuple[int, int, int]]] = []
    layered_strokes_by_colour: list[list[list[GridRun]]] = []
    skipped_white_runs = 0
    skipped_auto_runs = 0
    overpaint_saved_runs = 0
    max_overpaint_layers = 0
    merge_tiny_runs = req.merge_tiny_runs and not req.use_dither
    smart_underpaint = req.batch_colours and req.smart_underpaint
    raw_runs_buf: list[tuple[int, int, int]] = []
    segment_buf: list[PaletteRun] = []
    pixel_rows = memoryview(pixel_indices)

    def _ensure_stroke_layer(layer_idx: int) -> None:
        while len(layered_strokes_by_colour) <= layer_idx:
            layered_strokes_by_colour.append([[] for _ in req.palette])

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

    for gy in range(req.rows):
        row = pixel_rows[gy * req.cols:(gy + 1) * req.cols]
        covered_row = auto_covered[gy] if auto_brush_actions else None
        raw_runs_buf.clear()
        cur_idx = -1 if covered_row is not None and covered_row[0] else row[0]
        run_start = 0

        for gx in range(1, req.cols):
            idx = -1 if covered_row is not None and covered_row[gx] else row[gx]
            if idx != cur_idx:
                raw_runs_buf.append((cur_idx, run_start, gx - 1))
                cur_idx, run_start = idx, gx
        raw_runs_buf.append((cur_idx, run_start, req.cols - 1))
        raw_runs = (
            _merge_tiny_noise(raw_runs_buf, residual_skip_indices)
            if merge_tiny_runs else raw_runs_buf
        )

        if req.batch_colours:
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

    line_optimizer_possible = (
        req.batch_colours and req.step == 1 and req.brush_px == 1
    )
    line_optimizer_enabled = (
        line_optimizer_possible
        and req.planner != PlannerMode.EXACT
        and (req.use_dither or not smart_underpaint or overpaint_saved_runs < 1024)
    )
    if req.planner == PlannerMode.EXACT:
        line_optimizer_enabled = line_optimizer_possible
    line_optimizer_fast = (
        line_optimizer_possible
        and (
            req.planner == PlannerMode.FAST
            or (
                req.planner == PlannerMode.AUTO
                and (index_result.fast_used or req.use_dither)
            )
        )
    )
    line_optimizer_diagonal = req.use_dither
    line_optimizer_paths = req.use_dither
    fill_accel_enabled = (
        req.paint_fill_accel
        and max_speed_planner
        and req.fill_ready
        and line_optimizer_possible
    )

    total_units = 0
    fill_actions_used = 0
    line_opt_before = 0
    line_opt_after = 0
    detail_colour_sequence: list[int] = []
    detail_colours: set[int] = set()

    if req.batch_colours:
        draw_layers: list[tuple[str, list[tuple[int, list[GridAction]]]]] = []

        def _make_line_plan(
                strokes_by_colour: list[list[GridRun]],
                optimize_lines: bool) -> list[tuple[int, list[GridAction]]]:
            nonlocal fill_actions_used, line_opt_before, line_opt_after
            plan: list[tuple[int, list[GridAction]]] = []
            for colour_idx, runs in enumerate(strokes_by_colour):
                if not runs:
                    continue

                original_count = len(runs)
                fill_actions: list[GridAction] = []
                plan_runs = runs
                if fill_accel_enabled:
                    plan_runs, fill_actions = _extract_rect_fill_actions(runs)
                    fill_actions_used += len(fill_actions)

                horizontal = _as_line_strokes(plan_runs)
                strokes: list[GridAction] = horizontal
                if optimize_lines:
                    if line_optimizer_fast:
                        optimized = _optimize_grid_strokes_fast(
                            plan_runs,
                            req.cols,
                            req.rows,
                        )
                    else:
                        optimized = _optimize_grid_strokes(
                            plan_runs,
                            req.cols,
                            req.rows,
                            diagonal=line_optimizer_diagonal,
                            paths=line_optimizer_paths,
                        )
                    if _action_event_cost(optimized) < _action_event_cost(horizontal):
                        strokes = optimized
                    line_opt_before += original_count
                    line_opt_after += sum(
                        _planned_action_count(action) for action in strokes
                    )

                if fill_actions:
                    strokes = fill_actions + strokes
                plan.append((colour_idx, strokes))
            return plan

        for layer_idx, strokes_by_colour in enumerate(layered_strokes_by_colour):
            layer_plan = _make_line_plan(
                strokes_by_colour,
                line_optimizer_enabled,
            )
            if not layer_plan:
                continue
            layer_plan.sort(key=lambda item: len(item[1]), reverse=True)
            layer_name = (
                f"overpaint {layer_idx + 1}/{max_overpaint_layers}"
                if smart_underpaint and max_overpaint_layers > 1 else "detail"
            )
            draw_layers.append((layer_name, layer_plan))

        for _, layer_plan in draw_layers:
            for colour_idx, actions in layer_plan:
                detail_colours.add(colour_idx)
                detail_colour_sequence.append(colour_idx)
                total_units += sum(_planned_action_count(action) for action in actions)
    else:
        for runs in rle_rows:
            for colour_idx, _, _ in runs:
                detail_colours.add(colour_idx)
                detail_colour_sequence.append(colour_idx)
                total_units += 1

    auto_colour_sequence: list[int] = []
    auto_colours: set[int] = set()
    brush_size_changes = 0
    if auto_brush_actions:
        by_size_colour: dict[int, dict[int, list[BrushAction]]] = {}
        for action in auto_brush_actions:
            by_size_colour.setdefault(action.brush_px, {}).setdefault(
                action.colour_idx,
                [],
            ).append(action)
        for size in sorted(by_size_colour, reverse=True):
            brush_size_changes += 1
            colour_plan = sorted(
                by_size_colour[size].items(),
                key=lambda item: len(item[1]),
                reverse=True,
            )
            for colour_idx, actions in colour_plan:
                auto_colours.add(colour_idx)
                auto_colour_sequence.append(colour_idx)
                total_units += len(actions)
        brush_size_changes += 1

    colour_changes = (
        len(auto_colour_sequence)
        + _count_colour_changes(detail_colour_sequence)
    )
    used_colours = len(detail_colours | auto_colours)
    stroke_units = max(0, total_units - fill_actions_used)
    prep_seconds = time.monotonic() - t0

    palette_pick_s = _click_seconds(req.pal_delay_s, req)
    if not req.skip_pencil_rearm:
        palette_pick_s += _click_seconds(req.pencil_rearm_s, req)
    brush_post_s = max(0.02, req.pal_delay_s)
    brush_size_s = 3 * _click_seconds(brush_post_s, req)
    fill_action_s = (
        2 * _click_seconds(req.pal_delay_s, req)
        + _click_seconds(req.pencil_rearm_s, req)
    )
    start_overhead_s = (
        req.focus_delay_s
        + _click_seconds(0.01, req)
        + (0.0 if req.hide_ui_while_drawing else 0.1)
    )
    seconds = (
        prep_seconds
        + start_overhead_s
        + stroke_units * _stroke_seconds(req)
        + colour_changes * palette_pick_s
        + brush_size_changes * brush_size_s
        + fill_actions_used * fill_action_s
    )

    mode = "smart" if smart_underpaint else ("batched" if req.batch_colours else "row")
    if effective_dither == DitherPreset.CLEAN or len(active_indices) < len(req.palette):
        mode += "+gray" if effective_dither == DitherPreset.CLEAN else "+limited"
    if req.use_dither:
        mode += "+" + effective_dither.value.lower().split()[0]
    mode += f"+{req.planner.value.lower()}"
    if auto_brush_actions:
        mode += f"+brush{req.max_auto_brush}"
    if fill_accel_enabled:
        mode += "+fill"

    return EstimateResult(
        seconds=seconds,
        prep_seconds=prep_seconds,
        total_units=total_units,
        stroke_units=stroke_units,
        colour_changes=colour_changes,
        brush_size_changes=brush_size_changes,
        fill_actions=fill_actions_used,
        auto_actions=len(auto_brush_actions),
        auto_pixels=auto_pixels,
        skipped_white_runs=skipped_white_runs,
        skipped_auto_runs=skipped_auto_runs,
        used_colours=used_colours,
        line_opt_before=line_opt_before,
        line_opt_after=line_opt_after,
        overpaint_saved_runs=overpaint_saved_runs,
        mode=mode,
        fast_dither_used=index_result.fast_used,
    )
