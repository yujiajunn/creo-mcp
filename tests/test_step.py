"""Tests for STEP inspection and the guarded Creo import call."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from creo_mcp.bridge import import_cad, import_step, inspect_model_geometry
from creo_mcp.cad import resolve_cad_source
from creo_mcp.errors import CreoMcpError
from creo_mcp.models import (
    ImportCadRequest,
    ImportStepRequest,
    InspectModelGeometryRequest,
    InspectStepRequest,
)
from creo_mcp.step import inspect_step_file

_PART_STEP = """ISO-10303-21;
HEADER;
FILE_NAME('sample_part','2026-10-08T12:00:00',('tester'),(''),
'PREPROCESSOR','ORIGINATING CAD','');
FILE_SCHEMA(('AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF'));
ENDSEC;
DATA;
#1=PRODUCT('SAMPLE','SAMPLE','',());
#2=MANIFOLD_SOLID_BREP('',#3);
#3=CLOSED_SHELL('',());
#4=ADVANCED_FACE('',(),#5,.T.);
#5=SI_UNIT(.MILLI.,.METRE.);
ENDSEC;
END-ISO-10303-21;
"""


class _FakeSequence:
    def __init__(self, items: list[object]) -> None:
        self._items = items
        self.Count = len(items)

    def Item(self, index: int) -> object:
        return self._items[index]


class _FakeUnit:
    Name = "mm"


class _FakeUnitSystem:
    def GetUnit(self, _unit_type: int) -> _FakeUnit:
        return _FakeUnit()


class _FakeImportedModel:
    FileName = "sample_import.prt"
    InstanceName = "SAMPLE_IMPORT"
    Type = 1

    def GetPrincipalUnits(self) -> _FakeUnitSystem:
        return _FakeUnitSystem()

    def EvalOutline(self, _transform: object, _excluded: object) -> _FakeSequence:
        return _FakeSequence([(-1.0, -2.0, -3.0), (9.0, 18.0, 27.0)])

    def GetMassProperty(self, _coordinate_system: object) -> object:
        return type("MassProperty", (), {"Volume": 6000.0})()


class _FakeSession:
    def __init__(self) -> None:
        self.import_arguments: tuple[object, ...] | None = None
        self.model = _FakeImportedModel()
        self.CurrentModel = self.model

    def GetModel(self, _name: str, _model_type: int) -> None:
        return None

    def GetImportSourceType(self, _path: str, import_type: int) -> int:
        self.detected_import_type = import_type
        return 1

    def ImportNewModel(self, *arguments: object) -> _FakeImportedModel:
        self.import_arguments = arguments
        return self.model

    def GetCurrentDirectory(self) -> str:
        return r"<work-dir>"


@contextmanager
def _fake_connection(session: _FakeSession):
    yield session, Path(r"D:\FixtureCreo\Creo13.4")


class StepTests(unittest.TestCase):
    def _write_step(self, directory: Path, name: str = "sample.stp") -> Path:
        path = directory / name
        path.write_text(_PART_STEP, encoding="ascii")
        return path

    def test_inspection_reports_native_structure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = self._write_step(Path(raw_directory))

            result = inspect_step_file(InspectStepRequest(step_path=str(path)))

        self.assertEqual(result.file_name, "sample_part")
        self.assertEqual(result.schema_name, "AP242_MANAGED_MODEL_BASED_3D_ENGINEERING_MIM_LF")
        self.assertEqual(result.originating_system, "ORIGINATING CAD")
        self.assertEqual(result.product_names, ["SAMPLE"])
        self.assertEqual(result.solid_body_count, 1)
        self.assertEqual(result.advanced_face_count, 1)
        self.assertEqual(result.declared_length_unit, "mm")
        self.assertEqual(result.inferred_model_kind, "part")

    def test_incomplete_exchange_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "broken.step"
            path.write_text("ISO-10303-21;\nHEADER;", encoding="ascii")

            with self.assertRaisesRegex(CreoMcpError, "not a complete"):
                inspect_step_file(InspectStepRequest(step_path=str(path)))

    def test_import_uses_detected_model_type_and_does_not_save(self) -> None:
        session = _FakeSession()
        with tempfile.TemporaryDirectory() as raw_directory:
            path = self._write_step(Path(raw_directory))
            request = ImportStepRequest(
                step_path=str(path),
                model_name="sample_import",
                activate=False,
                confirmation="import_unsaved_model",
            )
            with patch(
                "creo_mcp.bridge._connected_session",
                return_value=_fake_connection(session),
            ):
                result = import_step(request)

        self.assertEqual(session.detected_import_type, 4)
        self.assertEqual(session.import_arguments[1:4], (4, 1, "sample_import"))
        self.assertEqual(result.detected_model_kind, "part")
        self.assertEqual(result.measured_extent_mm.model_dump(), {
            "x_mm": 10.0,
            "y_mm": 20.0,
            "z_mm": 30.0,
        })
        self.assertFalse(result.saved)

    def test_geometry_inspection_normalizes_extent_and_volume(self) -> None:
        session = _FakeSession()
        with patch(
            "creo_mcp.bridge._connected_session",
            return_value=_fake_connection(session),
        ):
            result = inspect_model_geometry(InspectModelGeometryRequest())

        self.assertEqual(result.model_kind, "part")
        self.assertEqual(result.measured_extent_mm.x_mm, 10.0)
        self.assertEqual(result.volume_mm3, 6000.0)


class CadImportTests(unittest.TestCase):
    def _request(
        self,
        path: Path,
        source_format: str = "auto",
    ) -> ImportCadRequest:
        return ImportCadRequest(
            source_path=str(path),
            source_format=source_format,
            model_name="sample_import",
            activate=False,
            confirmation="import_unsaved_model",
        )

    def test_auto_detects_supported_exchange_formats(self) -> None:
        expected_formats = {
            "sample.step": "step",
            "sample.iges": "iges",
            "sample.x_t": "parasolid",
            "sample.sat": "acis",
            "sample.stl": "stl",
            "sample.jt": "jt",
            "sample.CATPart": "catia_part",
            "sample.SLDASM": "solidworks_assembly",
            "sample.ipt": "inventor_part",
        }
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            for file_name, expected_format in expected_formats.items():
                with self.subTest(file_name=file_name):
                    path = directory / file_name
                    path.touch()
                    source = resolve_cad_source(self._request(path))
                    self.assertEqual(source.source_format, expected_format)

    def test_auto_rejects_ambiguous_prt_extension(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "sample.prt"
            path.touch()

            with self.assertRaisesRegex(CreoMcpError, "ambiguous"):
                resolve_cad_source(self._request(path))

    def test_explicit_nx_accepts_prt_extension(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "sample.prt"
            path.touch()

            source = resolve_cad_source(self._request(path, "nx"))

        self.assertEqual(source.source_format, "nx")
        self.assertEqual(source.geometry_representation, "brep")

    def test_explicit_format_rejects_wrong_extension(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "sample.stl"
            path.touch()

            with self.assertRaisesRegex(CreoMcpError, "expects one of"):
                resolve_cad_source(self._request(path, "step"))

    def test_generic_import_uses_format_enum_and_never_saves(self) -> None:
        session = _FakeSession()
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "sample.x_t"
            path.touch()
            with patch(
                "creo_mcp.bridge._connected_session",
                return_value=_fake_connection(session),
            ):
                result = import_cad(self._request(path))

        self.assertEqual(session.detected_import_type, 12)
        self.assertEqual(session.import_arguments[1:4], (12, 1, "sample_import"))
        self.assertEqual(result.source.source_format, "parasolid")
        self.assertEqual(result.detected_model_kind, "part")
        self.assertEqual(result.volume_mm3, 6000.0)
        self.assertFalse(result.saved)


if __name__ == "__main__":
    unittest.main()
