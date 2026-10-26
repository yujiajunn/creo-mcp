"""Conservative hard-edge product envelopes for concept packaging cavities."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .concept_geometry import _box, _write_colored_concept_step, measure_shape_extent
from .errors import CreoMcpError
from .fit_analysis import (
    _bounds,
    _fit_ocp,
    _load_step_shape,
    _orientation_matrix,
    _transform,
    _translate,
    _volume,
)
from .models import (
    BoxDimensions,
    BoxExtent,
    InspectStepRequest,
    ProductClearanceTrayPlan,
    ProductClearanceTrayPlanRequest,
)
from .step import inspect_step_file, resolve_step_path

_TOL = 1e-5
_MESH_LINEAR_DEFLECTION_MM = 0.5
_MESH_ANGULAR_DEFLECTION_RAD = 0.15
_TARGET_DEPTH_LAYER_HEIGHT_MM = 1.0
_MAX_DEPTH_LAYER_COUNT = 64
_RECTILINEAR_STRIP_WIDTH_MM = 2.0
_ASSEMBLY_COMPONENT_THRESHOLD = 8
_ASSEMBLY_MAX_DEPTH_LAYER_COUNT = 24
_MAX_ASSEMBLY_STRIP_WIDTH_MM = 8.0
CLEARANCE_AUTOMATION_REVISION = 5


def _clearance_ocp() -> dict[str, Any]:
    api = _fit_ocp()
    try:
        from OCP.BRep import BRep_Builder, BRep_Tool
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopLoc import TopLoc_Location
        from OCP.TopoDS import TopoDS, TopoDS_Compound
        from OCP.TopTools import TopTools_ListOfShape
    except ImportError as exc:
        raise CreoMcpError(
            "Directional product-envelope generation requires the optional "
            "'concept-geometry' extra."
        ) from exc
    api.update(
        {
            "BRep_Tool": BRep_Tool,
            "BRep_Builder": BRep_Builder,
            "BRepAlgoAPI_Cut": BRepAlgoAPI_Cut,
            "BRepMesh_IncrementalMesh": BRepMesh_IncrementalMesh,
            "TopAbs_FACE": TopAbs_FACE,
            "TopAbs_SOLID": TopAbs_SOLID,
            "TopLoc_Location": TopLoc_Location,
            "TopoDS": TopoDS,
            "TopoDS_Compound": TopoDS_Compound,
            "TopExp_Explorer": TopExp_Explorer,
            "TopTools_ListOfShape": TopTools_ListOfShape,
        }
    )
    return api


def _solids(shape: Any, api: dict[str, Any]) -> list[Any]:
    result: list[Any] = []
    explorer = api["TopExp_Explorer"](shape, api["TopAbs_SOLID"])
    while explorer.More():
        result.append(explorer.Current())
        explorer.Next()
    return result


def _shape_list(shapes: list[Any], api: dict[str, Any]) -> Any:
    result = api["TopTools_ListOfShape"]()
    for shape in shapes:
        result.Append(shape)
    return result


def _compound(shapes: list[Any], api: dict[str, Any]) -> Any:
    result = api["TopoDS_Compound"]()
    builder = api["BRep_Builder"]()
    builder.MakeCompound(result)
    for shape in shapes:
        builder.Add(result, shape)
    return result


def _cut_many(source: Any, tools: Any, api: dict[str, Any]) -> Any:
    operation = api["BRepAlgoAPI_Cut"]()
    operation.SetArguments(_shape_list(_solids(source, api), api))
    operation.SetTools(_shape_list(_solids(tools, api), api))
    operation.SetFuzzyValue(_TOL)
    operation.Build()
    if not operation.IsDone() or operation.Shape().IsNull():
        raise CreoMcpError("OpenCascade could not cut the directional product envelope.")
    return operation.Shape()


def _load_positioned_product(
    request: ProductClearanceTrayPlanRequest,
) -> tuple[Path, str, Any, list[Any], dict[str, Any]]:
    path = resolve_step_path(request.product_step_path)
    info = inspect_step_file(InspectStepRequest(step_path=str(path)))
    if info.declared_length_unit != "mm":
        raise CreoMcpError("Product clearance planning requires a STEP declared in millimetres.")
    api = _clearance_ocp()
    source, _ = _load_step_shape(path, api)
    source_solids = _solids(source, api)
    if not source_solids:
        raise CreoMcpError(f"STEP contains no solid product bodies: {path}")
    matrix = _orientation_matrix(request.orientation)
    oriented_solids = [_transform(solid, matrix, api) for solid in source_solids]
    # Keep placement in the same coordinate frame used for the mesh-derived cavities.
    solid_bounds = [_mesh_triangles_and_bounds(solid, api)[1] for solid in oriented_solids]
    x_min = min(bounds[0] for bounds in solid_bounds)
    y_min = min(bounds[1] for bounds in solid_bounds)
    z_max = max(bounds[5] for bounds in solid_bounds)
    x_max = max(bounds[3] for bounds in solid_bounds)
    y_max = max(bounds[4] for bounds in solid_bounds)
    translation = (
        -(x_min + x_max) / 2,
        -(y_min + y_max) / 2,
        -request.top_clearance_mm - z_max,
    )
    solids = [
        _translate(solid, translation[0], translation[1], translation[2], api)
        for solid in oriented_solids
    ]
    positioned = _compound(solids, api)
    return path, hashlib.sha256(path.read_bytes()).hexdigest(), positioned, solids, api


def _mesh_triangles_and_bounds(
    solid: Any,
    api: dict[str, Any],
) -> tuple[
    list[tuple[tuple[float, float, float], ...]],
    tuple[float, float, float, float, float, float],
]:
    mesher = api["BRepMesh_IncrementalMesh"](
        solid,
        _MESH_LINEAR_DEFLECTION_MM,
        False,
        _MESH_ANGULAR_DEFLECTION_RAD,
        True,
    )
    mesher.Perform()
    if not mesher.IsDone():
        raise CreoMcpError("OpenCascade could not triangulate the complete product BREP.")

    triangles: list[tuple[tuple[float, float, float], ...]] = []
    coordinates_3d: list[tuple[float, float, float]] = []
    explorer = api["TopExp_Explorer"](solid, api["TopAbs_FACE"])
    while explorer.More():
        face = api["TopoDS"].Face_s(explorer.Current())
        location = api["TopLoc_Location"]()
        triangulation = api["BRep_Tool"].Triangulation_s(face, location)
        if triangulation is not None:
            transform = location.Transformation()
            for index in range(1, triangulation.NbTriangles() + 1):
                node_indexes = triangulation.Triangle(index).Get()
                points = [
                    triangulation.Node(node_index).Transformed(transform)
                    for node_index in node_indexes
                ]
                coordinates = tuple(
                    (float(point.X()), float(point.Y()), float(point.Z()))
                    for point in points
                )
                coordinates_3d.extend(coordinates)
                triangles.append(coordinates)
        explorer.Next()
    if not triangles:
        raise CreoMcpError("Product BREP triangulation produced no projectable faces.")
    bounds = (
        min(point[0] for point in coordinates_3d),
        min(point[1] for point in coordinates_3d),
        min(point[2] for point in coordinates_3d),
        max(point[0] for point in coordinates_3d),
        max(point[1] for point in coordinates_3d),
        max(point[2] for point in coordinates_3d),
    )
    return triangles, bounds


def _clip_triangle_below_z(
    triangle: tuple[tuple[float, float, float], ...],
    maximum_z: float,
) -> list[tuple[float, float, float]]:
    clipped: list[tuple[float, float, float]] = []
    previous = triangle[-1]
    previous_inside = previous[2] <= maximum_z + _TOL
    for current in triangle:
        current_inside = current[2] <= maximum_z + _TOL
        if current_inside != previous_inside:
            fraction = (maximum_z - previous[2]) / (current[2] - previous[2])
            clipped.append(
                (
                    previous[0] + fraction * (current[0] - previous[0]),
                    previous[1] + fraction * (current[1] - previous[1]),
                    maximum_z,
                )
            )
        if current_inside:
            clipped.append(current)
        previous = current
        previous_inside = current_inside
    return clipped


def _depth_layers(
    bounds: tuple[float, float, float, float, float, float],
    bottom_clearance: float,
    component_count: int = 1,
) -> tuple[float, float, int]:
    product_depth = bounds[5] - bounds[2]
    maximum_layers = (
        _ASSEMBLY_MAX_DEPTH_LAYER_COUNT
        if component_count > _ASSEMBLY_COMPONENT_THRESHOLD
        else _MAX_DEPTH_LAYER_COUNT
    )
    layer_count = max(
        1,
        min(
            maximum_layers,
            math.ceil(product_depth / _TARGET_DEPTH_LAYER_HEIGHT_MM),
        ),
    )
    layer_height = product_depth / layer_count
    cavity_bottom = bounds[2] - bottom_clearance - _MESH_LINEAR_DEFLECTION_MM
    return cavity_bottom, layer_height, layer_count


def _component_rectangles(
    triangles: list[tuple[tuple[float, float, float], ...]],
    bounds: tuple[float, float, float, float, float, float],
    *,
    maximum_z: float | None,
    strip_width: float,
) -> list[tuple[float, float, float, float]]:
    """Return conservative hard-edge strips for one product solid at one depth."""
    x_min, y_min, _, x_max, y_max, _ = bounds
    split_x = x_max - x_min >= y_max - y_min
    start = x_min if split_x else y_min
    end = x_max if split_x else y_max
    strip_count = max(1, math.ceil((end - start) / strip_width))
    secondary_limits: list[list[float] | None] = [None] * strip_count

    for triangle in triangles:
        coordinates = (
            triangle if maximum_z is None else _clip_triangle_below_z(triangle, maximum_z)
        )
        if len(coordinates) < 3:
            continue
        primary_values = [point[0] if split_x else point[1] for point in coordinates]
        secondary_values = [point[1] if split_x else point[0] for point in coordinates]
        first = max(0, math.floor((min(primary_values) - start) / strip_width))
        last = min(
            strip_count - 1,
            math.floor((max(primary_values) - start) / strip_width),
        )
        for index in range(first, last + 1):
            limits = secondary_limits[index]
            if limits is None:
                secondary_limits[index] = [min(secondary_values), max(secondary_values)]
            else:
                limits[0] = min(limits[0], min(secondary_values))
                limits[1] = max(limits[1], max(secondary_values))

    rectangles: list[tuple[float, float, float, float]] = []
    index = 0
    while index < strip_count:
        limits = secondary_limits[index]
        if limits is None:
            index += 1
            continue
        end_index = index
        while end_index + 1 < strip_count:
            next_limits = secondary_limits[end_index + 1]
            if next_limits is None or any(
                abs(left - right) > _MESH_LINEAR_DEFLECTION_MM
                for left, right in zip(limits, next_limits)
            ):
                break
            end_index += 1
        primary_min = start + index * strip_width
        primary_max = min(end, start + (end_index + 1) * strip_width)
        if split_x:
            rectangles.append((primary_min, limits[0], primary_max, limits[1]))
        else:
            rectangles.append((limits[0], primary_min, limits[1], primary_max))
        index = end_index + 1
    return rectangles


def _expanded_rectangle(
    rectangle: tuple[float, float, float, float],
    clearance: float,
) -> tuple[float, float, float, float]:
    x_min, y_min, x_max, y_max = rectangle
    expansion = clearance + _MESH_LINEAR_DEFLECTION_MM
    return x_min - expansion, y_min - expansion, x_max + expansion, y_max + expansion


def _component_strip_width(
    bounds: tuple[float, float, float, float, float, float],
    component_count: int,
) -> float:
    if component_count <= _ASSEMBLY_COMPONENT_THRESHOLD:
        return _RECTILINEAR_STRIP_WIDTH_MM
    primary_span = max(bounds[3] - bounds[0], bounds[4] - bounds[1])
    return min(
        _MAX_ASSEMBLY_STRIP_WIDTH_MM,
        max(_RECTILINEAR_STRIP_WIDTH_MM, primary_span / 48),
    )


def _component_full_depth_envelope(
    bounds: tuple[float, float, float, float, float, float],
    *,
    side_clearance: float,
    bottom_clearance: float,
    opening_top_z: float,
    api: dict[str, Any],
) -> Any:
    x_min, y_min, z_min, x_max, y_max, _ = bounds
    expansion = side_clearance + _MESH_LINEAR_DEFLECTION_MM
    bottom_z = z_min - bottom_clearance - _MESH_LINEAR_DEFLECTION_MM
    return _box(
        x_max - x_min + 2 * expansion,
        y_max - y_min + 2 * expansion,
        opening_top_z - bottom_z,
        x_min - expansion,
        y_min - expansion,
        bottom_z,
        api,
    )


def _build_component_rectilinear_envelope(
    component_meshes: list[
        tuple[
            list[tuple[tuple[float, float, float], ...]],
            tuple[float, float, float, float, float, float],
        ]
    ],
    bounds: tuple[float, float, float, float, float, float],
    *,
    side_clearance: float,
    bottom_clearance: float,
    opening_top_z: float,
    api: dict[str, Any],
    layer_grid: tuple[float, float, int] | None = None,
    insertion_probe: bool = False,
    strip_widths: list[float] | None = None,
) -> tuple[Any, float, float, int]:
    cavity_bottom, layer_height, layer_count = layer_grid or _depth_layers(
        bounds,
        bottom_clearance,
    )
    active: dict[tuple[float, float, float, float], list[float]] = {}
    boxes: list[tuple[float, float, float, float, float, float]] = []
    strip_widths = strip_widths or [_RECTILINEAR_STRIP_WIDTH_MM] * len(component_meshes)
    if len(strip_widths) != len(component_meshes):
        raise CreoMcpError("Component strip widths do not match the product solid count.")
    for index in range(layer_count):
        lower_z = cavity_bottom + index * layer_height
        upper_z = cavity_bottom + (index + 1) * layer_height
        is_top_layer = index == layer_count - 1
        threshold_offset = (
            _MESH_LINEAR_DEFLECTION_MM
            if insertion_probe
            else bottom_clearance + _MESH_LINEAR_DEFLECTION_MM
        )
        rectangles = [
            _expanded_rectangle(rectangle, side_clearance)
            for (triangles, component_bounds), strip_width in zip(component_meshes, strip_widths)
            for rectangle in _component_rectangles(
                triangles,
                component_bounds,
                maximum_z=None if is_top_layer else upper_z + threshold_offset,
                strip_width=strip_width,
            )
        ]
        current = {tuple(round(value, 5) for value in rectangle) for rectangle in rectangles}
        for rectangle, limits in list(active.items()):
            if rectangle not in current:
                boxes.append((*rectangle, limits[0], limits[1]))
                del active[rectangle]
        prism_top_z = opening_top_z if is_top_layer else upper_z
        for rectangle in current:
            active.setdefault(rectangle, [lower_z, prism_top_z])[1] = prism_top_z
    boxes.extend((*rectangle, limits[0], limits[1]) for rectangle, limits in active.items())
    prisms = [
        _box(x_max - x_min, y_max - y_min, top_z - bottom_z, x_min, y_min, bottom_z, api)
        for x_min, y_min, x_max, y_max, bottom_z, top_z in boxes
        if x_max - x_min > _TOL and y_max - y_min > _TOL and top_z - bottom_z > _TOL
    ]
    if not prisms:
        raise CreoMcpError("The complete product produced no component cavity prisms.")
    envelope = prisms[0] if len(prisms) == 1 else _compound(prisms, api)
    return envelope, cavity_bottom, layer_height, layer_count


def _build_envelope(
    solids: list[Any],
    *,
    side_clearance: float,
    bottom_clearance: float,
    opening_top_z: float,
    api: dict[str, Any],
) -> tuple[
    Any,
    float,
    float,
    int,
    list[Any],
    tuple[float, float, float, float, float, float],
    list[Any],
    list[float],
]:
    component_meshes = [_mesh_triangles_and_bounds(solid, api) for solid in solids]
    bounds = (
        min(component_bounds[0] for _, component_bounds in component_meshes),
        min(component_bounds[1] for _, component_bounds in component_meshes),
        min(component_bounds[2] for _, component_bounds in component_meshes),
        max(component_bounds[3] for _, component_bounds in component_meshes),
        max(component_bounds[4] for _, component_bounds in component_meshes),
        max(component_bounds[5] for _, component_bounds in component_meshes),
    )
    component_count = len(component_meshes)
    layer_grid = _depth_layers(bounds, bottom_clearance, component_count)
    if component_count > _ASSEMBLY_COMPONENT_THRESHOLD:
        component_envelopes = [
            _component_full_depth_envelope(
                component_bounds,
                side_clearance=side_clearance,
                bottom_clearance=bottom_clearance,
                opening_top_z=opening_top_z,
                api=api,
            )
            for _, component_bounds in component_meshes
        ]
        cavity_bottom = min(
            component_bounds[2] - bottom_clearance - _MESH_LINEAR_DEFLECTION_MM
            for _, component_bounds in component_meshes
        )
        layer_grid = (cavity_bottom, bounds[5] - bounds[2], 1)
        strip_widths: list[float] = []
    else:
        strip_widths = [
            _component_strip_width(component_bounds, component_count)
            for _, component_bounds in component_meshes
        ]
        component_envelopes = [
            _build_component_rectilinear_envelope(
                [component_mesh],
                bounds,
                side_clearance=side_clearance,
                bottom_clearance=bottom_clearance,
                opening_top_z=opening_top_z,
                api=api,
                layer_grid=layer_grid,
                strip_widths=[strip_width],
            )[0]
            for component_mesh, strip_width in zip(component_meshes, strip_widths)
        ]
    envelope = (
        component_envelopes[0]
        if len(component_envelopes) == 1
        else _compound(component_envelopes, api)
    )
    cavity_bottom, layer_height, layer_count = layer_grid
    return (
        envelope,
        cavity_bottom,
        layer_height,
        layer_count,
        component_meshes,
        bounds,
        component_envelopes,
        strip_widths,
    )


def _verify_static_containment(product: Any, envelope: Any, api: dict[str, Any]) -> float:
    outside = _cut_many(product, envelope, api)
    outside_volume = _volume(outside, api)
    tolerance = max(1e-5, _volume(product, api) * 1e-8)
    if outside_volume > tolerance:
        raise CreoMcpError(
            "Conservative projected cavity does not contain the complete product: "
            f"outside volume {outside_volume:.6f} mm3."
        )
    return outside_volume


def _verify_component_static_containment(
    solids: list[Any],
    component_envelopes: list[Any],
    api: dict[str, Any],
) -> float:
    if len(solids) != len(component_envelopes):
        raise CreoMcpError("Component envelopes do not match the product solid count.")
    return sum(
        _verify_static_containment(solid, envelope, api)
        for solid, envelope in zip(solids, component_envelopes)
    )


def _verify_insertion_containment(
    component_meshes: list[Any],
    bounds: tuple[float, float, float, float, float, float],
    component_envelopes: list[Any],
    *,
    cavity_bottom: float,
    layer_height: float,
    layer_count: int,
    opening_top_z: float,
    api: dict[str, Any],
    strip_widths: list[float],
) -> float:
    if len(component_meshes) != len(component_envelopes):
        raise CreoMcpError("Component envelopes do not match the product solid count.")
    if not strip_widths:
        # Each high-component-count envelope is an open vertical rectangular prism.
        # Static containment therefore also proves an unobstructed +Z insertion path.
        return 0.0
    outside_volume = 0.0
    for component_mesh, envelope, strip_width in zip(
        component_meshes,
        component_envelopes,
        strip_widths,
    ):
        sweep_probe, _, _, _ = _build_component_rectilinear_envelope(
            [component_mesh],
            bounds,
            side_clearance=0.0,
            bottom_clearance=0.0,
            opening_top_z=opening_top_z,
            api=api,
            layer_grid=(cavity_bottom, layer_height, layer_count),
            insertion_probe=True,
            strip_widths=[strip_width],
        )
        outside = _cut_many(sweep_probe, envelope, api)
        component_outside = _volume(outside, api)
        tolerance = max(1e-5, _volume(sweep_probe, api) * 1e-8)
        if component_outside > tolerance:
            raise CreoMcpError(
                "Local-depth cavity does not contain a complete component insertion sweep: "
                f"outside volume {component_outside:.6f} mm3."
            )
        outside_volume += component_outside
    return outside_volume


def _progress(
    cb: Callable[[float, str], None] | None, value: float, message: str
) -> None:
    """Report one progress step; observation must never affect the geometry work."""
    if cb is None:
        return
    try:
        cb(value, message)
    except Exception:
        pass


def plan_product_clearance_tray(
    request: ProductClearanceTrayPlanRequest,
    *,
    progress_cb: Callable[[float, str], None] | None = None,
) -> ProductClearanceTrayPlan:
    path, source_sha256, _, solids, api = _load_positioned_product(request)
    _progress(progress_cb, 10, "产品 STEP 已读取并定位")
    opening_top_z = request.flange_thickness_mm + 0.1
    (
        envelope,
        cavity_bottom,
        depth_layer_height,
        depth_layer_count,
        component_meshes,
        mesh_bounds,
        component_envelopes,
        strip_widths,
    ) = _build_envelope(
        solids,
        side_clearance=request.side_clearance_mm,
        bottom_clearance=request.bottom_clearance_mm,
        opening_top_z=opening_top_z,
        api=api,
    )
    _progress(progress_cb, 45, "组件扫掠型腔包络已生成")
    static_outside = _verify_component_static_containment(solids, component_envelopes, api)
    _progress(progress_cb, 55, "静态包容校验完成")
    insertion_outside = _verify_insertion_containment(
        component_meshes,
        mesh_bounds,
        component_envelopes,
        cavity_bottom=cavity_bottom,
        layer_height=depth_layer_height,
        layer_count=depth_layer_count,
        opening_top_z=opening_top_z,
        api=api,
        strip_widths=strip_widths,
    )
    _progress(progress_cb, 85, "插入路径包容校验完成")
    envelope_bounds = _bounds(envelope, api)
    outer_length = envelope_bounds[3] - envelope_bounds[0] + 2 * (
        request.wall_thickness_mm + request.flange_width_mm
    )
    outer_width = envelope_bounds[4] - envelope_bounds[1] + 2 * (
        request.wall_thickness_mm + request.flange_width_mm
    )
    outer_height = -cavity_bottom + request.bottom_thickness_mm + request.flange_thickness_mm
    normalized = request.model_dump(mode="json")
    normalized.update(
        {
            "product_step_path": str(path),
            "product_sha256": source_sha256,
            "geometry_recipe_revision": CLEARANCE_AUTOMATION_REVISION,
            "cavity_geometry_mode": "hard_edge_component_rectilinear_directional_sweep",
            "geometry_source_mode": "conservative_brep_component_rectilinear_depth_envelope",
            "mesh_linear_deflection_mm": _MESH_LINEAR_DEFLECTION_MM,
            "clearance_conservatism_mm": _MESH_LINEAR_DEFLECTION_MM,
            "depth_layer_height_mm": depth_layer_height,
            "depth_layer_count": depth_layer_count,
            "vertical_conservatism_mm": _MESH_LINEAR_DEFLECTION_MM
            + depth_layer_height,
            "static_outside_volume_mm3": static_outside,
            "insertion_sweep_outside_volume_mm3": insertion_outside,
        }
    )
    plan_id = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    _progress(progress_cb, 100, "方案生成完成")
    return ProductClearanceTrayPlan(
        plan_id=plan_id,
        execution_state="dry_run_only",
        model_mode="directional_product_clearance_brep",
        concept_stage="hard_edge_fit",
        model_state="CONCEPT_ONLY",
        creo_geometry_kind="imported_brep",
        parameter_update_mode="regenerate_with_mcp",
        product_step_path=str(path),
        product_sha256=source_sha256,
        source_solid_count=len(solids),
        orientation=request.orientation,
        insertion_axis=request.insertion_axis,
        side_clearance_mm=request.side_clearance_mm,
        top_clearance_mm=request.top_clearance_mm,
        bottom_clearance_mm=request.bottom_clearance_mm,
        wall_thickness_mm=request.wall_thickness_mm,
        bottom_thickness_mm=request.bottom_thickness_mm,
        flange_thickness_mm=request.flange_thickness_mm,
        flange_width_mm=request.flange_width_mm,
        static_outside_volume_mm3=static_outside,
        insertion_sweep_outside_volume_mm3=insertion_outside,
        insertion_path_verified=True,
        cavity_geometry_mode="hard_edge_component_rectilinear_directional_sweep",
        geometry_source_mode="conservative_brep_component_rectilinear_depth_envelope",
        mesh_linear_deflection_mm=_MESH_LINEAR_DEFLECTION_MM,
        clearance_conservatism_mm=_MESH_LINEAR_DEFLECTION_MM,
        depth_layer_height_mm=depth_layer_height,
        depth_layer_count=depth_layer_count,
        vertical_conservatism_mm=_MESH_LINEAR_DEFLECTION_MM + depth_layer_height,
        corner_radius_mm=0.0,
        draft_expansion_mm=0.0,
        automatic_handling_reliefs="none",
        expected_extent_mm=BoxDimensions(
            length_mm=outer_length,
            width_mm=outer_width,
            height_mm=outer_height,
        ),
        part_name=request.part_name,
        expected_model_file=f"{request.part_name}.prt",
        output_directory=request.output_directory,
        operations=[
            "Read the complete product BREP and apply the confirmed orientation.",
            "Triangulate every product face with a recorded maximum linear deflection.",
            "Split each product solid into conservative straight rectangular depth strips.",
            "Expand every component strip by the requested clearance plus recorded mesh error.",
            "Extend monotonically growing component strips toward the opening plane.",
            "Verify both static containment and a separately built insertion-sweep probe.",
            "Create one CONCEPT_ONLY imported BREP without draft, radius, or automatic reliefs.",
        ],
        requires_user_confirmation=True,
    )


def build_product_clearance_tray_shape(plan: ProductClearanceTrayPlan) -> Any:
    request = ProductClearanceTrayPlanRequest(
        product_step_path=plan.product_step_path,
        orientation=plan.orientation,
        insertion_axis=plan.insertion_axis,
        side_clearance_mm=plan.side_clearance_mm,
        top_clearance_mm=plan.top_clearance_mm,
        bottom_clearance_mm=plan.bottom_clearance_mm,
        wall_thickness_mm=plan.wall_thickness_mm,
        bottom_thickness_mm=plan.bottom_thickness_mm,
        flange_thickness_mm=plan.flange_thickness_mm,
        flange_width_mm=plan.flange_width_mm,
        part_name=plan.part_name,
        output_directory=plan.output_directory,
    )
    _, source_sha256, _, solids, api = _load_positioned_product(request)
    if source_sha256 != plan.product_sha256:
        raise CreoMcpError("Product STEP changed after the clearance plan was created.")
    opening_top_z = plan.flange_thickness_mm + 0.1
    (
        envelope,
        cavity_bottom,
        layer_height,
        layer_count,
        component_meshes,
        mesh_bounds,
        component_envelopes,
        strip_widths,
    ) = _build_envelope(
        solids,
        side_clearance=plan.side_clearance_mm,
        bottom_clearance=plan.bottom_clearance_mm,
        opening_top_z=opening_top_z,
        api=api,
    )
    _verify_component_static_containment(solids, component_envelopes, api)
    _verify_insertion_containment(
        component_meshes,
        mesh_bounds,
        component_envelopes,
        cavity_bottom=cavity_bottom,
        layer_height=layer_height,
        layer_count=layer_count,
        opening_top_z=opening_top_z,
        api=api,
        strip_widths=strip_widths,
    )
    envelope_bounds = _bounds(envelope, api)
    outer = _box(
        plan.expected_extent_mm.length_mm,
        plan.expected_extent_mm.width_mm,
        plan.expected_extent_mm.height_mm,
        envelope_bounds[0] - plan.wall_thickness_mm - plan.flange_width_mm,
        envelope_bounds[1] - plan.wall_thickness_mm - plan.flange_width_mm,
        cavity_bottom - plan.bottom_thickness_mm,
        api,
    )
    tray = _cut_many(outer, envelope, api)
    if (
        tray.IsNull()
        or not api["BRepCheck_Analyzer"](tray).IsValid()
        or len(_solids(tray, api)) != 1
    ):
        raise CreoMcpError("OpenCascade produced invalid hard-edge clearance tray geometry.")
    return tray


def export_product_clearance_tray_step(
    plan: ProductClearanceTrayPlan,
    output_path: Path,
) -> BoxExtent:
    if output_path.suffix.casefold() not in {".stp", ".step"}:
        raise CreoMcpError(f"Concept geometry output must be STEP: {output_path}")
    if output_path.exists():
        raise CreoMcpError(f"Refusing to overwrite generated STEP file: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    shape = build_product_clearance_tray_shape(plan)
    measured = measure_shape_extent(shape)
    _write_colored_concept_step(shape, output_path, "hard-edge product-clearance tray")
    return measured
