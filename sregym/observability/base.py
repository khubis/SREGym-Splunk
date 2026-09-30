"""Provider-neutral observability lifecycle types."""

import re
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, get_args

ProviderName = Literal["none", "splunk"]
SignalName = Literal["metrics", "traces", "logs", "kubernetes_events"]
FailureKind = Literal[
    "configuration",
    "authentication",
    "permission",
    "transient_exhausted",
    "readiness_timeout",
    "cleanup",
]

SIGNAL_NAMES: tuple[SignalName, ...] = ("metrics", "traces", "logs", "kubernetes_events")
_FAILURE_KINDS = frozenset(get_args(FailureKind))
_RUN_ID_PATTERN = re.compile(r"anon_[0-9a-f]{32}\Z")


def new_run_id() -> str:
    """Allocate an opaque identity that contains no benchmark semantics."""
    return f"anon_{secrets.token_hex(16)}"


def validate_run_id(run_id: str) -> str:
    """Return a canonical opaque identity or reject it before side effects."""
    if not isinstance(run_id, str) or _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("invalid opaque run identity")
    return run_id


def _validate_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise ValueError("attempt_started_at must be timezone-aware UTC")


@dataclass(frozen=True)
class AttemptContext:
    run_id: str
    profile: str
    comparable: bool
    attempt_started_at: datetime

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        _validate_utc(self.attempt_started_at)


@dataclass(frozen=True)
class ApplicationScope:
    app_name: str
    namespaces: tuple[str, ...]


@dataclass(frozen=True)
class SignalReadiness:
    signal: SignalName
    ready: bool
    checked_at: datetime
    evidence: dict[str, str | int | float | bool | None]


@dataclass(frozen=True)
class ReadinessReport:
    run_id: str
    signals: tuple[SignalReadiness, ...]
    ready: bool

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        observed = tuple(signal.signal for signal in self.signals)
        if len(observed) != len(SIGNAL_NAMES) or set(observed) != set(SIGNAL_NAMES):
            raise ValueError("readiness report must contain every signal exactly once")
        if self.ready != all(signal.ready for signal in self.signals):
            raise ValueError("readiness summary must match its signals")


@dataclass(frozen=True)
class DeliveryReport:
    run_id: str
    opening: ReadinessReport
    closing: ReadinessReport
    first_visible_lag_ms: dict[SignalName, float | None]
    sent_delta: dict[SignalName, int | None]
    send_failed_delta: dict[SignalName, int | None]
    enqueue_failed_delta: dict[SignalName, int | None]
    queue_high_water: dict[SignalName, int | None]
    queue_final_size: dict[SignalName, int | None]
    drained: bool
    valid: bool
    queue_drain_minimum: dict[SignalName, int | None] | None = None
    counter_samples: dict[str, dict[str, dict[SignalName, int | None]]] | None = None

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if self.opening.run_id != self.run_id or self.closing.run_id != self.run_id:
            raise ValueError("delivery and readiness reports must share one run identity")


@dataclass(frozen=True)
class ExternalOtlpExport:
    endpoint: str
    run_id: str
    resource_attributes: dict[str, str]

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)


class ProviderError(RuntimeError):
    """A classified provider failure with an artifact-safe public message."""

    def __init__(
        self,
        kind: FailureKind,
        safe_message: str,
        *,
        readiness_report: ReadinessReport | None = None,
    ) -> None:
        if kind not in _FAILURE_KINDS:
            raise ValueError("invalid provider failure kind")
        super().__init__(safe_message)
        self.kind = kind
        self.readiness_report = readiness_report


class ObservabilityProvider(Protocol):
    name: ProviderName

    def preflight(self) -> None: ...

    def prepare_attempt(self, context: AttemptContext) -> ExternalOtlpExport | None: ...

    def wait_until_queryable(self, context: AttemptContext, scope: ApplicationScope) -> ReadinessReport: ...

    def finish_attempt(self, context: AttemptContext, scope: ApplicationScope) -> DeliveryReport: ...

    def close(self) -> None: ...


class NullObservabilityProvider:
    """Default provider that preserves the benchmark's local-only behavior."""

    name: ProviderName = "none"

    def preflight(self) -> None:
        return None

    def prepare_attempt(self, context: AttemptContext) -> None:
        return None

    def wait_until_queryable(self, context: AttemptContext, scope: ApplicationScope) -> ReadinessReport:
        return _ready_report(context.run_id)

    def finish_attempt(self, context: AttemptContext, scope: ApplicationScope) -> DeliveryReport:
        opening = _ready_report(context.run_id)
        closing = _ready_report(context.run_id)
        unavailable = dict.fromkeys(SIGNAL_NAMES)
        return DeliveryReport(
            run_id=context.run_id,
            opening=opening,
            closing=closing,
            first_visible_lag_ms=unavailable.copy(),
            sent_delta=unavailable.copy(),
            send_failed_delta=unavailable.copy(),
            enqueue_failed_delta=unavailable.copy(),
            queue_high_water=unavailable.copy(),
            queue_final_size=unavailable.copy(),
            drained=True,
            valid=True,
        )

    def close(self) -> None:
        return None


ProviderArtifact = (
    AttemptContext
    | ApplicationScope
    | SignalReadiness
    | ReadinessReport
    | DeliveryReport
    | ExternalOtlpExport
    | ProviderError
)
_SERIALIZABLE_TYPES = (
    AttemptContext,
    ApplicationScope,
    SignalReadiness,
    ReadinessReport,
    DeliveryReport,
    ExternalOtlpExport,
)


def serialize_provider_artifact(value: ProviderArtifact) -> dict[str, Any]:
    """Serialize only allowlisted provider contract values, never exception causes."""
    if isinstance(value, ProviderError):
        payload: dict[str, Any] = {
            "kind": value.kind,
            "message": str(value),
            "readiness_report": (
                serialize_provider_artifact(value.readiness_report) if value.readiness_report is not None else None
            ),
        }
        return payload
    if not isinstance(value, _SERIALIZABLE_TYPES):
        raise TypeError("unsupported provider artifact type")
    payload = _json_safe(asdict(value))
    if isinstance(value, DeliveryReport) and value.counter_samples is None:
        payload.pop("counter_samples")
    return payload


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _ready_report(run_id: str) -> ReadinessReport:
    checked_at = datetime.now(UTC)
    return ReadinessReport(
        run_id=run_id,
        signals=tuple(
            SignalReadiness(signal=signal, ready=True, checked_at=checked_at, evidence={}) for signal in SIGNAL_NAMES
        ),
        ready=True,
    )
