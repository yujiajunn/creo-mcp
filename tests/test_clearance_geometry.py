"""Offline tests for hard-edge, directionally insertable product cavities."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Fuse
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

from creo_mcp.clearance_geometry import (
    build_product_clearance_tray_shape,
    export_product_clearance_tray_step,
    plan_product_clearance_tray,
)
from creo_mcp.models import InspectStepRequest, ProductClearanceTrayPlanRequest
from creo_mcp.step import inspect_step_file


def _write_step(path: Path, shapes: list[object]) -> None:
    writer = STEPControl_Writer()
    for shape in shapes:
        if writer.Transfer(shape, STEPControl_AsIs) != IFSelect_RetDone:
            raise AssertionError("Could not transfer test shape to STEP.")
    if writer.Write(str(path)) != IFSelect_RetDone:
        raise AssertionError("Could not write test STEP.")


def _common_volume(left: object, right: object) -> float:
    operation = BRepAlgoAPI_Common(left, right)
    operation.Build()
    if not operation.IsDone():
        raise AssertionError("Could not measure test-shape intersection.")
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(operation.Shape(), properties)
    return float(properties.Mass())


def _request(product_path: Path, output_directory: str) -> ProductClearanceTrayPlanRequest:
    return ProductClearanceTrayPlanRequest(
        product_step_path=str(product_path),
        orientation={
            "tray_x_from": "x",
            "tray_y_from": "y",
            "tray_z_from": "z",
        },
        side_clearance_mm=2.0,
        top_clearance_mm=0.5,
        bottom_clearance_mm=0.5,
        wall_thickness_mm=1.2,
        bottom_thickness_mm=1.0,
        flange_thickness_mm=0.8,
        flange_width_mm=8.0,
        part_name="hard_edge_concept_r1",
        output_directory=output_directory,
    )


class ProductClearanceGeometryTests(unittest.TestCase):
    def test_separate_components_keep_flat_material_between_cavities(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            product_path = Path(raw_directory) / "separate-components.step"
            left = BRepPrimAPI_MakeBox(12.0, 10.0, 10.0).Shape()
            right = BRepPrimAPI_MakeBox(gp_Pnt(60.0, 0.0, 0.0), 12.0, 10.0, 10.0).Shape()
            _write_step(product_path, [left, right])

            plan = plan_product_clearance_tray(_request(product_path, raw_directory))
            tray = build_product_clearance_tray_shape(plan)
            gap_probe = BRepPrimAPI_MakeBox(gp_Pnt(-2.0, -2.0, -9.0), 4.0, 4.0, 6.0).Shape()

            self.assertGreater(_common_volume(tray, gap_probe), 50.0)
            self.assertEqual(plan.source_solid_count, 2)
            self.assertEqual(plan.static_outside_volume_mm3, 0.0)

    def test_irregular_product_keeps_material_in_bounding_box_corner(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            product_path = Path(raw_directory) / "cross.step"
            horizontal = BRepPrimAPI_MakeBox(40.0, 10.0, 10.0).Shape()
            vertical = BRepPrimAPI_MakeBox(
                gp_Pnt(15.0, -10.0, -5.0),
                10.0,
                30.0,
                20.0,
            ).Shape()
            fused = BRepAlgoAPI_Fuse(horizontal, vertical)
            fused.Build()
            self.assertTrue(fused.IsDone())
            _write_step(product_path, [fused.Shape()])

            plan = plan_product_clearance_tray(_request(product_path, raw_directory))
            tray = build_product_clearance_tray_shape(plan)
            missing_corner_probe = BRepPrimAPI_MakeBox(
                gp_Pnt(12.0, 10.0, -10.0),
                2.0,
                2.0,
                5.0,
            ).Shape()

            self.assertGreater(_common_volume(tray, missing_corner_probe), 1.0)
            self.assertEqual(plan.static_outside_volume_mm3, 0.0)
            self.assertEqual(plan.insertion_sweep_outside_volume_mm3, 0.0)
            self.assertTrue(plan.insertion_path_verified)
            self.assertEqual(
                plan.geometry_source_mode,
                "conservative_brep_component_rectilinear_depth_envelope",
            )
            self.assertGreater(plan.mesh_linear_deflection_mm, 0.0)
            self.assertEqual(
                plan.clearance_conservatism_mm,
                plan.mesh_linear_deflection_mm,
            )
            self.assertEqual(plan.corner_radius_mm, 0.0)
            self.assertEqual(plan.draft_expansion_mm, 0.0)
            self.assertEqual(plan.automatic_handling_reliefs, "none")
            self.assertGreater(plan.depth_layer_count, 1)
            self.assertLessEqual(plan.depth_layer_height_mm, 1.0)
            self.assertGreater(
                plan.vertical_conservatism_mm,
                plan.mesh_linear_deflection_mm,
            )

    def test_one_solid_keeps_local_shallow_region_shallower(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            product_path = Path(raw_directory) / "local-depth.step"
            deep_body = BRepPrimAPI_MakeBox(10.0, 10.0, 20.0).Shape()
            shallow_arm = BRepPrimAPI_MakeBox(
                gp_Pnt(10.0, 2.0, 14.0),
                30.0,
                6.0,
                6.0,
            ).Shape()
            fused = BRepAlgoAPI_Fuse(deep_body, shallow_arm)
            fused.Build()
            self.assertTrue(fused.IsDone())
            _write_step(product_path, [fused.Shape()])

            plan = plan_product_clearance_tray(_request(product_path, raw_directory))
            tray = build_product_clearance_tray_shape(plan)
            shallow_region_material = BRepPrimAPI_MakeBox(
                gp_Pnt(5.0, -1.0, -18.0),
                2.0,
                2.0,
                8.0,
            ).Shape()
            deep_region_void = BRepPrimAPI_MakeBox(
                gp_Pnt(-18.0, -1.0, -18.0),
                2.0,
                2.0,
                8.0,
            ).Shape()

            self.assertGreater(_common_volume(tray, shallow_region_material), 1.0)
            self.assertAlmostEqual(_common_volume(tray, deep_region_void), 0.0, delta=1e-6)
            self.assertEqual(plan.static_outside_volume_mm3, 0.0)
            self.assertEqual(plan.insertion_sweep_outside_volume_mm3, 0.0)

    def test_insertion_sweep_opens_a_lower_wide_feature_to_the_top(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            product_path = Path(raw_directory) / "undercut.step"
            lower_flange = BRepPrimAPI_MakeBox(30.0, 20.0, 3.0).Shape()
            upper_stem = BRepPrimAPI_MakeBox(
                gp_Pnt(10.0, 7.0, 3.0),
                10.0,
                6.0,
                17.0,
            ).Shape()
            fused = BRepAlgoAPI_Fuse(lower_flange, upper_stem)
            fused.Build()
            self.assertTrue(fused.IsDone())
            _write_step(product_path, [fused.Shape()])

            plan = plan_product_clearance_tray(_request(product_path, raw_directory))
            tray = build_product_clearance_tray_shape(plan)
            opening_probe = BRepPrimAPI_MakeBox(
                gp_Pnt(10.0, -1.0, -1.0),
                2.0,
                2.0,
                1.5,
            ).Shape()

            self.assertAlmostEqual(_common_volume(tray, opening_probe), 0.0, delta=1e-6)
            self.assertEqual(plan.insertion_sweep_outside_volume_mm3, 0.0)

    def test_export_is_one_closed_solid_with_the_planned_extent(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            product_path = Path(raw_directory) / "product.step"
            _write_step(product_path, [BRepPrimAPI_MakeBox(24.0, 12.0, 8.0).Shape()])
            plan = plan_product_clearance_tray(_request(product_path, raw_directory))
            second_plan = plan_product_clearance_tray(_request(product_path, raw_directory))
            output_path = Path(raw_directory) / "tray.step"

            measured = export_product_clearance_tray_step(plan, output_path)
            inspected = inspect_step_file(InspectStepRequest(step_path=str(output_path)))

            self.assertEqual(plan.plan_id, second_plan.plan_id)
            self.assertAlmostEqual(measured.x_mm, plan.expected_extent_mm.length_mm, delta=0.05)
            self.assertAlmostEqual(measured.y_mm, plan.expected_extent_mm.width_mm, delta=0.05)
            self.assertAlmostEqual(measured.z_mm, plan.expected_extent_mm.height_mm, delta=0.05)
            self.assertEqual(inspected.solid_body_count, 1)
            self.assertEqual(inspected.closed_shell_count, 1)


if __name__ == "__main__":
    unittest.main()
