from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from clients.assistant_v3.client import (
    AssistantEvent,
    AssistantV3Client,
    AssistantV3Config,
    AssistantV3Error,
    RetryPolicy,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "assistant_v3"
AUTH_TOKEN = "assistant-auth-secret-value"
SF_TOKEN = "splunk-access-secret-value"


class ChunkStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes | Exception]) -> None:
        self.chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def config(**overrides: object) -> AssistantV3Config:
    values: dict[str, object] = {
        "base_url": "https://assistant.example.test",
        "auth_token": AUTH_TOKEN,
        "sf_token": SF_TOKEN,
        "model": "gpt-5.6-luna",
        "reasoning": "medium",
    }
    values.update(overrides)
    return AssistantV3Config(**values)  # type: ignore[arg-type]


def response(status: int, body: bytes = b"", *, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers, stream=ChunkStream([body]))


def streaming_response(*chunks: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=ChunkStream(list(chunks)))


def client_for(
    handler: httpx.MockTransport | object,
    *,
    policy: RetryPolicy | None = None,
    sleep: Mock | None = None,
    clock: Mock | None = None,
) -> AssistantV3Client:
    transport = handler if isinstance(handler, httpx.MockTransport) else httpx.MockTransport(handler)  # type: ignore[arg-type]
    return AssistantV3Client(
        config(),
        http_client=httpx.Client(transport=transport),
        retry_policy=policy or RetryPolicy(max_attempts=3, initial_backoff_seconds=0.25, max_backoff_seconds=2),
        sleep=sleep or Mock(),
        clock=clock,
    )


def complete_stream(text: str = "diagnosis") -> bytes:
    return (
        'event: session.created\ndata: {"session_id":"asst_v3_test"}\n\n'
        f'event: message.complete\ndata: {{"final_text":{json.dumps(text)},"session_id":"asst_v3_test"}}\n\n'
    ).encode()


def test_config_from_environment_and_url_validation() -> None:
    parsed = AssistantV3Config.from_env(
        {
            "ASSISTANT_V3_URL": "https://assistant.example.test/",
            "ASSISTANT_V3_AUTH_TOKEN": AUTH_TOKEN,
            "SF_TOKEN": SF_TOKEN,
            "AGENT_MODEL_ID": "gpt-5.6-luna",
            "AGENT_REASONING_EFFORT": "high",
        }
    )

    assert parsed.base_url == "https://assistant.example.test"
    assert parsed.reasoning == "high"
    for bad_url in (
        "http://assistant.example.test",
        "ftp://assistant.example.test",
        "https://user:pass@assistant.example.test",
        "https://assistant.example.test/path?token=secret",
    ):
        with pytest.raises(AssistantV3Error, match="configuration"):
            config(base_url=bad_url)
    assert config(base_url="http://127.0.0.1:8903").base_url == "http://127.0.0.1:8903"
    assert config(base_url="http://localhost:8903").base_url == "http://localhost:8903"
    assert (
        config(base_url="http://host.docker.internal:8903").base_url
        == "http://host.docker.internal:8903"
    )
    with pytest.raises(AssistantV3Error, match="HTTPS"):
        config(base_url="http://host.docker.internal.example.test:8903")


@pytest.mark.parametrize(
    ("overrides", "missing_name"),
    [
        ({"ASSISTANT_V3_AUTH_TOKEN": ""}, "ASSISTANT_V3_AUTH_TOKEN"),
        ({"SF_TOKEN": ""}, "SF_TOKEN"),
        ({"AGENT_MODEL_ID": ""}, "AGENT_MODEL_ID"),
        ({"AGENT_REASONING_EFFORT": ""}, "AGENT_REASONING_EFFORT"),
        ({"ASSISTANT_V3_URL": ""}, "ASSISTANT_V3_URL"),
    ],
)
def test_missing_configuration_is_actionable_and_secret_free(overrides: dict[str, str], missing_name: str) -> None:
    environment = {
        "ASSISTANT_V3_URL": "https://assistant.example.test",
        "ASSISTANT_V3_AUTH_TOKEN": AUTH_TOKEN,
        "SF_TOKEN": SF_TOKEN,
        "AGENT_MODEL_ID": "gpt-5.6-luna",
        "AGENT_REASONING_EFFORT": "medium",
    }
    environment.update(overrides)

    with pytest.raises(AssistantV3Error) as caught:
        AssistantV3Config.from_env(environment)

    assert caught.value.kind == "configuration"
    assert missing_name in str(caught.value)
    assert AUTH_TOKEN not in str(caught.value)
    assert SF_TOKEN not in str(caught.value)


