import errno
import socket
from unittest.mock import Mock

import pytest

from clients.external_agent.transport import assert_port_available, retention_step


@pytest.mark.parametrize("name", ["grafana-forward", "prometheus-forward"])
def test_forward_recovery_oserror_retains_other_owned_children_and_fails_closed(name):
    states = {name: False, "other-tunnel": True}
    guard = Mock()
    invalidate = Mock()
    restart = Mock(side_effect=OSError(errno.EADDRINUSE, "private-details"))
    row = retention_step(lambda: states, restart, guard, invalidate)
    assert row["phase"] == "retained_degraded_no_replay"
    guard.assert_not_called()
    invalidate.assert_called_once()
    assert row["failed_children"] == [name]
    assert row["recovery_errors"][0]["errno"] == errno.EADDRINUSE and "private-details" not in str(row)


@pytest.mark.parametrize("name", ["grafana-tunnel", "prometheus-gateway"])
def test_dead_nonforward_never_auto_substitutes_url_or_tears_down_others(name):
    restart = Mock()
    guard = Mock()
    invalidate = Mock()
    row = retention_step(lambda: {name: False, "other-forward": True}, restart, guard, invalidate)
    assert row["phase"] == "retained_degraded_no_replay"
    restart.assert_not_called()
    guard.assert_not_called()
    invalidate.assert_called_once()


def test_origin_guard_failure_keeps_live_children_and_invalidates_authority():
    invalidate = Mock()
    row = retention_step(
        lambda: {"grafana-forward": True, "grafana-tunnel": True},
        Mock(),
        Mock(side_effect=RuntimeError("private-source-details")),
        invalidate,
    )
    assert row["phase"] == "retained_waiting_for_origin_guard"
    invalidate.assert_called_once()
    assert "private-source" not in str(row)


def test_successful_exact_owned_forward_recovery_is_guarded_before_reuse():
    state = {"grafana-forward": False, "grafana-tunnel": True}
    guard = Mock()
    invalidate = Mock()

    def restart(name):
        state[name] = True

    row = retention_step(lambda: state, restart, guard, invalidate)
    assert row["phase"] == "retained_running"
    guard.assert_called_once()
    invalidate.assert_not_called()


def test_port_preflight_rejects_existing_foreign_listener():
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen()
        with pytest.raises(OSError):
            assert_port_available(server.getsockname()[1])


def test_port_preflight_does_not_treat_own_timewait_as_foreign_listener():
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.listen()
    client = socket.create_connection(("127.0.0.1", port))
    accepted, _ = server.accept()
    accepted.close()
    assert client.recv(1) == b""
    client.close()
    server.close()
    assert_port_available(port)
