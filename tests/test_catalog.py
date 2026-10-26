"""Contract tests for the Creo MCP tool catalog and convention checks.

The tests build a catalog from the real ``create_server()`` object.  That does
not need Creo, a network connection, or an MCP transport: ``create_server`` only
registers tool definitions, and ``build_catalog`` only reads them back.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from creo_mcp.catalog import (
    GROUP_ORDER,
    META_TOOL_NAMES,
    CatalogTool,
    ToolCatalog,
    build_catalog,
    find_issues,
    render_json,
    render_text,
)
from creo_mcp.server import create_server
from creo_mcp.version import SERVER_NAME, SERVER_VERSION

_ALLOWED_GROUPS = frozenset(GROUP_ORDER)


@pytest.fixture(scope="module")
def catalog() -> ToolCatalog:
    """Catalog of the real server, built without connecting to Creo."""
    return asyncio.run(build_catalog(create_server()))


def _tool(**overrides: Any) -> CatalogTool:
    """Build a synthetic, convention-clean CatalogTool, overriding any field."""
    fields: dict[str, Any] = {
        "name": "creo_synthetic",
        "title": "Synthetic Tool",
        "description": "A synthetic tool used by catalog tests.",
        "group": "read",
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object"},
    }
    fields.update(overrides)
    return CatalogTool(**fields)


def _synthetic_catalog(*tools: CatalogTool) -> ToolCatalog:
    return ToolCatalog(server_name="Synthetic MCP", server_version="0.0.0", tools=tools)


# ---------------------------------------------------------------------------
# Real server surface
# ---------------------------------------------------------------------------


def test_real_catalog_identity(catalog: ToolCatalog) -> None:
    assert catalog.server_name == SERVER_NAME
    assert catalog.server_version == SERVER_VERSION
    assert catalog.count == len(catalog.tools) >= 27
    names = [tool.name for tool in catalog.tools]
    assert len(names) == len(set(names))
    assert all(name.startswith("creo_") for name in names)


def test_every_tool_has_a_known_group(catalog: ToolCatalog) -> None:
    for tool in catalog.tools:
        assert tool.group in _ALLOWED_GROUPS, tool.name
        assert isinstance(tool.annotations, dict)
        assert isinstance(tool.input_schema, dict)


def test_groups_follow_the_annotation_rules(catalog: ToolCatalog) -> None:
    for tool in catalog.tools:
        if tool.name in META_TOOL_NAMES:
            assert tool.group == "meta", tool.name
        elif tool.annotations.get("readOnlyHint") is True:
            assert tool.group == "read", tool.name
        elif tool.annotations.get("destructiveHint") is True:
            assert tool.group == "modify", tool.name
        else:
            assert tool.group == "write", tool.name


def test_groups_partition_every_tool_exactly_once(catalog: ToolCatalog) -> None:
    groups = catalog.groups()

    assert set(groups) <= _ALLOWED_GROUPS
    for group, names in groups.items():
        assert names, f"empty group {group}"
        assert list(names) == sorted(names)

    flattened = [name for names in groups.values() for name in names]
    assert len(flattened) == catalog.count
    assert sorted(flattened) == sorted(tool.name for tool in catalog.tools)
    assert META_TOOL_NAMES & set(flattened) == set(groups.get("meta", ()))


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def _assert_ascii_structure(text: str) -> None:
    """The module itself must only add printable-ASCII structure."""
    assert "\x1b" not in text
    assert not any(0x2500 <= ord(char) <= 0x257F for char in text)
    assert "\t" not in text


def test_render_text_is_stable_plain_ascii_and_width_limited(catalog: ToolCatalog) -> None:
    text = render_text(catalog)

    assert text == render_text(catalog)
    assert text.isascii()
    _assert_ascii_structure(text)
    assert max(len(line) for line in text.splitlines()) <= 100
    for tool in catalog.tools:
        assert tool.name in text


def test_render_text_header_and_group_sections(catalog: ToolCatalog) -> None:
    lines = render_text(catalog).splitlines()
    groups = catalog.groups()
    expected_counts = ", ".join(f"{group} {len(groups.get(group, ()))}" for group in GROUP_ORDER)

    assert lines[0] == f"{SERVER_NAME} {SERVER_VERSION}"
    assert lines[1] == f"{catalog.count} tools: {expected_counts}"
    assert set(lines[2]) == {"="}
    for group in groups:
        assert f"[{group}]" in "\n".join(lines)


def test_render_text_include_schema_appends_descriptions(catalog: ToolCatalog) -> None:
    plain = render_text(catalog)
    detailed = render_text(catalog, include_schema=True, width=200)

    assert detailed != plain
    _assert_ascii_structure(detailed)
    assert max(len(line) for line in detailed.splitlines()) <= 200
    first = catalog.tools[0]
    description_prefix = " ".join(first.description.split())[:12]
    assert description_prefix in detailed


def test_render_text_clamps_narrow_width(catalog: ToolCatalog) -> None:
    narrow = render_text(catalog, include_schema=True, width=5)

    assert all(len(line) <= 20 for line in narrow.splitlines())


# ---------------------------------------------------------------------------
# JSON rendering
# ---------------------------------------------------------------------------


def test_render_json_is_stable_and_json_serialisable(catalog: ToolCatalog) -> None:
    payload = render_json(catalog)
    expected_keys = {"name", "title", "group", "description", "annotations", "parameterCount"}

    assert payload == render_json(catalog)
    json.dumps(payload)
    assert payload["server"] == {"name": SERVER_NAME, "version": SERVER_VERSION}
    assert payload["count"] == catalog.count == len(payload["tools"])
    assert payload["groups"] == {group: list(names) for group, names in catalog.groups().items()}
    assert all(set(entry) == expected_keys for entry in payload["tools"])


def test_render_json_includes_schemas_on_request(catalog: ToolCatalog) -> None:
    detailed = render_json(catalog, include_schema=True)

    json.dumps(detailed)
    for entry in detailed["tools"]:
        assert "inputSchema" in entry
        assert "outputSchema" in entry
    screenshot = next(
        entry for entry in detailed["tools"] if entry["name"] == "creo_capture_screenshot"
    )
    assert screenshot["outputSchema"] is None


# ---------------------------------------------------------------------------
# Real-server findings
# ---------------------------------------------------------------------------


def test_find_issues_reports_real_findings(catalog: ToolCatalog) -> None:
    issues = find_issues(catalog)
    tool_names = {tool.name for tool in catalog.tools}

    assert isinstance(issues, list)
    assert all(isinstance(issue, str) and issue.strip() for issue in issues)
    # Concrete known finding on the shipped surface: the screenshot tool returns an
    # image, so it has no structured output schema.
    assert "creo_capture_screenshot: no outputSchema declared" in issues
    for issue in issues:
        assert issue.split(":", 1)[0] in tool_names


# ---------------------------------------------------------------------------
# Grouping and cross-reference rules on synthetic surfaces
# ---------------------------------------------------------------------------


def _grouping_server() -> FastMCP:
    server = FastMCP("Synthetic MCP", instructions="Synthetic catalog for tests.")

    @server.tool(
        name="creo_get_status",
        title="Status",
        description="Return synthetic status.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    def creo_get_status() -> str:
        return "ok"

    @server.tool(
        name="creo_synthetic_read",
        title="Synthetic Read",
        description="Read something synthetic.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    def creo_synthetic_read() -> str:
        return "ok"

    @server.tool(
        name="creo_synthetic_modify",
        title="Synthetic Modify",
        description="Modify something synthetic.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
    )
    def creo_synthetic_modify() -> str:
        return "ok"

    @server.tool(
        name="creo_synthetic_write",
        title="Synthetic Write",
        description="Write something synthetic.",
    )
    def creo_synthetic_write() -> str:
        return "ok"

    return server


def test_groups_are_attribute_driven_on_a_synthetic_server() -> None:
    synthetic = asyncio.run(build_catalog(_grouping_server(), server_version="9.9.9"))
    by_name = {tool.name: tool for tool in synthetic.tools}

    assert synthetic.server_name == "Synthetic MCP"
    assert synthetic.server_version == "9.9.9"
    assert [tool.name for tool in synthetic.tools] == sorted(by_name)
    assert by_name["creo_get_status"].group == "meta"
    assert by_name["creo_synthetic_read"].group == "read"
    assert by_name["creo_synthetic_modify"].group == "modify"
    assert by_name["creo_synthetic_write"].group == "write"
    assert by_name["creo_synthetic_read"].annotations == {
        "readOnlyHint": True,
        "destructiveHint": False,
    }
    assert by_name["creo_synthetic_write"].annotations == {}


def _dangling_server() -> FastMCP:
    server = FastMCP("Dangling MCP")

    @server.tool(
        name="creo_fake_tool",
        title="Fake Tool",
        description="Delegates to the removed creo_does_not_exist helper.",
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
    )
    def creo_fake_tool() -> str:
        return "ok"

    return server


def test_dangling_reference_detected_on_a_real_fastmcp_server() -> None:
    synthetic = asyncio.run(build_catalog(_dangling_server(), server_version="0.0.0"))
    issues = find_issues(synthetic)

    assert any("creo_does_not_exist" in issue for issue in issues)
    assert any(issue.startswith("creo_fake_tool: description") for issue in issues)


def test_dangling_reference_detected_inside_input_schema_descriptions() -> None:
    tool = _tool(
        input_schema={
            "type": "object",
            "properties": {
                "request": {
                    "type": "object",
                    "description": "Calls creo_missing_property_tool after validation.",
                }
            },
            "$defs": {
                "Request": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Handled by creo_missing_nested_tool.",
                        }
                    },
                }
            },
        }
    )
    issues = find_issues(_synthetic_catalog(tool))

    assert any("creo_missing_property_tool" in issue for issue in issues)
    assert any("creo_missing_nested_tool" in issue for issue in issues)


def test_registered_cross_reference_is_not_reported() -> None:
    tool = _tool(description="Pairs with creo_other_tool for round trips.")
    other = _tool(name="creo_other_tool")

    assert find_issues(_synthetic_catalog(tool, other)) == []


# ---------------------------------------------------------------------------
# Individual convention rules on synthetic tools
# ---------------------------------------------------------------------------


def test_find_issues_reports_missing_title_and_description() -> None:
    tool = _tool(title="   ", description="   ")

    assert find_issues(_synthetic_catalog(tool)) == [
        "creo_synthetic: missing title",
        "creo_synthetic: missing description",
    ]


def test_find_issues_reports_overlong_description() -> None:
    tool = _tool(description="x" * 20)

    assert find_issues(_synthetic_catalog(tool), max_description_chars=10) == [
        "creo_synthetic: description is 20 characters, limit is 10"
    ]


def test_find_issues_reports_missing_annotations() -> None:
    tool = _tool(annotations={})

    assert find_issues(_synthetic_catalog(tool)) == ["creo_synthetic: missing annotations"]


def test_find_issues_reports_readonly_destructive_conflict() -> None:
    tool = _tool(annotations={"readOnlyHint": True, "destructiveHint": True})

    assert find_issues(_synthetic_catalog(tool)) == [
        "creo_synthetic: read-only tool declares destructiveHint=true"
    ]


def test_output_schema_rule_exempts_meta_tools() -> None:
    synthetic = _tool(output_schema=None)
    meta = _tool(name="creo_tool_catalog", group="meta", output_schema=None)

    assert find_issues(_synthetic_catalog(synthetic, meta)) == [
        "creo_synthetic: no outputSchema declared"
    ]
