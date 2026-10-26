"""Tool-surface catalog and convention checks for the Creo MCP server.

This module turns the tools registered on a :class:`~mcp.server.fastmcp.FastMCP`
server into a small, stable, inspectable data model.  It backs the ``--list``
and ``--list-json`` command-line switches and the ``creo_tool_catalog`` /
``creo_get_capabilities`` meta tools, and it audits the advertised surface
against the conventions the project relies on (titles, descriptions,
annotations, structured output, and cross-references between tools).

Nothing in this module talks to Creo.  Building a catalog only reads the tool
definitions a ``FastMCP`` instance already holds, so it works with
``creo_mcp.server.create_server()`` and never opens a Creo connection, a
network socket, or an MCP transport.  Library functions return values and never
write diagnostic or protocol output to stdout.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .version import SERVER_NAME, SERVER_VERSION

if TYPE_CHECKING:  # pragma: no cover - imported for type checkers only
    from mcp.server.fastmcp import FastMCP
    from mcp.types import Tool

__all__ = [
    "GROUP_ORDER",
    "META_TOOL_NAMES",
    "CatalogTool",
    "ToolCatalog",
    "build_catalog",
    "find_issues",
    "render_json",
    "render_text",
]

META_TOOL_NAMES: frozenset[str] = frozenset(
    {"creo_get_status", "creo_tool_catalog", "creo_get_capabilities"}
)
"""Tools that inspect the MCP server itself rather than Creo.

