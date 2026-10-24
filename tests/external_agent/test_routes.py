from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from clients.external_agent.driver import Hold
from clients.external_agent.routes import validate_routes


def fixture():
    routes = {
        "urls": {"grafana": "https://one.trycloudflare.com", "prometheus": "https://two.trycloudflare.com"},
        "processes": {
            "controller": {"pid": 1, "cwd": "/owned/controller", "full_ps": "exact"},
            "grafana-forward": {"pid": 2, "cwd": "/owned", "full_ps": "child"},
        },
    }
    state = {
        "phase": "retained_running",
        "heartbeat_at": datetime.fromtimestamp(1000, UTC).isoformat(),
        "controller_pid": 1,
        "children": {"grafana-forward": {"pid": 2}},
        "urls": deepcopy(routes["urls"]),
    }
    return routes, state, {"valid_until": 1080}


def test_fresh_exact_live_routes_are_required():
    r, s, g = fixture()
    check = Mock()
    assert validate_routes(r, s, g, clock=1000, check_process=check) == r["urls"]
    assert check.call_count == 2


@pytest.mark.parametrize("phase", ["held_exited", "retained_degraded_no_replay", "retained_waiting_for_origin_guard"])
def test_degraded_or_exited_runtime_is_not_admitted(phase):
    r, s, g = fixture()
    s["phase"] = phase
    with pytest.raises(Hold):
        validate_routes(r, s, g, clock=1000, check_process=Mock())


@pytest.mark.parametrize("age", [-6, 71])
def test_stale_or_future_heartbeat_is_rejected(age):
    r, s, g = fixture()
    with pytest.raises(Hold):
        validate_routes(r, s, g, clock=1000 + age, check_process=Mock())


def test_expired_guard_blocks_routes():
    r, s, g = fixture()
    g["valid_until"] = 999
    with pytest.raises(Hold):
        validate_routes(r, s, g, clock=1000, check_process=Mock())


def test_replaced_pid_is_not_adopted():
    r, s, g = fixture()
    s["children"]["grafana-forward"]["pid"] = 3
    with pytest.raises(Hold):
        validate_routes(r, s, g, clock=1000, check_process=Mock())


def test_live_full_identity_failure_is_not_hidden():
    r, s, g = fixture()
    check = Mock(side_effect=Hold("process replaced"))
    with pytest.raises(Hold):
        validate_routes(r, s, g, clock=1000, check_process=check)


def test_changed_url_is_not_substituted():
    r, s, g = fixture()
    s["urls"]["grafana"] = "https://other.trycloudflare.com"
    with pytest.raises(Hold):
        validate_routes(r, s, g, clock=1000, check_process=Mock())


def test_foreign_domain_is_rejected_even_if_state_matches():
    r, s, g = fixture()
    s["urls"]["grafana"] = r["urls"]["grafana"] = "https://foreign.invalid"
    with pytest.raises(Hold):
        validate_routes(r, s, g, clock=1000, check_process=Mock())
