"""Tests for bounded, read-only Creo model structure discovery."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from creo_mcp.bridge import inspect_model_structure
from creo_mcp.models import InspectModelStructureRequest


class _FakeSequence:
    def __init__(self, items: list[object]) -> None:
        self._items = items
        self.Count = len(items)

    def Item(self, index: int) -> object:
        return self._items[index]


class _FakeNamedItem:
    def __init__(self, item_id: int, name: str | None) -> None:
        self.Id = item_id
        self._name = name

    def GetName(self) -> str:
        if self._name is None:
            raise RuntimeError("unnamed")
        return self._name


class _FakeFeature(_FakeNamedItem):
    def __init__(
        self,
        item_id: int,
        name: str,
        feature_type: int,
        feature_type_name: str,
        status: int,
    ) -> None:
        super().__init__(item_id, name)
        self.FeatType = feature_type
        self.FeatTypeName = feature_type_name
        self.Status = status


class _FakeParamValue:
    def __init__(self, value_type: int, member: str, value: object) -> None:
        self.discr = value_type
        setattr(self, member, value)


class _FakeParameter(_FakeNamedItem):
    Units = None

    def __init__(self, name: str, value: _FakeParamValue) -> None:
        super().__init__(0, name)
        self._value = value

    def GetScaledValue(self) -> _FakeParamValue:
        return self._value


class _FakeUnsetParameter(_FakeNamedItem):
    Units = None

    def GetScaledValue(self) -> _FakeParamValue:
        raise RuntimeError("not set")


class _FakeUnnamedParameter:
    Units = None

    def GetName(self) -> str:
        raise RuntimeError("unnamed")


class _FakeUnreadableValueParameter(_FakeNamedItem):
    Units = None

    def GetScaledValue(self) -> object:
        return type("UnreadableValue", (), {"discr": 0})()


class _FakeModel:
    FileName = "sample.prt"
    InstanceName = "SAMPLE"
    Type = 1

    def __init__(self) -> None:
        self.items = {
            0: [
                _FakeFeature(1, "FRONT", 13, "DATUM PLANE", 0),
                _FakeFeature(2, "EXTRUDE_1", 7, "PROTRUSION", 5),
            ],
            1: [_FakeNamedItem(10, None)],
            2: [_FakeNamedItem(11, None)],
            3: [_FakeNamedItem(20, "DEFAULT_CSYS")],
            4: [_FakeNamedItem(30, "A_1")],
            5: [],
            10: [],
        }
        self.parameters = [
            _FakeParameter("DESCRIPTION", _FakeParamValue(0, "StringValue", "sample")),
            _FakeParameter("REVISION", _FakeParamValue(1, "IntValue", 3)),
            _FakeUnsetParameter(0, "OPTIONAL_VALUE"),
            _FakeUnreadableValueParameter(0, "UNREADABLE_VALUE"),
            _FakeUnnamedParameter(),
        ]

    def ListItems(self, item_type: int) -> _FakeSequence | None:
        items = self.items[item_type]
        return _FakeSequence(items) if items else None

    def ListParams(self) -> _FakeSequence:
        return _FakeSequence(self.parameters)


class _FakeSession:
    def __init__(self, model: _FakeModel) -> None:
        self.CurrentModel = model


@contextmanager
def _fake_connection(session: _FakeSession):
    yield session, Path(r"D:\FixtureCreo\Creo13.4")


class ModelStructureTests(unittest.TestCase):
    def test_structure_returns_stable_feature_and_reference_metadata(self) -> None:
        model = _FakeModel()
        session = _FakeSession(model)
        with patch(
            "creo_mcp.bridge._connected_session",
            return_value=_fake_connection(session),
        ):
            result = inspect_model_structure(InspectModelStructureRequest())

        self.assertEqual(result.model_kind, "part")
        self.assertEqual(result.features[0].name, "FRONT")
        self.assertEqual(result.features[0].feature_type, 13)
        self.assertEqual(result.features[0].status_name, "active")
        self.assertEqual(result.features[1].status_name, "suppressed")
        self.assertEqual(result.surfaces[0].item_id, 10)
        self.assertEqual(result.edges[0].item_id, 11)
        self.assertEqual(result.axes[0].item_id, 30)
        self.assertEqual(result.coordinate_systems[0].name, "DEFAULT_CSYS")
        self.assertEqual(result.parameters[0].value, "sample")
        self.assertEqual(result.parameters[1].value, 3)
        self.assertEqual(result.parameters[2].value_type, "unset")
        self.assertIsNone(result.parameters[2].value)
        self.assertEqual(result.parameters[3].value_type, "unset")
        self.assertEqual(result.skipped_unnamed_parameter_count, 1)
        self.assertEqual(result.truncated_categories, [])

    def test_structure_limits_each_category_and_reports_truncation(self) -> None:
        model = _FakeModel()
        session = _FakeSession(model)
        with patch(
            "creo_mcp.bridge._connected_session",
            return_value=_fake_connection(session),
        ):
            result = inspect_model_structure(
                InspectModelStructureRequest(max_items_per_category=1)
            )

        self.assertEqual(len(result.features), 1)
        self.assertEqual(len(result.parameters), 1)
        self.assertEqual(result.truncated_categories, ["features", "parameters"])


if __name__ == "__main__":
    unittest.main()
