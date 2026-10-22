"""Tests for privacy-minimized local operation logging."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from creo_mcp.models import CreoModelInfo, UpdateModelMetadataResult
from creo_mcp.observability import (
    append_operation_record,
    append_operation_result,
    operation_log_path,
    read_recent_operations,
)


class ObservabilityTests(unittest.TestCase):
    def test_writes_and_reads_recent_records_without_payload_data(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory, patch.dict(
            os.environ, {"CREO_MCP_LOG_DIR": raw_directory}
        ):
            written = append_operation_record(
                operation_id="a" * 32,
                operation="inspect_model_structure",
                duration_ms=12,
                success=True,
                target_model="SAMPLE",
                saved=False,
            )
            append_operation_record(
                operation_id="b" * 32,
                operation="export_model",
                duration_ms=30,
                success=False,
                error_type="CreoMcpError",
            )
            result = read_recent_operations(1)
            raw_log = operation_log_path().read_text(encoding="utf-8")

        self.assertEqual(len(result.records), 1)
        self.assertTrue(written)
        self.assertEqual(result.records[0].operation_id, "b" * 32)
        self.assertNotIn("payload", raw_log.casefold())

    def test_ignores_malformed_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory, patch.dict(
            os.environ, {"CREO_MCP_LOG_DIR": raw_directory}
        ):
            append_operation_record(
                operation_id="c" * 32,
                operation="handoff_report",
                duration_ms=4,
                success=True,
            )
            with operation_log_path().open("a", encoding="utf-8") as stream:
                stream.write("not-json\n")
            result = read_recent_operations(1)

        self.assertEqual([record.operation_id for record in result.records], ["c" * 32])

    def test_extracts_target_model_and_saved_state_from_result(self) -> None:
        result = UpdateModelMetadataResult(
            model=CreoModelInfo(file_name="sample.prt", instance_name="SAMPLE"),
            renames=[],
            dimensions=[],
            parameters=[],
            regenerated=True,
            saved=False,
        )
        with tempfile.TemporaryDirectory() as raw_directory, patch.dict(
            os.environ, {"CREO_MCP_LOG_DIR": raw_directory}
        ):
            append_operation_result("d" * 32, "update_model_metadata", 9, result)
            payload = json.loads(operation_log_path().read_text(encoding="utf-8"))

        self.assertEqual(payload["target_model"], "SAMPLE")
        self.assertFalse(payload["saved"])


if __name__ == "__main__":
    unittest.main()
