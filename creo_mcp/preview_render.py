"""Creo-free software rasterizer for geometry previews.

Tessellates an OpenCascade solid and renders two orthographic-style views
(isometric and top) with PIL using a painter's-algorithm flat shader. This is
a diagnostic picture for humans and agents (orientation sanity checks), not a
photorealistic render.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from .errors import CreoMcpError

VIEW_SIZE = (560, 420)
BASE_COLOR = (66, 223, 208)
BACKGROUND = (245, 246, 248)
LIGHT = (0.35, 0.45, 0.82)


def _mesh_api() -> dict[str, Any]:
    try:
        from OCP.BRep import BRep_Tool
        from OCP.BRepMesh import BRepMesh_IncrementalMesh
        from OCP.TopAbs import TopAbs_FACE
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopLoc import TopLoc_Location
        from OCP.TopoDS import TopoDS
    except ImportError as exc:
        raise CreoMcpError(
            "Plan previews require the optional 'concept-geometry' extra. Install "
            "this project with creo-mcp[concept-geometry]."
        ) from exc
    return {
        "BRep_Tool": BRep_Tool,
        "BRepMesh_IncrementalMesh": BRepMesh_IncrementalMesh,
        "TopAbs_FACE": TopAbs_FACE,
        "TopExp_Explorer": TopExp_Explorer,
        "TopLoc_Location": TopLoc_Location,
        "TopoDS": TopoDS,
    }


def _triangulate(shape: Any, deflection: float) -> list[list[tuple[float, float, float]]]:
    """Collect world-space triangles [(a, b, c), ...] from a solid shape."""
    api = _mesh_api()
    api["BRepMesh_IncrementalMesh"](shape, deflection, False, 0.1, True)
    triangles: list[list[tuple[float, float, float]]] = []
    explorer = api["TopExp_Explorer"]()
    explorer.Init(shape, api["TopAbs_FACE"])
    while explorer.More():
        face = api["TopoDS"].Face_s(explorer.Current())
        location = api["TopLoc_Location"]()
        try:
            triangulation = api["BRep_Tool"].Triangulation_s(face, location)
        except Exception:
            triangulation = None
        if triangulation is not None:
            trsf = location.Transformation()
            nodes = [
                triangulation.Node(index).Transformed(trsf)
                for index in range(1, triangulation.NbNodes() + 1)
            ]
            for triangle in triangulation.Triangles():
                try:
                    a, b, c = triangle.Value(1), triangle.Value(2), triangle.Value(3)
                    triangles.append([
                        (nodes[a - 1].X(), nodes[a - 1].Y(), nodes[a - 1].Z()),
                        (nodes[b - 1].X(), nodes[b - 1].Y(), nodes[b - 1].Z()),
                        (nodes[c - 1].X(), nodes[c - 1].Y(), nodes[c - 1].Z()),
                    ])
                except Exception:
                    pass
        explorer.Next()
    return triangles


def _view_basis(kind: str) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    """Return (right, up, towards_viewer) unit axes for the named view."""
    if kind == "top":
        return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    az, el = math.radians(45.0), math.radians(32.0)
    forward = (
        math.cos(el) * math.cos(az),
        math.cos(el) * math.sin(az),
        math.sin(el),
    )
    right = (-math.sin(az), math.cos(az), 0.0)
    up = (
        -math.sin(el) * math.cos(az),
        -math.sin(el) * math.sin(az),
        math.cos(el),
    )
    return right, up, forward


def _render_view(
    triangles: list[list[tuple[float, float, float]]],
    kind: str,
    canvas: Any,
    origin: tuple[int, int],
    size: tuple[int, int],
    bounds: tuple[float, float, float, float, float, float],
) -> None:
    from PIL import ImageDraw

    right, up, depth = _view_basis(kind)
    span = max(
        (bounds[1] - bounds[0]) * abs(right[0]) + (bounds[3] - bounds[2]) * abs(right[1])
        + (bounds[5] - bounds[4]) * abs(right[2]),
        (bounds[1] - bounds[0]) * abs(up[0]) + (bounds[3] - bounds[2]) * abs(up[1])
        + (bounds[5] - bounds[4]) * abs(up[2]),
        1e-6,
    )
    scale = (min(size) - 48) / span
    center = [
        (bounds[0] + bounds[1]) / 2,
        (bounds[2] + bounds[3]) / 2,
        (bounds[4] + bounds[5]) / 2,
    ]

    def project(point: tuple[float, float, float]) -> tuple[float, float, float]:
        rel = (point[0] - center[0], point[1] - center[1], point[2] - center[2])
        x = sum(rel[i] * right[i] for i in range(3))
        y = sum(rel[i] * up[i] for i in range(3))
        z = sum(rel[i] * depth[i] for i in range(3))
        return (origin[0] + size[0] / 2 + x * scale, origin[1] + size[1] / 2 - y * scale, z)

    seen: list[tuple[float, list[tuple[float, float]], int]] = []
    for a, b, c in triangles:
        normal = _normal(a, b, c)
        facing = sum(normal[i] * depth[i] for i in range(3))
        if facing < -0.05:
            continue  # strict back face; near-edge-on walls stay visible (dark)
        shade = _shade(normal, facing)
        pa, pb, pc = project(a), project(b), project(c)
        centroid_z = (pa[2] + pb[2] + pc[2]) / 3
        seen.append((centroid_z, [pa[:2], pb[:2], pc[:2]], shade))
    seen.sort(key=lambda item: item[0])  # far first (depth = towards viewer)
    draw = ImageDraw.Draw(canvas)
    for _, points, shade in seen:
        draw.polygon(points, fill=shade, outline=shade)


def _normal(a: Any, b: Any, c: Any) -> tuple[float, float, float]:
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    length = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return (nx / length, ny / length, nz / length)


def _shade(normal: tuple[float, float, float], facing: float) -> tuple[int, int, int]:
    length = math.sqrt(sum(component * component for component in LIGHT))
    diffuse = max(
        0.0, sum(normal[i] * LIGHT[i] / length for i in range(3))
    )
    # facing==0 walls (pocket sides) get a dark tone so openings read in top view
    side = 0.30 + 0.30 * abs(facing)
    factor = side * (0.42 + 0.58 * diffuse)
    return tuple(max(0, min(255, int(channel * factor))) for channel in BASE_COLOR)  # type: ignore[return-value]


def _bounds(triangles: Any) -> tuple[float, float, float, float, float, float]:
    lo = [math.inf, math.inf, math.inf]
    hi = [-math.inf, -math.inf, -math.inf]
    for triangle in triangles:
        for point in triangle:
            for axis in range(3):
                lo[axis] = min(lo[axis], point[axis])
                hi[axis] = max(hi[axis], point[axis])
    return (lo[0], hi[0], lo[1], hi[1], lo[2], hi[2])


def render_shape_preview(
    shape: Any, output_path: Path, context: str, deflection: float = 0.8
) -> Path:
    """Render iso + top views of one shape into a PNG; returns the path."""
    from PIL import Image, ImageDraw, ImageFont

    triangles = _triangulate(shape, deflection)
    if not triangles:
        raise CreoMcpError(f"No meshable geometry found while previewing {context}.")
    bounds = _bounds(triangles)

    width = VIEW_SIZE[0] * 2 + 32
    height = VIEW_SIZE[1] + 60
    canvas = Image.new("RGB", (width, height), BACKGROUND)
    _render_view(triangles, "iso", canvas, (8, 8), VIEW_SIZE, bounds)
    _render_view(triangles, "top", canvas, (VIEW_SIZE[0] + 24, 8), VIEW_SIZE, bounds)

    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default()
    except Exception:  # pragma: no cover
        font = None
    extents = (
        bounds[1] - bounds[0],
        bounds[3] - bounds[2],
        bounds[5] - bounds[4],
    )
    caption = f"X {extents[0]:.1f}  Y {extents[1]:.1f}  Z {extents[2]:.1f} mm  |  insertion axis +Z (top view: pockets face you)"
    draw.text((12, height - 42), context[:80], fill=(40, 44, 52), font=font)
    draw.text((12, height - 24), caption, fill=(40, 44, 52), font=font)
    draw.text((10, VIEW_SIZE[1] - 16), "iso", fill=(120, 128, 140), font=font)
    draw.text((VIEW_SIZE[0] + 26, VIEW_SIZE[1] - 16), "top", fill=(120, 128, 140), font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG")
    return output_path
