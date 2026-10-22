"""Isolated access to the official PTC Creo VB API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import winreg
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal

from .cad import CAD_IMPORT_TYPE, resolve_cad_source
from .concept_geometry import _write_colored_concept_step
from .clearance_geometry import (
    CLEARANCE_AUTOMATION_REVISION,
    export_product_clearance_tray_step,
    plan_product_clearance_tray,
)
from .errors import CreoMcpError
from .geometry_dsl import (
    BuildGeometryRequest,
    BuildGeometryResult,
    build_geometry_program,
)
from .models import (
    AppliedDimensionUpdate,
    AppliedModelItemRename,
    AppliedParameterUpdate,
    BoxDimensions,
    BoxExtent,
    CreatePackagingBoxRequest,
    CreatePackagingBoxResult,
    CreatePartRequest,
    CreatePartResult,
    CreateProductClearanceTrayRequest,
    CreateProductClearanceTrayResult,
    CreoDimensionInfo,
    CreoFeatureInfo,
    CreoModelInfo,
    CreoModelItemInfo,
    CreoParameterInfo,
    CreoStatus,
    EdgeGeometryInfo,
    ExportModelRequest,
    ExportModelResult,
    FindSurfacesRequest,
    FindSurfacesResult,
    GeometryBounds,
    GeometryDetailsResult,
    HandoffReportRequest,
    HandoffReportResult,
    ImportCadRequest,
    ImportCadResult,
    ImportStepRequest,
    ImportStepResult,
    InspectGeometryDetailsRequest,
    InspectModelGeometryRequest,
    InspectModelStructureRequest,
    InspectPartRequest,
    InspectPartResult,
    InspectStepRequest,
    ModelGeometryResult,
    ModelStructureResult,
    OpenNativeModelRequest,
    OpenNativeModelResult,
    ParameterUpdate,
    PlaceUdfRequest,
    PlaceUdfResult,
    ProductClearanceTrayPlan,
    ProductClearanceTrayPlanRequest,
    Point3D,
    RunUdfQualificationRequest,
    SaveModelAsRequest,
    SaveModelAsResult,
    SurfaceGeometryInfo,
    UdfQualificationRunResult,
    UpdateModelMetadataRequest,
    UpdateModelMetadataResult,
)
from .step import inspect_step_file, resolve_step_path
from .udf import load_qualified_udf, load_udf_manifest

CONCEPT_DISPLAY_COLOR_HEX = "#42DFD0"
CONCEPT_DISPLAY_FINISH = "MATTE_NON_METALLIC"
CONCEPT_DISPLAY_ROLE = "VISUALIZATION_ONLY_NOT_MATERIAL"
ASYNC_CONNECTION_CLSID = "{456E0110-2031-3907-AFE5-9201C97A915E}"
ASYNC_CONNECTION_PROG_ID = "pfcls.pfcAsyncConnection"
CREO_CONNECTION_MUTEX_NAME = r"Local\CreoMCP-VBAPI-Connection"
CREO_CONNECTION_LOCK_TIMEOUT_SECONDS = 5
CREO_CONNECT_TIMEOUT_SECONDS = 15
CREO_DISCONNECT_TIMEOUT_SECONDS = 5
CREO_CONNECTION_COOLDOWN_SECONDS = 3
MODEL_DESCRIPTOR_PROG_ID = "pfcls.pfcModelDescriptor"
RETRIEVE_OPTIONS_PROG_ID = "pfcls.pfcRetrieveModelOptions"
CREO_PART_MODEL_TYPE = 1
CREO_DIMENSION_ITEM_TYPE = 10
CREO_FEATURE_ITEM_TYPE = 0
CREO_SURFACE_ITEM_TYPE = 1
CREO_EDGE_ITEM_TYPE = 2
CREO_COORD_SYS_ITEM_TYPE = 3
CREO_AXIS_ITEM_TYPE = 4
CREO_POINT_ITEM_TYPE = 5
CREO_LENGTH_UNIT_TYPE = 0
CREO_ASSEMBLY_MODEL_TYPE = 0
CREO_STEP_IMPORT_TYPE = CAD_IMPORT_TYPE["step"]
UDF_CUSTOM_CREATE_INSTRUCTIONS_PROG_ID = "pfcls.pfcUDFCustomCreateInstructions"
UDF_REFERENCE_PROG_ID = "pfcls.pfcUDFReference"
UDF_VARIANT_DIMENSION_PROG_ID = "pfcls.pfcUDFVariantDimension"
UDF_REFERENCES_PROG_ID = "pfcls.pfcUDFReferences"
UDF_VARIANT_VALUES_PROG_ID = "pfcls.pfcUDFVariantValues"
SELECT_FACTORY_PROG_ID = "pfcls.MpfcSelect"
MODEL_ITEM_FACTORY_PROG_ID = "pfcls.MpfcModelItem"

FEATURE_STATUS_NAMES = {
    0: "active",
    1: "inactive",
    2: "family_table_suppressed",
    3: "simplified_representation_suppressed",
    4: "program_suppressed",
    5: "suppressed",
    6: "unregenerated",
}
PARAMETER_VALUE_TYPES = {
    0: ("string", "StringValue"),
    1: ("integer", "IntValue"),
    2: ("boolean", "BoolValue"),
    3: ("double", "DoubleValue"),
    4: ("note", "NoteId"),
}
UDF_REFERENCE_ITEM_TYPES = {
    "feature": CREO_FEATURE_ITEM_TYPE,
    "surface": CREO_SURFACE_ITEM_TYPE,
    "edge": CREO_EDGE_ITEM_TYPE,
    "coordinate_system": CREO_COORD_SYS_ITEM_TYPE,
    "axis": CREO_AXIS_ITEM_TYPE,
    "point": CREO_POINT_ITEM_TYPE,
}
EXPORT_INTF_3D_TYPES = {
    "step": 15,
    "iges": 14,
    "parasolid": 39,
}
EXPORT_EXTENSIONS = {
    "step": {".stp", ".step"},
    "iges": {".igs", ".iges"},
    "parasolid": {".x_t", ".x_b"},
}

_PERSISTENT_SESSION: tuple[Any, Path] | None = None
_PERSISTENT_SESSION_THREAD_ID: int | None = None
PARAMETER_VALUE_TYPE_CODES = {
    "string": 0,
    "integer": 1,
    "boolean": 2,
    "double": 3,
}
SURFACE_TYPE_NAMES = {
    0: "plane",
    1: "cylinder",
    2: "cone",
    3: "torus",
    4: "ruled",
    5: "revolved",
    6: "tabulated_cylinder",
    7: "fillet",
    8: "coons_patch",
    9: "spline",
    10: "nurbs",
    11: "cylindrical_spline",
    12: "spherical_spline",
    13: "foreign",
    14: "spline_second_derivative",
}
CURVE_TYPE_NAMES = {
    0: "composite",
    1: "point",
    2: "line",
    3: "arrow",
    4: "arc",
    5: "spline",
    6: "bspline",
    7: "circle",
    8: "ellipse",
    9: "polygon",
    10: "text",
}


def _creo_loadpoint_from_registry() -> Path:
    """Locate the Creo installation through the registered official VB API server."""
    key_path = rf"CLSID\{ASYNC_CONNECTION_CLSID}\LocalServer32"
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key_path) as key:
            raw_path, _ = winreg.QueryValueEx(key, None)
    except FileNotFoundError as exc:
        raise CreoMcpError(
            "Creo VB API is not registered. Run vb_api_register.bat as Administrator."
        ) from exc

    server_path = Path(str(raw_path).strip().strip('"'))
    # .../<Creo loadpoint>/Common Files/x86e_win64/obj/pfclscom.exe
    try:
        return server_path.parents[3]
    except IndexError as exc:
        raise CreoMcpError(f"Unexpected Creo VB API path: {server_path}") from exc


def _configure_creo_environment() -> Path:
    """Set the official VB API communication variables for this child process."""
    loadpoint = _creo_loadpoint_from_registry()
    common_files = loadpoint / "Common Files"
    communication_executable = common_files / "x86e_win64" / "obj" / "pro_comm_msg.exe"
    if not communication_executable.is_file():
        raise CreoMcpError(
            f"Creo communication executable was not found: {communication_executable}"
        )

    os.environ["PRO_DIRECTORY"] = str(common_files)
    os.environ["PRO_COMM_MSG_EXE"] = str(communication_executable)
    current_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{communication_executable.parent}{os.pathsep}{current_path}"
    return loadpoint


def _model_info(model: Any) -> CreoModelInfo | None:
    """Convert a dynamic COM model object into a stable JSON-safe payload."""
    if model is None:
        return None
    return CreoModelInfo(
        file_name=str(model.FileName),
        instance_name=_optional_string(model, "InstanceName"),
        model_type=_optional_string(model, "Type"),
    )


def _optional_string(com_object: Any, member: str) -> str | None:
    """Read an optional COM field without turning missing metadata into a failure."""
    try:
        value = getattr(com_object, member)
    except Exception:  # Dynamic COM proxies do not share a stable exception hierarchy.
        return None
    return None if value is None else str(value)


def _optional_int(com_object: Any, member: str) -> int | None:
    """Read an optional integer field from a dynamic COM proxy."""
    try:
        value = getattr(com_object, member)
    except Exception:
        return None
    return None if value is None else int(value)


def _optional_bool(com_object: Any, member: str) -> bool | None:
    """Read an optional boolean field from a dynamic COM proxy."""
    try:
        value = getattr(com_object, member)
    except Exception:
        return None
    return None if value is None else bool(value)


def _optional_name(com_object: Any) -> str | None:
    """Read a Creo model-item name without failing on unnamed geometry."""
    try:
        value = com_object.GetName()
    except Exception:
        try:
            value = com_object.Name
        except Exception:
            return None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _resolve_native_model_path(
    raw_path: str,
) -> tuple[Path, int, Literal["part", "assembly"]]:
    """Resolve a native Creo part or assembly, including versioned files."""
    unresolved = Path(raw_path).expanduser()
    if not unresolved.is_absolute():
        raise CreoMcpError(f"Native Creo model path must be absolute: {raw_path}")
    path = unresolved.resolve(strict=False)
    if not path.is_file():
        raise CreoMcpError(f"Native Creo model does not exist: {path}")
    match = re.search(r"\.(prt|asm)(?:\.\d+)?$", path.name, re.IGNORECASE)
    if match is None:
        raise CreoMcpError(f"Expected a native Creo .prt or .asm file: {path}")
    if match.group(1).casefold() == "prt":
        return path, CREO_PART_MODEL_TYPE, "part"
    return path, CREO_ASSEMBLY_MODEL_TYPE, "assembly"


def _resolve_part_path(raw_path: str) -> Path:
    """Resolve and validate a native Creo part path, including versioned .prt.N files."""
    path, _, model_kind = _resolve_native_model_path(raw_path)
    if model_kind != "part":
        raise CreoMcpError(f"Expected a native Creo .prt file: {path}")
    return path


def _dispatch_factory(prog_id: str) -> Any:
    """Create one registered PTC VB API helper factory."""
    try:
        import win32com.client
    except ImportError as exc:
        raise CreoMcpError("The Python dependency pywin32 is missing.") from exc
    return win32com.client.Dispatch(prog_id)


def _native_descriptor_name(model_path: Path) -> str:
    """Return the base instance name; the descriptor type supplies .prt or .asm."""
    match = re.search(r"\.(?:prt|asm)(?:\.\d+)?$", model_path.name, re.IGNORECASE)
    if match is None:
        raise CreoMcpError(f"Cannot derive a Creo model name from: {model_path}")
    return model_path.name[: match.start()]


def _native_descriptor_file_name(model_path: Path) -> str:
    """Return an unversioned Creo file name for CreateFromFileName."""
    match = re.search(r"\.(prt|asm)(?:\.\d+)?$", model_path.name, re.IGNORECASE)
    if match is None:
        raise CreoMcpError(f"Cannot derive a Creo file name from: {model_path}")
    return f"{model_path.name[: match.start()]}.{match.group(1).casefold()}"


def _retrieve_native_model(session: Any, model_path: Path, model_type: int) -> Any:
    """Retrieve one native model from an explicit path without displaying it."""
    descriptor_factory = _dispatch_factory(MODEL_DESCRIPTOR_PROG_ID)
    descriptor = descriptor_factory.Create(
        model_type,
        _native_descriptor_name(model_path),
        None,
    )
    descriptor.Path = str(model_path)

    options_factory = _dispatch_factory(RETRIEVE_OPTIONS_PROG_ID)
    options = options_factory.Create()
    options.AskUserAboutReps = False
    errors = []
    try:
        model = session.RetrieveModelWithOpts(descriptor, options)
    except Exception as exc:
        model = None
        errors.append(f"RetrieveModelWithOpts: {exc}")
    if model is not None:
        return model

    original_directory = str(session.GetCurrentDirectory())
    try:
        session.ChangeDirectory(str(model_path.parent))
        fallback_descriptor = descriptor_factory.CreateFromFileName(
            _native_descriptor_file_name(model_path)
        )
        model = session.RetrieveModel(fallback_descriptor)
    except Exception as exc:
        model = None
        errors.append(f"working-directory RetrieveModel: {exc}")
    finally:
        try:
            session.ChangeDirectory(original_directory)
        except Exception as exc:
            errors.append(f"restore working directory: {exc}")

    if model is None:
        raise CreoMcpError(
            f"Creo could not retrieve the native model at {model_path}. "
            f"Attempts: {'; '.join(errors)}"
        )
    return model


def _retrieve_part(session: Any, part_path: Path) -> Any:
    """Retrieve one native part from an explicit path without displaying it."""
    return _retrieve_native_model(session, part_path, CREO_PART_MODEL_TYPE)


def _sequence_item(sequence: Any, index: int) -> Any:
    """Read an item from a PTC COM sequence across pywin32 wrapper variants."""
    try:
        return sequence.Item(index)
    except TypeError:
        return sequence.Item[index]


def _list_dimensions(model: Any) -> list[CreoDimensionInfo]:
    """Return all driving dimensions exposed by a native Creo model."""
    raw_dimensions = model.ListItems(CREO_DIMENSION_ITEM_TYPE)
    if raw_dimensions is None:
        return []

    dimensions: list[CreoDimensionInfo] = []
    for index in range(int(raw_dimensions.Count)):
        dimension = _sequence_item(raw_dimensions, index)
        dimensions.append(
            CreoDimensionInfo(
                symbol=str(dimension.Symbol),
                value=float(dimension.DimValue),
                dimension_type=_optional_string(dimension, "DimType"),
                item_id=_optional_int(dimension, "Id"),
            )
        )
    return dimensions


def _bounded_sequence(sequence: Any, limit: int) -> tuple[list[Any], bool]:
    """Convert a PTC COM sequence to a bounded Python list."""
    if sequence is None:
        return [], False
    count = int(sequence.Count)
    return [_sequence_item(sequence, index) for index in range(min(count, limit))], count > limit


def _list_features(model: Any, limit: int) -> tuple[list[CreoFeatureInfo], bool]:
    raw_items, truncated = _bounded_sequence(model.ListItems(CREO_FEATURE_ITEM_TYPE), limit)
    features = []
    for feature in raw_items:
        feature_type = _optional_int(feature, "FeatType")
        status = _optional_int(feature, "Status")
        features.append(
            CreoFeatureInfo(
                item_id=int(feature.Id),
                name=_optional_name(feature),
                feature_type=feature_type,
                feature_type_name=_optional_string(feature, "FeatTypeName"),
                status=status,
                status_name=FEATURE_STATUS_NAMES.get(status),
            )
        )
    return features, truncated


def _feature_info(feature: Any) -> CreoFeatureInfo:
    feature_type = _optional_int(feature, "FeatType")
    status = _optional_int(feature, "Status")
    return CreoFeatureInfo(
        item_id=int(feature.Id),
        name=_optional_name(feature),
        feature_type=feature_type,
        feature_type_name=_optional_string(feature, "FeatTypeName"),
        status=status,
        status_name=FEATURE_STATUS_NAMES.get(status),
    )


def _list_reference_items(
    model: Any,
    item_type: int,
    item_type_name: Literal["surface", "edge", "axis", "point", "coordinate_system"],
    limit: int,
) -> tuple[list[CreoModelItemInfo], bool]:
    raw_items, truncated = _bounded_sequence(model.ListItems(item_type), limit)
    return [
        CreoModelItemInfo(
            item_id=int(item.Id),
            name=_optional_name(item),
            item_type=item_type_name,
        )
        for item in raw_items
    ], truncated


def _parameter_units(parameter: Any) -> str | None:
    try:
        units = parameter.Units
    except Exception:
        return None
    if units is None:
        return None
    return _optional_string(units, "Name")


def _parameter_info(parameter: Any) -> CreoParameterInfo | None:
    name = _optional_name(parameter)
    if name is None:
        return None
    try:
        raw_value = parameter.GetScaledValue()
        value_type = int(raw_value.discr)
    except Exception:
        return CreoParameterInfo(
            name=name,
            value_type="unset",
            value=None,
            units=_parameter_units(parameter),
        )

    type_and_member = PARAMETER_VALUE_TYPES.get(value_type)
    if type_and_member is None:
        return CreoParameterInfo(
            name=name,
            value_type="unset",
            value=None,
            units=_parameter_units(parameter),
        )
    type_name, member = type_and_member
    try:
        value = getattr(raw_value, member)
    except Exception:
        return CreoParameterInfo(
            name=name,
            value_type="unset",
            value=None,
            units=_parameter_units(parameter),
        )
    return CreoParameterInfo(
        name=name,
        value_type=type_name,
        value=value,
        units=_parameter_units(parameter),
    )


def _list_parameters(
    model: Any,
    limit: int,
) -> tuple[list[CreoParameterInfo], bool, int]:
    try:
        sequence = model.ListParams()
    except Exception as exc:
        if "XToolkitNotFound" in str(exc):
            sequence = None
        else:
            raise
    if sequence is None:
        return [], False, 0

    parameters: list[CreoParameterInfo] = []
    skipped_unnamed = 0
    count = int(sequence.Count)
    for index in range(count):
        parameter_info = _parameter_info(_sequence_item(sequence, index))
        if parameter_info is None:
            skipped_unnamed += 1
            continue
        if len(parameters) < limit:
            parameters.append(parameter_info)
    return parameters, count - skipped_unnamed > limit, skipped_unnamed


def _length_unit_name(model: Any) -> str:
    """Read the native model's principal length unit."""
    unit_system = model.GetPrincipalUnits()
    length_unit = unit_system.GetUnit(CREO_LENGTH_UNIT_TYPE)
    if length_unit is None:
        raise CreoMcpError("Creo did not report a principal length unit for the template.")
    return str(length_unit.Name)


