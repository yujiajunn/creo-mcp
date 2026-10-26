"""A constrained, JSON-only geometry program that builds one solid by boolean composition.

This module is deliberately NOT a scripting surface. The accepted vocabulary is a
fixed set of primitives (box, cylinder, sphere), boolean operations (union,
difference, intersect), a mirror, and a linear pattern, all in millimetres.
The result must validate as exactly one closed solid within the size limits,
after which the caller may export it to STEP and import it into Creo through
the same guarded path used by the clearance-tray pipeline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from .concept_geometry import measure_shape_extent
from .errors import CreoMcpError
from .models import BoxExtent, CreoModelInfo
from .step import resolve_step_path

MAX_DEPTH = 8
MAX_CHILDREN = 32
MAX_NODES = 200
MAX_EXTENT_MM = 2000.0

# Each node carries only its own numbers; every bound is checked at build time.
AxisToken = Literal["x", "y", "z"]


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Vec3(BaseModel):
    """A millimetre coordinate used as a box corner or a feature base point."""

    model_config = ConfigDict(extra="forbid")

    x_mm: float = 0.0
    y_mm: float = 0.0
    z_mm: float = 0.0

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x_mm, self.y_mm, self.z_mm)


class BoxNode(_Node):
    op: Literal["box"]
    length_mm: float = Field(gt=0, le=MAX_EXTENT_MM)
    width_mm: float = Field(gt=0, le=MAX_EXTENT_MM)
    height_mm: float = Field(gt=0, le=MAX_EXTENT_MM)
    at: Vec3 = Field(default_factory=Vec3, description="Corner at the minimum bounds.")


class CylinderNode(_Node):
    op: Literal["cylinder"]
    radius_mm: float = Field(gt=0, le=MAX_EXTENT_MM / 2)
    height_mm: float = Field(gt=0, le=MAX_EXTENT_MM)
    axis: AxisToken = Field(default="z", description="Extrusion direction from the base point.")
    at: Vec3 = Field(default_factory=Vec3, description="Center of the base circle.")


class SphereNode(_Node):
    op: Literal["sphere"]
    radius_mm: float = Field(gt=0, le=MAX_EXTENT_MM / 2)
    at: Vec3 = Field(default_factory=Vec3, description="Sphere center.")


class BooleanNode(_Node):
    op: Literal["union", "difference", "intersect"]
    children: list[GeometryNode] = Field(min_length=2, max_length=MAX_CHILDREN)


class MirrorNode(_Node):
    op: Literal["mirror"]
    plane: Literal["xy", "xz", "yz"]
    child: GeometryNode


class PatternNode(_Node):
    op: Literal["linear_pattern"]
    count: int = Field(ge=2, le=32)
    step: Vec3 = Field(description="Translation applied to each successive copy.")
    child: GeometryNode


class SourceNode(_Node):
    op: Literal["source"]
    path: str = Field(
        description=(
            "Absolute path to an existing millimetre .step/.stp file whose solid takes "
            "part in the booleans as-is."
        )
    )
    solid_index: int = Field(
        default=1,
        ge=1,
        le=64,
        description="1-based index of the solid to use when the file holds several.",
    )


class TransformNode(_Node):
    op: Literal["transform"]
    child: GeometryNode
    translate: Vec3 = Field(default_factory=Vec3)
    rotate_axis: AxisToken | None = None
    rotate_angle_deg: float | None = Field(default=None, ge=-360.0, le=360.0)
    rotate_about: Vec3 = Field(default_factory=Vec3)

    @model_validator(mode="after")
    def _rotation_needs_axis(self) -> TransformNode:
        if (self.rotate_axis is None) != (self.rotate_angle_deg is None):
            raise ValueError(
                "rotate_axis and rotate_angle_deg must be provided together."
            )
        return self


GeometryNode = Annotated[
    BoxNode | CylinderNode | SphereNode | BooleanNode | MirrorNode | PatternNode | SourceNode | TransformNode,
    Field(discriminator="op"),
]

BooleanNode.model_rebuild()
MirrorNode.model_rebuild()
PatternNode.model_rebuild()
TransformNode.model_rebuild()

_GEOMETRY_NODE_ADAPTER: TypeAdapter = TypeAdapter(GeometryNode)


class BuildGeometryRequest(BaseModel):
    """Request to build one JSON-program solid and import it as a new saved Creo part."""

    model_config = ConfigDict(extra="forbid")

    program: GeometryNode = Field(
        description=(
            "Geometry program: box/cylinder/sphere primitives plus source (an existing "
            "millimetre STEP file used as an operand) combined with "
            "union/difference/intersect, mirror, linear_pattern, and transform "
            "(rotate about an axis + translate), all in millimetres. The program must "
            "resolve to exactly one closed solid within 2000 mm."
        )
    )
    part_name: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
        description="New Creo part name without an extension; it must not already exist.",
    )
    output_directory: str = Field(
        description="Absolute existing directory where the new .prt file will be saved."
    )
    activate: bool = Field(
        default=True,
        description="Whether to open the saved part through Creo's normal file-open path.",
    )
    confirmation: Literal["build_geometry"] = Field(
        description="Must be exactly 'build_geometry' to authorize this write operation."
    )


class BuildGeometryResult(BaseModel):
    """Result of importing a geometry-program solid as a new saved Creo part."""

    model_config = ConfigDict(extra="forbid")

    created_model: CreoModelInfo = Field(description="Metadata for the new Creo part.")
    model_path: str = Field(description="Saved native .prt path.")
    working_directory: str = Field(description="Creo working directory used for the save.")
    saved: Literal[True] = Field(description="The new part was saved under a fresh name.")
    activated: bool = Field(description="Whether the part window was opened.")
    node_count: int = Field(description="Number of geometry program nodes evaluated.")
    program_sha256: str = Field(description="Hash of the canonical geometry program JSON.")
    measured_extent_mm: BoxExtent = Field(description="Creo-measured envelope of the new part.")
    volume_mm3: float = Field(
        description=(
            "Solid volume computed by the geometry kernel (Creo reports null volume for "
            "imported BREP features)."
        )
    )
    generated_step_deleted: Literal[True] = Field(
        description="The intermediate STEP file was removed after import."
    )
    model_state: Literal["GENERATED_SOLID"] = Field(
        description="The part is one imported solid body, not parametric sketch features."
    )


@dataclass(frozen=True)
class GeometryBuildOutcome:
    """The validated single solid plus its program statistics."""

    shape: Any
    node_count: int
    measured_extent_mm: tuple[float, float, float]
    volume_mm3: float


def _api() -> dict[str, Any]:
    try:
        from OCP.BRepAlgoAPI import (
            BRepAlgoAPI_Common,
            BRepAlgoAPI_Cut,
            BRepAlgoAPI_Fuse,
        )
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.BRepCheck import BRepCheck_Analyzer
        from OCP.BRepGProp import BRepGProp
        from OCP.BRepPrimAPI import (
            BRepPrimAPI_MakeBox,
            BRepPrimAPI_MakeCylinder,
            BRepPrimAPI_MakeSphere,
        )
        from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
        from OCP.GProp import GProp_GProps
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_Reader
        from OCP.TopAbs import TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer
    except ImportError as exc:
        raise CreoMcpError(
            "The build_geometry tool requires the optional 'concept-geometry' extra. "
            "Install this project with creo-mcp[concept-geometry]."
        ) from exc
    return {
        "BRepAlgoAPI_Common": BRepAlgoAPI_Common,
        "BRepAlgoAPI_Cut": BRepAlgoAPI_Cut,
        "BRepAlgoAPI_Fuse": BRepAlgoAPI_Fuse,
        "BRepBuilderAPI_Transform": BRepBuilderAPI_Transform,
        "BRepCheck_Analyzer": BRepCheck_Analyzer,
        "BRepGProp": BRepGProp,
        "BRepPrimAPI_MakeBox": BRepPrimAPI_MakeBox,
        "BRepPrimAPI_MakeCylinder": BRepPrimAPI_MakeCylinder,
        "BRepPrimAPI_MakeSphere": BRepPrimAPI_MakeSphere,
        "GProp_GProps": GProp_GProps,
        "IFSelect_RetDone": IFSelect_RetDone,
        "STEPControl_Reader": STEPControl_Reader,
        "TopAbs_SOLID": TopAbs_SOLID,
        "TopExp_Explorer": TopExp_Explorer,
        "gp_Ax1": gp_Ax1,
        "gp_Ax2": gp_Ax2,
        "gp_Dir": gp_Dir,
        "gp_Pnt": gp_Pnt,
        "gp_Trsf": gp_Trsf,
        "gp_Vec": gp_Vec,
    }


_AXIS_VECTORS = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1)}
_MIRROR_NORMALS = {"xy": (0, 0, 1), "xz": (0, 1, 0), "yz": (1, 0, 0)}


def _translate(shape: Any, at: Vec3, api: dict[str, Any]) -> Any:
    if at.as_tuple() == (0.0, 0.0, 0.0):
        return shape
    transform = api["gp_Trsf"]()
    transform.SetTranslation(api["gp_Vec"](*at.as_tuple()))
    return api["BRepBuilderAPI_Transform"](shape, transform, True).Shape()


def _count_solids(shape: Any, api: dict[str, Any]) -> int:
    explorer = api["TopExp_Explorer"]()
    explorer.Init(shape, api["TopAbs_SOLID"])
    count = 0
    while explorer.More():
        count += 1
        explorer.Next()
    return count


def _require_valid(shape: Any, path: str, api: dict[str, Any]) -> Any:
    try:
        analyzer = api["BRepCheck_Analyzer"](shape)
        valid = analyzer.IsValid()
    except Exception as exc:  # pragma: no cover - defensive against OCP surprises
        raise CreoMcpError(
            f"geometry node {path} could not be validated: {exc}"
        ) from exc
    if not valid:
        raise CreoMcpError(
            f"geometry node {path} produced an invalid solid. Check for coincident "
            "faces, zero-thickness cuts, or tools that fully remove their argument."
        )
    return shape


def _load_source(node: SourceNode, api: dict[str, Any], path: str) -> Any:
    step_path = resolve_step_path(node.path)
    reader = api["STEPControl_Reader"]()
    if reader.ReadFile(str(step_path)) != api["IFSelect_RetDone"]:
        raise CreoMcpError(
            f"geometry node {path}: OpenCascade could not read STEP file: {step_path}"
        )
    if reader.TransferRoots() <= 0:
        raise CreoMcpError(
            f"geometry node {path}: OpenCascade could not transfer STEP file: {step_path}"
        )
    shape = reader.OneShape()
    explorer = api["TopExp_Explorer"]()
    explorer.Init(shape, api["TopAbs_SOLID"])
    solids: list[Any] = []
    while explorer.More():
        solids.append(explorer.Current())
        explorer.Next()
    if not solids:
        raise CreoMcpError(
            f"geometry node {path}: {step_path} contains no solid bodies; surface or "
            "mid-sheet models cannot participate in solid booleans."
        )
    if node.solid_index > len(solids):
        raise CreoMcpError(
            f"geometry node {path}: solid_index={node.solid_index} exceeds the "
            f"{len(solids)} solid bodies found in {step_path.name}."
        )
    return _require_valid(
        solids[node.solid_index - 1],
        f"{path} (source {step_path.name} solid {node.solid_index})",
        api,
    )


def _build(node: GeometryNode, api: dict[str, Any], path: str, stats: dict[str, int]) -> Any:
    stats["node_count"] += 1
    if stats["node_count"] > MAX_NODES:
        raise CreoMcpError(
            f"geometry program exceeds the {MAX_NODES}-node limit at {path}."
        )
    stats["depth"] += 1
    if stats["depth"] > MAX_DEPTH:
        raise CreoMcpError(
            f"geometry program exceeds the {MAX_DEPTH}-node depth limit at {path}."
        )
    try:
        shape = _build_node(node, api, path, stats)
    finally:
        stats["depth"] -= 1
    return shape


def _build_node(node: GeometryNode, api: dict[str, Any], path: str, stats: dict[str, int]) -> Any:
    if isinstance(node, BoxNode):
        shape = api["BRepPrimAPI_MakeBox"](
            api["gp_Pnt"](0.0, 0.0, 0.0),
            node.length_mm,
            node.width_mm,
            node.height_mm,
        ).Shape()
        return _require_valid(_translate(shape, node.at, api), path, api)
    if isinstance(node, CylinderNode):
        direction = node.axis
        base = api["gp_Pnt"](*node.at.as_tuple())
        axis = api["gp_Dir"](*_AXIS_VECTORS[direction])
        shape = api["BRepPrimAPI_MakeCylinder"](
            api["gp_Ax2"](base, axis), node.radius_mm, node.height_mm
        )
        return _require_valid(shape.Shape(), path, api)
    if isinstance(node, SphereNode):
        center = api["gp_Pnt"](*node.at.as_tuple())
        shape = api["BRepPrimAPI_MakeSphere"](center, node.radius_mm)
        return _require_valid(shape.Shape(), path, api)
    if isinstance(node, BooleanNode):
        shapes = [
            _build(child, api, f"{path}.children[{index}]", stats)
            for index, child in enumerate(node.children)
        ]
        result = shapes[0]
        for index, other in enumerate(shapes[1:], start=1):
            if node.op == "union":
                tool = api["BRepAlgoAPI_Fuse"](result, other)
            elif node.op == "difference":
                tool = api["BRepAlgoAPI_Cut"](result, other)
            else:
                tool = api["BRepAlgoAPI_Common"](result, other)
            if not tool.IsDone():
                raise CreoMcpError(
                    f"boolean '{node.op}' failed combining {path}.children[0] with "
                    f"{path}.children[{index}]."
                )
            result = _require_valid(tool.Shape(), f"{path} (after {node.op} index {index})", api)
        return result
    if isinstance(node, MirrorNode):
        child = _build(node.child, api, f"{path}.child", stats)
        # gp_Trsf.SetMirror reflects across the X-Y plane of the given axis system,
        # so the named plane becomes the Z direction of that system.
        plane = api["gp_Ax2"](api["gp_Pnt"](0.0, 0.0, 0.0), api["gp_Dir"](*_MIRROR_NORMALS[node.plane]))
        transform = api["gp_Trsf"]()
        transform.SetMirror(plane)
        mirrored = api["BRepBuilderAPI_Transform"](child, transform, True).Shape()
        fuse = api["BRepAlgoAPI_Fuse"](child, mirrored)
        if not fuse.IsDone():
            raise CreoMcpError(f"mirror at {path} could not join the mirrored copy.")
        return _require_valid(fuse.Shape(), f"{path} (mirror)", api)
    if isinstance(node, PatternNode):
        base = _build(node.child, api, f"{path}.child", stats)
        result = base
        for copy_index in range(1, node.count):
            offset = api["gp_Trsf"]()
            offset.SetTranslation(
                api["gp_Vec"](
                    node.step.x_mm * copy_index,
                    node.step.y_mm * copy_index,
                    node.step.z_mm * copy_index,
                )
            )
            copy_shape = api["BRepBuilderAPI_Transform"](base, offset, True).Shape()
            fuse = api["BRepAlgoAPI_Fuse"](result, copy_shape)
            if not fuse.IsDone():
                raise CreoMcpError(
                    f"linear_pattern at {path} failed combining copy {copy_index}."
                )
            result = _require_valid(fuse.Shape(), f"{path} (copy {copy_index})", api)
        return result
    if isinstance(node, SourceNode):
        return _load_source(node, api, path)
    if isinstance(node, TransformNode):
        shape = _build(node.child, api, f"{path}.child", stats)
        if node.rotate_axis is not None and node.rotate_angle_deg is not None:
            rotation = api["gp_Trsf"]()
            rotation.SetRotation(
                api["gp_Ax1"](
                    api["gp_Pnt"](*node.rotate_about.as_tuple()),
                    api["gp_Dir"](*_AXIS_VECTORS[node.rotate_axis]),
                ),
                math.radians(node.rotate_angle_deg),
            )
            shape = _require_valid(
                api["BRepBuilderAPI_Transform"](shape, rotation, True).Shape(),
                f"{path} (after rotation)",
                api,
            )
        if node.translate.as_tuple() != (0.0, 0.0, 0.0):
            translation = api["gp_Trsf"]()
            translation.SetTranslation(api["gp_Vec"](*node.translate.as_tuple()))
            shape = api["BRepBuilderAPI_Transform"](shape, translation, True).Shape()
        return shape
    raise CreoMcpError(f"unsupported geometry node at {path}.")


def build_geometry_program(program: GeometryNode) -> GeometryBuildOutcome:
    """Build, validate, and measure the single solid described by a geometry program."""
    api = _api()
    stats = {"node_count": 0, "depth": 0}
    try:
        root = _GEOMETRY_NODE_ADAPTER.validate_python(program)
    except Exception as exc:
        raise CreoMcpError(f"The geometry program is invalid: {exc}") from exc
    shape = _build(root, api, "root", stats)

    solid_count = _count_solids(shape, api)
    if solid_count == 0:
        raise CreoMcpError(
            "The geometry program produced no solid (everything was subtracted or "
            "intersected away). Check the boolean arguments."
        )
    if solid_count > 1:
        raise CreoMcpError(
            f"The geometry program produced {solid_count} separate solids; exactly one "
            "connected solid is required. Join disjoint bodies with a connecting "
            "primitive or use a single union whose result is connected."
        )

    extent = measure_shape_extent(shape)
    if max(extent.x_mm, extent.y_mm, extent.z_mm) > MAX_EXTENT_MM:
        raise CreoMcpError(
            f"The resulting part ({extent.x_mm:.1f} x {extent.y_mm:.1f} x "
            f"{extent.z_mm:.1f} mm) exceeds the {MAX_EXTENT_MM:.0f} mm limit."
        )
    props = api["GProp_GProps"]()
    api["BRepGProp"].VolumeProperties_s(shape, props)
    volume_mm3 = float(props.Mass())
    if volume_mm3 <= 0:
        raise CreoMcpError("The resulting solid has non-positive volume.")
    return GeometryBuildOutcome(
        shape=shape,
        node_count=stats["node_count"],
        measured_extent_mm=(extent.x_mm, extent.y_mm, extent.z_mm),
        volume_mm3=volume_mm3,
    )
