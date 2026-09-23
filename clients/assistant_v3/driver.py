"""Run-level artifacts and deterministic metrics for the Assistant v3 driver."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args

from clients.assistant_v3.client import AssistantEvent
from clients.assistant_v3.prompt import PromptProvenance, RenderedPrompt
from sregym.observability.base import DeliveryReport, ReadinessReport, validate_run_id

CAPABILITY_PROFILE = "splunk_o11y_read_only_no_direct_kubernetes"

TerminalOutcome = Literal[
    "completed",
    "configuration_error",
    "authentication_error",
    "permission_error",
    "transient_exhausted",
    "incomplete_stream",
    "assistant_error",
    "ambiguous_completion",
    "capability_policy_violation",
    "infrastructure_invalid",
]
FailureClassification = Literal[
    "configuration_error",
    "authentication_error",
    "permission_error",
    "transient_exhausted",
    "incomplete_stream",
    "assistant_error",
    "ambiguous_completion",
    "capability_policy_violation",
    "infrastructure_invalid",
]
FailurePhase = Literal[
    "provider_preflight",
    "assistant_preflight",
    "assistant_execution",
    "delivery",
    "cleanup",
]
CleanupStatus = Literal["pending", "completed", "failed"]

_TERMINAL_OUTCOMES = frozenset(get_args(TerminalOutcome))
_FAILURE_CLASSIFICATIONS = frozenset(get_args(FailureClassification))
_FAILURE_PHASES = frozenset(get_args(FailurePhase))
_CLEANUP_STATUSES = frozenset(get_args(CleanupStatus))
_REASONING_EFFORTS = frozenset({"low", "medium", "high"})


class ArtifactError(RuntimeError):
    """An artifact could not be validated or persisted safely."""


@dataclass(frozen=True)
class AssistantRequest:
    run_id: str
    prompt: RenderedPrompt
    requested_model: str
    requested_reasoning: str

    def validate(self) -> None:
        validate_run_id(self.run_id)
        if not self.requested_model.strip():
            raise ArtifactError("Assistant request requires a model")
        if self.requested_reasoning not in _REASONING_EFFORTS:
            raise ArtifactError("Assistant request has invalid reasoning")
        if not self.prompt.text.strip():
            raise ArtifactError("Assistant request requires a prompt")
        if self.run_id in self.prompt.text:
            raise ArtifactError("Assistant prompt must not contain the run identity")
        rendered_hash = hashlib.sha256(self.prompt.text.encode()).hexdigest()
        if rendered_hash != self.prompt.provenance.rendered_sha256:
            raise ArtifactError("Assistant prompt provenance does not match its content")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "sregym.assistant_v3.request.v1",
            "problem_id": self.run_id,
            "prompt": self.prompt.text,
            "prompt_profile_id": self.prompt.provenance.profile_id,
            "prompt_sha256": self.prompt.provenance.rendered_sha256,
            "reference_sha256": self.prompt.provenance.reference_sha256,
            "substitution_ids": list(self.prompt.provenance.substitution_ids),
            "requested_model": self.requested_model,
            "requested_reasoning": self.requested_reasoning,
            "session_id": None,
            "surface": None,
        }


@dataclass(frozen=True)
class AssistantTerminal:
    outcome: TerminalOutcome
    session_id: str | None
    final_text: str | None
    submitted: bool
    submission_count: int
    resolved_model: str | None
    resolved_reasoning: str | None

    def validate(self) -> None:
        if self.outcome not in _TERMINAL_OUTCOMES:
            raise ValueError("invalid Assistant terminal outcome")
        if self.submission_count < 0:
            raise ValueError("submission count must be non-negative")
        if self.submitted != (self.submission_count == 1):
            raise ValueError("submission state must match exactly one submission")
        if self.submitted and self.outcome != "completed":
            raise ValueError("only a completed Assistant result may be submitted")
        if self.outcome == "completed":
            if not isinstance(self.final_text, str) or not self.final_text.strip():
                raise ValueError("completed Assistant result requires final text")
        elif self.final_text is not None:
            raise ValueError("non-completed Assistant result must not contain final text")
        if self.resolved_reasoning is not None and self.resolved_reasoning not in _REASONING_EFFORTS:
            raise ValueError("resolved reasoning is invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "sregym.assistant_v3.terminal.v1",
            "outcome": self.outcome,
            "session_id": self.session_id,
            "final_text": self.final_text,
            "submitted": self.submitted,
            "submission_count": self.submission_count,
            "resolved_model": self.resolved_model,
            "resolved_reasoning": self.resolved_reasoning,
        }


@dataclass(frozen=True)
class AssistantRunMetadata:
    run_id: str
    attempt: int
    agent_name: str
    agent_version: str | None
    benchmark_profile: str
    comparable: bool
    capability_profile: str
    requested_model: str
    resolved_model: str | None
    requested_reasoning: str
    resolved_reasoning: str | None
    judge_model: str
    judge_backend: str
    prompt_provenance: PromptProvenance
    observability_provider: str
    observability_chart_version: str | None
    hec_index: str | None
    logs_connection_id: str | None
    readiness_report: ReadinessReport | None
    attempt_started_at: datetime
    agent_started_at: datetime | None
    agent_ended_at: datetime | None
    classification: TerminalOutcome

    def validate(self) -> None:
        validate_run_id(self.run_id)
        if self.attempt < 1:
            raise ArtifactError("attempt number must be positive")
        required_strings = (
            self.agent_name,
            self.benchmark_profile,
            self.requested_model,
            self.judge_model,
            self.judge_backend,
            self.observability_provider,
        )
        if any(not value.strip() for value in required_strings):
            raise ArtifactError("run metadata contains an empty required field")
        if self.capability_profile != CAPABILITY_PROFILE:
            raise ArtifactError("run metadata has an unexpected capability profile")
        if self.requested_reasoning not in _REASONING_EFFORTS:
            raise ArtifactError("run metadata has invalid requested reasoning")
        if self.resolved_reasoning is not None and self.resolved_reasoning not in _REASONING_EFFORTS:
            raise ArtifactError("run metadata has invalid resolved reasoning")
        if self.classification not in _TERMINAL_OUTCOMES:
            raise ArtifactError("run metadata has invalid classification")
        timestamps = tuple(
            value
            for value in (self.attempt_started_at, self.agent_started_at, self.agent_ended_at)
            if value is not None
        )
        if any(value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value) for value in timestamps):
            raise ArtifactError("run metadata timestamps must be UTC")
        if self.agent_started_at is not None and self.agent_started_at < self.attempt_started_at:
            raise ArtifactError("agent start cannot precede the attempt")
        if self.agent_ended_at is not None and (
            self.agent_started_at is None or self.agent_ended_at < self.agent_started_at
        ):
            raise ArtifactError("agent end requires an ordered agent start")
        if self.readiness_report is not None and self.readiness_report.run_id != self.run_id:
            raise ArtifactError("readiness and run metadata identities must match")

    def as_dict(self, *, classification: TerminalOutcome) -> dict[str, Any]:
        return {
            "schema": "sregym.assistant_v3.run_metadata.v1",
            "problem_id": self.run_id,
            "run_id": self.run_id,
            "attempt": self.attempt,
            "agent_name": self.agent_name,
            "agent_version": self.agent_version,
            "benchmark_profile": self.benchmark_profile,
            "comparable": self.comparable,
            "capability_profile": self.capability_profile,
            "requested_model": self.requested_model,
            "resolved_model": self.resolved_model,
            "requested_reasoning": self.requested_reasoning,
            "resolved_reasoning": self.resolved_reasoning,
            "judge_model": self.judge_model,
            "judge_backend": self.judge_backend,
            "prompt_provenance": asdict(self.prompt_provenance),
            "observability_provider": self.observability_provider,
            "observability_chart_version": self.observability_chart_version,
            "hec_index": self.hec_index,
            "logs_connection_id": self.logs_connection_id,
            "readiness_report": asdict(self.readiness_report) if self.readiness_report is not None else None,
            "attempt_started_at": self.attempt_started_at,
            "agent_started_at": self.agent_started_at,
            "agent_ended_at": self.agent_ended_at,
            "classification": classification,
            "included_in_diagnosis_pass_rate": classification != "infrastructure_invalid",
        }


@dataclass(frozen=True)
class AssistantFailure:
    classification: FailureClassification
    safe_message: str
    last_sequence: int
    retry_count: int
    phase: FailurePhase
    cleanup_status: CleanupStatus

    def validate(self) -> None:
        if self.classification not in _FAILURE_CLASSIFICATIONS:
            raise ArtifactError("invalid failure classification")
        if not self.safe_message.strip():
            raise ArtifactError("failure message must be non-empty")
        if self.last_sequence < 0 or self.retry_count < 0:
            raise ArtifactError("failure counters must be non-negative")
        if self.phase not in _FAILURE_PHASES:
            raise ArtifactError("invalid failure phase")
        if self.cleanup_status not in _CLEANUP_STATUSES:
            raise ArtifactError("invalid cleanup status")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "sregym.assistant_v3.failure.v1",
            "classification": self.classification,
            "safe_message": self.safe_message,
            "last_sequence": self.last_sequence,
            "retry_count": self.retry_count,
            "phase": self.phase,
            "cleanup_status": self.cleanup_status,
            "included_in_diagnosis_pass_rate": self.classification != "infrastructure_invalid",
        }


@dataclass(frozen=True)
class AssistantMetrics:
    agent_duration_ms: float
    time_to_first_event_ms: float | None
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    tool_calls: int
    failed_tool_results: int
    terminal_outcome: TerminalOutcome

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "sregym.assistant_v3.metrics.v1",
            "agent_duration_ms": self.agent_duration_ms,
            "time_to_first_event_ms": self.time_to_first_event_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "tool_calls": self.tool_calls,
            "failed_tool_results": self.failed_tool_results,
            "terminal_outcome": self.terminal_outcome,
        }


@dataclass(frozen=True)
class AssistantArtifactBundle:
    request: AssistantRequest
    events: tuple[AssistantEvent, ...]
    terminal: AssistantTerminal
    metadata: AssistantRunMetadata
    agent_duration_ms: float
    delivery: DeliveryReport | None = None
    failure: AssistantFailure | None = None
    retry_count: int = 0
    cleanup_status: CleanupStatus = "pending"


@dataclass(frozen=True)
class ArtifactWriteResult:
    classification: TerminalOutcome
    metrics: AssistantMetrics
    files: tuple[Path, ...]


def derive_metrics(
    events: tuple[AssistantEvent, ...],
    *,
    agent_duration_ms: float,
    terminal_outcome: TerminalOutcome,
) -> AssistantMetrics:
    """Derive the v1 metrics solely from native events and terminal state."""
    _validate_events(events, agent_duration_ms)
    if terminal_outcome not in _TERMINAL_OUTCOMES:
        raise ArtifactError("metrics require a valid terminal outcome")

    first_event = next((item.offset_ms for item in events if item.event != "ping"), None)
    terminal_usage: Mapping[str, Any] | None = None
    tool_call_ids: set[str] = set()
    failed_result_ids: set[str] = set()
    for item in events:
        if item.event == "assistant.usage":
            usage = item.data.get("usage")
            terminal_usage = usage if isinstance(usage, dict) else None
        if item.event == "tool.use":
            tool_call_ids.add(_stable_or_sequence_id(item.data.get("id"), item.sequence))
        elif item.event == "assistant.subagent.action" and isinstance(item.data.get("tool_name"), str):
            tool_call_ids.add(_stable_or_sequence_id(item.data.get("action_id"), item.sequence))

        if item.data.get("is_error") is not True:
            continue
        if item.event == "tool.result":
            failed_result_ids.add(_stable_or_sequence_id(item.data.get("tool_use_id"), item.sequence))
        elif item.event == "assistant.subagent.action":
            failed_result_ids.add(_stable_or_sequence_id(item.data.get("action_id"), item.sequence))

    usage = terminal_usage or {}
    return AssistantMetrics(
        agent_duration_ms=agent_duration_ms,
        time_to_first_event_ms=first_event,
        input_tokens=_optional_nonnegative_int(usage.get("input_tokens")),
        output_tokens=_optional_nonnegative_int(usage.get("output_tokens")),
        reasoning_tokens=_optional_nonnegative_int(usage.get("reasoning_tokens")),
        total_tokens=_optional_nonnegative_int(usage.get("total_tokens")),
        tool_calls=len(tool_call_ids),
        failed_tool_results=len(failed_result_ids),
        terminal_outcome=terminal_outcome,
    )


class AssistantArtifactStore:
    """Validate and atomically replace one attempt's deterministic artifacts."""

    def __init__(self, root: Path, *, secrets: Iterable[str] = ()) -> None:
        self.root = Path(root)
        self._secrets = tuple(sorted({value for value in secrets if value}, key=len, reverse=True))

    def write(self, bundle: AssistantArtifactBundle) -> ArtifactWriteResult:
        classification, failure = self._validate_bundle(bundle)
        metrics = derive_metrics(
            bundle.events,
            agent_duration_ms=bundle.agent_duration_ms,
            terminal_outcome=bundle.terminal.outcome,
        )
        payloads: dict[Path, bytes] = {
            Path("assistant_v3/request.json"): _json_bytes(bundle.request.as_dict()),
            Path("assistant_v3/events.jsonl"): _events_bytes(bundle.request.run_id, bundle.events),
            Path("assistant_v3/terminal.json"): _json_bytes(bundle.terminal.as_dict()),
            Path("run_metadata.json"): _json_bytes(bundle.metadata.as_dict(classification=classification)),
            Path("metrics.json"): _json_bytes(metrics.as_dict()),
        }
        if failure is not None:
            payloads[Path("failure.json")] = _json_bytes(failure.as_dict())
        if bundle.delivery is not None:
            payloads[Path("observability/delivery.json")] = _json_bytes(asdict(bundle.delivery))

        self._scan_for_secrets(payloads)
        written: list[Path] = []
        for relative_path, content in payloads.items():
            destination = self.root / relative_path
            _atomic_write(destination, content)
            written.append(destination)
        self._remove_stale_optional_file(Path("failure.json"), present=failure is not None)
        self._remove_stale_optional_file(
            Path("observability/delivery.json"),
            present=bundle.delivery is not None,
        )
        return ArtifactWriteResult(classification=classification, metrics=metrics, files=tuple(written))

    def _validate_bundle(self, bundle: AssistantArtifactBundle) -> tuple[TerminalOutcome, AssistantFailure | None]:
        try:
            bundle.request.validate()
            bundle.terminal.validate()
            bundle.metadata.validate()
        except ValueError as error:
            raise ArtifactError("Assistant artifact bundle is invalid") from error
        if bundle.retry_count < 0 or bundle.cleanup_status not in _CLEANUP_STATUSES:
            raise ArtifactError("Assistant bundle retry or cleanup state is invalid")
        _validate_events(bundle.events, bundle.agent_duration_ms)
        run_id = bundle.request.run_id
        if bundle.metadata.run_id != run_id:
            raise ArtifactError("request and metadata run identities must match")
        if bundle.delivery is not None and bundle.delivery.run_id != run_id:
            raise ArtifactError("delivery and request run identities must match")
        if bundle.metadata.prompt_provenance != bundle.request.prompt.provenance:
            raise ArtifactError("request and metadata prompt provenance must match")
        if bundle.metadata.requested_model != bundle.request.requested_model:
            raise ArtifactError("request and metadata models must match")
        if bundle.metadata.requested_reasoning != bundle.request.requested_reasoning:
            raise ArtifactError("request and metadata reasoning must match")
        if bundle.metadata.classification != bundle.terminal.outcome:
            raise ArtifactError("metadata and Assistant terminal classifications must initially match")

        delivery_invalid = bundle.delivery is not None and not bundle.delivery.valid
        classification: TerminalOutcome = "infrastructure_invalid" if delivery_invalid else bundle.terminal.outcome
        failure = bundle.failure
        if delivery_invalid and failure is None:
            failure = AssistantFailure(
                classification="infrastructure_invalid",
                safe_message="Post-execution telemetry delivery verification failed",
                last_sequence=bundle.events[-1].sequence if bundle.events else 0,
                retry_count=bundle.retry_count,
                phase="delivery",
                cleanup_status=bundle.cleanup_status,
            )
        if classification == "completed" and failure is not None:
            raise ArtifactError("completed attempt must not contain a failure artifact")
        if classification != "completed" and failure is None:
            raise ArtifactError("non-completed attempt requires a failure artifact")
        if failure is not None:
            failure.validate()
            if failure.classification != classification:
                raise ArtifactError("failure and attempt classifications must match")
            last_sequence = bundle.events[-1].sequence if bundle.events else 0
            if failure.last_sequence != last_sequence:
                raise ArtifactError("failure last sequence must match received events")
        return classification, failure

    def _scan_for_secrets(self, payloads: Mapping[Path, bytes]) -> None:
        for secret in self._secrets:
            encoded = secret.encode()
            if any(encoded in content for content in payloads.values()):
                raise ArtifactError("artifact output contains a configured secret")

    def _remove_stale_optional_file(self, relative_path: Path, *, present: bool) -> None:
        if present:
            return
        try:
            (self.root / relative_path).unlink(missing_ok=True)
        except OSError:
            raise ArtifactError("stale artifact could not be removed safely") from None


