from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from clients.assistant_v3.client import AssistantEvent
from clients.assistant_v3.driver import (
    CAPABILITY_PROFILE,
    ArtifactError,
    AssistantArtifactBundle,
    AssistantArtifactStore,
    AssistantFailure,
    AssistantRequest,
    AssistantRunMetadata,
    AssistantTerminal,
    derive_metrics,
)
from clients.assistant_v3.prompt import PromptProvenance, RenderedPrompt
from sregym.observability.base import DeliveryReport, ReadinessReport, SignalName, SignalReadiness

RUN_ID = "anon_0123456789abcdef0123456789abcdef"
OTHER_RUN_ID = "anon_ffffffffffffffffffffffffffffffff"
PROMPT = "Investigate the application and diagnose the root cause."
STARTED_AT = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
GOLDEN = Path(__file__).parents[1] / "fixtures" / "assistant_v3" / "golden_infrastructure_invalid"
SECRET = "fixture-secret-that-must-never-be-written"


def prompt() -> RenderedPrompt:
    return RenderedPrompt(
        text=PROMPT,
        provenance=PromptProvenance(
            profile_id="sregym-stratus-diagnosis-v1",
            profile_sha256="1" * 64,
            source_path="agents/stratus/src/stratus/prompts/diagnosis_agent_prompts.yaml",
            source_commit="3f6bd041231715184f962cedb33cfbcefa9b5b3d",
            source_file_sha256="2" * 64,
            reference_sha256="3" * 64,
            template_sha256="4" * 64,
            rendered_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
            substitution_ids=("remove-kubernetes-enumeration",),
        ),
    )


def event(sequence: int, offset_ms: float, name: str, data: dict[str, object], event_id: str | None = None):
    return AssistantEvent(
        sequence=sequence,
        offset_ms=offset_ms,
        event=name,
        event_id=event_id,
        data=data,
        redacted=False,
    )


def events() -> tuple[AssistantEvent, ...]:
    return (
        event(1, 10.0, "ping", {"timestamp": "2026-09-23T12:00:00Z"}),
        event(2, 20.0, "tool.use", {"id": "call-1", "name": "o11y_apm_fetch", "input": {}}, "evt-2"),
        event(3, 25.0, "tool.use", {"id": "call-1", "name": "o11y_apm_fetch", "input": {}}),
        event(4, 30.0, "message.delta", {"text": "same text"}),
        event(5, 35.0, "message.delta", {"text": "same text"}),
        event(
            6,
            40.0,
            "assistant.subagent.action",
            {"action_id": "action-2", "tool_name": "analyze_table", "status": "running"},
        ),
        event(
            7,
            50.0,
            "assistant.subagent.action",
            {"action_id": "action-2", "tool_name": "analyze_table", "status": "complete", "is_error": True},
        ),
        event(8, 60.0, "tool.result", {"tool_use_id": "call-1", "content": "failed", "is_error": True}),
        event(9, 65.0, "tool.result", {"tool_use_id": "call-1", "content": "failed", "is_error": True}),
        event(
            10,
            90.0,
            "assistant.usage",
            {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 25,
                    "reasoning_tokens": None,
                    "total_tokens": 125,
                },
                "session_usage": None,
            },
        ),
        event(11, 100.0, "message.complete", {"final_text": "Root cause: dependency saturation."}),
    )


def readiness(*, ready: bool = True, checked_at: datetime = STARTED_AT, run_id: str = RUN_ID) -> ReadinessReport:
    signals = tuple(
        SignalReadiness(signal=signal, ready=ready, checked_at=checked_at, evidence={"count": 1 if ready else 0})
        for signal in ("metrics", "traces", "logs", "kubernetes_events")
    )
    return ReadinessReport(run_id=run_id, signals=signals, ready=ready)


