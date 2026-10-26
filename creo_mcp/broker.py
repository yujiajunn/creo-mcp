"""Single-instance Creo MCP broker over a local Windows named pipe.

The broker owns the one supported VB API connection. MCP server processes and
CLI probes forward requests to it instead of connecting to Creo themselves, so
client restarts and multiple clients no longer race for the cross-process
connection mutex.
"""

from __future__ import annotations

import getpass
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

BROKER_PROTOCOL_VERSION = 1
BROKER_START_TIMEOUT_SECONDS = 30.0
BROKER_PING_TIMEOUT_SECONDS = 1.0
BROKER_STOP_TIMEOUT_SECONDS = 5.0


class BrokerError(RuntimeError):
    """The local Creo MCP broker could not serve a request."""


def _user_token() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", user) or "user"


def pipe_name() -> str:
    """Return the per-user named-pipe address used by the broker."""
    return rf"\\.\pipe\creo-mcp-broker-{_user_token()}"


def runtime_dir() -> Path:
    """Return the per-user runtime directory for broker state and logs.

    ``CREO_MCP_RUNTIME_DIR`` overrides it so test runs and side-by-side
    instances never write into the production ``CreoMCP`` directory tree.
    """
    configured = os.environ.get("CREO_MCP_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "CreoMCP"


def state_path() -> Path:
    return runtime_dir() / "broker.json"


def log_path() -> Path:
    return runtime_dir() / "logs" / "broker.log"


def _log(message: str) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


def _connect(address: str | None = None):
    from multiprocessing.connection import Client

    return Client(address or pipe_name())


def _envelope(payload: dict[str, Any]) -> dict[str, Any]:
    return {"protocol": BROKER_PROTOCOL_VERSION, **payload}


def _read_state() -> dict[str, Any] | None:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def ping(
    timeout: float = BROKER_PING_TIMEOUT_SECONDS,
    *,
    address: str | None = None,
) -> dict[str, Any] | None:
    """Return the broker ping response, or None when it is not running."""
    try:
        conn = _connect(address)
    except (OSError, FileNotFoundError):
        return None
    try:
        conn.send(_envelope({"kind": "ping"}))
        if not conn.poll(timeout):
            return None
        response = conn.recv()
        return response if isinstance(response, dict) else None
    except (OSError, EOFError, ValueError):
        return None
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _spawn_broker() -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    try:
        subprocess.Popen(
            [sys.executable, "-m", "creo_mcp.broker", "--serve"],
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=stream,
            creationflags=flags,
            close_fds=True,
            cwd=str(runtime_dir()),
        )
    finally:
        stream.close()


def ensure_broker(*, timeout: float = BROKER_START_TIMEOUT_SECONDS) -> None:
    """Start the broker if needed and wait until it answers a ping."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ping() is not None:
            return

        mutex = None
        acquired = False
        try:
            import win32api
            import win32event

            mutex = win32event.CreateMutex(None, False, r"Local\CreoMCP-Broker-Start")
            acquired = win32event.WaitForSingleObject(
                mutex,
                min(5000, max(500, int((deadline - time.monotonic()) * 1000))),
            ) in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED)
            if acquired and ping() is None:
                _spawn_broker()
        except ImportError:
            _spawn_broker()
        finally:
            if mutex is not None:
                if acquired:
                    win32event.ReleaseMutex(mutex)
                win32api.CloseHandle(mutex)

        wait_until = min(deadline, time.monotonic() + 3.0)
        while time.monotonic() < wait_until:
            if ping() is not None:
                return
            time.sleep(0.25)

    raise BrokerError(
        "The Creo MCP broker did not become ready. Check the broker log at "
        f"{log_path()} and confirm Creo is running."
    )


def call(
    request: dict[str, Any],
    *,
    timeout_seconds: float,
    address: str | None = None,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Send one request to the broker, starting it on demand.

    ``on_progress`` receives each intermediate ``{"kind": "progress", ...}``
    frame while the operation runs. Progress is opt-in via ``want_progress``,
    so older brokers and clients keep working unchanged.
    """
    if on_progress is not None:
        request = {**request, "want_progress": True}
    for attempt in (0, 1):
        try:
            conn = _connect(address)
        except (OSError, FileNotFoundError):
            if attempt:
                raise BrokerError(
                    "The Creo MCP broker is unavailable. Check the broker log at "
                    f"{log_path()}."
                )
            ensure_broker()
            continue

        try:
            conn.send(_envelope(request))
            deadline = time.monotonic() + timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not conn.poll(remaining):
                    raise BrokerError(
                        "The Creo MCP broker did not respond in time. The operation may still "
                        "be running; inspect Creo before retrying, and use --broker-status to "
                        "check the broker."
                    )
                frame = conn.recv()
                if not isinstance(frame, dict):
                    raise BrokerError("The Creo MCP broker returned an invalid response.")
                if frame.get("kind") == "progress":
                    try:
                        on_progress(frame)
                    except Exception:
                        pass
                    continue
                response = frame
                break
            if response.get("protocol") != BROKER_PROTOCOL_VERSION:
                raise BrokerError(
                    "The Creo MCP broker uses an incompatible protocol version; stop it "
                    "with --broker-stop and retry."
                )
            return response
        except (EOFError, OSError) as exc:
            if attempt == 0 and request.get("kind") == "status":
                # A status probe is safe to retry when the broker was just replaced.
                ensure_broker()
                continue
            raise BrokerError(f"Lost the connection to the Creo MCP broker: {exc}") from exc
        finally:
            try:
                conn.close()
            except OSError:
                pass

    raise BrokerError("The Creo MCP broker is unavailable.")


def status() -> dict[str, Any]:
    """Return a diagnostic snapshot for the CLI."""
    response = ping()
    return {
        "running": response is not None,
        "ping": response,
        "state": _read_state(),
        "pipe": pipe_name(),
        "log": str(log_path()),
    }


def stop(*, timeout: float = BROKER_STOP_TIMEOUT_SECONDS) -> bool:
    """Ask a running broker to disconnect and exit."""
    try:
        conn = _connect()
    except (OSError, FileNotFoundError):
        return False
    try:
        conn.send(_envelope({"kind": "shutdown"}))
        if not conn.poll(timeout):
            return False
        conn.recv()
    except (OSError, EOFError, ValueError):
        return False
    finally:
        try:
            conn.close()
        except OSError:
            pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ping(0.2) is None:
            return True
        time.sleep(0.1)
    return False


def _serve_connection(conn: Any, requests: queue.Queue, stop_event: threading.Event) -> None:
    """Read requests from one client and forward them to the COM owner thread."""
    try:
        while not stop_event.is_set():
            try:
                request = conn.recv()
            except (EOFError, OSError):
                return
            if not isinstance(request, dict):
                continue
            if request.get("protocol") != BROKER_PROTOCOL_VERSION:
                conn.send(_envelope({"ok": False, "error": "Broker protocol mismatch."}))
                continue
            kind = request.get("kind")
            if kind == "ping":
                conn.send(_envelope({"ok": True, "pid": os.getpid()}))
                continue
            if kind == "shutdown":
                conn.send(_envelope({"ok": True, "pid": os.getpid()}))
                stop_event.set()
                return
            reply: queue.Queue = queue.Queue(maxsize=1)
            requests.put((request, reply, conn))
            response = reply.get()
            conn.send(_envelope(response))
    except (OSError, EOFError, ValueError):
        return
    finally:
        try:
            conn.close()
        except OSError:
            pass


def run_broker(address: str | None = None) -> int:
    """Own the Creo connection and serve requests until shutdown."""
    from multiprocessing.connection import Listener

    from .bridge import persistent_creo_connection
    from .server import _operation_response, get_status

    address = address or pipe_name()
    try:
        listener = Listener(address)
    except OSError:
        _log("broker start skipped: another broker owns the pipe")
        return 0

    owns_state = address == pipe_name()
    if owns_state:
        state_path().parent.mkdir(parents=True, exist_ok=True)
        state_path().write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "started_at": time.time(),
                    "protocol": BROKER_PROTOCOL_VERSION,
                    "pipe": address,
                }
            ),
            encoding="utf-8",
        )
    requests: queue.Queue = queue.Queue()
    stop_event = threading.Event()

    def accept_loop() -> None:
        while not stop_event.is_set():
            try:
                conn = listener.accept()
            except (OSError, EOFError):
                if stop_event.is_set():
                    return
                continue
            threading.Thread(
                target=_serve_connection,
                args=(conn, requests, stop_event),
                daemon=True,
                name="creo-mcp-broker-client",
            ).start()

    accept_thread = threading.Thread(
        target=accept_loop,
        daemon=True,
        name="creo-mcp-broker-accept",
    )
    _log(f"broker starting on {address} (pid {os.getpid()})")
    try:
        with persistent_creo_connection():
            _log("broker connected to Creo")
            accept_thread.start()
            while not stop_event.is_set():
                try:
                    request, reply, client_conn = requests.get(timeout=0.5)
                except queue.Empty:
                    continue
                progress_cb = None
                if request.get("want_progress"):
                    def progress_cb(value: float, message: str, _conn: Any = client_conn) -> None:
                        try:
                            _conn.send(
                                _envelope(
                                    {
                                        "kind": "progress",
                                        "progress": value,
                                        "total": 100,
                                        "message": message,
                                    }
                                )
                            )
                        except OSError:
                            pass
                try:
                    kind = request.get("kind")
                    if kind == "status":
                        response: dict[str, Any] = {
                            "ok": True,
                            "result": get_status().model_dump(),
                        }
                    elif kind == "operation":
                        response = _operation_response(
                            str(request.get("operation", "")),
                            dict(request.get("payload") or {}),
                            progress_cb=progress_cb,
                        )
                    else:
                        response = {"ok": False, "error": "Unknown broker request."}
                except Exception as exc:
                    response = {"ok": False, "error": str(exc)}
                reply.put(response)
    except Exception as exc:
        _log(f"broker stopped with an error: {exc}")
        return 1
    finally:
        stop_event.set()
        try:
            listener.close()
        except OSError:
            pass
        if owns_state:
            try:
                state_path().unlink(missing_ok=True)
            except OSError:
                pass
        _log("broker stopped")
    return 0


def main() -> None:
    raise SystemExit(run_broker())


if __name__ == "__main__":
    main()
