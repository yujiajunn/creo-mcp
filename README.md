# Creo MCP

This is a portable Creo bridge built on the standard MCP stdio transport:

```text
any MCP client → MCP (stdio) → Python → official PTC VB API / COM → Creo
```

It uses the standard MCP stdio transport and contains no nanobot-specific code.
It works with any compatible local MCP client, including nanobot, Codex,
Claude Desktop, and Cursor.

See [ARCHITECTURE.md](ARCHITECTURE.md) or the
[Chinese architecture guide](ARCHITECTURE.zh-CN.md) for the boundary between
reusable Creo capabilities, product skills, project data, safety invariants,
and the planned generic feature set. See the
[Chinese engineering delivery standard](ENGINEERING_STANDARD.zh-CN.md) for
human handoff, naming, editability, observability, and acceptance rules. See
[UDF_MANIFEST.zh-CN.md](UDF_MANIFEST.zh-CN.md) for the qualified UDF manifest
format and live qualification workflow. Real-session evidence and the remaining
qualification gaps are tracked in
[LIVE_VALIDATION.zh-CN.md](LIVE_VALIDATION.zh-CN.md). Client setup, protocol
compatibility, distribution options, and the prioritized improvement backlog are
documented in [CLIENT_SETUP.zh-CN.md](CLIENT_SETUP.zh-CN.md).

It currently exposes these tools:

- `creo_get_status` — verifies the connection and returns the active model plus
  working directory.
- `creo_get_active_model` — returns active-model metadata only.
- `creo_get_model_geometry` — measures the true Creo envelope and solid volume
  of the active model or a named loaded model, normalized to millimetres.
- `creo_get_geometry_details` — resolves explicit surface and edge IDs into
  useful geometry: types, area or length, bounds or sample points, adjacent
  surfaces, and owning features, normalized to millimetres.
- `creo_find_surfaces` — scans all model surfaces, filters by type, area, or
  constant X/Y/Z coordinate, and returns a bounded candidate list instead of a
  complete topology dump.
- `creo_get_model_structure` — returns a bounded inventory of model-tree
  features, surfaces, edges, axes, points, coordinate systems, driving
  dimensions, and model parameters with stable IDs and names for later
  operations.
- `creo_get_handoff_report` — checks feature health, unnamed features, named
  dimensions, automation ownership parameters, units, and model envelope before
  an engineer takes over the model.
- `creo_get_operation_log` — reads recent privacy-minimized JSONL audit records
  with operation IDs, durations, outcomes, target model names, and save state.
- `creo_validate_udf_manifest` — validates a strict UDF qualification record
  and verifies the exact `.gph` SHA-256 hash without starting Creo.
- `creo_run_udf_qualification` — performs one explicitly confirmed,
  supervised placement from a candidate manifest whose
  `verified_non_interactive` flag is still false. It never saves or grants
  qualification automatically.
- `creo_place_qualified_udf` — places a fully bound, version-qualified UDF via
  the official `CreateUDFGroup` API, regenerates and measures the target, and
  leaves it unsaved. Interactive repair is disabled.
- `creo_plan_packaging_box` — validates a coarse box request and returns a
  deterministic dry-run plan. It does not connect to Creo or create a file.
- `creo_inspect_part_dimensions` — retrieves a native `.prt` file and reports
  its principal length unit and driving dimension symbols without saving it.
- `creo_inspect_step_file` — validates a local STEP file and reports its AP
  schema, products, unit, solid bodies, faces, and assembly structure without
  starting Creo.
- `creo_import_cad` — imports a supported CAD file as a new **unsaved** Creo
  part or assembly and returns its model type, unit, envelope, and solid volume
  when available. It supports automatic format detection for STEP, IGES,
  Parasolid, ACIS, STL, JT, CATIA, SolidWorks, Inventor, VDA, and Creo Neutral.
- `creo_open_native_model` — loads and measures an existing native Creo `.prt`
  or `.asm` without modifying or saving its source file.
- `creo_create_empty_part` — creates and activates a new **unsaved** Creo part.
  It is the first write-path verification and should require client-side approval.
