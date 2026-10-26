"""MCP transport, schemas, and process isolation for Creo tools."""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from importlib.resources import files
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP, Image
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import broker
from .bridge import (
    build_geometry,
    create_empty_part,
    create_packaging_box,
    create_product_clearance_tray,
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
    persistent_creo_connection,
    place_udf,
    run_udf_qualification,
    save_model_as,
    update_feature_suppression,
    update_model_metadata,
)
from .clearance_geometry import plan_product_clearance_tray
from .errors import CreoMcpError
from .fit_analysis import inspect_step_solids
from .geometry_dsl import BuildGeometryRequest, BuildGeometryResult
from .models import (
    CreatePackagingBoxRequest,
    CreatePackagingBoxResult,
    CreatePartRequest,
    CreatePartResult,
    CreateProductClearanceTrayRequest,
    CreateProductClearanceTrayResult,
    CreoModelInfo,
    CreoStatus,
    ExportModelRequest,
    ExportModelResult,
    FindSurfacesRequest,
    FindSurfacesResult,
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
    InspectStepSolidsResult,
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
    ProductClearanceTrayPlan,
    ProductClearanceTrayPlanRequest,
    RunUdfQualificationRequest,
    SaveModelAsRequest,
    SaveModelAsResult,
    StepFileInfo,
    UdfManifestValidationResult,
    UdfQualificationRunResult,
    UpdateFeatureSuppressionRequest,
    UpdateFeatureSuppressionResult,
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

PROBE_TIMEOUT_SECONDS = 30
OPERATION_TIMEOUT_SECONDS = 420
MCP_CALL_LOCK_TIMEOUT_SECONDS = 5
PERSISTENT_WORKER_RESPONSE_PREFIX = "__CREO_MCP_RESPONSE__:"
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

# Capability boundaries the caller must respect. Kept as data (not prose in a
# prompt) so the model can query them exactly like the ZW3D MCP capabilities
# meta tool, and so they stay testable. Every entry mirrors docs/ARCHITECTURE.md.
CAPABILITY_BOUNDARIES: dict[str, Any] = {
    "bridge": {
        "api": "official PTC Creo VB API over the pfcls COM type library",
        "session": "one interactive Creo session per Windows desktop user",
        "concurrency": "never call two tools concurrently; operations are serialized by the server",
        "default_state": "edits stay unsaved; opening, importing, editing, saving and exporting are separate permissions",
    },
    "feature_creation": {
        "supported": [
            "parameterized UDF placement via creo_place_qualified_udf after creo_validate_udf_manifest",
            "solid bodies from a constrained JSON geometry program via creo_build_geometry",
            (
                "native parametric parts by copying a validated Creo template and driving its "
                "named dimensions (proven in Creo 13.4 by creo_create_packaging_box)"
            ),
        ],
        "unsupported": [
            (
                "IpfcSolid.CreateFeature for ordinary extrude/revolve: the Creo 13.4 VB API "
                "type library exposes no public instruction factories for those features"
            ),
            (
                "raw .gph placement without a qualified manifest (Creo would prompt interactively "
                "and break the automation contract)"
            ),
        ],
        "rule": "general feature creation must go through a qualified UDF manifest; do not promise "
        "native sketch-based extrude/revolve until a qualified asset exists",
    },
    "model_tree": {
        "editable": [
            "named driving dimensions, typed parameters and semantic model-item names via "
            "creo_update_model_metadata",
            "model-tree suppression via creo_update_feature_suppression",
        ],
        "unsupported": [
            (
                "family table, simplified representation, and program-driven suppression states: "
                "that mechanism owns them, and creo_update_feature_suppression refuses such features"
            ),
        ],
        "rule": "suppression is only confirmed when each feature status is read back after regeneration",
    },
    "cad_input": {
        "auto_detected": ["step", "iges", "parasolid", "acis", "stl", "jt", "catia", "solidworks",
                          "inventor", "vda", "creo_neutral"],
        "ambiguous": {
            ".prt": "native Creo part or NX part; never auto-detect. Use creo_open_native_model for "
                    "native and creo_import_cad with source_format='nx' for NX",
        },
        "mesh_caveat": "mesh and tessellated inputs import as reference geometry; they are not "
        "editable B-rep solids",
    },
    "units": {
        "length": "millimetres after normalization; native reported unit is preserved in results",
        "angles": "degrees",
        "rule": "unit conversion happens once, in the bridge, and is reported on every geometry result",
    },
    "verification": {
        "after_edit": ["regenerate the model", "re-measure the envelope and volume",
                       "confirm the returned feature names and geometry before claiming success"],
        "never": ["implicitly overwrite, delete, purge or replace an existing model",
                  "save or export merely because a model was opened or created"],
    },
}


def _documentation_text(name: str) -> str:
    """Read one bundled Markdown document from the installed package."""
    return (files("creo_mcp") / "docs" / name).read_text(encoding="utf-8")


def _preview_enabled() -> bool:
    mode = os.environ.get("CREO_MCP_DISPLAY_MODE", "reopen").strip().lower()
    return mode in {"reopen", "display_only", "display"}


def _attach_image(result: Any, path: Path, key: str) -> Any:
    payload = result.model_dump(mode="json", by_alias=True)
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps({"result": payload, key: str(path)}, ensure_ascii=False),
            ),
            Image(path=str(path), format="png").to_image_content(),
        ],
        structuredContent=payload,
    )


