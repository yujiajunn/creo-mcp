"""Tests for strict UDF qualification and guarded placement."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("CREO_MCP_DISPLAY_MODE", "display")

from creo_mcp.bridge import place_udf, run_udf_qualification
from creo_mcp.errors import CreoMcpError
from creo_mcp.models import (
    PlaceUdfRequest,
    RunUdfQualificationRequest,
    UdfManifest,
    ValidateUdfManifestRequest,
)
from creo_mcp.udf import load_qualified_udf, validate_udf_manifest


class _FakeSequence:
    def __init__(self, items: list[object]) -> None:
        self._items = items
        self.Count = len(items)

    def Item(self, index: int) -> object:
        return self._items[index]

    def Set(self, index: int, value: object) -> None:
        if index == len(self._items):
            self._items.append(value)
        else:
            self._items[index] = value


class _FakeFactory:
    def __init__(self, creator):
        self._creator = creator

    def Create(self, *arguments: object) -> object:
        return self._creator(*arguments)


class _FakeItem:
    def __init__(self, item_id: int, name: str | None = None) -> None:
        self.Id = item_id
        self._name = name

    def GetName(self) -> str:
        if self._name is None:
            raise RuntimeError("unnamed")
        return self._name


class _FakeFeature(_FakeItem):
    FeatType = 7
    FeatTypeName = "PROTRUSION"
    Status = 0


class _FakeGroup:
    GroupName = "NODE_GROUP"

    def ListMembers(self) -> _FakeSequence:
        return _FakeSequence([_FakeFeature(101, "NODE")])


class _FakeUnit:
    Name = "cm"


class _FakeUnitSystem:
    def GetUnit(self, _unit_type: int) -> _FakeUnit:
        return _FakeUnit()


class _FakeModel:
    FileName = "target.prt"
    InstanceName = "TARGET"
    Type = 1

    def __init__(self) -> None:
        self.instructions = None
        self.regenerated = False

    def GetItemById(self, item_type: int, item_id: int) -> _FakeItem | None:
        if (item_type, item_id) == (3, 20):
            return _FakeItem(20, "DEFAULT_CSYS")
        return None

    def GetPrincipalUnits(self) -> _FakeUnitSystem:
        return _FakeUnitSystem()

    def CreateUDFGroup(self, instructions: object) -> _FakeGroup:
        self.instructions = instructions
        return _FakeGroup()

    def Regenerate(self, _instructions: object) -> None:
        self.regenerated = True

    def EvalOutline(self, _transform: object, _excluded: object) -> _FakeSequence:
        return _FakeSequence([(0.0, 0.0, 0.0), (1.0, 2.0, 3.0)])

    def GetMassProperty(self, _coordinate_system: object) -> object:
        return SimpleNamespace(Volume=6.0)


class _FakeWindow:
    def __init__(self) -> None:
        self.repaint_count = 0

    def Repaint(self) -> None:
        self.repaint_count += 1


class _FakeSession:
    def __init__(self) -> None:
        self.CurrentModel = _FakeModel()
        self.CurrentWindow = _FakeWindow()
        self.directory = r"D:\CreoWork"
        self.directory_changes: list[str] = []

    def GetCurrentDirectory(self) -> str:
        return self.directory

    def ChangeDirectory(self, directory: str) -> None:
        self.directory = directory
        self.directory_changes.append(directory)


@contextmanager
def _fake_connection(session: _FakeSession):
    yield session, Path(r"D:\FixtureCreo\Creo13.4\Creo 13.4.1.0")


def _manifest_payload(
    sha256: str, *, verified_non_interactive: bool = True
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "udf_name": "node",
        "udf_file": "node.gph",
        "sha256": sha256,
        "qualified_creo_versions": ["13.4.1.0"],
        "allowed_model_kinds": ["part"],
        "verified_non_interactive": verified_non_interactive,
        "references": [{"prompt": "REF_CSYS", "item_type": "coordinate_system"}],
        "variant_dimensions": [
            {"name": "d11", "unit": "mm", "minimum": 1.0, "maximum": 100.0}
        ],
    }


class UdfQualificationTests(unittest.TestCase):
    def _write_manifest(
        self, directory: Path, *, verified_non_interactive: bool = True
    ) -> Path:
        udf_path = directory / "node.gph"
        udf_path.write_bytes(b"qualified udf fixture")
        digest = hashlib.sha256(udf_path.read_bytes()).hexdigest()
        manifest_path = directory / "node.udf.json"
        manifest_path.write_text(
            json.dumps(
                _manifest_payload(
                    digest,
                    verified_non_interactive=verified_non_interactive,
                )
            ),
            encoding="utf-8",
        )
        return manifest_path

    def test_validates_relative_udf_path_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            manifest_path = self._write_manifest(Path(raw_directory))
            result = validate_udf_manifest(
                ValidateUdfManifestRequest(manifest_path=str(manifest_path))
            )

        self.assertEqual(result.manifest.udf_name, "node")
        self.assertEqual(result.sha256, result.manifest.sha256)
        self.assertEqual(len(result.validation_id), 64)

    def test_rejects_tampered_udf(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            manifest_path = self._write_manifest(Path(raw_directory))
            (manifest_path.parent / "node.gph").write_bytes(b"tampered")
            with self.assertRaisesRegex(CreoMcpError, "SHA-256 mismatch"):
                load_qualified_udf(str(manifest_path))

    def test_candidate_validates_offline_but_is_rejected_for_production(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            manifest_path = self._write_manifest(
                Path(raw_directory), verified_non_interactive=False
            )
            result = validate_udf_manifest(
                ValidateUdfManifestRequest(manifest_path=str(manifest_path))
            )
            self.assertFalse(result.manifest.verified_non_interactive)
            with self.assertRaisesRegex(CreoMcpError, "qualification candidate"):
                load_qualified_udf(str(manifest_path))


class UdfPlacementTests(unittest.TestCase):
    def _qualified(self, *, verified_non_interactive: bool = True) -> SimpleNamespace:
        manifest = UdfManifest.model_validate(
            _manifest_payload(
                "a" * 64,
                verified_non_interactive=verified_non_interactive,
            )
        )
        return SimpleNamespace(
            manifest=manifest,
            validation_id="b" * 64,
            udf_path=Path(r"D:\QualifiedUdfs\node.gph"),
        )

    def _request(self) -> PlaceUdfRequest:
        return PlaceUdfRequest(
            manifest_path=r"D:\QualifiedUdfs\node.udf.json",
            validation_id="b" * 64,
            references=[
                {
                    "prompt": "REF_CSYS",
                    "item_type": "coordinate_system",
                    "item_id": 20,
                }
            ],
            variant_dimensions=[{"name": "d11", "value": 25.4}],
            confirmation="place_qualified_udf",
        )

    def _qualification_request(self) -> RunUdfQualificationRequest:
        return RunUdfQualificationRequest(
            manifest_path=r"D:\QualifiedUdfs\node.udf.json",
            validation_id="b" * 64,
            references=[
                {
                    "prompt": "REF_CSYS",
                    "item_type": "coordinate_system",
                    "item_id": 20,
                }
            ],
            variant_dimensions=[{"name": "d11", "value": 25.4}],
            confirmation="run_udf_qualification",
        )

    def _factory(self, prog_id: str) -> object:
        if prog_id in {"pfcls.pfcUDFReferences", "pfcls.pfcUDFVariantValues"}:
            return _FakeSequence([])
        if prog_id == "pfcls.MpfcSelect":
            return SimpleNamespace(
                CreateModelItemSelection=lambda item, path: SimpleNamespace(
                    item=item, path=path
                )
            )
        if prog_id == "pfcls.pfcUDFReference":
            return _FakeFactory(
                lambda prompt, selection: SimpleNamespace(
                    PromptForReference=prompt,
                    ReferenceItem=selection,
                    IsExternal=None,
                )
            )
        if prog_id == "pfcls.pfcUDFVariantDimension":
            return _FakeFactory(
                lambda name, value: SimpleNamespace(Name=name, DimensionValue=value)
            )
        if prog_id == "pfcls.pfcUDFCustomCreateInstructions":
            return _FakeFactory(lambda name: SimpleNamespace(Name=name))
        raise AssertionError(prog_id)

    def test_places_complete_contract_regenerates_and_never_saves(self) -> None:
        session = _FakeSession()
        with (
            patch("creo_mcp.bridge.load_qualified_udf", return_value=self._qualified()),
            patch(
                "creo_mcp.bridge._connected_session",
                return_value=_fake_connection(session),
            ),
            patch("creo_mcp.bridge._dispatch_factory", side_effect=self._factory),
        ):
            result = place_udf(self._request())

        self.assertTrue(session.CurrentModel.regenerated)
        self.assertEqual(session.CurrentWindow.repaint_count, 1)
        variant = session.CurrentModel.instructions.VariantValues.Item(0)
        self.assertEqual(variant.DimensionValue, 2.54)
        self.assertEqual(result.group_members[0].item_id, 101)
        self.assertEqual(result.measured_extent_mm.z_mm, 30.0)
        self.assertEqual(result.volume_mm3, 6000.0)
        self.assertFalse(result.saved)
        self.assertEqual(session.directory, r"D:\CreoWork")

    def test_candidate_run_does_not_grant_qualification_or_save(self) -> None:
        session = _FakeSession()
        with (
            patch(
                "creo_mcp.bridge.load_udf_manifest",
                return_value=self._qualified(verified_non_interactive=False),
            ),
            patch(
                "creo_mcp.bridge._connected_session",
                return_value=_fake_connection(session),
            ),
            patch("creo_mcp.bridge._dispatch_factory", side_effect=self._factory),
        ):
            result = run_udf_qualification(self._qualification_request())

        self.assertTrue(session.CurrentModel.regenerated)
        self.assertEqual(session.CurrentWindow.repaint_count, 1)
        self.assertEqual(result.qualification_state, "candidate_run_completed")
        self.assertFalse(result.manifest_verified_non_interactive)
        self.assertFalse(result.saved)
        self.assertEqual(session.directory, r"D:\CreoWork")

    def test_rejects_incomplete_bindings_before_creo_mutation(self) -> None:
        session = _FakeSession()
        request = self._request().model_copy(update={"references": []})
        with (
            patch("creo_mcp.bridge.load_qualified_udf", return_value=self._qualified()),
            patch(
                "creo_mcp.bridge._connected_session",
                return_value=_fake_connection(session),
            ),
            patch("creo_mcp.bridge._dispatch_factory") as dispatch,
        ):
            with self.assertRaisesRegex(CreoMcpError, "exactly match"):
                place_udf(request)

        dispatch.assert_not_called()
        self.assertIsNone(session.CurrentModel.instructions)


if __name__ == "__main__":
    unittest.main()
