"""Stable, JSON-schema-backed models exposed by the MCP tools."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic import model_validator


class CreoModelInfo(BaseModel):
    """Read-only metadata for the active Creo model."""

    model_config = ConfigDict(extra="forbid")

    file_name: str = Field(description="Creo file name, including the extension.")
    instance_name: str | None = Field(
        default=None,
        description="Creo instance name when one is available.",
    )
    model_type: str | None = Field(
        default=None,
        description="Creo model type returned by the official API.",
    )


class CreoStatus(BaseModel):
    """Result of a verified, read-only connection to Creo."""

    model_config = ConfigDict(extra="forbid")

    connected: bool = Field(description="Whether the API verified a live Creo connection.")
    creo_loadpoint: str = Field(description="Root path of the detected Creo installation.")
    working_directory: str = Field(description="Current Creo working directory.")
    active_model: CreoModelInfo | None = Field(
        default=None,
        description="Active model metadata, or null when no model is active.",
    )


class BoxDimensions(BaseModel):
    """Length, width, and height in millimetres."""

    model_config = ConfigDict(extra="forbid")

    length_mm: float = Field(gt=0, le=100000, description="Overall length in millimetres.")
    width_mm: float = Field(gt=0, le=100000, description="Overall width in millimetres.")
    height_mm: float = Field(gt=0, le=100000, description="Overall height in millimetres.")


class PackagingBoxPlanRequest(BoxDimensions):
    """Validated inputs for the first packaging-box modelling workflow."""

    model_config = ConfigDict(extra="forbid")

    part_name: str = Field(
        default="packaging_box",
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
        description="Target Creo part name without an extension.",
    )
    output_directory: str | None = Field(
        default=None,
        description="Optional target directory. Omit to use Creo's current working directory.",
    )
    wall_thickness_mm: float | None = Field(
        default=None,
        gt=0,
        description="Requested wall thickness. This is validated now and modelled in a later step.",
    )

    @model_validator(mode="after")
    def validate_wall_thickness(self) -> "PackagingBoxPlanRequest":
        if self.wall_thickness_mm is not None and self.wall_thickness_mm * 2 >= min(
            self.length_mm,
            self.width_mm,
            self.height_mm,
        ):
            raise ValueError("wall_thickness_mm must be less than half of every box dimension.")
        return self


class PackagingBoxPlan(BaseModel):
    """A dry-run model plan that never changes Creo."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(description="Stable identifier derived from the validated request.")
    execution_state: Literal["dry_run_only"] = Field(
        description="This plan has not created or changed a Creo model."
    )
    model_mode: Literal["solid_envelope"] = Field(
        description="The first production model will be a coarse solid bounding envelope."
    )
    part_name: str = Field(description="Planned Creo part name without an extension.")
    expected_model_file: str = Field(description="Expected Creo part file name.")
    output_directory: str | None = Field(
        description="Requested output directory, or null for Creo's current working directory."
    )
    dimensions_mm: BoxDimensions = Field(
        description="Validated bounding dimensions in millimetres."
    )
    wall_thickness_mm: float | None = Field(
        description="Validated requested wall thickness, not yet applied to geometry."
    )
    operations: list[str] = Field(
        description="Ordered proposed operations for the future write-enabled tool."
    )
    requires_user_confirmation: bool = Field(
        description="Whether a later write-enabled execution requires explicit confirmation."
    )


class CreatePartRequest(BaseModel):
    """Explicit request for a new unsaved Creo part."""

    model_config = ConfigDict(extra="forbid")

    part_name: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
        description="New Creo part name without an extension.",
    )
    output_directory: str | None = Field(
        default=None,
        description=(
            "Optional existing working directory to switch Creo to before creating the part."
        ),
    )
    activate: bool = Field(
        default=True,
        description="Whether to open and activate the new part window.",
    )


class CreatePartResult(BaseModel):
    """Result of creating an unsaved Creo part in the active session."""

    model_config = ConfigDict(extra="forbid")

    created_model: CreoModelInfo = Field(description="Metadata for the new unsaved Creo part.")
    working_directory: str = Field(description="Creo working directory used for the new part.")
    saved: Literal[False] = Field(description="The tool never saves a model to disk.")
    activated: bool = Field(description="Whether the new part window was activated.")


class InspectPartRequest(BaseModel):
    """Request to inspect a native Creo part without changing or saving it."""

    model_config = ConfigDict(extra="forbid")

    part_path: str = Field(description="Absolute path to an existing native Creo .prt file.")