def _require_millimetre_template(model: Any) -> str:
    """Reject templates whose driving dimensions are not expressed in millimetres."""
    unit_name = _length_unit_name(model)
    normalized = unit_name.casefold().replace(" ", "")
    if normalized not in {"mm", "millimeter", "millimeters", "millimetre", "millimetres"}:
        raise CreoMcpError(
            f"The packaging-box template must use millimetres; Creo reported {unit_name!r}."
        )
    return unit_name


def _dimension_map(model: Any) -> dict[str, Any]:
    """Index a model's native driving dimensions by case-insensitive symbol."""
    raw_dimensions = model.ListItems(CREO_DIMENSION_ITEM_TYPE)
    if raw_dimensions is None:
        raise CreoMcpError("The template does not expose any driving dimensions.")

    dimensions_by_symbol: dict[str, Any] = {}
    for index in range(int(raw_dimensions.Count)):
        dimension = _sequence_item(raw_dimensions, index)
        dimensions_by_symbol[str(dimension.Symbol).casefold()] = dimension
    return dimensions_by_symbol


def _require_box_dimension_symbols(model: Any, request: CreatePackagingBoxRequest) -> None:
    """Verify the complete dimension mapping before any output file is created."""
    dimensions_by_symbol = _dimension_map(model)
    required = [
        request.dimension_symbols.length,
        request.dimension_symbols.width,
        request.dimension_symbols.height,
    ]
    missing = [symbol for symbol in required if symbol.casefold() not in dimensions_by_symbol]
    if missing:
        available = sorted(str(dimension.Symbol) for dimension in dimensions_by_symbol.values())
        raise CreoMcpError(
            "Template is missing required driving dimensions "
            f"{missing}. Available dimensions: {available or 'none'}."
        )


def _apply_box_dimensions(model: Any, request: CreatePackagingBoxRequest) -> None:
    """Apply the three requested values to named native driving dimensions."""
    dimensions_by_symbol = _dimension_map(model)

    requested_values = {
        request.dimension_symbols.length: request.length_mm,
        request.dimension_symbols.width: request.width_mm,
        request.dimension_symbols.height: request.height_mm,
    }
    _require_box_dimension_symbols(model, request)

    for symbol, value in requested_values.items():
        dimensions_by_symbol[symbol.casefold()].DimValue = float(value)


def _point_coordinate(point: Any, index: int) -> float:
    try:
        return float(point.Item(index))
    except (AttributeError, TypeError):
        return float(point[index])


def _measure_extent(model: Any) -> BoxExtent:
    """Measure the regenerated axis-aligned model outline."""
    outline = model.EvalOutline(None, None)
    minimum = _sequence_item(outline, 0)
    maximum = _sequence_item(outline, 1)
    values = [
        abs(_point_coordinate(maximum, index) - _point_coordinate(minimum, index))
        for index in range(3)
    ]
    return BoxExtent(x_mm=values[0], y_mm=values[1], z_mm=values[2])


def _length_factor_to_millimetres(unit_name: str) -> float:
    """Return the multiplier from one Creo model length unit to millimetres."""
    normalized = unit_name.casefold().replace(" ", "")
    factors = {
        "mm": 1.0,
        "millimeter": 1.0,
        "millimeters": 1.0,
        "millimetre": 1.0,
        "millimetres": 1.0,
        "cm": 10.0,
        "centimeter": 10.0,
        "centimeters": 10.0,
        "m": 1000.0,
        "meter": 1000.0,
        "meters": 1000.0,
        "in": 25.4,
        "inch": 25.4,
        "inches": 25.4,
    }
    factor = factors.get(normalized)
    if factor is None:
        raise CreoMcpError(
            f"Cannot convert Creo length unit {unit_name!r} to millimetres."
        )
    return factor


