"""Tests for the constrained JSON geometry program (creo_build_geometry)."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from creo_mcp.concept_geometry import _write_colored_concept_step
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
        with self.assertRaises(ValidationError):
            BuildGeometryRequest.model_validate({**base, "confirmation": "yes"})
        with self.assertRaises(ValidationError):
            BuildGeometryRequest.model_validate({**base, "part_name": "9bad name"})


class GeometryDslV1Tests(unittest.TestCase):
    """source/transform nodes: external STEP operands and rigid placement."""

    def _write_source_step(self, tmp: Path) -> Path:
        outcome = build_geometry_program(
            {"op": "box", "length_mm": 50, "width_mm": 30, "height_mm": 10}
        )
        path = tmp / "block.stp"
        _write_colored_concept_step(outcome.shape, path, "test block")
        return path

    def test_source_can_be_cut_by_primitive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            source = self._write_source_step(tmp)
            outcome = build_geometry_program(
                {
                    "op": "difference",
                    "children": [
                        {"op": "source", "path": str(source)},
                        {
                            "op": "cylinder",
                            "radius_mm": 5,
                            "height_mm": 30,
                            "axis": "z",
                            "at": {"x_mm": 25, "y_mm": 15, "z_mm": -10},
                        },
                    ],
                }
            )
            expected = 50 * 30 * 10 - math.pi * 25 * 10
            self.assertAlmostEqual(outcome.volume_mm3, expected, delta=1.0)

    def test_source_solid_index_out_of_range_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = self._write_source_step(Path(raw))
            with self.assertRaises(CreoMcpError):
                build_geometry_program({"op": "source", "path": str(source), "solid_index": 3})

    def test_source_missing_file_is_rejected(self) -> None:
        with self.assertRaises(CreoMcpError):
            build_geometry_program({"op": "source", "path": "C:/definitely/missing.step"})

    def test_transform_rotates_and_translates(self) -> None:
        outcome = build_geometry_program(
            {
                "op": "transform",
                "rotate_axis": "z",
                "rotate_angle_deg": 90,
                "translate": {"x_mm": 5},
                "child": {"op": "box", "length_mm": 40, "width_mm": 10, "height_mm": 5},
            }
        )
        # 90° about z swaps x/y: the 40 mm length now spans y, the 10 mm depth spans x.
        self.assertEqual(round(outcome.measured_extent_mm[0], 6), 10.0)
        self.assertEqual(round(outcome.measured_extent_mm[1], 6), 40.0)

    def test_transform_requires_axis_and_angle_together(self) -> None:
        with self.assertRaises(CreoMcpError):
            build_geometry_program(
                {
                    "op": "transform",
                    "rotate_angle_deg": 30,
                    "child": {"op": "box", "length_mm": 5, "width_mm": 5, "height_mm": 5},
                }
            )

    def test_source_plus_pattern_produces_connected_solid(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = self._write_source_step(Path(raw))
            outcome = build_geometry_program(
                {
                    "op": "union",
                    "children": [
                        {"op": "source", "path": str(source)},
                        {
                            "op": "linear_pattern",
                            "count": 2,
                            "step": {"x_mm": 20},
                            "child": {
                                "op": "cylinder",
                                "radius_mm": 4,
                                "height_mm": 10,
                                "at": {"x_mm": 10, "y_mm": 15, "z_mm": 5},
                            },
                        },
                    ],
                }
            )
            block = 50 * 30 * 10
            # Posts embed half their height, so the fused volume strictly grows.
            self.assertGreater(outcome.volume_mm3, block)


if __name__ == "__main__":
    unittest.main()
