"""Authenticated, retry-bounded client for the Assistant v3 SSE API."""

from __future__ import annotations

import codecs
import ipaddress
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx

FailureKind = Literal[
    "configuration",
    "authentication",
    "permission",
    "transient_exhausted",
    "incomplete_stream",
    "assistant_error",
    "ambiguous_completion",
    "capability_policy_violation",
]
ReasoningEffort = Literal["low", "medium", "high"]

_REASONING_EFFORTS = frozenset({"low", "medium", "high"})
_TRANSIENT_STATUSES = frozenset({408, 429, *range(500, 600)})
_REDACTION_MARKER = "[REDACTED]"
_SESSION_PATH = "/v2/assistant/sessions"


class AssistantV3Error(RuntimeError):
    """A classified Assistant failure containing only artifact-safe context."""

    def __init__(
        self,
        kind: FailureKind,
        safe_message: str,
        *,
        status_code: int | None = None,
        retry_count: int = 0,
        events: tuple[AssistantEvent, ...] = (),
    ) -> None:
        super().__init__(safe_message)
        self.kind = kind
        self.status_code = status_code
        self.retry_count = retry_count
        self.events = events


@dataclass(frozen=True)
class AssistantV3Config:
    """Validated Assistant endpoint and explicit runtime selection."""

    base_url: str
    auth_token: str
    sf_token: str
    model: str
    reasoning: ReasoningEffort

    def __post_init__(self) -> None:
        base_url = _validated_base_url(self.base_url)
        if not isinstance(self.model, str) or not self.model.strip():
            raise AssistantV3Error("configuration", "Assistant configuration requires a non-empty model")
        if self.reasoning not in _REASONING_EFFORTS:
            raise AssistantV3Error("configuration", "Assistant configuration has an unsupported reasoning effort")
        if not isinstance(self.auth_token, str) or not self.auth_token.strip():
            raise AssistantV3Error("configuration", "Assistant configuration requires credentials")
        if not isinstance(self.sf_token, str) or not self.sf_token.strip():
            raise AssistantV3Error("configuration", "Assistant configuration requires credentials")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "auth_token", self.auth_token.strip())
        object.__setattr__(self, "sf_token", self.sf_token.strip())

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> AssistantV3Config:
        """Load required values without including their contents in failures."""
        source = os.environ if environment is None else environment
        names = (
            "ASSISTANT_V3_URL",
            "ASSISTANT_V3_AUTH_TOKEN",
            "SF_TOKEN",
            "AGENT_MODEL_ID",
            "AGENT_REASONING_EFFORT",
        )
        missing = [name for name in names if not isinstance(source.get(name), str) or not source[name].strip()]
        if missing:
            raise AssistantV3Error(
                "configuration",
                f"Assistant configuration is missing required variables: {', '.join(missing)}",
            )
        return cls(
            base_url=source["ASSISTANT_V3_URL"],
            auth_token=source["ASSISTANT_V3_AUTH_TOKEN"],
            sf_token=source["SF_TOKEN"],
            model=source["AGENT_MODEL_ID"],
            reasoning=cast(ReasoningEffort, source["AGENT_REASONING_EFFORT"]),
        )


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry and timeout settings for connectivity and pre-stream failures."""

    max_attempts: int = 3
    request_timeout_seconds: float = 600.0
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 8.0
    retry_after_cap_seconds: float = 15.0

    def __post_init__(self) -> None:
        numeric = (
            self.request_timeout_seconds,
            self.initial_backoff_seconds,
            self.max_backoff_seconds,
            self.retry_after_cap_seconds,
        )
        if self.max_attempts < 1 or any(value <= 0 for value in numeric):
            raise ValueError("retry policy values must be positive")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("maximum backoff must not be smaller than initial backoff")


@dataclass(frozen=True)
class AssistantEvent:
    """One parsed, redacted native SSE event in receive order."""

    sequence: int
    offset_ms: float
    event: str
    event_id: str | None
    data: dict[str, Any]
    redacted: bool


@dataclass(frozen=True)
class AssistantSessionResult:
    """Validated terminal result for one fresh Assistant session."""

    events: tuple[AssistantEvent, ...]
    final_text: str
    session_id: str | None
    usage: dict[str, Any] | None
    retry_count: int


class AssistantV3Client:
    """Use the existing Assistant session surface without exposing benchmark internals."""

    def __init__(
        self,
        configuration: AssistantV3Config,
        *,
        http_client: httpx.Client | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.configuration = configuration
        self.retry_policy = retry_policy or RetryPolicy()
        self._client = http_client or httpx.Client()
        self._owns_client = http_client is None
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic

    def preflight(self, *, request_id: str) -> None:
        """Prove authenticated read access without opening a model turn."""
        _validate_request_id(request_id)
        for attempt in range(1, self.retry_policy.max_attempts + 1):  # pragma: no branch - every final attempt exits
            try:
                response = self._client.get(
                    self._session_url,
                    params={"limit": 1},
                    headers=self._headers(request_id, accept="application/json"),
                    timeout=self.retry_policy.request_timeout_seconds,
                )
            except httpx.TransportError:
                if attempt == self.retry_policy.max_attempts:
                    raise AssistantV3Error(
                        "transient_exhausted",
                        "Assistant preflight exhausted transient connectivity retries",
                        retry_count=attempt - 1,
                    ) from None
                self._sleep(self._backoff(attempt, None))
                continue
            if 200 <= response.status_code < 300:
                return
            kind = _status_failure_kind(response.status_code)
            if kind is not None:
                raise AssistantV3Error(
                    kind,
                    f"Assistant preflight failed with {kind} status {response.status_code}",
                    status_code=response.status_code,
                    retry_count=attempt - 1,
                )
            if attempt == self.retry_policy.max_attempts:
                raise AssistantV3Error(
                    "transient_exhausted",
                    f"Assistant preflight exhausted transient status {response.status_code}",
                    status_code=response.status_code,
                    retry_count=attempt - 1,
                )
            self._sleep(self._backoff(attempt, response.headers.get("Retry-After")))

    def run_session(
        self,
        *,
        prompt: str,
        request_id: str,
        action_instructions: str | None = None,
        event_sink: Callable[[AssistantEvent], None] | None = None,
    ) -> AssistantSessionResult:
        """Run one fresh session and return only an unambiguous completion."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise AssistantV3Error("configuration", "Assistant prompt must be a non-empty string")
        if action_instructions is not None and (
            not isinstance(action_instructions, str) or not action_instructions.strip()
        ):
            raise AssistantV3Error("configuration", "Assistant action instructions must be a non-empty string")
        _validate_request_id(request_id)
        started_at = self._clock()
        events: list[AssistantEvent] = []
        meaningful_seen = False
        completion: dict[str, Any] | None = None
        usage: dict[str, Any] | None = None
        session_id: str | None = None
        capability_violation = False
        assistant_error_seen = False

        for attempt in range(1, self.retry_policy.max_attempts + 1):
            try:
                request_body: dict[str, Any] = {
                    "prompt": prompt,
                    "session_id": None,
                    "model": self.configuration.model,
                    "reasoning": self.configuration.reasoning,
                }
                if action_instructions is not None:
                    request_body["action_instructions"] = action_instructions
                with self._client.stream(
                    "POST",
                    self._session_url,
                    headers=self._headers(request_id, accept="text/event-stream"),
                    json=request_body,
                    timeout=self.retry_policy.request_timeout_seconds,
                ) as response:
                    if not 200 <= response.status_code < 300:
                        kind = _status_failure_kind(response.status_code)
                        if kind is not None:
                            raise AssistantV3Error(
                                kind,
                                f"Assistant session request failed with {kind} status {response.status_code}",
                                status_code=response.status_code,
                                retry_count=attempt - 1,
                                events=tuple(events),
                            )
                        if attempt == self.retry_policy.max_attempts:
                            raise AssistantV3Error(
                                "transient_exhausted",
                                f"Assistant session exhausted transient status {response.status_code}",
                                status_code=response.status_code,
                                retry_count=attempt - 1,
                                events=tuple(events),
                            )
                        self._sleep(self._backoff(attempt, response.headers.get("Retry-After")))
                        continue

                    for event_name, event_id, raw_data in _iter_sse(response.iter_bytes()):
                        try:
                            parsed = json.loads(raw_data)
                        except (json.JSONDecodeError, UnicodeError):
                            raise self._stream_error(
                                "incomplete_stream",
                                "Assistant stream contained malformed event data",
                                attempt,
                                events,
                            ) from None
                        if not isinstance(parsed, dict):
                            raise self._stream_error(
                                "incomplete_stream",
                                "Assistant stream event data must be a JSON object",
                                attempt,
                                events,
                            )
                        safe_data, data_redacted = _redact_value(parsed, self._secrets)
                        safe_event_id, id_redacted = _redact_value(event_id, self._secrets)
                        native_event = AssistantEvent(
                            sequence=len(events) + 1,
                            offset_ms=round((self._clock() - started_at) * 1000, 3),
                            event=event_name,
                            event_id=cast(str | None, safe_event_id),
                            data=cast(dict[str, Any], safe_data),
                            redacted=data_redacted or id_redacted,
                        )
                        events.append(native_event)
                        if event_sink is not None:
                            event_sink(native_event)

                        if event_name != "ping":
                            meaningful_seen = True
                        if _is_direct_kubernetes_event(event_name, native_event.data):
                            capability_violation = True
                        if event_name == "session.created":
                            candidate_session_id = native_event.data.get("session_id")
                            if isinstance(candidate_session_id, str) and candidate_session_id:
                                session_id = candidate_session_id
                        elif event_name == "assistant.usage":
                            candidate_usage = native_event.data.get("usage")
                            usage = candidate_usage if isinstance(candidate_usage, dict) else None
                        elif event_name == "assistant.error":
                            assistant_error_seen = True
                        elif event_name == "message.complete":
                            if completion is not None:
                                raise self._stream_error(
                                    "ambiguous_completion",
                                    "Assistant stream contained multiple completion events",
                                    attempt,
                                    events,
                                )
                            completion = native_event.data
                            candidate_session_id = completion.get("session_id")
                            if session_id is None and isinstance(candidate_session_id, str) and candidate_session_id:
                                session_id = candidate_session_id
            except AssistantV3Error:
                raise
            except UnicodeError:
                raise self._stream_error(
                    "incomplete_stream",
                    "Assistant stream contained invalid text encoding",
                    attempt,
                    events,
                ) from None
            except httpx.TransportError:
                if assistant_error_seen:
                    raise self._stream_error(
                        "assistant_error",
                        "Assistant stream reported a terminal error",
                        attempt,
                        events,
                    ) from None
                if meaningful_seen:
                    raise self._stream_error(
                        "incomplete_stream",
                        "Assistant stream disconnected after execution began",
                        attempt,
                        events,
                    ) from None
                if attempt == self.retry_policy.max_attempts:
                    raise self._stream_error(
                        "transient_exhausted",
                        "Assistant session exhausted transient pre-stream retries",
                        attempt,
                        events,
                    ) from None
                self._sleep(self._backoff(attempt, None))
                continue

            if completion is not None:
                if capability_violation:
                    raise self._stream_error(
                        "capability_policy_violation",
                        "Assistant used a forbidden direct Kubernetes capability",
                        attempt,
                        events,
                    )
                if assistant_error_seen:
                    raise self._stream_error(
                        "assistant_error",
                        "Assistant stream reported a terminal error",
                        attempt,
                        events,
                    )
                final_text = _completion_text(completion, attempt=attempt, events=events)
                return AssistantSessionResult(
                    events=tuple(events),
                    final_text=final_text,
                    session_id=session_id,
                    usage=usage,
                    retry_count=attempt - 1,
                )
            if assistant_error_seen:
                raise self._stream_error(
                    "assistant_error",
                    "Assistant stream reported a terminal error",
                    attempt,
                    events,
                )
            if meaningful_seen:
                raise self._stream_error(
                    "incomplete_stream",
                    "Assistant stream ended before a completion event",
                    attempt,
                    events,
                )
            if attempt == self.retry_policy.max_attempts:
                raise self._stream_error(
                    "transient_exhausted",
                    "Assistant session exhausted transient pre-stream retries",
                    attempt,
                    events,
                )
            self._sleep(self._backoff(attempt, None))

        raise AssertionError(
            "bounded Assistant attempts must return or raise"
        )  # pragma: no cover - defensive invariant

    def close(self) -> None:
        if self._owns_client and not self._client.is_closed:
            self._client.close()

    @property
    def _session_url(self) -> str:
        return f"{self.configuration.base_url}{_SESSION_PATH}"

    @property
    def _secrets(self) -> tuple[str, ...]:
        return tuple(sorted((self.configuration.auth_token, self.configuration.sf_token), key=len, reverse=True))

    def _headers(self, request_id: str, *, accept: str) -> dict[str, str]:
        return {
            "Accept": accept,
            "Authorization": f"Bearer {self.configuration.auth_token}",
            "X-SF-TOKEN": self.configuration.sf_token,
            "X-Request-ID": request_id,
        }

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        parsed_retry_after = _retry_after_seconds(retry_after)
        if parsed_retry_after is not None:
            return min(parsed_retry_after, self.retry_policy.retry_after_cap_seconds)
        exponential = self.retry_policy.initial_backoff_seconds * (2 ** (attempt - 1))
        return min(exponential, self.retry_policy.max_backoff_seconds)

    @staticmethod
    def _stream_error(
        kind: FailureKind,
        message: str,
        attempt: int,
        events: list[AssistantEvent],
    ) -> AssistantV3Error:
        return AssistantV3Error(kind, message, retry_count=attempt - 1, events=tuple(events))


