"""Read-only validation and metadata extraction for STEP exchange files."""

from __future__ import annotations

import re
from pathlib import Path

from .errors import CreoMcpError
from .models import InspectStepRequest, StepFileInfo


MAX_STEP_FILE_BYTES = 250_000_000
_QUOTED_STRING = re.compile(r"'((?:''|[^'])*)'")


def resolve_step_path(raw_path: str) -> Path:
    """Resolve an explicit local STEP path and reject ambiguous or oversized inputs."""
    unresolved = Path(raw_path).expanduser()
    if not unresolved.is_absolute():
        raise CreoMcpError(f"STEP path must be absolute: {raw_path}")
    path = unresolved.resolve(strict=False)
    if not path.is_file():
        raise CreoMcpError(f"STEP file does not exist: {path}")
    if path.suffix.casefold() not in {".stp", ".step"}:
        raise CreoMcpError(f"Expected a .stp or .step file: {path}")
    if path.stat().st_size > MAX_STEP_FILE_BYTES:
        raise CreoMcpError(
            f"STEP file exceeds the {MAX_STEP_FILE_BYTES}-byte inspection limit: {path}"
        )
    return path


def _entity_payload(text: str, entity_name: str) -> str | None:
    match = re.search(rf"\b{re.escape(entity_name)}\s*\((.*?)\)\s*;", text, re.DOTALL)
    return None if match is None else match.group(1)


def _quoted_values(payload: str | None) -> list[str]:
    if payload is None:
        return []
    return [match.group(1).replace("''", "'") for match in _QUOTED_STRING.finditer(payload)]


def _declared_length_unit(text: str) -> str:
    compact = re.sub(r"\s+", "", text).upper()
    if "SI_UNIT(.MILLI.,.METRE.)" in compact:
        return "mm"
    if "SI_UNIT(.CENTI.,.METRE.)" in compact:
        return "cm"
    if "SI_UNIT($,.METRE.)" in compact:
        return "m"
    if re.search(r"CONVERSION_BASED_UNIT\('(?:INCH|IN)'", compact):
        return "inch"
    return "unknown"


def inspect_step_file(request: InspectStepRequest) -> StepFileInfo:
    """Validate a STEP physical file and return stable structural metadata."""
    path = resolve_step_path(request.step_path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if not text.lstrip().startswith("ISO-10303-21;") or "END-ISO-10303-21;" not in text:
        raise CreoMcpError(f"File is not a complete ISO-10303-21 STEP exchange file: {path}")

    file_name_values = _quoted_values(_entity_payload(text, "FILE_NAME"))
    schema_values = _quoted_values(_entity_payload(text, "FILE_SCHEMA"))
    product_names = list(
        dict.fromkeys(
            values[0]
            for match in re.finditer(r"\bPRODUCT\s*\((.*?)\)\s*;", text, re.DOTALL)
            if (values := _quoted_values(match.group(1)))
        )
    )
    assembly_relationship_count = len(
        re.findall(r"\bNEXT_ASSEMBLY_USAGE_OCCURRENCE\s*\(", text)
    )

    return StepFileInfo(
        step_path=str(path),
        file_size_bytes=path.stat().st_size,
        file_name=file_name_values[0] if file_name_values else None,
        timestamp=file_name_values[1] if len(file_name_values) > 1 else None,
        schema_name=schema_values[0] if schema_values else None,
        originating_system=file_name_values[-2] if len(file_name_values) >= 2 else None,
        product_names=product_names,
        solid_body_count=len(re.findall(r"\b(?:MANIFOLD_SOLID_BREP|BREP_WITH_VOIDS)\s*\(", text)),
        advanced_face_count=len(re.findall(r"\bADVANCED_FACE\s*\(", text)),
        closed_shell_count=len(re.findall(r"\bCLOSED_SHELL\s*\(", text)),
        assembly_relationship_count=assembly_relationship_count,
        declared_length_unit=_declared_length_unit(text),
        inferred_model_kind="assembly" if assembly_relationship_count else "part",
    )
