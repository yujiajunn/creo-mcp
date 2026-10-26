"""Collapse the conservative cavity prisms into a small editable segment stack.

The clearance planner already reduces an arbitrary product shape to axis-aligned
rectangular prisms (``clearance_geometry._build_component_rectilinear_envelope``).
Those prisms are exactly the right material for a native, human-editable tray: every
merged group becomes one rectangular cut feature whose length, width and depth a
person can redefine later in Creo.

Two roles stay separate on purpose:

* :func:`build_cavity_prisms` mirrors the planner's layer stacking, so the segment
  table is derived from the same conservative geometry that gates ``+Z`` insertion.
* :func:`consolidate_prisms` is pure Python: no OCP, no Creo, fully testable.

Conservatism always goes the same way: a merged footprint is the *union* of what it
covers, never an average, and the monotonicity pass only ever enlarges a shallower
step so a straight draw cannot jam. Approximation waste is reported, not hidden.
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .clearance_geometry import (
    _MESH_LINEAR_DEFLECTION_MM,
    _component_rectangles,
    _component_strip_width,
    _depth_layers,
    _expanded_rectangle,
    _load_positioned_product,
    _mesh_triangles_and_bounds,
)
from .errors import CreoMcpError
from .models import ProductClearanceTrayPlanRequest

Prism = tuple[float, float, float, float, float, float]
Rectangle = tuple[float, float, float, float]
Band = tuple[float, float, Rectangle]

DEFAULT_MAX_SEGMENTS = 6
DEFAULT_FOOTPRINT_TOLERANCE_MM = 0.05
_EPSILON = 1e-6


@dataclass
class CavitySegment:
    """One merged rectangular cavity step, described top-first for the machinist."""

    index: int
    length_mm: float
    width_mm: float
    depth_mm: float
    x_min_mm: float
    y_min_mm: float
    z_bottom_mm: float
    z_top_mm: float
    merged_bands: int = 1
    enlarged_for_insertion: bool = False
    column_index: int = 1
    device_index: int = 1

    @property
    def volume_mm3(self) -> float:
        return self.length_mm * self.width_mm * self.depth_mm

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "length_mm": round(self.length_mm, 3),
            "width_mm": round(self.width_mm, 3),
            "depth_mm": round(self.depth_mm, 3),
            "x_min_mm": round(self.x_min_mm, 3),
            "y_min_mm": round(self.y_min_mm, 3),
            "z_bottom_mm": round(self.z_bottom_mm, 3),
            "z_top_mm": round(self.z_top_mm, 3),
            "merged_bands": self.merged_bands,
            "enlarged_for_insertion": self.enlarged_for_insertion,
            "column_index": self.column_index,
            "device_index": self.device_index,
        }


@dataclass
class SegmentPlan:
    """A merged segment stack plus the conservatism it had to add.

    ``union_volume_mm3`` is the cavity the planner actually cuts: the exact union of the
    component strip rectangles per depth band. ``reference_volume_mm3`` is the volume of
    the *finest* staircase (one bounding rectangle per band cluster). Merging and
    insertion enlargement can only add material above either baseline, so
    ``waste_volume_mm3`` is never negative — and the gap between the two baselines is
    the price of representing the cavity as a handful of rectangular cut features.
    ``prism_volume_sum_mm3`` is diagnostic only — the planner's prisms overlap along
    Z (each rectangle runs up to the cavity mouth), so their raw sum exceeds any real
    cavity volume and must not be used as a baseline.
    """

    segments: list[CavitySegment]
    reference_volume_mm3: float
    segment_volume_mm3: float
    prism_volume_sum_mm3: float
    union_volume_mm3: float
    cavity_bottom_mm: float
    opening_top_mm: float
    enlarged_segments: list[int] = field(default_factory=list)
    column_count: int = 1
    device_count: int = 1
    cross_column_merges: list[int] = field(default_factory=list)

    @property
    def baseline_volume_mm3(self) -> float:
        """The cavity the planner actually cuts, used as the over-cut baseline."""
        return self.union_volume_mm3 if self.union_volume_mm3 > 0 else self.reference_volume_mm3

    @property
    def waste_volume_mm3(self) -> float:
        return self.segment_volume_mm3 - self.baseline_volume_mm3

    @property
    def waste_ratio(self) -> float:
        baseline = self.baseline_volume_mm3
        if baseline <= 0:
            return 0.0
        return self.segment_volume_mm3 / baseline - 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_count": len(self.segments),
            "segments": [segment.to_dict() for segment in self.segments],
            "cavity_union_volume_mm3": round(self.union_volume_mm3, 3),
            "reference_staircase_volume_mm3": round(self.reference_volume_mm3, 3),
            "segment_volume_mm3": round(self.segment_volume_mm3, 3),
            "prism_volume_sum_mm3": round(self.prism_volume_sum_mm3, 3),
            "over_cut_volume_mm3": round(self.waste_volume_mm3, 3),
            "over_cut_ratio": round(self.waste_ratio, 4),
            "waste_volume_mm3": round(self.waste_volume_mm3, 3),
            "waste_ratio": round(self.waste_ratio, 4),
            "cavity_bottom_mm": round(self.cavity_bottom_mm, 3),
            "opening_top_mm": round(self.opening_top_mm, 3),
            "enlarged_segments": list(self.enlarged_segments),
            "pocket_columns": self.column_count,
            "device_count": self.device_count,
            "cross_column_merges": list(self.cross_column_merges),
        }


def _union_rect(left: Rectangle, right: Rectangle) -> Rectangle:
    return (
        min(left[0], right[0]),
        min(left[1], right[1]),
        max(left[2], right[2]),
        max(left[3], right[3]),
    )


def _group_rect(bands: list[Band]) -> Rectangle:
    rect = bands[0][2]
    for band in bands[1:]:
        rect = _union_rect(rect, band[2])
    return rect


def _group_height(bands: list[Band]) -> float:
    return bands[-1][1] - bands[0][0]


def _rect_area(rect: Rectangle) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def _rect_contains(outer: Rectangle, inner: Rectangle, tolerance: float = _EPSILON) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _merge_cost(deeper: list[Band], shallower: list[Band]) -> float:
    """Extra cavity material created by merging two adjacent steps into one."""
    before = _rect_area(_group_rect(deeper)) * _group_height(
        deeper
    ) + _rect_area(_group_rect(shallower)) * _group_height(shallower)
    merged = _union_rect(_group_rect(deeper), _group_rect(shallower))
    after = _rect_area(merged) * (_group_height(deeper) + _group_height(shallower))
    return after - before


def bottom_threshold(bottom_clearance_mm: float) -> float:
    """The clipping offset the planner uses to expand shallower components."""
    return bottom_clearance_mm + _MESH_LINEAR_DEFLECTION_MM


def build_cavity_prisms(
    request: ProductClearanceTrayPlanRequest,
) -> tuple[list[Prism], list[int], float, float, int, float]:
    """Return the conservative cavity prisms for one positioned product.

    Mirrors the planner's stacking rule so the segment table cannot drift away from
    the geometry the ``+Z`` insertion check was validated against.

    Returns ``(prisms, owners, cavity_bottom_z, opening_top_z, layer_count, layer_height)``
    where ``owners[i]`` is the product solid index ``prisms[i]`` came from.
    """
    _path, _digest, _positioned, solids, api = _load_positioned_product(request)
    meshes = [_mesh_triangles_and_bounds(solid, api) for solid in solids]
    component_count = len(meshes)
    bounds: Rectangle = (
        min(mesh[1][0] for mesh in meshes),
        min(mesh[1][1] for mesh in meshes),
        max(mesh[1][3] for mesh in meshes),
        max(mesh[1][4] for mesh in meshes),
    )
    full_bounds = (
        bounds[0],
        bounds[1],
        min(mesh[1][2] for mesh in meshes),
        bounds[2],
        bounds[3],
        max(mesh[1][5] for mesh in meshes),
    )
    strip_widths = [
        _component_strip_width(mesh[1], component_count) for mesh in meshes
    ]
    opening_top_z = request.top_clearance_mm
    cavity_bottom, layer_height, layer_count = _depth_layers(
        full_bounds, request.bottom_clearance_mm
    )
    threshold = bottom_threshold(request.bottom_clearance_mm)

    active: dict[tuple[Rectangle, int], list[float]] = {}
    boxes: list[Prism] = []
    box_owners: list[int] = []
    for index in range(layer_count):
        lower_z = cavity_bottom + index * layer_height
        upper_z = cavity_bottom + (index + 1) * layer_height
        is_top_layer = index == layer_count - 1
        rectangles = [
            (_expanded_rectangle(rectangle, request.side_clearance_mm), owner)
            for owner, ((triangles, component_bounds), strip_width) in enumerate(
                zip(meshes, strip_widths, strict=True)
            )
            for rectangle in _component_rectangles(
                triangles,
                component_bounds,
                maximum_z=None if is_top_layer else upper_z + threshold,
                strip_width=strip_width,
            )
        ]
        current = {(tuple(round(value, 5) for value in rect), owner) for rect, owner in rectangles}
        for key, limits in list(active.items()):
            if key not in current:
                rect, owner = key
                boxes.append((*rect, limits[0], limits[1]))
                box_owners.append(owner)
                del active[key]
        prism_top_z = opening_top_z if is_top_layer else upper_z
        for key in current:
            active.setdefault(key, [lower_z, prism_top_z])[1] = prism_top_z
    for key, limits in active.items():
        rect, owner = key
        boxes.append((*rect, limits[0], limits[1]))
        box_owners.append(owner)

    prisms: list[Prism] = []
    owners: list[int] = []
    for box, owner in zip(boxes, box_owners, strict=True):
        if box[2] - box[0] > 1e-5 and box[3] - box[1] > 1e-5 and box[5] - box[4] > 1e-5:
            prisms.append(box)
            owners.append(owner)
    if not prisms:
        raise CreoMcpError("The product produced no cavity prisms to segment.")
    return prisms, owners, cavity_bottom, opening_top_z, layer_count, layer_height


def consolidate_prisms(
    prisms: list[Prism],
    *,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
    footprint_tolerance_mm: float = DEFAULT_FOOTPRINT_TOLERANCE_MM,
    min_step_depth_mm: float = 0.0,
    allow_cross_column_merge: bool = True,
    owners: list[int] | None = None,
) -> SegmentPlan:
    """Merge cavity prisms into at most ``max_segments`` stacked rectangular steps.

    ``owners`` optionally carries the index of the device each prism came from. When it
    is given, pockets are tracked per device, so a tray holding many devices gets many
    small pockets instead of one bounding box that swallows the air between them.

    ``min_step_depth_mm`` folds steps thinner than that threshold into whichever
    neighbour costs least material: a 1 mm ledge is not worth a separate cut feature
    that an engineer later has to find and edit in the model tree.

    ``max_segments`` is spent on real steps. Groups are only ever merged inside their
    own pocket column; when the budget still cannot be met, ``allow_cross_column_merge``
    lets two columns collapse as a last resort and the affected indices are reported in
    ``cross_column_merges`` instead of being silently absorbed into one bounding box.
    """
    if not prisms:
        raise CreoMcpError("No cavity prisms to consolidate.")
    if max_segments < 1:
        raise CreoMcpError("max_segments must be at least 1.")
    if min_step_depth_mm < 0:
        raise CreoMcpError("min_step_depth_mm must not be negative.")
    if owners is not None and len(owners) != len(prisms):
        raise CreoMcpError("owners must have one entry per cavity prism.")
    owner_of = list(owners) if owners is not None else [0] * len(prisms)

    cavity_bottom = min(prism[4] for prism in prisms)
    opening_top = max(prism[5] for prism in prisms)
    edges = sorted({round(value, 6) for prism in prisms for value in (prism[4], prism[5])})
    if len(edges) < 2:
        raise CreoMcpError("Cavity prisms do not span any depth band.")

    band_clusters: list[tuple[float, float, list[tuple[Rectangle, int]]]] = []
    union_volume = 0.0
    for lower, upper in itertools.pairwise(edges):
        present = [
            position
            for position, prism in enumerate(prisms)
            if prism[4] < upper - _EPSILON and prism[5] > lower + _EPSILON
        ]
        if not present:
            continue
        raw_rects = [_prism_rect(prisms[position]) for position in present]
        # Exact footprint of the band, before any rectangle-per-step simplification.
        union_volume += _union_area(raw_rects) * (upper - lower)
        # Cluster per owning device: two devices whose footprints touch must still cut
        # two pockets, which is how a real blister tray is made and what keeps the
        # bounding-box cost per device instead of per tray.
        clustered: list[tuple[Rectangle, int]] = []
        for owner in sorted({owner_of[position] for position in present}):
            member_rects = [
                _prism_rect(prisms[position])
                for position in present
                if owner_of[position] == owner
            ]
            clustered.extend(
                (rect, owner)
                for rect in _cluster_rects(member_rects, footprint_tolerance_mm)
            )
        band_clusters.append((lower, upper, sorted(clustered)))
    if not band_clusters:
        raise CreoMcpError("Cavity prisms produce no depth bands.")

    # Follow every footprint column upward through the bands. A column continues only
    # for its own device and only when a band cluster actually overlaps its current
    # footprint, so devices never collapse into one inflated bounding box.
    columns: list[list[Band]] = []
    column_owner: list[int] = []
    tails: list[int] = []
    for band_index, (lower, upper, clusters) in enumerate(band_clusters):
        for rect, owner in clusters:
            best: tuple[float, int] | None = None
            for column_index, tail in enumerate(tails):
                if tail != band_index - 1 or column_owner[column_index] != owner:
                    continue
                footprint = columns[column_index][-1][2]
                overlap = _rect_overlap_area(footprint, rect)
                if overlap <= footprint_tolerance_mm:
                    continue
                if best is None or overlap > best[0]:
                    best = (overlap, column_index)
            if best is None:
                columns.append([(lower, upper, rect)])
                column_owner.append(owner)
                tails.append(band_index)
            else:
                _overlap, column_index = best
                columns[column_index].append((lower, upper, rect))
                tails[column_index] = band_index

    # Split each column where its footprint changes: (column, bands) runs, deep -> top.
    groups: list[tuple[int, list[Band]]] = []
    for column_index, column in enumerate(columns):
        run: list[Band] = [column[0]]
        for band in column[1:]:
            if _rect_changed(band[2], run[-1][2], footprint_tolerance_mm):
                groups.append((column_index, run))
                run = [band]
            else:
                run.append(band)
        groups.append((column_index, run))

    # Fold thin ledges, but only onto a neighbour of the same column that touches them.
    if min_step_depth_mm > 0:
        while True:
            target: tuple[int, int] | None = None
            for position, (_column, group) in enumerate(groups):
                if _group_height(group) >= min_step_depth_mm:
                    continue
                for other in _same_column_neighbours(groups, position):
                    cost = _merge_cost(groups[other][1], group)
                    if target is None or cost < _merge_cost(groups[target[1]][1], groups[target[0]][1]):
                        target = (other, position)
            if target is None:
                break
            groups = _merge_groups(groups, target[0], target[1])

    cross_column: list[int] = []
    while len(groups) > max_segments:
        options = [
            (_merge_cost(groups[index][1], groups[index + 1][1]), index, False)
            for index in range(len(groups) - 1)
            if groups[index][0] == groups[index + 1][0]
            and _bands_touch(groups[index][1], groups[index + 1][1])
        ]
        if not options and allow_cross_column_merge:
            options = [
                (_merge_cost(groups[index][1], groups[index + 1][1]), index, True)
                for index in range(len(groups) - 1)
            ]
        if not options:
            break
        _cost, worst, crossed = min(options)
        groups = _merge_groups(groups, worst, worst + 1)
        if crossed:
            cross_column.append(worst)

    ordered = sorted(
        range(len(groups)),
        key=lambda position: (groups[position][0], -groups[position][1][-1][1]),
    )
    segments: list[CavitySegment] = []
    index_by_group: dict[int, int] = {}
    for position in ordered:  # per column, top (shallowest) first
        column_index, group = groups[position]
        rect = _group_rect(group)
        index = len(segments) + 1
        index_by_group[position] = index
        segments.append(
            CavitySegment(
                index=index,
                column_index=column_index + 1,
                device_index=column_owner[column_index] + 1,
                length_mm=rect[2] - rect[0],
                width_mm=rect[3] - rect[1],
                depth_mm=_group_height(group),
                x_min_mm=rect[0],
                y_min_mm=rect[1],
                z_bottom_mm=group[0][0],
                z_top_mm=group[-1][1],
                merged_bands=len(group),
            )
        )

    segments, enlarged = _enforce_insertion_stack(segments)
    # Reference = the finest staircase: every band cluster counted once per band, so
    # separated devices never contribute the air between them.
    reference_volume = sum(
        _rect_area(rect) * (upper - lower)
        for lower, upper, clustered in band_clusters
        for rect, _owner in clustered
    )
    prism_volume_sum = sum(
        _rect_area(_prism_rect(prism)) * (prism[5] - prism[4]) for prism in prisms
    )
    return SegmentPlan(
        segments=segments,
        reference_volume_mm3=reference_volume,
        segment_volume_mm3=sum(segment.volume_mm3 for segment in segments),
        prism_volume_sum_mm3=prism_volume_sum,
        union_volume_mm3=union_volume,
        cavity_bottom_mm=cavity_bottom,
        opening_top_mm=opening_top,
        enlarged_segments=enlarged,
        column_count=len(columns),
        device_count=len(set(column_owner)),
        cross_column_merges=sorted(
            index_by_group[position]
            for position in range(len(groups))
            if any(entry in (position, position + 1) for entry in cross_column)
        ),
    )


def _rect_changed(
    band: Rectangle,
    reference: Rectangle,
    tolerance_mm: float,
) -> bool:
    return any(
        abs(band_value - reference_value) > tolerance_mm
        for band_value, reference_value in zip(band, reference)
    )


def _prism_rect(prism: Prism) -> Rectangle:
    return (prism[0], prism[1], prism[2], prism[3])


def _rect_overlap_area(left: Rectangle, right: Rectangle) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _rects_touch(left: Rectangle, right: Rectangle, tolerance_mm: float) -> bool:
    return (
        min(left[2], right[2]) - max(left[0], right[0]) > -tolerance_mm
        and min(left[3], right[3]) - max(left[1], right[1]) > -tolerance_mm
    )


def _union_area(rects: list[Rectangle]) -> float:
    """Exact area covered by a set of rectangles (sweep over unique X edges).

    This separates two very different numbers: the cavity the planner actually cuts
    (the union of many thin component strips) and the cavity a K-slot native template
    must cut (one bounding rectangle per step). Their ratio is the representation cost
    of the template, and no merge policy can hide it.
    """
    if not rects:
        return 0.0
    x_edges = sorted({value for rect in rects for value in (rect[0], rect[2])})
    area = 0.0
    for left, right in itertools.pairwise(x_edges):
        span = [
            (rect[1], rect[3])
            for rect in rects
            if rect[0] <= left + _EPSILON and rect[2] >= right - _EPSILON
        ]
        if not span:
            continue
        covered_spans: list[list[float]] = []
        for low, high in sorted(span):
            if covered_spans and low <= covered_spans[-1][1]:
                covered_spans[-1][1] = max(covered_spans[-1][1], high)
            else:
                covered_spans.append([low, high])
        covered = sum(high - low for low, high in covered_spans)
        area += covered * (right - left)
    return area


def _cluster_rects(rects: list[Rectangle], tolerance_mm: float) -> list[Rectangle]:
    """Group one depth band's footprints into connected pockets.

    Rectangles that touch belong to the same pocket; everything else stays separate.
    """
    pending = list(rects)
    clusters: list[Rectangle] = []
    while pending:
        cluster = pending.pop()
        grew = True
        while grew:
            grew = False
            for rect in list(pending):
                if _rects_touch(cluster, rect, tolerance_mm):
                    cluster = _union_rect(cluster, rect)
                    pending.remove(rect)
                    grew = True
        clusters.append(cluster)
    return sorted(clusters, key=lambda rect: (rect[0], rect[1]))


def _segment_rect(segment: CavitySegment) -> Rectangle:
    return (
        segment.x_min_mm,
        segment.y_min_mm,
        segment.x_min_mm + segment.length_mm,
        segment.y_min_mm + segment.width_mm,
    )


def _bands_touch(deeper: list[Band], shallower: list[Band]) -> bool:
    return abs(deeper[-1][1] - shallower[0][0]) <= _EPSILON


def _same_column_neighbours(groups: list[tuple[int, list[Band]]], position: int) -> list[int]:
    column = groups[position][0]
    neighbours: list[int] = []
    for other in (position - 1, position + 1):
        if not 0 <= other < len(groups) or groups[other][0] != column:
            continue
        first, second = sorted((position, other))
        if _bands_touch(groups[first][1], groups[second][1]):
            neighbours.append(other)
    return neighbours


def _merge_groups(
    groups: list[tuple[int, list[Band]]], first: int, second: int
) -> list[tuple[int, list[Band]]]:
    low, high = min(first, second), max(first, second)
    merged_bands = sorted(groups[low][1] + groups[high][1], key=lambda band: band[0])
    merged: list[tuple[int, list[Band]]] = []
    for position, (column, band_list) in enumerate(groups):
        if position == low:
            merged.append((column, merged_bands))
        elif position == high:
            continue
        else:
            merged.append((column, band_list))
    return merged


def _enforce_insertion_stack(
    segments: list[CavitySegment],
) -> tuple[list[CavitySegment], list[int]]:
    """Cover every deeper step with a shallower one, enlarging the shallower only."""
    enlarged: list[int] = []
    by_column: dict[int, list[CavitySegment]] = {}
    for segment in segments:
        by_column.setdefault(segment.column_index, []).append(segment)

    for column_segments in by_column.values():
        shallower: list[CavitySegment] = []
        for segment in sorted(column_segments, key=lambda item: -item.z_top_mm):
            rect = _segment_rect(segment)
            if shallower and not any(
                _rect_contains(_segment_rect(item), rect) for item in shallower
            ):
                best = max(shallower, key=lambda item: _rect_overlap_area(_segment_rect(item), rect))
                merged = _union_rect(_segment_rect(best), rect)
                best.x_min_mm, best.y_min_mm = merged[0], merged[1]
                best.length_mm = merged[2] - merged[0]
                best.width_mm = merged[3] - merged[1]
                best.enlarged_for_insertion = True
                if best.index not in enlarged:
                    enlarged.append(best.index)
            shallower.append(segment)
    return segments, sorted(enlarged)


def analyse_product(
    request: ProductClearanceTrayPlanRequest,
    *,
    max_segments: int = DEFAULT_MAX_SEGMENTS,
    footprint_tolerance_mm: float = DEFAULT_FOOTPRINT_TOLERANCE_MM,
    min_step_depth_mm: float = 0.0,
    per_device: bool = False,
) -> dict[str, Any]:
    """Return the editable segment stack for one device, plus its conservatism.

    ``per_device`` tracks pockets separately for every solid in the STEP. It only helps
    when the solids sit side by side (a rack of separate items); when they are stacked
    or overlap in plan view — one product made of many parts — separate pockets cut
    *more* air, so it stays off unless the packaging unit is known to be per solid.
    """
    prisms, owners, cavity_bottom, opening_top, layer_count, layer_height = build_cavity_prisms(
        request
    )
    plan = consolidate_prisms(
        prisms,
        max_segments=max_segments,
        footprint_tolerance_mm=footprint_tolerance_mm,
        min_step_depth_mm=min_step_depth_mm,
        owners=owners if per_device else None,
    )
    path = Path(request.product_step_path)
    return {
        "product_step_path": str(path),
        "product_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "orientation": request.orientation.model_dump(),
        "requested_max_segments": max_segments,
        "per_device_pockets": per_device,
        "footprint_tolerance_mm": footprint_tolerance_mm,
        "min_step_depth_mm": min_step_depth_mm,
        "depth_layer_count": layer_count,
        "depth_layer_height_mm": round(layer_height, 4),
        "cavity_prism_count": len(prisms),
        "insertion_axis": request.insertion_axis,
        "clearance_mm": {
            "side": request.side_clearance_mm,
            "top": request.top_clearance_mm,
            "bottom": request.bottom_clearance_mm,
        },
        "segmentation": {
            **plan.to_dict(),
            "cavity_bottom_mm": round(cavity_bottom, 3),
            "opening_top_mm": round(opening_top, 3),
        },
    }


__all__ = [
    "DEFAULT_FOOTPRINT_TOLERANCE_MM",
    "DEFAULT_MAX_SEGMENTS",
    "CavitySegment",
    "Prism",
    "SegmentPlan",
    "analyse_product",
    "bottom_threshold",
    "build_cavity_prisms",
    "consolidate_prisms",
]
