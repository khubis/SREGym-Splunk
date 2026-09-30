from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from clients.assistant_v3 import driver as driver_module
from clients.assistant_v3.client import AssistantEvent, AssistantV3Client, AssistantV3Config, RetryPolicy
from clients.assistant_v3.driver import (
    CAPABILITY_PROFILE,
    ArtifactError,
    AssistantArtifactBundle,
    AssistantArtifactStore,
    AssistantFailure,
    AssistantRequest,
    AssistantRunMetadata,
    AssistantTerminal,
    ConductorClient,
    DriverRunConfig,
    derive_metrics,
    execute_assistant_attempt,
    finalize_attempt_artifacts,
    write_pre_agent_failure,
)
from clients.assistant_v3.prompt import PromptProvenance, RenderedPrompt
from sregym.observability.base import (
    DeliveryReport,
    ReadinessReport,
    SignalName,
    SignalReadiness,
    serialize_provider_artifact,
)

RUN_ID = "anon_0123456789abcdef0123456789abcdef"
OTHER_RUN_ID = "anon_ffffffffffffffffffffffffffffffff"
PROMPT = "Investigate the application and diagnose the root cause."
STARTED_AT = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
WINDOW_ENDED_AT = STARTED_AT + timedelta(minutes=5)
GOLDEN = Path(__file__).parents[1] / "fixtures" / "assistant_v3" / "golden_infrastructure_invalid"
WORKFLOW_DOC = Path(__file__).parents[2] / "docs" / "assistant-v3-evaluations.md"
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
        AssistantRequest(
            run_id=RUN_ID,
            prompt=prompt(),
            requested_model="model",
            requested_reasoning="medium",
            action_instructions="   ",
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


@pytest.mark.parametrize("symptom", [None, "Checkout requests are failing."])
def test_driver_uses_public_app_metadata_one_fresh_session_and_one_diagnosis_submission(
    tmp_path: Path, symptom: str | None,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "conductor" and request.url.path == "/status":
            return httpx.Response(200, json={"stage": "diagnosis"})
        if request.url.host == "conductor" and request.url.path == "/get_app":
            return httpx.Response(
                200,
                json={
                    "app_name": "Astronomy Shop",
                    "namespace": "otel-demo",
                    "namespaces": ["otel-demo"],
                    "descriptions": "A microservice application.",
                    "oracle": "must never reach the prompt",
                },
            )
        if request.url.host == "conductor" and request.url.path == "/submit":
            return httpx.Response(200, json={"status": "200", "stage": "diagnosis", "message": "accepted"})
        if request.url.host == "assistant" and request.url.path == "/v2/assistant/sessions":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'event: session.created\ndata: {"session_id":"fresh-session"}\n\n'
                    b'event: message.complete\ndata: {"final_text":"The checkout dependency is saturated."}\n\n'
                ),
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    assistant = AssistantV3Client(
        AssistantV3Config(
            base_url="https://assistant",
            auth_token="assistant-secret",
            sf_token="sf-secret",
            model="gpt-5.6-luna",
            reasoning="medium",
        ),
        http_client=httpx.Client(transport=transport),
        retry_policy=RetryPolicy(max_attempts=1),
        clock=iter((0.0, 0.01, 0.02)).__next__,
    )
    conductor = ConductorClient("http://conductor", http_client=httpx.Client(transport=transport))
    result = execute_assistant_attempt(
        DriverRunConfig(
            run_id=RUN_ID,
            attempt=1,
            artifacts_root=tmp_path,
            benchmark_profile="full",
            comparable=symptom is None,
            judge_model="fixed-judge",
            judge_backend="api",
            observability_provider="splunk",
            readiness_report=readiness(),
            attempt_started_at=STARTED_AT,
            telemetry_window_ended_at=WINDOW_ENDED_AT,
            symptom=symptom,
        ),
        conductor=conductor,
        assistant=assistant,
        clock=iter((100.0, 100.2)).__next__,
    )

    assert result.classification == "completed"
    assert [request.url.path for request in requests] == [
        "/status",
        "/get_app",
        "/v2/assistant/sessions",
        "/submit",
    ]
    session_request = json.loads(requests[2].content)
    assert session_request["session_id"] is None
    assert session_request["model"] == "gpt-5.6-luna"
    assert session_request["reasoning"] == "medium"
    assert RUN_ID not in session_request["action_instructions"]
    assert "otel-demo" not in session_request["action_instructions"]
    assert "2026-09-23T12:00:00Z" in session_request["action_instructions"]
    assert "2026-09-23T12:05:00Z" in session_request["action_instructions"]
    if symptom is not None:
        assert session_request["action_instructions"].endswith("Observed symptom: " + symptom)
    assert RUN_ID not in session_request["prompt"]
    assert "surface" not in session_request
    assert "oracle" not in session_request["prompt"]
    assert requests[2].headers["X-Request-ID"] == "01234567-89ab-cdef-0123-456789abcdef"
    assert json.loads(requests[3].content) == {
        "solution": "The checkout dependency is saturated.",
        "stage": "diagnosis",
    }
    assert json.loads((tmp_path / "assistant_v3" / "terminal.json").read_text())["submission_count"] == 1
    persisted_request = json.loads((tmp_path / "assistant_v3" / "request.json").read_text())
    assert persisted_request["action_instructions"] == session_request["action_instructions"]
    if symptom is not None:
        assert persisted_request["action_profile_id"] == "sregym-symptom-window-v1"
    assert persisted_request["prompt"] == session_request["prompt"]


def test_driver_rejects_an_explicit_out_of_window_query_before_submission(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/status":
            return httpx.Response(200, json={"stage": "diagnosis"})
        if request.url.path == "/get_app":
            return httpx.Response(
                200,
                json={
                    "app_name": "Astronomy Shop",
                    "namespace": "otel-demo",
                    "namespaces": ["otel-demo"],
                    "descriptions": "A microservice application.",
                },
            )
        if request.url.path == "/v2/assistant/sessions":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    b'event: tool.use\ndata: {"id":"call-1","input":{"start_time":"2026-09-23T11:59:59Z"}}\n\n'
                    b'event: message.complete\ndata: {"final_text":"Diagnosis from stale telemetry."}\n\n'
                ),
            )
        if request.url.path == "/submit":
            raise AssertionError("scope-violating output must never be submitted")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    assistant = AssistantV3Client(
        AssistantV3Config("https://assistant", "assistant-secret", "sf-secret", "gpt-5.6-luna", "medium"),
        http_client=httpx.Client(transport=transport),
        retry_policy=RetryPolicy(max_attempts=1),
        clock=iter((0.0, 0.01, 0.02)).__next__,
    )
    result = execute_assistant_attempt(
        DriverRunConfig(
            run_id=RUN_ID,
            attempt=1,
            artifacts_root=tmp_path,
            benchmark_profile="full",
            comparable=True,
            judge_model="fixed-judge",
            judge_backend="api",
            observability_provider="splunk",
            readiness_report=readiness(),
            attempt_started_at=STARTED_AT,
            telemetry_window_ended_at=WINDOW_ENDED_AT,
        ),
        conductor=ConductorClient("http://conductor", http_client=httpx.Client(transport=transport)),
        assistant=assistant,
        clock=iter((100.0, 100.2)).__next__,
    )

    assert result.classification == "telemetry_scope_violation"
    assert "/submit" not in [request.url.path for request in requests]
    failure = json.loads((tmp_path / "failure.json").read_text())
    assert failure["classification"] == "telemetry_scope_violation"
    assert failure["included_in_diagnosis_pass_rate"] is False


