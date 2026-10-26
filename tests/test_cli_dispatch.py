"""Tests for the maintenance CLI paths that touch the broker.

These two behaviours were real defects (audit A-18 and A-19):

* ``--operation`` opened a second direct VB API connection, so it always failed
  with a busy error while the broker was running.
* the broker wrote its state and log into the production runtime directory even
  during test runs.
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from creo_mcp import broker
from creo_mcp import server as server_module


class RunOperationRoutingTests(unittest.TestCase):
    def test_operation_routes_through_a_running_broker(self) -> None:
        captured = io.StringIO()
        envelope = {
            "ok": True,
            "operation_id": "deadbeef",
            "log": {"written": True, "path": "operations.jsonl"},
            "result": {"file_name": "a.prt"},
        }
        with (
            mock.patch.object(broker, "ping", return_value={"ok": True, "pid": 4242}),
            mock.patch.object(server_module, "_call_creo", return_value=envelope) as routed,
            mock.patch.object(server_module, "_operation_response") as direct,
            redirect_stdout(captured),
        ):
            code = server_module._run_operation("inspect_model_structure", {})

        self.assertEqual(code, 0)
        routed.assert_called_once()
        self.assertEqual(routed.call_args.args[0]["kind"], "operation")
        direct.assert_not_called()
        payload = json.loads(captured.getvalue())
        self.assertEqual(payload["via"], "broker")
        self.assertTrue(payload["ok"])
        # README promises operation_id on one-shot responses; routing must keep it.
        self.assertEqual(payload["operation_id"], "deadbeef")
        self.assertEqual(payload["log"]["written"], True)

    def test_operation_failure_through_broker_exits_one(self) -> None:
        captured = io.StringIO()
        with (
            mock.patch.object(broker, "ping", return_value={"ok": True}),
            mock.patch.object(
                server_module,
                "_call_creo",
                side_effect=server_module.CreoMcpError("Creo refused the request."),
            ),
            redirect_stdout(captured),
        ):
            code = server_module._run_operation("save_model_as", {})

        self.assertEqual(code, 1)
        payload = json.loads(captured.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["via"], "broker")
        self.assertIn("Creo refused", payload["error"])

    def test_operation_reports_a_broker_level_failure_without_operation_id(self) -> None:
        captured = io.StringIO()
        with (
            mock.patch.object(broker, "ping", return_value={"ok": True}),
            mock.patch.object(
                server_module,
                "_call_creo",
                return_value={"ok": False, "error": "Unknown broker request."},
            ),
            redirect_stdout(captured),
        ):
            code = server_module._run_operation("no_such_operation", {})

        self.assertEqual(code, 1)
        self.assertEqual(json.loads(captured.getvalue())["error"], "Unknown broker request.")

    def test_operation_runs_directly_only_when_no_broker_is_alive(self) -> None:
        captured = io.StringIO()
        envelope = {"ok": True, "operation_id": "abc", "log": {}, "result": {}}
        with (
            mock.patch.object(broker, "ping", return_value=None),
            mock.patch.object(server_module, "_call_creo") as routed,
            mock.patch.object(
                server_module, "_operation_response", return_value=envelope
            ),
            redirect_stdout(captured),
        ):
            code = server_module._run_operation("inspect_part", {})

        self.assertEqual(code, 0)
        routed.assert_not_called()
        self.assertEqual(json.loads(captured.getvalue())["operation_id"], "abc")


class BrokerRuntimeDirTests(unittest.TestCase):
    def test_runtime_dir_honours_the_environment_override(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            # broker.runtime_dir() resolves the configured value, so compare canonically.
            override = Path(raw_directory).resolve(strict=False) / "creo-mcp"
            with mock.patch.dict(os.environ, {"CREO_MCP_RUNTIME_DIR": str(override)}):
                self.assertEqual(broker.runtime_dir(), override)
                self.assertEqual(broker.state_path(), override / "broker.json")
                self.assertEqual(broker.log_path(), override / "logs" / "broker.log")

    def test_runtime_dir_falls_back_to_local_app_data(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            environment = {"LOCALAPPDATA": raw_directory}
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertEqual(
                    broker.runtime_dir(), Path(raw_directory) / "CreoMCP"
                )

    def test_test_session_runtime_dir_is_not_the_production_one(self) -> None:
        override = os.environ.get("CREO_MCP_RUNTIME_DIR")

        self.assertIsNotNone(override, "tests/conftest.py must isolate the runtime dir")
        self.assertTrue(Path(override).is_dir())
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            self.assertNotIn(
                str(Path(local_app_data) / "CreoMCP"), str(override)
            )


if __name__ == "__main__":
    unittest.main()


class StdioEncodingTests(unittest.TestCase):
    """The maintenance CLI prints Chinese hints.

    With the console code page (often cp936 on zh-CN Windows) as the stream
    encoding, redirecting or piping the output mangles every non-ASCII byte, so
    agents and log files receive garbage. main() therefore pins UTF-8.
    """

    def test_both_streams_are_reconfigured_to_utf8(self) -> None:
        calls: list[dict] = []

        class _Stream:
            def reconfigure(self, **kwargs):
                calls.append(kwargs)

        with mock.patch.object(server_module.sys, "stdout", _Stream()), mock.patch.object(
            server_module.sys, "stderr", _Stream()
        ):
            server_module._configure_stdio_utf8()

        self.assertEqual(len(calls), 2)
        for kwargs in calls:
            self.assertEqual(kwargs["encoding"], "utf-8")
            self.assertEqual(kwargs["errors"], "replace")

    def test_streams_without_reconfigure_are_skipped(self) -> None:
        with mock.patch.object(server_module.sys, "stdout", io.StringIO()), mock.patch.object(
            server_module.sys, "stderr", io.StringIO()
        ):
            server_module._configure_stdio_utf8()  # must not raise

    def test_failing_reconfigure_is_tolerated(self) -> None:
        class _Broken:
            def reconfigure(self, **kwargs):
                raise OSError("stream is closed")

        with mock.patch.object(server_module.sys, "stdout", _Broken()), mock.patch.object(
            server_module.sys, "stderr", _Broken()
        ):
            server_module._configure_stdio_utf8()  # must not abort the CLI
