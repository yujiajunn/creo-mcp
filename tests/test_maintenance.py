"""Tests for maintainable model edits, handoff checks, saving, and export."""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("CREO_MCP_DISPLAY_MODE", "display")

from pydantic import ValidationError

from creo_mcp.bridge import (
    export_model,
    get_handoff_report,
    save_model_as,
    update_feature_suppression,
    update_model_metadata,
)
from creo_mcp.errors import CreoMcpError
from creo_mcp.models import (
    BoxExtent,
    ExportModelRequest,
    HandoffReportRequest,
    SaveModelAsRequest,
    UpdateFeatureSuppressionRequest,
    UpdateModelMetadataRequest,
)


class _FakeSequence:
    def __init__(self, items: list[object]) -> None:
        self._items = items
        self.Count = len(items)

    def Item(self, index: int) -> object:
        return self._items[index]


class _FakeItem:
    def __init__(self, item_id: int, name: str | None) -> None:
        self.Id = item_id
        self.name = name
        self.set_names: list[str] = []

    def GetName(self) -> str:
        if self.name is None:
            raise RuntimeError("unnamed")
        return self.name

    def SetName(self, name: str) -> None:
        self.name = name
        self.set_names.append(name)


class _FakeFeature(_FakeItem):
    def __init__(self, item_id: int, name: str | None, status: int = 0) -> None:
        super().__init__(item_id, name)
        self.FeatType = 13
        self.FeatTypeName = "DATUM PLANE"
        self.Status = status
        self.suppress_calls = 0
        self.resume_calls = 0

    def Suppress(self) -> None:
        self.suppress_calls += 1
        self.Status = 5

    def Resume(self) -> None:
        self.resume_calls += 1
        self.Status = 0


class _FeatureWithoutSuppressionApi(_FakeFeature):
    """Feature object whose type library exposes neither suppress nor resume."""

    def __getattribute__(self, name: str) -> object:
        if name in ("Suppress", "SuppressFeature", "Resume", "ResumeFeature"):
            raise AttributeError(name)
        return super().__getattribute__(name)


class _FakeDimension:
    def __init__(self, symbol: str, value: float) -> None:
        self.Symbol = symbol
        self.DimValue = value


class _FakeParamValue:
    def __init__(self, discr: int, member: str, value: object) -> None:
        self.discr = discr
        setattr(self, member, value)


class _FakeParameter(_FakeItem):
    Units = None

    def __init__(self, name: str, value: _FakeParamValue) -> None:
        super().__init__(0, name)
        self.Value = value

    def GetScaledValue(self) -> _FakeParamValue:
        return self.Value


class _FakeParamFactory:
    def CreateStringParamValue(self, value: str) -> _FakeParamValue:
        return _FakeParamValue(0, "StringValue", value)

    def CreateIntParamValue(self, value: int) -> _FakeParamValue:
        return _FakeParamValue(1, "IntValue", value)

    def CreateBoolParamValue(self, value: bool) -> _FakeParamValue:
        return _FakeParamValue(2, "BoolValue", value)

    def CreateDoubleParamValue(self, value: float) -> _FakeParamValue:
        return _FakeParamValue(3, "DoubleValue", value)


class _FakeLengthUnit:
    def __init__(self, name: str) -> None:
        self.Name = name


class _FakeUnitSystem:
    def __init__(self, name: str) -> None:
        self.name = name

    def GetUnit(self, _unit_type: int) -> _FakeLengthUnit:
        return _FakeLengthUnit(self.name)