def _extent_to_millimetres(extent: BoxExtent, unit_name: str) -> BoxExtent:
    """Convert a Creo model-unit extent into millimetres for stable tool output."""
    factor = _length_factor_to_millimetres(unit_name)
    return BoxExtent(
        x_mm=extent.x_mm * factor,
        y_mm=extent.y_mm * factor,
        z_mm=extent.z_mm * factor,
    )


def _point_to_millimetres(point: Any, factor: float) -> Point3D:
    return Point3D(
        x_mm=_point_coordinate(point, 0) * factor,
        y_mm=_point_coordinate(point, 1) * factor,
        z_mm=_point_coordinate(point, 2) * factor,
    )


def _geometry_bounds_to_millimetres(outline: Any, factor: float) -> GeometryBounds:
    first = _point_to_millimetres(_sequence_item(outline, 0), factor)
    second = _point_to_millimetres(_sequence_item(outline, 1), factor)
    return GeometryBounds(
        minimum=Point3D(
            x_mm=min(first.x_mm, second.x_mm),
            y_mm=min(first.y_mm, second.y_mm),
            z_mm=min(first.z_mm, second.z_mm),
        ),
        maximum=Point3D(
            x_mm=max(first.x_mm, second.x_mm),
            y_mm=max(first.y_mm, second.y_mm),
            z_mm=max(first.z_mm, second.z_mm),
        ),
    )


def _validate_extent(extent: BoxExtent, request: CreatePackagingBoxRequest) -> None:
    """Confirm that the copied native template regenerated to the requested envelope."""
    measured = sorted([extent.x_mm, extent.y_mm, extent.z_mm])
    expected = sorted([request.length_mm, request.width_mm, request.height_mm])
    tolerance = max(0.01, max(expected) * 1e-6)
    if any(abs(actual - target) > tolerance for actual, target in zip(measured, expected)):
        raise CreoMcpError(
            "Regenerated model extent does not match the requested dimensions. "
            f"Expected {expected} mm, measured {measured} mm."
        )


def _ensure_new_part_name(output_directory: Path, part_name: str) -> None:
    """Prevent Creo's versioned save format from silently reusing an existing model name."""
    expected_prefix = f"{part_name}.prt".casefold()
    conflicts = [
        path.name
        for path in output_directory.iterdir()
        if path.is_file()
        and (
            path.name.casefold() == expected_prefix
            or path.name.casefold().startswith(f"{expected_prefix}.")
        )
    ]
    if conflicts:
        raise CreoMcpError(
            f"Refusing to overwrite an existing Creo model named {part_name!r}: {conflicts}"
        )


def _ensure_model_not_loaded(session: Any, part_name: str) -> None:
    """Prevent a new model from colliding with an unsaved model already in session."""
    for model_type in (CREO_ASSEMBLY_MODEL_TYPE, CREO_PART_MODEL_TYPE):
        try:
            loaded_model = session.GetModel(part_name, model_type)
        except Exception:
            loaded_model = None
        if loaded_model is not None:
            raise CreoMcpError(
                f"Refusing to create {part_name!r}; that name is already loaded in Creo."
            )


def _saved_model_path(output_directory: Path, part_name: str) -> Path:
    """Locate the newest Creo version file created for the requested model name."""
    expected_prefix = f"{part_name}.prt".casefold()
    candidates = [
        path
        for path in output_directory.iterdir()
        if path.is_file()
        and (
            path.name.casefold() == expected_prefix
            or path.name.casefold().startswith(f"{expected_prefix}.")
        )
    ]
    if not candidates:
        raise CreoMcpError(
            f"Creo reported a successful save but no model file was found for {part_name!r}."
        )
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


def _activate_and_repaint_model(session: Any, model: Any) -> None:
    """Show a model only through Creo's standard display path.

    ``pfcModel.Display`` is the same session-level display path Creo uses for a
    normal open. Two other operations have been observed to corrupt Creo 13.4's
    graphics state (ORDER_DEBUG + later access violation) and are no longer used:
    ``CreateModelWindow`` and explicit window ``Activate`` calls.

    Set ``CREO_MCP_DISPLAY_MODE`` to control automatic display:
    ``none`` skips it, ``reopen`` (default) erases the in-session copy of a
    saved model and opens it again through File > Open, ``display_only`` calls
    ``pfcModel.Display`` (view only; graphics interaction stays dead on Creo
    13.4 because the session never really activated the model), ``openfile``
    reopens without erasing first (a no-op while the model is in the session),
    and ``display`` adds a current-window repaint.
    """
    mode = os.environ.get("CREO_MCP_DISPLAY_MODE", "reopen").strip().lower()
    if mode == "none":
        return
    if mode == "reopen":
        _reopen_saved_model(session, model)
        return
    if mode == "openfile":
        _open_saved_model(session, model)
        return
    if mode == "display_only":
        display = getattr(model, "Display", None)
        if callable(display):
            display()
        return
    display = getattr(model, "Display", None)
    if callable(display):
        try:
            display()
        except Exception as exc:
            raise CreoMcpError(
                "Creo created or loaded the model but could not display it. "
                f"Vendor error: {exc}"
            ) from exc
    else:
        # Older builds: refresh an existing window only, never create or activate one.
        try:
            window = session.GetModelWindow(model)
        except Exception:
            window = None
        if window is None:
            return
    _repaint_model_if_current(session, model)


def _reopen_saved_model(session: Any, model: Any) -> None:
    """Erase the in-session copy of a saved model, then open it like File > Open.

    ``session.OpenFile`` is a no-op display-wise while the model is already in
    the session, and ``pfcModel.Display`` alone leaves Creo 13.4 with a window
    whose graphics interaction never activates (clicks ignored, only mouse
    rotate works). Erasing first turns the reopen into a genuine Creo open:
    the model becomes the active model, the Navigator switches to the Model
    Tree, and clicks work exactly as after a manual open. Unsaved models fall
    back to ``Display``; models with unsaved edits cannot be erased and keep
    their current window instead of losing changes.
    """
    try:
        descr = model.Descr
        directory = Path(str(getattr(descr, "Path", "") or "").strip())
        file_name = str(getattr(model, "FileName", "") or "").strip()
        if not str(directory) or not file_name or not (directory / file_name).is_file():
            display = getattr(model, "Display", None)
            if callable(display):
                try:
                    display()
                except Exception:
                    pass
            return
        erase = getattr(model, "Erase", None)
        if callable(erase):
            try:
                erase()
            except Exception:
                pass
        window = session.OpenFile(descr)
        activate = getattr(window, "Activate", None)
        if callable(activate):
            activate()
    except Exception:
        # Display is best-effort; a failed reopen must not fail the CAD operation.
        return


def _open_saved_model(session: Any, model: Any) -> None:
    """Open a saved model through Creo's normal file-open path.

    This is the same session operation used by File > Open. It is the safest
    automatic display path on Creo 13.4, where API-created windows can corrupt
    the UI. Unsaved models are left alone; the caller still gets the result.
    """
    try:
        descr = model.Descr
        directory = Path(str(getattr(descr, "Path", "") or "").strip())
        file_name = str(getattr(model, "FileName", "") or "").strip()
        if not str(directory) or not file_name or not (directory / file_name).is_file():
            return
        window = session.OpenFile(descr)
        activate = getattr(window, "Activate", None)
        if callable(activate):
            activate()
    except Exception:
        # Display is best-effort; a failed reopen must not fail the CAD operation.
        return


def _repaint_model_if_current(session: Any, model: Any) -> bool:
    """Repaint an edited model when it is the model visible in the current window."""
    try:
        target_info = _model_info(model)
        current_info = _model_info(session.CurrentModel)
        if target_info is None or current_info != target_info:
            return False
        window = session.CurrentWindow
        if window is None:
            return False
        window.Repaint()
    except Exception as exc:
        raise CreoMcpError(
            "Creo regenerated the model but could not repaint the current window. "
            "The in-memory changes may already be present; inspect Creo before "
            f"retrying. Vendor error: {exc}"
        ) from exc
    return True


@contextmanager
def _serialized_creo_connection() -> Iterator[None]:
    """Allow only one local process to own a Creo VB API connection at a time."""
    try:
        import win32api
        import win32event
    except ImportError as exc:
        raise CreoMcpError("The Python dependency pywin32 is missing.") from exc

    mutex = win32event.CreateMutex(None, False, CREO_CONNECTION_MUTEX_NAME)
    acquired = False
    try:
        wait_result = win32event.WaitForSingleObject(
            mutex,
            CREO_CONNECTION_LOCK_TIMEOUT_SECONDS * 1000,
        )
        acquired = wait_result in (
            win32event.WAIT_OBJECT_0,
            win32event.WAIT_ABANDONED,
        )
        if not acquired:
            raise CreoMcpError(
                "Creo is busy with another MCP or VB API operation. Wait for that operation "
                "to finish before retrying; do not start concurrent Creo probes."
            )
        yield
    finally:
        if acquired:
            win32event.ReleaseMutex(mutex)
        win32api.CloseHandle(mutex)


@contextmanager
def _new_connected_session() -> Iterator[tuple[Any, Path]]:
    """Create one official VB API connection and always disconnect afterwards."""
    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise CreoMcpError("The Python dependency pywin32 is missing.") from exc

    with _serialized_creo_connection():
        loadpoint = _configure_creo_environment()
        factory = None
        connection = None
        pythoncom.CoInitialize()
        try:
            try:
                factory = win32com.client.Dispatch(ASYNC_CONNECTION_PROG_ID)
                connection = factory.Connect(
                    None,
                    None,
                    None,
                    CREO_CONNECT_TIMEOUT_SECONDS,
                )
            except CreoMcpError:
                raise
            except Exception as exc:  # Translate vendor COM errors into actionable diagnostics.
                raise CreoMcpError(
                    "Could not connect to Creo. Confirm that exactly one Creo session is running "
                    f"under this Windows account. Vendor error: {exc}"
                ) from exc
            yield connection.Session, loadpoint
        finally:
            if connection is not None:
                try:
                    connection.Disconnect(CREO_DISCONNECT_TIMEOUT_SECONDS)
                except Exception:
                    pass
            connection = None
            factory = None
            pythoncom.CoUninitialize()
            # pfclscom can remain in COM teardown briefly after Disconnect returns. Keep the
            # cross-process mutex during this window so another client cannot race activation.
            time.sleep(CREO_CONNECTION_COOLDOWN_SECONDS)


@contextmanager
def persistent_creo_connection() -> Iterator[None]:
    """Keep one VB API connection alive for a serialized long-running worker."""
    global _PERSISTENT_SESSION, _PERSISTENT_SESSION_THREAD_ID

    if _PERSISTENT_SESSION is not None:
        raise CreoMcpError("A persistent Creo connection is already active in this process.")

    with _new_connected_session() as connected:
        _PERSISTENT_SESSION = connected
        _PERSISTENT_SESSION_THREAD_ID = threading.get_ident()
        try:
            yield
        finally:
            _PERSISTENT_SESSION = None
            _PERSISTENT_SESSION_THREAD_ID = None