def test_noncomparable_guided_run_submits_final_answer_and_records_scope_warning(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/status":
            return httpx.Response(200, json={"stage": "diagnosis"})
        if request.url.path == "/get_app":
            return httpx.Response(200, json={
                "app_name": "Hotel Reservation", "namespace": "hotel-reservation",
                "namespaces": ["hotel-reservation"], "descriptions": "A service application.",
            })
        if request.url.path == "/v2/assistant/sessions":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(
                b'event: tool.use\ndata: {"id":"call-1","input":{"end_time":"2026-09-23T12:06:00Z"}}\n\n'
                b'event: message.complete\ndata: {"final_text":"Recommendation failed during the incident."}\n\n'
            ))
        if request.url.path == "/submit":
            return httpx.Response(200, json={"status": "200", "stage": "diagnosis", "message": "accepted"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    assistant = AssistantV3Client(
        AssistantV3Config("https://assistant", "assistant-secret", "sf-secret", "gpt-5.6-luna", "medium"),
        http_client=httpx.Client(transport=transport), retry_policy=RetryPolicy(max_attempts=1),
        clock=iter((0.0, 0.01, 0.02)).__next__,
    )
    result = execute_assistant_attempt(
        DriverRunConfig(
            run_id=RUN_ID, attempt=1, artifacts_root=tmp_path, benchmark_profile="svelte",
            comparable=False, judge_model="fixed-judge", judge_backend="api",
            observability_provider="splunk", readiness_report=readiness(),
            attempt_started_at=STARTED_AT, telemetry_window_ended_at=WINDOW_ENDED_AT,
            symptom="Recommendation requests are timing out.",
        ),
        conductor=ConductorClient("http://conductor", http_client=httpx.Client(transport=transport)),
        assistant=assistant, clock=iter((100.0, 100.2)).__next__,
    )
    assert result.classification == "completed"
    assert [request.url.path for request in requests].count("/submit") == 1
    assert json.loads(requests[-1].content)["solution"] == "Recommendation failed during the incident."
    metadata = json.loads((tmp_path / "run_metadata.json").read_text())
    assert metadata["telemetry_scope_warning"] == "Assistant tool trace queried telemetry after the provided time window"
    assert metadata["included_in_diagnosis_pass_rate"] is True


def test_scope_validator_rejects_explicit_pre_attempt_windows_and_allows_current_scope() -> None:
    current = event(
        1,
        1.0,
        "tool.use",
        {
            "input": {
                "filters": [{"cluster": RUN_ID}],
                "start_time": "2026-09-23T12:00:00Z",
            }
        },
    )
    stale_iso = event(
        2,
        2.0,
        "tool.use",
        {"input": {"window": [{"start-time": "2026-09-23T11:59:59Z"}]}},
    )
    stale_epoch_ms = event(
        3,
        3.0,
        "tool.use",
        {"input": {"start": int((STARTED_AT - timedelta(seconds=1)).timestamp() * 1000)}},
    )
    late_end = event(
        4,
        4.0,
        "tool.use",
        {"input": {"end_time": "2026-09-23T12:05:01Z"}},
    )
    foreign_run = event(5, 5.0, "tool.result", {"output": f"cluster {OTHER_RUN_ID}"})

    assert driver_module._scope_violation(
        (current, foreign_run), window_started_at=STARTED_AT, window_ended_at=WINDOW_ENDED_AT
    ) is None
    assert "before" in (
        driver_module._scope_violation(
            (stale_iso,), window_started_at=STARTED_AT, window_ended_at=WINDOW_ENDED_AT
        )
        or ""
    )
    assert "before" in (
        driver_module._scope_violation(
            (stale_epoch_ms,), window_started_at=STARTED_AT, window_ended_at=WINDOW_ENDED_AT
        )
        or ""
    )
    assert "after" in (
        driver_module._scope_violation(
            (late_end,), window_started_at=STARTED_AT, window_ended_at=WINDOW_ENDED_AT
        )
        or ""
    )


def test_action_window_preserves_subsecond_precision_used_by_scope_validator() -> None:
    precise_start = STARTED_AT.replace(microsecond=291_000)
    precise_end = WINDOW_ENDED_AT.replace(microsecond=88_000)
    instructions = driver_module._build_action_instructions(precise_start, precise_end)

    assert "2026-09-23T12:00:00.291000Z" in instructions
    assert "2026-09-23T12:05:00.088000Z" in instructions


    bounded_query = event(
        1,
        1.0,
        "tool.use",
        {
            "input": {
                "start": "2026-09-23T12:00:00.291000Z",
                "end": "2026-09-23T12:05:00.088000Z",
            }
        },
    )
    assert driver_module._scope_violation(
        (bounded_query,),
        window_started_at=precise_start,
        window_ended_at=precise_end,
    ) is None


def test_symptom_guided_instructions_only_append_the_reviewed_symptom() -> None:
    baseline = driver_module._build_action_instructions(STARTED_AT, WINDOW_ENDED_AT)
    symptom = "A scheduled background task in Hotel Reservation is taking unusually long to finish."
    guided = driver_module._build_action_instructions(
        STARTED_AT, WINDOW_ENDED_AT, symptom=symptom,
    )
    assert guided == baseline + "\nObserved symptom: " + symptom


def test_driver_preserves_invalid_stream_and_never_submits_it(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/status":
            return httpx.Response(200, json={"stage": "diagnosis"})
        if request.url.path == "/get_app":
            return httpx.Response(
                200,
                json={
                    "app_name": "Astronomy Shop",
                    "namespace": "otel-demo",
                    "namespaces": ["otel-demo"],
                    "descriptions": "A microservice application.",
                },
            )
        if request.url.path == "/v2/assistant/sessions":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b'event: message.delta\ndata: {"text":"partial"}\n\n',
            )
        if request.url.path == "/submit":
            raise AssertionError("partial output must never be submitted")
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    assistant = AssistantV3Client(
        AssistantV3Config("https://assistant", "assistant-secret", "sf-secret", "gpt-5.6-luna", "medium"),
        http_client=httpx.Client(transport=transport),
        retry_policy=RetryPolicy(max_attempts=1),
        clock=iter((0.0, 0.01)).__next__,
    )
    conductor = ConductorClient("http://conductor", http_client=httpx.Client(transport=transport))

    result = execute_assistant_attempt(
        DriverRunConfig(
            run_id=RUN_ID,
            attempt=1,
            artifacts_root=tmp_path,
            benchmark_profile="full",
            comparable=True,
            judge_model="fixed-judge",
            judge_backend="api",
            observability_provider="splunk",
            readiness_report=readiness(),
        ),
        conductor=conductor,
        assistant=assistant,
        clock=iter((100.0, 100.2)).__next__,
    )

    assert result.classification == "incomplete_stream"
    assert "/submit" not in [request.url.path for request in requests]
    terminal = json.loads((tmp_path / "assistant_v3" / "terminal.json").read_text())
    assert terminal["submitted"] is False
    assert terminal["final_text"] is None
    assert (tmp_path / "failure.json").exists()


def test_pre_agent_provider_failure_still_writes_complete_inspectable_artifacts(tmp_path: Path) -> None:
    config = DriverRunConfig(
        run_id=RUN_ID,
        attempt=1,
        artifacts_root=tmp_path,
        benchmark_profile="svelte",
        comparable=False,
        judge_model="fixed-judge",
        judge_backend="api",
        observability_provider="splunk",
        readiness_report=readiness(ready=False),
    )
    assistant_config = AssistantV3Config(
        "https://assistant.example.test",
        "assistant-secret",
        "sf-secret",
        "gpt-5.6-luna",
        "medium",
    )

    result = write_pre_agent_failure(
        config,
        assistant_configuration=assistant_config,
        safe_message="required telemetry was not queryable",
        prompt_context={
            "app_name": "Astronomy Shop",
            "app_description": "A microservice application.",
            "app_namespace": "otel-demo",
        },
    )

    assert result.classification == "infrastructure_invalid"
    metadata_value = json.loads((tmp_path / "run_metadata.json").read_text())
    assert metadata_value["included_in_diagnosis_pass_rate"] is False
    assert metadata_value["comparable"] is False
    assert (tmp_path / "assistant_v3" / "events.jsonl").exists()


def test_closing_delivery_audit_updates_artifacts_without_erasing_agent_evidence(tmp_path: Path) -> None:
    AssistantArtifactStore(tmp_path).write(completed_bundle(delivery=None, cleanup_status="pending"))
    before_events = (tmp_path / "assistant_v3" / "events.jsonl").read_bytes()

    finalize_attempt_artifacts(
        tmp_path,
        delivery=invalid_delivery(),
        cleanup_status="completed",
    )

    assert (tmp_path / "assistant_v3" / "events.jsonl").read_bytes() == before_events
    metadata_value = json.loads((tmp_path / "run_metadata.json").read_text())
    failure_value = json.loads((tmp_path / "failure.json").read_text())
    assert metadata_value["classification"] == "infrastructure_invalid"
    assert metadata_value["included_in_diagnosis_pass_rate"] is False
    assert failure_value["phase"] == "delivery"
    assert failure_value["cleanup_status"] == "completed"
    assert json.loads((tmp_path / "observability" / "delivery.json").read_text())["valid"] is False


def test_closing_delivery_audit_saves_counter_samples_for_review(tmp_path: Path) -> None:
    AssistantArtifactStore(tmp_path).write(completed_bundle(delivery=None, cleanup_status="pending"))
    samples = {"closing_initial": {"sent": {"logs": None}}, "closing": {"sent": {"logs": 42}}}
    delivery = replace(valid_delivery(), counter_samples=samples)

    finalize_attempt_artifacts(tmp_path, delivery=delivery, cleanup_status="completed")

    saved = json.loads((tmp_path / "observability" / "delivery.json").read_text())
    assert saved["counter_samples"] == samples
    assert saved["valid"] is True


def test_driver_config_file_round_trip_preserves_readiness_and_attempt_start(tmp_path: Path) -> None:
    payload = {
        "run_id": RUN_ID,
        "attempt": 2,
        "benchmark_profile": "full",
        "comparable": True,
        "judge_model": "fixed-judge",
        "judge_backend": "api",
        "observability_provider": "splunk",
        "readiness_report": serialize_provider_artifact(readiness()),
        "attempt_started_at": "2026-09-23T12:00:00Z",
        "telemetry_window_ended_at": "2026-09-23T12:05:00Z",
    }
    path = tmp_path / "assistant_v3_driver_config.json"
    path.write_text(json.dumps(payload))

    config = DriverRunConfig.from_file(path)

    assert config.artifacts_root == tmp_path
    assert config.readiness_report == readiness()
    assert config.attempt_started_at == STARTED_AT
    assert config.telemetry_window_ended_at == WINDOW_ENDED_AT


@pytest.mark.parametrize(
    "payload, message",
    [
        ("not json", "unavailable or invalid"),
        ("[]", "must be an object"),
        ("{}", "incomplete"),
        (json.dumps({"readiness_report": {}}), "readiness"),
        (
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "attempt": 1,
                    "benchmark_profile": "full",
                    "comparable": True,
                    "judge_model": "judge",
                    "judge_backend": "api",
                    "observability_provider": "splunk",
                    "readiness_report": {"signals": [{}]},
                }
            ),
            "readiness",
        ),
        (
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "attempt": 1,
                    "benchmark_profile": "full",
                    "comparable": True,
                    "judge_model": "judge",
                    "judge_backend": "api",
                    "observability_provider": "splunk",
                    "attempt_started_at": "bad-date",
                }
            ),
            "incomplete",
        ),
    ],
)
def test_driver_config_file_rejects_invalid_inputs(tmp_path: Path, payload: str, message: str) -> None:
    path = tmp_path / "config.json"
    path.write_text(payload)
    with pytest.raises(ArtifactError, match=message):
        DriverRunConfig.from_file(path)


