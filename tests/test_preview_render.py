"""Tests for the Creo-free plan preview rasterizer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from creo_mcp.geometry_dsl import build_geometry_program
from creo_mcp.preview_render import render_shape_preview


class PreviewRenderTests(unittest.TestCase):
    def test_renders_pocketed_block_with_visible_interior(self) -> None:
        outcome = build_geometry_program(
            {
                "op": "difference",
                "children": [
                    {"op": "box", "length_mm": 200, "width_mm": 80, "height_mm": 30},
                    {
                        "op": "box",
                        "length_mm": 40,
                        "width_mm": 40,
                        "height_mm": 60,
                        "at": {"x_mm": 80, "y_mm": 20, "z_mm": 10},
                    },
                ],
            }
        )
        with tempfile.TemporaryDirectory() as raw:
            path = render_shape_preview(
                outcome.shape, Path(raw) / "preview.png", "TEST"
            )
            self.assertTrue(path.is_file())
            self.assertGreater(path.stat().st_size, 4000)

            from PIL import Image

            image = Image.open(path)
            self.assertEqual(image.size[0], image.size[1] + 672)
            # Painter shading must produce multiple tones (not a flat fill).
            tones = set(image.convert("L").getdata())
            self.assertGreater(len(tones), 10)

    def test_empty_shape_rejected(self) -> None:
        from creo_mcp.errors import CreoMcpError

        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises((CreoMcpError, Exception)):
                render_shape_preview(None, Path(raw) / "p.png", "NONE")


if __name__ == "__main__":
    unittest.main()
