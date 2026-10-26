# Creo MCP Architecture

Chinese version: [ARCHITECTURE.zh-CN.md](creo_mcp/docs/ARCHITECTURE.zh-CN.md)

## Purpose

Creo MCP is a general, deterministic capability layer for PTC Creo. It exposes
small CAD operations through standard MCP tools. It does not contain product
recipes for syringes, blister trays, or other product families.

The intended composition is:

```text
agent
  -> product or process skill
    -> general Creo MCP tools
      -> official PTC VB API / COM
        -> Creo Parametric
```

This boundary lets product workflows evolve without coupling the Creo bridge to
one part type, one source format, or one manufacturing process.

## Responsibilities

### MCP capability layer

The MCP owns behavior that must remain true regardless of the caller or product:

- Connect to the current Creo session through the official API.
- Open native Creo models and import supported external CAD formats.
- Read model metadata, units, dimensions, parameters, geometry, and assemblies.
- Create and edit native sketches and features through explicit operations.
- Regenerate the model and return measured post-operation results.
- Keep unit conversion and coordinate-system behavior deterministic.
- Reject invalid names, ambiguous formats, missing references, and stale plans.
- Require explicit confirmation for session-changing or file-writing operations.
- Refuse implicit overwrite, deletion, purge, or replacement of existing models.
- Return structured errors rather than relying on Creo dialog text.
- Keep write operations isolated, bounded by timeouts, and observable.

These rules are part of the implementation and cannot depend only on an LLM
prompt or skill instructions.

### Skill workflow layer

Skills own product knowledge and modelling strategy:

- Which features should be created and in what order.
- Which parameters describe a syringe, plunger, needle hub, or blister tray.
- Product-specific ranges, clearances, wall thicknesses, draft angles, and radii.
- How reference geometry becomes a cavity, fixture, insert, or package.
- Naming conventions, feature grouping, model-tree organization, and checklists.
- Which MCP result should be checked before advancing to the next operation.
- Recovery policy when a feature fails or a result falls outside expectations.
- Organization-specific review and approval steps.

A skill may impose stricter rules than the MCP, but it cannot weaken MCP safety
invariants.

### Project data and templates

Company or project standards should remain data rather than Python control flow:

- Material and stock definitions.
- Standard clearances and tolerance tables.
- Approved native templates and start parts.
- Drawing formats and parameter mappings.
- Naming prefixes and output directory conventions.

Skills can select this data and pass it to MCP tools. The MCP validates the
resolved values before changing Creo.

## Tool contract principles

Each tool should perform one coherent operation and expose a strict JSON schema.

Read tools:

- Must not save, overwrite, or change model geometry.
- Should be idempotent for the same Creo state.
- Return normalized units when possible and preserve the reported native unit.

Session-changing tools:

- Require a literal confirmation value in the request schema.
- State whether a model was activated, created, imported, modified, or saved.
- Do not imply permission to save merely because a model was opened or created.

Geometry-changing tools:

- Identify the target model and references explicitly.
- Validate all references before applying the first change.
- Regenerate after the operation.
- Measure or inspect the result and return the observed state.
- Prefer an operation identifier or deterministic plan identifier for retries.

File-writing tools:

- Are separate from geometry creation tools whenever practical.
- Require an absolute destination or an explicitly reported working directory.
- Reject every existing Creo version of a target model name unless a future,
  separately designed replacement operation is explicitly requested.

## Model lifecycle

The preferred lifecycle is intentionally explicit:

```text
inspect source
  -> open or import unsaved model
  -> measure and qualify geometry
  -> plan operations
  -> create or modify native features
  -> regenerate and validate
  -> request explicit save or export
```

Opening, importing, editing, saving, and exporting are separate permissions.
This makes intermediate results inspectable and prevents a modelling request
from silently becoming a filesystem mutation.

## CAD input policy

The general importer supports formats that Creo exposes through
`EpfcNewModelImportType`. Format values are taken from the locally registered
Creo 13.4 type library rather than guessed.

