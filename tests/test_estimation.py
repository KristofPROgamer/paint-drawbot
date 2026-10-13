from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PIL import Image

from paint_drawbot.estimation import EstimateRequest, estimate_draw
from paint_drawbot.models import AutoBrushMode, DitherPreset, PlannerMode


class EstimationTests(unittest.TestCase):
    def test_estimate_counts_actual_row_runs_after_white_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "source.png"
            image = Image.new("RGB", (4, 2))
            image.putdata([
                (0, 0, 0),
                (0, 0, 0),
                (255, 255, 255),
                (255, 255, 255),
                (255, 255, 255),
                (0, 0, 0),
                (0, 0, 0),
                (255, 255, 255),
            ])
            image.save(image_path)

            result = estimate_draw(
                EstimateRequest(
                    image_path=str(image_path),
                    cols=4,
                    rows=2,
                    step=1,
                    brush_px=1,
                    palette=(
                        ("Black", (0, 0, 0)),
                        ("White", (255, 255, 255)),
                    ),
                    active_palette_indices=(0, 1),
                    dither=DitherPreset.FLAT,
                    planner=PlannerMode.AUTO,
                    use_dither=False,
                    batch_colours=False,
                    skip_white=True,
                    smart_underpaint=False,
                    merge_tiny_runs=False,
                    paint_fill_accel=False,
                    fill_ready=False,
                    auto_brush=AutoBrushMode.OFF,
                    max_auto_brush=48,
                    hide_ui_while_drawing=True,
                    stroke_duration_s=0.0,
                    move_settle_s=0.0,
                    press_settle_s=0.0,
                    post_step_s=0.0,
                    release_settle_s=0.0,
                    min_stroke_s=0.0,
                    drag_steps=1,
                    click_pre_down_s=0.0,
                    click_pre_up_s=0.001,
                    pal_delay_s=0.004,
                    pencil_rearm_s=0.020,
                    skip_pencil_rearm=True,
                    focus_delay_s=0.060,
                )
            )

            self.assertEqual(result.total_units, 2)
            self.assertEqual(result.colour_changes, 1)
            self.assertEqual(result.used_colours, 1)
            self.assertGreater(result.skipped_white_runs, 0)
            self.assertGreater(result.seconds, result.prep_seconds)


if __name__ == "__main__":
    unittest.main()
