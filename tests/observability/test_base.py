import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import get_args

import pytest

from sregym.observability import create_provider
from sregym.observability.base import (
    SIGNAL_NAMES,
    ApplicationScope,
    AttemptContext,
    DeliveryReport,
    ExternalOtlpExport,
    FailureKind,
    ProviderError,
    ReadinessReport,
    SignalReadiness,
    new_run_id,
    serialize_provider_artifact,
)
from sregym.run_artifacts import RunArtifacts

RUN_ID = "anon_0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


def attempt_context(run_id: str = RUN_ID) -> AttemptContext:
    return AttemptContext(
        run_id=run_id,
        profile="full",
        comparable=True,
        attempt_started_at=NOW,
    )


def application_scope() -> ApplicationScope:
    return ApplicationScope(app_name="social-network", namespaces=("social-network",))


def readiness_report(run_id: str = RUN_ID) -> ReadinessReport:
    return ReadinessReport(
        run_id=run_id,
        signals=tuple(
            SignalReadiness(signal=signal, ready=True, checked_at=NOW, evidence={"count": 1})
            for signal in SIGNAL_NAMES
        ),
        ready=True,
    )


def test_new_run_id_has_strict_opaque_shape_and_is_unique():
    first = new_run_id()
    second = new_run_id()

    assert re.fullmatch(r"anon_[0-9a-f]{32}", first)
    assert re.fullmatch(r"anon_[0-9a-f]{32}", second)
    assert first != second


@pytest.mark.parametrize(
    "run_id",
    [
        "anon_0123456789abcdef0123456789abcde",
        "anon_0123456789abcdef0123456789abcdef0",
        "anon_0123456789ABCDEF0123456789ABCDEF",
        "problem_edge_request_filter_cpu_saturation",
        "../anon_0123456789abcdef0123456789abcdef",
    ],
)
def test_attempt_context_rejects_noncanonical_run_ids(run_id):
    with pytest.raises(ValueError, match="opaque run identity"):
        attempt_context(run_id)


