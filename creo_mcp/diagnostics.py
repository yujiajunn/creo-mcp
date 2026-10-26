"""Preflight diagnostics for the Creo MCP server (``--doctor``).

The doctor answers the question "why do all of my tool calls fail?" before the
MCP transport starts. Each check is fully isolated: a broken probe degrades to a
warning instead of aborting the report, and nothing here ever prints or mutates
Creo. Imports of the bridge, broker and observability modules stay inside the
individual check functions so that importing this module stays cheap (and works
on non-Windows machines where the COM bridge is unavailable).
"""

from __future__ import annotations

import csv
import importlib
import io
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from importlib import metadata
from pathlib import Path
from typing import Any

__all__ = [
    "STATUS_FAIL",
    "STATUS_OK",
    "STATUS_SKIP",
    "STATUS_WARN",
    "CheckResult",
    "DoctorReport",
    "run_doctor",
]

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIP = "skip"

# Worst status wins: fail > warn > skip > ok.
_STATUS_RANK: dict[str, int] = {
    STATUS_OK: 0,
    STATUS_SKIP: 1,
    STATUS_WARN: 2,
    STATUS_FAIL: 3,
}

_CREO_PROCESS_NAMES = ("xtop.exe", "parametric.exe", "nmsd.exe")
_CREO_MAIN_PROCESS_NAME = "xtop.exe"
_PROCESS_LIST_TIMEOUT_SECONDS = 15
_MAX_DETAIL_LENGTH = 320
_MAX_PROBE_TIMEOUT_SECONDS = 600

_CHECK_TITLES: dict[str, str] = {
    "platform": "Operating system and architecture",
    "python": "Python interpreter",
    "pywin32": "pywin32 COM bindings",
    "mcp": "MCP SDK package",
    "creo_install": "Creo installation (VB API registry)",
    "com_registration": "Creo VB API COM registration",
    "creo_process": "Creo Parametric process",
    "workspace": "Operation log directory",
    "broker": "Creo MCP broker",
    "creo_probe": "Live Creo connection probe",
}

# Runs in a child interpreter so the probe can be bounded by a real timeout and
# can never take the doctor process down with it.
_PROBE_SCRIPT = (
    "import json\n"
    "from creo_mcp.server import _status_from_isolated_process\n"
    "status = _status_from_isolated_process()\n"
    "print(json.dumps(status.model_dump(), ensure_ascii=True))\n"
)

_PROBE_FAIL_HINT = (
    "Creo 连接探针失败：请确认 Creo Parametric 已启动并激活模型、Creo VB API 已按管理员身份注册；"
    "可先运行 `python -m creo_mcp.server --broker-status` 检查代理，必要时重启 Creo 后重试。"
)


@dataclass(frozen=True)
class CheckResult:
    """One preflight check and its factual, one-line verdict."""

    id: str
    title: str
    status: str
    detail: str
    hint: str = ""

    def __post_init__(self) -> None:
        if self.status not in _STATUS_RANK:
            raise ValueError(f"unknown check status: {self.status!r}")


