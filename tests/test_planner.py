from __future__ import annotations

import unittest

from paint_drawbot.planner import (
    GridAction,
    GridLine,
    _is_grid_fill,
    _is_grid_path,
    _merge_tiny_noise,
    _optimize_grid_strokes,
    _optimize_grid_strokes_fast,
)


def _line_cells(line: GridLine) -> set[tuple[int, int]]:
    gy1, gx1, gy2, gx2 = line
    return _segment_cells((gy1, gx1), (gy2, gx2))


def _segment_cells(
        start: tuple[int, int],
        end: tuple[int, int]) -> set[tuple[int, int]]:
    gy1, gx1 = start
    gy2, gx2 = end
    dy = 0 if gy2 == gy1 else (1 if gy2 > gy1 else -1)
    dx = 0 if gx2 == gx1 else (1 if gx2 > gx1 else -1)
    length = max(abs(gy2 - gy1), abs(gx2 - gx1)) + 1
    return {(gy1 + dy * step, gx1 + dx * step) for step in range(length)}


def _covered_cells(actions: list[GridAction]) -> set[tuple[int, int]]:
    covered: set[tuple[int, int]] = set()
    for action in actions:
        if _is_grid_fill(action):
            raise AssertionError("fill actions are not expected in these tests")
        if _is_grid_path(action):
            for start, end in zip(action, action[1:]):
                covered.update(_segment_cells(start, end))
        else:
            covered.update(_line_cells(action))
    return covered


class PlannerTests(unittest.TestCase):
    def test_merge_tiny_noise_replaces_single_pixel_between_same_colour(self) -> None:
        runs = [(1, 0, 3), (2, 4, 4), (1, 5, 9)]

        self.assertEqual(_merge_tiny_noise(runs, set()), [(1, 0, 9)])

    def test_optimized_strokes_cover_same_cells(self) -> None:
        runs = [
            (0, 0, 3),
            (1, 1, 3),
            (2, 2, 3),
            (3, 3, 3),
        ]
        expected = {
            (gy, gx)
            for gy, gx_start, gx_end in runs
            for gx in range(gx_start, gx_end + 1)
        }

        exact = _optimize_grid_strokes(runs, cols=4, rows=4)
        fast = _optimize_grid_strokes_fast(runs, cols=4, rows=4)

        self.assertEqual(_covered_cells(exact), expected)
        self.assertEqual(_covered_cells(fast), expected)


if __name__ == "__main__":
    unittest.main()
