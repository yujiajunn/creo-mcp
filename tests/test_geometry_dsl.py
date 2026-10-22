"""Tests for the constrained JSON geometry program (creo_build_geometry)."""

from __future__ import annotations

import math
import unittest

from creo_mcp.errors import CreoMcpError
from creo_mcp.geometry_dsl import BuildGeometryRequest, build_geometry_program


class GeometryDslTests(unittest.TestCase):
    def test_box_with_cylindrical_cut(self) -> None:
        outcome = build_geometry_program(
            {
                "op": "difference",
                "children": [
                    {"op": "box", "length_mm": 100, "width_mm": 60, "height_mm": 20},
                    {
                        "op": "cylinder",
                        "radius_mm": 10,
                        "height_mm": 40,
                        "axis": "z",
                        "at": {"x_mm": 50, "y_mm": 30, "z_mm": -10},
                    },
                ],
            }
        )
        expected = 100 * 60 * 20 - math.pi * 10**2 * 20
        self.assertAlmostEqual(outcome.volume_mm3, expected, delta=1.0)
        self.assertEqual(outcome.measured_extent_mm, (100.0, 60.0, 20.0))
        self.assertEqual(outcome.node_count, 3)

    def test_sphere_linear_pattern_fused_to_plate(self) -> None:
        outcome = build_geometry_program(
            {
                "op": "union",
                "children": [
                    {"op": "box", "length_mm": 120, "width_mm": 40, "height_mm": 5},
                    {
                        "op": "linear_pattern",
                        "count": 3,
                        "step": {"x_mm": 40},
                        "child": {
                            "op": "sphere",
                            "radius_mm": 6,
                            "at": {"x_mm": 20, "y_mm": 20, "z_mm": 5},
                        },
                    },
                ],
            }
        )
        plate = 120 * 40 * 5
        spheres = 3 * (4 / 3) * math.pi * 6**3
        self.assertGreater(outcome.volume_mm3, plate)
        self.assertLess(outcome.volume_mm3, plate + spheres)

    def test_mirror_produces_symmetric_extent(self) -> None:
        outcome = build_geometry_program(
            {
                "op": "mirror",
                "plane": "yz",
                "child": {"op": "box", "length_mm": 10, "width_mm": 10, "height_mm": 10},
            }
        )
        self.assertEqual(outcome.measured_extent_mm[0], 20.0)

    def test_rejects_negative_dimension(self) -> None:
        with self.assertRaises(CreoMcpError):
            build_geometry_program(
                {"op": "box", "length_mm": -5, "width_mm": 10, "height_mm": 10}
            )

    def test_rejects_extra_fields(self) -> None:
        with self.assertRaises(CreoMcpError):
            build_geometry_program({"op": "sphere", "radius_mm": 5, "bogus": 1})

    def test_rejects_total_subtraction(self) -> None:
        with self.assertRaises(CreoMcpError):
            build_geometry_program(
                {
                    "op": "difference",
                    "children": [
                        {"op": "box", "length_mm": 10, "width_mm": 10, "height_mm": 10},
                        {"op": "box", "length_mm": 30, "width_mm": 30, "height_mm": 30},
                    ],
                }
            )

    def test_rejects_disconnected_solids(self) -> None:
        with self.assertRaises(CreoMcpError):
            build_geometry_program(
                {
                    "op": "union",
                    "children": [
                        {"op": "box", "length_mm": 10, "width_mm": 10, "height_mm": 10},
                        {
                            "op": "box",
                            "length_mm": 10,
                            "width_mm": 10,
                            "height_mm": 10,
                            "at": {"x_mm": 50},
                        },
                    ],
                }
            )

    def test_rejects_extent_over_limit(self) -> None:
        with self.assertRaises(CreoMcpError):
            build_geometry_program(
                {"op": "box", "length_mm": 5000, "width_mm": 10, "height_mm": 10}
            )

    def test_request_requires_confirmation(self) -> None:
        base = {
            "program": {"op": "box", "length_mm": 10, "width_mm": 10, "height_mm": 10},
            "part_name": "MCP_DEMO_001",
            "output_directory": "C:/temp",
            "confirmation": "build_geometry",
        }
        BuildGeometryRequest.model_validate(base)
        with self.assertRaises(Exception):
            BuildGeometryRequest.model_validate({**base, "confirmation": "yes"})
        with self.assertRaises(Exception):
            BuildGeometryRequest.model_validate({**base, "part_name": "9bad name"})


if __name__ == "__main__":
    unittest.main()