def test_driver_config_file_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError, match="unavailable"):
        DriverRunConfig.from_file(tmp_path / "missing.json")


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"attempt": 0}, "attempt"),
        ({"benchmark_profile": "tiny"}, "profile"),
        ({"benchmark_profile": "svelte", "comparable": True}, "comparable"),
        ({"judge_model": ""}, "judge"),
        ({"readiness_report": readiness(run_id=OTHER_RUN_ID)}, "identities"),
        ({"attempt_started_at": datetime(2026, 9, 23, 12, 0)}, "UTC"),
        ({"telemetry_window_ended_at": datetime(2026, 9, 23, 12, 5)}, "UTC"),
        (
            {"attempt_started_at": STARTED_AT, "telemetry_window_ended_at": STARTED_AT - timedelta(seconds=1)},
            "window",
        ),
    ],
)
def test_driver_config_validation_rejects_inaccurate_labels(changes: dict[str, Any], message: str) -> None:
    values: dict[str, Any] = {
        "run_id": RUN_ID,
        "attempt": 1,
        "artifacts_root": Path("/tmp/artifacts"),
        "benchmark_profile": "full",
        "comparable": True,
        "judge_model": "judge",
        "judge_backend": "api",
        "observability_provider": "splunk",
        "readiness_report": readiness(),
    }
    values.update(changes)
    with pytest.raises(ArtifactError, match=message):
        DriverRunConfig(**values).validate()