def test_invalid_reasoning_and_blank_constructor_fields_fail_safely() -> None:
    with pytest.raises(AssistantV3Error, match="reasoning"):
        config(reasoning="maximum")
    with pytest.raises(AssistantV3Error, match="model"):
        config(model="  ")
    with pytest.raises(AssistantV3Error, match="credentials"):
        config(auth_token="")
    with pytest.raises(AssistantV3Error, match="credentials"):
        config(sf_token="")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"request_timeout_seconds": 0},
        {"initial_backoff_seconds": 2, "max_backoff_seconds": 1},
    ],
)
def test_retry_policy_rejects_invalid_bounds(kwargs: dict[str, float | int]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)  # type: ignore[arg-type]


def test_config_can_load_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSISTANT_V3_URL", "https://assistant.example.test")
    monkeypatch.setenv("ASSISTANT_V3_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("SF_TOKEN", SF_TOKEN)
    monkeypatch.setenv("AGENT_MODEL_ID", "gpt-5.6-luna")
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "low")

    assert AssistantV3Config.from_env().reasoning == "low"


@pytest.mark.parametrize(
    "bad_url",
    [None, "", "https:///missing-host", "https://assistant.example.test/path", "https://assistant.example.test:bad"],
)
def test_invalid_base_url_shapes_fail_safely(bad_url: object) -> None:
    with pytest.raises(AssistantV3Error, match="URL"):
        config(base_url=bad_url)


def test_preflight_uses_authenticated_read_only_endpoint() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"threads": []})

    assistant = client_for(handler)
    assistant.preflight(request_id="anon_preflight")

    request = observed[0]
    assert request.method == "GET"
    assert request.url == "https://assistant.example.test/v2/assistant/sessions?limit=1"
    assert request.headers["Authorization"] == f"Bearer {AUTH_TOKEN}"
    assert request.headers["X-SF-TOKEN"] == SF_TOKEN
    assert request.headers["X-Request-ID"] == "anon_preflight"
    assert request.content == b""


@pytest.mark.parametrize(
    ("status", "kind"),
    [(400, "configuration"), (401, "authentication"), (403, "permission")],
)
def test_preflight_terminal_statuses_fail_without_retry(status: int, kind: str) -> None:
    requests = 0
    sleep = Mock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return response(status, b'secret response body "' + AUTH_TOKEN.encode() + b'"')

    assistant = client_for(handler, sleep=sleep)
    with pytest.raises(AssistantV3Error) as caught:
        assistant.preflight(request_id="anon_preflight")

    assert caught.value.kind == kind
    assert caught.value.status_code == status
    assert requests == 1
    sleep.assert_not_called()
    assert AUTH_TOKEN not in str(caught.value)


def test_preflight_retries_transient_failures_with_capped_retry_after() -> None:
    attempts = 0
    sleep = Mock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return response(429, headers={"Retry-After": "999"})
        return httpx.Response(200, json={"threads": []})

    assistant = client_for(
        handler,
        policy=RetryPolicy(max_attempts=2, retry_after_cap_seconds=3),
        sleep=sleep,
    )
    assistant.preflight(request_id="anon_preflight")

    assert attempts == 2
    sleep.assert_called_once_with(3)


def test_preflight_exhaustion_and_timeout_are_classified() -> None:
    sleep = Mock()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("contains " + AUTH_TOKEN, request=request)

    assistant = client_for(handler, policy=RetryPolicy(max_attempts=2), sleep=sleep)
    with pytest.raises(AssistantV3Error) as caught:
        assistant.preflight(request_id="anon_preflight")

    assert caught.value.kind == "transient_exhausted"
    assert caught.value.retry_count == 1
    assert AUTH_TOKEN not in str(caught.value)
    assert sleep.call_count == 1


