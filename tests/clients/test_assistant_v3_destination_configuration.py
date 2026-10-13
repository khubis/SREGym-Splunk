import dataclasses
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_assistant_v3_client import client_for, complete_stream, streaming_response

from clients.assistant_v3.driver import ArtifactError, ConductorClient, DriverRunConfig, execute_assistant_attempt


def configuration(tmp_path, index, connection):
    start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    return DriverRunConfig(
        run_id="anon_0123456789abcdef0123456789abcdef",
        attempt=1,
        artifacts_root=tmp_path,
        benchmark_profile="svelte",
        comparable=False,
        judge_model="fixed-judge",
        judge_backend="fixed-backend",
        observability_provider="splunk",
        readiness_report=None,
        attempt_started_at=start,
        telemetry_window_ended_at=start + timedelta(minutes=2),
        logs_connection_id=connection,
        hec_index=index,
    )


@pytest.mark.parametrize("index", [None, ""])
def test_selected_logs_connection_without_index_never_dispatches(tmp_path, index):
    assistant_calls, conductor_calls = [], []

    def assistant_handler(request):
        assistant_calls.append(request)
        return streaming_response(complete_stream())

    def conductor_handler(request):
        conductor_calls.append(request)
        if request.url.path == "/status":
            return httpx.Response(200, json={"stage": "diagnosis"})
        if request.url.path == "/get_app":
            return httpx.Response(
                200, json={"app_name": "Shop", "descriptions": "Online shopping", "namespaces": ["shop"]}
            )
        return httpx.Response(200, json={"status": "success"})

    with pytest.raises(ArtifactError, match="selected Logs connection requires an explicit HEC index"):
        execute_assistant_attempt(
            configuration(tmp_path, index, "selected-connection"),
            conductor=ConductorClient(
                "https://conductor.test", http_client=httpx.Client(transport=httpx.MockTransport(conductor_handler))
            ),
            assistant=client_for(assistant_handler),
        )
    assert assistant_calls == []
    assert conductor_calls == []


def test_complete_destination_and_legacy_omitted_destination_remain_valid(tmp_path):
    valid = configuration(tmp_path, "incident-events", "selected-connection")
    valid.validate()
    dataclasses.replace(valid, hec_index=None, logs_connection_id=None).validate()
    dataclasses.replace(valid, logs_connection_id=None).validate()
