from __future__ import annotations

import numpy as np

from .models import (
    AutoBrushMode,
    BrushAction,
    BRUSH_SIZE_STOPS,
    PlanStats,
    _nearest_brush_size,
)

def _coalesce_runs(runs: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    merged: list[tuple[int, int, int]] = []
    for idx, start, end in runs:
        if merged and merged[-1][0] == idx and merged[-1][2] + 1 >= start:
            prev_idx, prev_start, _ = merged[-1]
            merged[-1] = (prev_idx, prev_start, end)
        else:
            merged.append((idx, start, end))
    return merged


def _merge_tiny_noise(
        runs: list[tuple[int, int, int]],
        skip_indices: set[int]) -> list[tuple[int, int, int]]:
    if len(runs) < 3:
        return runs

    smoothed = list(runs)
    for i in range(1, len(runs) - 1):
        idx, start, end = runs[i]
        if idx in skip_indices or end > start:
            continue
        left_idx = runs[i - 1][0]
        right_idx = runs[i + 1][0]
        if left_idx == right_idx:
            smoothed[i] = (left_idx, start, end)

    return _coalesce_runs(smoothed)


PaletteRun = tuple[int, int, int]              # colour_idx, gx_start, gx_end
GridRun = tuple[int, int, int]                 # gy, gx_start, gx_end
GridLine = tuple[int, int, int, int]           # gy1, gx1, gy2, gx2
GridPath = tuple[tuple[int, int], ...]         # (gy, gx), ...
GridFill = tuple[str, int, int, GridPath]      # "fill", seed_gy, seed_gx, boundary
GridAction = GridLine | GridPath | GridFill
_GRID_PATH_VERTEX_LIMIT = 1024
_OVERPAINT_MAX_DEPTH = 8
_OVERPAINT_CANDIDATES = 4
_FILL_MIN_SIDE = 8
_FILL_MIN_AREA = 256
_STROKE_SETUP_COST = 6
_FILL_TOOL_COST = 14
_MAX_PATH_EVENT_FACTOR = 2.75


def _overpaint_cost(layers: list[list[PaletteRun]]) -> int:
    return sum(len(layer) for layer in layers)


def _merge_overpaint_layers(
        target: list[list[PaletteRun]],
        source: list[list[PaletteRun]],
        offset: int = 0) -> None:
    while len(target) < offset + len(source):
        target.append([])
    for idx, layer in enumerate(source):
        target[offset + idx].extend(layer)


def _overpaint_segment_layers(
        segment: list[PaletteRun],
        depth: int = 0) -> list[list[PaletteRun]]:
    """
    Build exact horizontal paint layers for one non-skipped row segment.

    A longer underpaint span may temporarily cover other colours, then later
    layers restore those inner runs. This preserves the final pixels while
    replacing repeated patterns such as A B C B A with fewer strokes.
    """
    def _solve(start: int, end: int, cur_depth: int) -> list[list[PaletteRun]]:
        count = end - start
        if count <= 0:
            return []
        if count < 3 or cur_depth >= _OVERPAINT_MAX_DEPTH:
            return [[segment[i] for i in range(start, end)]]

        positions: dict[int, list[int]] = {}
        pixel_counts: dict[int, int] = {}
        for idx in range(start, end):
            colour_idx, gx_start, gx_end = segment[idx]
            positions.setdefault(colour_idx, []).append(idx)
            pixel_counts[colour_idx] = (
                pixel_counts.get(colour_idx, 0) + gx_end - gx_start + 1
            )

        candidates = [
            colour_idx for colour_idx, pos in positions.items()
            if len(pos) >= 2
        ]
        if not candidates:
            return [[segment[i] for i in range(start, end)]]

        candidates.sort(
            key=lambda colour_idx: (
                len(positions[colour_idx]), pixel_counts[colour_idx]
            ),
            reverse=True,
        )

        best_layers: list[list[PaletteRun]] | None = None
        best_cost = count

        for colour_idx in candidates[:_OVERPAINT_CANDIDATES]:
            pos = positions[colour_idx]
            first = pos[0]
            last = pos[-1]
            layers: list[list[PaletteRun]] = [[(
                colour_idx, segment[first][1], segment[last][2]
            )]]

            # Recurse on index ranges to avoid allocating sliced run lists.
            if first > start:
                _merge_overpaint_layers(
                    layers,
                    _solve(start, first, cur_depth + 1),
                    0,
                )

            prev = first
            for nxt in pos[1:]:
                if nxt > prev + 1:
                    _merge_overpaint_layers(
                        layers,
                        _solve(prev + 1, nxt, cur_depth + 1),
                        1,
                    )
                prev = nxt

            if last + 1 < end:
                _merge_overpaint_layers(
                    layers,
                    _solve(last + 1, end, cur_depth + 1),
                    0,
                )

            cost = _overpaint_cost(layers)
            if cost < best_cost:
                best_layers = layers
                best_cost = cost

        return (
            best_layers if best_layers is not None
            else [[segment[i] for i in range(start, end)]]
        )

    return _solve(0, len(segment), depth)


def _as_line_strokes(runs: list[GridRun]) -> list[GridLine]:
    return [(gy, gx_start, gy, gx_end) for gy, gx_start, gx_end in runs]


def _is_grid_path(action: GridAction) -> bool:
    return bool(action) and isinstance(action[0], tuple)


def _is_grid_fill(action: GridAction) -> bool:
    return bool(action) and action[0] == "fill"


def _raw_action_event_count(actions: list[GridAction]) -> int:
    total = 0
    for action in actions:
        if _is_grid_fill(action):
            _, _, _, boundary = action
            total += len(boundary) + 4
        elif _is_grid_path(action):
            total += len(action) + 1
        else:
            gy1, gx1, gy2, gx2 = action
            total += 2 if gy1 == gy2 and gx1 == gx2 else 3
    return total


def _action_event_cost(actions: list[GridAction]) -> int:
    """
    Estimate draw-time cost, not preprocessing cost.

    Raw SendInput events are cheap compared with starting/stopping strokes and
    switching tools. This weighted model intentionally accepts a slightly longer
    continuous path when it replaces many separate Paint strokes.
    """
    total = 0
    for action in actions:
        if _is_grid_fill(action):
            _, _, _, boundary = action
            total += _STROKE_SETUP_COST + len(boundary) + 1 + _FILL_TOOL_COST
        elif _is_grid_path(action):
            total += _STROKE_SETUP_COST + len(action) + 1
        else:
            gy1, gx1, gy2, gx2 = action
            raw = 2 if gy1 == gy2 and gx1 == gx2 else 3
            total += _STROKE_SETUP_COST + raw
    return total


def _actions_are_cheaper(
        candidate: list[GridAction],
        baseline: list[GridAction],
        max_path_event_factor: float = _MAX_PATH_EVENT_FACTOR) -> bool:
    if not candidate:
        return False
    if _action_event_cost(candidate) >= _action_event_cost(baseline):
        return False
    raw_candidate = _raw_action_event_count(candidate)
    raw_baseline = max(1, _raw_action_event_count(baseline))
    return raw_candidate <= max(raw_baseline + 32, int(raw_baseline * max_path_event_factor))


def _planned_action_count(action: GridAction) -> int:
    return 2 if _is_grid_fill(action) else 1


def _split_grid_path(points: list[tuple[int, int]]) -> list[GridPath]:
    if len(points) <= _GRID_PATH_VERTEX_LIMIT:
        return [tuple(points)]

    paths: list[GridPath] = []
    start = 0
    while start < len(points):
        end = min(len(points), start + _GRID_PATH_VERTEX_LIMIT)
        chunk = points[start:end] if start == 0 else points[start - 1:end]
        paths.append(tuple(chunk))
        start = end
    return paths


def _component_walk_path(
        component: set[int], cols: int, rows: int) -> list[tuple[int, int]]:
    def _point(cell: int) -> tuple[int, int]:
        gy, gx = divmod(cell, cols)
        return gy, gx

    def _neighbours(cell: int) -> list[int]:
        gy, gx = divmod(cell, cols)
        out: list[int] = []
        for dy, dx in (
                (0, 1), (1, 0), (0, -1), (-1, 0),
                (1, 1), (1, -1), (-1, 1), (-1, -1)):
            ngy, ngx = gy + dy, gx + dx
            if not (0 <= ngx < cols and 0 <= ngy < rows):
                continue
            n = ngy * cols + ngx
            if n in component:
                out.append(n)
        return out

    start = min(component)
    visited = {start}
    path = [_point(start)]
    stack: list[tuple[int, list[int], int]] = [(start, _neighbours(start), 0)]

    while stack:
        cell, neighbours, i = stack[-1]
        while i < len(neighbours) and neighbours[i] in visited:
            i += 1
        if i >= len(neighbours):
            stack.pop()
            if stack:
                path.append(_point(stack[-1][0]))
            continue

        nxt = neighbours[i]
        stack[-1] = (cell, neighbours, i + 1)
        visited.add(nxt)
        path.append(_point(nxt))
        stack.append((nxt, _neighbours(nxt), 0))

    return path


def _grid_components(
        cells: set[int],
        cols: int,
        rows: int,
        diagonal: bool = True) -> list[set[int]]:
    remaining = set(cells)
    components: list[set[int]] = []
    neighbour_offsets = [(0, 1), (1, 0), (0, -1), (-1, 0)]
    if diagonal:
        neighbour_offsets.extend([(1, 1), (1, -1), (-1, 1), (-1, -1)])
    while remaining:
        start = remaining.pop()
        component = {start}
        stack = [start]
        while stack:
            cell = stack.pop()
            gy, gx = divmod(cell, cols)
            for dy, dx in neighbour_offsets:
                ngy, ngx = gy + dy, gx + dx
                if not (0 <= ngx < cols and 0 <= ngy < rows):
                    continue
                n = ngy * cols + ngx
                if n in remaining:
                    remaining.remove(n)
                    component.add(n)
                    stack.append(n)
        components.append(component)
    return components


def _optimize_cells_as_lines(
        source_cells: set[int],
        cols: int,
        rows: int,
        diagonal: bool = True) -> list[GridLine]:
    cells = set(source_cells)
    if not cells:
        return []

    directions = [(1, 0), (0, 1)]
    if diagonal:
        directions.extend([(1, 1), (1, -1)])

    def _inside(gx: int, gy: int) -> bool:
        return 0 <= gx < cols and 0 <= gy < rows

    strokes: list[GridLine] = []
    while cells:
        first = next(iter(cells))
        gy, gx = divmod(first, cols)

        best_len = 1
        best = (gx, gy, gx, gy, 1, 0)
        for dx, dy in directions:
            sx, sy = gx, gy
            while True:
                nx, ny = sx - dx, sy - dy
                if not _inside(nx, ny) or ny * cols + nx not in cells:
                    break
                sx, sy = nx, ny

            ex, ey = gx, gy
            while True:
                nx, ny = ex + dx, ey + dy
                if not _inside(nx, ny) or ny * cols + nx not in cells:
                    break
                ex, ey = nx, ny

            length = max(abs(ex - sx), abs(ey - sy)) + 1
            if length > best_len:
                best_len = length
                best = (sx, sy, ex, ey, dx, dy)

        sx, sy, ex, ey, dx, dy = best
        strokes.append((sy, sx, ey, ex))

        cx, cy = sx, sy
        for _ in range(best_len):
            cells.discard(cy * cols + cx)
            cx += dx
            cy += dy

    return strokes


def _find_longest_grid_line(
        cells: set[int],
        cols: int,
        rows: int,
        directions: list[tuple[int, int]]) -> tuple[int, int, int, int, int, int, int]:
    best_len = 1
    best = next(iter(cells))
    best_gy, best_gx = divmod(best, cols)
    best_line = (best_gx, best_gy, best_gx, best_gy, 1, 0, best_len)

    for cell in cells:
        gy, gx = divmod(cell, cols)
        for dx, dy in directions:
            prev_x = gx - dx
            prev_y = gy - dy
            if 0 <= prev_x < cols and 0 <= prev_y < rows:
                if prev_y * cols + prev_x in cells:
                    continue

            ex, ey = gx, gy
            length = 1
            while True:
                nx, ny = ex + dx, ey + dy
                if not (0 <= nx < cols and 0 <= ny < rows):
                    break
                if ny * cols + nx not in cells:
                    break
                ex, ey = nx, ny
                length += 1

            if length > best_len:
                best_len = length
                best_line = (gx, gy, ex, ey, dx, dy, length)

    return best_line


def _optimize_cells_as_global_lines(
        source_cells: set[int],
        cols: int,
        rows: int,
        diagonal: bool = True) -> list[GridLine]:
    cells = set(source_cells)
    if not cells:
        return []

    directions = [(1, 0), (0, 1)]
    if diagonal:
        directions.extend([(1, 1), (1, -1)])

    strokes: list[GridLine] = []
    while cells:
        sx, sy, ex, ey, dx, dy, length = _find_longest_grid_line(
            cells, cols, rows, directions
        )
        if length < 3:
            strokes.extend(_optimize_cells_as_lines(cells, cols, rows, diagonal))
            break

        strokes.append((sy, sx, ey, ex))
        cx, cy = sx, sy
        for _ in range(length):
            cells.discard(cy * cols + cx)
            cx += dx
            cy += dy

    return strokes


def _optimize_grid_strokes(
        runs: list[GridRun],
        cols: int,
        rows: int,
        diagonal: bool = True,
        paths: bool = True) -> list[GridAction]:
    """
    Cover the same 1px grid cells with cheaper exact actions.

    First we try straight horizontal/vertical/diagonal strokes. For grainy
    connected clusters that do not form long lines, we may instead trace a
    one-colour path through the component. The path can revisit pixels, but
    every touched pixel is already supposed to be that colour.
    """
    if len(runs) < 2:
        return _as_line_strokes(runs)

    cells: set[int] = set()
    for gy, gx_start, gx_end in runs:
        base = gy * cols
        cells.update(range(base + gx_start, base + gx_end + 1))
    if not cells:
        return []

    actions: list[GridAction] = []
    for component in _grid_components(cells, cols, rows, diagonal):
        line_actions = _optimize_cells_as_global_lines(
            component, cols, rows, diagonal
        )
        path_actions: list[GridAction] = []
        if paths and len(component) > 1:
            path_actions = list(_split_grid_path(
                _component_walk_path(component, cols, rows)
            ))

        if path_actions and _actions_are_cheaper(path_actions, line_actions):
            actions.extend(path_actions)
        else:
            actions.extend(line_actions)

    return actions


def _runs_to_mask(runs: list[GridRun], cols: int, rows: int) -> np.ndarray:
    mask = np.zeros((rows, cols), dtype=bool)
    for gy, gx_start, gx_end in runs:
        mask[gy, gx_start:gx_end + 1] = True
    return mask


def _row_mask_runs(mask: np.ndarray) -> list[list[list[int]]]:
    row_runs: list[list[list[int]]] = []
    for gy in range(mask.shape[0]):
        xs = np.flatnonzero(mask[gy])
        if xs.size == 0:
            row_runs.append([])
            continue
        gaps = np.flatnonzero(np.diff(xs) > 1)
        starts = np.concatenate((xs[:1], xs[gaps + 1]))
        ends = np.concatenate((xs[gaps], xs[-1:]))
        row_runs.append([[int(start), int(end), 0] for start, end in zip(starts, ends)])
    return row_runs


def _chain_to_grid_actions(chain: list[GridRun]) -> list[GridAction]:
    if len(chain) < 2:
        return _as_line_strokes(chain)

    def _clean_waypoints(values: list[int]) -> list[int]:
        cleaned: list[int] = []
        for value in values:
            if not cleaned or cleaned[-1] != value:
                cleaned.append(value)
        return cleaned

    def _row_cover_waypoints(
            gx_start: int,
            gx_end: int,
            entry_x: int,
            exit_x: int) -> list[int]:
        if gx_start == gx_end:
            return _clean_waypoints([gx_start, exit_x])

        candidates: list[list[int]] = []
        if min(entry_x, exit_x) <= gx_start and max(entry_x, exit_x) >= gx_end:
            candidates.append([exit_x])
        candidates.extend((
            [gx_start, gx_end, exit_x],
            [gx_end, gx_start, exit_x],
        ))

        def _score(path: list[int]) -> tuple[int, int]:
            cur = entry_x
            distance = 0
            for x in path:
                distance += abs(x - cur)
                cur = x
            return distance, len(_clean_waypoints(path))

        return _clean_waypoints(min(candidates, key=_score))

    def _best_exit_x(
            gx_start: int,
            gx_end: int,
            entry_x: int,
            next_run: GridRun | None) -> int:
        if next_run is None:
            options = [gx_start, gx_end]
        else:
            _, next_start, next_end = next_run
            overlap_start = max(gx_start, next_start)
            overlap_end = min(gx_end, next_end)
            options = [overlap_start, overlap_end]

        def _cost(exit_x: int) -> tuple[int, int]:
            path = _row_cover_waypoints(gx_start, gx_end, entry_x, exit_x)
            cur = entry_x
            distance = 0
            for x in path:
                distance += abs(x - cur)
                cur = x
            return distance, -abs(exit_x - entry_x)

        return min(options, key=_cost)

    points: list[tuple[int, int]] = []
    gy0, gx_start0, _ = chain[0]
    cur_x = gx_start0
    points.append((gy0, cur_x))

    for i, (gy, gx_start, gx_end) in enumerate(chain):
        if points[-1] != (gy, cur_x):
            points.append((gy, cur_x))

        next_run = chain[i + 1] if i + 1 < len(chain) else None
        exit_x = _best_exit_x(gx_start, gx_end, cur_x, next_run)
        for gx in _row_cover_waypoints(gx_start, gx_end, cur_x, exit_x):
            if points[-1] != (gy, gx):
                points.append((gy, gx))
        cur_x = exit_x
        if next_run is not None:
            next_gy = next_run[0]
            if points[-1] != (next_gy, cur_x):
                points.append((next_gy, cur_x))

    path_actions = list(_split_grid_path(points))
    line_actions = _as_line_strokes(chain)
    return (
        path_actions
        if _actions_are_cheaper(path_actions, line_actions)
        else line_actions
    )


def _action_start_end(action: GridAction) -> tuple[tuple[int, int], tuple[int, int]]:
    if _is_grid_fill(action):
        _, seed_gy, seed_gx, _ = action
        point = (seed_gy, seed_gx)
        return point, point
    if _is_grid_path(action):
        return action[0], action[-1]
    gy1, gx1, gy2, gx2 = action
    return (gy1, gx1), (gy2, gx2)


def _reverse_action(action: GridAction) -> GridAction:
    if _is_grid_fill(action):
        return action
    if _is_grid_path(action):
        return tuple(reversed(action))
    gy1, gx1, gy2, gx2 = action
    return gy2, gx2, gy1, gx1


def _order_actions_nearest(
        actions: list[GridAction],
        radius: int = 64) -> list[GridAction]:
    if len(actions) < 3:
        return actions

    remaining = list(actions)
    ordered: list[GridAction] = []
    cur = (0, 0)
    _ = radius
    while remaining:
        best_i = 0
        best_d = 1 << 60
        best_reverse = False
        for i, action in enumerate(remaining):
            start, end = _action_start_end(action)
            d = (start[0] - cur[0]) ** 2 + (start[1] - cur[1]) ** 2
            reverse = False
            if not _is_grid_fill(action):
                rev_d = (end[0] - cur[0]) ** 2 + (end[1] - cur[1]) ** 2
                if rev_d < d:
                    d = rev_d
                    reverse = True
            if d < best_d:
                best_i = i
                best_d = d
                best_reverse = reverse
                if d == 0:
                    break

        action = remaining.pop(best_i)
        if best_reverse:
            action = _reverse_action(action)
        ordered.append(action)
        _, cur = _action_start_end(action)

    return ordered


def _mask_serpentine_actions(mask: np.ndarray) -> list[GridAction]:
    rows, _ = mask.shape
    row_runs = _row_mask_runs(mask)
    actions: list[GridAction] = []

    for gy in range(rows):
        for run in row_runs[gy]:
            if run[2]:
                continue
            chain: list[GridRun] = []
            cur_gy = gy
            cur_run = run
            while True:
                gx_start, gx_end, _ = cur_run
                cur_run[2] = 1
                chain.append((cur_gy, gx_start, gx_end))
                if cur_gy + 1 >= rows:
                    break

                best_run: list[int] | None = None
                best_overlap = 0
                for next_run in row_runs[cur_gy + 1]:
                    if next_run[2]:
                        continue
                    overlap = min(gx_end, next_run[1]) - max(gx_start, next_run[0]) + 1
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best_run = next_run
                if best_run is None or best_overlap <= 0:
                    break
                cur_gy += 1
                cur_run = best_run

            actions.extend(_chain_to_grid_actions(chain))

    return _order_actions_nearest(actions)


def _optimize_grid_strokes_fast(
        runs: list[GridRun],
        cols: int,
        rows: int) -> list[GridAction]:
    if len(runs) < 2:
        return _as_line_strokes(runs)

    horizontal = _as_line_strokes(runs)
    mask = _runs_to_mask(runs, cols, rows)
    # PERF: NumPy row masks avoid Python set components for the max-speed path.
    serpentine = _mask_serpentine_actions(mask)
    return (
        serpentine
        if _actions_are_cheaper(serpentine, horizontal)
        else horizontal
    )


def _extract_rect_fill_actions(
        runs: list[GridRun]) -> tuple[list[GridRun], list[GridAction]]:
    if len(runs) < _FILL_MIN_SIDE:
        return runs, []

    groups: dict[tuple[int, int], list[list[int]]] = {}
    for gy, gx_start, gx_end in sorted(runs):
        group_runs = groups.setdefault((gx_start, gx_end), [])
        if group_runs and group_runs[-1][1] + 1 == gy:
            group_runs[-1][1] = gy
        else:
            group_runs.append([gy, gy])

    covered: set[GridRun] = set()
    fill_actions: list[GridAction] = []
    for (gx_start, gx_end), gy_ranges in groups.items():
        width = gx_end - gx_start + 1
        if width < _FILL_MIN_SIDE:
            continue
        for gy_start, gy_end in gy_ranges:
            height = gy_end - gy_start + 1
            area = width * height
            if height < _FILL_MIN_SIDE or area < _FILL_MIN_AREA:
                continue
            boundary: GridPath = (
                (gy_start, gx_start),
                (gy_start, gx_end),
                (gy_end, gx_end),
                (gy_end, gx_start),
                (gy_start, gx_start),
            )
            seed_gy = (gy_start + gy_end) // 2
            seed_gx = (gx_start + gx_end) // 2
            fill_action: GridFill = ("fill", seed_gy, seed_gx, boundary)
            old_cost = height * (_STROKE_SETUP_COST + 3)
            # PERF: Fill is used only for exact solid rectangles with a clear
            # 25% event-cost win, so the tool switch cannot make it slower.
            if _action_event_cost([fill_action]) > old_cost * 0.75:
                continue
            fill_actions.append(fill_action)
            for gy in range(gy_start, gy_end + 1):
                covered.add((gy, gx_start, gx_end))

    if not fill_actions:
        return runs, []

    remaining = [run for run in runs if run not in covered]
    return remaining, fill_actions


def _auto_brush_candidate_sizes(
        max_px: int,
        step: int,
        mode: AutoBrushMode) -> list[int]:
    if mode == AutoBrushMode.OFF:
        return []
    max_px = _nearest_brush_size(max_px)
    minimum = max(8, step * 4)
    if mode == AutoBrushMode.AGGRESSIVE:
        candidates = [
            px for px in BRUSH_SIZE_STOPS
            if minimum <= px <= max_px
        ]
    else:
        anchors = (248, 244, 220, 196, 172, 140, 104, 72, 48, 32, 24, 16, 12, 8)
        candidates = [
            _nearest_brush_size(px)
            for px in anchors
            if minimum <= px <= max_px
        ]
    return sorted(set(candidates), reverse=True)


def _erode_square_mask(mask: np.ndarray, window: int) -> np.ndarray:
    if window <= 1:
        return mask.copy()
    h, w = mask.shape
    out = np.zeros((h, w), dtype=bool)
    if h < window or w < window:
        return out

    integral = np.pad(
        mask.astype(np.int32).cumsum(axis=0).cumsum(axis=1),
        ((1, 0), (1, 0)),
    )
    sums = (
        integral[window:, window:]
        - integral[:-window, window:]
        - integral[window:, :-window]
        + integral[:-window, :-window]
    )
    good = sums == window * window
    off = window // 2
    out[off:off + good.shape[0], off:off + good.shape[1]] = good
    return out


def _brush_coverage_slices(
        shape: tuple[int, int],
        action: BrushAction) -> tuple[slice, slice]:
    cover = max(1, action.cover_cells)
    half = cover // 2
    extra = cover - half - 1
    y0 = max(0, action.gy - half)
    y1 = min(shape[0], action.gy + extra + 1)
    x0 = max(0, action.gx_start - half)
    x1 = min(shape[1], action.gx_end + extra + 1)
    return slice(y0, y1), slice(x0, x1)


def _count_new_brush_coverage(
        covered: np.ndarray,
        action: BrushAction) -> int:
    ys, xs = _brush_coverage_slices(covered.shape, action)
    return int(np.count_nonzero(~covered[ys, xs]))


def _mark_brush_coverage(
        covered: np.ndarray,
        action: BrushAction) -> None:
    ys, xs = _brush_coverage_slices(covered.shape, action)
    covered[ys, xs] = True


def _extract_safe_brush_runs(
        safe_centers: np.ndarray,
        brush_px: int,
        colour_idx: int,
        cover_cells: int,
        mode: AutoBrushMode) -> list[BrushAction]:
    actions: list[BrushAction] = []
    min_run = max(cover_cells, 6 if mode == AutoBrushMode.SAFE else 3)
    for gy in range(safe_centers.shape[0]):
        xs = np.flatnonzero(safe_centers[gy])
        if xs.size == 0:
            continue
        gaps = np.flatnonzero(np.diff(xs) > 1)
        starts = np.concatenate((xs[:1], xs[gaps + 1]))
        ends = np.concatenate((xs[gaps], xs[-1:]))
        for start, end in zip(starts, ends):
            run_len = int(end - start + 1)
            if run_len < min_run:
                continue
            actions.append(
                BrushAction(
                    brush_px=brush_px,
                    colour_idx=colour_idx,
                    gy=gy,
                    gx_start=int(start),
                    gx_end=int(end),
                    cover_cells=cover_cells,
                )
            )
    return actions


def _plan_auto_brush_actions(
        pixel_indices: np.ndarray,
        palette_count: int,
        skip_indices: set[int],
        candidate_sizes: list[int],
        step: int,
        mode: AutoBrushMode) -> tuple[list[BrushAction], np.ndarray, PlanStats]:
    covered = np.zeros(pixel_indices.shape, dtype=bool)
    stats = PlanStats(planner_mode=mode.value)
    if mode == AutoBrushMode.OFF or not candidate_sizes:
        return [], covered, stats

    all_actions: list[BrushAction] = []
    safe_pad = 2 if mode == AutoBrushMode.SAFE else 1
    cover_shrink = 3 if mode == AutoBrushMode.SAFE else 1
    min_area = 96 if mode == AutoBrushMode.SAFE else 32

    for colour_idx in range(palette_count):
        if colour_idx in skip_indices:
            continue
        colour_mask = pixel_indices == colour_idx
        if int(np.count_nonzero(colour_mask)) < min_area:
            continue

        colour_covered = np.zeros(pixel_indices.shape, dtype=bool)
        for brush_px in candidate_sizes:
            cells = max(1, int(round(brush_px / max(1, step))))
            if cells < 4:
                continue
            safe_window = cells + safe_pad
            cover_cells = max(1, cells - cover_shrink)
            remaining = colour_mask & ~colour_covered
            if int(np.count_nonzero(remaining)) < max(min_area, safe_window * safe_window):
                continue

            safe_centers = _erode_square_mask(remaining, safe_window)
            if not np.any(safe_centers):
                continue

            actions = _extract_safe_brush_runs(
                safe_centers, brush_px, colour_idx, cover_cells, mode
            )
            if not actions:
                continue

            accepted = 0
            for action in actions:
                new_pixels = _count_new_brush_coverage(colour_covered, action)
                if new_pixels <= 0:
                    continue
                # One large stroke should replace a meaningful chunk of the
                # small-stroke residual plan after accounting for size changes.
                if new_pixels < max(cover_cells * 2, 32):
                    continue
                _mark_brush_coverage(colour_covered, action)
                all_actions.append(action)
                accepted += 1
                stats.auto_brush_pixels += new_pixels

            if accepted:
                stats.auto_brush_sizes.append(brush_px)

        covered |= colour_covered

    stats.auto_brush_actions = len(all_actions)
    stats.auto_brush_sizes = sorted(set(stats.auto_brush_sizes), reverse=True)
    return all_actions, covered, stats