def _with_preview(result: Any) -> Any:
    """Attach a screenshot of the visible Creo window to a tool result.

    The structured result is preserved, so clients that only read structured
    content are unaffected. If the window cannot be captured, the plain result
    is returned and the CAD operation still succeeds.
    """
    if not _preview_enabled():
        return result
    try:
        from .screenshot import capture_creo_window

        path, _title, _width, _height = capture_creo_window()
        return _attach_image(result, path, "screenshot_path")
    except Exception:
        return result


def _plan_preview_enabled() -> bool:
    return os.environ.get("CREO_MCP_PLAN_PREVIEW", "1").strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def _plan_with_preview(plan: Any) -> Any:
    """Render a Creo-free iso+top picture of the planned tray next to the plan.

    Orientation mistakes (wrong axis chosen as the insertion direction) are
    obvious in the picture and cheap to catch at dry-run time instead of after
    a multi-minute Creo import. Any rendering problem returns the bare plan.
    """
    if not _plan_preview_enabled():
        return plan
    try:
        from .clearance_geometry import build_product_clearance_tray_shape
        from .preview_render import render_shape_preview
        from .screenshot import screenshots_dir

        shape = build_product_clearance_tray_shape(plan)
        path = screenshots_dir() / f"plan_{plan.plan_id}.png"
        render_shape_preview(
            shape, path, f"plan {plan.plan_id}  product: {Path(plan.product_step_path).name}"
        )
        return _attach_image(plan, path, "preview_path")
    except Exception:
        return plan


_ISOLATED_CALL_LOCK = threading.Lock()
_CIRCUIT_STATE_LOCK = threading.Lock()
_timed_out_worker: subprocess.Popen[str] | None = None
_persistent_worker: subprocess.Popen[str] | None = None
_persistent_worker_responses: queue.Queue[str | None] | None = None
_persistent_worker_stderr: list[str] = []


def _probe_script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "creo_mcp_server.py"


def _worker_command(*args: str) -> list[str]:
    """Build the command that re-enters this server in worker mode.

    The repository shim (``creo_mcp_server.py``) is used when it exists so a
    local checkout keeps its exact import behaviour. A wheel installation has no
    shim next to the package, so fall back to the module entry point instead of
    spawning a process that cannot be found.
    """
    script = _probe_script_path()
    if script.is_file():
        return [sys.executable, str(script), *args]
    return [sys.executable, "-m", "creo_mcp.server", *args]


def _ensure_worker_circuit_closed() -> None:
    global _timed_out_worker
    with _CIRCUIT_STATE_LOCK:
        worker = _timed_out_worker
        if worker is not None and worker.poll() is not None:
            _timed_out_worker = None
            worker = None
    if worker is not None:
        raise CreoMcpError(
            f"Creo MCP worker PID {worker.pid} is still finishing a timed-out COM call. "
            "New Creo calls are blocked to protect the VB API connection. Do not retry "
            "automatically or run process-management shell commands. Ask the operator to wait; "
            "if it remains stuck, the operator must recover the Creo VB API connection first."
        )


def _mark_timed_out_worker(worker: subprocess.Popen[str]) -> None:
    global _timed_out_worker
    with _CIRCUIT_STATE_LOCK:
        _timed_out_worker = worker


def _clear_timed_out_worker(worker_pid: int) -> None:
    global _timed_out_worker
    with _CIRCUIT_STATE_LOCK:
        if _timed_out_worker is not None and _timed_out_worker.pid == worker_pid:
            _timed_out_worker = None


def _drain_timed_out_worker(process: subprocess.Popen[str]) -> None:
    """Let a timed-out COM client finish and disconnect instead of terminating it."""
    try:
        process.wait()
    finally:
        _clear_timed_out_worker(process.pid)


def _run_isolated_process(
    command: list[str],
    *,
    timeout_seconds: int,
    timeout_message: str,
) -> subprocess.CompletedProcess[str]:
    """Run one serialized worker without killing it in the middle of a COM call."""
    _ensure_worker_circuit_closed()
    acquired = _ISOLATED_CALL_LOCK.acquire(timeout=MCP_CALL_LOCK_TIMEOUT_SECONDS)
    if not acquired:
        raise CreoMcpError(
            "Creo is busy with another MCP operation. Wait for it to finish before retrying; "
            "parallel Creo tool calls are intentionally disabled."
        )

    try:
        _ensure_worker_circuit_closed()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _mark_timed_out_worker(process)
            threading.Thread(
                target=_drain_timed_out_worker,
                args=(process,),
                name=f"creo-mcp-worker-{process.pid}",
                daemon=True,
            ).start()
            raise CreoMcpError(
                f"{timeout_message} Worker PID {process.pid} was left running so it can "
                "finish or disconnect cleanly; it was not force-killed. Do not retry the "
                "operation automatically and do not run process-management shell commands. "
                "Ask the operator to wait for the worker to exit. A timed-out write has an "
                "unknown outcome, so inspect Creo and the operation log before another write."
            ) from exc

        return subprocess.CompletedProcess(
            process.args,
            process.returncode,
            stdout,
            stderr,
        )
    finally:
        _ISOLATED_CALL_LOCK.release()


