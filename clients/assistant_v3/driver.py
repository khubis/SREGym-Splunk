"""Run-level artifacts and deterministic metrics for the Assistant v3 driver."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args
from uuid import UUID

import httpx

from clients.assistant_v3.client import AssistantEvent, AssistantV3Client, AssistantV3Config, AssistantV3Error
from clients.assistant_v3.prompt import PromptProvenance, RenderedPrompt, render_prompt
from sregym.observability.base import DeliveryReport, ReadinessReport, SignalReadiness, validate_run_id

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
    "telemetry_scope_violation",
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
    "telemetry_scope_violation",
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
_PREFLIGHT_REQUEST_ID = "00000000-0000-0000-0000-000000000000"


class ArtifactError(RuntimeError):
    """An artifact could not be validated or persisted safely."""


class ConductorError(RuntimeError):
    """The public Conductor contract was unavailable or inconsistent."""


def _assistant_request_id(run_id: str) -> str:
    """Format the anonymous SRE Gym identity for Assistant's UUID header contract."""
    validate_run_id(run_id)
    return str(UUID(hex=run_id.removeprefix("anon_")))


@dataclass(frozen=True)
class DriverRunConfig:
    run_id: str
    attempt: int
    artifacts_root: Path
    benchmark_profile: str
    comparable: bool
    judge_model: str
    judge_backend: str
    observability_provider: str
    readiness_report: ReadinessReport | None
    agent_version: str | None = None
    observability_chart_version: str | None = None
    hec_index: str | None = None
    logs_connection_id: str | None = None
    attempt_started_at: datetime | None = None
    telemetry_window_ended_at: datetime | None = None

    @classmethod
    def from_file(cls, path: Path) -> DriverRunConfig:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ArtifactError("Assistant driver configuration is unavailable or invalid") from None
        if not isinstance(payload, dict):
            raise ArtifactError("Assistant driver configuration must be an object")
        readiness_value = payload.get("readiness_report")
        readiness_report = _readiness_from_dict(readiness_value) if readiness_value is not None else None
        try:
            config = cls(
                run_id=payload["run_id"],
                attempt=payload["attempt"],
                artifacts_root=path.parent,
                benchmark_profile=payload["benchmark_profile"],
                comparable=payload["comparable"],
                judge_model=payload["judge_model"],
                judge_backend=payload["judge_backend"],
                observability_provider=payload["observability_provider"],
                readiness_report=readiness_report,
                agent_version=payload.get("agent_version"),
                observability_chart_version=payload.get("observability_chart_version"),
                hec_index=payload.get("hec_index"),
                logs_connection_id=payload.get("logs_connection_id"),
                attempt_started_at=(
                    datetime.fromisoformat(payload["attempt_started_at"].replace("Z", "+00:00"))
                    if isinstance(payload.get("attempt_started_at"), str)
                    else None
                ),
                telemetry_window_ended_at=(
                    datetime.fromisoformat(payload["telemetry_window_ended_at"].replace("Z", "+00:00"))
                    if isinstance(payload.get("telemetry_window_ended_at"), str)
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            raise ArtifactError("Assistant driver configuration is incomplete") from None
        config.validate()
        return config

    def validate(self) -> None:
        validate_run_id(self.run_id)
        if self.attempt < 1:
            raise ArtifactError("attempt number must be positive")
        if self.benchmark_profile not in {"full", "svelte"}:
            raise ArtifactError("benchmark profile must be full or svelte")
        if self.comparable != (self.benchmark_profile == "full"):
            raise ArtifactError("only the full benchmark profile is comparable")
        if not self.judge_model.strip() or not self.judge_backend.strip():
            raise ArtifactError("judge model and backend must be explicit")
        if self.readiness_report is not None and self.readiness_report.run_id != self.run_id:
            raise ArtifactError("readiness and driver identities must match")
        for label, timestamp in (
            ("attempt start", self.attempt_started_at),
            ("telemetry window end", self.telemetry_window_ended_at),
        ):
            if timestamp is not None and (
                timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp)
            ):
                raise ArtifactError(f"{label} must be UTC")
        if (
            self.attempt_started_at is not None
            and self.telemetry_window_ended_at is not None
            and self.telemetry_window_ended_at < self.attempt_started_at
        ):
            raise ArtifactError("telemetry window end must not precede its start")


class ConductorClient:
    """Minimal client for the three existing public Conductor endpoints."""

    def __init__(self, base_url: str, *, http_client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = http_client or httpx.Client(timeout=30)
        self._owns_client = http_client is None
        self._submitted = False

    def require_diagnosis(self) -> None:
        response = self._client.get(f"{self.base_url}/status")
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeError):
            raise ConductorError("Conductor status response is invalid") from None
        if response.status_code != 200 or not isinstance(payload, dict) or payload.get("stage") != "diagnosis":
            raise ConductorError("Conductor is not ready for the diagnosis stage")

    def get_prompt_context(self) -> dict[str, str]:
        response = self._client.get(f"{self.base_url}/get_app")
        if response.status_code != 200:
            raise ConductorError("Conductor application metadata is unavailable")
        try:
            payload = response.json()
        except (json.JSONDecodeError, UnicodeError):
            raise ConductorError("Conductor application metadata is invalid") from None
        if not isinstance(payload, dict):
            raise ConductorError("Conductor application metadata is invalid")
        app_name = payload.get("app_name")
        description = payload.get("descriptions")
        namespaces = payload.get("namespaces")
        namespace = payload.get("namespace")
        if not isinstance(app_name, str) or not isinstance(description, str):
            raise ConductorError("Conductor application metadata is invalid")
        if isinstance(namespaces, list) and namespaces and all(isinstance(item, str) for item in namespaces):
            app_namespace = ", ".join(namespaces)
        elif isinstance(namespace, str) and namespace:
            app_namespace = namespace
        else:
            raise ConductorError("Conductor application metadata is invalid")
        return {
            "app_name": app_name,
            "app_description": description,
            "app_namespace": app_namespace,
        }

    def submit_diagnosis(self, diagnosis: str) -> None:
        if self._submitted:
            raise ConductorError("Diagnosis submission was already attempted")
        self._submitted = True
        response = self._client.post(
            f"{self.base_url}/submit",
            json={"solution": diagnosis, "stage": "diagnosis"},
        )
        if response.status_code != 200:
            raise ConductorError("Conductor diagnosis submission was not accepted")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _readiness_from_dict(value: Any) -> ReadinessReport:
    if not isinstance(value, dict) or not isinstance(value.get("signals"), list):
        raise ArtifactError("Assistant readiness configuration is invalid")
    try:
        signals = tuple(
            SignalReadiness(
                signal=item["signal"],
                ready=item["ready"],
                checked_at=datetime.fromisoformat(item["checked_at"].replace("Z", "+00:00")),
                evidence=item["evidence"],
            )
            for item in value["signals"]
        )
        return ReadinessReport(run_id=value["run_id"], signals=signals, ready=value["ready"])
    except (KeyError, TypeError, ValueError):
        raise ArtifactError("Assistant readiness configuration is invalid") from None


@dataclass(frozen=True)
class AssistantRequest:
    run_id: str
    prompt: RenderedPrompt
    requested_model: str
    requested_reasoning: str
    action_instructions: str | None = None

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
        if self.action_instructions is not None and not self.action_instructions.strip():
            raise ArtifactError("Assistant action instructions must be non-empty")
        rendered_hash = hashlib.sha256(self.prompt.text.encode()).hexdigest()
        if rendered_hash != self.prompt.provenance.rendered_sha256:
            raise ArtifactError("Assistant prompt provenance does not match its content")

    def as_dict(self) -> dict[str, Any]:
        result = {
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
        if self.action_instructions is not None:
            result["action_instructions"] = self.action_instructions
        return result


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
            "included_in_diagnosis_pass_rate": classification
            not in {"infrastructure_invalid", "telemetry_scope_violation"},
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
            "included_in_diagnosis_pass_rate": self.classification
            not in {"infrastructure_invalid", "telemetry_scope_violation"},
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


_CLIENT_FAILURES: dict[str, FailureClassification] = {
    "configuration": "configuration_error",
    "authentication": "authentication_error",
    "permission": "permission_error",
    "transient_exhausted": "transient_exhausted",
    "incomplete_stream": "incomplete_stream",
    "assistant_error": "assistant_error",
    "ambiguous_completion": "ambiguous_completion",
    "capability_policy_violation": "capability_policy_violation",
}

_ABSOLUTE_START_KEYS = frozenset(
    {
        "from",
        "from_time",
        "start",
        "start_time",
        "start_timestamp",
        "starttime",
        "starttimestamp",
    }
)
_ABSOLUTE_END_KEYS = frozenset(
    {
        "end",
        "end_time",
        "end_timestamp",
        "endtime",
        "endtimestamp",
        "to",
        "to_time",
    }
)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _build_action_instructions(window_started_at: datetime, window_ended_at: datetime) -> str:
    return (
        f"Telemetry time window: {_format_utc(window_started_at)} through "
        f"{_format_utc(window_ended_at)}, inclusive. Investigate using only telemetry within this "
        "time window. If telemetry is unavailable in this window, report that instead of using "
        "data outside it."
    )


def _walk_values(value: Any) -> Iterable[tuple[str | None, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            yield normalized, item
            yield from _walk_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_values(item)


def _absolute_time(value: Any) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate or ("T" not in candidate and not candidate.endswith("Z")):
            return None
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)
    return None


def _scope_violation(
    events: Iterable[AssistantEvent], *, window_started_at: datetime, window_ended_at: datetime
) -> str | None:
    for event in events:
        if event.event != "tool.use":
            continue
        for key, value in _walk_values(event.data):
            if key not in _ABSOLUTE_START_KEYS and key not in _ABSOLUTE_END_KEYS:
                continue
            parsed = _absolute_time(value)
            if parsed is not None and parsed < window_started_at:
                return "Assistant tool trace queried telemetry before the provided time window"
            if parsed is not None and parsed > window_ended_at:
                return "Assistant tool trace queried telemetry after the provided time window"
    return None


def execute_assistant_attempt(
    config: DriverRunConfig,
    *,
    conductor: ConductorClient,
    assistant: AssistantV3Client,
    clock: Callable[[], float] = time.monotonic,
) -> ArtifactWriteResult:
    """Run one diagnosis turn and persist success or partial failure artifacts."""
    config.validate()
    started_at = datetime.now(UTC)
    started_clock = clock()
    rendered: RenderedPrompt | None = None
    action_instructions: str | None = None
    events: tuple[AssistantEvent, ...] = ()
    event_spool = config.artifacts_root / "assistant_v3" / "events.partial.jsonl"
    session_id: str | None = None
    retry_count = 0
    failure: AssistantFailure | None = None
    terminal: AssistantTerminal
    try:
        conductor.require_diagnosis()
        prompt_context = conductor.get_prompt_context()
        rendered = render_prompt(prompt_context)
        window_started_at = config.attempt_started_at or started_at
        window_ended_at = config.telemetry_window_ended_at or started_at
        action_instructions = _build_action_instructions(
            window_started_at,
            window_ended_at,
        )
        result = assistant.run_session(
            prompt=rendered.text,
            request_id=_assistant_request_id(config.run_id),
            action_instructions=action_instructions,
            event_sink=lambda item: _append_event_spool(event_spool, item),
        )
        events = result.events
        session_id = result.session_id
        retry_count = result.retry_count
        scope_failure = _scope_violation(
            events,
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
        )
        if scope_failure is not None:
            terminal = AssistantTerminal(
                outcome="telemetry_scope_violation",
                session_id=session_id,
                final_text=None,
                submitted=False,
                submission_count=0,
                resolved_model=assistant.configuration.model,
                resolved_reasoning=assistant.configuration.reasoning,
            )
            failure = AssistantFailure(
                classification="telemetry_scope_violation",
                safe_message=scope_failure,
                last_sequence=events[-1].sequence if events else 0,
                retry_count=retry_count,
                phase="assistant_execution",
                cleanup_status="pending",
            )
        else:
            try:
                conductor.submit_diagnosis(result.final_text)
            except (ConductorError, httpx.HTTPError):
                terminal = AssistantTerminal(
                    outcome="ambiguous_completion",
                    session_id=session_id,
                    final_text=None,
                    submitted=False,
                    submission_count=0,
                    resolved_model=assistant.configuration.model,
                    resolved_reasoning=assistant.configuration.reasoning,
                )
                failure = AssistantFailure(
                    classification="ambiguous_completion",
                    safe_message="Conductor diagnosis submission could not be confirmed",
                    last_sequence=events[-1].sequence if events else 0,
                    retry_count=retry_count,
                    phase="assistant_execution",
                    cleanup_status="pending",
                )
            else:
                terminal = AssistantTerminal(
                    outcome="completed",
                    session_id=session_id,
                    final_text=result.final_text,
                    submitted=True,
                    submission_count=1,
                    resolved_model=assistant.configuration.model,
                    resolved_reasoning=assistant.configuration.reasoning,
                )
    except AssistantV3Error as exc:
        events = exc.events
        retry_count = exc.retry_count
        classification = _CLIENT_FAILURES[exc.kind]
        terminal = AssistantTerminal(
            outcome=classification,
            session_id=session_id,
            final_text=None,
            submitted=False,
            submission_count=0,
            resolved_model=assistant.configuration.model,
            resolved_reasoning=assistant.configuration.reasoning,
        )
        failure = AssistantFailure(
            classification=classification,
            safe_message=str(exc),
            last_sequence=events[-1].sequence if events else 0,
            retry_count=retry_count,
            phase="assistant_execution",
            cleanup_status="pending",
        )
    except (ConductorError, httpx.HTTPError) as exc:
        terminal = AssistantTerminal(
            outcome="configuration_error",
            session_id=None,
            final_text=None,
            submitted=False,
            submission_count=0,
            resolved_model=assistant.configuration.model,
            resolved_reasoning=assistant.configuration.reasoning,
        )
        failure = AssistantFailure(
            classification="configuration_error",
            safe_message=str(exc),
            last_sequence=0,
            retry_count=0,
            phase="assistant_execution",
            cleanup_status="pending",
        )

    ended_at = datetime.now(UTC)
    duration_ms = max(0.0, (clock() - started_clock) * 1000)
    if rendered is None:
        # Conductor failures before metadata retrieval still need a deterministic,
        # provenance-backed request artifact without inventing application facts.
        rendered = render_prompt(
            {"app_name": "unavailable", "app_description": "unavailable", "app_namespace": "unavailable"}
        )
    if action_instructions is None:
        window_started_at = config.attempt_started_at or started_at
        window_ended_at = config.telemetry_window_ended_at or started_at
        action_instructions = _build_action_instructions(
            window_started_at,
            window_ended_at,
        )
    request = AssistantRequest(
        run_id=config.run_id,
        prompt=rendered,
        requested_model=assistant.configuration.model,
        requested_reasoning=assistant.configuration.reasoning,
        action_instructions=action_instructions,
    )
    metadata = AssistantRunMetadata(
        run_id=config.run_id,
        attempt=config.attempt,
        agent_name="assistant_v3",
        agent_version=config.agent_version,
        benchmark_profile=config.benchmark_profile,
        comparable=config.comparable,
        capability_profile=CAPABILITY_PROFILE,
        requested_model=assistant.configuration.model,
        resolved_model=assistant.configuration.model,
        requested_reasoning=assistant.configuration.reasoning,
        resolved_reasoning=assistant.configuration.reasoning,
        judge_model=config.judge_model,
        judge_backend=config.judge_backend,
        prompt_provenance=rendered.provenance,
        observability_provider=config.observability_provider,
        observability_chart_version=config.observability_chart_version,
        hec_index=config.hec_index,
        logs_connection_id=config.logs_connection_id,
        readiness_report=config.readiness_report,
        attempt_started_at=config.attempt_started_at or started_at,
        agent_started_at=started_at,
        agent_ended_at=ended_at,
        classification=terminal.outcome,
    )
    write_result = AssistantArtifactStore(
        config.artifacts_root,
        secrets=(assistant.configuration.auth_token, assistant.configuration.sf_token),
    ).write(
        AssistantArtifactBundle(
            request=request,
            events=events,
            terminal=terminal,
            metadata=metadata,
            agent_duration_ms=duration_ms,
            failure=failure,
            retry_count=retry_count,
            cleanup_status="pending",
        )
    )
    event_spool.unlink(missing_ok=True)
    return write_result


def _append_event_spool(path: Path, event: AssistantEvent) -> None:
    """Durably retain each redacted event before terminal interpretation."""
    record = {
        "sequence": event.sequence,
        "offset_ms": event.offset_ms,
        "event": event.event,
        "event_id": event.event_id,
        "data": event.data,
        "redacted": event.redacted,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as spool:
            spool.write((_canonical_json(record) + "\n").encode())
            spool.flush()
            os.fsync(spool.fileno())
    except OSError:
        raise ArtifactError("Assistant event spool could not be written") from None


def write_pre_agent_failure(
    config: DriverRunConfig,
    *,
    assistant_configuration: AssistantV3Config,
    safe_message: str,
    prompt_context: Mapping[str, str],
) -> ArtifactWriteResult:
    """Persist an infrastructure-invalid attempt that never launched Assistant."""
    config.validate()
    rendered = render_prompt(prompt_context)
    timestamp = datetime.now(UTC)
    terminal = AssistantTerminal(
        outcome="infrastructure_invalid",
        session_id=None,
        final_text=None,
        submitted=False,
        submission_count=0,
        resolved_model=None,
        resolved_reasoning=None,
    )
    failure = AssistantFailure(
        classification="infrastructure_invalid",
        safe_message=safe_message,
        last_sequence=0,
        retry_count=0,
        phase="provider_preflight",
        cleanup_status="completed",
    )
    metadata = AssistantRunMetadata(
        run_id=config.run_id,
        attempt=config.attempt,
        agent_name="assistant_v3",
        agent_version=config.agent_version,
        benchmark_profile=config.benchmark_profile,
        comparable=config.comparable,
        capability_profile=CAPABILITY_PROFILE,
        requested_model=assistant_configuration.model,
        resolved_model=None,
        requested_reasoning=assistant_configuration.reasoning,
        resolved_reasoning=None,
        judge_model=config.judge_model,
        judge_backend=config.judge_backend,
        prompt_provenance=rendered.provenance,
        observability_provider=config.observability_provider,
        observability_chart_version=config.observability_chart_version,
        hec_index=config.hec_index,
        logs_connection_id=config.logs_connection_id,
        readiness_report=config.readiness_report,
        attempt_started_at=config.attempt_started_at or timestamp,
        agent_started_at=None,
        agent_ended_at=None,
        classification="infrastructure_invalid",
    )
    return AssistantArtifactStore(
        config.artifacts_root,
        secrets=(assistant_configuration.auth_token, assistant_configuration.sf_token),
    ).write(
        AssistantArtifactBundle(
            request=AssistantRequest(
                run_id=config.run_id,
                prompt=rendered,
                requested_model=assistant_configuration.model,
                requested_reasoning=assistant_configuration.reasoning,
            ),
            events=(),
            terminal=terminal,
            metadata=metadata,
            agent_duration_ms=0.0,
            failure=failure,
            cleanup_status="completed",
        )
    )


def finalize_attempt_artifacts(
    root: Path,
    *,
    delivery: DeliveryReport | None,
    cleanup_status: CleanupStatus,
    infrastructure_error: str | None = None,
) -> None:
    """Attach trusted post-agent delivery/cleanup evidence without rewriting the trace."""
    if cleanup_status not in _CLEANUP_STATUSES:
        raise ArtifactError("invalid cleanup status")
    metadata_path = root / "run_metadata.json"
    terminal_path = root / "assistant_v3" / "terminal.json"
    events_path = root / "assistant_v3" / "events.jsonl"
    try:
        metadata_value = json.loads(metadata_path.read_text(encoding="utf-8"))
        terminal_value = json.loads(terminal_path.read_text(encoding="utf-8"))
        event_lines = events_path.read_text(encoding="utf-8").splitlines()[1:]
    except (OSError, json.JSONDecodeError, TypeError):
        raise ArtifactError("Assistant artifacts are unavailable for finalization") from None
    if not isinstance(metadata_value, dict) or not isinstance(terminal_value, dict):
        raise ArtifactError("Assistant artifacts are invalid for finalization")

    invalid_delivery = infrastructure_error is not None or (delivery is not None and not delivery.valid)
    failure_path = root / "failure.json"
    failure_value: dict[str, Any] | None = None
    if failure_path.exists():
        try:
            loaded_failure = json.loads(failure_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ArtifactError("Assistant failure artifact is invalid") from None
        if not isinstance(loaded_failure, dict):
            raise ArtifactError("Assistant failure artifact is invalid")
        failure_value = loaded_failure

    if invalid_delivery:
        metadata_value["classification"] = "infrastructure_invalid"
        metadata_value["included_in_diagnosis_pass_rate"] = False
        failure_value = AssistantFailure(
            classification="infrastructure_invalid",
            safe_message=infrastructure_error or "Post-execution telemetry delivery verification failed",
            last_sequence=len(event_lines),
            retry_count=0,
            phase="delivery",
            cleanup_status=cleanup_status,
        ).as_dict()
    elif failure_value is not None:
        failure_value["cleanup_status"] = cleanup_status

    _atomic_write(metadata_path, _json_bytes(metadata_value))
    if delivery is not None:
        _atomic_write(root / "observability" / "delivery.json", _json_bytes(asdict(delivery)))
    if failure_value is not None:
        _atomic_write(failure_path, _json_bytes(failure_value))


def run_preflight() -> None:
    """Validate Assistant configuration and authenticated connectivity."""
    configuration = AssistantV3Config.from_env()
    client = AssistantV3Client(configuration)
    try:
        client.preflight(request_id=_PREFLIGHT_REQUEST_ID)
    finally:
        client.close()


def main() -> int:
    """Run the registry-launched Assistant driver for one prepared attempt."""
    config_path = Path(os.environ.get("SREGYM_ASSISTANT_DRIVER_CONFIG", "/logs/assistant_v3_driver_config.json"))
    config = DriverRunConfig.from_file(config_path)
    assistant_configuration = AssistantV3Config.from_env()
    api_host = os.environ.get("API_HOSTNAME", "localhost")
    api_port = os.environ.get("API_PORT", "8000")
    conductor = ConductorClient(f"http://{api_host}:{api_port}")
    assistant = AssistantV3Client(assistant_configuration)
    try:
        result = execute_assistant_attempt(config, conductor=conductor, assistant=assistant)
    finally:
        assistant.close()
        conductor.close()
    return 0 if result.classification == "completed" else 1


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
    "ConductorClient",
    "ConductorError",
    "CleanupStatus",
    "DriverRunConfig",
    "FailureClassification",
    "FailurePhase",
    "TerminalOutcome",
    "derive_metrics",
    "execute_assistant_attempt",
    "finalize_attempt_artifacts",
    "main",
    "run_preflight",
    "write_pre_agent_failure",
]


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    try:
        raise SystemExit(main())
    except (ArtifactError, AssistantV3Error, ConductorError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