@pytest.mark.parametrize(
    "path, response, method, message",
    [
        ("/status", httpx.Response(200, text="not-json"), "require_diagnosis", "status response"),
        ("/status", httpx.Response(200, json={"stage": "mitigation"}), "require_diagnosis", "not ready"),
        ("/get_app", httpx.Response(503), "get_prompt_context", "unavailable"),
        ("/get_app", httpx.Response(200, text="not-json"), "get_prompt_context", "metadata is invalid"),
        ("/get_app", httpx.Response(200, json=[]), "get_prompt_context", "metadata is invalid"),
        ("/get_app", httpx.Response(200, json={"app_name": 1}), "get_prompt_context", "metadata is invalid"),
        (
            "/get_app",
            httpx.Response(200, json={"app_name": "app", "descriptions": "description"}),
            "get_prompt_context",
            "metadata is invalid",
        ),
    ],
)
def test_conductor_client_rejects_invalid_public_contract(path, response, method, message) -> None:
    client = ConductorClient(
        "http://conductor/",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: response)),
    )
    with pytest.raises(driver_module.ConductorError, match=message):
        getattr(client, method)()


def test_conductor_client_uses_single_namespace_fallback_and_rejects_second_submit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/get_app":
            return httpx.Response(
                200,
                json={"app_name": "app", "descriptions": "description", "namespace": "only-one"},
            )
        return httpx.Response(200)

    client = ConductorClient("http://conductor", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.get_prompt_context()["app_namespace"] == "only-one"
    client.submit_diagnosis("answer")
    with pytest.raises(driver_module.ConductorError, match="already attempted"):
        client.submit_diagnosis("answer again")


def test_conductor_client_rejects_unaccepted_submission_and_closes_owned_client() -> None:
    client = ConductorClient(
        "http://conductor",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))),
    )
    with pytest.raises(driver_module.ConductorError, match="not accepted"):
        client.submit_diagnosis("answer")

    owned = ConductorClient("http://conductor")
    close = Mock()
    owned._client = SimpleNamespace(close=close)  # type: ignore[assignment]
    owned.close()
    close.assert_called_once()

    external = ConductorClient("http://conductor", http_client=httpx.Client())
    external.close()


