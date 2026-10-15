import json
from datetime import UTC, datetime, timedelta

import httpx
from test_assistant_v3_client import client_for, complete_stream, streaming_response

from clients.assistant_v3.driver import ConductorClient, DriverRunConfig, execute_assistant_attempt


def test_adapter_transports_configured_logs_destination_as_non_oracle_context(tmp_path):
    observed = []

    def assistant_handler(request):
        observed.append(json.loads(request.content))
        return streaming_response(complete_stream())

    def conductor_handler(request):
        if request.url.path == "/status":
            return httpx.Response(200, json={"stage": "diagnosis"})
        if request.url.path == "/get_app":
            return httpx.Response(
                200, json={"app_name": "Shop", "descriptions": "Online shopping", "namespaces": ["shop"]}
            )
        return httpx.Response(200, json={"status": "success"})

    start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    config = DriverRunConfig(
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
        logs_connection_id="selected-connection",
        hec_index="incident-events",
    )
    execute_assistant_attempt(
        config,
        conductor=ConductorClient(
            "https://conductor.test", http_client=httpx.Client(transport=httpx.MockTransport(conductor_handler))
        ),
        assistant=client_for(assistant_handler),
    )
    assert observed[0]["incident_scope"]["logs_source"] == {
        "connection_id": "selected-connection",
        "index": "incident-events",
    }
    assert "selected-connection" not in observed[0]["prompt"]
    assert "incident-events" not in observed[0]["prompt"]
    assert "selected-connection" not in observed[0]["action_instructions"]