class CreoDimensionInfo(BaseModel):
    """One driving dimension exposed by a native Creo part."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(description="Creo dimension symbol, for example BOX_LENGTH or d0.")
    value: float = Field(description="Current dimension value in the part's principal length unit.")
    dimension_type: str | None = Field(
        default=None,
        description="Raw dimension type returned by the official API.",
    )
    item_id: int | None = Field(
        default=None,
        description="Creo model-item identifier when the API exposes one.",
    )


class InspectPartResult(BaseModel):
    """Native part metadata needed to qualify it as a modelling template."""

    model_config = ConfigDict(extra="forbid")

    model: CreoModelInfo = Field(description="Metadata for the inspected native part.")
    part_path: str = Field(description="Resolved source path used to retrieve the part.")
    length_unit: str = Field(description="Principal length-unit name reported by Creo.")
    dimensions: list[CreoDimensionInfo] = Field(
        description="Driving dimensions available for template mapping."
    )


class BoxDimensionSymbols(BaseModel):
    """Dimension symbols expected in the native packaging-box template."""

    model_config = ConfigDict(extra="forbid")

    length: str = Field(default="BOX_LENGTH", min_length=1, max_length=32)
    width: str = Field(default="BOX_WIDTH", min_length=1, max_length=32)
    height: str = Field(default="BOX_HEIGHT", min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_unique_symbols(self) -> "BoxDimensionSymbols":
        normalized = {self.length.upper(), self.width.upper(), self.height.upper()}
        if len(normalized) != 3:
            raise ValueError("length, width, and height dimension symbols must be distinct.")
        return self


class CreatePackagingBoxRequest(BoxDimensions):
    """Confirmed request to create a new native part from a qualified template."""

    model_config = ConfigDict(extra="forbid")

    part_name: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
        description="New Creo part name without an extension.",
    )
    output_directory: str | None = Field(
        default=None,
        description="Existing output directory, or null to use Creo's current working directory.",
    )
    template_path: str = Field(
        description="Absolute path to a native Creo .prt template with named driving dimensions."
    )
    dimension_symbols: BoxDimensionSymbols = Field(
        default_factory=BoxDimensionSymbols,
        description="Template dimension symbols mapped to requested box dimensions.",
    )
    wall_thickness_mm: float | None = Field(
        default=None,
        gt=0,
        description="Reserved for a later hollow-box workflow; must currently be null.",
    )
    plan_id: str = Field(
        min_length=16,
        max_length=16,
        pattern=r"^[0-9a-f]{16}$",
        description="Plan identifier returned by creo_plan_packaging_box for the same inputs.",
    )
    confirmation: Literal["create_new_model"] = Field(
        description="Explicit acknowledgement that this operation creates and saves a new file."
    )
    activate: bool = Field(
        default=True,
        description="Whether to activate the created model window.",
    )

    @model_validator(mode="after")
    def validate_confirmed_plan(self) -> "CreatePackagingBoxRequest":
        if self.wall_thickness_mm is not None:
            raise ValueError(
                "wall_thickness_mm is not supported by the solid-envelope creator yet; use null."
            )
        planned = create_packaging_box_plan(
            PackagingBoxPlanRequest(
                length_mm=self.length_mm,
                width_mm=self.width_mm,
                height_mm=self.height_mm,
                part_name=self.part_name,
                output_directory=self.output_directory,
                wall_thickness_mm=self.wall_thickness_mm,
            )
        )
        if self.plan_id != planned.plan_id:
            raise ValueError(
                "plan_id does not match the requested name, dimensions, and output directory."
            )
        return self


class BoxExtent(BaseModel):
    """Axis-aligned Creo model extent in millimetres."""

    model_config = ConfigDict(extra="forbid")

    x_mm: float
    y_mm: float
    z_mm: float


class CreatePackagingBoxResult(BaseModel):
    """Result of creating and saving a new native packaging-box envelope."""

    model_config = ConfigDict(extra="forbid")

    created_model: CreoModelInfo
    source_template: str
    model_path: str
    working_directory: str
    saved: Literal[True]
    activated: bool
    applied_dimensions_mm: BoxDimensions
    measured_extent_mm: BoxExtent


class InspectStepRequest(BaseModel):
    """Request to inspect a local STEP file without starting Creo."""

    model_config = ConfigDict(extra="forbid")

    step_path: str = Field(description="Absolute path to an existing .stp or .step file.")


class StepFileInfo(BaseModel):
    """Deterministic metadata parsed from an ISO-10303-21 STEP exchange file."""

    model_config = ConfigDict(extra="forbid")

    step_path: str
    file_size_bytes: int = Field(ge=0)
    file_name: str | None
    timestamp: str | None
    schema_name: str | None
    originating_system: str | None
    product_names: list[str]
    solid_body_count: int = Field(ge=0)
    advanced_face_count: int = Field(ge=0)
    closed_shell_count: int = Field(ge=0)
    assembly_relationship_count: int = Field(ge=0)
    declared_length_unit: str
    inferred_model_kind: Literal["part", "assembly"]


CadImportFormat = Literal[
    "auto",
    "step",
    "iges",
    "parasolid",
    "acis",
    "stl",
    "jt",
    "catia_part",
    "catia_product",
    "solidworks_part",
    "solidworks_assembly",
    "inventor_part",
    "inventor_assembly",
    "nx",
    "vda",
    "creo_neutral",
]
ResolvedCadImportFormat = Literal[
    "step",
    "iges",
    "parasolid",
    "acis",
    "stl",
    "jt",
    "catia_part",
    "catia_product",
    "solidworks_part",
    "solidworks_assembly",
    "inventor_part",
    "inventor_assembly",
    "nx",
    "vda",
    "creo_neutral",
]


class ImportCadRequest(BaseModel):
    """Request to import a supported CAD file as a new, unsaved Creo model."""

    model_config = ConfigDict(extra="forbid")

    source_path: str = Field(description="Absolute path to an existing supported CAD file.")
    source_format: CadImportFormat = Field(
        default="auto",
        description=(
            "CAD format, or auto to infer it from an unambiguous file extension. "
            "Use nx explicitly for NX .prt files."
        ),
    )
    model_name: str = Field(
        min_length=1,
        max_length=31,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
        description="New Creo model name without an extension.",
    )
    activate: bool = Field(
        default=True,
        description="Whether to activate the imported model window.",
    )
    confirmation: Literal["import_unsaved_model"] = Field(
        description="Explicit acknowledgement that Creo session state will change."
    )


class CadSourceInfo(BaseModel):
    """Validated source metadata for one supported CAD import."""

    model_config = ConfigDict(extra="forbid")

    source_path: str
    file_name: str
    file_size_bytes: int = Field(ge=0)
    source_format: ResolvedCadImportFormat
    geometry_representation: Literal["brep", "mesh", "tessellated_or_brep"]


class ImportCadResult(BaseModel):
    """Result of importing a supported CAD file without saving the Creo model."""

    model_config = ConfigDict(extra="forbid")

    source: CadSourceInfo
    created_model: CreoModelInfo
    detected_model_kind: Literal["part", "assembly", "other"]
    working_directory: str
    length_unit: str
    measured_extent_mm: BoxExtent
    volume_mm3: float | None = Field(
        default=None,
        ge=0,
        description="Solid volume in cubic millimetres when Creo can evaluate it.",
    )
    saved: Literal[False]
    activated: bool


class OpenNativeModelRequest(BaseModel):
    """Request to load and optionally activate an existing native Creo model."""

    model_config = ConfigDict(extra="forbid")

    model_path: str = Field(
        description="Absolute path to an existing native Creo .prt or .asm file."
    )
    activate: bool = Field(
        default=True,
        description="Whether to create and activate a window for the loaded model.",
    )
    confirmation: Literal["open_native_model"] = Field(
        description="Explicit acknowledgement that Creo session state will change."
    )


class OpenNativeModelResult(BaseModel):
    """Measured result for a native model loaded without changing its source file."""

    model_config = ConfigDict(extra="forbid")

    source_path: str
    model: CreoModelInfo
    model_kind: Literal["part", "assembly"]
    length_unit: str
    measured_extent_mm: BoxExtent
    volume_mm3: float | None = Field(default=None, ge=0)
    source_modified: Literal[False]
    activated: bool


class ImportStepRequest(BaseModel):
    """Request to import a STEP file as a new, unsaved Creo model."""

    model_config = ConfigDict(extra="forbid")

    step_path: str = Field(description="Absolute path to an existing .stp or .step file.")
    model_name: str = Field(
        min_length=1,
        max_length=31,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
        description="New Creo model name without an extension.",
    )
    activate: bool = Field(
        default=True,
        description="Whether to activate the imported model window.",
    )
    confirmation: Literal["import_unsaved_model"] = Field(
        description="Explicit acknowledgement that Creo session state will change."
    )


class ImportStepResult(BaseModel):
    """Result of importing a STEP file into the active Creo session without saving it."""

    model_config = ConfigDict(extra="forbid")

    source: StepFileInfo
    created_model: CreoModelInfo
    detected_model_kind: Literal["part", "assembly", "other"]
    working_directory: str
    length_unit: str
    measured_extent_mm: BoxExtent
    saved: Literal[False]
    activated: bool


class InspectModelGeometryRequest(BaseModel):
    """Request geometry for the active model or one named model already in session."""

    model_config = ConfigDict(extra="forbid")

    model_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=31,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
        description="Loaded Creo model name without extension, or null for the active model.",
    )
    model_kind: Literal["part", "assembly"] | None = Field(
        default=None,
        description="Required with model_name; omit when inspecting the active model.",
    )

    @model_validator(mode="after")
    def validate_model_lookup(self) -> "InspectModelGeometryRequest":
        if (self.model_name is None) != (self.model_kind is None):
            raise ValueError("model_name and model_kind must either both be set or both be null.")
        return self


class ModelGeometryResult(BaseModel):
    """Measured geometry for one loaded Creo part or assembly."""

    model_config = ConfigDict(extra="forbid")

    model: CreoModelInfo
    model_kind: Literal["part", "assembly", "other"]
    length_unit: str
    measured_extent_mm: BoxExtent
    volume_mm3: float | None = Field(
        default=None,
        ge=0,
        description="Solid volume converted to cubic millimetres when Creo can evaluate it.",
    )


class Point3D(BaseModel):
    """One model-space point normalized to millimetres."""

    model_config = ConfigDict(extra="forbid")

    x_mm: float
    y_mm: float
    z_mm: float


class GeometryBounds(BaseModel):
    """Axis-aligned model-space bounds normalized to millimetres."""

    model_config = ConfigDict(extra="forbid")

    minimum: Point3D
    maximum: Point3D


class InspectGeometryDetailsRequest(InspectModelGeometryRequest):
    """Request detailed geometry for explicit surface and edge IDs."""

    model_config = ConfigDict(extra="forbid")

    surface_ids: list[int] = Field(default_factory=list, max_length=200)
    edge_ids: list[int] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_geometry_ids(self) -> "InspectGeometryDetailsRequest":
        if not self.surface_ids and not self.edge_ids:
            raise ValueError("At least one surface ID or edge ID is required.")
        for category, item_ids in (
            ("surface_ids", self.surface_ids),
            ("edge_ids", self.edge_ids),
        ):
            if any(item_id < 0 for item_id in item_ids):
                raise ValueError(f"{category} must contain only non-negative IDs.")
            if len(item_ids) != len(set(item_ids)):
                raise ValueError(f"{category} must not contain duplicate IDs.")
        return self


class InspectModelStructureRequest(InspectModelGeometryRequest):
    """Request a bounded structural inventory of one loaded Creo model."""

    model_config = ConfigDict(extra="forbid")

    max_items_per_category: int = Field(
        default=500,
        ge=1,
        le=5000,
        description="Maximum returned items in each category.",
    )


class CreoFeatureInfo(BaseModel):
    """Stable reference metadata for one Creo model-tree feature."""

    model_config = ConfigDict(extra="forbid")

    item_id: int
    name: str | None
    feature_type: int | None
    feature_type_name: str | None
    status: int | None
    status_name: str | None


class SurfaceGeometryInfo(BaseModel):
    """Measured, reference-ready metadata for one Creo surface."""

    model_config = ConfigDict(extra="forbid")

    item_id: int
    name: str | None
    surface_type: int | None
    surface_type_name: str | None
    visible: bool | None
    area_mm2: float | None = Field(default=None, ge=0)
    bounds_mm: GeometryBounds | None
    feature: CreoFeatureInfo | None
    evaluation_errors: list[str]


class EdgeGeometryInfo(BaseModel):
    """Measured, reference-ready metadata for one Creo edge."""

    model_config = ConfigDict(extra="forbid")

    item_id: int
    name: str | None
    curve_type: int | None
    curve_type_name: str | None
    visible: bool | None
    length_mm: float | None = Field(default=None, ge=0)
    start_point_mm: Point3D | None
    midpoint_mm: Point3D | None
    end_point_mm: Point3D | None
    adjacent_surface_ids: list[int]
    feature: CreoFeatureInfo | None
    evaluation_errors: list[str]


class GeometryDetailsResult(BaseModel):
    """Detailed surface and edge geometry for one loaded Creo model."""

    model_config = ConfigDict(extra="forbid")

    model: CreoModelInfo
    model_kind: Literal["part", "assembly", "other"]
    length_unit: str
    surfaces: list[SurfaceGeometryInfo]
    edges: list[EdgeGeometryInfo]


SurfaceTypeName = Literal[
    "plane",
    "cylinder",
    "cone",
    "torus",
    "ruled",
    "revolved",
    "tabulated_cylinder",
    "fillet",
    "coons_patch",
    "spline",
    "nurbs",
    "cylindrical_spline",
    "spherical_spline",
    "foreign",
    "spline_second_derivative",
]


class FindSurfacesRequest(InspectModelGeometryRequest):
    """Search all surfaces in one loaded model using normalized geometry filters."""

    model_config = ConfigDict(extra="forbid")

    surface_types: list[SurfaceTypeName] = Field(default_factory=list, max_length=15)
    minimum_area_mm2: float | None = Field(default=None, ge=0)
    maximum_area_mm2: float | None = Field(default=None, ge=0)
    constant_axis: Literal["x", "y", "z"] | None = None
    axis_coordinate_mm: float | None = None
    axis_tolerance_mm: float = Field(default=0.01, gt=0, le=1000)
    sort_by: Literal["area_desc", "area_asc", "item_id"] = "area_desc"
    max_results: int = Field(default=20, ge=1, le=200)

    @model_validator(mode="after")
    def validate_surface_filters(self) -> "FindSurfacesRequest":
        if len(self.surface_types) != len(set(self.surface_types)):
            raise ValueError("surface_types must not contain duplicates.")
        if (
            self.minimum_area_mm2 is not None
            and self.maximum_area_mm2 is not None
            and self.minimum_area_mm2 > self.maximum_area_mm2
        ):
            raise ValueError("minimum_area_mm2 must not exceed maximum_area_mm2.")
        if (self.constant_axis is None) != (self.axis_coordinate_mm is None):
            raise ValueError(
                "constant_axis and axis_coordinate_mm must either both be set or both be null."
            )
        if self.axis_coordinate_mm is not None and not math.isfinite(
            self.axis_coordinate_mm
        ):
            raise ValueError("axis_coordinate_mm must be finite.")
        return self


class FindSurfacesResult(BaseModel):
    """Bounded, sorted surface candidates from a complete model scan."""

    model_config = ConfigDict(extra="forbid")

    model: CreoModelInfo
    model_kind: Literal["part", "assembly", "other"]
    length_unit: str
    scanned_surface_count: int = Field(ge=0)
    matched_surface_count: int = Field(ge=0)
    truncated: bool
    surfaces: list[SurfaceGeometryInfo]


class CreoModelItemInfo(BaseModel):
    """Stable reference metadata for one datum or coordinate-system item."""

    model_config = ConfigDict(extra="forbid")

    item_id: int
    name: str | None
    item_type: Literal["surface", "edge", "axis", "point", "coordinate_system"]


class CreoParameterInfo(BaseModel):
    """One model-level Creo parameter with a JSON-safe value."""

    model_config = ConfigDict(extra="forbid")

    name: str
    value_type: Literal["string", "integer", "boolean", "double", "note", "unset"]
    value: str | int | float | bool | None
    units: str | None = None


class ModelStructureResult(BaseModel):
    """Bounded structural inventory used to form stable later operation references."""

    model_config = ConfigDict(extra="forbid")

    model: CreoModelInfo
    model_kind: Literal["part", "assembly", "other"]
    features: list[CreoFeatureInfo]
    surfaces: list[CreoModelItemInfo]
    edges: list[CreoModelItemInfo]
    axes: list[CreoModelItemInfo]
    points: list[CreoModelItemInfo]
    coordinate_systems: list[CreoModelItemInfo]
    dimensions: list[CreoDimensionInfo]
    parameters: list[CreoParameterInfo]
    skipped_unnamed_parameter_count: int = Field(
        ge=0,
        description="Parameters omitted because Creo exposed no usable name.",
    )
    truncated_categories: list[
        Literal[
            "features",
            "surfaces",
            "edges",
            "axes",
            "points",
            "coordinate_systems",
            "dimensions",
            "parameters",
        ]
    ]


UdfReferenceItemType = Literal[
    "feature",
    "surface",
    "edge",
    "coordinate_system",
    "axis",
    "point",
]


class UdfReferenceSpec(BaseModel):
    """One mandatory reference prompt declared by a qualified UDF."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=128)
    item_type: UdfReferenceItemType


