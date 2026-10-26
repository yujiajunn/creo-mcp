"""Regression tests for the portable MCP result schemas."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from creo_mcp.models import (
    BoxDimensionSymbols,
    CreatePackagingBoxRequest,
    CreoModelInfo,
    CreoStatus,
    CreatePartRequest,
    InspectModelGeometryRequest,
    PackagingBoxPlanRequest,
    UdfManifest,
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
        request = CreatePartRequest(
            part_name="mcp_test_part",
            activate=False,
            confirmation="create_empty_part",
        )

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

    def test_geometry_lookup_accepts_numeric_imported_component_name(self) -> None:
        request = InspectModelGeometryRequest(model_name="0009", model_kind="part")

        self.assertEqual(request.model_name, "0009")

    def test_model_item_new_names_are_unique_per_type(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be unique"):
            UpdateModelMetadataRequest(
                renames=[
                    {"item_type": "feature", "item_id": 1, "new_name": "BODY"},
                    {"item_type": "feature", "item_id": 2, "new_name": "body"},
                ],
                confirmation="update_model_metadata",
            )


class BlisterTrayUdfCandidateManifestTests(unittest.TestCase):
    """The pending tray UDF asset must not drift into an invalid contract."""

    MANIFEST = (
        Path(__file__).resolve().parents[1]
        / "live-validation"
        / "blister-tray.candidate.udf.json"
    )

    def test_candidate_manifest_is_schema_valid_and_unqualified(self) -> None:
        raw = json.loads(self.MANIFEST.read_text(encoding="utf-8"))
        manifest = UdfManifest.model_validate(raw)

        self.assertIs(manifest.verified_non_interactive, False)
        self.assertEqual(manifest.qualified_creo_versions, ["13.4.1.0"])
        self.assertEqual(manifest.allowed_model_kinds, ["part"])
        self.assertTrue(manifest.references, "the tray UDF must declare its references")

    def test_candidate_manifest_still_marks_pending_asset_steps(self) -> None:
        raw = json.loads(self.MANIFEST.read_text(encoding="utf-8"))

        # The .gph has not been exported yet, so the hash stays a visible placeholder
        # and every dimension name that must be read from the template keeps its TODO.
        self.assertEqual(raw["sha256"], "0" * 64)
        pending = [dimension["name"] for dimension in raw["variant_dimensions"]]
        self.assertEqual(len(pending), 5)
        self.assertTrue(all(name.startswith("TODO_") for name in pending))


if __name__ == "__main__":
    unittest.main()
