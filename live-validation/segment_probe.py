"""Offline probe: collapse a device STEP into editable tray cavity segments.

Usage:
    python live-validation/segment_probe.py <device.step> [--k 6] [--min-step 1.5]
        [--side 1.5] [--top 2.0] [--bottom 1.5] [--flange-width 10.0]
        [--axes x,y,z] [--part seg_probe] [--out <dir>] [--json]

Read-only: this parses the STEP and prints the segment table. It never starts Creo
and never creates, changes or saves a model. Console output is ASCII-only.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from creo_mcp.cavity_segments import analyse_product
from creo_mcp.models import ProductClearanceTrayPlanRequest, ProductOrientation


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", help="device STEP file, must declare millimetres")
    parser.add_argument("--k", type=int, default=6, help="max segments (cut features)")
    parser.add_argument("--min-step", type=float, default=1.5, help="thin ledge threshold mm")
    parser.add_argument("--tolerance", type=float, default=0.05, help="footprint tolerance mm")
    parser.add_argument("--side", type=float, default=1.5, help="side clearance mm")
    parser.add_argument("--top", type=float, default=2.0, help="top clearance mm")
    parser.add_argument("--bottom", type=float, default=1.5, help="bottom clearance mm")
    parser.add_argument("--wall", type=float, default=1.0, help="wall thickness mm")
    parser.add_argument("--floor", type=float, default=1.0, help="bottom thickness mm")
    parser.add_argument("--flange-thickness", type=float, default=0.8, help="flange thickness mm")
    parser.add_argument("--flange-width", type=float, default=10.0, help="flange width mm")
    parser.add_argument(
        "--axes",
        default="x,y,z",
        help='device axis mapped to tray X,Y,Z, e.g. "y,-x,z" (draw direction = tray Z)',
    )
    parser.add_argument("--part", default="seg_probe", help="candidate part name")
    parser.add_argument("--out", default="", help="directory for the JSON report")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    parser.add_argument(
        "--per-device",
        action="store_true",
        help="track one pocket per solid (only right for a rack of separate items)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    tokens = [token.strip() for token in args.axes.split(",")]
    if len(tokens) != 3:
        print("ERROR --axes needs three entries, for example x,y,z or y,-x,z")
        return 2
    step_path = Path(args.step)
    if not step_path.is_file():
        print(f"ERROR file not found: {step_path}")
        return 2
    out_dir = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "creo-tray-segments"
    out_dir.mkdir(parents=True, exist_ok=True)

    request = ProductClearanceTrayPlanRequest(
        product_step_path=str(step_path.resolve()),
        orientation=ProductOrientation(tray_x_from=tokens[0], tray_y_from=tokens[1], tray_z_from=tokens[2]),
        side_clearance_mm=args.side,
        top_clearance_mm=args.top,
        bottom_clearance_mm=args.bottom,
        wall_thickness_mm=args.wall,
        bottom_thickness_mm=args.floor,
        flange_thickness_mm=args.flange_thickness,
        flange_width_mm=args.flange_width,
        part_name=args.part,
        output_directory=str(out_dir),
    )
    report = analyse_product(
        request,
        max_segments=args.k,
        footprint_tolerance_mm=args.tolerance,
        min_step_depth_mm=args.min_step,
        per_device=args.per_device,
    )
    segmentation = report["segmentation"]
    mouth = segmentation["opening_top_mm"]

    print(f"file            : {step_path.name}")
    print(f"sha256          : {report['product_sha256']}")
    print(f"draw direction  : tray Z <- device {tokens[2]}   insertion {report['insertion_axis']}")
    print(
        "clearances (mm) : "
        f"side {args.side} / top {args.top} / bottom {args.bottom} "
        f"(PLACEHOLDER values, confirm with process engineering)"
    )
    print(
        f"source geometry : {report['depth_layer_count']} layers x "
        f"{report['depth_layer_height_mm']} mm -> {report['cavity_prism_count']} prisms"
    )
    print(
        f"segments        : {segmentation['segment_count']} cut features "
        f"(K cap {args.k}, thin ledge >= {args.min_step} mm kept)"
    )
    print(
        f"cavity union    : {segmentation['cavity_union_volume_mm3']:.0f} mm3  "
        "(what the planner actually cuts in Creo)"
    )
    print(
        f"finest staircase: {segmentation['reference_staircase_volume_mm3']:.0f} mm3   "
        f"K-slot segments: {segmentation['segment_volume_mm3']:.0f} mm3   "
        f"over-cut +{segmentation['over_cut_ratio'] * 100:.2f} %"
    )
    print(
        f"pocket columns  : {segmentation['pocket_columns']}   "
        f"devices: {segmentation['device_count']}   "
        f"cross-column collapses: {segmentation['cross_column_merges'] or 'none'}"
    )
    print(
        f"mouth z         : {mouth:.3f} mm   cavity floor z: "
        f"{segmentation['cavity_bottom_mm']:.3f} mm"
    )
    print(
        "insertion fixups : "
        + ("none needed (deeper steps all fit inside the step above)"
           if not segmentation["enlarged_segments"]
           else f"enlarged steps {segmentation['enlarged_segments']}")
    )
    print()
    print("  dev col slot  length    width  cum_depth   x_off    y_off")
    print("              (mm)      (mm)      (mm)      (mm)     (mm)")
    for item in segmentation["segments"]:
        centre_x = item["x_min_mm"] + item["length_mm"] / 2.0
        centre_y = item["y_min_mm"] + item["width_mm"] / 2.0
        cumulative = mouth - item["z_bottom_mm"]
        print(
            f"  {item['device_index']:>3} {item['column_index']:>3} {item['index']:>4}  "
            f"{item['length_mm']:>8.2f}  {item['width_mm']:>8.2f}  "
            f"{cumulative:>10.2f}  {centre_x:>8.2f}  {centre_y:>8.2f}"
        )

    report_path = out_dir / f"{step_path.stem}.segments.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nreport          : {report_path}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