def _validated_base_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AssistantV3Error("configuration", "Assistant configuration requires a valid URL")
    parsed = urlsplit(value.strip())
    try:
        _port = parsed.port
    except ValueError:
        raise AssistantV3Error("configuration", "Assistant configuration requires a valid URL port") from None
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise AssistantV3Error("configuration", "Assistant configuration requires a credential-free base URL")
    if parsed.path not in {"", "/"} or not parsed.hostname:
        raise AssistantV3Error("configuration", "Assistant configuration requires a base URL without a path")
    if parsed.scheme == "https":
        return value.strip().rstrip("/")
    if parsed.scheme == "http" and _is_loopback(parsed.hostname):
        return value.strip().rstrip("/")
    raise AssistantV3Error("configuration", "Assistant configuration requires HTTPS except for loopback development")


def _is_loopback(hostname: str) -> bool:
    if hostname.casefold() in {"localhost", "host.docker.internal"}:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_request_id(request_id: str) -> None:
    if not isinstance(request_id, str) or not request_id.strip() or "\n" in request_id or "\r" in request_id:
        raise AssistantV3Error("configuration", "Assistant request identity is invalid")


def _status_failure_kind(status_code: int) -> FailureKind | None:
    if status_code == 401:
        return "authentication"
    if status_code == 403:
        return "permission"
    if status_code not in _TRANSIENT_STATUSES:
        return "configuration"
    return None


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _iter_sse(chunks: Iterator[bytes]) -> Iterator[tuple[str, str | None, str]]:
    decoder = codecs.getincrementaldecoder("utf-8")()
    text_buffer = ""
    event_name = ""
    event_id: str | None = None
    data_lines: list[str] = []

    def process_line(line: str) -> tuple[str, str | None, str] | None:
        nonlocal event_name, event_id, data_lines
        if not line:
            if not data_lines:
                event_name = ""
                event_id = None
                return None
            completed = (event_name or "message", event_id, "\n".join(data_lines))
            event_name = ""
            event_id = None
            data_lines = []
            return completed
        if line.startswith(":"):
            return None
        field, separator, raw_value = line.partition(":")
        value = raw_value[1:] if separator and raw_value.startswith(" ") else raw_value
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
        elif field == "id" and "\0" not in value:
            event_id = value
        return None

    for chunk in chunks:
        text_buffer += decoder.decode(chunk)
        while "\n" in text_buffer:
            line, text_buffer = text_buffer.split("\n", 1)
            completed = process_line(line.removesuffix("\r"))
            if completed is not None:
                yield completed
    text_buffer += decoder.decode(b"", final=True)
    if text_buffer:
        process_line(text_buffer.removesuffix("\r"))
    completed = process_line("")
    if completed is not None:
        yield completed