These names are the only hard-coded tool list in this module.  Meta tools are
categorised before the annotation rules apply, and they are exempt from the
structured-output check because catalog/status payloads must stay cheap.
"""

GROUP_ORDER: tuple[str, ...] = ("meta", "read", "modify", "write")
"""Canonical rendering order for tool groups."""

_GROUP_META = "meta"
_GROUP_READ = "read"
_GROUP_MODIFY = "modify"
_GROUP_WRITE = "write"

_CROSS_REFERENCE_PATTERN = re.compile(r"creo_[a-z0-9_]+")
_MIN_RENDER_WIDTH = 20
_MAX_NAME_COLUMN = 40


@dataclass(frozen=True)
class CatalogTool:
    """One registered tool as advertised to MCP clients.

    Attributes:
        name: Registered (wire) tool name, for example ``creo_get_status``.
        title: Human-readable title, or ``None`` when the tool declares none.
        description: Advertised description; an empty string when absent.
        group: Behavioural group, one of :data:`GROUP_ORDER`.  ``meta`` wins
            over the annotation rules; otherwise ``readOnlyHint=True`` means
            ``read``, ``destructiveHint=True`` means ``modify``, and everything
            else is ``write``.
        annotations: The tool's MCP annotations serialised to a plain dict
            (``{}`` when the tool declares none).  ``None``-valued hint fields
            are omitted so the dict reflects only explicit declarations.
        input_schema: JSON schema for the tool arguments.
        output_schema: JSON schema for structured output, or ``None`` when the
            tool does not declare structured output.
    """

    name: str
    title: str | None
    description: str
    group: str
    annotations: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None


@dataclass(frozen=True)
class ToolCatalog:
    """Immutable snapshot of a server's advertised tool surface.

    ``tools`` is sorted by tool name so repeated builds of the same server
    produce byte-identical renderings.
    """

    server_name: str
    server_version: str
    tools: tuple[CatalogTool, ...]

    @property
    def count(self) -> int:
        """Number of tools in the catalog."""
        return len(self.tools)

    def groups(self) -> dict[str, tuple[str, ...]]:
        """Return ``group -> sorted tool names`` for the non-empty groups."""
        buckets: dict[str, list[str]] = {}
        for tool in self.tools:
            buckets.setdefault(tool.group, []).append(tool.name)
        return {group: tuple(sorted(names)) for group, names in sorted(buckets.items())}


async def build_catalog(server: FastMCP, *, server_version: str | None = None) -> ToolCatalog:
    """Build a :class:`ToolCatalog` from an already configured ``FastMCP`` server.

    Args:
        server: A configured ``FastMCP`` instance, typically the object returned
            by :func:`creo_mcp.server.create_server`.  The server is only asked
            for its registered tools via the async ``list_tools`` API; no
            transport is started and no Creo connection is opened.
        server_version: Optional version override.  When omitted, the version
            exposed by the underlying low-level MCP server is used (the project
            server sets it in ``create_server``), falling back to
            :data:`creo_mcp.version.SERVER_VERSION`.

    Returns:
        A ``ToolCatalog`` whose tools are sorted by name.
    """
    advertised = await server.list_tools()
    tools = tuple(sorted((_to_catalog_tool(tool) for tool in advertised), key=lambda t: t.name))
    return ToolCatalog(
        server_name=_server_name(server),
        server_version=_resolve_server_version(server, server_version),
        tools=tools,
    )


def render_text(catalog: ToolCatalog, *, include_schema: bool = False, width: int = 100) -> str:
    """Render a catalog as stable, human-readable plain text.

    The output is a header line with the server name/version, a line with the
    total and per-group tool counts, then one section per group in
    :data:`GROUP_ORDER`.  Each tool occupies exactly one line containing its
    name, group, top-level parameter count, and title.  With
    ``include_schema=True`` the tool description (whitespace-collapsed and
    truncated to fit) is appended to that same line; full JSON schemas are
    available from :func:`render_json`.

    Separators are ASCII only (``=`` characters and ``[group]`` headings) so
    the output is safe on Windows consoles.  No ANSI escapes are emitted and no
    non-ASCII box-drawing characters are ever added by this function; tool
    titles and descriptions themselves are rendered verbatim.

    Args:
        catalog: Catalog to render.
        include_schema: Append each tool's description to its line.
        width: Maximum line length in characters.  Values below
            :data:`_MIN_RENDER_WIDTH` are clamped to that minimum.

    Returns:
        Rendered text without a trailing newline.
    """
    line_width = max(int(width), _MIN_RENDER_WIDTH)
    grouped = _grouped_tools(catalog)
    counts = ", ".join(f"{group} {len(grouped.get(group, ()))}" for group in GROUP_ORDER)
    name_column = _name_column(catalog.tools)
    lines = [
        _fit(f"{catalog.server_name} {catalog.server_version}".rstrip(), line_width),
        _fit(f"{catalog.count} tools: {counts}", line_width),
        "=" * line_width,
    ]
    sections = 0
    for group in _section_order(grouped):
        members = grouped[group]
        if not members:
            continue
        if sections:
            lines.append("")
        sections += 1
        noun = "tool" if len(members) == 1 else "tools"
        lines.append(f"[{group}] {len(members)} {noun}")
        for tool in members:
            title = tool.title.strip() if isinstance(tool.title, str) else ""
            line = (
                f"  {tool.name:<{name_column}}  {tool.group:<6}  "
                f"{_parameter_count(tool):>2}p  {title or '-'}"
            )
            if include_schema and tool.description.strip():
                line = f"{line}  ::  {_collapse_whitespace(tool.description)}"
            lines.append(_fit(line, line_width))
    return "\n".join(lines)


def render_json(catalog: ToolCatalog, *, include_schema: bool = False) -> dict[str, Any]:
    """Render a catalog as a JSON-serialisable ``dict``.

    The shape is stable and sorted by tool name::

        {
            "server": {"name": str, "version": str},
            "count": int,
            "groups": {group: [tool_name, ...]},
            "tools": [
                {
                    "name": str,
                    "title": str | None,
                    "group": str,
                    "description": str,
                    "annotations": dict,
                    "parameterCount": int,
                    # only with include_schema=True:
                    "inputSchema": dict,
                    "outputSchema": dict | None,
                },
                ...
            ],
        }

    Args:
        catalog: Catalog to render.
        include_schema: Include the full input and output JSON schemas.

    Returns:
        A fresh dict of JSON-serialisable primitives suitable for
        ``json.dumps``.
    """
    tools: list[dict[str, Any]] = []
    for tool in catalog.tools:
        entry: dict[str, Any] = {
            "name": tool.name,
            "title": tool.title,
            "group": tool.group,
            "description": tool.description,
            "annotations": dict(tool.annotations),
            "parameterCount": _parameter_count(tool),
        }
        if include_schema:
            entry["inputSchema"] = dict(tool.input_schema)
            entry["outputSchema"] = (
                dict(tool.output_schema) if tool.output_schema is not None else None
            )
        tools.append(entry)
    groups = catalog.groups()
    return {
        "server": {"name": catalog.server_name, "version": catalog.server_version},
        "count": catalog.count,
        "groups": {group: list(names) for group, names in groups.items()},
        "tools": tools,
    }


def find_issues(catalog: ToolCatalog, *, max_description_chars: int = 1200) -> list[str]:
    """Audit a catalog against the project's tool-surface conventions.

    Each issue is a human-readable string that starts with the offending tool
    name, for example ``"creo_capture_screenshot: no outputSchema declared"``.
    Issues are ordered by tool name, then by check order, and cross-reference
    findings are de-duplicated per tool.

    Checks performed:

    * missing or blank title;
    * missing or blank description;
    * description longer than ``max_description_chars``;
    * missing annotations (empty ``annotations`` dict);
    * a tool declaring ``readOnlyHint=True`` together with
      ``destructiveHint=True``;
    * missing ``outputSchema``.  This is only reported for tools outside
      :data:`META_TOOL_NAMES`: meta tools (catalog/status/capabilities) are
      deliberately cheap and may return plain text or an inline result, so
      requiring structured output there would be counter-productive.  This rule
      deliberately does not inspect the Python return annotation; a tool that
      returns an image or plain text is still reported because the surface
      should make its result shape explicit, and the meta exemption keeps the
      rule cheap and stable.
    * dangling cross-references: any ``creo_[a-z0-9_]+`` token found in a tool
      description or in any ``description`` inside the tool's input schema that
      does not name a currently registered tool.  This catches documentation
      drift where a description points at a renamed or removed tool.

    Args:
        catalog: Catalog to audit.
        max_description_chars: Maximum allowed description length.

    Returns:
        A list of issue strings; empty when the surface is clean.
    """
    registered = {tool.name for tool in catalog.tools}
    issues: list[str] = []
    for tool in catalog.tools:
        issues.extend(_tool_issues(tool, registered, max_description_chars))
    return issues


def _to_catalog_tool(tool: Tool) -> CatalogTool:
    """Convert one ``mcp.types.Tool`` (or duck-typed equivalent) into a CatalogTool."""
    annotations = _annotations_dict(getattr(tool, "annotations", None))
    input_schema = getattr(tool, "inputSchema", None)
    output_schema = getattr(tool, "outputSchema", None)
    description = getattr(tool, "description", None)
    title = getattr(tool, "title", None)
    return CatalogTool(
        name=tool.name,
        title=title if isinstance(title, str) else None,
        description=description if isinstance(description, str) else "",
        group=_group_for(tool.name, annotations),
        annotations=annotations,
        input_schema=dict(input_schema) if isinstance(input_schema, dict) else {},
        output_schema=dict(output_schema) if isinstance(output_schema, dict) else None,
    )


def _annotations_dict(annotations: Any) -> dict[str, Any]:
    """Serialise MCP tool annotations to a plain dict, dropping unset hints."""
    if annotations is None:
        return {}
    dump = getattr(annotations, "model_dump", None)
    if callable(dump):
        dumped = dump(exclude_none=True)
        return dict(dumped) if isinstance(dumped, dict) else {}
    if isinstance(annotations, dict):
        return dict(annotations)
    return {}


def _group_for(name: str, annotations: dict[str, Any]) -> str:
    """Classify a tool into a group using only its name and declared hints."""
    if name in META_TOOL_NAMES:
        return _GROUP_META
    if annotations.get("readOnlyHint") is True:
        return _GROUP_READ
    if annotations.get("destructiveHint") is True:
        return _GROUP_MODIFY
    return _GROUP_WRITE


def _server_name(server: FastMCP) -> str:
    """Return the advertised server name, falling back to the package identity."""
    name = getattr(server, "name", None)
    return name if isinstance(name, str) and name.strip() else SERVER_NAME


def _resolve_server_version(server: FastMCP, override: str | None) -> str:
    """Resolve the server version: explicit override, then the MCP server, then package."""
    if override is not None:
        return override
    inner = getattr(server, "_mcp_server", None)
    version = getattr(inner, "version", None)
    if isinstance(version, str) and version.strip():
        return version
    return SERVER_VERSION


def _parameter_count(tool: CatalogTool) -> int:
    """Count the top-level parameters declared in a tool's input schema."""
    properties = tool.input_schema.get("properties")
    return len(properties) if isinstance(properties, dict) else 0