def invalid_delivery() -> DeliveryReport:
    signal_values: dict[SignalName, int | None] = {
        signal: 1 for signal in ("metrics", "traces", "logs", "kubernetes_events")
    }
    no_failures: dict[SignalName, int | None] = {signal: 0 for signal in signal_values}
    send_failures: dict[SignalName, int | None] = dict(no_failures)
    send_failures["logs"] = 1
    return DeliveryReport(
        run_id=RUN_ID,
        opening=readiness(),
        closing=readiness(checked_at=STARTED_AT + timedelta(seconds=2)),
        first_visible_lag_ms={signal: 25.0 for signal in signal_values},
        sent_delta=signal_values,
        send_failed_delta=send_failures,
        enqueue_failed_delta=no_failures,
        queue_high_water=signal_values,
        queue_final_size=no_failures,
        drained=True,
        valid=False,
    )


def valid_delivery() -> DeliveryReport:
    delivery = invalid_delivery()
    return replace(
        delivery,
        send_failed_delta={signal: 0 for signal in delivery.send_failed_delta},
        valid=True,
    )


def metadata(*, classification: str = "completed") -> AssistantRunMetadata:
    return AssistantRunMetadata(
        run_id=RUN_ID,
        attempt=1,
        agent_name="assistant_v3",
        agent_version="v3-test",
        benchmark_profile="full",
        comparable=True,
        capability_profile=CAPABILITY_PROFILE,
        requested_model="gpt-5.6-luna",
        resolved_model="gpt-5.6-luna",
        requested_reasoning="medium",
        resolved_reasoning="medium",
        judge_model="judge-model-fixed",
        judge_backend="openai-compatible",
        prompt_provenance=prompt().provenance,
        observability_provider="splunk",
        observability_chart_version="0.160.0",
        hec_index="main",
        logs_connection_id="logs-connection-1",
        readiness_report=readiness(),
        attempt_started_at=STARTED_AT,
        agent_started_at=STARTED_AT + timedelta(seconds=1),
        agent_ended_at=STARTED_AT + timedelta(seconds=1, milliseconds=120),
        classification=classification,  # type: ignore[arg-type]
    )


def completed_bundle(**changes: object) -> AssistantArtifactBundle:
    values: dict[str, object] = {
        "request": AssistantRequest(
            run_id=RUN_ID,
            prompt=prompt(),
            requested_model="gpt-5.6-luna",
            requested_reasoning="medium",
        ),
        "events": events(),
        "terminal": AssistantTerminal(
            outcome="completed",
            session_id="asst_v3_fixture",
            final_text="Root cause: dependency saturation.",
            submitted=True,
            submission_count=1,
            resolved_model="gpt-5.6-luna",
            resolved_reasoning="medium",
        ),
        "metadata": metadata(),
        "agent_duration_ms": 120.0,
        "delivery": invalid_delivery(),
        "retry_count": 0,
        "cleanup_status": "completed",
    }
    values.update(changes)
    return AssistantArtifactBundle(**values)  # type: ignore[arg-type]