class _FakeModel:
    FileName = "sample.prt"
    InstanceName = "SAMPLE"
    Type = 1

    def __init__(self, unit: str = "mm") -> None:
        self.unit = unit
        self.features = [_FakeFeature(1, "FRONT")]
        self.coordinate_systems = [_FakeItem(92, "CS0")]
        self.dimensions = [_FakeDimension("LENGTH", 2.0)]
        self.parameters = {
            "MCP_AUTOMATION_ID": _FakeParameter(
                "MCP_AUTOMATION_ID", _FakeParamValue(0, "StringValue", "old")
            )
        }
        self.created_parameters: list[tuple[str, _FakeParamValue]] = []
        self.regenerate_count = 0
        self.session: _FakeSession | None = None

    def ListItems(self, item_type: int) -> _FakeSequence | None:
        values = {
            0: self.features,
            3: self.coordinate_systems,
            10: self.dimensions,
        }.get(item_type, [])
        return _FakeSequence(values) if values else None

    def GetItemById(self, item_type: int, item_id: int) -> object | None:
        sequence = self.ListItems(item_type)
        if sequence is None:
            return None
        return next((item for item in sequence._items if item.Id == item_id), None)

    def GetParam(self, name: str) -> _FakeParameter | None:
        return self.parameters.get(name.upper())

    def Display(self) -> None:
        if self.session is not None:
            self.session.CurrentModel = self
            self.session.window.activated = True

    def CreateParam(self, name: str, value: _FakeParamValue) -> None:
        self.created_parameters.append((name, value))
        self.parameters[name.upper()] = _FakeParameter(name, value)

    def ListParams(self) -> _FakeSequence:
        return _FakeSequence(list(self.parameters.values()))

    def GetPrincipalUnits(self) -> _FakeUnitSystem:
        return _FakeUnitSystem(self.unit)

    def Regenerate(self, _instructions: object) -> None:
        self.regenerate_count += 1

    def CopyAndRetrieve(self, name: str, _instructions: object) -> _FakeModel:
        assert self.session is not None
        copied = _FakeModel(self.unit)
        copied.session = self.session
        copied.FileName = f"{name}.prt"
        copied.InstanceName = name.upper()
        (Path(self.session.directory) / f"{name}.prt.1").write_bytes(b"native")
        return copied

    def ExportIntf3D(self, path: str, export_type: int, profile: str | None) -> None:
        self.last_export = (path, export_type, profile)
        Path(f"{path}.stp").write_bytes(b"neutral-cad")


class _FakeWindow:
    def __init__(self) -> None:
        self.activated = False
        self.repaint_count = 0

    def Activate(self) -> None:
        self.activated = True

    def Repaint(self) -> None:
        self.repaint_count += 1


class _FakeSession:
    def __init__(self, model: _FakeModel, directory: str) -> None:
        self.CurrentModel = model
        self.directory = directory
        self.directory_changes: list[str] = []
        self.window = _FakeWindow()
        self.CurrentWindow = self.window
        model.session = self

    def GetCurrentDirectory(self) -> str:
        return self.directory

    def ChangeDirectory(self, directory: str) -> None:
        self.directory = directory
        self.directory_changes.append(directory)

    def GetModel(self, _name: str, _model_type: int) -> None:
        return None

    def CreateModelWindow(self, _model: _FakeModel) -> _FakeWindow:
        return self.window


@contextmanager
def _fake_connection(session: _FakeSession):
    yield session, Path(r"D:\FixtureCreo\Creo13.4")