@dataclass(frozen=True)
class DoctorReport:
    """Ordered collection of check results."""

    checks: tuple[CheckResult, ...]

    @property
    def status(self) -> str:
        """Return the worst status across all checks."""
        worst = STATUS_OK
        for check in self.checks:
            if _STATUS_RANK[check.status] > _STATUS_RANK[worst]:
                worst = check.status
        return worst

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the report."""
        return {
            "status": self.status,
            "checks": [
                {
                    "id": check.id,
                    "title": check.title,
                    "status": check.status,
                    "detail": check.detail,
                    "hint": check.hint,
                }
                for check in self.checks
            ],
        }

    def render_text(self) -> str:
        """Render the report as stable text with ASCII-only separators."""
        lines = ["Creo MCP doctor"]
        for check in self.checks:
            lines.append(f"[{check.status}] {check.id}: {_one_line(check.detail)}")
            hint = _one_line(check.hint)
            if hint:
                lines.append(f"  hint: {hint}")

        counts = dict.fromkeys((STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIP), 0)
        for check in self.checks:
            counts[check.status] += 1
        lines.append("")
        lines.append(
            "Summary: "
            f"ok={counts[STATUS_OK]} warn={counts[STATUS_WARN]} "
            f"fail={counts[STATUS_FAIL]} skip={counts[STATUS_SKIP]}"
        )
        lines.append(f"Result: {self.status}")
        return "\n".join(lines)


def run_doctor(*, probe_creo: bool = False, timeout_seconds: int = 60) -> DoctorReport:
    """Run every preflight check; never raise, never print.

    ``probe_creo`` opts in to a live connection probe (the only check that may
    talk to Creo). ``timeout_seconds`` bounds that probe.
    """
    checks = [
        _guarded("platform", _CHECK_TITLES["platform"], _check_platform),
        _guarded("python", _CHECK_TITLES["python"], _check_python),
        _guarded("pywin32", _CHECK_TITLES["pywin32"], _check_pywin32),
        _guarded("mcp", _CHECK_TITLES["mcp"], _check_mcp),
        _guarded("creo_install", _CHECK_TITLES["creo_install"], _check_creo_install),
        _guarded(
            "com_registration",
            _CHECK_TITLES["com_registration"],
            _check_com_registration,
        ),
        _guarded("creo_process", _CHECK_TITLES["creo_process"], _check_creo_process),
        _guarded("workspace", _CHECK_TITLES["workspace"], _check_workspace),
        _guarded("broker", _CHECK_TITLES["broker"], _check_broker),
        _guarded(
            "creo_probe",
            _CHECK_TITLES["creo_probe"],
            partial(
                _check_creo_probe,
                probe_creo=probe_creo,
                timeout_seconds=timeout_seconds,
            ),
        ),
    ]
    return DoctorReport(checks=tuple(checks))


def _guarded(
    check_id: str,
    title: str,
    check: Callable[[], CheckResult],
) -> CheckResult:
    """Run one check, converting any unexpected failure into a warning."""
    try:
        result = check()
    except Exception as exc:  # noqa: BLE001 - one broken probe must not abort the report
        return CheckResult(
            id=check_id,
            title=title,
            status=STATUS_WARN,
            detail=_one_line(f"unexpected {type(exc).__name__}: {exc}"),
            hint="预检项执行异常，请记录上述错误类型并提交问题；其余检查结果仍然有效。",
        )
    if not isinstance(result, CheckResult):
        return CheckResult(
            id=check_id,
            title=title,
            status=STATUS_WARN,
            detail=_one_line(f"check returned {type(result).__name__} instead of CheckResult"),
            hint="预检项返回了无效结果，请记录并提交问题。",
        )
    return result


def _one_line(text: object, *, limit: int = _MAX_DETAIL_LENGTH) -> str:
    """Collapse arbitrary text to a single bounded line."""
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3] + "..."


def _is_windows() -> bool:
    return os.name == "nt"


def _check_platform() -> CheckResult:
    """Report the operating system and CPU architecture."""
    system = platform.system() or "unknown"
    machine = platform.machine() or "unknown"
    release = platform.version() or platform.release() or "unknown"
    if system != "Windows":
        return CheckResult(
            id="platform",
            title=_CHECK_TITLES["platform"],
            status=STATUS_FAIL,
            detail=f"{system} ({machine}) is not supported by the COM-based Creo VB API bridge.",
            hint="creo-mcp 依赖 Windows COM 与 Creo VB API，请改用 64 位 Windows 10/11 运行。",
        )
    return CheckResult(
        id="platform",
        title=_CHECK_TITLES["platform"],
        status=STATUS_OK,
        detail=f"{system} {release} ({machine})",
    )


def _check_python() -> CheckResult:
    """Report the interpreter version and require Python 3.11 or newer."""
    version = platform.python_version()
    implementation = platform.python_implementation() or "Python"
    environment = "virtualenv" if sys.prefix != sys.base_prefix else "system"
    detail = f"{implementation} {version} ({environment}) at {sys.executable}"
    if sys.version_info < (3, 11):  # noqa: UP036 - runtime guard for older interpreters
        return CheckResult(
            id="python",
            title=_CHECK_TITLES["python"],
            status=STATUS_WARN,
            detail=f"{detail}; Python 3.11 or newer is required.",
            hint="请使用 Python 3.11+ 重建虚拟环境，例如 `py -3.11 -m venv .venv`，然后重新安装依赖。",
        )
    return CheckResult(
        id="python",
        title=_CHECK_TITLES["python"],
        status=STATUS_OK,
        detail=detail,
    )


def _check_pywin32() -> CheckResult:
    """Verify that the pywin32 COM bindings can be imported."""
    try:
        importlib.import_module("win32com.client")
    except ModuleNotFoundError as exc:
        return CheckResult(
            id="pywin32",
            title=_CHECK_TITLES["pywin32"],
            status=STATUS_FAIL,
            detail=f"win32com.client is not importable ({type(exc).__name__}: {exc}).",
            hint="缺少 pywin32：请执行 `pip install pywin32`，随后运行 "
            "`python -m pywin32_postinstall -install` 完成 COM 注册并重启终端。",
        )
    try:
        installed = metadata.version("pywin32")
    except metadata.PackageNotFoundError:
        installed = "unknown"
    return CheckResult(
        id="pywin32",
        title=_CHECK_TITLES["pywin32"],
        status=STATUS_OK,
        detail=f"win32com.client importable (pywin32 {installed})",
    )


def _check_mcp() -> CheckResult:
    """Verify that a supported MCP SDK version is installed."""
    try:
        importlib.import_module("mcp")
        installed = metadata.version("mcp")
    except (metadata.PackageNotFoundError, ModuleNotFoundError) as exc:
        return CheckResult(
            id="mcp",
            title=_CHECK_TITLES["mcp"],
            status=STATUS_WARN,
            detail=f"mcp package is not importable ({type(exc).__name__}).",
            hint='未安装 mcp SDK：请执行 `pip install "mcp>=1.30,<2"` 后重启客户端。',
        )
    match = re.match(r"\s*(\d+)\.(\d+)", installed)
    if match is None:
        return CheckResult(
            id="mcp",
            title=_CHECK_TITLES["mcp"],
            status=STATUS_WARN,
            detail=f"mcp {installed} has an unrecognised version number.",
            hint='请执行 `pip install "mcp>=1.30,<2"` 升级 mcp SDK 后重启客户端。',
        )
    major, minor = int(match.group(1)), int(match.group(2))
    if (major, minor) < (1, 30):
        return CheckResult(
            id="mcp",
            title=_CHECK_TITLES["mcp"],
            status=STATUS_WARN,
            detail=f"mcp {installed} is older than the required 1.30.",
            hint='mcp SDK 版本过低：请执行 `pip install "mcp>=1.30,<2"` 升级后重启客户端。',
        )
    return CheckResult(
        id="mcp",
        title=_CHECK_TITLES["mcp"],
        status=STATUS_OK,
        detail=f"mcp {installed}",
    )


def _check_creo_install() -> CheckResult:
    """Locate the Creo loadpoint through the registered VB API server."""
    if not _is_windows():
        return CheckResult(
            id="creo_install",
            title=_CHECK_TITLES["creo_install"],
            status=STATUS_SKIP,
            detail="not applicable on this operating system",
        )
    from .bridge import _creo_loadpoint_from_registry
    from .errors import CreoMcpError

    try:
        loadpoint = _creo_loadpoint_from_registry()
    except CreoMcpError as exc:
        return CheckResult(
            id="creo_install",
            title=_CHECK_TITLES["creo_install"],
            status=STATUS_WARN,
            detail=_one_line(str(exc)),
            hint="未找到 Creo 安装点：请以管理员身份运行 Creo 安装目录下的 `vb_api_register.bat` "
            "（通常在 Parametric\\bin 下），然后重新打开终端并重试。",
        )
    if not loadpoint.is_dir():
        return CheckResult(
            id="creo_install",
            title=_CHECK_TITLES["creo_install"],
            status=STATUS_WARN,
            detail=f"registry points to {loadpoint}, but that directory does not exist.",
            hint="注册表中的 Creo 安装路径已失效：请先修复或重装 Creo，再以管理员身份运行 "
            "`vb_api_register.bat` 重新注册 VB API。",
        )
    common_files = loadpoint / "Common Files"
    if not common_files.is_dir():
        return CheckResult(
            id="creo_install",
            title=_CHECK_TITLES["creo_install"],
            status=STATUS_WARN,
            detail=f"{loadpoint} exists but has no 'Common Files' directory.",
            hint="Creo 安装不完整或安装点异常：请确认安装的是完整 Creo Parametric，并以管理员身份重新运行 "
            "`vb_api_register.bat`。",
        )
    return CheckResult(
        id="creo_install",
        title=_CHECK_TITLES["creo_install"],
        status=STATUS_OK,
        detail=f"{loadpoint} (Common Files present)",
    )


def _check_com_registration() -> CheckResult:
    """Verify the COM ProgID used by the bridge resolves to a registered class."""
    if not _is_windows():
        return CheckResult(
            id="com_registration",
            title=_CHECK_TITLES["com_registration"],
            status=STATUS_SKIP,
            detail="not applicable on this operating system",
        )
    import winreg

    from .bridge import ASYNC_CONNECTION_CLSID, ASYNC_CONNECTION_PROG_ID

    prog_id = ASYNC_CONNECTION_PROG_ID
    hive, registered_clsid = _registered_prog_id_clsid(winreg, prog_id)
    if not registered_clsid:
        return CheckResult(
            id="com_registration",
            title=_CHECK_TITLES["com_registration"],
            status=STATUS_FAIL,
            detail=f"COM ProgID {prog_id} is not registered in HKLM/HKCU.",
            hint="Creo VB API 未注册：请以管理员身份运行 Creo 安装目录下的 `vb_api_register.bat`，"
            "并确认使用与 Creo 匹配的 64 位版本（x86e_win64）。",
        )
    if registered_clsid.upper() != ASYNC_CONNECTION_CLSID.upper():
        return CheckResult(
            id="com_registration",
            title=_CHECK_TITLES["com_registration"],
            status=STATUS_WARN,
            detail=f"{prog_id} -> {registered_clsid} via {hive}; bridge expects "
            f"{ASYNC_CONNECTION_CLSID}.",
            hint="注册表中 ProgID 指向的 CLSID 与 bridge 预期不一致：请以管理员身份重新运行 "
            "`vb_api_register.bat` 覆盖旧注册，然后重启终端。",
        )

    server_path = _local_server_executable(winreg, registered_clsid)
    if not server_path:
        return CheckResult(
            id="com_registration",
            title=_CHECK_TITLES["com_registration"],
            status=STATUS_WARN,
            detail=f"{prog_id} -> {registered_clsid} via {hive}, but LocalServer32 is missing.",
            hint="COM 注册缺少 LocalServer32 指向：请以管理员身份重新运行 `vb_api_register.bat` "
            "完成 VB API 注册。",
        )
    executable = Path(server_path)
    if not executable.is_file():
        return CheckResult(
            id="com_registration",
            title=_CHECK_TITLES["com_registration"],
            status=STATUS_WARN,
            detail=f"{prog_id} -> {registered_clsid}, but {executable} does not exist.",
            hint="COM 注册指向的服务器程序不存在：Creo 可能已移动或卸载，请修复安装后以管理员身份重新运行 "
            "`vb_api_register.bat`。",
        )
    return CheckResult(
        id="com_registration",
        title=_CHECK_TITLES["com_registration"],
        status=STATUS_OK,
        detail=f"{prog_id} -> {registered_clsid} via {hive} (LocalServer32: {executable})",
    )


def _registered_prog_id_clsid(winreg: Any, prog_id: str) -> tuple[str, str]:
    """Return the hive label and CLSID registered for a ProgID, or empty strings."""
    prog_key = rf"SOFTWARE\Classes\{prog_id}\CLSID"
    views = (
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_CURRENT_USER, "HKCU", winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_CLASSES_ROOT, "HKCR", 0),
    )
    for root, label, view in views:
        value = _registry_default_value(winreg, root, prog_key, view=view)
        if value:
            return label, value
    return "", ""


def _registry_default_value(
    winreg: Any,
    root: Any,
    subkey: str,
    *,
    view: int = 0,
) -> str | None:
    """Read a registry key's default value, returning None when it is absent."""
    try:
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ | view) as key:
            value, _ = winreg.QueryValueEx(key, None)
    except OSError:
        return None
    return str(value).strip() or None


