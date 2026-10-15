import json
import logging
from dataclasses import replace
from types import SimpleNamespace

from test_splunk import FakeBackend, attempt_context, prepared_provider

from sregym.conductor.conductor import Conductor


def test_shared_campaign_provider_keeps_current_capture_and_preserves_completed_attempt_ledgers(tmp_path):
    provider = prepared_provider(FakeBackend())
    archived = {}
    previous = None
    for number in range(32):
        context = replace(attempt_context(), run_id=f"anon_{number:032x}")
        provider.prepare_attempt(context)
        conductor = Conductor.__new__(Conductor)
        conductor.problem_id = "example"
        conductor.logger = logging.getLogger("observation-retention-test")
        conductor.app = SimpleNamespace(app_name="example", namespace="example")
        conductor._observability_provider = provider
        conductor._observability_context = context
        conductor.observability_readiness = None
        conductor.observability_delivery = None
        path = tmp_path / f"phases_{number}.jsonl"
        conductor.bind_phase_ledger(path, attempt=1)
        conductor.wait_for_observability()
        delivery = conductor.finish_observability()
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        captures = [row for row in rows if row["phase"] == "collector_observations"]
        assert delivery.valid is True
        assert len(captures[-1]["observations"]) == 2
        assert provider.counter_observation_evidence(context)["observations"]
        assert len(provider._reliability) == 1
        if previous is not None:
            assert provider.counter_observation_evidence(previous) == {"observations": [], "dropped": 0}
        archived[path] = path.read_bytes()
        previous = context
    assert all(path.read_bytes() == payload for path, payload in archived.items())


def test_idempotent_preparation_preserves_current_capture_and_new_attempt_resets_drop_accounting():
    provider = prepared_provider(FakeBackend())
    first = attempt_context()
    for _ in range(260):
        provider._snapshot_with_retry(first, strict=False, attempts=1)
    detached = provider.counter_observation_evidence(first)
    provider.prepare_attempt(first)
    assert provider.counter_observation_evidence(first) == detached
    second = replace(first, run_id="anon_22222222222222222222222222222222")
    provider.prepare_attempt(second)
    provider._snapshot_with_retry(second, strict=False, attempts=1)
    assert provider.counter_observation_evidence(first) == {"observations": [], "dropped": 0}
    assert len(provider.counter_observation_evidence(second)["observations"]) == 1
    assert provider.counter_observation_evidence(second)["dropped"] == 0
    assert len(detached["observations"]) == 256
    assert detached["dropped"] == 4