@pytest.mark.parametrize("submission_status", [500, 503])
def test_driver_preserves_ambiguous_submission_without_retry(tmp_path: Path, submission_status: int) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/status":
            return httpx.Response(200, json={"stage": "diagnosis"})
        if request.url.path == "/get_app":
            return httpx.Response(
                200,
                json={"app_name": "app", "descriptions": "description", "namespace": "namespace"},
            )
        if request.url.path == "/v2/assistant/sessions":
            return httpx.Response(
                200,
                content=b'event: message.complete\ndata: {"final_text":"answer"}\n\n',
            )
        return httpx.Response(submission_status)

    transport = httpx.MockTransport(handler)
    assistant = AssistantV3Client(
        AssistantV3Config("https://assistant", "assistant-secret", "sf-secret", "model", "medium"),
        http_client=httpx.Client(transport=transport),
        retry_policy=RetryPolicy(max_attempts=1),
        clock=iter((0.0, 0.01)).__next__,
    )
    config = DriverRunConfig(
        RUN_ID,
        1,
        tmp_path,
        "full",
        True,
        "judge",
        "api",
        "splunk",
        readiness(),
    )

    result = execute_assistant_attempt(
        config,
        conductor=ConductorClient("http://conductor", http_client=httpx.Client(transport=transport)),
        assistant=assistant,
        clock=iter((1.0, 1.1)).__next__,
    )

    assert result.classification == "ambiguous_completion"
    assert paths.count("/submit") == 1