def _grouped_tools(catalog: ToolCatalog) -> dict[str, tuple[CatalogTool, ...]]:
    """Group tools by ``group`` and sort the members inside every group."""
    buckets: dict[str, list[CatalogTool]] = {}
    for tool in catalog.tools:
        buckets.setdefault(tool.group, []).append(tool)
    return {
        group: tuple(sorted(members, key=lambda item: item.name))
        for group, members in buckets.items()
    }


def _section_order(grouped: dict[str, tuple[CatalogTool, ...]]) -> tuple[str, ...]:
    """Return canonical groups first, then any unexpected groups sorted by name."""
    extras = tuple(sorted(set(grouped) - set(GROUP_ORDER)))
    return GROUP_ORDER + extras


def _name_column(tools: tuple[CatalogTool, ...]) -> int:
    """Compute a deterministic, bounded name column width for text rendering."""
    longest = max((len(tool.name) for tool in tools), default=0)
    return max(min(longest, _MAX_NAME_COLUMN), 8)


def _collapse_whitespace(text: str) -> str:
    """Collapse all runs of whitespace to single spaces and trim the ends."""
    return " ".join(text.split())


def _fit(line: str, width: int) -> str:
    """Trim a rendered line to ``width`` characters with an ASCII ellipsis."""
    line = line.rstrip()
    if len(line) <= width:
        return line
    if width <= len("..."):
        return line[:width]
    return line[: width - len("...")].rstrip() + "..."