Automatic detection is used only for unambiguous extensions. `.prt` is not
auto-detected because it can mean a native Creo part or an NX part:

- Native Creo `.prt` and `.asm` use `creo_open_native_model`.
- NX `.prt` uses `creo_import_cad` with `source_format: "nx"`.

Mesh and tessellated inputs can be useful references, but the MCP must not
promise that they behave like editable B-rep solids. The import result reports
the expected geometry representation.

## Target generic feature set

### Creo VB API feature-creation boundary

Creo 13.4 exposes `IpfcSolid.CreateFeature`, but its VB API type library does
not provide public instruction factories for ordinary extrude or revolve
features. The supported general mechanism that accepts programmatic references
and variant dimensions is `IpfcSolid.CreateUDFGroup`.

Generic feature creation will therefore use parameterized UDFs with structured
manifests. A manifest records the UDF hash, allowed model kinds, required
references, variable dimensions, and unit rules. The MCP validates the entire
manifest and request before placement, invokes the official UDF API, regenerates
the model, and verifies the result. Skills select a qualified UDF and calculate
engineering values, but cannot bypass manifest validation.

The base path is now implemented by `creo_validate_udf_manifest`, expanded
surface and edge reference discovery in `creo_get_model_structure`, and
targeted geometry classification in `creo_get_geometry_details`, plus
bounded surface search in `creo_find_surfaces`, supervised candidate runs in
`creo_run_udf_qualification`, plus production placement in
`creo_place_qualified_udf`. Candidate runs require
`verified_non_interactive=false`, explicit confirmation, and do not grant
qualification automatically. Production placement requires the flag to be true.
Placement revalidates the exact file hash, stale
validation ID, Creo version, target model kind, complete references, and bounded
dimension values before calling `CreateUDFGroup`. It disables interactive repair,
regenerates, reports group members and geometry, and never saves the target.

Model-tree editing is implemented by `creo_update_model_metadata` (semantic
renames, named dimensions, typed parameters) and `creo_update_feature_suppression`
(suppress and resume features by name or stable ID). Suppression resolves every
target before touching any feature, skips features already in the requested state
without a Creo call, refuses states a family table, simplified representation, or
the model program owns, regenerates, and then reads each status back so an
unconfirmed result is an error rather than a silent success. It never saves.

Raw `.gph` files must not be placed non-interactively without a qualified
manifest. Missing inputs cause Creo to prompt interactively, which breaks the
automation contract and can leave a timed-out operation waiting in the UI.

High-level MCP tools may still expose operations such as `create_extrude` and
`create_revolve`; their implementation can use verified UDF assets without
exposing UDF placement details to callers.

The MCP should grow by capability, not by product. The planned order is:

1. Model and reference discovery
   - list loaded models, features, datums, surfaces, axes, and coordinate systems
   - inspect dimensions and parameters on active or named models
2. Sketch construction
   - create sketch planes and orientations
   - add lines, arcs, circles, centerlines, and construction geometry
   - add dimensions and geometric constraints
3. Core solid features
   - extrude, revolve, hole, shell, round, chamfer, pattern, mirror, and sweep
   - delivered through the Phase 1.5 dispatch table and qualified UDFs, not by
     creating features from scratch
4. Feature and parameter editing
   - read and set named dimensions and parameters
   - suppress and resume features (`creo_update_feature_suppression`; implemented,
     pending live Creo validation)
   - reorder, and regenerate with explicit validation
5. Assembly operations
   - create assemblies, add components, and apply placement constraints
6. Persistence and interchange
   - guarded save-as, backup, and export to requested formats

Product skills compose these operations. For example, a syringe skill can use
revolve and extrude operations, while a blister skill can use measured product
geometry, offsets, draft, shell, and rounds.

### Phase 1.5: the template dispatch table