def _run_probe() -> int:
    """Run one serialized status probe and emit a single machine-readable line."""
    try:
        response = _call_creo({"kind": "status"}, timeout_seconds=PROBE_TIMEOUT_SECONDS)
        if not response.get("ok"):
            raise CreoMcpError(str(response.get("error", "Unknown Creo VB API error.")))
        print(json.dumps({"ok": True, "result": response["result"]}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


def _execute_operation(
    operation: str, payload: dict[str, Any], progress_cb: Any = None
) -> Any:
    """Validate and execute one named Creo operation in the current worker process."""
    if operation == "create_empty_part":
        return create_empty_part(CreatePartRequest.model_validate(payload))
    if operation == "inspect_part":
        return inspect_part(InspectPartRequest.model_validate(payload))
    if operation == "create_packaging_box":
        return create_packaging_box(CreatePackagingBoxRequest.model_validate(payload))
    if operation == "plan_product_clearance_tray":
        return plan_product_clearance_tray(
            ProductClearanceTrayPlanRequest.model_validate(payload),
            progress_cb=progress_cb,
        )
    if operation == "create_product_clearance_tray":
        return create_product_clearance_tray(
            CreateProductClearanceTrayRequest.model_validate(payload),
            progress_cb=progress_cb,
        )
    if operation == "inspect_step_solids":
        return inspect_step_solids(InspectStepRequest.model_validate(payload))
    if operation == "inspect_step":
        return inspect_step_file(InspectStepRequest.model_validate(payload))
    if operation == "import_step":
        return import_step(
            ImportStepRequest.model_validate(payload), progress_cb=progress_cb
        )
    if operation == "import_cad":
        return import_cad(
            ImportCadRequest.model_validate(payload), progress_cb=progress_cb
        )
    if operation == "build_geometry":
        return build_geometry(
            BuildGeometryRequest.model_validate(payload), progress_cb=progress_cb
        )
    if operation == "inspect_model_geometry":
        return inspect_model_geometry(InspectModelGeometryRequest.model_validate(payload))
    if operation == "inspect_geometry_details":
        return inspect_geometry_details(InspectGeometryDetailsRequest.model_validate(payload))
    if operation == "find_surfaces":
        return find_surfaces(FindSurfacesRequest.model_validate(payload))
    if operation == "inspect_model_structure":
        return inspect_model_structure(InspectModelStructureRequest.model_validate(payload))
    if operation == "validate_udf_manifest":
        return validate_udf_manifest(ValidateUdfManifestRequest.model_validate(payload))
    if operation == "open_native_model":
        return open_native_model(
            OpenNativeModelRequest.model_validate(payload), progress_cb=progress_cb
        )
    if operation == "place_udf":
        return place_udf(PlaceUdfRequest.model_validate(payload))
    if operation == "qualify_udf":
        return run_udf_qualification(RunUdfQualificationRequest.model_validate(payload))
    if operation == "update_feature_suppression":
        return update_feature_suppression(
            UpdateFeatureSuppressionRequest.model_validate(payload)
        )
    if operation == "update_model_metadata":
        return update_model_metadata(UpdateModelMetadataRequest.model_validate(payload))
    if operation == "handoff_report":
        return get_handoff_report(HandoffReportRequest.model_validate(payload))
    if operation == "save_model_as":
        return save_model_as(SaveModelAsRequest.model_validate(payload))
    if operation == "export_model":
        return export_model(ExportModelRequest.model_validate(payload))
    raise CreoMcpError(f"Unsupported internal Creo operation: {operation}")


def _operation_response(
    operation: str, payload: dict[str, Any], progress_cb: Any = None
) -> dict[str, Any]:
    """Execute and log one operation, returning its machine-readable response."""
    operation_id = uuid.uuid4().hex
    started = time.perf_counter()
    try:
        result = _execute_operation(operation, payload, progress_cb=progress_cb)
        duration_ms = round((time.perf_counter() - started) * 1000)
        logged = append_operation_result(operation_id, operation, duration_ms, result)
        return {
            "ok": True,
            "operation_id": operation_id,
            "log": {"written": logged, "path": str(operation_log_path())},
            "result": result.model_dump(),
        }
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started) * 1000)
        logged = append_operation_record(
            operation_id=operation_id,
            operation=operation,
            duration_ms=duration_ms,
            success=False,
            error_type=type(exc).__name__,
        )
        return {
            "ok": False,
            "operation_id": operation_id,
            "log": {"written": logged, "path": str(operation_log_path())},
            "error": str(exc),
        }


def _run_operation(operation: str, payload: dict[str, Any]) -> int:
    """Run one operation and emit one JSON result line for the parent process.

    A running broker owns the only usable VB API connection, so when one is
    alive the request is routed through it exactly like an MCP tool call.
    Opening a second direct connection fails with a busy error, which previously
    made this documented diagnostic path unusable whenever the broker was up.
    Direct in-process execution is kept for the standalone single-shot case.
    """
    if broker.ping() is not None:
        try:
            response = _call_creo(
                {"kind": "operation", "operation": operation, "payload": payload},
                timeout_seconds=OPERATION_TIMEOUT_SECONDS,
            )
        except CreoMcpError as exc:
            response = {"ok": False, "error": str(exc)}
        # The broker runs the same _operation_response as the direct path, so the
        # operation_id and audit-log location documented in README survive here too.
        print(json.dumps({"via": "broker", **response}, ensure_ascii=False))
        return 0 if response.get("ok") else 1

    response = _operation_response(operation, payload)
    print(json.dumps(response))
    return 0 if response["ok"] else 1


def _read_persistent_worker_stdout(
    process: subprocess.Popen[str],
    responses: queue.Queue[str | None],
) -> None:
    assert process.stdout is not None
    try:
        for line in process.stdout:
            stripped = line.strip()
            marker_index = stripped.find(PERSISTENT_WORKER_RESPONSE_PREFIX)
            if marker_index < 0:
                if stripped:
                    _persistent_worker_stderr.append(f"worker stdout: {stripped}")
                    del _persistent_worker_stderr[:-50]
                continue
            diagnostic = stripped[:marker_index].strip()
            if diagnostic:
                _persistent_worker_stderr.append(f"worker stdout: {diagnostic}")
                del _persistent_worker_stderr[:-50]
            responses.put(stripped[marker_index + len(PERSISTENT_WORKER_RESPONSE_PREFIX) :])
    finally:
        responses.put(None)


