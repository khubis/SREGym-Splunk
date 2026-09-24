import subprocess
from unittest.mock import Mock

import pytest
import yaml

from sregym.observability.base import ExternalOtlpExport
from sregym.observer.otel_collector.otel_collector import OtelCollector

RUN_ID = "anon_0123456789abcdef0123456789abcdef"


def external_export() -> ExternalOtlpExport:
    return ExternalOtlpExport(
        endpoint="splunk-otel-collector-agent.observe.svc.cluster.local:4317",
        run_id=RUN_ID,
        resource_attributes={"deployment.environment": "sregym"},
    )


def documents(manifest: str) -> list[dict]:
    return list(yaml.safe_load_all(manifest))


def collector_config(manifest: str) -> dict:
    config_map = next(document for document in documents(manifest) if document["kind"] == "ConfigMap")
    return yaml.safe_load(config_map["data"]["config.yaml"])


def test_disabled_rendering_returns_the_original_manifest_and_pipelines():
    collector = OtelCollector()
    original = collector.config_file.read_text()

    rendered = collector.render_manifest(None)

    assert rendered == original
    assert collector_config(rendered)["service"]["pipelines"] == collector_config(original)["service"]["pipelines"]


def test_external_rendering_fans_out_traces_without_federating_all_metrics(monkeypatch):
    monkeypatch.setenv("SPLUNK_HEC_TOKEN", "must-not-appear")
    collector = OtelCollector()
    original = collector_config(collector.render_manifest(None))

    manifest = collector.render_manifest(external_export())
    config = collector_config(manifest)

    assert set(config["exporters"]) - set(original["exporters"]) == {"otlp/external"}
    assert config["exporters"]["otlp/external"] == {
        "endpoint": "splunk-otel-collector-agent.observe.svc.cluster.local:4317",
        "tls": {"insecure": True},
    }
    attributes = config["processors"]["resource/external"]["attributes"]
    assert {attribute["key"]: attribute["value"] for attribute in attributes} == {
        "deployment.environment": "sregym",
        "sregym.run.id": RUN_ID,
    }
    assert all(attribute["action"] == "upsert" for attribute in attributes)
    for pipeline_name in ("traces", "traces/otlp"):
        assert config["service"]["pipelines"][pipeline_name]["exporters"] == [
            *original["service"]["pipelines"][pipeline_name]["exporters"],
            "otlp/external",
        ]
        assert config["service"]["pipelines"][pipeline_name]["processors"] == ["resource/external"]

    assert "prometheus/external" not in config["receivers"]
    assert "metrics/external" not in config["service"]["pipelines"]
    assert "must-not-appear" not in manifest


def test_deploy_uses_argument_arrays_and_manifest_stdin(monkeypatch):
    collector = OtelCollector()
    run_cmd = Mock(return_value="")
    monkeypatch.setattr(collector, "run_cmd", run_cmd)
    monkeypatch.setattr(collector, "_wait_for_ready", Mock())

    collector.deploy(external_export())

    assert run_cmd.call_count == 3
    delete_service, apply_service, apply_collector = run_cmd.call_args_list
    assert delete_service.args == (
        ["kubectl", "-n", "observe", "delete", "svc", "jaeger-backend", "--ignore-not-found"],
    )
    assert apply_service.args == (["kubectl", "apply", "-f", "-"],)
    assert apply_service.kwargs["input_text"]
    assert apply_collector.args == (["kubectl", "-n", "observe", "apply", "-f", "-"],)
    assert apply_collector.kwargs["input_text"] == collector.render_manifest(external_export())
    assert all(isinstance(call.args[0], list) for call in run_cmd.call_args_list)


def test_run_cmd_passes_stdin_without_shell_interpolation(monkeypatch):
    completed = subprocess.CompletedProcess([], 0, stdout="applied\n", stderr="")
    run = Mock(return_value=completed)
    monkeypatch.setattr("sregym.observer.otel_collector.otel_collector.subprocess.run", run)
    collector = OtelCollector()

    result = collector.run_cmd(["kubectl", "apply", "-f", "-"], input_text="manifest")

    assert result == "applied"
    run.assert_called_once_with(
        ["kubectl", "apply", "-f", "-"],
        input="manifest",
        capture_output=True,
        text=True,
    )


def test_run_cmd_failure_does_not_include_stdin(monkeypatch):
    secret = "never-include-stdin"
    completed = subprocess.CompletedProcess([], 1, stdout="", stderr="apply failed")
    monkeypatch.setattr("sregym.observer.otel_collector.otel_collector.subprocess.run", Mock(return_value=completed))

    with pytest.raises(RuntimeError) as failure:
        OtelCollector().run_cmd(["kubectl", "apply", "-f", "-"], input_text=secret)

    assert "apply failed" in str(failure.value)
    assert secret not in str(failure.value)


def test_readiness_timeout_tolerates_transient_kubectl_failure(monkeypatch):
    collector = OtelCollector()
    monkeypatch.setattr(collector, "run_cmd", Mock(side_effect=RuntimeError("temporarily unavailable")))
    monkeypatch.setattr("sregym.observer.otel_collector.otel_collector.time.time", Mock(side_effect=[0, 0, 2]))
    monkeypatch.setattr("sregym.observer.otel_collector.otel_collector.time.sleep", Mock())

    with pytest.raises(RuntimeError, match="not ready within 1s"):
        collector._wait_for_ready(timeout=1)
