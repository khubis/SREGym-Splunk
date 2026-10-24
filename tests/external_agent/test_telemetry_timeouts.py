from unittest.mock import Mock

import httpx
import pytest

from clients.external_agent.driver import Hold
from clients.external_agent.telemetry import TelemetryPending, wait_for_telemetry


def clocked(probe, limit=5):
    time = [0]
    records = []

    def sleep(seconds):
        time[0] += seconds

    value = wait_for_telemetry(probe, records.append, clock=lambda: time[0], sleep=sleep, limit=limit)
    return value, records, time[0]


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.WriteTimeout, httpx.PoolTimeout])
def test_transient_timeout_then_actual_fresh_predicate_must_pass(error_type):
    probe = Mock(side_effect=[error_type("private-url-token-must-not-leak"), {"fresh_live_uid_logs": True}])
    value, records, elapsed = clocked(probe)
    assert probe.call_count == 2 and value["fresh_live_uid_logs"] and elapsed == 2
    assert records[-1]["phase"] == "ready"
    assert records[0]["observations"][0]["reason"] == "telemetry_transport_timeout"
    assert records[0]["observations"][0]["error_type"] == error_type.__name__
    assert "private-url-token" not in str(records)


def test_repeated_transport_timeout_is_bounded_and_never_ready():
    current = [0]
    records = []
    probe = Mock(side_effect=httpx.ReadTimeout("timeout"))
    with pytest.raises(Hold, match="setup deadline"):
        wait_for_telemetry(
            probe,
            records.append,
            clock=lambda: current[0],
            sleep=lambda t: current.__setitem__(0, current[0] + t),
            limit=5,
        )
    assert current[0] == 5 and probe.call_count == 3 and records[-1]["phase"] == "held"
    assert all("accepted" not in x for x in records)


@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_http_status_failure_is_not_transport_pending(status):
    req = httpx.Request("GET", "https://example.invalid")
    resp = httpx.Response(status, request=req)
    probe = Mock(side_effect=httpx.HTTPStatusError("status", request=req, response=resp))
    with pytest.raises(httpx.HTTPStatusError):
        clocked(probe)
    assert probe.call_count == 1


def test_timeout_then_stale_uid_is_still_rejected_at_deadline():
    probe = Mock(
        side_effect=[
            httpx.ReadTimeout("timeout"),
            TelemetryPending("metric_uid_outside_live_cohort"),
            TelemetryPending("metric_uid_outside_live_cohort"),
        ]
    )
    with pytest.raises(Hold, match="setup deadline"):
        clocked(probe)
    assert probe.call_count == 3
