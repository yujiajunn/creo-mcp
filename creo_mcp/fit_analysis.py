"""Offline STEP geometry utilities used by hard-edge product trays."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .concept_geometry import _ocp
from .errors import CreoMcpError
from .models import (
    BoxExtent,
    InspectStepRequest,
    InspectStepSolidsResult,
    ProductOrientation,
    StepPoint3D,
    StepSolidBounds,
    StepSolidGeometry,
    StepSolidRelationship,
)
from .step import inspect_step_file, resolve_step_path

_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}
_GEOMETRY_TOLERANCE_MM = 1e-5


def _fit_ocp() -> dict[str, Any]:
    api = _ocp()
    try:
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.BRepExtrema import BRepExtrema_DistShapeShape
        from OCP.BRepGProp import BRepGProp
        from OCP.gp import gp_Trsf, gp_Vec
        from OCP.GProp import GProp_GProps
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer
    except ImportError as exc:
        raise CreoMcpError(
            "Concept fit analysis requires the optional 'concept-geometry' extra."
        ) from exc
    api.update(
        {
            "BRepBuilderAPI_Transform": BRepBuilderAPI_Transform,
            "BRepExtrema_DistShapeShape": BRepExtrema_DistShapeShape,
            "BRepGProp": BRepGProp,
            "GProp_GProps": GProp_GProps,
            "IFSelect_RetDone": IFSelect_RetDone,
            "STEPControl_Reader": STEPControl_Reader,
            "TopAbs_SOLID": TopAbs_SOLID,
            "TopExp_Explorer": TopExp_Explorer,
            "gp_Trsf": gp_Trsf,
            "gp_Vec": gp_Vec,
        }
    )
    return api


def _load_step_shape(path: Path, api: dict[str, Any]) -> tuple[Any, int]:
    reader = api["STEPControl_Reader"]()
    if reader.ReadFile(str(path)) != api["IFSelect_RetDone"]:
        raise CreoMcpError(f"OpenCascade could not read STEP geometry: {path}")
    if reader.TransferRoots() <= 0:
        raise CreoMcpError(f"OpenCascade could not transfer STEP geometry: {path}")
    shape = reader.OneShape()
    if shape.IsNull():
        raise CreoMcpError(f"STEP file contains no transferable geometry: {path}")

    explorer = api["TopExp_Explorer"](shape, api["TopAbs_SOLID"])
    solid_count = 0
    while explorer.More():
        solid_count += 1
        explorer.Next()
    if solid_count == 0:
        raise CreoMcpError(f"STEP file contains no solid product bodies: {path}")
    return shape, solid_count


def _bounds(shape: Any, api: dict[str, Any]) -> tuple[float, float, float, float, float, float]:
    bounds = api["Bnd_Box"]()
    api["BRepBndLib"].Add_s(shape, bounds)
    if bounds.IsVoid():
        raise CreoMcpError("Cannot measure an empty STEP shape.")
    return bounds.Get()


def _volume(shape: Any, api: dict[str, Any]) -> float:
    properties = api["GProp_GProps"]()
    api["BRepGProp"].VolumeProperties_s(shape, properties)
    return max(0.0, float(properties.Mass()))


def _orientation_matrix(orientation: ProductOrientation) -> list[list[int]]:
    rows: list[list[int]] = []
    for token in (
        orientation.tray_x_from,
        orientation.tray_y_from,
        orientation.tray_z_from,
    ):
        row = [0, 0, 0]
        row[_AXIS_INDEX[token.removeprefix("-")]] = -1 if token.startswith("-") else 1
        rows.append(row)
    return rows


def _transform(shape: Any, matrix: list[list[int]], api: dict[str, Any]) -> Any:
    transform = api["gp_Trsf"]()
    transform.SetValues(
        matrix[0][0], matrix[0][1], matrix[0][2], 0.0,
        matrix[1][0], matrix[1][1], matrix[1][2], 0.0,
        matrix[2][0], matrix[2][1], matrix[2][2], 0.0,
    )
    return api["BRepBuilderAPI_Transform"](shape, transform, True).Shape()


def _translate(shape: Any, x: float, y: float, z: float, api: dict[str, Any]) -> Any:
    transform = api["gp_Trsf"]()
    transform.SetTranslation(api["gp_Vec"](x, y, z))
    return api["BRepBuilderAPI_Transform"](shape, transform, True).Shape()


def inspect_step_solids(request: InspectStepRequest) -> InspectStepSolidsResult:
    """Measure every solid in a millimetre STEP without changing the source file."""
    path = resolve_step_path(request.step_path)
    step_info = inspect_step_file(InspectStepRequest(step_path=str(path)))
    if step_info.declared_length_unit != "mm":
        raise CreoMcpError(
            "Per-solid STEP analysis currently requires a file declared in millimetres."
        )

    api = _fit_ocp()
    shape, solid_count = _load_step_shape(path, api)
    explorer = api["TopExp_Explorer"](shape, api["TopAbs_SOLID"])
    solids: list[StepSolidGeometry] = []
    solid_shapes: list[Any] = []
    while explorer.More():
        solid = explorer.Current()
        properties = api["GProp_GProps"]()
        api["BRepGProp"].VolumeProperties_s(solid, properties)
        volume = max(0.0, float(properties.Mass()))
        if volume <= 0:
            raise CreoMcpError(
                f"STEP solid {len(solids) + 1} has no measurable volume: {path}"
            )
        x_min, y_min, z_min, x_max, y_max, z_max = _bounds(solid, api)
        center = properties.CentreOfMass()
        solid_shapes.append(solid)
        solids.append(
            StepSolidGeometry(
                solid_index=len(solids) + 1,
                bounds_mm=StepSolidBounds(
                    minimum=StepPoint3D(x_mm=x_min, y_mm=y_min, z_mm=z_min),
                    maximum=StepPoint3D(x_mm=x_max, y_mm=y_max, z_mm=z_max),
                ),
                extent_mm=BoxExtent(
                    x_mm=x_max - x_min,
                    y_mm=y_max - y_min,
                    z_mm=z_max - z_min,
                ),
                center_of_mass_mm=StepPoint3D(
                    x_mm=float(center.X()),
                    y_mm=float(center.Y()),
                    z_mm=float(center.Z()),
                ),
                volume_mm3=volume,
            )
        )
        explorer.Next()

    relationships: list[StepSolidRelationship] = []
    for first_index, first in enumerate(solid_shapes, start=1):
        for second_index in range(first_index + 1, len(solid_shapes) + 1):
            distance = api["BRepExtrema_DistShapeShape"](
                first,
                solid_shapes[second_index - 1],
            )
            distance.Perform()
            if not distance.IsDone():
                raise CreoMcpError(
                    f"Could not measure distance between STEP solids {first_index} "
                    f"and {second_index}."
                )
            minimum_distance = max(0.0, float(distance.Value()))
            relationships.append(
                StepSolidRelationship(
                    first_solid_index=first_index,
                    second_solid_index=second_index,
                    minimum_distance_mm=minimum_distance,
                    intersects_or_touches=minimum_distance <= _GEOMETRY_TOLERANCE_MM,
                )
            )
    return InspectStepSolidsResult(
        step_path=str(path),
        declared_length_unit="mm",
        solid_count=solid_count,
        solids=solids,
        relationships=relationships,
    )