def test_driver_persists_conductor_failure_before_session_start(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"stage": "mitigation"}))
    assistant = AssistantV3Client(
        AssistantV3Config("https://assistant", "assistant-secret", "sf-secret", "model", "medium"),
        http_client=httpx.Client(transport=transport),
    )
    config = DriverRunConfig(RUN_ID, 1, tmp_path, "full", True, "judge", "api", "splunk", readiness())

    result = execute_assistant_attempt(
        config,
        conductor=ConductorClient("http://conductor", http_client=httpx.Client(transport=transport)),
        assistant=assistant,
        clock=iter((1.0, 1.1)).__next__,
    )

    assert result.classification == "configuration_error"
    request_value = json.loads((tmp_path / "assistant_v3" / "request.json").read_text())
    assert "unavailable" in request_value["prompt"]


def test_event_spool_reports_disk_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "open", Mock(side_effect=OSError("disk full")))
    with pytest.raises(ArtifactError, match="spool"):
        driver_module._append_event_spool(tmp_path / "events.jsonl", event(1, 1.0, "message.delta", {}))


def test_finalizer_updates_existing_agent_failure_cleanup_status(tmp_path: Path) -> None:
    failure = AssistantFailure("incomplete_stream", "ended", 5, 0, "assistant_execution", "pending")
    partial_events = events()[:5]
    AssistantArtifactStore(tmp_path).write(
        completed_bundle(
            events=partial_events,
            terminal=AssistantTerminal("incomplete_stream", None, None, False, 0, None, None),
            metadata=metadata(classification="incomplete_stream"),
            agent_duration_ms=50,
            delivery=None,
            failure=failure,
        )
    )

    finalize_attempt_artifacts(tmp_path, delivery=valid_delivery(), cleanup_status="failed")

    assert json.loads((tmp_path / "failure.json").read_text())["cleanup_status"] == "failed"
    assert (tmp_path / "observability" / "delivery.json").exists()


