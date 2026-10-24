from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest

import clients.external_agent.routes as routes
from clients.external_agent.driver import Hold
from clients.external_agent.telemetry import TelemetryPending, wait_for_telemetry

from .test_routes import fixture


def sample():
    r, s, g = fixture()
    r["forward_recipes"] = {
        "grafana-forward": {
            "command": "kubectl fixed forward",
            "command_sha256": "expected",
            "cwd": "/owned",
            "port": 28300,
            "controller_started_at": 900,
        }
    }
    s["children"]["grafana-forward"] = {
        "pid": 3,
        "command_sha256": "expected",
        "started_at": datetime.fromtimestamp(950, UTC).isoformat(),
    }
    return r, s, g


def test_owned_forward_replacement_requires_new_full_identity_check():
    r, s, g = sample()
    check = Mock()
    forward = Mock()
    assert routes.validate_routes(r, s, g, clock=1000, check_process=check, check_forward=forward) == r["urls"]
    assert forward.call_count == 1 and check.call_count == 1
    assert forward.call_args.args[0]["pid"] == 3


@pytest.mark.parametrize("change", ["command_hash", "parent_id", "future_start", "port_name"])
def test_changed_forward_runtime_or_recipe_is_rejected(change):
    r, s, g = sample()
    if change == "command_hash":
        s["children"]["grafana-forward"]["command_sha256"] = "wrong"
    if change == "parent_id":
        s["controller_pid"] = 99
    if change == "future_start":
        s["children"]["grafana-forward"]["started_at"] = datetime.fromtimestamp(2000, UTC).isoformat()
    if change == "port_name":
        r["forward_recipes"]["foreign-forward"] = r["forward_recipes"].pop("grafana-forward")
    with pytest.raises(Hold):
        routes.validate_routes(r, s, g, clock=1000, check_process=Mock(), check_forward=Mock())


def test_forward_live_inspection_cannot_be_omitted_or_fail_open():
    r, s, g = sample()
    with pytest.raises(Hold):
        routes.validate_routes(r, s, g, clock=1000, check_process=Mock())
    with pytest.raises(Hold):
        routes.validate_routes(
            r, s, g, clock=1000, check_process=Mock(), check_forward=Mock(side_effect=Hold("foreign listener"))
        )


def evidence():
    child = {"pid": 3, "started_at": datetime.fromtimestamp(950, UTC).isoformat(), "command_sha256": "expected"}
    recipe = {
        "command": "kubectl fixed forward",
        "command_sha256": "expected",
        "cwd": "/owned",
        "port": 28300,
        "controller_started_at": 900,
    }
    return (
        child,
        recipe,
        {
            "pid": 3,
            "ppid": 1,
            "pgid": 3,
            "start_unix": 950,
            "command": "kubectl fixed forward",
            "cwd": "/owned",
            "listeners": ["127.0.0.1:28300"],
        },
    )


def test_exact_owner_command_cwd_start_and_loopback_listener_required():
    c, r, e = evidence()
    routes.validate_forward(c, r, 1, 1000, e)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", 4),
        ("ppid", 77),
        ("pgid", 4),
        ("start_unix", 949),
        ("command", "kubectl different"),
        ("cwd", "/foreign"),
        ("listeners", []),
        ("listeners", ["*:28300"]),
        ("listeners", ["127.0.0.1:28302"]),
    ],
)
def test_foreign_reused_or_wrong_listener_forward_rejected(field, value):
    c, r, e = evidence()
    e[field] = value
    with pytest.raises(Hold):
        routes.validate_forward(c, r, 1, 1000, e)


def test_origin_pending_does_not_admit_data_or_bypass_command_metadata():
    r, s, g = sample()
    s["phase"] = "retained_waiting_for_origin_guard"
    forward = Mock()
    with pytest.raises(TelemetryPending):
        routes.validate_routes(r, s, g, clock=1000, check_process=Mock(), check_forward=forward)
    assert forward.call_count == 0
    s["children"]["grafana-forward"]["command_sha256"] = "foreign"
    with pytest.raises(Hold):
        routes.validate_routes(r, s, g, clock=1000, check_process=Mock(), check_forward=forward)


@pytest.mark.parametrize(
    "body,status",
    [(b'{"error":"upstream unavailable"}', 502), (b'{"error":"upstream identity not currently verified"}', 503)],
)
def test_only_known_owned_gateway_pending_shapes_wait(body, status):
    response = httpx.Response(
        status,
        content=body,
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://one.trycloudflare.com/api/v1/query"),
    )
    with pytest.raises(TelemetryPending):
        routes.telemetry_status(response)


@pytest.mark.parametrize(
    "status,body",
    [
        (401, b'{"error":"upstream unavailable"}'),
        (403, b'{"error":"upstream unavailable"}'),
        (502, b"cloudflare error"),
        (503, b'{"error":"upstream viewer token expired"}'),
        (500, b'{"error":"upstream unavailable"}'),
    ],
)
def test_other_status_auth_unknown_or_expired_credentials_are_immediate(status, body):
    response = httpx.Response(
        status,
        content=body,
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "https://one.trycloudflare.com/api/v1/query"),
    )
    with pytest.raises(httpx.HTTPStatusError):
        routes.telemetry_status(response)


def test_known_gateway_recovery_stays_within_same_setup_deadline_and_requires_success():
    time = [0]
    observed = []
    calls = [0]

    def probe():
        calls[0] += 1
        if calls[0] < 3:
            raise TelemetryPending("telemetry_gateway_origin_unavailable")
        return {"same_live_pod_uids": True}

    def sleep(n):
        time[0] += n

    assert wait_for_telemetry(probe, observed.append, clock=lambda: time[0], sleep=sleep, limit=5) == {
        "same_live_pod_uids": True
    }
    assert observed[-1]["phase"] == "ready" and calls[0] == 3
    calls[0] = 0
    time[0] = 0
    with pytest.raises(Hold):
        wait_for_telemetry(
            lambda: (_ for _ in ()).throw(TelemetryPending("telemetry_gateway_origin_unavailable")),
            observed.append,
            clock=lambda: time[0],
            sleep=sleep,
            limit=5,
        )
    assert time[0] == 5 and observed[-1]["phase"] == "held"
