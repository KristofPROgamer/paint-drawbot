from __future__ import annotations

import unittest

from PIL import Image

from paint_drawbot.image_indexing import _index_image_data
from paint_drawbot.models import DitherPreset, PlannerMode


class ImageIndexingTests(unittest.TestCase):
    def test_flat_indexing_uses_nearest_palette_entries(self) -> None:
        image = Image.new("RGB", (2, 2))
        image.putdata([
            (0, 0, 0),
            (255, 255, 255),
            (10, 10, 10),
            (240, 240, 240),
        ])

        result = _index_image_data(
            image,
            [(0, 0, 0), (255, 255, 255)],
            DitherPreset.FLAT,
            PlannerMode.AUTO,
        )

        self.assertEqual(result.pixel_indices, bytes([0, 1, 0, 1]))
        self.assertFalse(result.fast_used)
        self.assertEqual(result.stats["mode"], DitherPreset.FLAT.value)

    def test_all_dither_modes_return_one_index_per_pixel(self) -> None:
        image = Image.linear_gradient("L").resize((8, 8)).convert("RGB")
        palette = [(0, 0, 0), (128, 128, 128), (255, 255, 255)]

        for preset in DitherPreset:
            with self.subTest(preset=preset):
                result = _index_image_data(
                    image,
                    palette,
                    preset,
                    PlannerMode.AUTO,
                )
                self.assertEqual(len(result.pixel_indices), image.width * image.height)


if __name__ == "__main__":
    unittest.main()