- `creo_update_model_metadata` — renames stable model items, changes named
  driving dimensions with explicit units, creates or updates typed model
  parameters, regenerates, and leaves the model unsaved.
- `creo_save_model_as` — creates a protected native part or assembly copy under
  a new name, refuses all existing Creo versions, and restores the original
  Creo working directory.
- `creo_export_model` — exports a loaded model to a new STEP, IGES, or Parasolid
  file, validates its extension and optional profile, and refuses overwrite.
- `creo_import_step` — imports a STEP file as a new **unsaved** Creo part or
  assembly, detects its model type, and measures the true Creo envelope. It is
  retained as a compatibility wrapper around the general CAD import path.
- `creo_create_packaging_box` — copies a qualified native Creo template to a
  new name, applies confirmed dimensions, regenerates, verifies the resulting
  envelope, and saves the new model. It refuses to overwrite any existing Creo
  version of the requested part name.

The tools use explicit JSON schemas, structured output, standard MCP tool
annotations, isolated COM calls, and timeouts. Write operations are separate
from inspection and planning operations.

## Architecture boundary

The MCP is the general Creo capability layer. It owns file loading, geometry
measurement, native feature operations, regeneration, and guarded saving or
exporting. It must not assume that the product is a syringe or that the target
is blister packaging.

Product workflows belong in later skills. A syringe skill can compose generic
revolve, extrude, shell, pattern, and parameter operations. A blister skill can
measure any loaded reference product, apply clearance and manufacturing rules,
and compose generic cavity and tray features. This keeps the same MCP useful for
other medical devices and packaging tasks.

Native Creo `.prt` files and NX `.prt` files share an extension. Automatic CAD
import therefore rejects `.prt`: use the native part inspection/loading path for
Creo files, or pass `source_format: "nx"` explicitly for NX. Mesh sources such
as STL are accepted as reference geometry but are not treated as editable B-rep
solids.

`creo_create_packaging_box` requires both the matching `plan_id` from
`creo_plan_packaging_box` and `confirmation: "create_new_model"`. The plan ID
changes whenever the name, dimensions, wall thickness, or output directory
changes, so a stale approval cannot be reused for different geometry.

## Native packaging-box template

The first modelling workflow deliberately uses a native Creo template instead
of an imported STEP body or UI automation. Create one solid rectangular
extrusion in Creo, use a millimetre unit system, and rename its three driving
dimensions to:

```text
BOX_LENGTH
BOX_WIDTH
BOX_HEIGHT
```

Save that part as a dedicated template. Before using it, call
`creo_inspect_part_dimensions` and verify that the unit is `mm` and all three
symbols are present. The create tool copies this file, changes only the copied
model, checks its regenerated outline, and then saves the new model. The source
template is never modified.

The current creator produces a solid bounding envelope. `wall_thickness_mm`
must be null until the separate hollow-box workflow is implemented.

## Requirements

- Creo 13.4.1.0 is installed and running.
- `VB API for Creo Parametric` was installed and registered using
  `vb_api_register.bat` as Administrator.
- The MCP client and Creo are launched from the same Windows account.

## Standalone installation

Create a dedicated virtual environment in this repository. Do not reuse an
application repository's environment:

```powershell
cd <repo>
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\creo-mcp.exe --probe-status
```

The editable install provides the `creo-mcp.exe` entry point while keeping the
server source and documentation in this repository. Client configurations may
use either that entry point or the explicit Python and `creo_mcp_server.py`
paths shown below.

## Run as an MCP server

Use the repository virtual environment:

```powershell
cd <repo>
.\.venv\Scripts\python.exe .\creo_mcp_server.py
```

This uses stdio, so it will appear idle in a terminal. nanobot launches it
automatically after the following connection is added to its config:

```json
{
  "tools": {
    "mcpServers": {
      "creo": {
        "command": "<repo>\\.venv\\Scripts\\python.exe",
        "args": [
          "<repo>\\creo_mcp_server.py"
        ],
        "cwd": "<repo>",
        "toolTimeout": 150,
        "enabledTools": [
          "creo_get_status",
          "creo_get_active_model",
          "creo_get_model_geometry",
          "creo_get_geometry_details",
          "creo_find_surfaces",
          "creo_get_model_structure",
          "creo_get_handoff_report",
          "creo_get_operation_log",
          "creo_validate_udf_manifest",
          "creo_run_udf_qualification",
          "creo_place_qualified_udf",
          "creo_plan_packaging_box",
          "creo_inspect_part_dimensions",
          "creo_inspect_step_file",
          "creo_create_empty_part",
          "creo_update_model_metadata",
          "creo_save_model_as",
          "creo_export_model",
          "creo_open_native_model",
          "creo_import_cad",
          "creo_import_step",
          "creo_create_packaging_box"
        ]
      }
    }
  }
}
```

Do not merge the JSON by hand while the gateway is running. Add it through the
WebUI MCP settings, or stop the gateway first, update the existing config, and
then restart nanobot.

## Local verification

Run the package tests from the integration directory:

```powershell
cd <repo>
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

One-shot operations are available for local diagnosis without an MCP client:

```powershell
<repo>\.venv\Scripts\python.exe .\creo_mcp_server.py `
  --operation inspect_part `
  --payload '{"part_path":"<work-dir>\\packaging_box_template.prt"}'
```

After obtaining stable IDs from `inspect_model_structure`, inspect selected
surfaces and edges in detail:

```powershell
<repo>\.venv\Scripts\python.exe .\creo_mcp_server.py `
  --operation inspect_geometry_details `
  --payload '{"surface_ids":[3669,3670],"edge_ids":[2843,2844]}'
```

Find large planar candidates at a known model coordinate:

```powershell
<repo>\.venv\Scripts\python.exe .\creo_mcp_server.py `
  --operation find_surfaces `
  --payload '{"surface_types":["plane"],"minimum_area_mm2":500,"constant_axis":"y","axis_coordinate_mm":0,"axis_tolerance_mm":0.01,"sort_by":"area_desc","max_results":10}'
```

STEP files can be inspected without a running Creo session:

```powershell
<repo>\.venv\Scripts\python.exe .\creo_mcp_server.py `
  --operation inspect_step `
  --payload '{"step_path":"C:\\path\\product.stp"}'
```

The first STEP import capability intentionally leaves the new model unsaved:

```powershell
<repo>\.venv\Scripts\python.exe .\creo_mcp_server.py `
  --operation import_step `
  --payload '{"step_path":"C:\\path\\product.stp","model_name":"product_reference","activate":true,"confirmation":"import_unsaved_model"}'
```

Use the general importer when the source format may vary:

```powershell
<repo>\.venv\Scripts\python.exe .\creo_mcp_server.py `
  --operation import_cad `
  --payload '{"source_path":"C:\\path\\product.x_t","source_format":"auto","model_name":"product_reference","activate":true,"confirmation":"import_unsaved_model"}'
```

Validate a candidate or qualified UDF manifest offline before any model mutation:

```powershell
<repo>\.venv\Scripts\python.exe .\creo_mcp_server.py `
  --operation validate_udf_manifest `
  --payload '{"manifest_path":"<udf-dir>\\node.candidate.udf.json"}'
```

For a candidate whose `verified_non_interactive` flag is false, pass the returned
`validation_id`, complete stable-ID references, and all dimensions to the
`qualify_udf` one-shot operation or `creo_run_udf_qualification` MCP tool. After
a human confirms that Creo showed no prompts and the geometry is correct, set the
formal manifest flag to true, validate it again, and use the new validation ID
with `creo_place_qualified_udf`. Both write paths change only the loaded model
and never save it.

For engineering handoff, first run `handoff_report`, then make explicitly
confirmed semantic-name, dimension, and ownership-parameter changes with
`update_model_metadata`. Inspect the report again before separately requesting
`save_model_as` or `export_model`. One-shot CLI responses include an
`operation_id`; recent records are also available through the
`creo_get_operation_log` MCP tool. The default JSONL location is
`%LOCALAPPDATA%\CreoMCP\logs\operations.jsonl`, and `CREO_MCP_LOG_DIR` can
override its directory.