class UdfVariantDimensionSpec(BaseModel):
    """One bounded variable dimension declared by a qualified UDF."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    unit: Literal["mm", "degree", "unitless"]
    minimum: float
    maximum: float

    @model_validator(mode="after")
    def validate_range(self) -> "UdfVariantDimensionSpec":
        if not math.isfinite(self.minimum) or not math.isfinite(self.maximum):
            raise ValueError("UDF dimension limits must be finite.")
        if self.minimum > self.maximum:
            raise ValueError("UDF dimension minimum must not exceed maximum.")
        return self


class UdfManifest(BaseModel):
    """Strict qualification record for one non-interactive Creo UDF asset."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    udf_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$",
    )
    udf_file: str = Field(
        min_length=1,
        description="Absolute path or path relative to the manifest file.",
    )
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    qualified_creo_versions: list[str] = Field(min_length=1, max_length=32)
    allowed_model_kinds: list[Literal["part", "assembly"]] = Field(
        min_length=1,
        max_length=2,
    )
    verified_non_interactive: bool = Field(
        description=(
            "False for a qualification candidate; true only after a supervised live "
            "qualification run has completed without Creo interaction."
        )
    )
    references: list[UdfReferenceSpec] = Field(default_factory=list, max_length=64)
    variant_dimensions: list[UdfVariantDimensionSpec] = Field(
        default_factory=list,
        max_length=128,
    )

    @model_validator(mode="after")
    def validate_unique_contract(self) -> "UdfManifest":
        versions = [version.strip().casefold() for version in self.qualified_creo_versions]
        if any(not version for version in versions):
            raise ValueError("qualified_creo_versions must not contain blank values.")
        if len(versions) != len(set(versions)):
            raise ValueError("qualified_creo_versions must be unique.")
        if len(self.allowed_model_kinds) != len(set(self.allowed_model_kinds)):
            raise ValueError("allowed_model_kinds must be unique.")
        prompts = [reference.prompt.casefold() for reference in self.references]
        if len(prompts) != len(set(prompts)):
            raise ValueError("UDF reference prompts must be unique.")
        names = [dimension.name.casefold() for dimension in self.variant_dimensions]
        if len(names) != len(set(names)):
            raise ValueError("UDF variant dimension names must be unique.")
        return self