def _tool_issues(tool: CatalogTool, registered: set[str], max_description_chars: int) -> list[str]:
    """Return all convention issues for a single tool, in check order."""
    issues: list[str] = []
    title = tool.title.strip() if isinstance(tool.title, str) else ""
    if not title:
        issues.append(f"{tool.name}: missing title")
    if not tool.description.strip():
        issues.append(f"{tool.name}: missing description")
    elif len(tool.description) > max_description_chars:
        issues.append(
            f"{tool.name}: description is {len(tool.description)} characters, "
            f"limit is {max_description_chars}"
        )
    if not tool.annotations:
        issues.append(f"{tool.name}: missing annotations")
    elif (
        tool.annotations.get("readOnlyHint") is True
        and tool.annotations.get("destructiveHint") is True
    ):
        issues.append(f"{tool.name}: read-only tool declares destructiveHint=true")
    if tool.name not in META_TOOL_NAMES and tool.output_schema is None:
        issues.append(f"{tool.name}: no outputSchema declared")
    issues.extend(_dangling_reference_issues(tool, registered))
    return issues


def _dangling_reference_issues(tool: CatalogTool, registered: set[str]) -> list[str]:
    """Report ``creo_*`` cross-references that name no registered tool."""
    issues: list[str] = []
    seen: set[str] = set()
    sources: list[tuple[str, str | None, str]] = []
    if tool.description.strip():
        sources.append(("description", None, tool.description))
    sources.extend(
        ("input schema description", path, text)
        for path, text in _iter_schema_descriptions(tool.input_schema)
    )
    for label, path, text in sources:
        for reference in _CROSS_REFERENCE_PATTERN.findall(text):
            if reference in registered or reference in seen:
                continue
            seen.add(reference)
            where = label if path is None else f"{label} at '{path}'"
            issues.append(f"{tool.name}: {where} references unknown tool '{reference}'")
    return issues


def _iter_schema_descriptions(schema: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(path, description)`` pairs for every description in a schema."""
    if isinstance(schema, dict):
        description = schema.get("description")
        if isinstance(description, str) and description.strip():
            yield path or "$", description
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for property_name, property_schema in properties.items():
                child_path = f"{path}.{property_name}" if path else property_name
                yield from _iter_schema_descriptions(property_schema, child_path)
        for key, value in schema.items():
            if key in {"description", "properties"}:
                continue
            child_path = f"{path}.{key}" if path else key
            yield from _iter_schema_descriptions(value, child_path)
    elif isinstance(schema, list):
        for index, item in enumerate(schema):
            yield from _iter_schema_descriptions(item, f"{path}[{index}]")