def test_preflight_transient_status_exhaustion_is_classified() -> None:
    assistant = client_for(
        lambda request: response(503),
        policy=RetryPolicy(max_attempts=1),
    )

    with pytest.raises(AssistantV3Error) as caught:
        assistant.preflight(request_id="anon_preflight")

    assert caught.value.kind == "transient_exhausted"
    assert caught.value.status_code == 503


def test_stream_parses_split_chunks_multiline_data_and_all_native_events() -> None:
    raw = (FIXTURES / "all_events.sse").read_bytes()
    chunks = [raw[:7], raw[7:91], raw[91:347], raw[347:701], raw[701:]]
    persisted: list[AssistantEvent] = []
    ticks = iter(float(value) for value in range(30))
    assistant = client_for(lambda request: streaming_response(*chunks), clock=Mock(side_effect=lambda: next(ticks)))

    result = assistant.run_session(prompt="Investigate", request_id="anon_run", event_sink=persisted.append)

    expected = [
        "session.created",
        "ping",
        "agent.thinking",
        "agent.model_start",
        "message.thinking",
        "assistant.progress",
        "tool.use",
        "tool.progress",
        "tool.result",
        "skill.loaded",
        "agent.skills",
        "assistant.subagent.started",
        "assistant.subagent.delta",
        "assistant.subagent.action",
        "assistant.subagent.complete",
        "session.title.updated",
        "message.delta",
        "assistant.suggestions",
        "assistant.usage",
        "message.complete",
    ]
    assert [event.event for event in result.events] == expected
    assert result.events == tuple(persisted)
    assert [event.sequence for event in result.events] == list(range(1, 21))
    assert result.events[0].event_id == "evt-1"
    assert result.events[6].data["input"] == {"environment": "prod"}
    assert result.final_text == "The root cause is a saturated downstream dependency."
    assert result.session_id == "asst_v3_fixture"
    assert result.usage == {"input_tokens": 100, "output_tokens": 25, "total_tokens": 125}
    assert result.retry_count == 0


def test_request_shape_is_exact_and_does_not_send_surface() -> None:
    observed: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return streaming_response(complete_stream())

    assistant = client_for(handler)
    assistant.run_session(prompt="Exact prompt", request_id="anon_run")

    request = observed[0]
    assert request.method == "POST"
    assert request.url == "https://assistant.example.test/v2/assistant/sessions"
    assert request.headers["Accept"] == "text/event-stream"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["Authorization"] == f"Bearer {AUTH_TOKEN}"
    assert request.headers["X-SF-TOKEN"] == SF_TOKEN
    assert request.headers["X-Request-ID"] == "anon_run"
    assert json.loads(request.content) == {
        "prompt": "Exact prompt",
        "session_id": None,
        "model": "gpt-5.6-luna",
        "reasoning": "medium",
    }


def test_text_is_used_only_when_final_text_is_absent_and_usage_can_be_missing() -> None:
    body = (
        b'event: session.created\ndata: {"session_id":"asst_v3_test"}\n\n'
        b'event: message.complete\ndata: {"text":"legacy answer","session_id":"asst_v3_test"}\n\n'
    )
    result = client_for(lambda request: streaming_response(body)).run_session(prompt="p", request_id="r")

    assert result.final_text == "legacy answer"
    assert result.usage is None


def test_completion_can_supply_session_id_and_invalid_optional_payloads_remain_native() -> None:
    body = (
        b'event: session.created\ndata: {"session_id":null}\n\n'
        b'event: assistant.usage\ndata: {"usage":"unknown"}\n\n'
        b'event: message.complete\ndata: {"final_text":"answer","session_id":"asst_v3_completion"}\n\n'
    )

    result = client_for(lambda request: streaming_response(body)).run_session(prompt="p", request_id="r")

    assert result.session_id == "asst_v3_completion"
    assert result.usage is None