class ValidateUdfManifestRequest(BaseModel):
    """Request offline validation of one UDF qualification manifest."""

    model_config = ConfigDict(extra="forbid")

    manifest_path: str = Field(description="Absolute path to a UDF manifest JSON file.")


class UdfManifestValidationResult(BaseModel):
    """Resolved, hash-verified UDF qualification data."""

    model_config = ConfigDict(extra="forbid")

    manifest_path: str
    udf_path: str
    file_size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest: UdfManifest


class UdfReferenceBinding(BaseModel):
    """Bind one manifest prompt to a stable item ID in the target model."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=128)
    item_type: UdfReferenceItemType
    item_id: int = Field(ge=0)


class UdfVariantDimensionBinding(BaseModel):
    """Bind one declared UDF variable dimension to a value in manifest units."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    value: float

    @model_validator(mode="after")
    def validate_finite_value(self) -> "UdfVariantDimensionBinding":
        if not math.isfinite(self.value):
            raise ValueError("UDF variant dimension values must be finite.")
        return self


class PlaceUdfRequest(InspectModelGeometryRequest):
    """Confirmed request to place one fully bound, qualified UDF."""

    model_config = ConfigDict(extra="forbid")

    manifest_path: str = Field(description="Absolute path to the validated manifest.")
    validation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    references: list[UdfReferenceBinding] = Field(default_factory=list, max_length=64)
    variant_dimensions: list[UdfVariantDimensionBinding] = Field(
        default_factory=list,
        max_length=128,
    )
    confirmation: Literal["place_qualified_udf"]

    @model_validator(mode="after")
    def validate_unique_bindings(self) -> "PlaceUdfRequest":
        prompts = [reference.prompt.casefold() for reference in self.references]
        if len(prompts) != len(set(prompts)):
            raise ValueError("UDF reference bindings must use unique prompts.")
        names = [dimension.name.casefold() for dimension in self.variant_dimensions]
        if len(names) != len(set(names)):
            raise ValueError("UDF dimension bindings must use unique names.")
        return self