@contextmanager
def _connected_session() -> Iterator[tuple[Any, Path]]:
    """Reuse the worker connection or create one connection for a standalone call."""
    if (
        _PERSISTENT_SESSION is not None
        and _PERSISTENT_SESSION_THREAD_ID == threading.get_ident()
    ):
        yield _PERSISTENT_SESSION
        return

    with _new_connected_session() as connected:
        yield connected


def get_status() -> CreoStatus:
    """Read session status and active-model metadata without modifying Creo."""
    with _connected_session() as (session, loadpoint):
        return CreoStatus(
            connected=True,
            creo_loadpoint=str(loadpoint),
            working_directory=str(session.GetCurrentDirectory()),
            active_model=_model_info(session.CurrentModel),
        )


def create_empty_part(request: CreatePartRequest) -> CreatePartResult:
    """Create an unsaved part and optionally activate its window.

    This is intentionally the smallest write operation. It never saves a file,
    deletes a model, or changes an existing model's geometry.
    """
    with _connected_session() as (session, _):
        if request.output_directory is not None:
            output_directory = Path(request.output_directory).expanduser().resolve()
            if not output_directory.is_dir():
                raise CreoMcpError(
                    f"output_directory does not exist or is not a directory: {output_directory}"
                )
            session.ChangeDirectory(str(output_directory))

        part = session.CreatePart(request.part_name)
        if request.activate:
            _activate_and_repaint_model(session, part)

        created_model = _model_info(part)
        if created_model is None:
            raise CreoMcpError(
                "Creo reported that it created a part but did not return model metadata."
            )
        return CreatePartResult(
            created_model=created_model,
            working_directory=str(session.GetCurrentDirectory()),
            saved=False,
            activated=request.activate,
        )


def inspect_part(request: InspectPartRequest) -> InspectPartResult:
    """Inspect native dimensions and units without modifying or saving the source part."""
    part_path = _resolve_part_path(request.part_path)
    with _connected_session() as (session, _):
        model = _retrieve_part(session, part_path)
        model_info = _model_info(model)
        if model_info is None:
            raise CreoMcpError("Creo retrieved the part but returned no model metadata.")
        return InspectPartResult(
            model=model_info,
            part_path=str(part_path),
            length_unit=_length_unit_name(model),
            dimensions=_list_dimensions(model),
        )


def create_packaging_box(request: CreatePackagingBoxRequest) -> CreatePackagingBoxResult:
    """Create a saved, native solid envelope by copying a qualified Creo template."""
    template_path = _resolve_part_path(request.template_path)
    with _connected_session() as (session, _):
        if request.output_directory is not None:
            unresolved_output = Path(request.output_directory).expanduser()
            if not unresolved_output.is_absolute():
                raise CreoMcpError(
                    f"output_directory must be absolute: {request.output_directory}"
                )
            output_directory = unresolved_output.resolve(strict=False)
        else:
            output_directory = Path(str(session.GetCurrentDirectory())).resolve(strict=False)
        if not output_directory.is_dir():
            raise CreoMcpError(
                f"output_directory does not exist or is not a directory: {output_directory}"
            )
        _ensure_new_part_name(output_directory, request.part_name)
        _ensure_model_not_loaded(session, request.part_name)
        session.ChangeDirectory(str(output_directory))

        template = _retrieve_part(session, template_path)
        _require_millimetre_template(template)
        _require_box_dimension_symbols(template, request)
        created = template.CopyAndRetrieve(request.part_name, None)
        if created is None:
            raise CreoMcpError("Creo copied the template but did not return the new model.")

        _apply_box_dimensions(created, request)
        created.Regenerate(None)
        measured_extent = _measure_extent(created)
        _validate_extent(measured_extent, request)
        created.Save()

        if request.activate:
            _activate_and_repaint_model(session, created)

        created_model = _model_info(created)
        if created_model is None:
            raise CreoMcpError("Creo created the model but returned no model metadata.")
        model_path = _saved_model_path(output_directory, request.part_name)
        return CreatePackagingBoxResult(
            created_model=created_model,
            source_template=str(template_path),
            model_path=str(model_path),
            working_directory=str(output_directory),
            saved=True,
            activated=request.activate,
            applied_dimensions_mm=BoxDimensions(
                length_mm=request.length_mm,
                width_mm=request.width_mm,
                height_mm=request.height_mm,
            ),
            measured_extent_mm=measured_extent,
        )


def _clearance_parameter_updates(
    plan: ProductClearanceTrayPlan,
) -> list[ParameterUpdate]:
    """Return source ownership and hard-edge safety-envelope parameters."""
    values: list[tuple[str, str, str | int | float | bool]] = [
        ("MCP_AUTOMATION_ID", "string", "PRODUCT_CLEARANCE_TRAY"),
        ("MCP_AUTOMATION_REVISION", "integer", CLEARANCE_AUTOMATION_REVISION),
        ("MCP_MODEL_ROLE", "string", "PACKAGING_CONCEPT"),
        ("MCP_SOURCE_FORMAT", "string", "STEP_PRODUCT"),
        ("MCP_SOURCE_SHA256", "string", plan.product_sha256),
        ("MCP_MODEL_STATE", "string", "CONCEPT_ONLY"),
        ("MCP_PARAMETER_EDIT_MODE", "string", "REGENERATE_WITH_MCP"),
        ("MCP_DISPLAY_COLOR_HEX", "string", CONCEPT_DISPLAY_COLOR_HEX),
        ("MCP_DISPLAY_FINISH", "string", CONCEPT_DISPLAY_FINISH),
        ("MCP_DISPLAY_ROLE", "string", CONCEPT_DISPLAY_ROLE),
        ("PKG_CONCEPT_STAGE", "string", "HARD_EDGE_FIT"),
        ("PKG_CAVITY_MODE", "string", plan.cavity_geometry_mode.upper()),
        ("PKG_GEOMETRY_SOURCE", "string", plan.geometry_source_mode.upper()),
        ("PKG_MESH_DEFLECTION_MM", "double", plan.mesh_linear_deflection_mm),
        ("PKG_CLEAR_CONSERVATIVE_MM", "double", plan.clearance_conservatism_mm),
        ("PKG_DEPTH_LAYER_MM", "double", plan.depth_layer_height_mm),
        ("PKG_DEPTH_LAYER_COUNT", "integer", plan.depth_layer_count),
        ("PKG_VERTICAL_CONSERVATIVE_MM", "double", plan.vertical_conservatism_mm),
        ("PKG_INSERTION_AXIS", "string", plan.insertion_axis.upper()),
        ("PKG_INSERTION_VERIFIED", "boolean", plan.insertion_path_verified),
        ("PKG_STATIC_OUTSIDE_MM3", "double", plan.static_outside_volume_mm3),
        ("PKG_SWEEP_OUTSIDE_MM3", "double", plan.insertion_sweep_outside_volume_mm3),
        ("PKG_CORNER_RADIUS_MM", "double", plan.corner_radius_mm),
        ("PKG_DRAFT_EXPANSION_MM", "double", plan.draft_expansion_mm),
        ("PKG_HANDLING_RELIEFS", "string", plan.automatic_handling_reliefs.upper()),
        ("PKG_SOURCE_SOLID_COUNT", "integer", plan.source_solid_count),
        ("PKG_SIDE_CLEAR_MM", "double", plan.side_clearance_mm),
        ("PKG_TOP_CLEAR_MM", "double", plan.top_clearance_mm),
        ("PKG_BOTTOM_CLEAR_MM", "double", plan.bottom_clearance_mm),
        ("PKG_WALL_THICKNESS_MM", "double", plan.wall_thickness_mm),
        ("PKG_BOTTOM_THICKNESS_MM", "double", plan.bottom_thickness_mm),
        ("PKG_FLANGE_THICKNESS_MM", "double", plan.flange_thickness_mm),
        ("PKG_FLANGE_WIDTH_MM", "double", plan.flange_width_mm),
    ]
    return [
        ParameterUpdate(name=name, value_type=value_type, value=value)
        for name, value_type, value in values
    ]


def _apply_new_model_parameters(model: Any, updates: list[ParameterUpdate]) -> None:
    factory = _dispatch_factory(MODEL_ITEM_FACTORY_PROG_ID)
    for update in updates:
        try:
            existing = model.GetParam(update.name)
        except Exception as exc:
            if "XToolkitNotFound" in str(exc):
                existing = None
            else:
                raise CreoMcpError(
                    f"Could not inspect generated model parameter {update.name!r}."
                ) from exc
        if existing is not None:
            raise CreoMcpError(
                f"Generated model unexpectedly already contains parameter {update.name!r}."
            )
        model.CreateParam(update.name, _parameter_value(factory, update))


def _rename_generated_import_feature(
    model: Any,
    name: str = "CONCEPT_TRAY_BREP",
) -> None:
    raw_features, _ = _bounded_sequence(model.ListItems(CREO_FEATURE_ITEM_TYPE), 100000)
    imported = [feature for feature in raw_features if int(feature.FeatType) == 22]
    if len(imported) != 1:
        raise CreoMcpError(
            "Generated concept model must contain exactly one imported BREP feature; "
            f"Creo returned {len(imported)}."
        )
    imported[0].SetName(name)


def _validate_expected_extent(measured: BoxExtent, expected: BoxDimensions) -> None:
    requested = sorted((expected.length_mm, expected.width_mm, expected.height_mm))
    actual = sorted((measured.x_mm, measured.y_mm, measured.z_mm))
    for requested_value, actual_value in zip(requested, actual, strict=True):
        tolerance = max(0.05, abs(requested_value) * 0.001)
        if abs(requested_value - actual_value) > tolerance:
            raise CreoMcpError(
                "Generated concept geometry does not match its planned extent: "
                f"expected {expected.model_dump()}, measured {measured.model_dump()}."
            )