@pytest.mark.parametrize(
    ("status", "kind"),
    [(400, "configuration"), (401, "authentication"), (403, "permission")],
)
def test_session_terminal_http_statuses_fail_fast(status: int, kind: str) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return response(status, b"ignored response body")

    with pytest.raises(AssistantV3Error) as caught:
        client_for(handler).run_session(prompt="p", request_id="r")

    assert caught.value.kind == kind
    assert caught.value.status_code == status
    assert attempts == 1


def test_session_transient_http_status_exhaustion_is_bounded() -> None:
    with pytest.raises(AssistantV3Error) as caught:
        client_for(lambda request: response(503), policy=RetryPolicy(max_attempts=1)).run_session(
            prompt="p", request_id="r"
        )

    assert caught.value.kind == "transient_exhausted"
    assert caught.value.status_code == 503


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_session_retries_bounded_transient_http_before_events(status: int) -> None:
    attempts = 0
    sleep = Mock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return response(status, headers={"Retry-After": "1"})
        return streaming_response(complete_stream())

    result = client_for(handler, sleep=sleep).run_session(prompt="p", request_id="r")

    assert result.retry_count == 2
    assert attempts == 3
    assert sleep.call_count == 2


def test_ping_then_disconnect_can_retry_and_preserves_received_ping() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                stream=ChunkStream(
                    [b'event: ping\ndata: {"timestamp":"now"}\n\n', httpx.ReadError("closed", request=request)]
                ),
            )
        return streaming_response(complete_stream())

    result = client_for(handler).run_session(prompt="p", request_id="r")

    assert attempts == 2
    assert result.retry_count == 1
    assert [event.event for event in result.events] == ["ping", "session.created", "message.complete"]


def test_transport_failure_before_events_exhausts_without_leaking_details() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret " + AUTH_TOKEN, request=request)

    with pytest.raises(AssistantV3Error) as caught:
        client_for(handler, policy=RetryPolicy(max_attempts=1)).run_session(prompt="p", request_id="r")

    assert caught.value.kind == "transient_exhausted"
    assert AUTH_TOKEN not in str(caught.value)


def test_empty_stream_exhausts_as_a_pre_stream_failure() -> None:
    with pytest.raises(AssistantV3Error) as caught:
        client_for(lambda request: streaming_response(), policy=RetryPolicy(max_attempts=1)).run_session(
            prompt="p", request_id="r"
        )

    assert caught.value.kind == "transient_exhausted"


def test_empty_stream_can_retry_before_any_meaningful_event() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return streaming_response() if attempts == 1 else streaming_response(complete_stream())

    result = client_for(handler).run_session(prompt="p", request_id="r")

    assert result.retry_count == 1


def test_disconnect_after_meaningful_event_is_terminal_without_retry() -> None:
    attempts = 0
    persisted: list[AssistantEvent] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=ChunkStream(
                [
                    b'event: session.created\ndata: {"session_id":"asst_v3_test"}\n\n',
                    httpx.ReadError("closed " + SF_TOKEN, request=request),
                ]
            ),
        )

    with pytest.raises(AssistantV3Error) as caught:
        client_for(handler).run_session(prompt="p", request_id="r", event_sink=persisted.append)

    assert caught.value.kind == "incomplete_stream"
    assert caught.value.events == tuple(persisted)
    assert attempts == 1
    assert SF_TOKEN not in str(caught.value)


def test_assistant_error_is_persisted_before_terminal_failure() -> None:
    persisted: list[AssistantEvent] = []
    body = (
        b'event: assistant.error\ndata: {"message":"provider unavailable","code":"UPSTREAM"}\n\n'
        b'event: assistant.usage\ndata: {"usage":{"total_tokens":10}}\n\n'
        b'event: message.complete\ndata: {"final_text":"partial answer"}\n\n'
    )

    with pytest.raises(AssistantV3Error) as caught:
        client_for(lambda request: streaming_response(body)).run_session(
            prompt="p", request_id="r", event_sink=persisted.append
        )

    assert caught.value.kind == "assistant_error"
    assert caught.value.events == tuple(persisted)
    assert [event.event for event in persisted] == ["assistant.error", "assistant.usage", "message.complete"]