Phase 1.5 widens feature coverage without introducing a Pro/Toolkit C++ add-in.
It is the equivalent of a competitor MCP's UI-command table, except that every
row must pass manifest validation.

A dispatch table maps one generic action name onto one verifiable asset:

| Field | Meaning |
|---|---|
| Action name | Generic verb, for example `cavity_cut`, `seal_flange`, `draft_all` |
| Asset | A qualified UDF `.gph`, or a part template with named driver dimensions |
| Reference bindings | Each reference bound to a stable role (cavity origin CSYS, seal plane) |
| Driver dimensions | Named dimension set, units, legal ranges, defaults |
| Suppression slots | Features preset but suppressed (draft, rounds); unsuppressing enables them |
| Release evidence | The live Creo validation record that qualifies the row |

The boundary follows from what the API can actually do: the Creo 13.4 VB API
cannot create a native feature, but it **can update the named dimensions of an
existing feature and regenerate**, and a UDF group can carry native features. Thin
feature tools are therefore composed from `creo_place_qualified_udf` (place a
feature group), `creo_update_model_metadata` (write named dimensions and typed
parameters), and `creo_update_feature_suppression` (enable suppressed features
already present in the template). That covers extrude, cut, draft, and round for
common cases. A Pro/Toolkit add-in
becomes a prerequisite only for creating a brand-new feature type from scratch or
replaying UI commands; it is the complement of this capability, not a precondition
for Phase 1.5.

Split table content from table execution, using the MCP-versus-skill test stated
above:

- Table content (which templates a product uses, how its dimensions are computed)
  belongs to a skill or a configuration resource.
- Table execution (manifest validation, reference binding, placement, dimension
  writes, suppression and resume, regeneration, measurement) belongs to the MCP.

Safety-critical actions never join the thin table. Saving, save-as, export, and
replacing an existing model stay separate tools with explicit confirmation. The
thin table widens what geometry may change, not what disk may be written.

A row may enter the table only after it satisfies the new-tool checklist in
`docs/TOOL_CONVENTIONS.zh-CN.md`, which requires a live Creo validation record for
write operations. Rows without such a record remain candidates and must not be
selectable by skills.

## Transitional tools

`creo_plan_packaging_box` and `creo_create_packaging_box` predate this boundary.
They remain useful for connection and guarded-write validation, but they are not
the target core API. Once the generic feature set can reproduce their behavior,
the packaging workflow should move into a skill and these tools can be removed.

`creo_import_step` is also transitional. `creo_import_cad` is the general import
contract. Compatibility is not currently a design constraint, so narrow wrappers
do not need to shape future interfaces.

## Testing strategy

The integration uses four verification levels:

1. Pure unit tests validate schemas, path handling, format detection, operation
   planning, unit conversion, and overwrite protection.
2. Surface contract tests validate the tool face itself: naming, titles,
   description budgets, annotation/verb agreement, required confirmation tokens,
   cross-references to tools that actually exist, and a `tools/list` payload
   budget (`tests/test_tool_conventions.py`, backed by `creo_mcp/catalog.py`).
3. A standard MCP client verifies tool discovery, annotations, and input/output
   schemas over stdio.
4. Live Creo checks run from the same interactive Windows session as Creo and
   verify official COM calls against real native and imported models.

The same information is available from the command line without a client:
`creo-mcp --list`, `creo-mcp --list-json`, and `creo-mcp --doctor` (plus
`--doctor --probe` for a live connection check). Preflight results are also a
readable resource, `creo://server/status`.

Live checks are required before a new write tool is considered production-ready.
Mocks prove request construction and invariants, but they cannot prove Creo's
feature semantics or regeneration behavior.

## Change policy

New functionality belongs in the MCP when it is reusable across product types
and can be expressed as a deterministic Creo operation. It belongs in a skill
when it selects, sequences, or constrains those operations for a product or
engineering process.

When uncertain, ask whether another unrelated product could use the same tool
without inheriting syringe or packaging terminology. If not, the behavior is
probably a skill.