def create_product_clearance_tray(
    request: CreateProductClearanceTrayRequest,
    *,
    progress_cb: Any = None,
) -> CreateProductClearanceTrayResult:
    """Create one protected hard-edge tray from a verified product-clearance plan."""
    output_directory = Path(request.output_directory).expanduser()
    if not output_directory.is_absolute():
        raise CreoMcpError(f"output_directory must be absolute: {request.output_directory}")
    output_directory = output_directory.resolve(strict=False)
    if not output_directory.is_dir():
        raise CreoMcpError(
            f"output_directory does not exist or is not a directory: {output_directory}"
        )
    _ensure_new_part_name(output_directory, request.part_name)

    _progress(progress_cb, 5, "正在复核方案与产品文件")
    planning_request = ProductClearanceTrayPlanRequest.model_validate(
        request.model_dump(exclude={"plan_id", "confirmation", "activate"})
    )
    plan = plan_product_clearance_tray(
        planning_request,
        progress_cb=(
            None
            if progress_cb is None
            else lambda v, m: _progress(progress_cb, 5 + v * 0.4, m)
        ),
    )
    if request.plan_id != plan.plan_id:
        raise CreoMcpError(
            "plan_id does not match the current product file and clearance parameters."
        )

    result: CreateProductClearanceTrayResult | None = None
    with tempfile.TemporaryDirectory(prefix="creo-mcp-clearance-") as raw_directory:
        generated_step = Path(raw_directory) / f"{request.part_name}.step"
        _progress(progress_cb, 50, "正在生成避让 STEP 几何")
        generated_extent = export_product_clearance_tray_step(plan, generated_step)
        _progress(progress_cb, 55, "正在校验生成的 STEP")
        _validate_expected_extent(generated_extent, plan.expected_extent_mm)
        source = inspect_step_file(InspectStepRequest(step_path=str(generated_step)))
        if (
            source.inferred_model_kind != "part"
            or source.solid_body_count != 1
            or source.closed_shell_count != 1
        ):
            raise CreoMcpError(
                "Generated product-clearance STEP did not validate as one closed solid part."
            )

        with _connected_session() as (session, _):
            _progress(progress_cb, 60, "正在导入 Creo")
            _ensure_model_not_loaded(session, request.part_name)
            session.ChangeDirectory(str(output_directory))
            created, detected_type = _import_new_model(
                session,
                generated_step,
                "generated product-clearance concept STEP",
                CREO_STEP_IMPORT_TYPE,
                request.part_name,
            )
            if detected_type != CREO_PART_MODEL_TYPE:
                raise CreoMcpError("Creo did not import the generated concept as a part.")
            _progress(progress_cb, 80, "正在命名特征并再生")
            _rename_generated_import_feature(created, "PRODUCT_CLEARANCE_BREP")
            _apply_new_model_parameters(created, _clearance_parameter_updates(plan))
            created.Regenerate(None)

            _progress(progress_cb, 90, "正在测量并保存")
            length_unit = _length_unit_name(created)
            measured_extent = _extent_to_millimetres(_measure_extent(created), length_unit)
            _validate_expected_extent(measured_extent, plan.expected_extent_mm)
            created.Save()

            if request.activate:
                _progress(progress_cb, 95, "正在打开模型窗口")
                _activate_and_repaint_model(session, created)

            created_model = _model_info(created)
            if created_model is None:
                raise CreoMcpError(
                    "Creo created the product-clearance tray but returned no model metadata."
                )
            result = CreateProductClearanceTrayResult(
                created_model=created_model,
                model_path=str(_saved_model_path(output_directory, request.part_name)),
                working_directory=str(output_directory),
                saved=True,
                activated=request.activate,
                model_state="CONCEPT_ONLY",
                creo_geometry_kind="imported_brep",
                parameter_update_mode="regenerate_with_mcp",
                generated_step_deleted=True,
                source_product_path=plan.product_step_path,
                source_product_sha256=plan.product_sha256,
                applied_plan=plan,
                measured_extent_mm=measured_extent,
            )
            _progress(progress_cb, 100, "托盘创建完成")

    if result is None:
        raise CreoMcpError("Product-clearance concept creation did not produce a result.")
    return result


def _import_new_model(
    session: Any,
    source_path: Path,
    source_format: str,
    import_type: int,
    model_name: str,
) -> tuple[Any, int]:
    """Import one validated CAD source into a new unsaved Creo model."""
    _ensure_model_not_loaded(session, model_name)
    try:
        detected_type = int(session.GetImportSourceType(str(source_path), import_type))
        created = session.ImportNewModel(
            str(source_path),
            import_type,
            detected_type,
            model_name,
            None,
        )
    except Exception as exc:
        raise CreoMcpError(
            f"Creo could not import the {source_format} file at {source_path}. "
            f"Vendor error: {exc}"
        ) from exc
    if created is None:
        raise CreoMcpError(f"Creo did not return an imported model for {source_path}.")
    return created, detected_type


def _model_kind(model_type: int | None) -> str:
    return {
        CREO_ASSEMBLY_MODEL_TYPE: "assembly",
        CREO_PART_MODEL_TYPE: "part",
    }.get(model_type, "other")


def _volume_in_millimetres(model: Any, length_unit: str) -> float | None:
    factor = _length_factor_to_millimetres(length_unit)
    try:
        return float(model.GetMassProperty(None).Volume) * factor**3
    except Exception:
        return None


def _progress(cb: Any, value: float, message: str) -> None:
    """Report one progress step through an optional callback; never fails the work."""
    if cb is None:
        return
    try:
        cb(value, message)
    except Exception:
        pass


def open_native_model(
    request: OpenNativeModelRequest,
    *,
    progress_cb: Any = None,
) -> OpenNativeModelResult:
    """Load and measure a native Creo part or assembly without saving it."""
    model_path, model_type, model_kind = _resolve_native_model_path(request.model_path)
    with _connected_session() as (session, _):
        _progress(progress_cb, 15, "正在检索原生模型")
        model = _retrieve_native_model(session, model_path, model_type)
        _progress(progress_cb, 50, "正在测量包络与体积")
        length_unit = _length_unit_name(model)
        measured_extent = _extent_to_millimetres(_measure_extent(model), length_unit)
        volume_mm3 = _volume_in_millimetres(model, length_unit)
        if request.activate:
            _progress(progress_cb, 75, "正在打开模型窗口")
            _activate_and_repaint_model(session, model)

        model_info = _model_info(model)
        if model_info is None:
            raise CreoMcpError("Creo loaded the native model but returned no model metadata.")
        _progress(progress_cb, 100, "打开完成")
        return OpenNativeModelResult(
            source_path=str(model_path),
            model=model_info,
            model_kind=model_kind,
            length_unit=length_unit,
            measured_extent_mm=measured_extent,
            volume_mm3=volume_mm3,
            source_modified=False,
            activated=request.activate,
        )


def import_cad(
    request: ImportCadRequest,
    *,
    progress_cb: Any = None,
) -> ImportCadResult:
    """Import a supported CAD file as a new unsaved Creo model and measure it."""
    source = resolve_cad_source(request)
    source_path = Path(source.source_path)
    import_type = CAD_IMPORT_TYPE[source.source_format]
    with _connected_session() as (session, _):
        _progress(progress_cb, 20, f"正在将 {source.source_format} 导入 Creo")
        created, detected_type = _import_new_model(
            session,
            source_path,
            source.source_format,
            import_type,
            request.model_name,
        )

        _progress(progress_cb, 60, "正在测量几何")
        length_unit = _length_unit_name(created)
        measured_extent = _extent_to_millimetres(_measure_extent(created), length_unit)
        volume_mm3 = _volume_in_millimetres(created, length_unit)
        if request.activate:
            _progress(progress_cb, 85, "正在打开模型窗口")
            _activate_and_repaint_model(session, created)

        created_model = _model_info(created)
        if created_model is None:
            raise CreoMcpError("Creo imported the CAD file but returned no model metadata.")
        return ImportCadResult(
            source=source,
            created_model=created_model,
            detected_model_kind=_model_kind(detected_type),
            working_directory=str(session.GetCurrentDirectory()),
            length_unit=length_unit,
            measured_extent_mm=measured_extent,
            volume_mm3=volume_mm3,
            saved=False,
            activated=request.activate,
        )


def import_step(
    request: ImportStepRequest,
    *,
    progress_cb: Any = None,
) -> ImportStepResult:
    """Import a STEP file as a new unsaved Creo part or assembly and measure it."""
    step_path = resolve_step_path(request.step_path)
    _progress(progress_cb, 10, "正在校验 STEP 文件")
    source = inspect_step_file(InspectStepRequest(step_path=str(step_path)))
    with _connected_session() as (session, _):
        _progress(progress_cb, 25, "正在将 STEP 导入 Creo")
        created, detected_type = _import_new_model(
            session,
            step_path,
            "STEP",
            CREO_STEP_IMPORT_TYPE,
            request.model_name,
        )

        _progress(progress_cb, 65, "正在测量几何")
        length_unit = _length_unit_name(created)
        measured_extent = _extent_to_millimetres(_measure_extent(created), length_unit)
        if request.activate:
            _progress(progress_cb, 85, "正在打开模型窗口")
            _activate_and_repaint_model(session, created)

        created_model = _model_info(created)
        if created_model is None:
            raise CreoMcpError("Creo imported the STEP file but returned no model metadata.")
        return ImportStepResult(
            source=source,
            created_model=created_model,
            detected_model_kind=_model_kind(detected_type),
            working_directory=str(session.GetCurrentDirectory()),
            length_unit=length_unit,
            measured_extent_mm=measured_extent,
            saved=False,
            activated=request.activate,
        )


