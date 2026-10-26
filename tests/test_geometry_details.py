"""Tests for targeted Creo surface and edge geometry inspection."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from creo_mcp.bridge import find_surfaces, inspect_geometry_details
from creo_mcp.errors import CreoMcpError
from creo_mcp.models import FindSurfacesRequest, InspectGeometryDetailsRequest


class _FakeSequence:
    def __init__(self, items: list[object]) -> None:
        self._items = items
        self.Count = len(items)

    def Item(self, index: int) -> object:
        return self._items[index]


class _FakeFeature:
    Id = 5
    FeatType = 22
    FeatTypeName = "IMPORT FEATURE"
    Status = 0

    def GetName(self) -> None:
        return None


class _FakeSurface:
    Id = 42
    IsVisible = True

    def GetName(self) -> None:
        return None

    def GetSurfaceType(self) -> int:
        return 0

    def EvalArea(self) -> float:
        return 2.0

    def GetXYZExtents(self) -> _FakeSequence:
        return _FakeSequence([(0.0, 0.0, 0.0), (1.0, 0.0, 3.0)])

    def GetFeature(self) -> _FakeFeature:
        return _FakeFeature()


class _FakeCurveDescriptor:
    def GetCurveType(self) -> int:
        return 2


class _FakeEdge:
    Id = 84
    IsVisible = True

    def __init__(self, surface: _FakeSurface) -> None:
        self.Surface1 = surface
        self.Surface2 = None

    def GetName(self) -> None:
        return None

    def GetCurveDescriptor(self) -> _FakeCurveDescriptor:
        return _FakeCurveDescriptor()

    def EvalLength(self) -> float:
        return 2.5

    def Eval3DData(self, parameter: float) -> object:
        return SimpleNamespace(Point=(parameter, parameter * 2, parameter * 3))

    def GetFeature(self) -> _FakeFeature:
        return _FakeFeature()


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
        self.surface = _FakeSurface()
        self.edge = _FakeEdge(self.surface)

    def GetItemById(self, item_type: int, item_id: int) -> object | None:
        return {(1, 42): self.surface, (2, 84): self.edge}.get((item_type, item_id))

    def GetPrincipalUnits(self) -> _FakeUnitSystem:
        return _FakeUnitSystem()

    def ListItems(self, item_type: int) -> _FakeSequence | None:
        if item_type == 1:
            return _FakeSequence([self.surface])
        return None


class _FakeSession:
    def __init__(self) -> None:
        self.CurrentModel = _FakeModel()


@contextmanager
def _fake_connection(session: _FakeSession):
    yield session, Path(r"D:\PTC\Creo13.4\Creo 13.4.1.0")


class GeometryDetailsTests(unittest.TestCase):
    def test_returns_surface_and_edge_geometry_in_millimetres(self) -> None:
        session = _FakeSession()
        with patch(
            "creo_mcp.bridge._connected_session",
            return_value=_fake_connection(session),
        ):
            result = inspect_geometry_details(
                InspectGeometryDetailsRequest(surface_ids=[42], edge_ids=[84])
            )

        surface = result.surfaces[0]
        self.assertEqual(surface.surface_type_name, "plane")
        self.assertEqual(surface.area_mm2, 200.0)
        self.assertEqual(surface.bounds_mm.maximum.z_mm, 30.0)
        self.assertEqual(surface.feature.item_id, 5)
        edge = result.edges[0]
        self.assertEqual(edge.curve_type_name, "line")
        self.assertEqual(edge.length_mm, 25.0)
        self.assertEqual(edge.midpoint_mm.y_mm, 10.0)
        self.assertEqual(edge.adjacent_surface_ids, [42])
        self.assertEqual(result.length_unit, "cm")

    def test_finds_constant_coordinate_planes_without_topology_dump(self) -> None:
        session = _FakeSession()
        with patch(
            "creo_mcp.bridge._connected_session",
            return_value=_fake_connection(session),
        ):
            result = find_surfaces(
                FindSurfacesRequest(
                    surface_types=["plane"],
                    minimum_area_mm2=100.0,
                    constant_axis="y",
                    axis_coordinate_mm=0.0,
                    max_results=5,
                )
            )

        self.assertEqual(result.scanned_surface_count, 1)
        self.assertEqual(result.matched_surface_count, 1)
        self.assertEqual(result.surfaces[0].item_id, 42)
        self.assertFalse(result.truncated)

    def test_rejects_missing_ids_before_geometry_evaluation(self) -> None:
        session = _FakeSession()
        with patch(
            "creo_mcp.bridge._connected_session",
            return_value=_fake_connection(session),
        ), self.assertRaisesRegex(CreoMcpError, "surface ID 999"):
            inspect_geometry_details(
                InspectGeometryDetailsRequest(surface_ids=[999], edge_ids=[84])
            )


if __name__ == "__main__":
    unittest.main()
