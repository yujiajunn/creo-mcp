"""Validation and format detection for CAD files imported through Creo."""

from __future__ import annotations

from pathlib import Path

from .errors import CreoMcpError
from .models import CadSourceInfo, ImportCadRequest, ResolvedCadImportFormat


MAX_CAD_FILE_BYTES = 2_000_000_000

# Values are from Creo 13.4's EpfcNewModelImportType type library.
CAD_IMPORT_TYPE: dict[ResolvedCadImportFormat, int] = {
    "iges": 0,
    "vda": 1,
    "creo_neutral": 2,
    "step": 4,
    "stl": 5,
    "acis": 11,
    "parasolid": 12,
    "catia_part": 14,
    "catia_product": 15,
    "nx": 16,
    "jt": 19,
    "solidworks_part": 20,
    "solidworks_assembly": 21,
    "inventor_part": 22,
    "inventor_assembly": 23,
}

CAD_FORMAT_EXTENSIONS: dict[ResolvedCadImportFormat, frozenset[str]] = {
    "step": frozenset({".stp", ".step"}),
    "iges": frozenset({".igs", ".iges"}),
    "parasolid": frozenset({".x_t", ".x_b"}),
    "acis": frozenset({".sat", ".sab"}),
    "stl": frozenset({".stl"}),
    "jt": frozenset({".jt"}),
    "catia_part": frozenset({".catpart"}),
    "catia_product": frozenset({".catproduct"}),
    "solidworks_part": frozenset({".sldprt"}),
    "solidworks_assembly": frozenset({".sldasm"}),
    "inventor_part": frozenset({".ipt"}),
    "inventor_assembly": frozenset({".iam"}),
    "nx": frozenset({".prt"}),
    "vda": frozenset({".vda"}),
    "creo_neutral": frozenset({".neu"}),
}

GEOMETRY_REPRESENTATION = {
    "stl": "mesh",
    "jt": "tessellated_or_brep",
}


def _format_from_extension(path: Path) -> ResolvedCadImportFormat:
    extension = path.suffix.casefold()
    if extension == ".prt":
        raise CreoMcpError(
            ".prt is ambiguous: use the native Creo part tool for a Creo file, or set "
            "source_format='nx' for an NX part."
        )
    for source_format, extensions in CAD_FORMAT_EXTENSIONS.items():
        if source_format != "nx" and extension in extensions:
            return source_format
    supported = sorted(
        extension
        for source_format, extensions in CAD_FORMAT_EXTENSIONS.items()
        if source_format != "nx"
        for extension in extensions
    )
    raise CreoMcpError(
        f"Cannot infer a supported CAD format from {path.name!r}. "
        f"Supported automatic extensions: {', '.join(supported)}."
    )


def resolve_cad_source(request: ImportCadRequest) -> CadSourceInfo:
    """Resolve one import source and validate its format without starting Creo."""
    unresolved = Path(request.source_path).expanduser()
    if not unresolved.is_absolute():
        raise CreoMcpError(f"CAD source path must be absolute: {request.source_path}")
    path = unresolved.resolve(strict=False)
    if not path.is_file():
        raise CreoMcpError(f"CAD source file does not exist: {path}")
    file_size = path.stat().st_size
    if file_size > MAX_CAD_FILE_BYTES:
        raise CreoMcpError(
            f"CAD source exceeds the {MAX_CAD_FILE_BYTES}-byte import limit: {path}"
        )

    if request.source_format == "auto":
        source_format = _format_from_extension(path)
    else:
        source_format = request.source_format
        allowed_extensions = CAD_FORMAT_EXTENSIONS[source_format]
        if path.suffix.casefold() not in allowed_extensions:
            raise CreoMcpError(
                f"Format {source_format!r} expects one of "
                f"{sorted(allowed_extensions)}, not {path.suffix or '<no extension>'!r}."
            )

    representation = GEOMETRY_REPRESENTATION.get(source_format, "brep")
    return CadSourceInfo(
        source_path=str(path),
        file_name=path.name,
        file_size_bytes=file_size,
        source_format=source_format,
        geometry_representation=representation,
    )