def build_geometry(
    request: BuildGeometryRequest,
    *,
    progress_cb: Any = None,
) -> BuildGeometryResult:
    """Build one JSON-program solid in OpenCascade and import it as a new saved part.

    The program is validated to exactly one closed solid before Creo is touched;
    the import then follows the same guarded path as the clearance-tray pipeline
    (fresh name, refused overwrite, named feature, save, real open).
    """
    output_directory = Path(request.output_directory).expanduser()
    if not output_directory.is_absolute():
        raise CreoMcpError(
            f"output_directory must be absolute: {request.output_directory}"
        )
    output_directory = output_directory.resolve(strict=False)
    if not output_directory.is_dir():
        raise CreoMcpError(
            f"output_directory does not exist or is not a directory: {output_directory}"
        )
    _ensure_new_part_name(output_directory, request.part_name)

    program_sha256 = hashlib.sha256(
        json.dumps(
            request.program.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    _progress(progress_cb, 10, "正在构建几何程序")
    outcome = build_geometry_program(request.program)

    result: BuildGeometryResult | None = None
    with tempfile.TemporaryDirectory(prefix="creo-mcp-build-") as raw_directory:
        generated_step = Path(raw_directory) / f"{request.part_name}.step"
        _progress(progress_cb, 45, "正在导出并校验 STEP")
        _write_colored_concept_step(
            outcome.shape, generated_step, "generated build_geometry solid"
        )
        source = inspect_step_file(InspectStepRequest(step_path=str(generated_step)))
        if (
            source.inferred_model_kind != "part"
            or source.solid_body_count != 1
            or source.closed_shell_count != 1
        ):
            raise CreoMcpError(
                "Generated geometry did not export as one closed solid part."
            )

        with _connected_session() as (session, _):
            _progress(progress_cb, 60, "正在导入 Creo")
            _ensure_model_not_loaded(session, request.part_name)
            session.ChangeDirectory(str(output_directory))
            created, detected_type = _import_new_model(
                session,
                generated_step,
                "generated build_geometry STEP",
                CREO_STEP_IMPORT_TYPE,
                request.part_name,
            )
            if detected_type != CREO_PART_MODEL_TYPE:
                raise CreoMcpError("Creo did not import the generated solid as a part.")
            _progress(progress_cb, 78, "正在命名特征")
            _rename_generated_import_feature(created, "GENERATED_SOLID")
            _progress(progress_cb, 88, "正在测量并保存")
            length_unit = _length_unit_name(created)
            measured_extent = _extent_to_millimetres(
                _measure_extent(created), length_unit
            )
            created.Save()
            if request.activate:
                _progress(progress_cb, 95, "正在打开模型窗口")
                _activate_and_repaint_model(session, created)

            created_model = _model_info(created)
            if created_model is None:
                raise CreoMcpError(
                    "Creo created the generated solid but returned no model metadata."
                )
            result = BuildGeometryResult(
                created_model=created_model,
                model_path=str(_saved_model_path(output_directory, request.part_name)),
                working_directory=str(output_directory),
                saved=True,
                activated=request.activate,
                node_count=outcome.node_count,
                program_sha256=program_sha256,
                measured_extent_mm=measured_extent,
                volume_mm3=outcome.volume_mm3,
                generated_step_deleted=True,
                model_state="GENERATED_SOLID",
            )
            _progress(progress_cb, 100, "几何构建完成")

    if result is None:  # pragma: no cover - defensive
        raise CreoMcpError("build_geometry did not produce a result.")
    return result


def _get_loaded_model(
    session: Any,
    model_name: str | None,
    model_kind: Literal["part", "assembly"] | None,
) -> Any:
    """Resolve the active model or one explicitly named model already in session."""
    if model_name is None:
        try:
            model = session.CurrentModel
        except Exception as exc:
            raise CreoMcpError("Creo has no active model to inspect.") from exc
    else:
        requested_type = {
            "assembly": CREO_ASSEMBLY_MODEL_TYPE,
            "part": CREO_PART_MODEL_TYPE,
        }[model_kind]
        try:
            model = session.GetModel(model_name, requested_type)
        except Exception as exc:
            raise CreoMcpError(f"Creo model {model_name!r} is not loaded in the session.") from exc
    if model is None:
        raise CreoMcpError("The requested Creo model is not loaded in the session.")
    return model


def inspect_model_structure(request: InspectModelStructureRequest) -> ModelStructureResult:
    """Return bounded, stable references for one loaded Creo model."""
    with _connected_session() as (session, _):
        model = _get_loaded_model(session, request.model_name, request.model_kind)
        model_info = _model_info(model)
        if model_info is None:
            raise CreoMcpError("Creo returned no metadata for the requested model.")

        limit = request.max_items_per_category
        features, features_truncated = _list_features(model, limit)
        surfaces, surfaces_truncated = _list_reference_items(
            model, CREO_SURFACE_ITEM_TYPE, "surface", limit
        )
        edges, edges_truncated = _list_reference_items(
            model, CREO_EDGE_ITEM_TYPE, "edge", limit
        )
        axes, axes_truncated = _list_reference_items(
            model, CREO_AXIS_ITEM_TYPE, "axis", limit
        )
        points, points_truncated = _list_reference_items(
            model, CREO_POINT_ITEM_TYPE, "point", limit
        )
        coordinate_systems, coordinate_systems_truncated = _list_reference_items(
            model, CREO_COORD_SYS_ITEM_TYPE, "coordinate_system", limit
        )
        dimensions = _list_dimensions(model)
        dimensions_truncated = len(dimensions) > limit
        dimensions = dimensions[:limit]
        parameters, parameters_truncated, skipped_unnamed_parameters = _list_parameters(
            model, limit
        )

        truncated_categories = [
            category
            for category, truncated in (
                ("features", features_truncated),
                ("surfaces", surfaces_truncated),
                ("edges", edges_truncated),
                ("axes", axes_truncated),
                ("points", points_truncated),
                ("coordinate_systems", coordinate_systems_truncated),
                ("dimensions", dimensions_truncated),
                ("parameters", parameters_truncated),
            )
            if truncated
        ]
        return ModelStructureResult(
            model=model_info,
            model_kind=_model_kind(_optional_int(model, "Type")),
            features=features,
            surfaces=surfaces,
            edges=edges,
            axes=axes,
            points=points,
            coordinate_systems=coordinate_systems,
            dimensions=dimensions,
            parameters=parameters,
            skipped_unnamed_parameter_count=skipped_unnamed_parameters,
            truncated_categories=truncated_categories,
        )


def _resolve_geometry_items(
    model: Any,
    item_type: int,
    item_ids: list[int],
    item_type_name: str,
) -> list[Any]:
    """Resolve every requested geometry ID before evaluating any item."""
    items = []
    for item_id in item_ids:
        try:
            item = model.GetItemById(item_type, item_id)
        except Exception as exc:
            raise CreoMcpError(
                f"Could not resolve {item_type_name} ID {item_id} in the target model."
            ) from exc
        if item is None:
            raise CreoMcpError(
                f"The target model does not contain {item_type_name} ID {item_id}."
            )
        items.append(item)
    return items


def _geometry_feature(item: Any, errors: list[str]) -> CreoFeatureInfo | None:
    try:
        feature = item.GetFeature()
    except Exception:
        errors.append("owning_feature_unavailable")
        return None
    if feature is None:
        errors.append("owning_feature_unavailable")
        return None
    return _feature_info(feature)


def _surface_geometry_info(surface: Any, factor: float) -> SurfaceGeometryInfo:
    errors: list[str] = []
    try:
        surface_type = int(surface.GetSurfaceType())
    except Exception:
        surface_type = None
        errors.append("surface_type_unavailable")
    try:
        area_mm2 = float(surface.EvalArea()) * factor**2
    except Exception:
        area_mm2 = None
        errors.append("area_unavailable")
    try:
        bounds = _geometry_bounds_to_millimetres(surface.GetXYZExtents(), factor)
    except Exception:
        bounds = None
        errors.append("bounds_unavailable")
    return SurfaceGeometryInfo(
        item_id=int(surface.Id),
        name=_optional_name(surface),
        surface_type=surface_type,
        surface_type_name=SURFACE_TYPE_NAMES.get(surface_type),
        visible=_optional_bool(surface, "IsVisible"),
        area_mm2=area_mm2,
        bounds_mm=bounds,
        feature=_geometry_feature(surface, errors),
        evaluation_errors=errors,
    )


def _edge_point(edge: Any, parameter: float, factor: float) -> Point3D:
    evaluated = edge.Eval3DData(parameter)
    return _point_to_millimetres(evaluated.Point, factor)


def _edge_geometry_info(edge: Any, factor: float) -> EdgeGeometryInfo:
    errors: list[str] = []
    try:
        descriptor = edge.GetCurveDescriptor()
        curve_type = int(descriptor.GetCurveType())
    except Exception:
        curve_type = None
        errors.append("curve_type_unavailable")
    try:
        length_mm = float(edge.EvalLength()) * factor
    except Exception:
        length_mm = None
        errors.append("length_unavailable")

    evaluated_points: list[Point3D | None] = []
    for parameter, error_name in (
        (0.0, "start_point_unavailable"),
        (0.5, "midpoint_unavailable"),
        (1.0, "end_point_unavailable"),
    ):
        try:
            evaluated_points.append(_edge_point(edge, parameter, factor))
        except Exception:
            evaluated_points.append(None)
            errors.append(error_name)

    adjacent_surface_ids = []
    for member in ("Surface1", "Surface2"):
        try:
            surface = getattr(edge, member)
        except Exception:
            surface = None
        if surface is not None:
            surface_id = int(surface.Id)
            if surface_id not in adjacent_surface_ids:
                adjacent_surface_ids.append(surface_id)

    return EdgeGeometryInfo(
        item_id=int(edge.Id),
        name=_optional_name(edge),
        curve_type=curve_type,
        curve_type_name=CURVE_TYPE_NAMES.get(curve_type),
        visible=_optional_bool(edge, "IsVisible"),
        length_mm=length_mm,
        start_point_mm=evaluated_points[0],
        midpoint_mm=evaluated_points[1],
        end_point_mm=evaluated_points[2],
        adjacent_surface_ids=adjacent_surface_ids,
        feature=_geometry_feature(edge, errors),
        evaluation_errors=errors,
    )


def inspect_geometry_details(
    request: InspectGeometryDetailsRequest,
) -> GeometryDetailsResult:
    """Return normalized geometric details for explicit surface and edge IDs."""
    with _connected_session() as (session, _):
        model = _get_loaded_model(session, request.model_name, request.model_kind)
        model_info = _model_info(model)
        if model_info is None:
            raise CreoMcpError("Creo returned no metadata for the requested model.")

        surfaces = _resolve_geometry_items(
            model, CREO_SURFACE_ITEM_TYPE, request.surface_ids, "surface"
        )
        edges = _resolve_geometry_items(model, CREO_EDGE_ITEM_TYPE, request.edge_ids, "edge")
        length_unit = _length_unit_name(model)
        factor = _length_factor_to_millimetres(length_unit)
        return GeometryDetailsResult(
            model=model_info,
            model_kind=_model_kind(_optional_int(model, "Type")),
            length_unit=length_unit,
            surfaces=[_surface_geometry_info(surface, factor) for surface in surfaces],
            edges=[_edge_geometry_info(edge, factor) for edge in edges],
        )


def _surface_matches(request: FindSurfacesRequest, surface: SurfaceGeometryInfo) -> bool:
    if request.surface_types and surface.surface_type_name not in request.surface_types:
        return False
    if request.minimum_area_mm2 is not None and (
        surface.area_mm2 is None or surface.area_mm2 < request.minimum_area_mm2
    ):
        return False
    if request.maximum_area_mm2 is not None and (
        surface.area_mm2 is None or surface.area_mm2 > request.maximum_area_mm2
    ):
        return False
    if request.constant_axis is not None:
        if surface.bounds_mm is None or request.axis_coordinate_mm is None:
            return False
        minimum = getattr(surface.bounds_mm.minimum, f"{request.constant_axis}_mm")
        maximum = getattr(surface.bounds_mm.maximum, f"{request.constant_axis}_mm")
        target = request.axis_coordinate_mm
        tolerance = request.axis_tolerance_mm
        if abs(minimum - target) > tolerance or abs(maximum - target) > tolerance:
            return False
    return True


def _sort_surface_candidates(
    surfaces: list[SurfaceGeometryInfo],
    sort_by: str,
) -> None:
    if sort_by == "item_id":
        surfaces.sort(key=lambda surface: surface.item_id)
    elif sort_by == "area_asc":
        surfaces.sort(
            key=lambda surface: (
                surface.area_mm2 is None,
                surface.area_mm2 if surface.area_mm2 is not None else 0.0,
                surface.item_id,
            )
        )
    else:
        surfaces.sort(
            key=lambda surface: (
                surface.area_mm2 is None,
                -(surface.area_mm2 if surface.area_mm2 is not None else 0.0),
                surface.item_id,
            )
        )


def find_surfaces(request: FindSurfacesRequest) -> FindSurfacesResult:
    """Search all model surfaces and return a bounded, geometry-ranked result."""
    with _connected_session() as (session, _):
        model = _get_loaded_model(session, request.model_name, request.model_kind)
        model_info = _model_info(model)
        if model_info is None:
            raise CreoMcpError("Creo returned no metadata for the requested model.")
        length_unit = _length_unit_name(model)
        factor = _length_factor_to_millimetres(length_unit)
        sequence = model.ListItems(CREO_SURFACE_ITEM_TYPE)
        count = 0 if sequence is None else int(sequence.Count)
        candidates = []
        for index in range(count):
            surface = _surface_geometry_info(_sequence_item(sequence, index), factor)
            if _surface_matches(request, surface):
                candidates.append(surface)
        _sort_surface_candidates(candidates, request.sort_by)
        return FindSurfacesResult(
            model=model_info,
            model_kind=_model_kind(_optional_int(model, "Type")),
            length_unit=length_unit,
            scanned_surface_count=count,
            matched_surface_count=len(candidates),
            truncated=len(candidates) > request.max_results,
            surfaces=candidates[: request.max_results],
        )


def _normalized_creo_version_string(raw_version: str) -> str:
    version = raw_version.strip()
    if version.casefold().startswith("creo "):
        version = version[5:].strip()
    return version.casefold()


def _normalized_creo_version(loadpoint: Path) -> str:
    return _normalized_creo_version_string(loadpoint.name)


def _validate_udf_contract(
    request: PlaceUdfRequest | RunUdfQualificationRequest,
    qualified: Any,
    model_kind: str,
) -> None:
    if request.validation_id != qualified.validation_id:
        raise CreoMcpError(
            "validation_id is stale or does not match the current UDF manifest and file."
        )
    if model_kind not in qualified.manifest.allowed_model_kinds:
        raise CreoMcpError(
            f"UDF {qualified.manifest.udf_name!r} is not qualified for {model_kind} models."
        )

    expected_references = {
        reference.prompt.casefold(): reference
        for reference in qualified.manifest.references
    }
    actual_references = {reference.prompt.casefold(): reference for reference in request.references}
    if actual_references.keys() != expected_references.keys():
        missing = sorted(expected_references.keys() - actual_references.keys())
        extra = sorted(actual_references.keys() - expected_references.keys())
        raise CreoMcpError(
            f"UDF reference bindings must exactly match the manifest; missing={missing}, "
            f"extra={extra}."
        )
    for prompt, binding in actual_references.items():
        expected_type = expected_references[prompt].item_type
        if binding.item_type != expected_type:
            raise CreoMcpError(
                f"UDF reference {binding.prompt!r} requires item_type={expected_type!r}, "
                f"not {binding.item_type!r}."
            )

    expected_dimensions = {
        dimension.name.casefold(): dimension
        for dimension in qualified.manifest.variant_dimensions
    }
    actual_dimensions = {
        dimension.name.casefold(): dimension for dimension in request.variant_dimensions
    }
    if actual_dimensions.keys() != expected_dimensions.keys():
        missing = sorted(expected_dimensions.keys() - actual_dimensions.keys())
        extra = sorted(actual_dimensions.keys() - expected_dimensions.keys())
        raise CreoMcpError(
            f"UDF dimension bindings must exactly match the manifest; missing={missing}, "
            f"extra={extra}."
        )
    for name, binding in actual_dimensions.items():
        specification = expected_dimensions[name]
        if not specification.minimum <= binding.value <= specification.maximum:
            raise CreoMcpError(
                f"UDF dimension {binding.name!r}={binding.value} {specification.unit} is "
                f"outside [{specification.minimum}, {specification.maximum}]."
            )


def _udf_dimension_value(value: float, unit: str, model_length_unit: str) -> float:
    if unit == "mm":
        return value / _length_factor_to_millimetres(model_length_unit)
    return value


def _place_validated_udf(
    request: PlaceUdfRequest | RunUdfQualificationRequest,
    qualified: Any,
    *,
    qualification_run: bool,
) -> PlaceUdfResult | UdfQualificationRunResult:
    """Place one hash-verified UDF contract without saving the target model."""
    with _connected_session() as (session, loadpoint):
        model = _get_loaded_model(session, request.model_name, request.model_kind)
        model_info = _model_info(model)
        if model_info is None:
            raise CreoMcpError("Creo returned no metadata for the UDF target model.")
        model_kind = _model_kind(_optional_int(model, "Type"))
        _validate_udf_contract(request, qualified, model_kind)

        actual_version = _normalized_creo_version(loadpoint)
        qualified_versions = {
            _normalized_creo_version_string(version)
            for version in qualified.manifest.qualified_creo_versions
        }
        if actual_version not in qualified_versions:
            raise CreoMcpError(
                f"UDF {qualified.manifest.udf_name!r} is not qualified for Creo "
                f"{loadpoint.name!r}; qualified versions are "
                f"{qualified.manifest.qualified_creo_versions}."
            )

        select_factory = _dispatch_factory(SELECT_FACTORY_PROG_ID)
        reference_factory = _dispatch_factory(UDF_REFERENCE_PROG_ID)
        references = _dispatch_factory(UDF_REFERENCES_PROG_ID)
        for index, binding in enumerate(request.references):
            try:
                item = model.GetItemById(
                    UDF_REFERENCE_ITEM_TYPES[binding.item_type], binding.item_id
                )
            except Exception as exc:
                raise CreoMcpError(
                    f"Could not resolve UDF reference {binding.prompt!r} as "
                    f"{binding.item_type} ID {binding.item_id}."
                ) from exc
            if item is None:
                raise CreoMcpError(
                    f"UDF reference {binding.prompt!r} does not exist: "
                    f"{binding.item_type} ID {binding.item_id}."
                )
            selection = select_factory.CreateModelItemSelection(item, None)
            reference = reference_factory.Create(binding.prompt, selection)
            reference.IsExternal = False
            references.Set(index, reference)

        length_unit = _length_unit_name(model)
        dimension_specs = {
            dimension.name.casefold(): dimension
            for dimension in qualified.manifest.variant_dimensions
        }
        dimension_factory = _dispatch_factory(UDF_VARIANT_DIMENSION_PROG_ID)
        variant_values = _dispatch_factory(UDF_VARIANT_VALUES_PROG_ID)
        for index, binding in enumerate(request.variant_dimensions):
            specification = dimension_specs[binding.name.casefold()]
            native_value = _udf_dimension_value(
                binding.value, specification.unit, length_unit
            )
            variant = dimension_factory.Create(specification.name, native_value)
            variant_values.Set(index, variant)

        instructions_factory = _dispatch_factory(UDF_CUSTOM_CREATE_INSTRUCTIONS_PROG_ID)
        instructions = instructions_factory.Create(qualified.manifest.udf_name)
        instructions.DependencyType = 0
        instructions.DimDisplayType = 2
        instructions.ScaleType = 0
        instructions.TurnOffFixModelUI = True
        instructions.TurnOffUDFRedefineMenu = True
        instructions.ShowUDFEditMenu = False
        instructions.References = references
        instructions.VariantValues = variant_values

        original_directory = str(session.GetCurrentDirectory())
        try:
            session.ChangeDirectory(str(qualified.udf_path.parent))
            group = model.CreateUDFGroup(instructions)
            if group is None:
                raise CreoMcpError("Creo returned no feature group after UDF placement.")
            model.Regenerate(None)
            _repaint_model_if_current(session, model)
        except CreoMcpError:
            raise
        except Exception as exc:
            raise CreoMcpError(
                f"Creo could not place qualified UDF {qualified.manifest.udf_name!r}: {exc}"
            ) from exc
        finally:
            try:
                session.ChangeDirectory(original_directory)
            except Exception:
                pass

        members, _ = _bounded_sequence(group.ListMembers(), 5000)
        extent = _extent_to_millimetres(_measure_extent(model), length_unit)
        result = {
            "target_model": model_info,
            "udf_name": qualified.manifest.udf_name,
            "group_name": _optional_string(group, "GroupName"),
            "group_members": [_feature_info(member) for member in members],
            "validation_id": qualified.validation_id,
            "length_unit": length_unit,
            "measured_extent_mm": extent,
            "volume_mm3": _volume_in_millimetres(model, length_unit),
            "saved": False,
        }
        if qualification_run:
            return UdfQualificationRunResult(
                **result,
                qualification_state="candidate_run_completed",
                manifest_verified_non_interactive=False,
            )
        return PlaceUdfResult(**result)


def place_udf(request: PlaceUdfRequest) -> PlaceUdfResult:
    """Place one fully bound, qualified UDF in a loaded model without saving it."""
    qualified = load_qualified_udf(request.manifest_path)
    result = _place_validated_udf(request, qualified, qualification_run=False)
    return PlaceUdfResult.model_validate(result)


def run_udf_qualification(
    request: RunUdfQualificationRequest,
) -> UdfQualificationRunResult:
    """Run one supervised candidate UDF placement without granting qualification."""
    candidate = load_udf_manifest(request.manifest_path)
    if candidate.manifest.verified_non_interactive:
        raise CreoMcpError(
            "UDF manifest is already marked verified_non_interactive=true; "
            "use the qualified placement operation instead."
        )
    result = _place_validated_udf(request, candidate, qualification_run=True)
    return UdfQualificationRunResult.model_validate(result)


def _parameter_value(factory: Any, update: Any) -> Any:
    if update.value_type == "string":
        return factory.CreateStringParamValue(update.value)
    if update.value_type == "integer":
        return factory.CreateIntParamValue(update.value)
    if update.value_type == "double":
        return factory.CreateDoubleParamValue(float(update.value))
    return factory.CreateBoolParamValue(update.value)


def update_model_metadata(request: UpdateModelMetadataRequest) -> UpdateModelMetadataResult:
    """Apply semantic names and controlled parameter updates without saving."""
    with _connected_session() as (session, _):
        model = _get_loaded_model(session, request.model_name, request.model_kind)
        model_info = _model_info(model)
        if model_info is None:
            raise CreoMcpError("Creo returned no metadata for the model update target.")

        resolved_renames: list[tuple[Any, Any, str | None]] = []
        for update in request.renames:
            item_type = UDF_REFERENCE_ITEM_TYPES[update.item_type]
            try:
                item = model.GetItemById(item_type, update.item_id)
            except Exception as exc:
                raise CreoMcpError(
                    f"Could not resolve rename target {update.item_type} ID {update.item_id}."
                ) from exc
            if item is None:
                raise CreoMcpError(
                    f"Rename target does not exist: {update.item_type} ID {update.item_id}."
                )
            raw_items, _ = _bounded_sequence(model.ListItems(item_type), 100000)
            for candidate in raw_items:
                if int(candidate.Id) == update.item_id:
                    continue
                candidate_name = _optional_name(candidate)
                if candidate_name and candidate_name.casefold() == update.new_name.casefold():
                    raise CreoMcpError(
                        f"Cannot rename {update.item_type} ID {update.item_id} to "
                        f"{update.new_name!r}; that name already exists for this item type."
                    )
            resolved_renames.append((update, item, _optional_name(item)))

        dimensions = _dimension_map(model)
        resolved_dimensions: list[tuple[Any, Any, float, float]] = []
        length_unit = _length_unit_name(model)
        for update in request.dimensions:
            dimension = dimensions.get(update.symbol.casefold())
            if dimension is None:
                available = sorted(str(item.Symbol) for item in dimensions.values())
                raise CreoMcpError(
                    f"Driving dimension {update.symbol!r} does not exist. "
                    f"Available dimensions: {available}"
                )
            native_value = _udf_dimension_value(update.value, update.unit, length_unit)
            resolved_dimensions.append(
                (update, dimension, float(dimension.DimValue), native_value)
            )

        parameter_factory = (
            _dispatch_factory(MODEL_ITEM_FACTORY_PROG_ID)
            if request.parameters
            else None
        )
        resolved_parameters: list[tuple[Any, Any | None, Any]] = []
        for update in request.parameters:
            try:
                parameter = model.GetParam(update.name)
            except Exception as exc:
                if "XToolkitNotFound" in str(exc):
                    parameter = None
                else:
                    raise CreoMcpError(
                        f"Could not inspect model parameter {update.name!r}."
                    ) from exc
            if parameter is not None:
                try:
                    existing_type = int(parameter.Value.discr)
                except Exception as exc:
                    raise CreoMcpError(
                        f"Could not determine the value type of parameter {update.name!r}."
                    ) from exc
                expected_type = PARAMETER_VALUE_TYPE_CODES[update.value_type]
                if existing_type != expected_type:
                    raise CreoMcpError(
                        f"Parameter {update.name!r} already exists with type code "
                        f"{existing_type}, not requested type {update.value_type!r}."
                    )
            resolved_parameters.append(
                (update, parameter, _parameter_value(parameter_factory, update))
            )

        applied_renames: list[AppliedModelItemRename] = []
        applied_dimensions: list[AppliedDimensionUpdate] = []
        applied_parameters: list[AppliedParameterUpdate] = []
        try:
            for update, item, previous_name in resolved_renames:
                item.SetName(update.new_name)
                applied_renames.append(
                    AppliedModelItemRename(
                        **update.model_dump(), previous_name=previous_name
                    )
                )
            for update, dimension, previous_value, native_value in resolved_dimensions:
                dimension.DimValue = native_value
                applied_dimensions.append(
                    AppliedDimensionUpdate(
                        **update.model_dump(),
                        previous_value_native=previous_value,
                        applied_value_native=native_value,
                    )
                )
            for update, parameter, value in resolved_parameters:
                created = parameter is None
                if created:
                    model.CreateParam(update.name, value)
                else:
                    parameter.Value = value
                applied_parameters.append(
                    AppliedParameterUpdate(**update.model_dump(), created=created)
                )
            model.Regenerate(None)
            _repaint_model_if_current(session, model)
        except Exception as exc:
            raise CreoMcpError(
                "Creo could not complete the model metadata update. Some in-memory "
                f"changes may already be present; inspect the model before continuing. {exc}"
            ) from exc

        return UpdateModelMetadataResult(
            model=model_info,
            renames=applied_renames,
            dimensions=applied_dimensions,
            parameters=applied_parameters,
            regenerated=True,
            saved=False,
        )


def get_handoff_report(request: HandoffReportRequest) -> HandoffReportResult:
    """Assess model health and maintainability without modifying Creo."""
    with _connected_session() as (session, _):
        model = _get_loaded_model(session, request.model_name, request.model_kind)
        model_info = _model_info(model)
        if model_info is None:
            raise CreoMcpError("Creo returned no metadata for the handoff report target.")

        features, features_truncated = _list_features(model, 100000)
        non_active = [feature for feature in features if feature.status != 0]
        unnamed = [feature for feature in features if not feature.name]
        imported = [feature for feature in features if feature.feature_type == 22]
        dimensions = _list_dimensions(model)
        parameters, parameters_truncated, skipped_parameters = _list_parameters(
            model, 100000
        )
        automation_parameters = [
            parameter
            for parameter in parameters
            if parameter.name.casefold().startswith("mcp_")
        ]

        warnings: list[str] = []
        if non_active:
            warnings.append(f"Model contains {len(non_active)} non-active feature(s).")
        if unnamed:
            warnings.append(f"Model contains {len(unnamed)} unnamed feature(s).")
        if imported:
            warnings.append(
                f"Model contains {len(imported)} imported feature(s); treat imported geometry "
                "as reference-only unless an editable rebuild strategy is documented."
            )
        if imported and not dimensions:
            warnings.append("Imported model exposes no named driving dimensions.")
        if not automation_parameters:
            warnings.append("No MCP_ automation ownership parameters are present.")
        if skipped_parameters:
            warnings.append(
                f"Creo returned {skipped_parameters} unnamed or unreadable parameter(s)."
            )
        if features_truncated or parameters_truncated:
            warnings.append("The report reached an internal traversal limit.")

        limit = request.max_reported_items
        length_unit = _length_unit_name(model)
        return HandoffReportResult(
            model=model_info,
            model_kind=_model_kind(_optional_int(model, "Type")),
            length_unit=length_unit,
            measured_extent_mm=_extent_to_millimetres(
                _measure_extent(model), length_unit
            ),
            feature_count=len(features),
            active_feature_count=sum(feature.status == 0 for feature in features),
            non_active_features=non_active[:limit],
            unnamed_features=unnamed[:limit],
            imported_features=imported[:limit],
            named_dimension_count=sum(bool(item.symbol.strip()) for item in dimensions),
            parameter_count=len(parameters),
            automation_parameters=automation_parameters[:limit],
            warnings=warnings,
            handoff_state="ready" if not warnings else "review_required",
            saved=False,
        )


def _native_model_extension(model_kind: str) -> str:
    if model_kind == "part":
        return "prt"
    if model_kind == "assembly":
        return "asm"
    raise CreoMcpError("Only Creo parts and assemblies can be copied as native models.")


def _ensure_new_native_model_name(directory: Path, name: str, extension: str) -> None:
    prefix = f"{name}.{extension}".casefold()
    conflicts = [
        child.name
        for child in directory.iterdir()
        if child.is_file()
        and (
            child.name.casefold() == prefix
            or re.fullmatch(re.escape(prefix) + r"\.\d+", child.name.casefold())
        )
    ]
    if conflicts:
        raise CreoMcpError(
            f"Refusing to overwrite existing native model versions: {sorted(conflicts)}"
        )


def save_model_as(request: SaveModelAsRequest) -> SaveModelAsResult:
    """Copy one loaded native model to a new protected name and directory."""
    unresolved_directory = Path(request.output_directory).expanduser()
    if not unresolved_directory.is_absolute():
        raise CreoMcpError(
            "Save-as output directory must be absolute: "
            f"{request.output_directory}"
        )
    output_directory = unresolved_directory.resolve(strict=False)
    if not output_directory.is_dir():
        raise CreoMcpError(
            f"Save-as output directory must be an existing absolute directory: "
            f"{output_directory}"
        )

    with _connected_session() as (session, _):
        source = _get_loaded_model(session, request.model_name, request.model_kind)
        source_info = _model_info(source)
        if source_info is None:
            raise CreoMcpError("Creo returned no source model metadata for save-as.")
        model_kind = _model_kind(_optional_int(source, "Type"))
        extension = _native_model_extension(model_kind)
        _ensure_new_native_model_name(output_directory, request.target_name, extension)
        _ensure_model_not_loaded(session, request.target_name)

        original_directory = str(session.GetCurrentDirectory())
        try:
            session.ChangeDirectory(str(output_directory))
            copied = source.CopyAndRetrieve(request.target_name, None)
        except Exception as exc:
            raise CreoMcpError(
                f"Creo could not copy {source_info.file_name!r} as "
                f"{request.target_name!r}: {exc}"
            ) from exc
        finally:
            try:
                session.ChangeDirectory(original_directory)
            except Exception:
                pass
        if copied is None:
            raise CreoMcpError("Creo returned no copied model after save-as.")

        copied_info = _model_info(copied)
        if copied_info is None:
            raise CreoMcpError("Creo returned no metadata for the copied model.")
        candidates = [
            child
            for child in output_directory.iterdir()
            if child.is_file()
            and re.fullmatch(
                re.escape(f"{request.target_name}.{extension}") + r"(?:\.\d+)?",
                child.name,
                re.IGNORECASE,
            )
        ]
        if not candidates:
            raise CreoMcpError("Creo reported a successful copy but no native file was found.")
        saved_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        if request.activate_copy:
            _activate_and_repaint_model(session, copied)

        return SaveModelAsResult(
            source_model=source_info,
            copied_model=copied_info,
            saved_path=str(saved_path),
            activated=request.activate_copy,
            source_modified=False,
            saved=True,
        )


def export_model(request: ExportModelRequest) -> ExportModelResult:
    """Export one loaded model to a new neutral CAD file without changing the source."""
    unresolved_output_path = Path(request.output_path).expanduser()
    if not unresolved_output_path.is_absolute():
        raise CreoMcpError(f"Export output path must be absolute: {request.output_path}")
    output_path = unresolved_output_path.resolve(strict=False)
    if not output_path.parent.is_dir():
        raise CreoMcpError(f"Export output directory does not exist: {output_path.parent}")
    if output_path.exists():
        raise CreoMcpError(f"Refusing to overwrite existing export file: {output_path}")
    if output_path.suffix.casefold() not in EXPORT_EXTENSIONS[request.export_format]:
        raise CreoMcpError(
            f"Output extension {output_path.suffix!r} does not match "
            f"export_format={request.export_format!r}."
        )
    output_base = output_path.with_suffix("")
    possible_outputs = [
        Path(f"{output_base}{extension}")
        for extension in EXPORT_EXTENSIONS[request.export_format]
    ]
    conflicts = [path for path in possible_outputs if path.exists()]
    if conflicts:
        raise CreoMcpError(
            "Refusing to let Creo overwrite an equivalent export file: "
            f"{[str(path) for path in conflicts]}"
        )
    profile_path = None
    if request.profile_path is not None:
        unresolved_profile = Path(request.profile_path).expanduser()
        if not unresolved_profile.is_absolute():
            raise CreoMcpError(
                f"Creo export profile path must be absolute: {request.profile_path}"
            )
        profile = unresolved_profile.resolve(strict=False)
        if not profile.is_file():
            raise CreoMcpError(f"Creo export profile does not exist: {profile}")
        profile_path = str(profile)

    with _connected_session() as (session, _):
        model = _get_loaded_model(session, request.model_name, request.model_kind)
        model_info = _model_info(model)
        if model_info is None:
            raise CreoMcpError("Creo returned no source model metadata for export.")
        try:
            model.ExportIntf3D(
                str(output_base), EXPORT_INTF_3D_TYPES[request.export_format], profile_path
            )
        except Exception as exc:
            raise CreoMcpError(
                f"Creo could not export {model_info.file_name!r} as "
                f"{request.export_format}: {exc}"
            ) from exc
        generated = [path for path in possible_outputs if path.is_file()]
        if len(generated) != 1 or generated[0].stat().st_size <= 0:
            raise CreoMcpError(
                "Creo returned from export but did not create exactly one non-empty file "
                f"for: {output_base}"
            )
        generated_path = generated[0]
        if generated_path != output_path:
            try:
                generated_path.rename(output_path)
            except OSError as exc:
                raise CreoMcpError(
                    f"Creo created {generated_path}, but it could not be renamed to "
                    f"the requested path {output_path}: {exc}"
                ) from exc
        return ExportModelResult(
            source_model=model_info,
            export_format=request.export_format,
            output_path=str(output_path),
            file_size_bytes=output_path.stat().st_size,
            source_modified=False,
            saved=False,
        )


def inspect_model_geometry(request: InspectModelGeometryRequest) -> ModelGeometryResult:
    """Measure one active or explicitly named Creo solid without changing it."""
    with _connected_session() as (session, _):
        model = _get_loaded_model(session, request.model_name, request.model_kind)

        model_info = _model_info(model)
        if model_info is None:
            raise CreoMcpError("Creo returned no metadata for the requested model.")
        raw_model_type = _optional_int(model, "Type")
        model_kind = _model_kind(raw_model_type)
        length_unit = _length_unit_name(model)
        extent = _extent_to_millimetres(_measure_extent(model), length_unit)
        volume_mm3 = _volume_in_millimetres(model, length_unit)
        return ModelGeometryResult(
            model=model_info,
            model_kind=model_kind,
            length_unit=length_unit,
            measured_extent_mm=extent,
            volume_mm3=volume_mm3,
        )