@pytest.mark.parametrize(
    "started_at",
    [
        datetime(2026, 9, 23, 12),
        datetime(2026, 9, 23, 12, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_attempt_context_requires_utc_start_time(started_at):
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        AttemptContext(RUN_ID, "full", True, started_at)


def test_external_export_rejects_noncanonical_run_id():
    with pytest.raises(ValueError, match="opaque run identity"):
        ExternalOtlpExport(
            endpoint="http://collector.observability.svc:4317",
            run_id="case-name",
            resource_attributes={"sregym.run.id": "case-name"},
        )


def test_readiness_report_requires_each_signal_once_and_consistent_summary():
    signals = readiness_report().signals

    with pytest.raises(ValueError, match="exactly once"):
        ReadinessReport(RUN_ID, signals[:-1], ready=True)
    with pytest.raises(ValueError, match="must match"):
        ReadinessReport(RUN_ID, signals, ready=False)


def test_null_provider_is_an_immediately_ready_noop():
    provider = create_provider("none")
    context = attempt_context()
    scope = application_scope()

    assert provider.name == "none"
    assert provider.preflight() is None
    assert provider.prepare_attempt(context) is None

    opening = provider.wait_until_queryable(context, scope)
    assert opening.run_id == RUN_ID
    assert opening.ready is True
    assert tuple(signal.signal for signal in opening.signals) == SIGNAL_NAMES
    assert all(signal.ready for signal in opening.signals)

    delivery = provider.finish_attempt(context, scope)
    assert delivery.opening.ready is True
    assert delivery.closing.ready is True
    assert delivery.drained is True
    assert delivery.valid is True
    for field in (
        delivery.first_visible_lag_ms,
        delivery.sent_delta,
        delivery.send_failed_delta,
        delivery.enqueue_failed_delta,
        delivery.queue_high_water,
        delivery.queue_final_size,
    ):
        assert tuple(field) == SIGNAL_NAMES
        assert set(field.values()) == {None}
    assert provider.close() is None


def test_factory_rejects_unavailable_or_unknown_providers_without_echoing_input():
    with pytest.raises(ProviderError) as unavailable:
        create_provider("splunk")
    assert unavailable.value.kind == "configuration"

    supplied = "unknown-provider-with-sensitive-suffix"
    with pytest.raises(ValueError) as unknown:
        create_provider(supplied)
    assert supplied not in str(unknown.value)


def test_failure_taxonomy_is_closed_and_runtime_validated():
    assert get_args(FailureKind) == (
        "configuration",
        "authentication",
        "permission",
        "transient_exhausted",
        "readiness_timeout",
        "cleanup",
    )
    for kind in get_args(FailureKind):
        assert ProviderError(kind, "safe summary").kind == kind

    with pytest.raises(ValueError, match="failure kind"):
        ProviderError("other", "safe summary")  # type: ignore[arg-type]


def test_provider_artifact_serialization_is_json_safe_and_omits_error_causes():
    report = readiness_report()
    delivery = DeliveryReport(
        run_id=RUN_ID,
        opening=report,
        closing=report,
        first_visible_lag_ms=dict.fromkeys(SIGNAL_NAMES, 10.0),
        sent_delta=dict.fromkeys(SIGNAL_NAMES, 1),
        send_failed_delta=dict.fromkeys(SIGNAL_NAMES, 0),
        enqueue_failed_delta=dict.fromkeys(SIGNAL_NAMES, 0),
        queue_high_water=dict.fromkeys(SIGNAL_NAMES, 2),
        queue_final_size=dict.fromkeys(SIGNAL_NAMES, 0),
        drained=True,
        valid=True,
    )
    export = ExternalOtlpExport(
        endpoint="http://collector.observability.svc:4317",
        run_id=RUN_ID,
        resource_attributes={"sregym.run.id": RUN_ID},
    )
    error = ProviderError("readiness_timeout", "telemetry was not ready", readiness_report=report)
    error.__cause__ = RuntimeError("SPLUNK_HEC_TOKEN=do-not-serialize")

    values = [attempt_context(), application_scope(), report.signals[0], report, delivery, export, error]
    encoded = json.dumps([serialize_provider_artifact(value) for value in values], sort_keys=True)

    assert "do-not-serialize" not in encoded
    assert "SPLUNK_HEC_TOKEN" not in encoded
    assert "2026-09-23T12:00:00Z" in encoded
    assert '"kind": "readiness_timeout"' in encoded


def test_delivery_report_rejects_mixed_attempt_identities():
    report = readiness_report()
    other_id = "anon_abcdef0123456789abcdef0123456789"
    other = readiness_report(other_id)
    empty = dict.fromkeys(SIGNAL_NAMES)

    with pytest.raises(ValueError, match="share one run identity"):
        DeliveryReport(
            run_id=RUN_ID,
            opening=report,
            closing=other,
            first_visible_lag_ms=empty.copy(),
            sent_delta=empty.copy(),
            send_failed_delta=empty.copy(),
            enqueue_failed_delta=empty.copy(),
            queue_high_water=empty.copy(),
            queue_final_size=empty.copy(),
            drained=True,
            valid=True,
        )


def test_provider_artifact_serialization_rejects_unapproved_objects():
    with pytest.raises(TypeError, match="provider artifact"):
        serialize_provider_artifact({"SPLUNK_HEC_TOKEN": "secret"})  # pyright: ignore[reportArgumentType]


def test_run_artifacts_accepts_a_valid_preallocated_identity(tmp_path):
    run = RunArtifacts.create(
        staging_root=tmp_path / "staging",
        results_root=tmp_path / "results",
        problem_id="real_case_name",
        agent="assistant_v3",
        attempt=1,
        artifact_id=RUN_ID,
    )

    assert run.artifact_id == RUN_ID
    assert run.active_dir == tmp_path / "staging" / "assistant_v3" / RUN_ID
    assert run.active_dir.is_dir()
    assert (run.active_dir / "trajectory").is_dir()


def test_run_artifacts_rejects_invalid_or_colliding_preallocated_identity(tmp_path):
    arguments = {
        "staging_root": tmp_path / "staging",
        "results_root": tmp_path / "results",
        "problem_id": "real_case_name",
        "agent": "assistant_v3",
        "attempt": 1,
    }
    with pytest.raises(ValueError, match="opaque run identity"):
        RunArtifacts.create(**arguments, artifact_id="real_case_name")
    assert not (tmp_path / "staging").exists()

    first = RunArtifacts.create(**arguments, artifact_id=RUN_ID)
    with pytest.raises(FileExistsError):
        RunArtifacts.create(**arguments, artifact_id=RUN_ID)
    assert first.active_dir.is_dir()


def test_default_artifact_allocation_uses_distinct_identity_for_resumed_attempt(tmp_path):
    common = {
        "staging_root": tmp_path / "staging",
        "results_root": tmp_path / "results",
        "problem_id": "real_case_name",
        "agent": "assistant_v3",
    }
    first = RunArtifacts.create(**common, attempt=1)
    resumed = RunArtifacts.create(**common, attempt=2)

    assert first.artifact_id != resumed.artifact_id
    assert re.fullmatch(r"anon_[0-9a-f]{32}", first.artifact_id)
    assert re.fullmatch(r"anon_[0-9a-f]{32}", resumed.artifact_id)
    assert first.final_dir != resumed.final_dir
