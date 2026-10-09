"""Contract tests for the public MCP server identity and catalog."""

from __future__ import annotations

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

        self.assertEqual(len(tools), 22)
        self.assertTrue(all(tool.title for tool in tools))
        self.assertTrue(all(tool.description for tool in tools))
        self.assertTrue(all(tool.output_schema for tool in tools))
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


if __name__ == "__main__":
    unittest.main()
