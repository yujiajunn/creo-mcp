"""Offline qualification and integrity checks for Creo UDF assets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .errors import CreoMcpError
from .models import (
    UdfManifest,
    UdfManifestValidationResult,
    ValidateUdfManifestRequest,
)


@dataclass(frozen=True)
class QualifiedUdf:
    """Internal resolved form of a hash-verified manifest and UDF file."""

    manifest_path: Path
    udf_path: Path
    file_size_bytes: int
    sha256: str
    validation_id: str
    manifest: UdfManifest

    def to_result(self) -> UdfManifestValidationResult:
        return UdfManifestValidationResult(
            manifest_path=str(self.manifest_path),
            udf_path=str(self.udf_path),
            file_size_bytes=self.file_size_bytes,
            sha256=self.sha256,
            validation_id=self.validation_id,
            manifest=self.manifest,
        )


def _resolve_manifest_path(raw_path: str) -> Path:
    unresolved = Path(raw_path).expanduser()
    if not unresolved.is_absolute():
        raise CreoMcpError(f"UDF manifest path must be absolute: {raw_path}")
    path = unresolved.resolve(strict=False)
    if not path.is_file():
        raise CreoMcpError(f"UDF manifest does not exist: {path}")
    if path.suffix.casefold() != ".json":
        raise CreoMcpError(f"UDF manifest must be a .json file: {path}")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_udf_manifest(raw_manifest_path: str) -> QualifiedUdf:
    """Parse a strict manifest, resolve its asset, and verify the complete file hash."""
    manifest_path = _resolve_manifest_path(raw_manifest_path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CreoMcpError(f"Could not parse UDF manifest {manifest_path}: {exc}") from exc
    try:
        manifest = UdfManifest.model_validate(payload)
    except ValidationError as exc:
        raise CreoMcpError(f"Invalid UDF manifest {manifest_path}: {exc}") from exc

    unresolved_udf = Path(manifest.udf_file).expanduser()
    if not unresolved_udf.is_absolute():
        unresolved_udf = manifest_path.parent / unresolved_udf
    udf_path = unresolved_udf.resolve(strict=False)
    if not udf_path.is_file():
        raise CreoMcpError(f"Qualified UDF file does not exist: {udf_path}")
    if udf_path.suffix.casefold() != ".gph":
        raise CreoMcpError(f"Qualified UDF file must use the .gph extension: {udf_path}")
    if udf_path.stem.casefold() != manifest.udf_name.casefold():
        raise CreoMcpError(
            "UDF manifest udf_name must match the qualified .gph file name."
        )
    file_size = udf_path.stat().st_size
    if file_size <= 0:
        raise CreoMcpError(f"Qualified UDF file is empty: {udf_path}")

    actual_sha256 = _file_sha256(udf_path)
    if actual_sha256.casefold() != manifest.sha256.casefold():
        raise CreoMcpError(
            f"UDF SHA-256 mismatch for {udf_path}: expected {manifest.sha256.casefold()}, "
            f"got {actual_sha256}."
        )

    identity_payload = {
        "manifest_path": str(manifest_path),
        "udf_path": str(udf_path),
        "manifest": manifest.model_dump(mode="json"),
        "actual_sha256": actual_sha256,
    }
    validation_id = hashlib.sha256(
        json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return QualifiedUdf(
        manifest_path=manifest_path,
        udf_path=udf_path,
        file_size_bytes=file_size,
        sha256=actual_sha256,
        validation_id=validation_id,
        manifest=manifest,
    )


def load_qualified_udf(raw_manifest_path: str) -> QualifiedUdf:
    """Load a hash-verified UDF that has completed non-interactive qualification."""
    qualified = load_udf_manifest(raw_manifest_path)
    if not qualified.manifest.verified_non_interactive:
        raise CreoMcpError(
            "UDF manifest is still a qualification candidate; "
            "verified_non_interactive must be true for production placement."
        )
    return qualified


def validate_udf_manifest(
    request: ValidateUdfManifestRequest,
) -> UdfManifestValidationResult:
    """Validate one UDF qualification record without connecting to Creo."""
    return load_udf_manifest(request.manifest_path).to_result()
