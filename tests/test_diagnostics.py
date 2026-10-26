"""Tests for the Creo MCP preflight diagnostics (``--doctor``).

Nothing here starts Creo, opens a network connection, or writes outside the
operation-log probe file. Environment-specific checks are either exercised
through their fail-safe wrapper or replaced with deterministic results.
"""

from __future__ import annotations

import json

import pytest

from creo_mcp import diagnostics
from creo_mcp.diagnostics import (
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_WARN,
    CheckResult,
    DoctorReport,
    run_doctor,
)

EXPECTED_CHECK_IDS = (
    "platform",
    "python",
    "pywin32",
    "mcp",
    "creo_install",
    "com_registration",
    "creo_process",
    "workspace",
    "broker",
    "creo_probe",
)


def _make_check(check_id: str, status: str, detail: str = "detail") -> CheckResult:
    return CheckResult(id=check_id, title=f"Title {check_id}", status=status, detail=detail)


def _check_by_id(report: DoctorReport, check_id: str) -> CheckResult:
    matching = [check for check in report.checks if check.id == check_id]
    assert len(matching) == 1
    return matching[0]


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ((), STATUS_OK),
        ((STATUS_OK,), STATUS_OK),
        ((STATUS_OK, STATUS_SKIP), STATUS_SKIP),
        ((STATUS_OK, STATUS_WARN), STATUS_WARN),
        ((STATUS_SKIP, STATUS_FAIL), STATUS_FAIL),
        ((STATUS_OK, STATUS_WARN, STATUS_SKIP, STATUS_FAIL), STATUS_FAIL),
    ],
)
def test_status_aggregation_prefers_worst_status(
    statuses: tuple[str, ...], expected: str
) -> None:
    report = DoctorReport(
        checks=tuple(_make_check(f"check-{index}", status) for index, status in enumerate(statuses))
    )

    assert report.status == expected


def test_skip_is_not_treated_as_failure() -> None:
    report = DoctorReport(
        checks=(
            _make_check("alpha", STATUS_OK),
            _make_check("beta", STATUS_SKIP),
        )
    )

    assert report.status == STATUS_SKIP
    assert report.status != STATUS_FAIL


def test_render_text_is_ascii_and_stable() -> None:
    report = DoctorReport(
        checks=(
            CheckResult(id="alpha", title="Alpha", status=STATUS_OK, detail="first"),
            CheckResult(
                id="beta",
                title="Beta",
                status=STATUS_WARN,
                detail="second",
                hint="fix beta",
            ),
            CheckResult(id="gamma", title="Gamma", status=STATUS_SKIP, detail="third"),
        )
    )

    rendered = report.render_text()

    assert rendered == (
        "Creo MCP doctor\n"
        "[ok] alpha: first\n"
        "[warn] beta: second\n"
        "  hint: fix beta\n"
        "[skip] gamma: third\n"
        "\n"
        "Summary: ok=1 warn=1 fail=0 skip=1\n"
        "Result: warn"
    )
    assert rendered == report.render_text()
    assert rendered.isascii()


def test_render_text_uses_ascii_separators_for_live_report() -> None:
    report = run_doctor(probe_creo=False)
    rendered = report.render_text()

    assert rendered.startswith("Creo MCP doctor\n")
    assert rendered.splitlines()[-1] == f"Result: {report.status}"
    for separator in ("[", "]", ": ", "  hint: ", "Summary: ", "Result: "):
        assert separator.isascii()
    for check in report.checks:
        assert f"[{check.status}] {check.id}: " in rendered


def test_to_dict_is_json_serialisable() -> None:
    report = DoctorReport(
        checks=(
            CheckResult(
                id="alpha",
                title="Alpha",
                status=STATUS_FAIL,
                detail="broken",
                hint="repair it",
            ),
        )
    )

    payload = report.to_dict()
    decoded = json.loads(json.dumps(payload, ensure_ascii=False))

    assert decoded["status"] == STATUS_FAIL
    assert decoded["checks"] == [
        {
            "id": "alpha",
            "title": "Alpha",
            "status": STATUS_FAIL,
            "detail": "broken",
            "hint": "repair it",
        }
    ]


def test_check_result_rejects_unknown_status() -> None:
    with pytest.raises(ValueError):
        CheckResult(id="alpha", title="Alpha", status="exploded", detail="detail")


def test_unexpected_check_exception_becomes_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> CheckResult:
        raise RuntimeError("simulated registry failure")

    monkeypatch.setattr(diagnostics, "_check_python", _boom)

    report = run_doctor(probe_creo=False)
    check = _check_by_id(report, "python")

    assert check.status == STATUS_WARN
    assert "RuntimeError" in check.detail
    assert "simulated registry failure" in check.detail
    assert check.hint


def test_check_returning_wrong_type_becomes_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diagnostics, "_check_platform", lambda: None)

    report = run_doctor(probe_creo=False)
    check = _check_by_id(report, "platform")

    assert check.status == STATUS_WARN
    assert "NoneType" in check.detail
    assert check.hint


def test_fail_result_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail() -> CheckResult:
        return CheckResult(
            id="workspace",
            title=diagnostics._CHECK_TITLES["workspace"],
            status=STATUS_FAIL,
            detail="simulated unwritable directory",
            hint="fix permissions",
        )

    monkeypatch.setattr(diagnostics, "_check_workspace", _fail)

    report = run_doctor(probe_creo=False)

    assert _check_by_id(report, "workspace").status == STATUS_FAIL
    assert report.status == STATUS_FAIL


def test_run_doctor_without_probe_reports_expected_checks() -> None:
    report = run_doctor(probe_creo=False)

    assert tuple(check.id for check in report.checks) == EXPECTED_CHECK_IDS
    probe = _check_by_id(report, "creo_probe")
    assert probe.status == STATUS_SKIP
    assert probe.detail == "not requested"
    assert probe.hint == ""
    assert report.status in {STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIP}
    json.dumps(report.to_dict())


def test_actionable_checks_carry_remediation_hints() -> None:
    report = run_doctor(probe_creo=False)

    for check in report.checks:
        assert check.status in {STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIP}
        if check.status == STATUS_OK:
            assert check.hint == ""
        if check.status in {STATUS_WARN, STATUS_FAIL}:
            assert check.hint
