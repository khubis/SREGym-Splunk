from unittest.mock import Mock

import pytest

from clients.external_agent.driver import Hold
from clients.external_agent.readiness import wait_for_preservation

READY_ERROR = "An original protected Pod lost readiness"


def run(probe, limit=12):
    current = [0]
    records = []

    def sleep(value):
        current[0] += value

    result = wait_for_preservation(
        probe, records.append, clock=lambda: current[0], sleep=sleep, limit=limit, interval=5
    )
    return result, records, current[0]


def test_true_original_guard_is_required_after_transient_readiness_hold():
    probe = Mock(side_effect=[Hold(READY_ERROR), {"strict": True}])
    value, records, elapsed = run(probe)
    assert value == {"strict": True} and probe.call_count == 2 and elapsed == 5
    assert records[-1]["phase"] == "ready" and records[-1]["observations"][0]["reason"] == "protected_readiness_pending"


def test_repeated_original_guard_failure_stops_at_fixed_deadline():
    probe = Mock(side_effect=Hold(READY_ERROR))
    current = [0]
    records = []
    admit = Mock()
    with pytest.raises(Hold, match="pre-deployment readiness deadline"):
        wait_for_preservation(
            probe,
            records.append,
            clock=lambda: current[0],
            sleep=lambda x: current.__setitem__(0, current[0] + x),
            limit=12,
            interval=5,
        )
        admit()
    assert current[0] == 12 and probe.call_count == 3 and records[-1]["phase"] == "held"
    admit.assert_not_called()


@pytest.mark.parametrize(
    "message",
    [
        "objects identity or state changed",
        "Shared helper process changed",
        "namespace identity changed",
        "All four original Kind nodes must be Ready",
    ],
)
def test_identity_or_other_guards_never_become_pending(message):
    probe = Mock(side_effect=Hold(message))
    with pytest.raises(Hold, match=message):
        run(probe)
    assert probe.call_count == 1


def test_non_hold_error_is_not_retried():
    probe = Mock(side_effect=RuntimeError("private-command-must-not-leak"))
    with pytest.raises(RuntimeError):
        run(probe)
    assert probe.call_count == 1


def test_late_ready_return_is_rejected_without_admission():
    current = [0]
    records = []
    admit = Mock()

    def slow():
        current[0] = 13
        return {"strict": True}

    with pytest.raises(Hold, match="pre-deployment readiness deadline"):
        wait_for_preservation(slow, records.append, clock=lambda: current[0], sleep=lambda _: None, limit=12)
        admit()
    admit.assert_not_called()
    assert records[-1]["phase"] == "held"


def test_guard_still_runs_when_already_ready():
    probe = Mock(return_value={"strict": True})
    value, records, elapsed = run(probe)
    assert probe.call_count == 1 and elapsed == 0 and value == {"strict": True}
    assert records[-1]["phase"] == "ready"