@pytest.mark.parametrize(
    "setup, message",
    [
        (lambda root: None, "unavailable"),
        (
            lambda root: (
                (root / "assistant_v3").mkdir(),
                (root / "run_metadata.json").write_text("[]"),
                (root / "assistant_v3" / "terminal.json").write_text("{}"),
                (root / "assistant_v3" / "events.jsonl").write_text("header\n"),
            ),
            "invalid",
        ),
    ],
)
def test_finalizer_rejects_missing_or_invalid_core_artifacts(tmp_path: Path, setup, message: str) -> None:
    setup(tmp_path)
    with pytest.raises(ArtifactError, match=message):
        finalize_attempt_artifacts(tmp_path, delivery=None, cleanup_status="completed")


@pytest.mark.parametrize("failure_content", ["not-json", "[]"])
def test_finalizer_rejects_invalid_existing_failure(tmp_path: Path, failure_content: str) -> None:
    AssistantArtifactStore(tmp_path).write(completed_bundle(delivery=valid_delivery()))
    (tmp_path / "failure.json").write_text(failure_content)
    with pytest.raises(ArtifactError, match="failure artifact"):
        finalize_attempt_artifacts(tmp_path, delivery=None, cleanup_status="completed")


def test_finalizer_rejects_invalid_cleanup_status(tmp_path: Path) -> None:
    with pytest.raises(ArtifactError, match="cleanup"):
        finalize_attempt_artifacts(
            tmp_path,
            delivery=None,
            cleanup_status="unknown",  # type: ignore[arg-type]
        )


def test_finalizer_accepts_success_without_optional_delivery_or_failure(tmp_path: Path) -> None:
    AssistantArtifactStore(tmp_path).write(completed_bundle(delivery=valid_delivery()))
    (tmp_path / "observability" / "delivery.json").unlink()

    finalize_attempt_artifacts(tmp_path, delivery=None, cleanup_status="completed")

    assert not (tmp_path / "failure.json").exists()
    assert not (tmp_path / "observability" / "delivery.json").exists()


def test_preflight_closes_client_on_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    configuration = AssistantV3Config("https://assistant", "auth", "sf", "model", "medium")
    monkeypatch.setattr(driver_module.AssistantV3Config, "from_env", Mock(return_value=configuration))
    client = Mock()
    monkeypatch.setattr(driver_module, "AssistantV3Client", Mock(return_value=client))

    driver_module.run_preflight()
    client.preflight.assert_called_once_with(request_id="00000000-0000-0000-0000-000000000000")
    client.close.assert_called_once()

    client.reset_mock()
    client.preflight.side_effect = RuntimeError("offline")
    with pytest.raises(RuntimeError, match="offline"):
        driver_module.run_preflight()
    client.close.assert_called_once()