def _local_server_executable(winreg: Any, clsid: str) -> str | None:
    """Read and normalise the LocalServer32 command for a CLSID."""
    raw = _registry_default_value(
        winreg,
        winreg.HKEY_CLASSES_ROOT,
        rf"CLSID\{clsid}\LocalServer32",
    )
    if not raw:
        return None
    # The registered value is often a quoted command line; keep only the executable.
    return raw.split('"')[1] if raw.startswith('"') and '"' in raw[1:] else raw


def _check_creo_process() -> CheckResult:
    """Detect the Creo Parametric process without extra dependencies."""
    if not _is_windows():
        return CheckResult(
            id="creo_process",
            title=_CHECK_TITLES["creo_process"],
            status=STATUS_SKIP,
            detail="not applicable on this operating system",
        )
    completed = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=_PROCESS_LIST_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        return CheckResult(
            id="creo_process",
            title=_CHECK_TITLES["creo_process"],
            status=STATUS_WARN,
            detail=f"tasklist exited with code {completed.returncode}; process list unavailable.",
            hint="无法读取进程列表：请在普通用户权限下重新运行 `--doctor`，并检查 tasklist 是否可用。",
        )

    found = _parse_tasklist_pids(completed.stdout)
    xtop_pids = found.get(_CREO_MAIN_PROCESS_NAME, [])
    if not xtop_pids:
        return CheckResult(
            id="creo_process",
            title=_CHECK_TITLES["creo_process"],
            status=STATUS_FAIL,
            detail=f"{_CREO_MAIN_PROCESS_NAME} is not running; found: {_format_found(found)}",
            hint="未检测到 Creo Parametric 进程：请启动 Creo Parametric 并打开/激活一个模型，"
            "然后重新运行 `--doctor`。",
        )
    return CheckResult(
        id="creo_process",
        title=_CHECK_TITLES["creo_process"],
        status=STATUS_OK,
        detail=f"found: {_format_found(found)}",
    )


