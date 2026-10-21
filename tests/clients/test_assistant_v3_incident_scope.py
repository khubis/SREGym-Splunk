import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_assistant_v3_client import client_for, complete_stream, streaming_response

from clients.assistant_v3.driver import ConductorClient, DriverRunConfig, execute_assistant_attempt

RUN_ID = "anon_0123456789abcdef0123456789abcdef"
START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
END = START + timedelta(minutes=2)
SCOPE = {
    "environment": RUN_ID,
    "cluster": RUN_ID,
    "namespace": "shop",
    "start_ms": int(START.timestamp() * 1000),
    "end_ms": int(END.timestamp() * 1000),
}


def test_client_transports_incident_scope_without_changing_prompt():
    observed = []

    def handler(request):
        observed.append(json.loads(request.content))
        return streaming_response(complete_stream())

    client = client_for(handler)
    result = client.run_session(prompt="Investigate the application", request_id=RUN_ID[5:], incident_scope=SCOPE)
    assert result.final_text == "diagnosis"
    assert observed[0]["incident_scope"] == SCOPE
    assert observed[0]["prompt"] == "Investigate the application"
    assert RUN_ID not in observed[0]["prompt"]


@pytest.mark.parametrize("cluster_name", [None, "sregym-kind-local"])
def test_splunk_driver_sends_exported_identity_in_separate_scope(tmp_path, cluster_name):
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

    config = DriverRunConfig(
        run_id=RUN_ID,
        attempt=1,
        artifacts_root=tmp_path,
        benchmark_profile="svelte",
        comparable=False,
        judge_model="fixed-judge",
        judge_backend="fixed-backend",
        observability_provider="splunk",
        readiness_report=None,
        attempt_started_at=START,
        telemetry_window_ended_at=END,
        cluster_name=cluster_name,
    )
    execute_assistant_attempt(
        config,
        conductor=ConductorClient(
            "https://conductor.test", http_client=httpx.Client(transport=httpx.MockTransport(conductor_handler))
        ),
        assistant=client_for(assistant_handler),
    )
    assert observed[0]["incident_scope"] == {**SCOPE, "cluster": cluster_name or RUN_ID}
    metadata = json.loads((tmp_path / "run_metadata.json").read_text())
    assert metadata.get("cluster_name") == cluster_name
    assert RUN_ID not in observed[0]["prompt"]
    assert RUN_ID not in observed[0]["action_instructions"]
