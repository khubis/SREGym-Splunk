import json
import logging
from types import SimpleNamespace

import pytest
from test_splunk import RUN_ID, SIGNALS, FakeBackend, attempt_context, prepared_provider, reliability_policy, snapshot

from sregym.conductor.conductor import Conductor
from sregym.observability.base import ApplicationScope, ProviderError, serialize_provider_artifact
from sregym.observability.splunk import SplunkBackendError


def test_unavailable_intermediate_snapshot_is_retained_without_repairing_delivery():
    backend = FakeBackend()
    error = SplunkBackendError(status_code=None, transient=True)
    error.args = ("credential-secret must never be copied",)
    backend.snapshot_outcomes = [snapshot(queue=0), snapshot(queue=1), error, snapshot(queue=0)]
    backend.drain_outcomes = [dict.fromkeys(SIGNALS, 1), dict.fromkeys(SIGNALS, 0)]
    provider = prepared_provider(backend, policy=reliability_policy(max_attempts=2))
    context = attempt_context()
    scope = ApplicationScope("hotel-reservation", ("hotel-reservation",))
    provider.wait_until_queryable(context, scope)
    delivery = provider.finish_attempt(context, scope)
    saved = serialize_provider_artifact(delivery)
    assert delivery.valid is False
    assert delivery.drained is True
    assert delivery.queue_high_water == dict.fromkeys(SIGNALS, None)
    assert delivery.queue_final_size == dict.fromkeys(SIGNALS, 0)
    rows = saved["counter_observations"]["observations"]
    snapshots = [r for r in rows if r["operation"] == "collector_snapshot"]
    assert [r["outcome"] for r in snapshots] == ["observed", "observed", "backend_error", "observed"]
    assert snapshots[2]["status_code"] is None
    assert snapshots[2]["transient"] is True
    assert snapshots[2]["strict"] is False
    assert snapshots[2]["attempt"] == 1
    assert all(r["checked_at"] and r["elapsed_ms"] >= 0 for r in rows)
    assert "credential-secret" not in json.dumps(saved)
    assert backend.snapshot_calls == [RUN_ID] * 4
    assert len(backend.drain_calls) == 2


def test_successful_snapshot_and_queue_minimum_are_captured_without_extra_queries():
    backend = FakeBackend()
    backend.snapshot_outcomes = [snapshot(sent=10), snapshot(sent=20, queue=1), snapshot(sent=25)]
    backend.drain_outcomes = [dict.fromkeys(SIGNALS, 0)]
    provider = prepared_provider(backend)
    context = attempt_context()
    scope = ApplicationScope("example", ("example",))
    provider.wait_until_queryable(context, scope)
    delivery = provider.finish_attempt(context, scope)
    evidence = serialize_provider_artifact(delivery)["counter_observations"]
    assert delivery.valid is True
    assert evidence["dropped"] == 0
    assert [r["operation"] for r in evidence["observations"]] == [
        "collector_snapshot",
        "collector_snapshot",
        "collector_queue_minimum",
        "collector_snapshot",
    ]
    assert evidence["observations"][2]["counters"]["queue_size"] == dict.fromkeys(SIGNALS, 0)
    assert backend.snapshot_calls == [RUN_ID] * 3
    assert backend.drain_calls == [RUN_ID]


def test_strict_fatal_snapshot_error_remains_observable_after_the_original_failure():
    backend = FakeBackend()
    backend.snapshot_outcomes = [SplunkBackendError(status_code=403, transient=False)]
    provider = prepared_provider(backend)
    context = attempt_context()
    with pytest.raises(ProviderError) as raised:
        provider.wait_until_queryable(context, ApplicationScope("example", ("example",)))
    assert raised.value.kind == "permission"
    rows = provider.counter_observation_evidence(context)["observations"]
    assert len(rows) == 1
    assert rows[0]["status_code"] == 403
    assert rows[0]["transient"] is False
    assert rows[0]["strict"] is True


