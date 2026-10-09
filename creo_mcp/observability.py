"""Privacy-minimized JSONL operation logging for local Creo automation."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import OperationLogRecord, OperationLogResult
from .version import SERVER_VERSION


def operation_log_path() -> Path:
    """Return the configured per-user operation log path."""
    configured = os.environ.get("CREO_MCP_LOG_DIR")
    if configured:
        directory = Path(configured).expanduser().resolve(strict=False)
    else:
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
        directory = base / "CreoMCP" / "logs"
    return directory / "operations.jsonl"


def append_operation_record(
    *,
    operation_id: str,
    operation: str,
    duration_ms: int,
    success: bool,
    error_type: str | None = None,
    target_model: str | None = None,
    saved: bool | None = None,
) -> bool:
    """Append one compact audit record without logging request payloads or model content."""
    record = OperationLogRecord(
        timestamp_utc=datetime.now(UTC).isoformat(),
        operation_id=operation_id,
        server_version=SERVER_VERSION,
        operation=operation,
        duration_ms=max(0, duration_ms),
        success=success,
        error_type=error_type,
        target_model=target_model,
        saved=saved,
    )
    try:
        path = operation_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(record.model_dump(), ensure_ascii=True) + "\n")
        return True
    except OSError:
        # Logging must never change the result of a Creo operation.
        return False


def _result_metadata(result: Any) -> tuple[str | None, bool | None]:
    payload = result.model_dump() if hasattr(result, "model_dump") else result
    if not isinstance(payload, dict):
        return None, None
    model = (
        payload.get("model")
        or payload.get("target_model")
        or payload.get("created_model")
        or payload.get("copied_model")
        or payload.get("source_model")
    )
    target_model = None
    if isinstance(model, dict):
        target_model = model.get("instance_name") or model.get("file_name")
    saved = payload.get("saved")
    return target_model, saved if isinstance(saved, bool) else None


def append_operation_result(
    operation_id: str,
    operation: str,
    duration_ms: int,
    result: Any,
) -> bool:
    target_model, saved = _result_metadata(result)
    return append_operation_record(
        operation_id=operation_id,
        operation=operation,
        duration_ms=duration_ms,
        success=True,
        target_model=target_model,
        saved=saved,
    )


def read_recent_operations(max_records: int) -> OperationLogResult:
    """Read the tail of the local JSONL log, ignoring malformed partial records."""
    path = operation_log_path()
    records: list[OperationLogRecord] = []
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        for line in reversed(lines):
            try:
                records.append(OperationLogRecord.model_validate_json(line))
            except Exception:
                continue
            if len(records) == max_records:
                break
        records.reverse()
    return OperationLogResult(log_path=str(path), records=records)
