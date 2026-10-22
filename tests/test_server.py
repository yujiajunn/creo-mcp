"""Contract tests for the public MCP server identity and catalog."""

from __future__ import annotations

import asyncio
import tomllib
import unittest
from pathlib import Path

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

        self.assertEqual(len(tools), 26)
        self.assertTrue(all(tool.title for tool in tools))
        self.assertTrue(all(tool.description for tool in tools))
        self.assertTrue(all(tool.output_schema for tool in tools if tool.name != "creo_capture_screenshot"))
        tool_names = {tool.name for tool in tools}
        self.assertIn("creo_capture_screenshot", tool_names)
        self.assertIn("creo_plan_product_clearance_tray", tool_names)
        self.assertIn("creo_create_product_clearance_tray", tool_names)
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

        self.assertEqual(len(docs), 5)
        self.assertTrue(all(text.strip() for text in docs.values()))


if __name__ == "__main__":
    unittest.main()
