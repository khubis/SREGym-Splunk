"""A persistent connector identity must never merge benchmark attempt scopes."""

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
import yaml

from sregym.observability.base import ApplicationScope, AttemptContext, ProviderError
from sregym.observability.splunk import CHART_VERSION, SplunkConfig, SplunkHttpBackend, SplunkObservabilityProvider
from sregym.observer.otel_collector.otel_collector import OtelCollector

CLUSTER = "sregym-kind-local"
RUN_IDS = ("anon_" + "1" * 32, "anon_" + "2" * 32)
START = datetime(2026, 10, 16, tzinfo=UTC)
SCOPE = ApplicationScope("social-network", ("social-network",))


def configuration(**overrides):
    return SplunkConfig.from_env({
        "SF_TOKEN": "query", "SPLUNK_O11Y_INGEST_TOKEN": "ingest",
        "SFX_REALM": "rc0", "SPLUNK_HOST": "example.invalid",
        "SPLUNK_HEC_PORT": "8088", "SPLUNK_HEC_TOKEN": "hec",
        "SPLUNK_CLUSTER_NAME": CLUSTER, **overrides,
    })


@pytest.mark.parametrize("name", ["", " ", "bad name", "x' OR 1=1", 'x"', "x,y", "x/y", "x" * 129])
def test_cluster_identity_rejects_query_syntax(name):
    with pytest.raises(ProviderError, match="SPLUNK_CLUSTER_NAME"):
        configuration(SPLUNK_CLUSTER_NAME=name)


def test_two_attempts_keep_cluster_but_replace_all_exported_run_attributes():
    values = []

    def run(command, stdin):
        if command[0] == "helm":
            values.append(yaml.safe_load(stdin))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    provider = SplunkObservabilityProvider(configuration(), core_api=Mock(), run=run)
    for run_id in RUN_IDS:
        context = AttemptContext(run_id, "full", True, START)
        export = provider.prepare_attempt(context)
        assert export.resource_attributes["k8s.cluster.name"] == CLUSTER
        assert export.resource_attributes["sregym.run.id"] == run_id
        assert export.resource_attributes["deployment.environment"] == run_id
        rendered = list(yaml.safe_load_all(OtelCollector().render_manifest(export)))
        configmap = next(doc for doc in rendered if doc["kind"] == "ConfigMap")
        config = yaml.safe_load(configmap["data"]["config.yaml"])
        attributes = {item["key"]: item["value"] for item in config["processors"]["resource/external"]["attributes"]}
        assert attributes["k8s.cluster.name"] == CLUSTER
        assert attributes["sregym.run.id"] == run_id
        assert attributes["deployment.environment"] == run_id
        for pipeline in ("traces", "traces/otlp"):
            assert "resource/external" in config["service"]["pipelines"][pipeline]["processors"]

    assert [value["clusterName"] for value in values] == [CLUSTER, CLUSTER]
    for value, run_id in zip(values, RUN_IDS, strict=True):
        custom = {item["name"]: item["value"] for item in value["extraAttributes"]["custom"]}
        assert custom["sregym.run.id"] == run_id
        attrs = value["gateway"]["config"]["service"]["telemetry"]["resource"]["attributes"]
        attrs = {item["name"]: item["value"] for item in attrs}
        assert attrs["k8s.cluster.name"] == CLUSTER
        assert attrs["sregym.run.id"] == run_id
        assert RUN_IDS[1 - RUN_IDS.index(run_id)] not in json.dumps(value)


