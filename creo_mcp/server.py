"""MCP transport, schemas, and process isolation for Creo tools."""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .bridge import (
    create_empty_part,
    create_packaging_box,
    export_model,
    find_surfaces,
    get_handoff_report,
    get_status,
    import_cad,
    import_step,
    inspect_geometry_details,
    inspect_model_geometry,
    inspect_model_structure,
    inspect_part,
    open_native_model,
    place_udf,
    run_udf_qualification,
    save_model_as,
    update_model_metadata,
)
from .errors import CreoMcpError
from .models import (
    CreoModelInfo,
    CreoStatus,
    CreatePackagingBoxRequest,
    CreatePackagingBoxResult,
    CreatePartRequest,
    CreatePartResult,
    FindSurfacesRequest,
    FindSurfacesResult,
    ExportModelRequest,
    ExportModelResult,
    ImportCadRequest,
    ImportCadResult,
    ImportStepRequest,
    ImportStepResult,
    GeometryDetailsResult,
    HandoffReportRequest,
    HandoffReportResult,
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
    OperationLogRequest,
    OperationLogResult,
    PackagingBoxPlan,
    PackagingBoxPlanRequest,
    PlaceUdfRequest,
    PlaceUdfResult,
    RunUdfQualificationRequest,
    SaveModelAsRequest,
    SaveModelAsResult,
    StepFileInfo,
    UdfManifestValidationResult,
    UdfQualificationRunResult,
    UpdateModelMetadataRequest,
    UpdateModelMetadataResult,
    ValidateUdfManifestRequest,
    create_packaging_box_plan,
)
from .observability import (
    append_operation_record,
    append_operation_result,
    operation_log_path,
    read_recent_operations,
)
from .step import inspect_step_file
from .udf import validate_udf_manifest
from .version import SERVER_NAME, SERVER_VERSION


PROBE_TIMEOUT_SECONDS = 20
OPERATION_TIMEOUT_SECONDS = 120
READ_ONLY_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
MODEL_EDIT_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)


def _probe_script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "creo_mcp_server.py"


def _run_probe() -> int:
    """Run one isolated COM probe and emit a single machine-readable result line."""
    try:
        result = get_status()
        print(json.dumps({"ok": True, "result": result.model_dump()}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


def _run_operation(operation: str, payload: dict[str, Any]) -> int:
    """Run one isolated operation and emit one JSON result line for the parent process."""
    operation_id = uuid.uuid4().hex
    started = time.perf_counter()
    try:
        if operation == "create_empty_part":
            result = create_empty_part(CreatePartRequest.model_validate(payload))
        elif operation == "inspect_part":
            result = inspect_part(InspectPartRequest.model_validate(payload))
        elif operation == "create_packaging_box":
            result = create_packaging_box(CreatePackagingBoxRequest.model_validate(payload))
        elif operation == "inspect_step":
            result = inspect_step_file(InspectStepRequest.model_validate(payload))
        elif operation == "import_step":
            result = import_step(ImportStepRequest.model_validate(payload))
        elif operation == "import_cad":
            result = import_cad(ImportCadRequest.model_validate(payload))
        elif operation == "inspect_model_geometry":
            result = inspect_model_geometry(
                InspectModelGeometryRequest.model_validate(payload)
            )
        elif operation == "inspect_geometry_details":
            result = inspect_geometry_details(
                InspectGeometryDetailsRequest.model_validate(payload)
            )
        elif operation == "find_surfaces":
            result = find_surfaces(FindSurfacesRequest.model_validate(payload))
        elif operation == "inspect_model_structure":
            result = inspect_model_structure(
                InspectModelStructureRequest.model_validate(payload)
            )
        elif operation == "validate_udf_manifest":
            result = validate_udf_manifest(
                ValidateUdfManifestRequest.model_validate(payload)
            )
        elif operation == "open_native_model":
            result = open_native_model(OpenNativeModelRequest.model_validate(payload))
        elif operation == "place_udf":
            result = place_udf(PlaceUdfRequest.model_validate(payload))
        elif operation == "qualify_udf":
            result = run_udf_qualification(
                RunUdfQualificationRequest.model_validate(payload)
            )
        elif operation == "update_model_metadata":
            result = update_model_metadata(
                UpdateModelMetadataRequest.model_validate(payload)
            )
        elif operation == "handoff_report":
            result = get_handoff_report(HandoffReportRequest.model_validate(payload))
        elif operation == "save_model_as":
            result = save_model_as(SaveModelAsRequest.model_validate(payload))
        elif operation == "export_model":
            result = export_model(ExportModelRequest.model_validate(payload))
        else:
            raise CreoMcpError(f"Unsupported internal Creo operation: {operation}")
        duration_ms = round((time.perf_counter() - started) * 1000)
        logged = append_operation_result(operation_id, operation, duration_ms, result)
        print(
            json.dumps(
                {
                    "ok": True,
                    "operation_id": operation_id,
                    "log": {
                        "written": logged,
                        "path": str(operation_log_path()),
                    },
                    "result": result.model_dump(),
                }
            )
        )
        return 0
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started) * 1000)
        logged = append_operation_record(
            operation_id=operation_id,
            operation=operation,
            duration_ms=duration_ms,
            success=False,
            error_type=type(exc).__name__,
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "operation_id": operation_id,
                    "log": {
                        "written": logged,
                        "path": str(operation_log_path()),
                    },
                    "error": str(exc),
                }
            )
        )
        return 1


