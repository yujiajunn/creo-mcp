"""Unit tests for template-based native Creo modelling logic."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creo_mcp.bridge import (
    _apply_box_dimensions,
    _ensure_new_part_name,
    _native_descriptor_file_name,
    _native_descriptor_name,
    _retrieve_part,
    _resolve_native_model_path,
    _validate_extent,
)
from creo_mcp.errors import CreoMcpError
from creo_mcp.models import (
    BoxExtent,
    CreatePackagingBoxRequest,
    PackagingBoxPlanRequest,
    create_packaging_box_plan,
)


class _FakeDimension:
    def __init__(self, symbol: str, value: float) -> None:
        self.Symbol = symbol
        self.DimValue = value


class _FakeSequence:
    def __init__(self, items: list[_FakeDimension]) -> None:
        self._items = items
        self.Count = len(items)

    def Item(self, index: int) -> _FakeDimension:
        return self._items[index]


class _FakeModel:
    def __init__(self, dimensions: list[_FakeDimension]) -> None:
        self.dimensions = dimensions

    def ListItems(self, _item_type: int) -> _FakeSequence:
        return _FakeSequence(self.dimensions)


class _FakeDescriptor:
    Path = ""


class _FakeDescriptorFactory:
    def __init__(self) -> None:
        self.model_type = None
        self.model_name = ""
        self.instance_name = object()
        self.file_name = None
        self.descriptor = _FakeDescriptor()

    def Create(
        self,
        model_type: int,
        model_name: str,
        instance_name: str | None,
    ) -> _FakeDescriptor:
        self.model_type = model_type
        self.model_name = model_name
        self.instance_name = instance_name
        return self.descriptor

    def CreateFromFileName(self, file_name: str) -> _FakeDescriptor:
        self.file_name = file_name
        return self.descriptor


class _FakeOptions:
    AskUserAboutReps = True


class _FakeOptionsFactory:
    def __init__(self) -> None:
        self.options = _FakeOptions()

    def Create(self) -> _FakeOptions:
        return self.options


class _FakeSession:
    def __init__(self) -> None:
        self.descriptor = None
        self.options = None
        self.model = object()

    def RetrieveModelWithOpts(self, descriptor: object, options: object) -> object:
        self.descriptor = descriptor
        self.options = options
        return self.model


class _FallbackSession(_FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.directory = r"D:\CreoWork"
        self.directory_changes: list[str] = []

    def RetrieveModelWithOpts(self, descriptor: object, options: object) -> object:
        raise RuntimeError("explicit path not found")

    def GetCurrentDirectory(self) -> str:
        return self.directory

    def ChangeDirectory(self, directory: str) -> None:
        self.directory = directory
        self.directory_changes.append(directory)

    def RetrieveModel(self, descriptor: object) -> object:
        self.descriptor = descriptor
        return self.model


def _create_request() -> CreatePackagingBoxRequest:
    planning_request = PackagingBoxPlanRequest(
        length_mm=300,
        width_mm=200,
        height_mm=120,
        part_name="sample_box",
    )
    plan = create_packaging_box_plan(planning_request)
    return CreatePackagingBoxRequest(
        length_mm=planning_request.length_mm,
        width_mm=planning_request.width_mm,
        height_mm=planning_request.height_mm,
        part_name=planning_request.part_name,
        template_path=r"D:\templates\packaging_box.prt",
        plan_id=plan.plan_id,
        confirmation="create_new_model",
    )


class TemplateBridgeTests(unittest.TestCase):
    def test_retrieve_part_populates_file_name_and_absolute_path(self) -> None:
        descriptor_factory = _FakeDescriptorFactory()
        options_factory = _FakeOptionsFactory()
        session = _FakeSession()
        part_path = Path(r"D:\templates\packaging_box.prt")

        with patch(
            "creo_mcp.bridge._dispatch_factory",
            side_effect=[descriptor_factory, options_factory],
        ):
            model = _retrieve_part(session, part_path)

        self.assertIs(model, session.model)
        self.assertEqual(descriptor_factory.model_type, 1)
        self.assertEqual(descriptor_factory.model_name, "packaging_box")
        self.assertIsNone(descriptor_factory.instance_name)
        self.assertEqual(descriptor_factory.descriptor.Path, str(part_path))
        self.assertFalse(options_factory.options.AskUserAboutReps)

    def test_retrieve_part_falls_back_to_official_working_directory_pattern(self) -> None:
        descriptor_factory = _FakeDescriptorFactory()
        options_factory = _FakeOptionsFactory()
        session = _FallbackSession()
        part_path = Path(r"D:\templates\plate.prt")

        with patch(
            "creo_mcp.bridge._dispatch_factory",
            side_effect=[descriptor_factory, options_factory],
        ):
            model = _retrieve_part(session, part_path)

        self.assertIs(model, session.model)
        self.assertEqual(descriptor_factory.file_name, "plate.prt")
        self.assertEqual(session.directory_changes, [str(part_path.parent), r"D:\CreoWork"])
        self.assertEqual(session.directory, r"D:\CreoWork")

    def test_applies_dimensions_by_symbol_case_insensitively(self) -> None:
        dimensions = [
            _FakeDimension("box_length", 1),
            _FakeDimension("BOX_WIDTH", 1),
            _FakeDimension("Box_Height", 1),
        ]

        _apply_box_dimensions(_FakeModel(dimensions), _create_request())

        self.assertEqual([dimension.DimValue for dimension in dimensions], [300, 200, 120])

    def test_reports_available_dimensions_when_template_mapping_is_missing(self) -> None:
        model = _FakeModel([_FakeDimension("d0", 10), _FakeDimension("d1", 20)])

        with self.assertRaisesRegex(CreoMcpError, "Available dimensions"):
            _apply_box_dimensions(model, _create_request())

    def test_extent_validation_accepts_any_axis_orientation(self) -> None:
        _validate_extent(BoxExtent(x_mm=120, y_mm=300, z_mm=200), _create_request())

    def test_extent_validation_rejects_wrong_geometry(self) -> None:
        with self.assertRaisesRegex(CreoMcpError, "does not match"):
            _validate_extent(BoxExtent(x_mm=100, y_mm=200, z_mm=300), _create_request())

    def test_existing_versioned_part_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            (directory / "sample_box.prt.1").touch()

            with self.assertRaisesRegex(CreoMcpError, "Refusing to overwrite"):
                _ensure_new_part_name(directory, "sample_box")

    def test_native_model_path_supports_parts_assemblies_and_versions(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            directory = Path(raw_directory)
            part = directory / "sample.prt"
            assembly = directory / "sample.asm.3"
            part.touch()
            assembly.touch()

            resolved_part, part_type, part_kind = _resolve_native_model_path(str(part))
            resolved_assembly, assembly_type, assembly_kind = _resolve_native_model_path(
                str(assembly)
            )

        self.assertEqual(resolved_part, part)
        self.assertEqual((part_type, part_kind), (1, "part"))
        self.assertEqual(resolved_assembly, assembly)
        self.assertEqual((assembly_type, assembly_kind), (0, "assembly"))

    def test_native_descriptor_name_strips_type_and_version_suffixes(self) -> None:
        self.assertEqual(_native_descriptor_name(Path("sample.prt")), "sample")
        self.assertEqual(_native_descriptor_name(Path("sample.asm.12")), "sample")
        self.assertEqual(_native_descriptor_file_name(Path("sample.prt")), "sample.prt")
        self.assertEqual(_native_descriptor_file_name(Path("sample.asm.12")), "sample.asm")


if __name__ == "__main__":
    unittest.main()