def _validate_events(events: tuple[AssistantEvent, ...], agent_duration_ms: float) -> None:
    if not math.isfinite(agent_duration_ms) or agent_duration_ms < 0:
        raise ArtifactError("agent duration must be a finite non-negative value")
    previous_offset = -1.0
    for expected_sequence, item in enumerate(events, start=1):
        if item.sequence != expected_sequence:
            raise ArtifactError("event sequence must be contiguous and receive ordered")
        if not math.isfinite(item.offset_ms) or item.offset_ms < previous_offset or item.offset_ms < 0:
            raise ArtifactError("event offsets must be finite, non-negative, and monotonic")
        previous_offset = item.offset_ms
    if events and agent_duration_ms < events[-1].offset_ms:
        raise ArtifactError("agent duration must cover every received event")


def _stable_or_sequence_id(value: Any, sequence: int) -> str:
    if isinstance(value, str) and value:
        return value
    return f"sequence:{sequence}"


def _optional_nonnegative_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _events_bytes(run_id: str, events: tuple[AssistantEvent, ...]) -> bytes:
    records: list[dict[str, Any]] = [{"schema": "sregym.assistant_v3.events.v1", "problem_id": run_id}]
    records.extend(
        {
            "sequence": item.sequence,
            "offset_ms": item.offset_ms,
            "event": item.event,
            "event_id": item.event_id,
            "data": item.data,
            "redacted": item.redacted,
        }
        for item in events
    )
    return ("\n".join(_canonical_json(record) for record in records) + "\n").encode()


def _json_bytes(value: Any) -> bytes:
    normalized = _normalize_json(value)
    try:
        serialized = json.dumps(normalized, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    except (TypeError, ValueError):
        raise ArtifactError("artifact value is not safe deterministic JSON") from None
    return f"{serialized}\n".encode()


def _canonical_json(value: Any) -> str:
    normalized = _normalize_json(value)
    try:
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ArtifactError("event value is not safe deterministic JSON") from None


def _normalize_json(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ArtifactError("artifact timestamps must be UTC")
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): _normalize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(item) for item in value]
    return value


def _atomic_write(path: Path, content: bytes) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    except OSError:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ArtifactError("artifact could not be written atomically") from None


__all__ = [
    "CAPABILITY_PROFILE",
    "ArtifactError",
    "ArtifactWriteResult",
    "AssistantArtifactBundle",
    "AssistantArtifactStore",
    "AssistantFailure",
    "AssistantMetrics",
    "AssistantRequest",
    "AssistantRunMetadata",
    "AssistantTerminal",
    "CleanupStatus",
    "FailureClassification",
    "FailurePhase",
    "TerminalOutcome",
    "derive_metrics",
]