def test_assistant_error_remains_authoritative_if_stream_disconnects_during_cleanup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            stream=ChunkStream(
                [
                    b'event: assistant.error\ndata: {"message":"provider unavailable"}\n\n',
                    httpx.ReadError("cleanup disconnected", request=request),
                ]
            ),
        )

    with pytest.raises(AssistantV3Error) as caught:
        client_for(handler).run_session(prompt="p", request_id="r")

    assert caught.value.kind == "assistant_error"
    assert [event.event for event in caught.value.events] == ["assistant.error"]


def test_assistant_error_remains_authoritative_if_cleanup_ends_without_completion() -> None:
    body = b'event: assistant.error\ndata: {"message":"provider unavailable"}\n\n'

    with pytest.raises(AssistantV3Error) as caught:
        client_for(lambda request: streaming_response(body)).run_session(prompt="p", request_id="r")

    assert caught.value.kind == "assistant_error"


def test_malformed_utf8_is_an_incomplete_stream_not_a_retryable_disconnect() -> None:
    with pytest.raises(AssistantV3Error) as caught:
        client_for(
            lambda request: streaming_response(b"event: message.delta\ndata: \xff\n\n"),
            policy=RetryPolicy(max_attempts=2),
        ).run_session(prompt="p", request_id="r")

    assert caught.value.kind == "incomplete_stream"
    assert caught.value.retry_count == 0


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (b"event: message.complete\ndata: {not-json}\n\n", "incomplete_stream"),
        (b"event: message.complete\ndata: []\n\n", "incomplete_stream"),
        (b'event: message.complete\ndata: {"final_text":"   "}\n\n', "ambiguous_completion"),
        (b'event: message.complete\ndata: {"final_text":null}\n\n', "ambiguous_completion"),
        (b'event: message.delta\ndata: {"text":"partial"}\n\n', "incomplete_stream"),
        (
            b'event: message.complete\ndata: {"final_text":"one"}\n\n'
            b'event: message.complete\ndata: {"final_text":"two"}\n\n',
            "ambiguous_completion",
        ),
    ],
)
def test_malformed_blank_missing_and_conflicting_completion_fail(body: bytes, kind: str) -> None:
    with pytest.raises(AssistantV3Error) as caught:
        client_for(lambda request: streaming_response(body)).run_session(prompt="p", request_id="r")

    assert caught.value.kind == kind


def test_known_credentials_are_redacted_even_when_split_across_chunks() -> None:
    body = (
        'event: tool.result\ndata: {"tool_use_id":"call-1","content":"auth '
        + AUTH_TOKEN
        + " sf "
        + SF_TOKEN
        + '","is_error":false}\n\n'
        'event: message.complete\ndata: {"final_text":"safe diagnosis"}\n\n'
    ).encode()
    auth_boundary = body.index(AUTH_TOKEN.encode()) + 5
    sf_boundary = body.index(SF_TOKEN.encode()) + 7
    chunks = (body[:auth_boundary], body[auth_boundary:sf_boundary], body[sf_boundary:])

    result = client_for(lambda request: streaming_response(*chunks)).run_session(prompt="p", request_id="r")

    serialized = json.dumps([event.data for event in result.events])
    assert AUTH_TOKEN not in serialized
    assert SF_TOKEN not in serialized
    assert serialized.count("[REDACTED]") == 2
    assert result.events[0].redacted is True


def test_known_credentials_are_redacted_from_sse_event_ids() -> None:
    body = f'event: message.delta\nid: event-{AUTH_TOKEN}\ndata: {{"text":"safe"}}\n\n'.encode() + complete_stream()
    boundary = body.index(AUTH_TOKEN.encode()) + 4

    result = client_for(lambda request: streaming_response(body[:boundary], body[boundary:])).run_session(
        prompt="p", request_id="r"
    )

    assert result.events[0].event_id == "event-[REDACTED]"
    assert result.events[0].redacted is True


