"""Regression tests for the portable MCP result schemas."""

from __future__ import annotations

import unittest

from creo_mcp.models import (
    BoxDimensionSymbols,
    CreatePackagingBoxRequest,
    CreoModelInfo,
    CreoStatus,
    CreatePartRequest,
    InspectModelGeometryRequest,
    PackagingBoxPlanRequest,
    UpdateModelMetadataRequest,
    create_packaging_box_plan,
)


class CreoModelSchemaTests(unittest.TestCase):
    """Verify that result payloads remain strict and JSON serializable."""

    def test_status_serializes_active_model(self) -> None:
        status = CreoStatus(
            connected=True,
            creo_loadpoint=r"D:\PTC\Creo13.4\Creo 13.4.1.0",
            working_directory=r"<work-dir>",
            active_model=CreoModelInfo(
                file_name="box.prt",
                instance_name="BOX",
                model_type="PART",
            ),
        )

        self.assertEqual(status.model_dump(mode="json")["active_model"]["file_name"], "box.prt")

    def test_status_schema_forbids_unexpected_fields(self) -> None:
        schema = CreoStatus.model_json_schema()

        self.assertFalse(schema["additionalProperties"])

    def test_packaging_plan_is_deterministic_and_read_only(self) -> None:
        request = PackagingBoxPlanRequest(
            length_mm=300,
            width_mm=200,
            height_mm=120,
            part_name="sample_box",
            wall_thickness_mm=2,
        )

        first = create_packaging_box_plan(request)
        second = create_packaging_box_plan(request)

        self.assertEqual(first.plan_id, second.plan_id)
        self.assertEqual(first.execution_state, "dry_run_only")
        self.assertTrue(first.requires_user_confirmation)

    def test_packaging_plan_rejects_impossible_wall_thickness(self) -> None:
        with self.assertRaises(ValueError):
            PackagingBoxPlanRequest(
                length_mm=30,
                width_mm=20,
                height_mm=10,
                wall_thickness_mm=5,
            )

    def test_empty_part_request_accepts_safe_creo_part_name(self) -> None:
        request = CreatePartRequest(part_name="mcp_test_part", activate=False)

        self.assertEqual(request.part_name, "mcp_test_part")
        self.assertFalse(request.activate)

    def test_create_box_requires_matching_plan(self) -> None:
        planned = create_packaging_box_plan(
            PackagingBoxPlanRequest(
                length_mm=300,
                width_mm=200,
                height_mm=120,
                part_name="sample_box",
            )
        )

        request = CreatePackagingBoxRequest(
            length_mm=300,
            width_mm=200,
            height_mm=120,
            part_name="sample_box",
            template_path=r"D:\templates\packaging_box.prt",
            plan_id=planned.plan_id,
            confirmation="create_new_model",
        )

        self.assertEqual(request.plan_id, planned.plan_id)

    def test_create_box_rejects_stale_plan(self) -> None:
        planned = create_packaging_box_plan(
            PackagingBoxPlanRequest(
                length_mm=300,
                width_mm=200,
                height_mm=120,
                part_name="sample_box",
            )
        )

        with self.assertRaisesRegex(ValueError, "plan_id does not match"):
            CreatePackagingBoxRequest(
                length_mm=301,
                width_mm=200,
                height_mm=120,
                part_name="sample_box",
                template_path=r"D:\templates\packaging_box.prt",
                plan_id=planned.plan_id,
                confirmation="create_new_model",
            )

    def test_create_box_rejects_unimplemented_wall_thickness(self) -> None:
        with self.assertRaisesRegex(ValueError, "not supported"):
            CreatePackagingBoxRequest(
                length_mm=300,
                width_mm=200,
                height_mm=120,
                part_name="sample_box",
                template_path=r"D:\templates\packaging_box.prt",
                wall_thickness_mm=2,
                plan_id="0" * 16,
                confirmation="create_new_model",
            )

    def test_dimension_symbols_must_be_unique(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be distinct"):
            BoxDimensionSymbols(length="SIZE", width="size", height="HEIGHT")

    def test_geometry_lookup_requires_name_and_kind_together(self) -> None:
        with self.assertRaisesRegex(ValueError, "must either both be set"):
            InspectModelGeometryRequest(model_name="sample")

        active_request = InspectModelGeometryRequest()
        self.assertIsNone(active_request.model_name)

    def test_model_item_new_names_are_unique_per_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be unique"):
            UpdateModelMetadataRequest(
                renames=[
                    {"item_type": "feature", "item_id": 1, "new_name": "BODY"},
                    {"item_type": "feature", "item_id": 2, "new_name": "body"},
                ],
                confirmation="update_model_metadata",
            )


if __name__ == "__main__":
    unittest.main()
