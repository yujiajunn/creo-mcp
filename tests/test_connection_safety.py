"""Regression tests for Creo VB API connection serialization and timeout handling."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import types
import unittest
from contextlib import nullcontext
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from creo_mcp import bridge, server
from creo_mcp.errors import CreoMcpError


class BridgeConnectionSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        bridge._PERSISTENT_SESSION = None
        bridge._PERSISTENT_SESSION_THREAD_ID = None

    def tearDown(self) -> None:
        bridge._PERSISTENT_SESSION = None
        bridge._PERSISTENT_SESSION_THREAD_ID = None

    def test_connected_session_uses_native_connect_and_disconnect_timeouts(self) -> None:
        connection = MagicMock()
        connection.Session = object()
        factory = MagicMock()
        factory.Connect.return_value = connection

        pythoncom = types.ModuleType("pythoncom")
        pythoncom.CoInitialize = MagicMock()  # type: ignore[attr-defined]
        pythoncom.CoUninitialize = MagicMock()  # type: ignore[attr-defined]
        win32com = types.ModuleType("win32com")
        win32com.__path__ = []  # type: ignore[attr-defined]
        win32com_client = types.ModuleType("win32com.client")
        win32com_client.Dispatch = MagicMock(return_value=factory)  # type: ignore[attr-defined]
        win32com.client = win32com_client  # type: ignore[attr-defined]

        with (
            patch.dict(
                sys.modules,
                {
                    "pythoncom": pythoncom,
                    "win32com": win32com,
                    "win32com.client": win32com_client,
                },
            ),
            patch("creo_mcp.bridge._serialized_creo_connection", return_value=nullcontext()),
            patch(
                "creo_mcp.bridge._configure_creo_environment",
                return_value=Path(r"D:\FixtureCreo\Creo"),
            ),
            patch("creo_mcp.bridge.time.sleep") as sleep,
        ):
            with bridge._connected_session() as (session, loadpoint):
                self.assertIs(session, connection.Session)
                self.assertEqual(loadpoint, Path(r"D:\FixtureCreo\Creo"))

        factory.Connect.assert_called_once_with(
            None,
            None,
            None,
            bridge.CREO_CONNECT_TIMEOUT_SECONDS,
        )
        connection.Disconnect.assert_called_once_with(
            bridge.CREO_DISCONNECT_TIMEOUT_SECONDS
        )
        pythoncom.CoInitialize.assert_called_once_with()  # type: ignore[attr-defined]
        pythoncom.CoUninitialize.assert_called_once_with()  # type: ignore[attr-defined]
        sleep.assert_called_once_with(bridge.CREO_CONNECTION_COOLDOWN_SECONDS)

    def test_named_mutex_rejects_a_concurrent_creo_connection(self) -> None:
        mutex = object()
        win32api = types.ModuleType("win32api")
        win32api.CloseHandle = MagicMock()  # type: ignore[attr-defined]
        win32event = types.ModuleType("win32event")
        win32event.WAIT_OBJECT_0 = 0  # type: ignore[attr-defined]
        win32event.WAIT_ABANDONED = 128  # type: ignore[attr-defined]
        win32event.WAIT_TIMEOUT = 258  # type: ignore[attr-defined]
        win32event.CreateMutex = MagicMock(return_value=mutex)  # type: ignore[attr-defined]
        win32event.WaitForSingleObject = MagicMock(return_value=258)  # type: ignore[attr-defined]
        win32event.ReleaseMutex = MagicMock()  # type: ignore[attr-defined]

        with patch.dict(
            sys.modules,
            {"win32api": win32api, "win32event": win32event},
        ), self.assertRaisesRegex(CreoMcpError, "busy"), bridge._serialized_creo_connection():
            self.fail("The mutex should not have been acquired")

        win32event.ReleaseMutex.assert_not_called()  # type: ignore[attr-defined]
        win32api.CloseHandle.assert_called_once_with(mutex)  # type: ignore[attr-defined]

    def test_connection_failure_still_cleans_up_com_and_observes_cooldown(self) -> None:
        pythoncom = types.ModuleType("pythoncom")
        pythoncom.CoInitialize = MagicMock()  # type: ignore[attr-defined]
        pythoncom.CoUninitialize = MagicMock()  # type: ignore[attr-defined]
        win32com = types.ModuleType("win32com")
        win32com.__path__ = []  # type: ignore[attr-defined]
        win32com_client = types.ModuleType("win32com.client")
        win32com_client.Dispatch = MagicMock(side_effect=RuntimeError("activation failed"))  # type: ignore[attr-defined]
        win32com.client = win32com_client  # type: ignore[attr-defined]

        with (
            patch.dict(
                sys.modules,
                {
                    "pythoncom": pythoncom,
                    "win32com": win32com,
                    "win32com.client": win32com_client,
                },
            ),
            patch("creo_mcp.bridge._serialized_creo_connection", return_value=nullcontext()),
            patch(
                "creo_mcp.bridge._configure_creo_environment",
                return_value=Path(r"D:\FixtureCreo\Creo"),
            ),
            patch("creo_mcp.bridge.time.sleep") as sleep,
            self.assertRaisesRegex(CreoMcpError, "activation failed"),bridge._connected_session()
        ):
            self.fail("A failed COM activation must not yield a session")

        pythoncom.CoInitialize.assert_called_once_with()  # type: ignore[attr-defined]
        pythoncom.CoUninitialize.assert_called_once_with()  # type: ignore[attr-defined]
        sleep.assert_called_once_with(bridge.CREO_CONNECTION_COOLDOWN_SECONDS)

    def test_persistent_worker_reuses_one_connected_session(self) -> None:
        session = object()
        connected = (session, Path(r"D:\FixtureCreo\Creo"))

        with patch(
            "creo_mcp.bridge._new_connected_session",
            return_value=nullcontext(connected),
        ) as connect, bridge.persistent_creo_connection():
            with bridge._connected_session() as first:
                self.assertEqual(first, connected)
            with bridge._connected_session() as second:
                self.assertEqual(second, connected)

        connect.assert_called_once_with()


class ServerProcessSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        with server._CIRCUIT_STATE_LOCK:
            server._timed_out_worker = None
        server._persistent_worker = None
        server._persistent_worker_responses = None
        server._persistent_worker_stderr.clear()

    def tearDown(self) -> None:
        with server._CIRCUIT_STATE_LOCK:
            server._timed_out_worker = None
        server._persistent_worker = None
        server._persistent_worker_responses = None
        server._persistent_worker_stderr.clear()

    def test_persistent_worker_stdout_ignores_native_library_output(self) -> None:
        process = MagicMock()
        process.stdout = StringIO(
            "OpenCascade transfer progress\n"
            "\n"
            f'{server.PERSISTENT_WORKER_RESPONSE_PREFIX}{json.dumps({"ok": True})}\n'
        )
        responses: queue.Queue[str | None] = queue.Queue()

        server._read_persistent_worker_stdout(process, responses)

        self.assertEqual(json.loads(responses.get_nowait()), {"ok": True})
        self.assertIsNone(responses.get_nowait())
        self.assertIn(
            "worker stdout: OpenCascade transfer progress",
            server._persistent_worker_stderr,
        )

    def test_isolated_process_returns_captured_output(self) -> None:
        process = MagicMock()
        process.args = ["python", "worker.py"]
        process.pid = 123
        process.returncode = 0
        process.communicate.return_value = ('{"ok": true}', "")

        with patch("creo_mcp.server.subprocess.Popen", return_value=process):
            completed = server._run_isolated_process(
                process.args,
                timeout_seconds=30,
                timeout_message="timed out",
            )

        self.assertEqual(completed.stdout, '{"ok": true}')
        self.assertEqual(completed.returncode, 0)
        process.communicate.assert_called_once_with(timeout=30)

    def test_timeout_leaves_worker_running_and_opens_circuit(self) -> None:
        process = MagicMock()
        process.args = ["python", "worker.py"]
        process.pid = 456
        process.poll.return_value = None
        process.communicate.side_effect = subprocess.TimeoutExpired(process.args, 30)
        drain_thread = MagicMock()

        with (
            patch("creo_mcp.server.subprocess.Popen", return_value=process),
            patch("creo_mcp.server.threading.Thread", return_value=drain_thread),
        ):
            with self.assertRaisesRegex(CreoMcpError, "was not force-killed"):
                server._run_isolated_process(
                    process.args,
                    timeout_seconds=30,
                    timeout_message="timed out",
                )

        drain_thread.start.assert_called_once_with()
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        with self.assertRaisesRegex(CreoMcpError, "PID 456"):
            server._ensure_worker_circuit_closed()

    def test_circuit_closes_when_timed_out_worker_has_exited(self) -> None:
        process = MagicMock()
        process.pid = 789
        process.poll.return_value = 1
        with server._CIRCUIT_STATE_LOCK:
            server._timed_out_worker = process

        server._ensure_worker_circuit_closed()

        with server._CIRCUIT_STATE_LOCK:
            self.assertIsNone(server._timed_out_worker)

    def test_parallel_mcp_call_is_rejected_before_spawning_worker(self) -> None:
        server._ISOLATED_CALL_LOCK.acquire()
        try:
            with (
                patch.object(server, "MCP_CALL_LOCK_TIMEOUT_SECONDS", 0),
                patch("creo_mcp.server.subprocess.Popen") as popen,
            ):
                with self.assertRaisesRegex(CreoMcpError, "parallel"):
                    server._run_isolated_process(
                        ["python", "worker.py"],
                        timeout_seconds=30,
                        timeout_message="timed out",
                    )
            popen.assert_not_called()
        finally:
            server._ISOLATED_CALL_LOCK.release()

    def test_persistent_worker_call_reuses_the_running_process(self) -> None:
        process = MagicMock()
        process.pid = 901
        process.poll.return_value = None
        responses: queue.Queue[str | None] = queue.Queue()
        responses.put(json.dumps({"request_id": "request-1", "ok": True, "result": {}}))
        server._persistent_worker = process
        server._persistent_worker_responses = responses

        with (
            patch("creo_mcp.server.uuid.uuid4", return_value=SimpleNamespace(hex="request-1")),
            patch("creo_mcp.server._start_persistent_worker") as start,
        ):
            result = server._persistent_worker_call({"kind": "status"}, timeout_seconds=1)

        self.assertTrue(result["ok"])
        start.assert_not_called()
        written = process.stdin.write.call_args.args[0]
        self.assertEqual(json.loads(written)["request_id"], "request-1")
        process.stdin.flush.assert_called_once_with()

    def test_persistent_worker_timeout_closes_input_without_killing_process(self) -> None:
        process = MagicMock()
        process.pid = 902
        process.poll.return_value = None
        responses: queue.Queue[str | None] = queue.Queue()
        server._persistent_worker = process
        server._persistent_worker_responses = responses
        drain_thread = MagicMock()

        with (
            patch("creo_mcp.server.threading.Thread", return_value=drain_thread),
            self.assertRaisesRegex(CreoMcpError, "was not force-killed"),
        ):
            server._persistent_worker_call({"kind": "status"}, timeout_seconds=0)

        process.stdin.close.assert_called_once_with()
        process.kill.assert_not_called()
        process.terminate.assert_not_called()
        drain_thread.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
