from __future__ import annotations

import unittest

from paint_drawbot.models import BrushSizeCalibration, _nearest_brush_size


class ModelTests(unittest.TestCase):
    def test_nearest_brush_size_snaps_to_supported_stop(self) -> None:
        self.assertEqual(_nearest_brush_size(25), 24)
        self.assertEqual(_nearest_brush_size(27), 26)
        self.assertEqual(_nearest_brush_size(249), 248)

    def test_brush_slider_uses_bottom_and_top_stops(self) -> None:
        calibration = BrushSizeCalibration(
            size_button=(10, 10),
            slider_bottom=(100, 200),
            slider_top=(100, 20),
        )

        self.assertEqual(calibration.slider_pos_for_size(1), (100, 200))
        self.assertEqual(calibration.slider_pos_for_size(248), (100, 20))


if __name__ == "__main__":
    unittest.main()
