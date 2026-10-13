from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

class DitherPreset(Enum):
    SMART = "Smart ordered"
    PHOTO = "Photo exact"
    CLEAN = "Clean mono"
    FLAT = "Flat"


class PlannerMode(Enum):
    AUTO = "Auto"
    FAST = "Fast"
    EXACT = "Exact"


class AutoBrushMode(Enum):
    OFF = "Off"
    SAFE = "Safe regions"
    AGGRESSIVE = "Aggressive"


BRUSH_SIZE_STOPS = (
    tuple(range(1, 25))
    + tuple(range(26, 105, 2))
    + tuple(range(108, 173, 4))
    + tuple(range(180, 245, 8))
    + (248,)
)
BRUSH_LADDER_TEST_SIZES = (1, 8, 24, 48, 104, 172, 248)


def _nearest_brush_size(px: int) -> int:
    return min(BRUSH_SIZE_STOPS, key=lambda stop: (abs(stop - px), stop))


def _brush_stop_index(px: int) -> int:
    snapped = _nearest_brush_size(px)
    return BRUSH_SIZE_STOPS.index(snapped)


@dataclass(frozen=True)
class BrushSizeCalibration:
    size_button: tuple[int, int]
    slider_bottom: tuple[int, int]
    slider_top: tuple[int, int]
    min_px: int = 1
    max_px: int = 248

    def slider_pos_for_size(self, px: int) -> tuple[int, int]:
        snapped = _nearest_brush_size(px)
        denom = max(1, len(BRUSH_SIZE_STOPS) - 1)
        t = _brush_stop_index(snapped) / denom
        bx, by = self.slider_bottom
        tx, ty = self.slider_top
        return (
            int(round(bx + (tx - bx) * t)),
            int(round(by + (ty - by) * t)),
        )


@dataclass(frozen=True)
class DrawSettings:
    dither: DitherPreset
    planner: PlannerMode
    auto_brush: AutoBrushMode
    max_auto_brush: int
    base_brush: int
    step: int
    grayscale_only: bool


@dataclass(frozen=True)
class BrushAction:
    brush_px: int
    colour_idx: int
    gy: int
    gx_start: int
    gx_end: int
    cover_cells: int


@dataclass
class IndexedImageResult:
    pixel_indices: bytes
    dither: DitherPreset
    fast_used: bool = False
    stats: dict[str, float | int | bool | str] = field(default_factory=dict)


@dataclass
class PlanStats:
    auto_brush_actions: int = 0
    auto_brush_pixels: int = 0
    auto_brush_sizes: list[int] = field(default_factory=list)
    planner_mode: str = PlannerMode.AUTO.value