def _redact_value(value: Any, secrets: tuple[str, ...]) -> tuple[Any, bool]:
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            redacted = redacted.replace(secret, _REDACTION_MARKER)
        return redacted, redacted != value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            safe_item, item_changed = _redact_value(item, secrets)
            result[str(key)] = safe_item
            changed = changed or item_changed
        return result, changed
    if isinstance(value, list):
        result_list: list[Any] = []
        changed = False
        for item in value:
            safe_item, item_changed = _redact_value(item, secrets)
            result_list.append(safe_item)
            changed = changed or item_changed
        return result_list, changed
    return value, False


def _completion_text(
    completion: dict[str, Any],
    *,
    attempt: int,
    events: list[AssistantEvent],
) -> str:
    value = completion.get("final_text") if "final_text" in completion else completion.get("text")
    if not isinstance(value, str) or not value.strip():
        raise AssistantV3Error(
            "ambiguous_completion",
            "Assistant completion did not contain non-blank final text",
            retry_count=attempt - 1,
            events=tuple(events),
        )
    return value


def _is_direct_kubernetes_event(event_name: str, data: Mapping[str, Any]) -> bool:
    candidates: list[str] = []
    if event_name in {"tool.use", "tool.progress", "tool.result", "assistant.subagent.action"}:
        for key in ("name", "tool_name", "server", "server_id", "connector", "connector_name"):
            value = data.get(key)
            if isinstance(value, str):
                candidates.append(value)
    if event_name in {"assistant.subagent.started", "assistant.subagent.complete"}:
        value = data.get("name")
        if isinstance(value, str):
            candidates.append(value)
    for candidate in candidates:
        normalized = candidate.strip().casefold().replace("-", "_")
        if normalized.startswith("o11y_"):
            continue
        if normalized == "k8s" or normalized.startswith(("k8s_", "kubectl", "kubernetes")):
            return True
    return False


__all__ = [
    "AssistantEvent",
    "AssistantSessionResult",
    "AssistantV3Client",
    "AssistantV3Config",
    "AssistantV3Error",
    "FailureKind",
    "RetryPolicy",
]