class MaintenanceTests(unittest.TestCase):
    def test_file_outputs_require_absolute_paths(self) -> None:
        with self.assertRaisesRegex(CreoMcpError, "must be absolute"):
            save_model_as(
                SaveModelAsRequest(
                    target_name="copy",
                    output_directory="relative-output",
                    confirmation="save_model_as",
                )
            )
        with self.assertRaisesRegex(CreoMcpError, "must be absolute"):
            export_model(
                ExportModelRequest(
                    output_path="relative-output/sample.stp",
                    export_format="step",
                    confirmation="export_model",
                )
            )

    def test_updates_names_dimensions_and_parameters_without_saving(self) -> None:
        model = _FakeModel(unit="in")
        session = _FakeSession(model, r"D:\CreoWork")
        request = UpdateModelMetadataRequest(
            renames=[
                {"item_type": "coordinate_system", "item_id": 92, "new_name": "PRODUCT_CSYS"}
            ],
            dimensions=[{"symbol": "length", "value": 25.4, "unit": "mm"}],
            parameters=[
                {"name": "MCP_AUTOMATION_ID", "value_type": "string", "value": "job-1"},
                {"name": "MCP_AUTOMATION_REVISION", "value_type": "integer", "value": 1},
            ],
            confirmation="update_model_metadata",
        )

        with (
            patch("creo_mcp.bridge._connected_session", return_value=_fake_connection(session)),
            patch("creo_mcp.bridge._dispatch_factory", return_value=_FakeParamFactory()),
        ):
            result = update_model_metadata(request)

        self.assertEqual(model.coordinate_systems[0].name, "PRODUCT_CSYS")
        self.assertAlmostEqual(model.dimensions[0].DimValue, 1.0)
        self.assertEqual(model.parameters["MCP_AUTOMATION_ID"].Value.StringValue, "job-1")
        self.assertEqual(model.created_parameters[0][0], "MCP_AUTOMATION_REVISION")
        self.assertEqual(model.regenerate_count, 1)
        self.assertEqual(session.window.repaint_count, 1)
        self.assertFalse(result.saved)

    def test_name_conflict_rejects_every_change_before_mutation(self) -> None:
        model = _FakeModel()
        model.coordinate_systems.append(_FakeItem(93, "PRODUCT_CSYS"))
        session = _FakeSession(model, r"D:\CreoWork")
        request = UpdateModelMetadataRequest(
            renames=[
                {"item_type": "coordinate_system", "item_id": 92, "new_name": "PRODUCT_CSYS"}
            ],
            dimensions=[{"symbol": "LENGTH", "value": 50, "unit": "mm"}],
            confirmation="update_model_metadata",
        )

        with patch(
            "creo_mcp.bridge._connected_session", return_value=_fake_connection(session)
        ), self.assertRaisesRegex(CreoMcpError, "already exists"):
            update_model_metadata(request)

        self.assertEqual(model.coordinate_systems[0].name, "CS0")
        self.assertEqual(model.dimensions[0].DimValue, 2.0)
        self.assertEqual(model.regenerate_count, 0)

    def test_parameter_type_mismatch_is_rejected_before_mutation(self) -> None:
        model = _FakeModel()
        session = _FakeSession(model, r"D:\CreoWork")
        request = UpdateModelMetadataRequest(
            parameters=[
                {"name": "MCP_AUTOMATION_ID", "value_type": "integer", "value": 2}
            ],
            confirmation="update_model_metadata",
        )

        with (
            patch("creo_mcp.bridge._connected_session", return_value=_fake_connection(session)),
            patch("creo_mcp.bridge._dispatch_factory", return_value=_FakeParamFactory()),
        ):
            with self.assertRaisesRegex(CreoMcpError, "already exists with type code"):
                update_model_metadata(request)

        self.assertEqual(model.parameters["MCP_AUTOMATION_ID"].Value.StringValue, "old")
        self.assertEqual(model.regenerate_count, 0)

    def test_missing_parameter_exception_allows_parameter_creation(self) -> None:
        model = _FakeModel()
        session = _FakeSession(model, r"D:\CreoWork")

        def missing_parameter(_name: str) -> None:
            raise RuntimeError("pfcExceptions::XToolkitNotFound")

        model.GetParam = missing_parameter  # type: ignore[method-assign]
        request = UpdateModelMetadataRequest(
            parameters=[{"name": "NEW_PARAM", "value_type": "boolean", "value": True}],
            confirmation="update_model_metadata",
        )
        with (
            patch("creo_mcp.bridge._connected_session", return_value=_fake_connection(session)),
            patch("creo_mcp.bridge._dispatch_factory", return_value=_FakeParamFactory()),
        ):
            update_model_metadata(request)

        self.assertTrue(model.parameters["NEW_PARAM"].Value.BoolValue)

    def test_handoff_report_finds_unnamed_and_non_active_features(self) -> None:
        model = _FakeModel()
        imported = _FakeFeature(2, None)
        imported.FeatType = 22
        imported.FeatTypeName = "IMPORT FEATURE"
        model.features.extend([imported, _FakeFeature(3, "FAILED", 5)])
        model.dimensions = []
        session = _FakeSession(model, r"D:\CreoWork")

        with (
            patch("creo_mcp.bridge._connected_session", return_value=_fake_connection(session)),
            patch("creo_mcp.bridge._measure_extent", return_value=BoxExtent(x_mm=1, y_mm=2, z_mm=3)),
        ):
            result = get_handoff_report(HandoffReportRequest())

        self.assertEqual(result.handoff_state, "review_required")
        self.assertEqual([item.item_id for item in result.non_active_features], [3])
        self.assertEqual([item.item_id for item in result.unnamed_features], [2])
        self.assertEqual([item.item_id for item in result.imported_features], [2])
        self.assertIn("Imported model exposes no named driving dimensions.", result.warnings)
        self.assertEqual(result.automation_parameters[0].name, "MCP_AUTOMATION_ID")

    def test_save_as_refuses_existing_version(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            (directory / "copy.prt.7").touch()
            model = _FakeModel()
            session = _FakeSession(model, raw_directory)
            request = SaveModelAsRequest(
                target_name="copy",
                output_directory=raw_directory,
                confirmation="save_model_as",
            )
            with patch(
                "creo_mcp.bridge._connected_session", return_value=_fake_connection(session)
            ), self.assertRaisesRegex(CreoMcpError, "Refusing to overwrite"):
                save_model_as(request)

    def test_save_as_restores_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_source, tempfile.TemporaryDirectory() as raw_target:
            model = _FakeModel()
            session = _FakeSession(model, raw_source)
            request = SaveModelAsRequest(
                target_name="copy",
                output_directory=raw_target,
                confirmation="save_model_as",
            )
            with patch(
                "creo_mcp.bridge._connected_session", return_value=_fake_connection(session)
            ):
                result = save_model_as(request)

        # bridge changes into and back out of resolved directories.
        source = str(Path(raw_source).resolve(strict=False))
        target = str(Path(raw_target).resolve(strict=False))
        self.assertEqual(session.directory, source)
        self.assertEqual(session.directory_changes, [target, source])
        self.assertTrue(result.saved_path.endswith("copy.prt.1"))
        self.assertTrue(session.window.activated)
        self.assertEqual(session.window.repaint_count, 1)

    def test_export_rejects_wrong_extension_and_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            model = _FakeModel()
            session = _FakeSession(model, raw_directory)
            wrong = ExportModelRequest(
                output_path=str(directory / "sample.igs"),
                export_format="step",
                confirmation="export_model",
            )
            existing_path = directory / "sample.stp"
            existing_path.touch()
            existing = ExportModelRequest(
                output_path=str(existing_path),
                export_format="step",
                confirmation="export_model",
            )
            with patch(
                "creo_mcp.bridge._connected_session", return_value=_fake_connection(session)
            ):
                with self.assertRaisesRegex(CreoMcpError, "does not match"):
                    export_model(wrong)
                with self.assertRaisesRegex(CreoMcpError, "Refusing to overwrite"):
                    export_model(existing)

    def test_export_verifies_non_empty_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            model = _FakeModel()
            session = _FakeSession(model, raw_directory)
            # export_model resolves output_path before handing it to Creo.
            output = Path(raw_directory).resolve(strict=False) / "sample.step"
            request = ExportModelRequest(
                output_path=str(output),
                export_format="step",
                confirmation="export_model",
            )
            with patch(
                "creo_mcp.bridge._connected_session", return_value=_fake_connection(session)
            ):
                result = export_model(request)

            self.assertGreater(result.file_size_bytes, 0)
            self.assertTrue(output.is_file())
            self.assertFalse((output.parent / "sample.stp").exists())
            self.assertEqual(model.last_export[1], 15)
            self.assertEqual(model.last_export[0], str(output.with_suffix("")))
            self.assertFalse(result.saved)


class _StubbornFeature(_FakeFeature):
    """Accepts a Suppress call that Creo never reflects in the model tree."""

    def Suppress(self) -> None:
        self.suppress_calls += 1


class FeatureSuppressionTests(unittest.TestCase):
    """Mock coverage for model-tree suppression; Creo semantics need a live session."""

    def _run(self, features, updates):
        model = _FakeModel()
        model.features = list(features)
        session = _FakeSession(model, r"D:\CreoWork")
        request = UpdateFeatureSuppressionRequest(
            updates=updates,
            confirmation="update_feature_suppression",
        )
        with patch(
            "creo_mcp.bridge._connected_session", return_value=_fake_connection(session)
        ):
            return model, session, update_feature_suppression(request)

    def test_suppress_by_name_and_resume_by_id_report_observed_status(self) -> None:
        slot = _FakeFeature(10, "SLOT1_CUT", status=0)
        flange = _FakeFeature(11, "SEAL_FLANGE", status=5)
        model, session, result = self._run(
            [_FakeFeature(1, "FRONT"), slot, flange],
            [
                {"feature_name": "slot1_cut", "suppressed": True},
                {"item_id": 11, "suppressed": False},
            ],
        )

        self.assertEqual(slot.suppress_calls, 1)
        self.assertEqual(flange.resume_calls, 1)
        self.assertEqual(model.regenerate_count, 1)
        self.assertEqual(session.window.repaint_count, 1)
        self.assertTrue(result.regenerated)
        self.assertFalse(result.saved)
        by_id = {item.item_id: item for item in result.features}
        self.assertEqual(by_id[10].name, "SLOT1_CUT")
        self.assertEqual(by_id[10].previous_status_name, "active")
        self.assertEqual(by_id[10].resulting_status_name, "suppressed")
        self.assertTrue(by_id[10].changed)
        self.assertEqual(by_id[11].previous_status_name, "suppressed")
        self.assertEqual(by_id[11].resulting_status_name, "active")

    def test_features_already_in_state_make_no_creos_call(self) -> None:
        slot = _FakeFeature(10, "SLOT1_CUT", status=5)
        model, _session, result = self._run(
            [slot], [{"feature_name": "SLOT1_CUT", "suppressed": True}]
        )

        self.assertEqual(slot.suppress_calls, 0)
        self.assertEqual(model.regenerate_count, 1)
        self.assertFalse(result.features[0].changed)
        self.assertEqual(result.features[0].resulting_status_name, "suppressed")

    def test_unknown_target_aborts_before_any_feature_is_touched(self) -> None:
        slot = _FakeFeature(10, "SLOT1_CUT", status=0)
        model = _FakeModel()
        model.features = [slot]
        session = _FakeSession(model, r"D:\CreoWork")
        request = UpdateFeatureSuppressionRequest(
            updates=[
                {"feature_name": "SLOT1_CUT", "suppressed": True},
                {"feature_name": "SLOT7_CUT", "suppressed": True},
            ],
            confirmation="update_feature_suppression",
        )

        with patch(
            "creo_mcp.bridge._connected_session", return_value=_fake_connection(session)
        ), self.assertRaisesRegex(CreoMcpError, "No feature named") as raised:
            update_feature_suppression(request)

        self.assertIn("SLOT1_CUT", str(raised.exception))
        self.assertEqual(slot.suppress_calls, 0)
        self.assertEqual(model.regenerate_count, 0)

    def test_program_driven_suppression_is_refused(self) -> None:
        feature = _FakeFeature(12, "DRAFT_ALL", status=4)
        model = _FakeModel()
        model.features = [feature]
        session = _FakeSession(model, r"D:\CreoWork")
        request = UpdateFeatureSuppressionRequest(
            updates=[{"feature_name": "DRAFT_ALL", "suppressed": False}],
            confirmation="update_feature_suppression",
        )

        with patch(
            "creo_mcp.bridge._connected_session", return_value=_fake_connection(session)
        ), self.assertRaisesRegex(CreoMcpError, "owned by a family table"):
            update_feature_suppression(request)

        self.assertEqual(feature.resume_calls, 0)
        self.assertEqual(model.regenerate_count, 0)

    def test_ambiguous_feature_name_requires_item_id(self) -> None:
        first = _FakeFeature(20, "SLOT_CUT", status=0)
        second = _FakeFeature(21, "SLOT_CUT", status=0)
        with self.assertRaisesRegex(CreoMcpError, "matches several features"):
            self._run([first, second], [{"feature_name": "SLOT_CUT", "suppressed": True}])
        self.assertEqual(first.suppress_calls, 0)

    def test_missing_suppression_method_reports_the_probe_failure(self) -> None:
        feature = _FeatureWithoutSuppressionApi(30, "MYSTERY", status=0)
        model = _FakeModel()
        model.features = [feature]
        session = _FakeSession(model, r"D:\CreoWork")
        request = UpdateFeatureSuppressionRequest(
            updates=[{"feature_name": "MYSTERY", "suppressed": True}],
            confirmation="update_feature_suppression",
        )

        with patch(
            "creo_mcp.bridge._connected_session", return_value=_fake_connection(session)
        ), self.assertRaisesRegex(CreoMcpError, "no feature suppression method") as raised:
            update_feature_suppression(request)

        self.assertIn("Probed", str(raised.exception))
        self.assertEqual(model.regenerate_count, 0)

    def test_unconfirmed_resulting_status_is_an_error(self) -> None:
        feature = _StubbornFeature(40, "SLOT9_CUT", status=0)
        model = _FakeModel()
        model.features = [feature]
        session = _FakeSession(model, r"D:\CreoWork")
        request = UpdateFeatureSuppressionRequest(
            updates=[{"feature_name": "SLOT9_CUT", "suppressed": True}],
            confirmation="update_feature_suppression",
        )

        with patch(
            "creo_mcp.bridge._connected_session", return_value=_fake_connection(session)
        ), self.assertRaisesRegex(CreoMcpError, "did not report the requested") as raised:
            update_feature_suppression(request)

        self.assertEqual(feature.suppress_calls, 1)
        self.assertEqual(model.regenerate_count, 1)
        self.assertIn("SLOT9_CUT", str(raised.exception))

    def test_each_target_needs_exactly_one_selector(self) -> None:
        no_selector = [{"suppressed": True}]
        both_selectors = [{"feature_name": "SLOT1_CUT", "item_id": 10, "suppressed": True}]
        for updates in (no_selector, both_selectors):
            with self.assertRaises(ValidationError):
                UpdateFeatureSuppressionRequest(
                    updates=updates, confirmation="update_feature_suppression"
                )

    def test_duplicate_targets_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "must be unique"):
            UpdateFeatureSuppressionRequest(
                updates=[
                    {"feature_name": "SLOT1_CUT", "suppressed": True},
                    {"feature_name": "slot1_cut", "suppressed": False},
                ],
                confirmation="update_feature_suppression",
            )


if __name__ == "__main__":
    unittest.main()