def _emit_persistent_worker_response(payload: dict[str, Any]) -> None:
    """Frame protocol output so native libraries cannot be mistaken for responses."""
    print(f"{PERSISTENT_WORKER_RESPONSE_PREFIX}{json.dumps(payload)}", flush=True)


def _read_persistent_worker_stderr(process: subprocess.Popen[str]) -> None:
    assert process.stderr is not None
    for line in process.stderr:
        _persistent_worker_stderr.append(line.rstrip())
        del _persistent_worker_stderr[:-50]


def _abandon_persistent_worker(process: subprocess.Popen[str], timeout_message: str) -> None:
    """Stop sending work and let a timed-out persistent worker disconnect when possible."""
    global _persistent_worker, _persistent_worker_responses

    _mark_timed_out_worker(process)
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    _persistent_worker = None
    _persistent_worker_responses = None
    threading.Thread(
        target=_drain_timed_out_worker,
        args=(process,),
        name=f"creo-mcp-worker-{process.pid}",
        daemon=True,
    ).start()
    raise CreoMcpError(
        f"{timeout_message} Persistent worker PID {process.pid} was left running so it can "
        "finish and disconnect cleanly; it was not force-killed. Do not retry automatically. "
        "A timed-out write has an unknown outcome, so inspect Creo and the operation log first."
    )