class RunUdfQualificationRequest(PlaceUdfRequest):
    """Confirmed live run for an unqualified UDF candidate."""

    confirmation: Literal["run_udf_qualification"]


class PlaceUdfResult(BaseModel):
    """Result of placing and regenerating one qualified UDF without saving."""

    model_config = ConfigDict(extra="forbid")

    target_model: CreoModelInfo
    udf_name: str
    group_name: str | None
    group_members: list[CreoFeatureInfo]
    validation_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    length_unit: str
    measured_extent_mm: BoxExtent
    volume_mm3: float | None = Field(default=None, ge=0)
    saved: Literal[False]


class UdfQualificationRunResult(PlaceUdfResult):
    """Observed result of a candidate run that does not itself grant qualification."""

    qualification_state: Literal["candidate_run_completed"]
    manifest_verified_non_interactive: Literal[False]


class ModelItemRename(BaseModel):
    """Rename one stable model item for an understandable model tree."""

    model_config = ConfigDict(extra="forbid")

    item_type: UdfReferenceItemType
    item_id: int = Field(ge=0)
    new_name: str = Field(
        min_length=1,
        max_length=31,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
    )


class DimensionUpdate(BaseModel):
    """Update one named driving dimension in an explicit unit."""

    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=64)
    value: float
    unit: Literal["mm", "degree", "unitless"] = "mm"

    @model_validator(mode="after")
    def validate_finite_value(self) -> "DimensionUpdate":
        if not math.isfinite(self.value):
            raise ValueError("Dimension update values must be finite.")
        return self