def _parse_tasklist_pids(output: str) -> dict[str, list[str]]:
    """Map image names to process ids from ``tasklist /FO CSV`` output."""
    found: dict[str, list[str]] = {}
    for row in csv.reader(io.StringIO(output)):
        if not row:
            continue
        name = row[0].strip().lower()
        if not name:
            continue
        pid = row[1].strip() if len(row) > 1 else "?"
        found.setdefault(name, []).append(pid)
    return found


def _format_found(found: dict[str, list[str]]) -> str:
    parts: list[str] = []
    for name in _CREO_PROCESS_NAMES:
        pids = found.get(name)
        if pids:
            parts.append(f"{name} (pid {', '.join(pids)})")
    return "; ".join(parts) if parts else "none of xtop.exe, parametric.exe, nmsd.exe"


def _check_workspace() -> CheckResult:
    """Verify the operation-log directory exists and is writable."""
    from .observability import operation_log_path

    log_path = operation_log_path()
    directory = log_path.parent
    probe_path = directory / f".creo-mcp-doctor-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            probe_path.write_text("probe", encoding="utf-8")
        finally:
            probe_path.unlink(missing_ok=True)
    except OSError as exc:
        return CheckResult(
            id="workspace",
            title=_CHECK_TITLES["workspace"],
            status=STATUS_FAIL,
            detail=f"operation log directory {directory} is not writable "
            f"({type(exc).__name__}: {exc}).",
            hint="无法写入日志目录：请设置环境变量 `CREO_MCP_LOG_DIR` 指向有写权限的目录，"
            "或修正该目录权限后重试。",
        )
    return CheckResult(
        id="workspace",
        title=_CHECK_TITLES["workspace"],
        status=STATUS_OK,
        detail=f"writable: {directory} (log file: {log_path.name})",
    )


