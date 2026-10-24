from unittest.mock import Mock

import pytest

from clients.external_agent.driver import Hold
from clients.external_agent.readiness import guard_with_wait


def run(probe, limit=12):
    now = [0]
    records = []

    def sleep(value):
        now[0] += value

    value = guard_with_wait(probe, records.append, clock=lambda: now[0], sleep=sleep, limit=limit, interval=5)
    return value, records, now[0]


@pytest.mark.parametrize(
    "message", ["An original protected Pod lost readiness", "An unrelated original Pod lost readiness"]
)
def test_actual_ordinary_pod_recovery_required_before_one_mutation(message):
    probe = Mock(side_effect=[Hold(message), {"same_strict_guard": True}])
    operation = Mock()
    value, records, elapsed = run(probe)
    operation()
    assert (
        value == {"same_strict_guard": True}
        and probe.call_count == 2
        and elapsed == 5
        and records[-1]["phase"] == "ready"
    )
    operation.assert_called_once()


def test_sustained_unreadiness_holds_without_mutation_or_gate_override():
    clock = [0]
    records = []
    operation = Mock()
    probe = Mock(side_effect=Hold("An original protected Pod lost readiness"))
    with pytest.raises(Hold, match="strict preservation readiness deadline"):
        guard_with_wait(
            probe,
            records.append,
            clock=lambda: clock[0],
            sleep=lambda x: clock.__setitem__(0, clock[0] + x),
            limit=12,
            interval=5,
        )
        operation()
    assert clock[0] == 12 and probe.call_count == 3 and records[-1]["phase"] == "held"
    operation.assert_not_called()


@pytest.mark.parametrize(
    "message",
    [
        "objects identity or state changed",
        "Shared helper process changed",
        "namespace identity changed",
        "All four original Kind nodes must be Ready",
        "Current CoreDNS Pod is not ready",
        "CoreDNS changed outside the exact original fault transformation",
    ],
)
def test_identity_node_or_native_dns_failures_are_never_pending(message):
    probe = Mock(side_effect=Hold(message))
    with pytest.raises(Hold, match=message):
        run(probe)
    assert probe.call_count == 1


def test_late_ready_response_never_authorizes_work():
    clock = [0]
    operation = Mock()

    def slow():
        clock[0] = 13
        return {"ready": True}

    with pytest.raises(Hold, match="strict preservation readiness deadline"):
        guard_with_wait(slow, lambda _: None, clock=lambda: clock[0], sleep=lambda _: None, limit=12)
        operation()
    operation.assert_not_called()