@pytest.mark.parametrize("run_id", RUN_IDS)
def test_every_signal_query_requires_its_own_attempt_on_the_shared_cluster(run_id):
    backend = SplunkHttpBackend(configuration())
    context = AttemptContext(run_id, "full", True, START)
    end = START + timedelta(minutes=10)
    backend._signalflow = Mock(return_value={})
    backend._search_log_examples = Mock(return_value=[])
    backend._graphql = Mock(return_value={})
    try:
        backend.query_signal("metrics", context, SCOPE, "connection", end, 1)
        backend.query_application_metric_values(("probe_success",), context, SCOPE, end, 1)
        backend.collector_snapshot(context, end, 1)
        backend.collector_queue_minimum(context, START, end, 1)
        for call in backend._signalflow.call_args_list:
            for statement in call.args[0].splitlines():
                assert f"filter('k8s.cluster.name', '{CLUSTER}')" in statement
                assert f"filter('sregym.run.id', '{run_id}')" in statement
        for signal in ("logs", "kubernetes_events"):
            backend.query_signal(signal, context, SCOPE, "connection", end, 1)
        for kind in ("pods", "events"):
            backend.query_object(kind, context, SCOPE, "connection", end, 1)
        backend.query_scoped_examples("logs", context, SCOPE, "connection", end, 1)
        for call in backend._search_log_examples.call_args_list:
            assert f'k8s.cluster.name="{CLUSTER}"' in call.args[0]
            assert f'sregym.run.id="{run_id}"' in call.args[0]
        backend.query_signal("traces", context, SCOPE, "connection", end, 1)
        filters = backend._graphql.call_args.args[3]["parameters"]["sharedParameters"]["filters"][0]
        assert filters["traceFilter"]["tags"] == [
            {"tag": "sf_environment", "operation": "IN", "values": [run_id]},
        ]
        assert filters["spanFilters"][0]["tags"] == [
            {"tag": "k8s.cluster.name", "operation": "IN", "values": [CLUSTER]},
        ]
    finally:
        backend.close()


def test_saved_legacy_cluster_scope_overrides_current_environment():
    backend = SplunkHttpBackend(configuration())
    old_id = RUN_IDS[0]
    context = AttemptContext(old_id, "full", True, START, cluster_name=old_id)
    backend._search_log_examples = Mock(return_value=[])
    backend._graphql = Mock(return_value={})
    try:
        backend.query_signal("logs", context, SCOPE, "connection", START, 1)
        query = backend._search_log_examples.call_args.args[0]
        assert f'k8s.cluster.name="{old_id}"' in query
        assert CLUSTER not in query
        backend.query_signal("traces", context, SCOPE, "connection", START, 1)
        filters = backend._graphql.call_args.args[3]["parameters"]["sharedParameters"]["filters"][0]
        assert filters["traceFilter"]["tags"] == []
        assert filters["spanFilters"][0]["tags"][0]["values"] == [old_id]
    finally:
        backend.close()


def test_pinned_chart_preserves_attempt_attributes_on_all_queried_signal_pipelines():
    if shutil.which("helm") is None:
        pytest.skip("Helm is not installed")
    command = [
        "helm", "template", "test", "splunk-otel-collector-chart/splunk-otel-collector",
        "--version", CHART_VERSION, "--namespace", "sregym-observability",
        "-f", "sregym/observer/splunk/values.yaml",
        "--set", "splunkObservability.realm=rc0",
        "--set", "splunkPlatform.endpoint=https://example.invalid:8088/services/collector",
        "--set", f"clusterName={CLUSTER}",
        "--set-json", "extraAttributes.custom=" + json.dumps([{"name": "sregym.run.id", "value": RUN_IDS[0]}]),
    ]
    rendered = subprocess.run(command, capture_output=True, text=True, timeout=30)
    if rendered.returncode and "repo splunk-otel-collector-chart not found" in rendered.stderr:
        pytest.skip("Pinned Splunk chart repository is not configured")
    assert rendered.returncode == 0, rendered.stderr
    documents = [doc for doc in yaml.safe_load_all(rendered.stdout) if isinstance(doc, dict)]
    expected = {
        "sregym-splunk-otel-otel-agent": ("logs", "metrics"),
        "sregym-splunk-otel-otel-k8s-cluster-receiver": ("logs/objects", "metrics"),
        "sregym-splunk-otel-otel-collector": ("traces", "metrics", "metrics/sregym_application"),
    }
    for name, pipelines in expected.items():
        doc = next(doc for doc in documents if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == name)
        config = yaml.safe_load(doc["data"]["relay"])
        for pipeline_name in pipelines:
            attributes = {}
            for processor in config["service"]["pipelines"][pipeline_name]["processors"]:
                if processor.startswith("resource"):
                    for item in config["processors"][processor].get("attributes", []):
                        if "value" in item:
                            attributes[item["key"]] = item["value"]
            assert attributes["k8s.cluster.name"] == CLUSTER, (name, pipeline_name)
            assert attributes["sregym.run.id"] == RUN_IDS[0], (name, pipeline_name)
