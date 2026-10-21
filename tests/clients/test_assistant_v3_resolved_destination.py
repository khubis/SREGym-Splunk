import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from test_assistant_v3_client import client_for, complete_stream, streaming_response

import main as benchmark_main
from clients.assistant_v3.driver import ArtifactError, ConductorClient, DriverRunConfig, execute_assistant_attempt


def write_native_configuration(monkeypatch, tmp_path, *, selected, index, resolved=None, cluster_name=None):
    # Exercise the default independently of the developer's private pilot env.
    monkeypatch.delenv("ASSISTANT_V3_CAPABILITY_PROFILE", raising=False)
    monkeypatch.setattr(benchmark_main, "get_profile", lambda: "svelte")
    monkeypatch.setenv("JUDGE_MODEL_ID", "fixed-judge")
    start = datetime(2026, 10, 1, 12, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef", attempt=1, active_dir=tmp_path)
    conductor = SimpleNamespace(
        observability_readiness=None,
        incident_started_at=start,
        incident_ended_at=start + timedelta(minutes=2),
        _observability_context=SimpleNamespace(attempt_started_at=start),
    )
    provider = SimpleNamespace(
        name="splunk",
        configuration=SimpleNamespace(hec_index=index, logs_connection_id=selected, cluster_name=cluster_name),
        _connection_id=resolved or selected or "resolved-default",
    )
    benchmark_main._write_assistant_driver_config(run, conductor, "v3", provider, "api")
    return tmp_path / "assistant_v3_driver_config.json"


@pytest.mark.parametrize("cluster_name", [None, "sregym-kind-local"])
@pytest.mark.parametrize("selected,index", [(None, None), (None, "events"), ("selected", "events")])
def test_native_config_preserves_selection_and_resolved_routing_through_actual_dispatch(
    monkeypatch, tmp_path, selected, index, cluster_name
):
    path = write_native_configuration(monkeypatch, tmp_path, selected=selected, index=index, cluster_name=cluster_name)
    config = DriverRunConfig.from_file(path)
    assert config.cluster_name == cluster_name
    assert config.logs_connection_id == selected
    resolved = selected or "resolved-default"
    assert config.resolved_logs_connection_id == resolved
    calls = []

    def assistant_handler(request):
        calls.append(json.loads(request.content))
        return streaming_response(complete_stream())

    def conductor_handler(request):
        if request.url.path == "/status":
            return httpx.Response(200, json={"stage": "diagnosis"})
        if request.url.path == "/get_app":
            return httpx.Response(
                200, json={"app_name": "Shop", "descriptions": "Online shopping", "namespaces": ["shop"]}
            )
        return httpx.Response(200, json={"status": "success"})

    execute_assistant_attempt(
        config,
        conductor=ConductorClient(
            "https://conductor.test", http_client=httpx.Client(transport=httpx.MockTransport(conductor_handler))
        ),
        assistant=client_for(assistant_handler),
    )
    if index is None:
        assert "logs_source" not in calls[0]["incident_scope"]
    else:
        assert calls[0]["incident_scope"]["logs_source"] == {"connection_id": resolved, "index": index}
    metadata = json.loads((tmp_path / "run_metadata.json").read_text())
    assert metadata["logs_connection_id"] == resolved


def test_explicit_destination_without_index_still_fails_native_roundtrip(monkeypatch, tmp_path):
    path = write_native_configuration(monkeypatch, tmp_path, selected="selected", index=None)
    with pytest.raises(ArtifactError, match="selected Logs connection requires an explicit HEC index"):
        DriverRunConfig.from_file(path)


def test_conflicting_resolved_destination_cannot_override_explicit_selection(monkeypatch, tmp_path):
    path = write_native_configuration(monkeypatch, tmp_path, selected="selected", index="events", resolved="other")
    with pytest.raises(ArtifactError, match="resolved Logs connection"):
        DriverRunConfig.from_file(path)


@pytest.mark.parametrize("index", [None, "events"])
def test_legacy_config_treats_old_connection_field_as_resolved_not_explicit(monkeypatch, tmp_path, index):
    path = write_native_configuration(monkeypatch, tmp_path, selected=None, index=index)
    payload = json.loads(path.read_text())
    payload.pop("resolved_logs_connection_id", None)
    payload["logs_connection_id"] = "resolved-default"
    path.write_text(json.dumps(payload))
    config = DriverRunConfig.from_file(path)
    assert config.logs_connection_id is None
    assert config.resolved_logs_connection_id == "resolved-default"
    assert config.hec_index == index