def _start_persistent_worker() -> tuple[subprocess.Popen[str], queue.Queue[str | None]]:
    """Start one process that owns one VB API connection for its entire lifetime."""
    global _persistent_worker, _persistent_worker_responses

    process = subprocess.Popen(
        _worker_command("--persistent-worker"),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    responses: queue.Queue[str | None] = queue.Queue()
    _persistent_worker_stderr.clear()
    threading.Thread(
        target=_read_persistent_worker_stdout,
        args=(process, responses),
        name=f"creo-mcp-worker-stdout-{process.pid}",
        daemon=True,
    ).start()
    threading.Thread(
        target=_read_persistent_worker_stderr,
        args=(process,),
        name=f"creo-mcp-worker-stderr-{process.pid}",
        daemon=True,
    ).start()
    _persistent_worker = process
    _persistent_worker_responses = responses

    try:
        ready_line = responses.get(timeout=PROBE_TIMEOUT_SECONDS)
    except queue.Empty:
        _abandon_persistent_worker(
            process,
            "Timed out while starting the persistent Creo VB API connection.",
        )
    if ready_line is None:
        detail = "\n".join(_persistent_worker_stderr[-10:]) or "worker exited"
        _persistent_worker = None
        _persistent_worker_responses = None
        raise CreoMcpError(f"Creo persistent worker failed to start: {detail}")

    try:
        ready = json.loads(ready_line)
    except json.JSONDecodeError as exc:
        raise CreoMcpError(
            f"Creo persistent worker returned invalid startup data: {ready_line}"
        ) from exc
    if not ready.get("ready"):
        _persistent_worker = None
        _persistent_worker_responses = None
        raise CreoMcpError(str(ready.get("error", "Creo persistent worker failed to connect.")))
    return process, responses


def _persistent_worker_call(request: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
    """Serialize one request through the long-running COM worker."""
    global _persistent_worker, _persistent_worker_responses

    _ensure_worker_circuit_closed()
    acquired = _ISOLATED_CALL_LOCK.acquire(timeout=MCP_CALL_LOCK_TIMEOUT_SECONDS)
    if not acquired:
        raise CreoMcpError(
            "Creo is busy with another MCP operation. Parallel Creo calls are disabled."
        )
    try:
        _ensure_worker_circuit_closed()
        process = _persistent_worker
        responses = _persistent_worker_responses
        if process is None or responses is None or process.poll() is not None:
            _persistent_worker = None
            _persistent_worker_responses = None
            process, responses = _start_persistent_worker()

        request_id = uuid.uuid4().hex
        request = {**request, "request_id": request_id}
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            _persistent_worker = None
            _persistent_worker_responses = None
            raise CreoMcpError(
                "Creo persistent worker closed before receiving the request."
            ) from exc

        try:
            response_line = responses.get(timeout=timeout_seconds)
        except queue.Empty:
            _abandon_persistent_worker(
                process,
                f"Timed out while running persistent Creo request {request.get('kind')!r}.",
            )
        if response_line is None:
            detail = "\n".join(_persistent_worker_stderr[-10:]) or "worker exited"
            _persistent_worker = None
            _persistent_worker_responses = None
            raise CreoMcpError(f"Creo persistent worker exited unexpectedly: {detail}")

        try:
            response: dict[str, Any] = json.loads(response_line)
        except json.JSONDecodeError as exc:
            raise CreoMcpError(
                f"Creo persistent worker returned an invalid response: {response_line}"
            ) from exc
        if response.get("request_id") != request_id:
            raise CreoMcpError("Creo persistent worker returned a mismatched response.")
        return response
    finally:
        _ISOLATED_CALL_LOCK.release()


def _run_persistent_worker() -> int:
    """Own one COM connection and execute newline-delimited requests on one thread."""
    ready_sent = False
    try:
        with persistent_creo_connection():
            _emit_persistent_worker_response({"ready": True})
            ready_sent = True
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                    if request.get("kind") == "shutdown":
                        break
                    if request.get("kind") == "status":
                        try:
                            result = get_status()
                            response = {"ok": True, "result": result.model_dump()}
                        except Exception as exc:
                            response = {"ok": False, "error": str(exc)}
                    elif request.get("kind") == "operation":
                        response = _operation_response(
                            str(request.get("operation", "")),
                            dict(request.get("payload") or {}),
                        )
                    else:
                        response = {"ok": False, "error": "Unknown persistent worker request."}
                    response["request_id"] = request.get("request_id")
                    _emit_persistent_worker_response(response)
                except Exception as exc:
                    _emit_persistent_worker_response(
                        {
                            "ok": False,
                            "request_id": None,
                            "error": f"Invalid persistent worker request: {exc}",
                        }
                    )
        return 0
    except Exception as exc:
        if not ready_sent:
            _emit_persistent_worker_response({"ready": False, "error": str(exc)})
        return 1


def _shutdown_persistent_worker() -> None:
    """Ask the healthy worker to disconnect cleanly when the MCP transport exits."""
    process = _persistent_worker
    if process is None or process.poll() is not None or process.stdin is None:
        return
    try:
        process.stdin.write(json.dumps({"kind": "shutdown"}) + "\n")
        process.stdin.flush()
        process.stdin.close()
        process.wait(timeout=10)
    except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
        pass


def _call_creo(
    request: dict[str, Any], *, timeout_seconds: int, on_progress: Any = None
) -> dict[str, Any]:
    """Route one request through the singleton broker (or the legacy worker)."""
    if os.environ.get("CREO_MCP_TRANSPORT", "broker").lower() == "worker":
        return _persistent_worker_call(request, timeout_seconds=timeout_seconds)
    try:
        return broker.call(
            request, timeout_seconds=timeout_seconds, on_progress=on_progress
        )
    except broker.BrokerError as exc:
        raise CreoMcpError(str(exc)) from exc


def _status_from_isolated_process() -> CreoStatus:
    """Read status through the single-instance Creo broker."""
    payload = _call_creo(
        {"kind": "status"},
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
    )
    if not payload.get("ok"):
        raise CreoMcpError(str(payload.get("error", "Unknown Creo VB API error.")))
    return CreoStatus.model_validate(payload["result"])


def _run_isolated_operation(
    operation: str, payload: dict[str, Any], on_progress: Any = None
) -> dict[str, Any]:
    """Execute one explicit operation through the single-instance Creo broker."""
    result = _call_creo(
        {"kind": "operation", "operation": operation, "payload": payload},
        timeout_seconds=OPERATION_TIMEOUT_SECONDS,
        on_progress=on_progress,
    )
    if not result.get("ok"):
        raise CreoMcpError(str(result.get("error", "Unknown Creo operation error.")))
    return result["result"]


async def _drain_progress(frames: queue.SimpleQueue, ctx: Context) -> None:
    while True:
        try:
            frame = frames.get_nowait()
        except queue.Empty:
            return
        try:
            await ctx.report_progress(
                float(frame.get("progress", 0.0)),
                total=frame.get("total"),
                message=frame.get("message"),
            )
        except Exception:
            pass


async def _await_with_progress(
    task: asyncio.Task, frames: queue.SimpleQueue, ctx: Context
) -> Any:
    try:
        while not task.done():
            await asyncio.sleep(0.1)
            await _drain_progress(frames, ctx)
        await _drain_progress(frames, ctx)
        return await task
    except asyncio.CancelledError:
        # The client hung up; the worker call cannot be cancelled mid-COM, so let
        # it finish in the background and record that we stopped relaying.
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        raise


async def _run_isolated_operation_with_progress(
    operation: str, payload: dict[str, Any], ctx: Context
) -> dict[str, Any]:
    """Run a long broker operation in a worker thread and relay progress to the client.

    The blocking broker call must leave the event loop free so MCP
    ``notifications/progress`` frames can actually be sent while the operation
    runs; progress frames are queued thread-safely and drained by the relay.
    """
    frames: queue.SimpleQueue = queue.SimpleQueue()
    task = asyncio.create_task(
        asyncio.to_thread(_run_isolated_operation, operation, payload, frames.put)
    )
    return await _await_with_progress(task, frames, ctx)


def create_server(host: str = "127.0.0.1", port: int = 8000) -> FastMCP:
    """Create an MCP server with stable, portable tool names and JSON schemas."""
    server = FastMCP(
        SERVER_NAME,
        instructions=(
            "Use Creo tools only for explicit CAD requests. Inspect and plan before writing. "
            "Model edits, native copies, and exports require exact confirmation values. "
            "Edits remain unsaved by default, and file operations never permit overwrite. "
            "For product packaging/trays you MUST use the validated pipeline: "
            "creo_plan_product_clearance_tray first, then creo_create_product_clearance_tray "
            "with the returned plan_id. Never hand-assemble trays from bounding boxes with "
            "creo_build_geometry: it produces ad-hoc single solids and its outer walls would "
            "follow the boxes, not the guaranteed rectangular clearance-tray geometry. "
            "Never call Creo tools concurrently. After a busy or timeout error, do not retry "
            "automatically and do not use shell commands to wait for or manage MCP processes; "
            "report the recovery guidance to the user."
        ),
        host=host,
        port=port,
    )
    # FastMCP does not currently expose its low-level server version argument.
    server._mcp_server.version = SERVER_VERSION

    @server.resource(
        "creo://docs/architecture",
        name="creo_architecture_zh_cn",
        title="Creo MCP 架构边界",
        description="通用 MCP、工程数据与产品 Skill 的职责边界。",
        mime_type="text/markdown",
    )
    def creo_architecture_resource() -> str:
        return _documentation_text("ARCHITECTURE.zh-CN.md")

    @server.resource(
        "creo://docs/engineering-standard",
        name="creo_engineering_standard_zh_cn",
        title="Creo MCP 工程交付与可维护性规范",
        description="人工接手、模型命名、可编辑性、日志和验收要求。",
        mime_type="text/markdown",
    )
    def creo_engineering_standard_resource() -> str:
        return _documentation_text("ENGINEERING_STANDARD.zh-CN.md")

    @server.resource(
        "creo://docs/udf-manifest",
        name="creo_udf_manifest_zh_cn",
        title="Creo UDF 清单规范",
        description="UDF 候选资格、完整性校验和正式放置流程。",
        mime_type="text/markdown",
    )
    def creo_udf_manifest_resource() -> str:
        return _documentation_text("UDF_MANIFEST.zh-CN.md")

    @server.resource(
        "creo://docs/live-validation",
        name="creo_live_validation_zh_cn",
        title="Creo MCP 真实会话验证记录",
        description="在真实 Creo 13.4.1.0 会话中已验证和待验证的能力。",
        mime_type="text/markdown",
    )
    def creo_live_validation_resource() -> str:
        return _documentation_text("LIVE_VALIDATION.zh-CN.md")

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
        return _documentation_text("CLIENT_SETUP.zh-CN.md")

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
        name="creo_capture_screenshot",
        title="Capture Creo Window",
        description=(
            "Capture the visible Creo Parametric application window as a PNG screenshot "
            "for visual verification. The tool does not change the model and requires the "
            "Creo window to be visible on the interactive desktop."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    def creo_capture_screenshot(output_path: str | None = None) -> Image:
        """Return a PNG image of the main Creo window."""
        from .screenshot import capture_creo_window

        path, _title, _width, _height = capture_creo_window(output_path)
        return Image(path=str(path), format="png")

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
        name="creo_plan_product_clearance_tray",
        title="Plan a Hard-Edge Product Clearance Tray",
        description=(
            "Read the complete millimetre STEP product, apply a confirmed orientation, "
            "derive a conservative hard-edge clearance cavity from every face of its full "
            "BREP, record mesh resolution and added error allowance, extend each local-depth "
            "profile to the opening plane, and verify static and linear-insertion containment. "
            "The baseline has zero draft, zero corner radius, and no automatic handling "
            "reliefs. Returns a deterministic CONCEPT_ONLY plan without connecting to Creo "
            "or changing files."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    async def creo_plan_product_clearance_tray(
        request: ProductClearanceTrayPlanRequest, ctx: Context
    ) -> ProductClearanceTrayPlan:
        """Plan a verified full-geometry safety baseline before forming optimization."""
        frames: queue.SimpleQueue = queue.SimpleQueue()

        def _plan_cb(value: float, message: str) -> None:
            frames.put({"progress": value, "total": 100, "message": message})

        task = asyncio.create_task(
            asyncio.to_thread(
                plan_product_clearance_tray, request, progress_cb=_plan_cb
            )
        )
        plan = await _await_with_progress(task, frames, ctx)
        return await asyncio.to_thread(_plan_with_preview, plan)

    @server.tool(
        name="creo_inspect_step_solids",
        title="Inspect Individual STEP Solids",
        description=(
            "Measure the axis-aligned extent and volume of every solid in a millimetre STEP "
            "in stable transfer order. Use this to plan separate cavities for multi-solid "
            "products. This tool never connects to Creo or changes files."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
        structured_output=True,
    )
    def creo_inspect_step_solids(request: InspectStepRequest) -> InspectStepSolidsResult:
        """Return one measurement record per STEP solid body."""
        return inspect_step_solids(request)

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
            "the current model and never saves a file. Requires "
            "confirmation='create_empty_part' because it changes Creo session state."
        ),
        annotations=WRITE_ANNOTATIONS,
        structured_output=True,
    )
    def creo_create_empty_part(request: CreatePartRequest) -> CreatePartResult:
        """Create an unsaved part as the first verified, non-destructive write operation."""
        result = CreatePartResult.model_validate(
            _run_isolated_operation("create_empty_part", request.model_dump())
        )
        return _with_preview(result) if request.activate else result

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
        name="creo_update_feature_suppression",
        title="Suppress Or Resume Creo Features",
        description=(
            "Suppress or resume model-tree features in the active or a named loaded model. "
            "Target each feature by name (case-insensitive; preferred for copied templates) "
            "or by a stable item_id from creo_get_model_structure. Every target resolves "
            "before anything is touched: unknown, ambiguous, or status-less features fail "
            "the call, and features held suppressed by a family table, simplified "
            "representation, or the model program are refused. Applied changes regenerate "
            "the model, then each status is read back and a mismatch is reported as an "
            "error. Features already in the requested state are reported unchanged without "
            "a Creo call. Never saves. Requires confirmation='update_feature_suppression'."
        ),
        annotations=MODEL_EDIT_ANNOTATIONS,
        structured_output=True,
    )
    def creo_update_feature_suppression(
        request: UpdateFeatureSuppressionRequest,
    ) -> UpdateFeatureSuppressionResult:
        """Suppress or resume resolved features, regenerate, and verify each status."""
        return UpdateFeatureSuppressionResult.model_validate(
            _run_isolated_operation("update_feature_suppression", request.model_dump())
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
    async def creo_open_native_model(
        request: OpenNativeModelRequest, ctx: Context
    ) -> OpenNativeModelResult:
        """Load and measure one native Creo model without changing its source file."""
        result = OpenNativeModelResult.model_validate(
            await _run_isolated_operation_with_progress(
                "open_native_model", request.model_dump(), ctx
            )
        )
        if request.activate:
            return await asyncio.to_thread(_with_preview, result)
        return result

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
        result = CreatePackagingBoxResult.model_validate(
            _run_isolated_operation("create_packaging_box", request.model_dump())
        )
        return _with_preview(result) if request.activate else result

    @server.tool(
        name="creo_create_product_clearance_tray",
        title="Create a Hard-Edge Product Clearance Tray",
        description=(
            "Create the verified conservative hard-edge safety baseline from the complete "
            "product BREP projection and its linear insertion envelope. Import it as one "
            "CONCEPT_ONLY BREP feature, record mesh resolution plus static and insertion-path "
            "verification, save under a new name, activate, and repaint. Requires a matching "
            "plan_id and "
            "confirmation='create_product_clearance_concept'. Never overwrites."
        ),
        annotations=WRITE_ANNOTATIONS,
        structured_output=True,
    )
    async def creo_create_product_clearance_tray(
        request: CreateProductClearanceTrayRequest, ctx: Context
    ) -> CreateProductClearanceTrayResult:
        """Create one hard-edge full-product safety baseline."""
        result = CreateProductClearanceTrayResult.model_validate(
            await _run_isolated_operation_with_progress(
                "create_product_clearance_tray",
                request.model_dump(),
                ctx,
            )
        )
        if request.activate:
            return await asyncio.to_thread(_with_preview, result)
        return result

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
    async def creo_import_cad(
        request: ImportCadRequest, ctx: Context
    ) -> ImportCadResult:
        """Import and measure one new unsaved Creo model from a supported CAD source."""
        result = ImportCadResult.model_validate(
            await _run_isolated_operation_with_progress(
                "import_cad", request.model_dump(), ctx
            )
        )
        if request.activate:
            return await asyncio.to_thread(_with_preview, result)
        return result

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
    async def creo_import_step(
        request: ImportStepRequest, ctx: Context
    ) -> ImportStepResult:
        """Import and measure one new unsaved Creo model from STEP."""
        result = ImportStepResult.model_validate(
            await _run_isolated_operation_with_progress(
                "import_step", request.model_dump(), ctx
            )
        )
        if request.activate:
            return await asyncio.to_thread(_with_preview, result)
        return result

    @server.tool(
        name="creo_build_geometry",
        title="Build Solid From Geometry Program",
        description=(
            "Build one solid from a constrained JSON geometry program — box/cylinder/sphere "
            "primitives plus source (existing millimetre STEP solids as operands) combined "
            "with union/difference/intersect, mirror, linear_pattern, and transform "
            "(rotate+translate) in millimetres — validate it as exactly one closed solid "
            "(max 2000 mm), export STEP, and import it as a new saved Creo part under a "
            "name that must not exist yet. The imported feature is named GENERATED_SOLID; "
            "this produces a solid-body concept, not parametric sketch features. Requires "
            "confirmation='build_geometry'. Emits progress notifications and, when "
            "activated, returns an automatic Creo window screenshot. Do NOT use this tool "
            "to hand-build packaging trays from boxes — use creo_plan_product_clearance_tray "
            "and creo_create_product_clearance_tray for trays."
        ),
        annotations=WRITE_ANNOTATIONS,
        structured_output=True,
    )
    async def creo_build_geometry(
        request: BuildGeometryRequest, ctx: Context
    ) -> BuildGeometryResult:
        """Build, validate, and import one JSON-program solid as a new Creo part."""
        result = BuildGeometryResult.model_validate(
            await _run_isolated_operation_with_progress(
                "build_geometry", request.model_dump(), ctx
            )
        )
        if request.activate:
            return await asyncio.to_thread(_with_preview, result)
        return result

    # ── Meta tools and machine-readable resources ────────────────────
    # Mirrors the discovery pattern proven by the ZW3D MCP server: a compact
    # in-band catalog plus explicit capability boundaries, so a caller can learn
    # the tool surface without paying for a large tools/list payload every turn.

    @server.tool(
        name="creo_tool_catalog",
        title="List Creo MCP Tools",
        description=(
            "List every Creo MCP tool grouped by capability, with title, read/write group, and "
            "parameter count. Use this to discover available operations or to confirm a tool name "
            "before writing a workflow. Set include_schema=true only when full input schemas are "
            "needed. Returns a compact JSON object and never changes Creo."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    async def creo_tool_catalog(include_schema: bool = False) -> dict[str, Any]:
        """Return the compact tool catalog for this server."""
        from .catalog import build_catalog, render_json

        catalog = await build_catalog(server)
        return render_json(catalog, include_schema=include_schema)

    @server.tool(
        name="creo_get_capabilities",
        title="Get Creo Capability Boundaries",
        description=(
            "Return the hard capability boundaries of this Creo bridge: which general feature "
            "creation paths the official Creo VB API does not expose, which CAD inputs are "
            "ambiguous, how units are normalized, and what must be verified after an edit. Read "
            "this before promising a modeling operation that may be unsupported. Never changes Creo."
        ),
        annotations=READ_ONLY_ANNOTATIONS,
    )
    def creo_get_capabilities() -> dict[str, Any]:
        """Return documented capability boundaries as structured data."""
        return CAPABILITY_BOUNDARIES

    @server.resource(
        "creo://server/status",
        name="creo_server_status",
        title="Creo MCP 服务器状态",
        description="服务器版本、工具面统计与本机预检结果（只读，不修改 Creo）。",
        mime_type="application/json",
    )
    async def creo_server_status_resource() -> str:
        from .diagnostics import run_doctor

        report = await asyncio.to_thread(lambda: run_doctor(probe_creo=False))
        payload = {
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "tool_count": len(await server.list_tools()),
            "resource_count": len(await server.list_resources()),
            "preflight": report.to_dict(),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @server.resource(
        "creo://tools/catalog",
        name="creo_tool_catalog_resource",
        title="Creo MCP 工具目录",
        description="全部工具的 JSON 目录（按能力分组，只读）。",
        mime_type="application/json",
    )
    async def creo_tools_catalog_resource() -> str:
        from .catalog import build_catalog, render_json

        catalog = await build_catalog(server)
        return json.dumps(render_json(catalog), ensure_ascii=False, indent=2)

    return server


def _tool_catalog_for_cli(*, as_json: bool, include_schema: bool) -> int:
    """Print the tool catalog without starting a transport (maintainer surface)."""
    from .catalog import build_catalog, find_issues, render_json, render_text

    async def _build():
        return await build_catalog(create_server())

    catalog = asyncio.run(_build())
    if as_json:
        print(
            json.dumps(
                render_json(catalog, include_schema=include_schema),
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(render_text(catalog, include_schema=include_schema))
        issues = find_issues(catalog)
        if issues:
            print(f"\nConvention findings: {len(issues)}")
            for issue in issues[:20]:
                print(f"  - {issue}")
    return 0


def _doctor_for_cli(*, probe: bool, as_json: bool) -> int:
    """Run the local preflight checks and print an actionable report."""
    from .diagnostics import STATUS_FAIL, run_doctor

    report = run_doctor(probe_creo=probe)
    if as_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(report.render_text())
    return 1 if report.status == STATUS_FAIL else 0


USAGE = """\
Creo MCP server

Usage:
  creo-mcp                          Serve MCP over stdio (default)
  creo-mcp --transport MODE         Serve with MODE = stdio | streamable-http
  creo-mcp --transport streamable-http [--host H] [--port P]
  creo-mcp --list                   Print every tool grouped by capability
  creo-mcp --list-json              Same, machine-readable JSON
  creo-mcp --list --with-schema     Also print one-line descriptions
  creo-mcp --list-json --with-schema  Include full input/output JSON schemas
  creo-mcp --doctor                 Run local preflight checks (no Creo mutation)
  creo-mcp --doctor --probe         Also verify a live Creo connection
  creo-mcp --doctor --json          Preflight report as JSON
  creo-mcp --broker | --broker-status | --broker-stop
  creo-mcp --probe-status
  creo-mcp --operation NAME --payload JSON
"""


def _flag_value(argv: list[str], flag: str) -> str | None:
    """Return the value following `flag`, or None when absent or valueless."""
    if flag not in argv:
        return None
    index = argv.index(flag) + 1
    if index >= len(argv):
        return None
    return argv[index]


def _configure_stdio_utf8() -> None:
    """Force UTF-8 on the CLI streams.

    The maintenance CLI prints Chinese hints. On Windows the inherited stream
    encoding follows the console code page (frequently cp936), so the text is
    re-encoded there and arrives garbled at anything decoding UTF-8 — a pipe or
    a redirected file, which is how agents read it. UTF-8 is also what the MCP
    stdio transport requires, so this is correct for the server path as well.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main() -> None:
    """Run either a maintenance CLI, a one-shot probe, the broker, or the MCP transport."""
    _configure_stdio_utf8()
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(USAGE, end="")
        raise SystemExit(0)
    if "--list" in argv or "--list-json" in argv:
        raise SystemExit(
            _tool_catalog_for_cli(
                as_json="--list-json" in argv,
                include_schema="--with-schema" in argv,
            )
        )
    if "--doctor" in argv:
        raise SystemExit(
            _doctor_for_cli(probe="--probe" in argv, as_json="--json" in argv)
        )
    if len(sys.argv) == 2 and sys.argv[1] == "--broker":
        raise SystemExit(broker.run_broker())
    if len(sys.argv) == 2 and sys.argv[1] == "--broker-status":
        print(json.dumps(broker.status(), ensure_ascii=False))
        raise SystemExit(0)
    if len(sys.argv) == 2 and sys.argv[1] == "--broker-stop":
        stopped = broker.stop()
        print(json.dumps({"stopped": stopped}, ensure_ascii=False))
        raise SystemExit(0 if stopped else 1)
    if len(sys.argv) == 2 and sys.argv[1] == "--probe-status":
        raise SystemExit(_run_probe())
    if len(sys.argv) == 2 and sys.argv[1] == "--persistent-worker":
        raise SystemExit(_run_persistent_worker())
    if len(sys.argv) == 5 and sys.argv[1] == "--operation" and sys.argv[3] == "--payload":
        try:
            payload = json.loads(sys.argv[4])
        except json.JSONDecodeError as exc:
            print(json.dumps({"ok": False, "error": f"Invalid operation payload: {exc}"}))
            raise SystemExit(1)
        raise SystemExit(_run_operation(sys.argv[2], payload))

    transport = _flag_value(argv, "--transport") or "stdio"
    if transport not in ("stdio", "streamable-http"):
        print(
            f"Unsupported --transport {transport!r}; use 'stdio' or 'streamable-http'.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    host = _flag_value(argv, "--host") or "127.0.0.1"
    raw_port = _flag_value(argv, "--port")
    try:
        port = int(raw_port) if raw_port is not None else 8000
    except ValueError:
        print(f"Invalid --port {raw_port!r}; expected an integer.", file=sys.stderr)
        raise SystemExit(2)

    try:
        create_server(host=host, port=port).run(transport=transport)
    finally:
        _shutdown_persistent_worker()


if __name__ == "__main__":
    main()
