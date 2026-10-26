"""Contract tests for the public tool surface.

These tests turn ``docs/TOOL_CONVENTIONS.zh-CN.md`` into executable rules. They
are the guard the ZW3D MCP reference implementation lacks: it ships five tool
descriptions that reference tools which no longer exist, and read-only tools
annotated as write tools, because nothing checks the surface as a whole.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
from contextlib import redirect_stdout

import pytest

from creo_mcp import server as server_module
from creo_mcp.catalog import META_TOOL_NAMES, ToolCatalog, build_catalog

# Verbs allowed in ``creo_<verb>_<object>`` and the annotation each one implies.
READ_VERBS = frozenset({"get", "list", "find", "inspect", "validate", "plan", "capture"})
WRITE_VERBS = frozenset(
    {"create", "update", "save", "export", "place", "run", "open", "import", "build"}
)
NAME_PATTERN = re.compile(r"^creo_[a-z][a-z0-9_]*(_[a-z0-9]+)+$")
CROSS_REFERENCE = re.compile(r"\bcreo_[a-z0-9_]+\b")

MAX_DESCRIPTION_CHARS = 1200
MAX_TOOLS_LIST_BYTES = 150_000


@pytest.fixture(scope="module")
def catalog() -> ToolCatalog:
    async def _build() -> ToolCatalog:
        return await build_catalog(server_module.create_server())

    return asyncio.run(_build())


def _walk_property_sets(schema: object):
    """Yield every ``properties`` mapping inside a JSON schema, recursively."""
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            yield properties
        for value in schema.values():
            yield from _walk_property_sets(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _walk_property_sets(value)


def _confirmation_token(schema: dict) -> str | None:
    """Return the literal confirmation value a tool requires, if any."""
    for properties in _walk_property_sets(schema):
        field = properties.get("confirmation")
        if not isinstance(field, dict):
            continue
        const = field.get("const")
        if isinstance(const, str):
            return const
        enum = field.get("enum")
        if isinstance(enum, list) and len(enum) == 1 and isinstance(enum[0], str):
            return enum[0]
    return None


def _schema_description_strings(schema: object):
    """Yield every ``description`` string inside a JSON schema."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "description" and isinstance(value, str):
                yield value
            else:
                yield from _schema_description_strings(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _schema_description_strings(value)


def test_tool_names_follow_the_convention(catalog: ToolCatalog) -> None:
    registered = {tool.name for tool in catalog.tools}
    for name in registered:
        assert NAME_PATTERN.match(name), f"{name}: name does not match creo_<verb>_<object>"
        if name in META_TOOL_NAMES:
            continue
        verb = name.split("_")[1]
        assert verb in READ_VERBS | WRITE_VERBS, (
            f"{name}: verb {verb!r} is not in the documented verb whitelist; "
            "extend docs/TOOL_CONVENTIONS.zh-CN.md deliberately"
        )


def test_every_tool_has_title_and_bounded_description(catalog: ToolCatalog) -> None:
    for tool in catalog.tools:
        assert tool.title and tool.title.strip(), f"{tool.name}: missing title"
        assert tool.description and tool.description.strip(), f"{tool.name}: missing description"
        assert len(tool.description) <= MAX_DESCRIPTION_CHARS, (
            f"{tool.name}: description is {len(tool.description)} chars, over the "
            f"{MAX_DESCRIPTION_CHARS} char budget"
        )


def test_annotations_match_the_verb_category(catalog: ToolCatalog) -> None:
    for tool in catalog.tools:
        annotations = tool.annotations or {}
        assert annotations, f"{tool.name}: missing annotations"
        read_only = annotations.get("readOnlyHint")
        verb = tool.name.split("_")[1]
        if tool.name in META_TOOL_NAMES or verb in READ_VERBS:
            assert read_only is True, f"{tool.name}: read-only verb must set readOnlyHint=true"
            assert annotations.get("destructiveHint") is not True, (
                f"{tool.name}: a read-only tool must not be destructive"
            )
        elif verb in WRITE_VERBS:
            assert read_only is False, f"{tool.name}: write verb must set readOnlyHint=false"
        assert annotations.get("openWorldHint") is False, (
            f"{tool.name}: openWorldHint must stay false"
        )


def test_write_tools_require_a_confirmation_token(catalog: ToolCatalog) -> None:
    for tool in catalog.tools:
        annotations = tool.annotations or {}
        if annotations.get("readOnlyHint") is not False:
            continue
        token = _confirmation_token(tool.input_schema)
        assert token, f"{tool.name}: write tools must require a literal confirmation token"
        assert token in tool.description, (
            f"{tool.name}: description must state the required token {token!r} verbatim"
        )


def test_no_tool_description_references_a_missing_tool(catalog: ToolCatalog) -> None:
    registered = {tool.name for tool in catalog.tools}
    dangling: list[str] = []
    for tool in catalog.tools:
        texts = [tool.description]
        texts.extend(_schema_description_strings(tool.input_schema))
        for text in texts:
            for reference in CROSS_REFERENCE.findall(text):
                if reference not in registered:
                    dangling.append(f"{tool.name} -> {reference}")
    assert dangling == [], f"descriptions reference unregistered tools: {sorted(set(dangling))}"


def test_structured_output_coverage(catalog: ToolCatalog) -> None:
    # Discovery meta tools stay cheap on purpose: tools/list size is a per-turn
    # cost, and a dict-returning meta tool may carry only the trivial object
    # schema. creo_get_status keeps its rich schema, which is the whole point.
    light_meta_tools = {"creo_tool_catalog", "creo_get_capabilities"}
    exempt = META_TOOL_NAMES | {"creo_capture_screenshot"}
    for tool in catalog.tools:
        if tool.name in light_meta_tools:
            if tool.output_schema is not None:
                size = len(json.dumps(tool.output_schema, ensure_ascii=False).encode("utf-8"))
                assert size <= 512, (
                    f"{tool.name}: discovery meta tools must not carry a rich output schema "
                    f"({size} bytes)"
                )
        elif tool.name not in exempt:
            assert tool.output_schema, f"{tool.name}: expected a structured output schema"


def test_tools_list_payload_stays_within_budget(catalog: ToolCatalog) -> None:
    payload = []
    for tool in catalog.tools:
        entry: dict[str, object] = {
            "name": tool.name,
            "title": tool.title,
            "description": tool.description,
            "inputSchema": tool.input_schema,
        }
        if tool.output_schema is not None:
            entry["outputSchema"] = tool.output_schema
        if tool.annotations:
            entry["annotations"] = tool.annotations
        payload.append(entry)
    size = len(json.dumps({"tools": payload}, ensure_ascii=False).encode("utf-8"))
    assert size <= MAX_TOOLS_LIST_BYTES, (
        f"tools/list is {size} bytes, over the {MAX_TOOLS_LIST_BYTES} byte budget; "
        "trim outputSchema first (see docs/TOOL_CONVENTIONS.zh-CN.md)"
    )


def test_catalog_cli_renders_both_formats(catalog: ToolCatalog) -> None:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert server_module._tool_catalog_for_cli(as_json=True, include_schema=False) == 0
    payload = json.loads(buffer.getvalue())
    assert payload["count"] == catalog.count
    assert set(payload["groups"]) <= {"meta", "read", "modify", "write"}
    grouped = {name for names in payload["groups"].values() for name in names}
    assert grouped == {tool.name for tool in catalog.tools}

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert server_module._tool_catalog_for_cli(as_json=False, include_schema=False) == 0
    text = buffer.getvalue()
    assert "[read]" in text and "[write]" in text
    assert text.isascii(), "the text catalog must stay ASCII for Windows consoles"


def test_groups_partition_the_surface(catalog: ToolCatalog) -> None:
    groups = catalog.groups()
    grouped = [name for names in groups.values() for name in names]
    assert len(grouped) == len(set(grouped)) == catalog.count