def _check_broker() -> CheckResult:
    """Report broker liveness without starting or stopping it."""
    from . import broker

    snapshot = broker.status()
    pipe = str(snapshot.get("pipe", ""))
    if snapshot.get("running"):
        ping = snapshot.get("ping") or {}
        return CheckResult(
            id="broker",
            title=_CHECK_TITLES["broker"],
            status=STATUS_OK,
            detail=f"broker running (pid {ping.get('pid', 'unknown')}) on {pipe}",
        )
    if snapshot.get("state"):
        return CheckResult(
            id="broker",
            title=_CHECK_TITLES["broker"],
            status=STATUS_WARN,
            detail=f"broker is not running but a stale state file exists; pipe {pipe}",
            hint="代理未运行但存在残留状态：下一次工具调用会自动重启代理；若持续失败，"
            "请删除 %LOCALAPPDATA%\\CreoMCP\\broker.json 或重启终端。",
        )
    return CheckResult(
        id="broker",
        title=_CHECK_TITLES["broker"],
        status=STATUS_OK,
        detail=f"broker not running (starts automatically on the first tool call); pipe {pipe}",
    )


def _check_creo_probe(*, probe_creo: bool, timeout_seconds: int) -> CheckResult:
    """Optionally run the server's isolated status probe against live Creo."""
    if not probe_creo:
        return CheckResult(
            id="creo_probe",
            title=_CHECK_TITLES["creo_probe"],
            status=STATUS_SKIP,
            detail="not requested",
        )
    timeout = min(max(int(timeout_seconds), 1), _MAX_PROBE_TIMEOUT_SECONDS)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _PROBE_SCRIPT],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            id="creo_probe",
            title=_CHECK_TITLES["creo_probe"],
            status=STATUS_FAIL,
            detail=f"isolated status probe timed out after {timeout} seconds.",
            hint="Creo 连接探针超时：请确认 Creo Parametric 没有弹窗或长时间计算，稍后重试，"
            "或通过 `--doctor-timeout` 调大超时时间。",
        )
    except OSError as exc:
        return CheckResult(
            id="creo_probe",
            title=_CHECK_TITLES["creo_probe"],
            status=STATUS_FAIL,
            detail=f"could not start probe subprocess ({type(exc).__name__}: {exc}).",
            hint=_PROBE_FAIL_HINT,
        )

    if completed.returncode != 0:
        reason = _probe_failure_reason(completed.stderr, completed.returncode)
        return CheckResult(
            id="creo_probe",
            title=_CHECK_TITLES["creo_probe"],
            status=STATUS_FAIL,
            detail=f"isolated status probe failed: {reason}",
            hint=_PROBE_FAIL_HINT,
        )

    try:
        last_line = completed.stdout.strip().splitlines()[-1]
        payload = json.loads(last_line)
    except (IndexError, ValueError):
        return CheckResult(
            id="creo_probe",
            title=_CHECK_TITLES["creo_probe"],
            status=STATUS_FAIL,
            detail="isolated status probe exited successfully but produced no JSON status.",
            hint=_PROBE_FAIL_HINT,
        )
    if not isinstance(payload, dict):
        return CheckResult(
            id="creo_probe",
            title=_CHECK_TITLES["creo_probe"],
            status=STATUS_FAIL,
            detail=f"isolated status probe returned {type(payload).__name__} instead of an object.",
            hint=_PROBE_FAIL_HINT,
        )
    active_model = payload.get("active_model") or {}
    active_name = active_model.get("file_name") if isinstance(active_model, dict) else None
    return CheckResult(
        id="creo_probe",
        title=_CHECK_TITLES["creo_probe"],
        status=STATUS_OK,
        detail=(
            f"connected={payload.get('connected')}; loadpoint={payload.get('creo_loadpoint')}; "
            f"working_directory={payload.get('working_directory')}; "
            f"active_model={active_name or 'none'}"
        ),
    )


def _probe_failure_reason(stderr: str | None, returncode: int) -> str:
    """Extract the last meaningful stderr line from a failed probe."""
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return f"probe exited with code {returncode} without a message"
    return _one_line(lines[-1])
