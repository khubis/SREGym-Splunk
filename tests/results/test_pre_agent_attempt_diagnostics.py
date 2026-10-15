import json
from datetime import UTC, datetime

import pytest

from sregym.observability.splunk import SplunkBackendError
from sregym.results.splunk_lite_causal import CaseGateError, _wait_for_pre_agent


def test_failed_gate_preserves_prior_outcomes_and_allocated_deadlines_without_payloads():
    calls = []

    def verify(end, timeout):
        calls.append(timeout)
        if len(calls) == 2:
            return {"status": "not_ready", "private_payload": "must-not-copy"}
        error = SplunkBackendError(status_code=503 if len(calls) == 1 else None, transient=True)
        error.args = ("must-not-copy",)
        raise error

    with pytest.raises(CaseGateError) as caught:
        _wait_for_pre_agent(
            verify,
            now=lambda: datetime(2026, 10, 15, tzinfo=UTC),
            sleep=lambda _: None,
            monotonic=lambda: 0.0,
            attempts=3,
            interval_seconds=1,
            max_wait_seconds=30,
        )
    diagnostics = caught.value.attempt_diagnostics
    assert diagnostics["attempt_count"] == 3
    assert diagnostics["dropped"] == 0
    rows = diagnostics["observations"]
    assert [row["status"] for row in rows] == ["query_error", "not_ready", "query_error"]
    assert [row["attempt"] for row in rows] == [1, 2, 3]
    assert rows[0]["http_status"] == 503
    assert "http_status" not in rows[-1]
    assert all(row["request_timeout_seconds"] == timeout for row, timeout in zip(rows, calls))  # noqa: B905
    assert all(row["elapsed_ms"] >= 0 for row in rows)
    assert "must-not-copy" not in json.dumps(diagnostics)
    assert caught.value.proof == {"status": "query_error", "schema": "sregym.splunk_lite_pre_agent.v1"}


def test_attempt_history_retains_first_and_latest_outcomes_with_bounded_size():
    with pytest.raises(CaseGateError) as caught:
        _wait_for_pre_agent(
            lambda *_: {"status": "not_ready"},
            now=lambda: datetime(2026, 10, 15, tzinfo=UTC),
            sleep=lambda _: None,
            monotonic=lambda: 0.0,
            attempts=90,
            interval_seconds=1,
            max_wait_seconds=100,
        )
    diagnostics = caught.value.attempt_diagnostics
    assert diagnostics["attempt_count"] == 90
    assert len(diagnostics["observations"]) == 60
    assert diagnostics["dropped"] == 30
    assert diagnostics["observations"][0]["attempt"] == 1
    assert diagnostics["observations"][-1]["attempt"] == 90
