"""OpenCascade helpers shared by hard-edge product-clearance geometry."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import CreoMcpError
from .models import BoxExtent


CONCEPT_DISPLAY_COLOR_RGB_255 = (66, 223, 208)



def _ocp() -> dict[str, Any]:
    try:
        from OCP.Bnd import Bnd_Box
        from OCP.BRepBndLib import BRepBndLib
        from OCP.BRepCheck import BRepCheck_Analyzer
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Pnt
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.Interface import Interface_Static
        from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
        from OCP.STEPCAFControl import STEPCAFControl_Writer
        from OCP.STEPControl import STEPControl_AsIs
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.TDocStd import TDocStd_Document
        from OCP.XCAFDoc import XCAFDoc_ColorType, XCAFDoc_DocumentTool
    except ImportError as exc:
        raise CreoMcpError(
            "Concept BREP generation requires the optional 'concept-geometry' extra. "
            "Install this project with creo-mcp[concept-geometry]."
        ) from exc
    return {
        "Bnd_Box": Bnd_Box,
        "BRepBndLib": BRepBndLib,
        "BRepCheck_Analyzer": BRepCheck_Analyzer,
        "BRepPrimAPI_MakeBox": BRepPrimAPI_MakeBox,
        "gp_Pnt": gp_Pnt,
        "IFSelect_RetDone": IFSelect_RetDone,
        "Interface_Static": Interface_Static,
        "Quantity_Color": Quantity_Color,
        "Quantity_TOC_sRGB": Quantity_TOC_sRGB,
        "STEPCAFControl_Writer": STEPCAFControl_Writer,
        "STEPControl_AsIs": STEPControl_AsIs,
        "TCollection_ExtendedString": TCollection_ExtendedString,
        "TDocStd_Document": TDocStd_Document,
        "XCAFDoc_ColorType": XCAFDoc_ColorType,
        "XCAFDoc_DocumentTool": XCAFDoc_DocumentTool,
    }


def _box(
    length: float,
    width: float,
    height: float,
    x: float,
    y: float,
    z: float,
    api: dict[str, Any],
) -> Any:
    return api["BRepPrimAPI_MakeBox"](
        api["gp_Pnt"](x, y, z), length, width, height
    ).Shape()


def measure_shape_extent(shape: Any) -> BoxExtent:
    """Measure an OpenCascade shape in its native millimetre coordinate system."""
    api = _ocp()
    bounds = api["Bnd_Box"]()
    api["BRepBndLib"].AddOptimal_s(shape, bounds, False, False)
    if bounds.IsVoid():
        raise CreoMcpError("Cannot measure an empty concept-tray shape.")
    x_min, y_min, z_min, x_max, y_max, z_max = bounds.Get()
    return BoxExtent(
        x_mm=x_max - x_min,
        y_mm=y_max - y_min,
        z_mm=z_max - z_min,
    )


def _write_colored_concept_step(shape: Any, output_path: Path, context: str) -> None:
    """Write AP242 with a display-only turquoise surface color for Creo import."""
    api = _ocp()
    document = api["TDocStd_Document"](
        api["TCollection_ExtendedString"]("XmlXCAF")
    )
    shape_label = api["XCAFDoc_DocumentTool"].ShapeTool_s(document.Main()).AddShape(
        shape,
        False,
    )
    red, green, blue = (channel / 255 for channel in CONCEPT_DISPLAY_COLOR_RGB_255)
    color = api["Quantity_Color"](
        red,
        green,
        blue,
        api["Quantity_TOC_sRGB"],
    )
    color_tool = api["XCAFDoc_DocumentTool"].ColorTool_s(document.Main())
    color_tool.SetColor(
        shape_label,
        color,
        api["XCAFDoc_ColorType"].XCAFDoc_ColorGen,
    )
    color_tool.SetColor(
        shape_label,
        color,
        api["XCAFDoc_ColorType"].XCAFDoc_ColorSurf,
    )

    api["Interface_Static"].SetCVal_s("write.step.schema", "AP242DIS")
    writer = api["STEPCAFControl_Writer"]()
    writer.SetColorMode(True)
    if not writer.Transfer(document, api["STEPControl_AsIs"]):
        raise CreoMcpError(f"OpenCascade could not transfer {context} to STEP.")
    if writer.Write(str(output_path)) != api["IFSelect_RetDone"]:
        raise CreoMcpError(f"OpenCascade could not write STEP file: {output_path}")
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise CreoMcpError(f"Generated STEP file is missing or empty: {output_path}")