def _status_from_isolated_process() -> CreoStatus:
    """Prevent an unavailable COM connection from blocking the MCP stdio server."""
    try:
        completed = subprocess.run(
            [sys.executable, str(_probe_script_path()), "--probe-status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CreoMcpError(
            "Timed out while connecting to Creo. Confirm that exactly one Creo session is "
            "running under this Windows account."
        ) from exc

    try:
        payload: dict[str, Any] = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or completed.stdout.strip() or (
            f"exit code {completed.returncode}"
        )
        raise CreoMcpError(f"Creo status probe returned an invalid response: {detail}") from exc

    if not payload.get("ok"):
        raise CreoMcpError(str(payload.get("error", "Unknown Creo VB API error.")))
    return CreoStatus.model_validate(payload["result"])


def _run_isolated_operation(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one explicit Creo operation without blocking the MCP transport process."""
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(_probe_script_path()),
                "--operation",
                operation,
                "--payload",
                json.dumps(payload),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=OPERATION_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CreoMcpError(f"Timed out while running Creo operation: {operation}") from exc

    try:
        result: dict[str, Any] = json.loads(completed.stdout.strip())
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or completed.stdout.strip() or (
            f"exit code {completed.returncode}"
        )
        raise CreoMcpError(f"Creo operation returned an invalid response: {detail}") from exc

    if not result.get("ok"):
        raise CreoMcpError(str(result.get("error", "Unknown Creo operation error.")))
    return result["result"]


def create_server() -> FastMCP:
    """Create an MCP server with stable, portable tool names and JSON schemas."""
    server = FastMCP(
        SERVER_NAME,
        instructions=(
            "Use Creo tools only for explicit CAD requests. Inspect and plan before writing. "
            "Model edits, native copies, and exports require exact confirmation values. "
            "Edits remain unsaved by default, and file operations never permit overwrite."
        ),
    )
    # FastMCP does not currently expose its low-level server version argument.
    server._mcp_server.version = SERVER_VERSION

    documentation_root = Path(__file__).resolve().parents[1]

    @server.resource(
        "creo://docs/architecture",
        name="creo_architecture_zh_cn",
        title="Creo MCP 架构边界",
        description="通用 MCP、工程数据与产品 Skill 的职责边界。",
        mime_type="text/markdown",
    )
    def creo_architecture_resource() -> str:
        return (documentation_root / "ARCHITECTURE.zh-CN.md").read_text(encoding="utf-8")

    @server.resource(
        "creo://docs/engineering-standard",
        name="creo_engineering_standard_zh_cn",
        title="Creo MCP 工程交付与可维护性规范",
        description="人工接手、模型命名、可编辑性、日志和验收要求。",
        mime_type="text/markdown",
    )
    def creo_engineering_standard_resource() -> str:
        return (documentation_root / "ENGINEERING_STANDARD.zh-CN.md").read_text(
            encoding="utf-8"
        )

    @server.resource(
        "creo://docs/udf-manifest",
        name="creo_udf_manifest_zh_cn",
        title="Creo UDF 清单规范",
        description="UDF 候选资格、完整性校验和正式放置流程。",
        mime_type="text/markdown",
    )
    def creo_udf_manifest_resource() -> str:
        return (documentation_root / "UDF_MANIFEST.zh-CN.md").read_text(encoding="utf-8")

    @server.resource(
        "creo://docs/live-validation",
        name="creo_live_validation_zh_cn",
        title="Creo MCP 真实会话验证记录",
        description="在真实 Creo 13.4.1.0 会话中已验证和待验证的能力。",
        mime_type="text/markdown",
    )
    def creo_live_validation_resource() -> str:
        return (documentation_root / "LIVE_VALIDATION.zh-CN.md").read_text(
            encoding="utf-8"
        )

    @server.resource(
        "creo://docs/client-setup",
        name="creo_client_setup_zh_cn",
        title="Creo MCP 客户端接入与兼容性",
        description=(
            "nanobot、Codex、Claude、Cursor 和 VS Code 的本地接入与后续优化说明。"
        ),
        mime_type="text/markdown",
    )
    def creo_client_setup_resource() -> str:
        return (documentation_root / "CLIENT_SETUP.zh-CN.md").read_text(encoding="utf-8")

    @server.tool(
        name="creo_get_status",
        title="Get Creo Status",
        description=(
            "Verify the current user's official VB API connection to Creo and return the "
            "active model plus working directory. This tool never changes Creo."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_get_status() -> CreoStatus:
        """Verify the Creo connection and return read-only session metadata."""
        return _status_from_isolated_process()

    @server.tool(
        name="creo_get_active_model",
        title="Get Active Creo Model",
        description=(
            "Return metadata for the current active Creo model. This tool never changes Creo."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_get_active_model() -> CreoModelInfo | None:
        """Return the active model or null when Creo has no active model."""
        return _status_from_isolated_process().active_model

    @server.tool(
        name="creo_get_model_geometry",
        title="Get Creo Model Geometry",
        description=(
            "Measure the axis-aligned envelope and solid volume of the active model or a "
            "named part or assembly already loaded in Creo. Results are normalized to "
            "millimetres. This tool never modifies or saves the model."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_get_model_geometry(
        request: InspectModelGeometryRequest,
    ) -> ModelGeometryResult:
        """Return normalized geometry for one loaded Creo model."""
        return ModelGeometryResult.model_validate(
            _run_isolated_operation("inspect_model_geometry", request.model_dump())
        )

    @server.tool(
        name="creo_get_geometry_details",
        title="Get Creo Geometry Details",
        description=(
            "Inspect explicit surface and edge IDs in the active model or a named loaded "
            "model. Return normalized surface types, areas, bounds, edge types, lengths, "
            "sample points, adjacent surfaces, and owning features. All IDs are validated "
            "before evaluation. This tool never changes or saves the model."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_get_geometry_details(
        request: InspectGeometryDetailsRequest,
    ) -> GeometryDetailsResult:
        """Return useful geometric meaning for stable surface and edge IDs."""
        return GeometryDetailsResult.model_validate(
            _run_isolated_operation("inspect_geometry_details", request.model_dump())
        )

    @server.tool(
        name="creo_find_surfaces",
        title="Find Creo Surfaces",
        description=(
            "Scan all surfaces in the active model or a named loaded model, filter by "
            "surface type, normalized area, or a constant X/Y/Z coordinate, then return "
            "a bounded candidate list sorted by area or stable ID. This tool never changes "
            "or saves the model."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_find_surfaces(request: FindSurfacesRequest) -> FindSurfacesResult:
        """Find useful surface references without dumping the complete model topology."""
        return FindSurfacesResult.model_validate(
            _run_isolated_operation("find_surfaces", request.model_dump())
        )

    @server.tool(
        name="creo_get_model_structure",
        title="Get Creo Model Structure",
        description=(
            "Return a bounded structural inventory for the active model or a named loaded "
            "model: model-tree features, axes, points, coordinate systems, driving "
            "dimensions, and model parameters. Each item includes stable IDs and names "
            "when Creo provides them. This tool never changes or saves the model."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_get_model_structure(
        request: InspectModelStructureRequest,
    ) -> ModelStructureResult:
        """Return bounded references for later generic modelling operations."""
        return ModelStructureResult.model_validate(
            _run_isolated_operation("inspect_model_structure", request.model_dump())
        )

    @server.tool(
        name="creo_get_handoff_report",
        title="Get Creo Engineering Handoff Report",
        description=(
            "Assess the active or named loaded model for engineering handoff. Return units, "
            "envelope, feature status, unnamed and imported features, named-dimension count, model "
            "parameters, MCP automation ownership parameters, and actionable warnings. "
            "This tool never changes or saves the model."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_get_handoff_report(request: HandoffReportRequest) -> HandoffReportResult:
        """Return a bounded, read-only engineering handoff assessment."""
        return HandoffReportResult.model_validate(
            _run_isolated_operation("handoff_report", request.model_dump())
        )

    @server.tool(
        name="creo_get_operation_log",
        title="Get Recent Creo MCP Operations",
        description=(
            "Read recent privacy-minimized local Creo MCP operation records, including "
            "operation IDs, durations, success state, target model, and whether a save "
            "occurred. Request payloads and model content are never logged."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_get_operation_log(request: OperationLogRequest) -> OperationLogResult:
        """Return recent local JSONL audit records without connecting to Creo."""
        return read_recent_operations(request.max_records)

    @server.tool(
        name="creo_validate_udf_manifest",
        title="Validate Creo UDF Manifest",
        description=(
            "Offline-validate a strict candidate or qualified JSON manifest and its Creo "
            ".gph UDF asset. Resolves the file, verifies its SHA-256 hash, and returns a "
            "validation_id for a supervised qualification run or production placement. "
            "This tool never connects to or changes Creo."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_validate_udf_manifest(
        request: ValidateUdfManifestRequest,
    ) -> UdfManifestValidationResult:
        """Validate a candidate or qualified UDF contract without starting Creo."""
        return validate_udf_manifest(request)

    @server.tool(
        name="creo_plan_packaging_box",
        title="Plan a Packaging Box",
        description=(
            "Validate dimensions and produce a deterministic dry-run plan for a coarse "
            "packaging-box part. This tool never connects to or changes Creo."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_plan_packaging_box(request: PackagingBoxPlanRequest) -> PackagingBoxPlan:
        """Return a validated plan before any model-creation operation is enabled."""
        return create_packaging_box_plan(request)

    @server.tool(
        name="creo_inspect_part_dimensions",
        title="Inspect Creo Part Dimensions",
        description=(
            "Retrieve an existing native Creo part and list its principal length unit plus "
            "driving dimension symbols. It does not change or save the part. Use this to "
            "qualify a part as a parameterized modelling template."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_inspect_part_dimensions(request: InspectPartRequest) -> InspectPartResult:
        """Inspect a native part's unit and driving dimensions without saving it."""
        return InspectPartResult.model_validate(
            _run_isolated_operation("inspect_part", request.model_dump())
        )

    @server.tool(
        name="creo_inspect_step_file",
        title="Inspect STEP File",
        description=(
            "Validate a local ISO-10303-21 STEP file and report its schema, originating "
            "system, product names, unit, solid bodies, faces, and assembly structure. "
            "This tool does not start or change Creo."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_inspect_step_file(request: InspectStepRequest) -> StepFileInfo:
        """Read structural STEP metadata without opening Creo."""
        return inspect_step_file(request)

    @server.tool(
        name="creo_create_empty_part",
        title="Create Empty Creo Part",
        description=(
            "Create and optionally activate a new unsaved Creo part. It does not modify "
            "the current model and never saves a file. The MCP client should request user "
            "approval before invoking this write operation."
        ),
        annotations=WRITE_ANNOTATIONS,
        structured_output=True,
    )
    def creo_create_empty_part(request: CreatePartRequest) -> CreatePartResult:
        """Create an unsaved part as the first verified, non-destructive write operation."""
        return CreatePartResult.model_validate(
            _run_isolated_operation("create_empty_part", request.model_dump())
        )

    @server.tool(
        name="creo_update_model_metadata",
        title="Update Creo Model Metadata",
        description=(
            "Apply semantic names to stable model-item IDs, update named driving dimensions "
            "with explicit units, and create or update typed model parameters. All targets "
            "are validated before mutation, then the model is regenerated. Requires "
            "confirmation='update_model_metadata' and never saves the model."
        ),
        annotations=MODEL_EDIT_ANNOTATIONS,
        structured_output=True,
    )
    def creo_update_model_metadata(
        request: UpdateModelMetadataRequest,
    ) -> UpdateModelMetadataResult:
        """Apply maintainability metadata and parameters, regenerate, and leave unsaved."""
        return UpdateModelMetadataResult.model_validate(
            _run_isolated_operation("update_model_metadata", request.model_dump())
        )

    @server.tool(
        name="creo_save_model_as",
        title="Save Creo Model As New Copy",
        description=(
            "Copy an active or named loaded Creo part or assembly to a new native model "
            "name in an existing directory. Refuses every existing .prt/.asm version, "
            "restores the Creo working directory, and optionally activates the copy. "
            "Requires confirmation='save_model_as' and never overwrites."
        ),
        annotations=WRITE_ANNOTATIONS,
        structured_output=True,
    )
    def creo_save_model_as(request: SaveModelAsRequest) -> SaveModelAsResult:
        """Create one protected native copy without modifying the source model."""
        return SaveModelAsResult.model_validate(
            _run_isolated_operation("save_model_as", request.model_dump())
        )

    @server.tool(
        name="creo_export_model",
        title="Export Creo Model",
        description=(
            "Export an active or named loaded Creo model to a new STEP, IGES, or Parasolid "
            "file through the official 3D interface API. Validates the extension and optional "
            "profile, refuses overwrite, and verifies a non-empty output. Requires "
            "confirmation='export_model' and never saves or modifies the source model."
        ),
        annotations=WRITE_ANNOTATIONS,
        structured_output=True,
    )
    def creo_export_model(request: ExportModelRequest) -> ExportModelResult:
        """Create one guarded neutral-format export without changing the source model."""
        return ExportModelResult.model_validate(
            _run_isolated_operation("export_model", request.model_dump())
        )

    @server.tool(
        name="creo_place_qualified_udf",
        title="Place Qualified Creo UDF",
        description=(
            "Place a hash-verified, version-qualified Creo UDF in the active or named "
            "loaded model using complete stable-ID reference and bounded dimension "
            "bindings. Requires a fresh validation_id and "
            "confirmation='place_qualified_udf'. Interactive repair is disabled. The "
            "target is regenerated and measured but never saved."
        ),
        annotations=WRITE_ANNOTATIONS,
        structured_output=True,
    )
    def creo_place_qualified_udf(request: PlaceUdfRequest) -> PlaceUdfResult:
        """Place and verify one fully qualified UDF without saving the target model."""
        return PlaceUdfResult.model_validate(
            _run_isolated_operation("place_udf", request.model_dump())
        )

    @server.tool(
        name="creo_run_udf_qualification",
        title="Run Creo UDF Qualification",
        description=(
            "Run one supervised placement of a hash-verified UDF candidate whose manifest "
            "has verified_non_interactive=false. Requires complete stable-ID references, "
            "bounded dimensions, an exact Creo version, and "
            "confirmation='run_udf_qualification'. Interactive repair is disabled. The "
            "target is regenerated and measured but never saved, and success does not "
            "automatically mark the manifest as qualified."
        ),
        annotations=WRITE_ANNOTATIONS,
        structured_output=True,
    )
    def creo_run_udf_qualification(
        request: RunUdfQualificationRequest,
    ) -> UdfQualificationRunResult:
        """Run one supervised candidate placement without granting qualification."""
        return UdfQualificationRunResult.model_validate(
            _run_isolated_operation("qualify_udf", request.model_dump())
        )

    @server.tool(
        name="creo_open_native_model",
        title="Open Native Creo Model",
        description=(
            "Load an existing native Creo .prt or .asm model, optionally activate its "
            "window, and return its true envelope, solid volume when available, units, "
            "and model type. Requires confirmation='open_native_model'. It never saves "
            "or modifies the source file."
        ),
        annotations=WRITE_ANNOTATIONS,
        structured_output=True,
    )
    def creo_open_native_model(
        request: OpenNativeModelRequest,
    ) -> OpenNativeModelResult:
        """Load and measure one native Creo model without changing its source file."""
        return OpenNativeModelResult.model_validate(
            _run_isolated_operation("open_native_model", request.model_dump())
        )

    @server.tool(
        name="creo_create_packaging_box",
        title="Create Native Creo Packaging Box",
        description=(
            "Copy a qualified native .prt template to a new, non-existing model name, apply "
            "the confirmed length/width/height dimensions in millimetres, regenerate, verify "
            "the envelope, and save the new model. Requires the matching dry-run plan_id and "
            "confirmation='create_new_model'. Never overwrites an existing model."
        ),
        annotations=WRITE_ANNOTATIONS,
        structured_output=True,
    )
    def creo_create_packaging_box(
        request: CreatePackagingBoxRequest,
    ) -> CreatePackagingBoxResult:
        """Create and validate one new native packaging-box envelope from a template."""
        return CreatePackagingBoxResult.model_validate(
            _run_isolated_operation("create_packaging_box", request.model_dump())
        )

    @server.tool(
        name="creo_import_cad",
        title="Import CAD Into Creo",
        description=(
            "Import a supported CAD file as a new unsaved Creo part or assembly. Auto-detect "
            "STEP, IGES, Parasolid, ACIS, STL, JT, CATIA, SolidWorks, Inventor, VDA, and "
            "Creo Neutral files; NX .prt requires source_format='nx'. Return the true Creo "
            "envelope, solid volume when available, units, and model type. Requires "
            "confirmation='import_unsaved_model'. It never saves or overwrites files."
        ),
        annotations=WRITE_ANNOTATIONS,
        structured_output=True,
    )
    def creo_import_cad(request: ImportCadRequest) -> ImportCadResult:
        """Import and measure one new unsaved Creo model from a supported CAD source."""
        return ImportCadResult.model_validate(
            _run_isolated_operation("import_cad", request.model_dump())
        )

    @server.tool(
        name="creo_import_step",
        title="Import STEP Into Creo",
        description=(
            "Import a validated STEP file as a new unsaved Creo part or assembly, detect "
            "its model type, measure its true Creo envelope in millimetres, and optionally "
            "activate its window. Requires confirmation='import_unsaved_model'. It does not "
            "save or overwrite files."
        ),
        annotations=WRITE_ANNOTATIONS,
        structured_output=True,
    )
    def creo_import_step(request: ImportStepRequest) -> ImportStepResult:
        """Import and measure one new unsaved Creo model from STEP."""
        return ImportStepResult.model_validate(
            _run_isolated_operation("import_step", request.model_dump())
        )

    return server


def main() -> None:
    """Run either a one-shot probe or the standard MCP stdio transport."""
    if len(sys.argv) == 2 and sys.argv[1] == "--probe-status":
        raise SystemExit(_run_probe())
    if len(sys.argv) == 5 and sys.argv[1] == "--operation" and sys.argv[3] == "--payload":
        try:
            payload = json.loads(sys.argv[4])
        except json.JSONDecodeError as exc:
            print(json.dumps({"ok": False, "error": f"Invalid operation payload: {exc}"}))
            raise SystemExit(1)
        raise SystemExit(_run_operation(sys.argv[2], payload))
    create_server().run(transport="stdio")