class ParameterUpdate(BaseModel):
    """Create or update one model parameter with an explicit value type."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        min_length=1,
        max_length=31,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
    )
    value_type: Literal["string", "integer", "double", "boolean"]
    value: str | int | float | bool

    @model_validator(mode="after")
    def validate_typed_value(self) -> "ParameterUpdate":
        valid = {
            "string": isinstance(self.value, str),
            "integer": isinstance(self.value, int) and not isinstance(self.value, bool),
            "double": (
                isinstance(self.value, (int, float))
                and not isinstance(self.value, bool)
                and math.isfinite(float(self.value))
            ),
            "boolean": isinstance(self.value, bool),
        }
        if not valid[self.value_type]:
            raise ValueError(
                f"Parameter {self.name!r} value does not match {self.value_type!r}."
            )
        return self


class UpdateModelMetadataRequest(InspectModelGeometryRequest):
    """Confirmed maintainability updates to one loaded model."""

    model_config = ConfigDict(extra="forbid")

    renames: list[ModelItemRename] = Field(default_factory=list, max_length=256)
    dimensions: list[DimensionUpdate] = Field(default_factory=list, max_length=256)
    parameters: list[ParameterUpdate] = Field(default_factory=list, max_length=256)
    confirmation: Literal["update_model_metadata"]

    @model_validator(mode="after")
    def validate_unique_updates(self) -> "UpdateModelMetadataRequest":
        if not self.renames and not self.dimensions and not self.parameters:
            raise ValueError("At least one rename, dimension, or parameter update is required.")
        item_keys = [(item.item_type, item.item_id) for item in self.renames]
        if len(item_keys) != len(set(item_keys)):
            raise ValueError("Model item rename targets must be unique.")
        rename_names = [
            (item.item_type, item.new_name.casefold()) for item in self.renames
        ]
        if len(rename_names) != len(set(rename_names)):
            raise ValueError("New model item names must be unique within each item type.")
        symbols = [item.symbol.casefold() for item in self.dimensions]
        if len(symbols) != len(set(symbols)):
            raise ValueError("Dimension update symbols must be unique.")
        names = [item.name.casefold() for item in self.parameters]
        if len(names) != len(set(names)):
            raise ValueError("Parameter update names must be unique.")
        return self


class AppliedModelItemRename(ModelItemRename):
    previous_name: str | None


class AppliedDimensionUpdate(DimensionUpdate):
    previous_value_native: float
    applied_value_native: float


class AppliedParameterUpdate(ParameterUpdate):
    created: bool


class UpdateModelMetadataResult(BaseModel):
    """Verified result of model maintainability edits without saving."""

    model_config = ConfigDict(extra="forbid")

    model: CreoModelInfo
    renames: list[AppliedModelItemRename]
    dimensions: list[AppliedDimensionUpdate]
    parameters: list[AppliedParameterUpdate]
    regenerated: Literal[True]
    saved: Literal[False]


class HandoffReportRequest(InspectModelGeometryRequest):
    """Request an engineering handoff assessment for one loaded model."""

    model_config = ConfigDict(extra="forbid")

    max_reported_items: int = Field(default=100, ge=1, le=1000)


class HandoffReportResult(BaseModel):
    """Read-only maintainability and model-health report."""

    model_config = ConfigDict(extra="forbid")

    model: CreoModelInfo
    model_kind: Literal["part", "assembly", "other"]
    length_unit: str
    measured_extent_mm: BoxExtent
    feature_count: int = Field(ge=0)
    active_feature_count: int = Field(ge=0)
    non_active_features: list[CreoFeatureInfo]
    unnamed_features: list[CreoFeatureInfo]
    imported_features: list[CreoFeatureInfo]
    named_dimension_count: int = Field(ge=0)
    parameter_count: int = Field(ge=0)
    automation_parameters: list[CreoParameterInfo]
    warnings: list[str]
    handoff_state: Literal["ready", "review_required"]
    saved: Literal[False]


class SaveModelAsRequest(InspectModelGeometryRequest):
    """Copy one loaded model to a new native Creo name without overwriting."""

    model_config = ConfigDict(extra="forbid")

    target_name: str = Field(
        min_length=1,
        max_length=31,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
    )
    output_directory: str = Field(description="Absolute existing output directory.")
    activate_copy: bool = True
    confirmation: Literal["save_model_as"]


class SaveModelAsResult(BaseModel):
    """Result of a protected native Creo copy operation."""

    model_config = ConfigDict(extra="forbid")

    source_model: CreoModelInfo
    copied_model: CreoModelInfo
    saved_path: str
    activated: bool
    source_modified: Literal[False]
    saved: Literal[True]


class ExportModelRequest(InspectModelGeometryRequest):
    """Export one loaded solid to a new neutral CAD file."""

    model_config = ConfigDict(extra="forbid")

    output_path: str = Field(description="Absolute non-existing output file path.")
    export_format: Literal["step", "iges", "parasolid"]
    profile_path: str | None = Field(
        default=None,
        description="Optional absolute Creo export profile supported by ExportIntf3D.",
    )
    confirmation: Literal["export_model"]


class ExportModelResult(BaseModel):
    """Result of a guarded neutral CAD export."""

    model_config = ConfigDict(extra="forbid")

    source_model: CreoModelInfo
    export_format: Literal["step", "iges", "parasolid"]
    output_path: str
    file_size_bytes: int = Field(gt=0)
    source_modified: Literal[False]
    saved: Literal[False]


class OperationLogRecord(BaseModel):
    """One privacy-minimized local operation audit record."""

    model_config = ConfigDict(extra="forbid")

    timestamp_utc: str
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    server_version: str
    operation: str
    duration_ms: int = Field(ge=0)
    success: bool
    error_type: str | None = None
    target_model: str | None = None
    saved: bool | None = None


class OperationLogRequest(BaseModel):
    """Request recent local Creo MCP operation records."""

    model_config = ConfigDict(extra="forbid")

    max_records: int = Field(default=50, ge=1, le=500)


class OperationLogResult(BaseModel):
    """Recent operations and the local JSONL audit path."""

    model_config = ConfigDict(extra="forbid")

    log_path: str
    records: list[OperationLogRecord]


def create_packaging_box_plan(request: PackagingBoxPlanRequest) -> PackagingBoxPlan:
    """Produce a deterministic, validated plan for a coarse packaging-box model."""
    normalized_request = request.model_dump(mode="json")
    serialized_request = json.dumps(normalized_request, sort_keys=True, separators=(",", ":"))
    plan_id = hashlib.sha256(serialized_request.encode("utf-8")).hexdigest()[:16]
    dimensions = BoxDimensions(
        length_mm=request.length_mm,
        width_mm=request.width_mm,
        height_mm=request.height_mm,
    )
    return PackagingBoxPlan(
        plan_id=plan_id,
        execution_state="dry_run_only",
        model_mode="solid_envelope",
        part_name=request.part_name,
        expected_model_file=f"{request.part_name}.prt",
        output_directory=request.output_directory,
        dimensions_mm=dimensions,
        wall_thickness_mm=request.wall_thickness_mm,
        operations=[
            "Inspect a native millimetre template and verify its named driving dimensions.",
            "Copy the template to a new model name without overwriting an existing file.",
            "Set the native length, width, and height driving dimensions.",
            "Regenerate and validate the resulting bounding dimensions.",
            "Save only after the write-enabled tool receives the matching plan and confirmation.",
        ],
        requires_user_confirmation=True,
    )
