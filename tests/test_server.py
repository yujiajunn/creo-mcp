"""Contract tests for the public MCP server identity and catalog."""

from __future__ import annotations

import asyncio
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from creo_mcp import server as server_module
from creo_mcp.errors import CreoMcpError
from creo_mcp.server import create_server
from creo_mcp.version import SERVER_NAME, SERVER_VERSION


class ServerContractTests(unittest.TestCase):
    def test_server_advertises_project_identity(self) -> None:
        server = create_server()
        project = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(server.name, SERVER_NAME)
        self.assertEqual(server._mcp_server.version, SERVER_VERSION)
        self.assertEqual(SERVER_VERSION, project["project"]["version"])
        self.assertIsNotNone(server._mcp_server.instructions)

    def test_public_catalog_has_schemas_and_documentation_resources(self) -> None:
        server = create_server()
        tools = server._tool_manager.list_tools()
        resources = server._resource_manager.list_resources()

        self.assertEqual(len(tools), 30)
        self.assertTrue(all(tool.title for tool in tools))
        self.assertTrue(all(tool.description for tool in tools))
        # Meta tools return compact JSON without an output schema on purpose:
        # tools/list payload size is a per-turn context cost (see
        # docs/TOOL_CONVENTIONS.zh-CN.md).
        meta_tools = {"creo_tool_catalog", "creo_get_capabilities"}
        schema_exempt = {"creo_capture_screenshot"} | meta_tools
        self.assertTrue(
            all(tool.output_schema for tool in tools if tool.name not in schema_exempt)
        )
        tool_names = {tool.name for tool in tools}
        self.assertIn("creo_capture_screenshot", tool_names)
        self.assertIn("creo_build_geometry", tool_names)
        self.assertIn("creo_plan_product_clearance_tray", tool_names)
        self.assertIn("creo_create_product_clearance_tray", tool_names)
        self.assertIn("creo_update_feature_suppression", tool_names)
        self.assertTrue(meta_tools <= tool_names)
        self.assertNotIn("creo_create_concept_tray", tool_names)
        self.assertNotIn("creo_create_product_silhouette_tray", tool_names)
        self.assertEqual(
            {str(resource.uri) for resource in resources},
            {
                "creo://docs/architecture",
                "creo://docs/client-setup",
                "creo://docs/engineering-standard",
                "creo://docs/live-validation",
                "creo://docs/udf-manifest",
                "creo://server/status",
                "creo://tools/catalog",
            },
        )

    def test_documentation_resources_read_from_package(self) -> None:
        server = create_server()

        async def read_docs() -> dict[str, str]:
            docs: dict[str, str] = {}
            for resource in server._resource_manager.list_resources():
                loaded = await server._resource_manager.get_resource(resource.uri)
                self.assertIsNotNone(loaded)
                content = await loaded.read()
                docs[str(resource.uri)] = content if isinstance(content, str) else ""
            return docs

        docs = asyncio.run(read_docs())

        self.assertEqual(len(docs), 7)
        self.assertTrue(all(text.strip() for text in docs.values()))


class WorkerCommandTests(unittest.TestCase):
    def test_worker_command_uses_the_repository_shim_when_present(self) -> None:
        command = server_module._worker_command("--persistent-worker")

        self.assertEqual(command[0], sys.executable)
        self.assertTrue(Path(command[1]).is_file())
        self.assertEqual(command[-1], "--persistent-worker")

    def test_worker_command_falls_back_to_the_module_entry(self) -> None:
        original = server_module._probe_script_path
        try:
            server_module._probe_script_path = (
                lambda: Path("Z:/definitely/missing/creo_mcp_server.py")
            )
            command = server_module._worker_command("--operation", "inspect_part")
        finally:
            server_module._probe_script_path = original

        self.assertEqual(command[1:3], ["-m", "creo_mcp.server"])


class ProgressRelayTests(unittest.IsolatedAsyncioTestCase):
    class _RecordingContext:
        def __init__(self) -> None:
            self.reports: list[tuple[float, str | None]] = []

        async def report_progress(self, progress, total=None, message=None) -> None:
            self.reports.append((progress, message))

    async def test_broker_progress_frames_are_relayed(self) -> None:
        def fake_call_creo(request, *, timeout_seconds, on_progress=None):
            self.assertEqual(request["kind"], "operation")
            self.assertIsNotNone(on_progress)
            on_progress(
                {"kind": "progress", "progress": 40.0, "total": 100, "message": "导入中"}
            )
            on_progress(
                {"kind": "progress", "progress": 100.0, "total": 100, "message": "完成"}
            )
            return {"ok": True, "result": {"done": True}}

        ctx = self._RecordingContext()
        with mock.patch.object(server_module, "_call_creo", fake_call_creo):
            result = await server_module._run_isolated_operation_with_progress(
                "import_step", {}, ctx
            )
        self.assertEqual(result, {"done": True})
        self.assertEqual(ctx.reports, [(40.0, "导入中"), (100.0, "完成")])

    async def test_operation_errors_still_propagate(self) -> None:
        def fake_call_creo(request, *, timeout_seconds, on_progress=None):
            return {"ok": False, "error": "Creo is busy."}

        ctx = self._RecordingContext()
        with mock.patch.object(server_module, "_call_creo", fake_call_creo):
            with self.assertRaises(CreoMcpError):
                await server_module._run_isolated_operation_with_progress(
                    "import_step", {}, ctx
                )


if __name__ == "__main__":
    unittest.main()
