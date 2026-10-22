"""Contract tests for the single-instance Creo MCP broker."""

from __future__ import annotations

import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from creo_mcp import broker


def _test_pipe() -> str:
    return rf"\\.\pipe\creo-mcp-test-{uuid.uuid4().hex}"


@contextmanager
def _fake_connection():
    yield


def _fake_status():
    return SimpleNamespace(model_dump=lambda: {"connected": True, "active_model": None})


def _fake_operation(operation: str, payload: dict, calls: list[str] | None = None):
    if calls is not None:
        calls.append(operation)
    time.sleep(0.05)
    return {"ok": True, "operation_id": uuid.uuid4().hex, "result": {"operation": operation}}


class BrokerTests(unittest.TestCase):
    def _start(self, pipe: str, calls: list[str] | None = None):
        patches = [
            mock.patch("creo_mcp.bridge.persistent_creo_connection", _fake_connection),
            mock.patch("creo_mcp.server.get_status", _fake_status),
            mock.patch(
                "creo_mcp.server._operation_response",
                lambda operation, payload, progress_cb=None: _fake_operation(
                    operation, payload, calls
                ),
            ),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        thread = threading.Thread(
            target=broker.run_broker,
            kwargs={"address": pipe},
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if broker.ping(0.2, address=pipe) is not None:
                break
            time.sleep(0.05)
        else:
            self.fail("broker did not start")
        return thread

    def test_status_and_operation_round_trip(self) -> None:
        pipe = _test_pipe()
        thread = self._start(pipe)
        try:
            status = broker.call({"kind": "status"}, timeout_seconds=5, address=pipe)
            self.assertTrue(status["ok"])
            self.assertTrue(status["result"]["connected"])

            result = broker.call(
                {"kind": "operation", "operation": "inspect_step", "payload": {"x": 1}},
                timeout_seconds=5,
                address=pipe,
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["result"]["operation"], "inspect_step")
        finally:
            broker.call({"kind": "shutdown"}, timeout_seconds=5, address=pipe)
            thread.join(timeout=5)

    def test_two_clients_share_one_broker_and_serialize(self) -> None:
        pipe = _test_pipe()
        calls: list[str] = []
        thread = self._start(pipe, calls)
        try:
            results: list[dict] = []

            def client(index: int) -> None:
                results.append(
                    broker.call(
                        {
                            "kind": "operation",
                            "operation": f"op_{index}",
                            "payload": {},
                        },
                        timeout_seconds=5,
                        address=pipe,
                    )
                )

            clients = [threading.Thread(target=client, args=(index,)) for index in range(3)]
            for client_thread in clients:
                client_thread.start()
            for client_thread in clients:
                client_thread.join(timeout=5)

            self.assertEqual(len(results), 3)
            self.assertTrue(all(result["ok"] for result in results))
            self.assertEqual(sorted(calls), ["op_0", "op_1", "op_2"])
        finally:
            broker.call({"kind": "shutdown"}, timeout_seconds=5, address=pipe)
            thread.join(timeout=5)

    def test_protocol_mismatch_is_rejected(self) -> None:
        pipe = _test_pipe()
        thread = self._start(pipe)
        try:
            from multiprocessing.connection import Client

            conn = Client(pipe)
            conn.send({"protocol": 999, "kind": "ping"})
            self.assertTrue(conn.poll(2))
            response = conn.recv()
            conn.close()
            self.assertFalse(response["ok"])
            self.assertIn("protocol", response["error"].lower())
        finally:
            broker.call({"kind": "shutdown"}, timeout_seconds=5, address=pipe)
            thread.join(timeout=5)

    def test_progress_frames_reach_requesting_client(self) -> None:
        pipe = _test_pipe()

        def fake_response(operation, payload, progress_cb=None):
            if progress_cb is not None:
                progress_cb(25, "中间阶段")
                progress_cb(50, "后半阶段")
            return {"ok": True, "operation_id": "op-1", "result": {"operation": operation}}

        patches = [
            mock.patch("creo_mcp.bridge.persistent_creo_connection", _fake_connection),
            mock.patch("creo_mcp.server.get_status", _fake_status),
            mock.patch("creo_mcp.server._operation_response", fake_response),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        thread = threading.Thread(
            target=broker.run_broker, kwargs={"address": pipe}, daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and broker.ping(0.2, address=pipe) is None:
            time.sleep(0.05)
        try:
            frames: list[tuple[float, str]] = []
            result = broker.call(
                {"kind": "operation", "operation": "slow_op", "payload": {}},
                timeout_seconds=5,
                address=pipe,
                on_progress=lambda frame: frames.append(
                    (frame["progress"], frame["message"])
                ),
            )
            self.assertTrue(result["ok"])
            self.assertEqual(frames, [(25, "中间阶段"), (50, "后半阶段")])

            # A client that does not ask for progress must not see frames either.
            plain = broker.call(
                {"kind": "operation", "operation": "slow_op", "payload": {}},
                timeout_seconds=5,
                address=pipe,
            )
            self.assertTrue(plain["ok"])
        finally:
            broker.call({"kind": "shutdown"}, timeout_seconds=5, address=pipe)
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