def test_counter_observation_capture_is_bounded_and_returns_independent_copies():
    backend = FakeBackend()
    provider = prepared_provider(backend)
    context = attempt_context()
    for _ in range(260):
        provider._snapshot_with_retry(context, strict=False, attempts=1)
    evidence = provider.counter_observation_evidence(context)
    assert len(evidence["observations"]) == 256
    assert evidence["dropped"] == 4
    evidence["observations"].clear()
    assert len(provider.counter_observation_evidence(context)["observations"]) == 256
    assert backend.snapshot_calls == [RUN_ID] * 260


def test_partial_snapshot_values_are_recorded_as_unknown_not_zero():
    backend = FakeBackend()
    backend.snapshot_outcomes = [snapshot(sent=None, queue=None)]
    provider = prepared_provider(backend)
    context = attempt_context()
    provider._snapshot_with_retry(context, strict=False, attempts=1)
    row = provider.counter_observation_evidence(context)["observations"][0]
    assert row["outcome"] == "observed"
    assert row["counters"]["queue_size"] == dict.fromkeys(SIGNALS, None)
    assert row["counters"]["sent"] == dict.fromkeys(SIGNALS, None)


def test_queue_minimum_failure_is_retained_with_unchanged_failed_delivery():
    backend = FakeBackend()
    backend.snapshot_outcomes = [snapshot(), snapshot(queue=1)]
    backend.drain_outcomes = [SplunkBackendError(status_code=403, transient=False)]
    provider = prepared_provider(backend)
    context = attempt_context()
    scope = ApplicationScope("example", ("example",))
    provider.wait_until_queryable(context, scope)
    delivery = provider.finish_attempt(context, scope)
    rows = serialize_provider_artifact(delivery)["counter_observations"]["observations"]
    assert delivery.valid is False
    assert delivery.drained is False
    assert rows[-1]["operation"] == "collector_queue_minimum"
    assert rows[-1]["status_code"] == 403
    assert rows[-1]["transient"] is False
    assert backend.drain_calls == [RUN_ID]


def test_transient_retry_retains_both_attempts_and_existing_recovery():
    backend = FakeBackend()
    backend.snapshot_outcomes = [SplunkBackendError(status_code=503, transient=True, retry_after_seconds=2), snapshot()]
    provider = prepared_provider(backend)
    context = attempt_context()
    result = provider._snapshot_with_retry(context, strict=False, attempts=2)
    rows = provider.counter_observation_evidence(context)["observations"]
    assert result.queue_size == dict.fromkeys(SIGNALS, 0)
    assert [r["attempt"] for r in rows] == [1, 2]
    assert rows[0]["status_code"] == 503
    assert rows[0]["retry_after_seconds"] == 2
    assert rows[1]["outcome"] == "observed"
    assert backend.snapshot_calls == [RUN_ID] * 2


@pytest.mark.parametrize("transient", [False, True])
def test_native_conductor_persists_strict_snapshot_failure_before_delivery_exists(tmp_path, transient):
    backend = FakeBackend()
    backend.snapshot_outcomes = [SplunkBackendError(status_code=503 if transient else 403, transient=transient)] * 3
    provider = prepared_provider(backend)
    conductor = Conductor.__new__(Conductor)
    conductor.problem_id = "example"
    conductor.logger = logging.getLogger("collector-observation-test")
    conductor.app = SimpleNamespace(app_name="example", namespace="example")
    conductor._observability_provider = provider
    conductor._observability_context = attempt_context()
    conductor.observability_readiness = None
    conductor.observability_delivery = None
    path = tmp_path / "phases_attempt1.jsonl"
    conductor.bind_phase_ledger(path, attempt=1)
    with pytest.raises(ProviderError) as raised:
        conductor.wait_for_observability()
    assert raised.value.kind == ("transient_exhausted" if transient else "permission")
    assert conductor.observability_delivery is None
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    captured = next(row for row in rows if row["phase"] == "collector_observations")
    assert len(captured["observations"]) == (3 if transient else 1)
    assert all(row["outcome"] == "backend_error" for row in captured["observations"])
    assert captured["dropped"] == 0