@pytest.mark.parametrize("classification, expected", [("completed", 0), ("assistant_error", 1)])
def test_driver_main_closes_clients_and_maps_exit_status(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    classification: str,
    expected: int,
) -> None:
    config = DriverRunConfig(RUN_ID, 1, tmp_path, "full", True, "judge", "api", "splunk", readiness())
    configuration = AssistantV3Config("https://assistant", "auth", "sf", "model", "medium")
    conductor = Mock()
    assistant = Mock()
    monkeypatch.setenv("SREGYM_ASSISTANT_DRIVER_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr(driver_module.DriverRunConfig, "from_file", Mock(return_value=config))
    monkeypatch.setattr(driver_module.AssistantV3Config, "from_env", Mock(return_value=configuration))
    monkeypatch.setattr(driver_module, "ConductorClient", Mock(return_value=conductor))
    monkeypatch.setattr(driver_module, "AssistantV3Client", Mock(return_value=assistant))
    monkeypatch.setattr(
        driver_module,
        "execute_assistant_attempt",
        Mock(return_value=SimpleNamespace(classification=classification)),
    )

    assert driver_module.main() == expected
    assistant.close.assert_called_once()
    conductor.close.assert_called_once()


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


def _documented_spotcheck() -> str:
    text = WORKFLOW_DOC.read_text(encoding="utf-8")
    match = re.search(
        r"<!-- BEGIN ASSISTANT_V3_SPOT_CHECK -->.*?uv run python - \"\$RUN_DIR\" <<'PY'\n(.*?)\nPY",
        text,
        re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def _spotcheck_run(tmp_path: Path, *, valid: bool) -> dict[str, Any]:
    run_dir = tmp_path / ("valid" if valid else "invalid")
    shutil.copytree(GOLDEN, run_dir)
    with (run_dir / "case_results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["Diagnosis.success", "Diagnosis.judgment", "Diagnosis.accuracy"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "Diagnosis.success": "True",
                "Diagnosis.judgment": "True",
                "Diagnosis.accuracy": "92.5",
            }
        )
    if valid:
        (run_dir / "failure.json").unlink()
        metadata = json.loads((run_dir / "run_metadata.json").read_text(encoding="utf-8"))
        metadata["classification"] = "completed"
        metadata["included_in_diagnosis_pass_rate"] = True
        (run_dir / "run_metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        delivery = json.loads((run_dir / "observability" / "delivery.json").read_text(encoding="utf-8"))
        delivery["valid"] = True
        delivery["send_failed_delta"]["logs"] = 0
        (run_dir / "observability" / "delivery.json").write_text(json.dumps(delivery), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-", str(run_dir)],
        input=_documented_spotcheck(),
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "", "SPLUNK_HEC_TOKEN": SECRET, "ASSISTANT_V3_AUTH_TOKEN": SECRET},
    )
    assert result.returncode == 0, result.stderr
    assert SECRET not in result.stdout + result.stderr
    return json.loads(result.stdout)


def test_assistant_v3_workflow_documents_reproducible_bounded_commands() -> None:
    text = WORKFLOW_DOC.read_text(encoding="utf-8")
    for required in (
        "ASSISTANT_V3_URL",
        "ASSISTANT_V3_AUTH_TOKEN",
        "SF_TOKEN",
        "SFX_REALM",
        "SPLUNK_HOST",
        "SPLUNK_HEC_PORT",
        "SPLUNK_HEC_TOKEN",
        "--problem edge_request_filter_cpu_saturation",
        "--profile full",
        "--profile svelte",
        "--suite sregym-lite",
        "--resume",
        "--stages diagnosis",
        "--agent assistant_v3",
        "--model gpt-5.6-luna",
        "--reasoning-effort medium",
        "--judge-model",
        "--judge-backend api",
        "--observability-provider splunk",
        "--allow-agent-endpoint \"$ASSISTANT_V3_URL\"",
        "--force-build",
        "splunk_o11y_read_only_no_direct_kubernetes",
        "included_in_diagnosis_pass_rate",
        "results/<batch>/assistant_v3/<problem_id>/run_<attempt>/",
    ):
        assert required in text
    assert "SPLUNK_HEC_TOKEN=" not in text
    assert "ASSISTANT_V3_AUTH_TOKEN=" not in text


def test_documented_spotcheck_reports_success_without_credentials(tmp_path: Path) -> None:
    summary = _spotcheck_run(tmp_path, valid=True)
    expected_request = json.loads((GOLDEN / "assistant_v3" / "request.json").read_text(encoding="utf-8"))

    assert summary["prompt"]["sha256"] == expected_request["prompt_sha256"]
    assert summary["prompt"]["text_path"].endswith("assistant_v3/request.json")
    assert summary["diagnosis"] == "Root cause: dependency saturation."
    assert summary["tools"] == {"calls": 2, "failed_results": 2}
    assert summary["delivery"]["valid"] is True
    assert summary["judge"] == {"accuracy": "92.5", "judgment": "True", "success": "True"}
    assert summary["failure"]["classification"] == "completed"


def test_documented_spotcheck_reports_invalid_delivery_and_failure(tmp_path: Path) -> None:
    summary = _spotcheck_run(tmp_path, valid=False)

    assert summary["delivery"]["valid"] is False
    assert summary["delivery"]["send_failed_delta"]["logs"] == 1
    assert summary["delivery"]["drained"] is True
    assert summary["failure"] == {
        "classification": "infrastructure_invalid",
        "cleanup_status": "completed",
        "included_in_diagnosis_pass_rate": False,
        "phase": "delivery",
        "safe_message": "Post-execution telemetry delivery verification failed",
    }