@pytest.mark.parametrize(
    "event",
    [
        'event: tool.use\ndata: {"id":"c1","name":"k8s_get_pods","input":{}}\n\n',
        'event: assistant.subagent.started\ndata: {"run_id":"s1","name":"kubernetes"}\n\n',
        'event: assistant.subagent.action\ndata: {"action_id":"a1","tool_name":"kubectl","status":"running"}\n\n',
        'event: assistant.subagent.started\ndata: {"run_id":"s1","name":"k8s"}\n\n',
    ],
)
def test_direct_kubernetes_events_are_preserved_and_mark_policy_violation(event: str) -> None:
    body = (event + complete_stream().decode()).encode()

    with pytest.raises(AssistantV3Error) as caught:
        client_for(lambda request: streaming_response(body)).run_session(prompt="p", request_id="r")

    assert caught.value.kind == "capability_policy_violation"
    assert len(caught.value.events) == 3
    assert caught.value.events[0].event in {"tool.use", "assistant.subagent.started", "assistant.subagent.action"}


def test_splunk_backed_kubernetes_history_tool_is_not_a_direct_access_violation() -> None:
    body = b'event: tool.use\ndata: {"id":"c1","name":"o11y_k8s_deployment_history","input":{}}\n\n' + complete_stream()
    result = client_for(lambda request: streaming_response(body)).run_session(prompt="p", request_id="r")

    assert result.final_text == "diagnosis"


def test_sse_comments_empty_frames_default_event_and_unterminated_tail_are_handled() -> None:
    body = (
        b": server comment\n\n\n"
        b'id: invalid\x00id\ndata: {"ignored":true}\n\n'
        b'event: message.complete\ndata: {"final_text":"answer"}\n\n'
        b"event: eof-event\ndata: {}"
    )
    result = client_for(lambda request: streaming_response(body)).run_session(prompt="p", request_id="r")

    assert [event.event for event in result.events] == ["message", "message.complete", "eof-event"]
    assert result.events[0].event_id is None


def test_subagent_complete_kubernetes_name_is_a_policy_violation() -> None:
    body = (
        b'event: assistant.subagent.complete\ndata: {"run_id":"s1","name":"kubernetes-investigator"}\n\n'
        + complete_stream()
    )
    with pytest.raises(AssistantV3Error) as caught:
        client_for(lambda request: streaming_response(body)).run_session(prompt="p", request_id="r")

    assert caught.value.kind == "capability_policy_violation"


def test_non_string_subagent_name_is_preserved_without_policy_inference() -> None:
    body = b'event: assistant.subagent.complete\ndata: {"run_id":"s1","name":null}\n\n' + complete_stream()
    result = client_for(lambda request: streaming_response(body)).run_session(prompt="p", request_id="r")

    assert result.events[0].data["name"] is None


@pytest.mark.parametrize("prompt", ["", "   ", None])
def test_blank_prompt_is_rejected_before_network(prompt: object) -> None:
    with pytest.raises(AssistantV3Error, match="prompt"):
        client_for(lambda request: pytest.fail("network must not be called")).run_session(
            prompt=prompt,  # pyright: ignore[reportArgumentType]
            request_id="r",
        )


@pytest.mark.parametrize("request_id", ["", "   ", "bad\nvalue", None])
def test_invalid_request_identity_is_rejected(request_id: object) -> None:
    assistant = client_for(lambda request: pytest.fail("network must not be called"))
    with pytest.raises(AssistantV3Error, match="identity"):
        assistant.preflight(request_id=request_id)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        ("not-a-delay", 0.25),
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),
        ("Wed, 21 Oct 2015 07:28:00", 0.0),
    ],
)
def test_retry_after_invalid_and_http_date_forms_are_bounded(retry_after: str, expected: float) -> None:
    attempts = 0
    sleep = Mock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return response(503, headers={"Retry-After": retry_after})
        return streaming_response(complete_stream())

    client_for(handler, sleep=sleep).run_session(prompt="p", request_id="r")

    sleep.assert_called_once_with(expected)


def test_client_closes_only_owned_http_client() -> None:
    owned = AssistantV3Client(config(), retry_policy=RetryPolicy(max_attempts=1))
    owned.close()
    owned.close()

    external = httpx.Client(transport=httpx.MockTransport(lambda request: streaming_response(complete_stream())))
    assistant = AssistantV3Client(config(), http_client=external)
    assistant.close()
    assert not external.is_closed
    external.close()