def artifact_files(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def test_golden_infrastructure_invalid_bundle_writes_every_contract_file(tmp_path: Path) -> None:
    result = AssistantArtifactStore(tmp_path).write(completed_bundle())

    assert result.classification == "infrastructure_invalid"
    expected = artifact_files(GOLDEN)
    actual = artifact_files(tmp_path)
    assert actual == expected
    assert set(actual) == {
        "assistant_v3/events.jsonl",
        "assistant_v3/request.json",
        "assistant_v3/terminal.json",
        "failure.json",
        "metrics.json",
        "observability/delivery.json",
        "run_metadata.json",
    }
    assert all(content.endswith(b"\n") for content in actual.values())


def test_reprocessing_is_byte_identical_and_preserves_duplicate_native_records(tmp_path: Path) -> None:
    store = AssistantArtifactStore(tmp_path)
    store.write(completed_bundle())
    first = artifact_files(tmp_path)

    store.write(completed_bundle())
    second = artifact_files(tmp_path)

    assert second == first
    lines = (tmp_path / "assistant_v3" / "events.jsonl").read_text().splitlines()
    records = [json.loads(line) for line in lines[1:]]
    assert len(records) == len(events())
    assert [record["data"]["text"] for record in records if record["event"] == "message.delta"] == [
        "same text",
        "same text",
    ]


def test_metrics_deduplicate_only_stable_tool_and_error_ids_and_ignore_ping() -> None:
    metrics = derive_metrics(events(), agent_duration_ms=120.0, terminal_outcome="completed")

    assert metrics.as_dict() == {
        "schema": "sregym.assistant_v3.metrics.v1",
        "agent_duration_ms": 120.0,
        "time_to_first_event_ms": 20.0,
        "input_tokens": 100,
        "output_tokens": 25,
        "reasoning_tokens": None,
        "total_tokens": 125,
        "tool_calls": 2,
        "failed_tool_results": 2,
        "terminal_outcome": "completed",
    }


def test_missing_usage_is_null_and_never_inferred_as_zero() -> None:
    no_usage = tuple(
        replace(item, sequence=sequence)
        for sequence, item in enumerate((item for item in events() if item.event != "assistant.usage"), start=1)
    )

    metrics = derive_metrics(no_usage, agent_duration_ms=120.0, terminal_outcome="completed")

    assert metrics.input_tokens is None
    assert metrics.output_tokens is None
    assert metrics.reasoning_tokens is None
    assert metrics.total_tokens is None


def test_partial_stream_writes_received_events_and_machine_readable_failure(tmp_path: Path) -> None:
    partial_events = events()[:5]
    failure = AssistantFailure(
        classification="incomplete_stream",
        safe_message="Assistant stream ended before completion",
        last_sequence=5,
        retry_count=0,
        phase="assistant_execution",
        cleanup_status="completed",
    )
    bundle = completed_bundle(
        events=partial_events,
        terminal=AssistantTerminal(
            outcome="incomplete_stream",
            session_id="asst_v3_fixture",
            final_text=None,
            submitted=False,
            submission_count=0,
            resolved_model="gpt-5.6-luna",
            resolved_reasoning="medium",
        ),
        metadata=metadata(classification="incomplete_stream"),
        delivery=None,
        failure=failure,
        agent_duration_ms=50.0,
    )

    result = AssistantArtifactStore(tmp_path).write(bundle)

    assert result.classification == "incomplete_stream"
    assert not (tmp_path / "observability" / "delivery.json").exists()
    persisted_failure = json.loads((tmp_path / "failure.json").read_text())
    assert persisted_failure == {
        "schema": "sregym.assistant_v3.failure.v1",
        "classification": "incomplete_stream",
        "safe_message": "Assistant stream ended before completion",
        "last_sequence": 5,
        "retry_count": 0,
        "phase": "assistant_execution",
        "cleanup_status": "completed",
        "included_in_diagnosis_pass_rate": True,
    }
    assert len((tmp_path / "assistant_v3" / "events.jsonl").read_text().splitlines()) == 6


def test_invalid_delivery_changes_attempt_classification_without_erasing_agent_result(tmp_path: Path) -> None:
    AssistantArtifactStore(tmp_path).write(completed_bundle())

    terminal = json.loads((tmp_path / "assistant_v3" / "terminal.json").read_text())
    run_metadata = json.loads((tmp_path / "run_metadata.json").read_text())
    metrics = json.loads((tmp_path / "metrics.json").read_text())
    failure = json.loads((tmp_path / "failure.json").read_text())
    assert terminal["outcome"] == "completed"
    assert terminal["submitted"] is True
    assert metrics["terminal_outcome"] == "completed"
    assert run_metadata["classification"] == "infrastructure_invalid"
    assert run_metadata["included_in_diagnosis_pass_rate"] is False
    assert failure["classification"] == "infrastructure_invalid"
    assert failure["included_in_diagnosis_pass_rate"] is False


def test_atomic_replace_failure_preserves_prior_files_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = AssistantArtifactStore(tmp_path)
    store.write(completed_bundle())
    before = artifact_files(tmp_path)

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr("clients.assistant_v3.driver.os.replace", fail_replace)
    changed = completed_bundle(metadata=replace(metadata(), agent_version="changed"))
    with pytest.raises(ArtifactError, match="atomically"):
        store.write(changed)

    assert artifact_files(tmp_path) == before
    assert not tuple(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize(
    "secret_location",
    ["event", "failure", "delivery"],
)
def test_secret_scan_rejects_every_output_before_writing(tmp_path: Path, secret_location: str) -> None:
    bundle = completed_bundle()
    if secret_location == "event":
        contaminated = replace(bundle.events[1], data={"content": SECRET})
        bundle = replace(bundle, events=(bundle.events[0], contaminated, *bundle.events[2:]))
    elif secret_location == "failure":
        bundle = replace(
            bundle,
            delivery=None,
            terminal=replace(
                bundle.terminal, outcome="assistant_error", final_text=None, submitted=False, submission_count=0
            ),
            metadata=replace(bundle.metadata, classification="assistant_error"),
            failure=AssistantFailure(
                classification="assistant_error",
                safe_message=f"unsafe {SECRET}",
                last_sequence=len(bundle.events),
                retry_count=0,
                phase="assistant_execution",
                cleanup_status="completed",
            ),
        )
    else:
        delivery = bundle.delivery
        assert delivery is not None
        contaminated_signal = replace(delivery.opening.signals[0], evidence={"destination": SECRET})
        contaminated_opening = replace(
            delivery.opening,
            signals=(contaminated_signal, *delivery.opening.signals[1:]),
        )
        bundle = replace(bundle, delivery=replace(delivery, opening=contaminated_opening))

    with pytest.raises(ArtifactError, match="secret"):
        AssistantArtifactStore(tmp_path, secrets=(SECRET,)).write(bundle)

    assert not artifact_files(tmp_path)


@pytest.mark.parametrize(
    "bad_events",
    [
        (event(2, 10.0, "message.delta", {"text": "gap"}),),
        (
            event(1, 20.0, "message.delta", {"text": "later"}),
            event(2, 10.0, "message.delta", {"text": "earlier"}),
        ),
    ],
)
def test_event_sequence_and_offsets_are_validated_before_writing(
    tmp_path: Path, bad_events: tuple[AssistantEvent, ...]
) -> None:
    with pytest.raises(ArtifactError, match="event"):
        AssistantArtifactStore(tmp_path).write(completed_bundle(events=bad_events, agent_duration_ms=100.0))

    assert not artifact_files(tmp_path)


def test_agent_duration_must_cover_every_received_event(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError, match="duration"):
        AssistantArtifactStore(tmp_path).write(completed_bundle(agent_duration_ms=99.0))


@pytest.mark.parametrize(
    "request_artifact",
    [
        AssistantRequest(run_id=RUN_ID, prompt=prompt(), requested_model="", requested_reasoning="medium"),
        AssistantRequest(run_id=RUN_ID, prompt=prompt(), requested_model="model", requested_reasoning="extreme"),
        AssistantRequest(
            run_id=RUN_ID,
            prompt=replace(prompt(), text="   "),
            requested_model="model",
            requested_reasoning="medium",
        ),
        AssistantRequest(
            run_id=RUN_ID,
            prompt=replace(prompt(), text=f"unsafe {RUN_ID}"),
            requested_model="model",
            requested_reasoning="medium",
        ),
        AssistantRequest(
            run_id=RUN_ID,
            prompt=replace(prompt(), text="hash mismatch"),
            requested_model="model",
            requested_reasoning="medium",
        ),
    ],
)
def test_request_artifact_rejects_invalid_or_unproven_inputs(request_artifact: AssistantRequest) -> None:
    with pytest.raises(ArtifactError):
        request_artifact.validate()


@pytest.mark.parametrize(
    "terminal",
    [
        AssistantTerminal(
            outcome="completed",
            session_id=None,
            final_text="answer",
            submitted=True,
            submission_count=0,
            resolved_model=None,
            resolved_reasoning=None,
        ),
        AssistantTerminal(
            outcome="assistant_error",
            session_id=None,
            final_text="must not survive",
            submitted=False,
            submission_count=0,
            resolved_model=None,
            resolved_reasoning=None,
        ),
    ],
)
def test_terminal_submission_invariants_are_enforced(terminal: AssistantTerminal) -> None:
    with pytest.raises(ValueError):
        terminal.validate()


@pytest.mark.parametrize(
    "terminal",
    [
        AssistantTerminal(
            outcome="not-real",  # type: ignore[arg-type]
            session_id=None,
            final_text=None,
            submitted=False,
            submission_count=0,
            resolved_model=None,
            resolved_reasoning=None,
        ),
        AssistantTerminal(
            outcome="assistant_error",
            session_id=None,
            final_text=None,
            submitted=False,
            submission_count=-1,
            resolved_model=None,
            resolved_reasoning=None,
        ),
        AssistantTerminal(
            outcome="assistant_error",
            session_id=None,
            final_text=None,
            submitted=True,
            submission_count=1,
            resolved_model=None,
            resolved_reasoning=None,
        ),
        AssistantTerminal(
            outcome="completed",
            session_id=None,
            final_text=" ",
            submitted=False,
            submission_count=0,
            resolved_model=None,
            resolved_reasoning=None,
        ),
        AssistantTerminal(
            outcome="completed",
            session_id=None,
            final_text="answer",
            submitted=False,
            submission_count=0,
            resolved_model=None,
            resolved_reasoning="extreme",  # type: ignore[arg-type]
        ),
    ],
)
def test_terminal_rejects_every_invalid_state(terminal: AssistantTerminal) -> None:
    with pytest.raises(ValueError):
        terminal.validate()


@pytest.mark.parametrize(
    "changed",
    [
        {"attempt": 0},
        {"agent_name": ""},
        {"capability_profile": "direct_kubernetes"},
        {"requested_reasoning": "extreme"},
        {"resolved_reasoning": "extreme"},
        {"classification": "not-real"},
        {"attempt_started_at": datetime(2026, 9, 23, 12, 0)},
        {"agent_started_at": STARTED_AT - timedelta(seconds=1)},
        {"agent_started_at": None, "agent_ended_at": STARTED_AT},
        {"agent_ended_at": STARTED_AT},
        {"readiness_report": readiness(run_id=OTHER_RUN_ID)},
    ],
)
def test_run_metadata_rejects_inaccurate_identity_runtime_and_timestamps(changed: dict[str, Any]) -> None:
    with pytest.raises(ArtifactError):
        replace(metadata(), **changed).validate()


@pytest.mark.parametrize(
    "changed",
    [
        {"classification": "not-real"},
        {"safe_message": ""},
        {"last_sequence": -1},
        {"retry_count": -1},
        {"phase": "not-real"},
        {"cleanup_status": "not-real"},
    ],
)
def test_failure_artifact_rejects_invalid_fields(changed: dict[str, Any]) -> None:
    failure = AssistantFailure(
        classification="assistant_error",
        safe_message="safe",
        last_sequence=1,
        retry_count=0,
        phase="assistant_execution",
        cleanup_status="completed",
    )
    with pytest.raises(ArtifactError):
        replace(failure, **changed).validate()


def test_metrics_cover_keepalive_only_invalid_usage_and_missing_stable_ids() -> None:
    metric_events = (
        event(1, 1.0, "ping", {}),
        event(2, 2.0, "tool.use", {"name": "one"}),
        event(3, 3.0, "tool.use", {"name": "two"}),
        event(4, 4.0, "tool.result", {"is_error": True}),
        event(5, 5.0, "tool.result", {"is_error": True}),
        event(
            6,
            6.0,
            "assistant.usage",
            {
                "usage": {
                    "input_tokens": True,
                    "output_tokens": -1,
                    "reasoning_tokens": "unknown",
                    "total_tokens": None,
                }
            },
        ),
        event(7, 7.0, "assistant.error", {"is_error": True}),
    )
    metrics = derive_metrics(metric_events, agent_duration_ms=8.0, terminal_outcome="assistant_error")
    assert metrics.tool_calls == 2
    assert metrics.failed_tool_results == 2
    assert metrics.input_tokens is None
    assert metrics.output_tokens is None
    assert metrics.reasoning_tokens is None
    assert metrics.total_tokens is None

    ping_only = derive_metrics((event(1, 1.0, "ping", {}),), agent_duration_ms=1.0, terminal_outcome="completed")
    assert ping_only.time_to_first_event_ms is None


def test_metrics_reject_invalid_terminal_outcome() -> None:
    with pytest.raises(ArtifactError, match="terminal"):
        derive_metrics((), agent_duration_ms=0, terminal_outcome="not-real")  # type: ignore[arg-type]


def test_success_reprocessing_removes_stale_failure_and_partial_reprocessing_removes_delivery(tmp_path: Path) -> None:
    store = AssistantArtifactStore(tmp_path)
    store.write(completed_bundle())
    assert (tmp_path / "failure.json").exists()

    successful = completed_bundle(delivery=valid_delivery())
    store.write(successful)
    assert not (tmp_path / "failure.json").exists()

    failure = AssistantFailure(
        classification="incomplete_stream",
        safe_message="stream ended",
        last_sequence=5,
        retry_count=0,
        phase="assistant_execution",
        cleanup_status="completed",
    )
    partial = completed_bundle(
        events=events()[:5],
        terminal=replace(
            successful.terminal,
            outcome="incomplete_stream",
            final_text=None,
            submitted=False,
            submission_count=0,
        ),
        metadata=replace(successful.metadata, classification="incomplete_stream"),
        agent_duration_ms=50,
        delivery=None,
        failure=failure,
    )
    store.write(partial)
    assert not (tmp_path / "observability" / "delivery.json").exists()


def test_stale_optional_file_removal_failure_is_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = AssistantArtifactStore(tmp_path)

    def fail_unlink(*args: object, **kwargs: object) -> None:
        raise OSError("unlink failed")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(ArtifactError, match="stale"):
        store.write(completed_bundle(delivery=valid_delivery()))


def test_bundle_cross_field_mismatches_are_rejected(tmp_path: Path) -> None:
    base = completed_bundle(delivery=valid_delivery())
    other_delivery = replace(
        valid_delivery(),
        run_id=OTHER_RUN_ID,
        opening=readiness(run_id=OTHER_RUN_ID),
        closing=readiness(run_id=OTHER_RUN_ID, checked_at=STARTED_AT + timedelta(seconds=2)),
    )
    mismatches = (
        replace(base, retry_count=-1),
        replace(base, cleanup_status="not-real"),  # type: ignore[arg-type]
        replace(base, metadata=replace(base.metadata, run_id=OTHER_RUN_ID)),
        replace(base, delivery=other_delivery),
        replace(base, metadata=replace(base.metadata, prompt_provenance=replace(prompt().provenance, profile_id="x"))),
        replace(base, metadata=replace(base.metadata, requested_model="different")),
        replace(base, metadata=replace(base.metadata, requested_reasoning="low")),
        replace(base, metadata=replace(base.metadata, classification="assistant_error")),
        replace(
            base,
            failure=AssistantFailure(
                classification="assistant_error",
                safe_message="unexpected",
                last_sequence=11,
                retry_count=0,
                phase="assistant_execution",
                cleanup_status="completed",
            ),
        ),
    )
    for bundle in mismatches:
        with pytest.raises(ArtifactError):
            AssistantArtifactStore(tmp_path).write(bundle)


def test_store_wraps_terminal_value_errors_as_artifact_errors(tmp_path: Path) -> None:
    base = completed_bundle(delivery=valid_delivery())
    invalid_terminal = replace(base.terminal, submitted=False)

    with pytest.raises(ArtifactError, match="bundle is invalid"):
        AssistantArtifactStore(tmp_path).write(replace(base, terminal=invalid_terminal))


def test_store_rejects_metadata_identity_even_when_readiness_identity_matches(tmp_path: Path) -> None:
    base = completed_bundle(delivery=valid_delivery())
    other_metadata = replace(
        base.metadata,
        run_id=OTHER_RUN_ID,
        readiness_report=readiness(run_id=OTHER_RUN_ID),
    )

    with pytest.raises(ArtifactError, match="request and metadata"):
        AssistantArtifactStore(tmp_path).write(replace(base, metadata=other_metadata))


def test_secret_scan_checks_every_configured_secret_before_writing(tmp_path: Path) -> None:
    contaminated = event(1, 1.0, "message.delta", {"text": SECRET})
    store = AssistantArtifactStore(
        tmp_path,
        secrets=("an-absent-secret-that-is-deliberately-longer-than-the-fixture-secret-value", SECRET),
    )

    with pytest.raises(ArtifactError, match="configured secret"):
        store.write(completed_bundle(events=(contaminated,), agent_duration_ms=1.0))
    assert list(tmp_path.iterdir()) == []


def test_non_serializable_metadata_is_rejected_before_writing(tmp_path: Path) -> None:
    base = completed_bundle(delivery=valid_delivery())
    unsafe_metadata = replace(base.metadata, agent_version=object())  # type: ignore[arg-type]

    with pytest.raises(ArtifactError, match="safe deterministic JSON"):
        AssistantArtifactStore(tmp_path).write(replace(base, metadata=unsafe_metadata))
    assert list(tmp_path.iterdir()) == []


def test_failure_classification_and_last_sequence_must_match_attempt(tmp_path: Path) -> None:
    terminal = AssistantTerminal(
        outcome="assistant_error",
        session_id=None,
        final_text=None,
        submitted=False,
        submission_count=0,
        resolved_model=None,
        resolved_reasoning=None,
    )
    base = completed_bundle(
        terminal=terminal,
        metadata=metadata(classification="assistant_error"),
        delivery=None,
        failure=None,
    )
    with pytest.raises(ArtifactError, match="requires"):
        AssistantArtifactStore(tmp_path).write(base)

    for failure in (
        AssistantFailure("incomplete_stream", "wrong kind", 11, 0, "assistant_execution", "completed"),
        AssistantFailure("assistant_error", "wrong sequence", 10, 0, "assistant_execution", "completed"),
    ):
        with pytest.raises(ArtifactError):
            AssistantArtifactStore(tmp_path).write(replace(base, failure=failure))


@pytest.mark.parametrize("duration", [-1.0, float("nan"), float("inf")])
def test_non_finite_or_negative_duration_is_rejected(duration: float) -> None:
    with pytest.raises(ArtifactError, match="duration"):
        derive_metrics((), agent_duration_ms=duration, terminal_outcome="completed")


def test_non_finite_event_offset_is_rejected() -> None:
    with pytest.raises(ArtifactError, match="offset"):
        derive_metrics((event(1, float("nan"), "ping", {}),), agent_duration_ms=1, terminal_outcome="completed")


def test_unsafe_json_values_and_non_utc_nested_timestamps_are_rejected(tmp_path: Path) -> None:
    unsafe_event = event(1, 1.0, "message.delta", {"unsupported": {"set"}})
    with pytest.raises(ArtifactError, match="event value"):
        AssistantArtifactStore(tmp_path).write(completed_bundle(events=(unsafe_event,), agent_duration_ms=1.0))

    delivery = valid_delivery()
    signal = replace(delivery.opening.signals[0], evidence={"when": datetime(2026, 9, 23)})
    opening = replace(delivery.opening, signals=(signal, *delivery.opening.signals[1:]))
    with pytest.raises(ArtifactError, match="timestamps"):
        AssistantArtifactStore(tmp_path).write(completed_bundle(delivery=replace(delivery, opening=opening)))


def test_atomic_directory_creation_failure_is_classified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_mkdir(*args: object, **kwargs: object) -> None:
        raise OSError("mkdir failed")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    with pytest.raises(ArtifactError, match="atomically"):
        AssistantArtifactStore(tmp_path / "missing").write(completed_bundle())
