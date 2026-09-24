import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
import yaml
from kubernetes.client.rest import ApiException

from sregym.observability import create_provider
from sregym.observability import splunk as splunk_module
from sregym.observability.base import ApplicationScope, AttemptContext, ProviderError, serialize_provider_artifact
from sregym.observability.splunk import (
    CHART_VERSION,
    NAMESPACE,
    RELEASE_NAME,
    SECRET_NAME,
    CollectorSnapshot,
    ReliabilityPolicy,
    SplunkBackendError,
    SplunkConfig,
    SplunkHttpBackend,
    SplunkObservabilityProvider,
)

RUN_ID = "anon_0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)
ACCESS_TOKEN = "access-token-value"
INGEST_TOKEN = "ingest-token-value"
HEC_TOKEN = "hec-token-value"
SIGNALS = ("metrics", "traces", "logs", "kubernetes_events")


def valid_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "SF_TOKEN": ACCESS_TOKEN,
        "SPLUNK_O11Y_INGEST_TOKEN": INGEST_TOKEN,
        "SFX_REALM": "us0",
        "SPLUNK_HOST": "http-inputs.example.splunkcloud.com",
        "SPLUNK_HEC_PORT": "8088",
        "SPLUNK_HEC_TOKEN": HEC_TOKEN,
    }
    environment.update(overrides)
    return environment


def attempt_context() -> AttemptContext:
    return AttemptContext(RUN_ID, "full", True, NOW)


def successful_command(command: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


def snapshot(
    *,
    sent: int | None = 10,
    failed: int | None = 0,
    enqueue_failed: int | None = 0,
    queue: int | None = 0,
) -> CollectorSnapshot:
    return CollectorSnapshot(
        sent=dict.fromkeys(SIGNALS, sent),
        send_failed=dict.fromkeys(SIGNALS, failed),
        enqueue_failed=dict.fromkeys(SIGNALS, enqueue_failed),
        queue_size=dict.fromkeys(SIGNALS, queue),
    )


class FakeBackend:
    def __init__(self) -> None:
        self.connection_id = "connection-default"
        self.connection_outcomes: list[str | Exception] = []
        self.query_outcomes: dict[str, list[int | Exception]] = {signal: [] for signal in SIGNALS}
        self.snapshot_outcomes: list[CollectorSnapshot | Exception] = []
        self.query_calls: list[tuple[str, str, tuple[str, ...], str]] = []
        self.snapshot_calls: list[str] = []
        self.connection_calls = 0
        self.requested_connection_ids: list[str | None] = []
        self.closed = False

    def resolve_logs_connection(self, timeout_seconds: float, requested_connection_id: str | None = None) -> str:
        self.connection_calls += 1
        self.requested_connection_ids.append(requested_connection_id)
        outcome = self.connection_outcomes.pop(0) if self.connection_outcomes else self.connection_id
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def query_signal(
        self,
        signal: str,
        context: AttemptContext,
        scope: ApplicationScope,
        connection_id: str,
        checked_at: datetime,
        timeout_seconds: float,
    ) -> int:
        self.query_calls.append((signal, context.run_id, scope.namespaces, connection_id))
        outcomes = self.query_outcomes[signal]
        outcome = outcomes.pop(0) if outcomes else 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def collector_snapshot(
        self,
        context: AttemptContext,
        checked_at: datetime,
        timeout_seconds: float,
    ) -> CollectorSnapshot:
        self.snapshot_calls.append(context.run_id)
        outcome = self.snapshot_outcomes.pop(0) if self.snapshot_outcomes else snapshot()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        self.closed = True


def reliability_policy(**overrides: float | int) -> ReliabilityPolicy:
    values = {
        "readiness_timeout_seconds": 30.0,
        "drain_timeout_seconds": 20.0,
        "request_timeout_seconds": 2.0,
        "max_attempts": 3,
        "initial_backoff_seconds": 1.0,
        "max_backoff_seconds": 4.0,
        "retry_after_cap_seconds": 3.0,
    }
    values.update(overrides)
    return ReliabilityPolicy(**values)


def prepared_provider(
    backend: FakeBackend,
    *,
    policy: ReliabilityPolicy | None = None,
    now: datetime = NOW + timedelta(seconds=5),
    sleep: Mock | None = None,
    monotonic: Mock | None = None,
) -> SplunkObservabilityProvider:
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()),
        core_api=Mock(),
        run=successful_command,
        backend=backend,
        policy=policy or reliability_policy(),
        now=lambda: now,
        monotonic=monotonic or Mock(return_value=0.0),
        sleep=sleep or Mock(),
        jitter=lambda delay: 0.0,
    )
    provider.prepare_attempt(attempt_context())
    return provider


@pytest.mark.parametrize(
    "missing",
    (
        "SF_TOKEN",
        "SPLUNK_O11Y_INGEST_TOKEN",
        "SFX_REALM",
        "SPLUNK_HOST",
        "SPLUNK_HEC_PORT",
        "SPLUNK_HEC_TOKEN",
    ),
)
def test_config_requires_every_credential_without_echoing_values(missing):
    environment = valid_environment()
    environment.pop(missing)

    with pytest.raises(ProviderError) as raised:
        SplunkConfig.from_env(environment)

    assert raised.value.kind == "configuration"
    assert missing in str(raised.value)
    assert ACCESS_TOKEN not in str(raised.value)
    assert INGEST_TOKEN not in str(raised.value)
    assert HEC_TOKEN not in str(raised.value)


def test_config_builds_https_hec_endpoint_and_safe_metadata():
    config = SplunkConfig.from_env(valid_environment(SPLUNK_HEC_INDEX="sregym_logs"))

    assert config.hec_endpoint == "https://http-inputs.example.splunkcloud.com:8088/services/collector/event"
    assert config.hec_port == 8088
    assert config.hec_index == "sregym_logs"
    assert config.artifact_metadata() == {
        "hec_host": "http-inputs.example.splunkcloud.com",
        "hec_index": "sregym_logs",
        "hec_port": 8088,
        "realm": "us0",
    }
    encoded = json.dumps(config.artifact_metadata(), sort_keys=True) + repr(config)
    assert ACCESS_TOKEN not in encoded
    assert INGEST_TOKEN not in encoded
    assert HEC_TOKEN not in encoded


def test_config_uses_the_hec_token_default_index_when_unset():
    assert SplunkConfig.from_env(valid_environment()).hec_index is None


def test_config_accepts_an_explicit_logs_connection_id():
    config = SplunkConfig.from_env(valid_environment(SPLUNK_LOGS_CONNECTION_ID="HPEC1vyAAAA"))

    assert config.logs_connection_id == "HPEC1vyAAAA"


@pytest.mark.parametrize("port", ("", "8088/tcp", "０８０８", "0", "65536", "-1"))
def test_config_rejects_invalid_hec_ports(port):
    with pytest.raises(ProviderError, match="SPLUNK_HEC_PORT"):
        SplunkConfig.from_env(valid_environment(SPLUNK_HEC_PORT=port))


@pytest.mark.parametrize(
    "overrides,variable",
    (
        ({"SPLUNK_HOST": "https://example.com"}, "SPLUNK_HOST"),
        ({"SPLUNK_HOST": "user@example.com"}, "SPLUNK_HOST"),
        ({"SPLUNK_HOST": "example.com/path"}, "SPLUNK_HOST"),
        ({"SPLUNK_HEC_INDEX": "logs,other"}, "SPLUNK_HEC_INDEX"),
        ({"SPLUNK_LOGS_CONNECTION_ID": "connection id"}, "SPLUNK_LOGS_CONNECTION_ID"),
        ({"SFX_REALM": "https://us0"}, "SFX_REALM"),
    ),
)
def test_config_rejects_unsafe_destination_fields(overrides, variable):
    with pytest.raises(ProviderError, match=variable):
        SplunkConfig.from_env(valid_environment(**overrides))


def test_values_pin_secure_bounded_single_gateway_configuration():
    values_path = Path("sregym/observer/splunk/values.yaml")
    values = yaml.safe_load(values_path.read_text())

    assert CHART_VERSION == "0.160.0"
    assert values["fullnameOverride"] == RELEASE_NAME
    assert values["secret"] == {"create": False, "name": SECRET_NAME, "validateSecret": True}
    assert values["featureGates"]["mountSplunkSecretAsFile"] is True
    assert values["splunkPlatform"]["insecureSkipVerify"] is False
    assert values["gateway"]["enabled"] is True
    assert values["gateway"]["tokenPassthrough"] is False
    assert values["gateway"]["replicaCount"] == 1
    assert values["agent"]["enabled"] is True
    assert values["clusterReceiver"]["eventsEnabled"] is True
    assert values["agent"]["ports"] == {
        "otlp": None,
        "otlp-http": None,
        "zipkin": None,
        "jaeger-thrift": None,
        "jaeger-grpc": None,
    }
    assert values["logsCollection"]["containers"]["excludePaths"] == [
        "/var/log/pods/chaos-mesh_*/*/*.log",
        "/var/log/pods/khaos_*/*/*.log",
        "/var/log/pods/kube-system_*/*/*.log",
        "/var/log/pods/observe_*/*/*.log",
        "/var/log/pods/openebs_*/*/*.log",
        "/var/log/pods/sregym-observability_*/*/*.log",
    ]
    for workload in ("agent", "clusterReceiver", "gateway"):
        resources = values[workload]["resources"]
        assert set(resources) == {"requests", "limits"}
        assert set(resources["requests"]) == {"cpu", "memory"}
        assert set(resources["limits"]) == {"cpu", "memory"}


def test_prepare_creates_secret_through_api_and_runs_idempotent_helm_upgrade():
    commands: list[tuple[list[str], str | None]] = []
    core_api = Mock()

    def run(command: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        commands.append((command, stdin))
        return successful_command(command, stdin)

    provider = SplunkObservabilityProvider(SplunkConfig.from_env(valid_environment()), core_api=core_api, run=run)

    first = provider.prepare_attempt(attempt_context())
    second = provider.prepare_attempt(attempt_context())

    assert first is second
    assert first.run_id == RUN_ID
    assert first.endpoint == f"{RELEASE_NAME}.{NAMESPACE}.svc.cluster.local:4317"
    assert first.resource_attributes == {
        "deployment.environment": RUN_ID,
        "deployment.environment.name": RUN_ID,
        "sregym.run.id": RUN_ID,
    }
    core_api.create_namespace.assert_called_once()
    core_api.create_namespaced_secret.assert_called_once()
    secret_call = core_api.create_namespaced_secret.call_args
    assert secret_call.kwargs["namespace"] == NAMESPACE
    secret = secret_call.kwargs["body"]
    assert secret.metadata.name == SECRET_NAME
    assert secret.string_data == {
        "splunk_observability_access_token": INGEST_TOKEN,
        "splunk_platform_hec_token": HEC_TOKEN,
    }
    assert len(commands) == 1
    command, stdin = commands[0]
    assert command[:4] == ["helm", "upgrade", "--install", RELEASE_NAME]
    assert command[command.index("--repository-config") + 1] == splunk_module.os.devnull
    assert command[command.index("--version") : command.index("--version") + 2] == ["--version", CHART_VERSION]
    assert "--atomic" in command
    assert "--wait" in command
    assert command[-2:] == ["--values", "-"]
    assert stdin is not None
    runtime_values = yaml.safe_load(stdin)
    assert runtime_values == {
        "clusterName": RUN_ID,
        "extraAttributes": {
            "custom": [
                {"name": "sregym.run.id", "value": RUN_ID},
                {"name": "deployment.environment", "value": RUN_ID},
                {"name": "deployment.environment.name", "value": RUN_ID},
            ]
        },
        "gateway": {
            "config": {
                "service": {
                    "telemetry": {
                        "resource": {
                            "attributes": [
                                {"name": "service.name", "value": "otel-collector"},
                                {"name": "otelcol.service.mode", "value": "gateway"},
                                {"name": "k8s.node.name", "value": "${K8S_NODE_NAME}"},
                                {"name": "k8s.pod.name", "value": "${K8S_POD_NAME}"},
                                {"name": "k8s.pod.uid", "value": "${K8S_POD_UID}"},
                                {"name": "k8s.namespace.name", "value": "${K8S_NAMESPACE}"},
                                {"name": "k8s.cluster.name", "value": RUN_ID},
                                {"name": "sregym.run.id", "value": RUN_ID},
                                {"name": "deployment.environment", "value": RUN_ID},
                                {"name": "deployment.environment.name", "value": RUN_ID},
                            ]
                        }
                    }
                }
            }
        },
        "splunkObservability": {"realm": "us0"},
        "splunkPlatform": {
            "endpoint": "https://http-inputs.example.splunkcloud.com:8088/services/collector/event",
            "insecureSkipVerify": False,
        },
    }
    rendered_inputs = json.dumps({"command": command, "stdin": stdin, "export": first.endpoint})
    assert ACCESS_TOKEN not in rendered_inputs
    assert INGEST_TOKEN not in rendered_inputs
    assert HEC_TOKEN not in rendered_inputs


def test_prepare_replaces_an_existing_secret_and_accepts_existing_namespace():
    core_api = Mock()
    core_api.create_namespace.side_effect = ApiException(status=409)
    core_api.create_namespaced_secret.side_effect = ApiException(status=409)
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()), core_api=core_api, run=successful_command
    )

    provider.prepare_attempt(attempt_context())

    core_api.patch_namespaced_secret.assert_called_once()
    assert core_api.patch_namespaced_secret.call_args.kwargs["name"] == SECRET_NAME
    assert core_api.patch_namespaced_secret.call_args.kwargs["namespace"] == NAMESPACE


@pytest.mark.parametrize("operation", ("namespace", "secret", "helm"))
def test_prepare_failures_are_classified_without_leaking_secrets(operation):
    core_api = Mock()

    def failed_command(command: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout=f"access={ACCESS_TOKEN}",
            stderr=f"hec={HEC_TOKEN}",
        )

    run = successful_command
    if operation == "namespace":
        core_api.create_namespace.side_effect = ApiException(status=500, reason=ACCESS_TOKEN)
    elif operation == "secret":
        core_api.create_namespaced_secret.side_effect = ApiException(status=500, reason=HEC_TOKEN)
    else:
        run = failed_command
    provider = SplunkObservabilityProvider(SplunkConfig.from_env(valid_environment()), core_api=core_api, run=run)

    with pytest.raises(ProviderError) as raised:
        provider.prepare_attempt(attempt_context())

    encoded = str(raised.value) + repr(raised.value) + json.dumps(serialize_provider_artifact(raised.value))
    assert ACCESS_TOKEN not in encoded
    assert HEC_TOKEN not in encoded


def test_preflight_checks_helm_without_persisting_credentials():
    commands: list[tuple[list[str], str | None]] = []

    def run(command: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        commands.append((command, stdin))
        return successful_command(command, stdin)

    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()), core_api=Mock(), run=run, backend=FakeBackend()
    )

    assert provider.preflight() is None
    assert commands == [(["helm", "version", "--short"], None)]


def test_preflight_resolves_the_configured_logs_connection():
    backend = FakeBackend()
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment(SPLUNK_LOGS_CONNECTION_ID="HPEC1vyAAAA")),
        core_api=Mock(),
        run=successful_command,
        backend=backend,
    )

    provider.preflight()

    assert backend.requested_connection_ids == ["HPEC1vyAAAA"]


@pytest.mark.parametrize(
    "error,kind",
    (
        (ApiException(status=401, reason=ACCESS_TOKEN), "authentication"),
        (ApiException(status=403, reason=HEC_TOKEN), "permission"),
        (ApiException(status=500, reason=ACCESS_TOKEN), "configuration"),
        (RuntimeError(HEC_TOKEN), "configuration"),
    ),
)
def test_preflight_classifies_kubernetes_access_without_leaking_secrets(error, kind):
    core_api = Mock()
    core_api.get_api_resources.side_effect = error
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()), core_api=core_api, run=successful_command
    )

    with pytest.raises(ProviderError) as raised:
        provider.preflight()

    assert raised.value.kind == kind
    assert ACCESS_TOKEN not in str(raised.value)
    assert HEC_TOKEN not in str(raised.value)


def test_preflight_rejects_a_missing_values_file(tmp_path):
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()),
        core_api=Mock(),
        run=successful_command,
        values_path=tmp_path / "missing.yaml",
    )

    with pytest.raises(ProviderError, match="values are unavailable"):
        provider.preflight()


def test_default_command_runner_uses_safe_subprocess_arguments(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess(["helm"], 0, stdout="ok", stderr=""))
    monkeypatch.setattr(splunk_module.subprocess, "run", run)
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()), core_api=Mock(), backend=FakeBackend()
    )

    provider.preflight()

    run.assert_called_once_with(["helm", "version", "--short"], input=None, capture_output=True, text=True, check=False)


def test_command_runner_exceptions_are_redacted():
    def raise_secret(command: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        raise RuntimeError(f"{ACCESS_TOKEN} {HEC_TOKEN}")

    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()), core_api=Mock(), run=raise_secret
    )

    with pytest.raises(ProviderError) as raised:
        provider.preflight()

    assert ACCESS_TOKEN not in str(raised.value)
    assert HEC_TOKEN not in str(raised.value)


def test_factory_constructs_splunk_provider_from_the_environment(monkeypatch):
    for key, value in valid_environment().items():
        monkeypatch.setenv(key, value)

    provider = create_provider("splunk")

    assert isinstance(provider, SplunkObservabilityProvider)


def test_close_is_idempotent_and_deletes_only_provider_secret():
    commands: list[tuple[list[str], str | None]] = []
    core_api = Mock()

    def run(command: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        commands.append((command, stdin))
        return successful_command(command, stdin)

    provider = SplunkObservabilityProvider(SplunkConfig.from_env(valid_environment()), core_api=core_api, run=run)
    provider.prepare_attempt(attempt_context())

    assert provider.close() is None
    assert provider.close() is None

    uninstall = [call for call in commands if call[0][1] == "uninstall"]
    assert uninstall == [
        (
            ["helm", "uninstall", RELEASE_NAME, "--namespace", NAMESPACE, "--ignore-not-found", "--wait"],
            None,
        )
    ]
    core_api.delete_namespaced_secret.assert_called_once_with(name=SECRET_NAME, namespace=NAMESPACE)


def test_close_accepts_an_already_deleted_secret():
    core_api = Mock()
    core_api.delete_namespaced_secret.side_effect = ApiException(status=404)
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()), core_api=core_api, run=successful_command
    )
    provider.prepare_attempt(attempt_context())

    assert provider.close() is None


def test_close_without_prepare_has_no_side_effects():
    core_api = Mock()
    run = Mock(side_effect=AssertionError("command must not run"))
    provider = SplunkObservabilityProvider(SplunkConfig.from_env(valid_environment()), core_api=core_api, run=run)

    assert provider.close() is None
    assert provider.close() is None
    run.assert_not_called()
    core_api.delete_namespaced_secret.assert_not_called()


@pytest.mark.parametrize("failure", ("helm", "secret_api", "secret_other"))
def test_close_classifies_cleanup_failures_and_still_attempts_both_operations(failure):
    core_api = Mock()

    def run(command: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        return_code = 1 if failure == "helm" and command[1] == "uninstall" else 0
        return subprocess.CompletedProcess(command, return_code, stdout=ACCESS_TOKEN, stderr=HEC_TOKEN)

    if failure == "secret_api":
        core_api.delete_namespaced_secret.side_effect = ApiException(status=500, reason=HEC_TOKEN)
    elif failure == "secret_other":
        core_api.delete_namespaced_secret.side_effect = RuntimeError(ACCESS_TOKEN)
    provider = SplunkObservabilityProvider(SplunkConfig.from_env(valid_environment()), core_api=core_api, run=run)
    provider.prepare_attempt(attempt_context())

    with pytest.raises(ProviderError) as raised:
        provider.close()

    assert raised.value.kind == "cleanup"
    assert ACCESS_TOKEN not in str(raised.value)
    assert HEC_TOKEN not in str(raised.value)
    core_api.delete_namespaced_secret.assert_called_once()


def test_close_can_be_retried_after_a_transient_cleanup_failure():
    core_api = Mock()
    uninstall_attempts = 0

    def run(command: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        nonlocal uninstall_attempts
        if command[1] == "uninstall":
            uninstall_attempts += 1
            return_code = 1 if uninstall_attempts == 1 else 0
            return subprocess.CompletedProcess(command, return_code, stdout="", stderr="")
        return successful_command(command, stdin)

    provider = SplunkObservabilityProvider(SplunkConfig.from_env(valid_environment()), core_api=core_api, run=run)
    provider.prepare_attempt(attempt_context())

    with pytest.raises(ProviderError, match="cleanup failed"):
        provider.close()
    assert provider.close() is None

    assert uninstall_attempts == 2
    assert core_api.delete_namespaced_secret.call_count == 2


def test_default_kubernetes_client_is_loaded_lazily(monkeypatch):
    core_api = Mock()
    load = Mock()
    constructor = Mock(return_value=core_api)
    monkeypatch.setattr(splunk_module.kubernetes_config, "load_kube_config", load)
    monkeypatch.setattr(splunk_module.client, "CoreV1Api", constructor)
    provider = SplunkObservabilityProvider(SplunkConfig.from_env(valid_environment()), run=successful_command)

    provider.prepare_attempt(attempt_context())

    load.assert_called_once_with()
    constructor.assert_called_once_with()


def test_kubernetes_client_load_failure_is_redacted(monkeypatch):
    def fail_load() -> None:
        raise RuntimeError(ACCESS_TOKEN)

    monkeypatch.setattr(splunk_module.kubernetes_config, "load_kube_config", fail_load)
    provider = SplunkObservabilityProvider(SplunkConfig.from_env(valid_environment()), run=successful_command)

    with pytest.raises(ProviderError) as raised:
        provider.prepare_attempt(attempt_context())

    assert ACCESS_TOKEN not in str(raised.value)


@pytest.mark.parametrize("operation", ("namespace", "secret_create", "secret_update", "secret_update_api"))
def test_unexpected_kubernetes_errors_are_redacted(operation):
    core_api = Mock()
    if operation == "namespace":
        core_api.create_namespace.side_effect = RuntimeError(ACCESS_TOKEN)
    elif operation == "secret_create":
        core_api.create_namespaced_secret.side_effect = RuntimeError(HEC_TOKEN)
    elif operation == "secret_update":
        core_api.create_namespaced_secret.side_effect = ApiException(status=409)
        core_api.patch_namespaced_secret.side_effect = RuntimeError(ACCESS_TOKEN)
    else:
        core_api.create_namespaced_secret.side_effect = ApiException(status=409)
        core_api.patch_namespaced_secret.side_effect = ApiException(status=403, reason=ACCESS_TOKEN)
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()), core_api=core_api, run=successful_command
    )

    with pytest.raises(ProviderError) as raised:
        provider.prepare_attempt(attempt_context())

    assert ACCESS_TOKEN not in str(raised.value)
    assert HEC_TOKEN not in str(raised.value)
    if operation == "secret_update_api":
        assert raised.value.kind == "permission"


def test_a_new_attempt_reconfigures_the_same_idempotent_release():
    commands: list[list[str]] = []

    def run(command: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return successful_command(command, stdin)

    provider = SplunkObservabilityProvider(SplunkConfig.from_env(valid_environment()), core_api=Mock(), run=run)
    provider.prepare_attempt(attempt_context())
    other = AttemptContext("anon_abcdef0123456789abcdef0123456789", "full", True, NOW)

    export = provider.prepare_attempt(other)

    assert export.run_id == other.run_id
    assert len(commands) == 2


def test_prepare_after_close_and_unprepared_assurance_methods_fail_safely():
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()), core_api=Mock(), run=successful_command, backend=FakeBackend()
    )
    context = attempt_context()
    scope = ApplicationScope("social-network", ("social-network",))

    with pytest.raises(ProviderError, match="prepared"):
        provider.wait_until_queryable(context, scope)
    with pytest.raises(ProviderError, match="opening readiness"):
        provider.finish_attempt(context, scope)
    provider.close()
    with pytest.raises(ProviderError, match="already closed"):
        provider.prepare_attempt(context)


@pytest.mark.parametrize(
    "connections,expected",
    (
        (
            [
                {
                    "connectionID": "first",
                    "connectionName": "first",
                    "isDefaultConnection": False,
                    "isAccessible": True,
                },
                {
                    "connectionID": "default",
                    "connectionName": "default",
                    "isDefaultConnection": True,
                    "isAccessible": True,
                },
            ],
            "default",
        ),
        (
            [
                {
                    "connectionID": "inaccessible-default",
                    "connectionName": "default",
                    "isDefaultConnection": True,
                    "isAccessible": False,
                },
                {
                    "connectionID": "fallback",
                    "connectionName": "fallback",
                    "isDefaultConnection": False,
                    "isAccessible": True,
                },
            ],
            "fallback",
        ),
    ),
)
def test_http_backend_selects_accessible_default_then_first_accessible(connections, expected):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"getConnections": connections}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    backend = SplunkHttpBackend(SplunkConfig.from_env(valid_environment()), http_client=client)

    assert backend.resolve_logs_connection(2.0) == expected
    payload = json.loads(requests[0].content)
    assert requests[0].url == "https://app.us0.signalfx.com/v2/logs/graphql"
    assert payload["operationName"] == "getConnections"
    assert requests[0].headers["x-sf-token"] == ACCESS_TOKEN
    backend.close()


def test_http_backend_selects_an_explicit_accessible_connection_over_the_default():
    connections = [
        {
            "connectionID": "default",
            "connectionName": "default",
            "isDefaultConnection": True,
            "isAccessible": True,
        },
        {
            "connectionID": "requested",
            "connectionName": "requested",
            "isDefaultConnection": False,
            "isAccessible": True,
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"getConnections": connections}})

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert backend.resolve_logs_connection(2.0, "requested") == "requested"


def test_http_backend_rejects_an_inaccessible_explicit_connection():
    connections = [
        {
            "connectionID": "requested",
            "connectionName": "requested",
            "isDefaultConnection": True,
            "isAccessible": False,
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"getConnections": connections}})

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SplunkBackendError) as raised:
        backend.resolve_logs_connection(2.0, "requested")

    assert raised.value.status_code == 400
    assert raised.value.transient is False


def test_http_backend_rejects_missing_accessible_logs_connection():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"getConnections": []}})

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SplunkBackendError) as raised:
        backend.resolve_logs_connection(2.0)

    assert raised.value.status_code == 400
    assert raised.value.transient is False


def test_http_backend_queries_all_four_signals_with_exported_run_scope():
    requests: list[tuple[str, dict | None, str]] = []
    log_jobs = iter(("log-job", "event-job"))
    sse = "\n".join(
        (
            "event: metadata",
            'data: {"tsId":"metric","properties":{"sf_streamLabel":"readiness"}}',
            "",
            "event: data",
            'data: {"logicalTimestampMs":1,"data":[{"tsId":"metric","value":2}]}',
            "",
            "event: control-message",
            'data: {"event":"END_OF_CHANNEL","timestampMs":1}',
            "",
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/signalflow/execute":
            requests.append(("signalflow", None, request.content.decode()))
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})
        payload = json.loads(request.content)
        operation = payload["operationName"]
        requests.append((operation, payload, ""))
        if operation == "StartAnalyticsSearch":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "startAnalyticsSearch": {
                            "sections": [
                                {
                                    "sectionType": "traceExamples",
                                    "isComplete": True,
                                    "traceExamples": [{"traceId": "trace-1"}],
                                }
                            ]
                        }
                    }
                },
            )
        if operation == "createSearchJob":
            return httpx.Response(200, json={"data": {"createSearchJob": {"id": next(log_jobs), "status": "RUNNING"}}})
        if operation == "searchJobResultsWithoutFieldsSummary":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "searchJob": {
                            "status": "DONE",
                            "results": {"results": [["one result"]]},
                        }
                    }
                },
            )
        raise AssertionError(operation)

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    context = attempt_context()
    scope = ApplicationScope("social-network", ("social-network", "observe"))
    checked_at = NOW + timedelta(minutes=1)

    counts = {
        signal: backend.query_signal(signal, context, scope, "connection-default", checked_at, 2.0)
        for signal in SIGNALS
    }

    assert counts == dict.fromkeys(SIGNALS, 1) | {"metrics": 2}
    signalflow_program = next(content for operation, _, content in requests if operation == "signalflow")
    assert "otelcol_exporter_sent_metric_points" in signalflow_program
    assert f"filter('sregym.run.id', '{RUN_ID}')" in signalflow_program
    trace_payload = next(payload for operation, payload, _ in requests if operation == "StartAnalyticsSearch")
    assert trace_payload is not None
    trace_tags = trace_payload["variables"]["parameters"]["sharedParameters"]["filters"][0]["spanFilters"][0]["tags"]
    assert trace_tags == [{"tag": "k8s.cluster.name", "operation": "IN", "values": [RUN_ID]}]
    assert trace_payload["variables"]["parameters"]["sectionsParameters"] == [
        {"sectionType": "traceExamples", "limit": 1}
    ]
    log_payloads = [
        payload for operation, payload, _ in requests if operation == "createSearchJob" and payload is not None
    ]
    log_queries = [payload["variables"]["query"] for payload in log_payloads]
    assert all('index="*"' in query for query in log_queries)
    assert all(f'k8s.cluster.name="{RUN_ID}"' in query and "social-network" in query for query in log_queries)
    assert "k8s.container.name=*" in log_queries[0]
    assert "k8s.event.reason=*" in log_queries[1]
    assert all(payload["variables"]["queryType"] == "SPL1" for payload in log_payloads)
    assert all(payload["variables"]["connectionID"] == "connection-default" for payload in log_payloads)
    assert all(payload["variables"]["queryParameters"]["timezone"] == "UTC" for payload in log_payloads)


@pytest.mark.parametrize(
    "status,retry_after,transient",
    (
        (400, None, False),
        (401, None, False),
        (403, None, False),
        (408, None, True),
        (429, "99", True),
        (503, None, True),
    ),
)
def test_http_backend_classifies_http_status_without_response_body_leaks(status, retry_after, transient):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, text=f"{ACCESS_TOKEN} {HEC_TOKEN}")

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SplunkBackendError) as raised:
        backend.resolve_logs_connection(2.0)

    assert raised.value.status_code == status
    assert raised.value.transient is transient
    assert raised.value.retry_after_seconds == (99.0 if retry_after else None)
    assert ACCESS_TOKEN not in str(raised.value)
    assert HEC_TOKEN not in str(raised.value)


def test_http_backend_classifies_transport_timeout_as_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"{ACCESS_TOKEN} {HEC_TOKEN}", request=request)

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SplunkBackendError) as raised:
        backend.resolve_logs_connection(2.0)

    assert raised.value.status_code is None
    assert raised.value.transient is True
    assert ACCESS_TOKEN not in str(raised.value)


def test_wait_until_queryable_records_all_signals_scope_and_first_visible_lag():
    backend = FakeBackend()
    backend.snapshot_outcomes = [snapshot(queue=2)]
    provider = prepared_provider(backend)
    scope = ApplicationScope("social-network", ("social-network",))

    report = provider.wait_until_queryable(attempt_context(), scope)

    assert report.ready is True
    assert tuple(signal.signal for signal in report.signals) == SIGNALS
    assert all(signal.evidence["count"] == 1 for signal in report.signals)
    assert all(signal.evidence["status_class"] == "2xx" for signal in report.signals)
    for signal in report.signals:
        if signal.signal in ("logs", "kubernetes_events"):
            assert signal.evidence["connection_id"] == "connection-default"
            assert signal.evidence["index"] == "*"
        else:
            assert "connection_id" not in signal.evidence
    assert all(call[1:] == (RUN_ID, ("social-network",), "connection-default") for call in backend.query_calls)
    assert backend.snapshot_calls == [RUN_ID]


@pytest.mark.parametrize("status", (None, 408, 429, 500, 503))
def test_readiness_retries_transient_failures_with_bounded_backoff(status):
    backend = FakeBackend()
    backend.query_outcomes["metrics"] = [
        SplunkBackendError(
            status_code=status,
            transient=True,
            retry_after_seconds=99.0 if status == 429 else None,
        ),
        1,
    ]
    sleep = Mock()
    provider = prepared_provider(backend, sleep=sleep)

    report = provider.wait_until_queryable(attempt_context(), ApplicationScope("social-network", ("social-network",)))

    assert report.ready is True
    expected_delay = 3.0 if status == 429 else 1.0
    sleep.assert_called_once_with(expected_delay)
    assert [call[0] for call in backend.query_calls].count("metrics") == 2
    assert [call[0] for call in backend.query_calls].count("traces") == 1


@pytest.mark.parametrize("status,kind", ((400, "configuration"), (401, "authentication"), (403, "permission")))
def test_readiness_fails_fast_for_terminal_http_errors(status, kind):
    backend = FakeBackend()
    backend.query_outcomes["metrics"] = [SplunkBackendError(status_code=status, transient=False)]
    sleep = Mock()
    provider = prepared_provider(backend, sleep=sleep)

    with pytest.raises(ProviderError) as raised:
        provider.wait_until_queryable(attempt_context(), ApplicationScope("social-network", ("social-network",)))

    assert raised.value.kind == kind
    sleep.assert_not_called()
    assert [call[0] for call in backend.query_calls] == ["metrics"]


def test_readiness_classifies_transient_exhaustion_with_partial_report():
    backend = FakeBackend()
    backend.query_outcomes["metrics"] = [
        SplunkBackendError(status_code=503, transient=True),
        SplunkBackendError(status_code=503, transient=True),
    ]
    provider = prepared_provider(backend, policy=reliability_policy(max_attempts=2))

    with pytest.raises(ProviderError) as raised:
        provider.wait_until_queryable(attempt_context(), ApplicationScope("social-network", ("social-network",)))

    assert raised.value.kind == "transient_exhausted"
    assert raised.value.readiness_report is not None
    assert raised.value.readiness_report.ready is False
    assert next(item for item in raised.value.readiness_report.signals if item.signal == "metrics").ready is False


def test_readiness_failure_evidence_identifies_the_safe_logs_destination():
    backend = FakeBackend()
    backend.query_outcomes["logs"] = [SplunkBackendError(status_code=503, transient=True)]
    provider = prepared_provider(backend, policy=reliability_policy(max_attempts=1))

    with pytest.raises(ProviderError) as raised:
        provider.wait_until_queryable(attempt_context(), ApplicationScope("social-network", ("social-network",)))

    assert raised.value.readiness_report is not None
    logs = next(item for item in raised.value.readiness_report.signals if item.signal == "logs")
    assert logs.evidence == {
        "status_class": "5xx",
        "connection_id": "connection-default",
        "index": "*",
    }


def test_readiness_classifies_empty_results_as_timeout():
    backend = FakeBackend()
    backend.query_outcomes["metrics"] = [0, 0]
    provider = prepared_provider(backend, policy=reliability_policy(max_attempts=2))

    with pytest.raises(ProviderError) as raised:
        provider.wait_until_queryable(attempt_context(), ApplicationScope("social-network", ("social-network",)))

    assert raised.value.kind == "readiness_timeout"
    assert raised.value.readiness_report is not None


def test_finish_attempt_reports_counter_deltas_high_water_and_queue_drain():
    backend = FakeBackend()
    backend.snapshot_outcomes = [
        snapshot(sent=10, queue=2),
        snapshot(sent=15, queue=1),
        snapshot(sent=15, queue=0),
    ]
    sleep = Mock()
    provider = prepared_provider(backend, sleep=sleep)
    scope = ApplicationScope("social-network", ("social-network",))
    provider.wait_until_queryable(attempt_context(), scope)

    delivery = provider.finish_attempt(attempt_context(), scope)

    assert delivery.opening.ready is True
    assert delivery.closing.ready is True
    assert delivery.first_visible_lag_ms == dict.fromkeys(SIGNALS, 5000.0)
    assert delivery.sent_delta == dict.fromkeys(SIGNALS, 5)
    assert delivery.send_failed_delta == dict.fromkeys(SIGNALS, 0)
    assert delivery.enqueue_failed_delta == dict.fromkeys(SIGNALS, 0)
    assert delivery.queue_high_water == dict.fromkeys(SIGNALS, 2)
    assert delivery.queue_final_size == dict.fromkeys(SIGNALS, 0)
    assert delivery.drained is True
    assert delivery.valid is True
    sleep.assert_called_once_with(1.0)
    assert provider.finish_attempt(attempt_context(), scope) is delivery


@pytest.mark.parametrize("invalid_case", ("missing_counter", "send_failure", "enqueue_failure", "undrained"))
def test_finish_attempt_invalidates_missing_failures_and_undrained_queues(invalid_case):
    backend = FakeBackend()
    opening = snapshot(sent=None) if invalid_case == "missing_counter" else snapshot()
    if invalid_case == "send_failure":
        closing = snapshot(failed=1)
    elif invalid_case == "enqueue_failure":
        closing = snapshot(enqueue_failed=1)
    elif invalid_case == "undrained":
        opening = snapshot(queue=1)
        closing = snapshot(queue=2)
    else:
        closing = snapshot(sent=None)
    backend.snapshot_outcomes = [opening, closing, closing]
    provider = prepared_provider(backend, policy=reliability_policy(max_attempts=1))
    scope = ApplicationScope("social-network", ("social-network",))
    provider.wait_until_queryable(attempt_context(), scope)

    delivery = provider.finish_attempt(attempt_context(), scope)

    assert delivery.valid is False
    if invalid_case == "undrained":
        assert delivery.drained is False
        assert delivery.queue_high_water == dict.fromkeys(SIGNALS, 2)
        assert delivery.queue_final_size == dict.fromkeys(SIGNALS, 2)
    elif invalid_case == "missing_counter":
        assert delivery.sent_delta == dict.fromkeys(SIGNALS, None)
    elif invalid_case == "send_failure":
        assert delivery.send_failed_delta == dict.fromkeys(SIGNALS, 1)
    else:
        assert delivery.enqueue_failed_delta == dict.fromkeys(SIGNALS, 1)


def test_finish_attempt_preserves_invalid_report_when_closing_signal_is_missing():
    backend = FakeBackend()
    backend.snapshot_outcomes = [snapshot(), snapshot()]
    provider = prepared_provider(backend, policy=reliability_policy(max_attempts=1))
    scope = ApplicationScope("social-network", ("social-network",))
    provider.wait_until_queryable(attempt_context(), scope)
    backend.query_outcomes["logs"] = [0]

    delivery = provider.finish_attempt(attempt_context(), scope)

    assert delivery.closing.ready is False
    assert delivery.valid is False


def test_finish_attempt_fails_fast_to_an_invalid_report_for_terminal_closing_query():
    backend = FakeBackend()
    backend.snapshot_outcomes = [snapshot(), snapshot()]
    sleep = Mock()
    provider = prepared_provider(backend, sleep=sleep)
    scope = ApplicationScope("social-network", ("social-network",))
    provider.wait_until_queryable(attempt_context(), scope)
    backend.query_outcomes["logs"] = [SplunkBackendError(status_code=403, transient=False)]

    delivery = provider.finish_attempt(attempt_context(), scope)

    assert delivery.closing.ready is False
    assert delivery.valid is False
    assert [call[0] for call in backend.query_calls].count("logs") == 2
    sleep.assert_not_called()


def test_delivery_evidence_and_backend_errors_never_serialize_supplied_secrets():
    backend = FakeBackend()
    backend.snapshot_outcomes = [snapshot(), snapshot()]
    provider = prepared_provider(backend)
    scope = ApplicationScope("social-network", ("social-network",))
    provider.wait_until_queryable(attempt_context(), scope)

    delivery = provider.finish_attempt(attempt_context(), scope)
    encoded = json.dumps(serialize_provider_artifact(delivery), sort_keys=True)
    error_text = repr(SplunkBackendError(status_code=500, transient=True))

    assert ACCESS_TOKEN not in encoded + error_text
    assert HEC_TOKEN not in encoded + error_text


@pytest.mark.parametrize(
    "overrides,message",
    (
        ({"max_attempts": 0}, "positive"),
        ({"request_timeout_seconds": 0.0}, "positive"),
        ({"initial_backoff_seconds": 2.0, "max_backoff_seconds": 1.0}, "maximum backoff"),
    ),
)
def test_reliability_policy_rejects_invalid_bounds(overrides, message):
    with pytest.raises(ValueError, match=message):
        reliability_policy(**overrides)


def test_default_reliability_policy_covers_observed_apm_visibility_lag():
    policy = ReliabilityPolicy()

    assert policy.readiness_timeout_seconds >= 300.0
    assert policy.max_attempts >= 36


def test_http_backend_reads_collector_counters_and_normalizes_sparse_zero_failure_series():
    labels = {
        "sent_metrics": 5,
        "sent_traces": 4,
        "sent_logs": 3,
        "failed_metrics": 0,
        "failed_traces": 1,
        "failed_logs": 0,
        "enqueue_metrics": 0,
        "enqueue_traces": 0,
        "queue_metrics": 2,
        "queue_traces": 1,
        "queue_logs": 0,
    }
    lines: list[str] = []
    for position, (label, value) in enumerate(labels.items()):
        lines.extend(
            (
                "event: metadata",
                f'data: {{"tsId":"{position}","properties":{{"sf_streamLabel":"{label}"}}}}',
                "",
                "event: data",
                f'data: {{"data":[{{"tsId":"{position}","value":{value}}}]}}',
                "",
            )
        )

    def handler(request: httpx.Request) -> httpx.Response:
        assert RUN_ID in request.content.decode()
        return httpx.Response(200, text="\n".join(lines))

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    result = backend.collector_snapshot(attempt_context(), NOW + timedelta(minutes=1), 2.0)

    assert result.sent == {"metrics": 5, "traces": 4, "logs": 3, "kubernetes_events": 3}
    assert result.send_failed == {"metrics": 0, "traces": 1, "logs": 0, "kubernetes_events": 0}
    assert result.enqueue_failed == {
        "metrics": 0,
        "traces": 0,
        "logs": 0,
        "kubernetes_events": 0,
    }
    assert result.queue_size == {"metrics": 2, "traces": 1, "logs": 0, "kubernetes_events": 0}


def test_http_backend_keeps_sparse_failure_counter_unknown_without_companion_evidence():
    labels = {
        "sent_metrics": 5,
        "queue_metrics": 0,
        "sent_traces": 4,
        "queue_traces": 0,
        "sent_logs": 3,
    }
    lines: list[str] = []
    for position, (label, value) in enumerate(labels.items()):
        lines.extend(
            (
                "event: metadata",
                f'data: {{"tsId":"{position}","properties":{{"sf_streamLabel":"{label}"}}}}',
                "",
                "event: data",
                f'data: {{"data":[{{"tsId":"{position}","value":{value}}}]}}',
                "",
            )
        )

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text="\n".join(lines)))
        ),
    )

    result = backend.collector_snapshot(attempt_context(), NOW + timedelta(minutes=1), 2.0)

    assert result.send_failed == {
        "metrics": 0,
        "traces": 0,
        "logs": None,
        "kubernetes_events": None,
    }
    assert result.enqueue_failed == result.send_failed


def test_http_backend_closes_only_its_owned_client(monkeypatch):
    client = Mock()
    monkeypatch.setattr(splunk_module.httpx, "Client", Mock(return_value=client))

    backend = SplunkHttpBackend(SplunkConfig.from_env(valid_environment()))
    backend.close()

    client.close.assert_called_once_with()


@pytest.mark.parametrize(
    "response",
    (
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"errors": [{"message": ACCESS_TOKEN}]}),
    ),
)
def test_http_backend_rejects_malformed_graphql_without_leaking_payload(response):
    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: response)),
    )

    with pytest.raises(SplunkBackendError) as raised:
        backend.resolve_logs_connection(2.0)

    assert raised.value.transient is True
    assert ACCESS_TOKEN not in str(raised.value)


def test_http_backend_rejects_log_jobs_without_an_identifier():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"createSearchJob": {"status": "RUNNING"}}})

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(SplunkBackendError) as raised:
        backend.query_signal(
            "logs",
            attempt_context(),
            ApplicationScope("social-network", ("social-network",)),
            "connection-default",
            NOW,
            2.0,
        )

    assert raised.value.transient is True


def test_http_backend_continues_existing_log_job_until_complete():
    operations: list[str] = []
    results = iter(
        (
            {"data": {"searchJob": {"status": "RUNNING", "results": {"results": []}}}},
            {"data": {"searchJob": {"status": "DONE", "results": {"results": [["row"]]}}}},
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        operation = json.loads(request.content)["operationName"]
        operations.append(operation)
        if operation == "createSearchJob":
            return httpx.Response(200, json={"data": {"createSearchJob": {"id": "job-1"}}})
        return httpx.Response(200, json=next(results))

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    arguments = (
        "logs",
        attempt_context(),
        ApplicationScope("social-network", ("social-network",)),
        "connection-default",
        NOW,
        2.0,
    )

    assert backend.query_signal(*arguments) == 0
    assert backend.query_signal(*arguments) == 1
    assert operations == [
        "createSearchJob",
        "searchJobResultsWithoutFieldsSummary",
        "searchJobResultsWithoutFieldsSummary",
    ]


def test_http_backend_continues_existing_trace_job_until_complete():
    operations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        operation = json.loads(request.content)["operationName"]
        operations.append(operation)
        if operation == "StartAnalyticsSearch":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "startAnalyticsSearch": {
                            "jobId": "trace-job",
                            "sections": [{"sectionType": "traceExamples", "isComplete": False}],
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "getAnalyticsSearch": {
                        "sections": [
                            {
                                "sectionType": "traceExamples",
                                "isComplete": True,
                                "data": {"traceExamples": [{"traceId": "trace-1"}]},
                            }
                        ]
                    }
                }
            },
        )

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    arguments = (
        "traces",
        attempt_context(),
        ApplicationScope("social-network", ("social-network",)),
        "connection-default",
        NOW,
        2.0,
    )

    assert backend.query_signal(*arguments) == 0
    assert backend.query_signal(*arguments) == 1
    assert operations == ["StartAnalyticsSearch", "GetAnalyticsSearch"]


def test_http_backend_restarts_incomplete_trace_response_without_job_id():
    operations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        operations.append(json.loads(request.content)["operationName"])
        return httpx.Response(
            200,
            json={
                "data": {"startAnalyticsSearch": {"sections": [{"sectionType": "traceExamples", "isComplete": False}]}}
            },
        )

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    arguments = (
        "traces",
        attempt_context(),
        ApplicationScope("social-network", ("social-network",)),
        "connection-default",
        NOW,
        2.0,
    )

    assert backend.query_signal(*arguments) == 0
    assert backend.query_signal(*arguments) == 0
    assert operations == ["StartAnalyticsSearch", "StartAnalyticsSearch"]


def test_http_backend_restarts_trace_job_after_poll_error():
    operations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        operation = json.loads(request.content)["operationName"]
        operations.append(operation)
        if operation == "StartAnalyticsSearch":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "startAnalyticsSearch": {
                            "jobId": "trace-job",
                            "sections": [{"sectionType": "traceExamples", "isComplete": False}],
                        }
                    }
                },
            )
        return httpx.Response(200, json={"data": None, "errors": [{"message": "trace job failed"}]})

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    arguments = (
        "traces",
        attempt_context(),
        ApplicationScope("social-network", ("social-network",)),
        "connection-default",
        NOW,
        2.0,
    )

    assert backend.query_signal(*arguments) == 0
    with pytest.raises(SplunkBackendError):
        backend.query_signal(*arguments)
    assert backend.query_signal(*arguments) == 0
    assert operations == ["StartAnalyticsSearch", "GetAnalyticsSearch", "StartAnalyticsSearch"]


def test_http_backend_treats_live_empty_trace_data_list_as_zero_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "startAnalyticsSearch": {
                        "jobId": "trace-job",
                        "sections": [
                            {
                                "sectionType": "traceExamples",
                                "isComplete": False,
                                "data": [],
                                "legacyTraceExamples": [],
                            }
                        ],
                    }
                }
            },
        )

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert (
        backend.query_signal(
            "traces",
            attempt_context(),
            ApplicationScope("social-network", ("social-network",)),
            "connection-default",
            NOW,
            2.0,
        )
        == 0
    )


def test_http_backend_treats_live_null_log_results_as_zero_results():
    def handler(request: httpx.Request) -> httpx.Response:
        operation = json.loads(request.content)["operationName"]
        if operation == "createSearchJob":
            return httpx.Response(200, json={"data": {"createSearchJob": {"id": "job-1"}}})
        return httpx.Response(
            200,
            json={
                "data": {
                    "searchJob": {
                        "status": "RUNNING",
                        "results": {"fields": [], "results": None},
                    }
                }
            },
        )

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert (
        backend.query_signal(
            "logs",
            attempt_context(),
            ApplicationScope("social-network", ("social-network",)),
            "connection-default",
            NOW,
            2.0,
        )
        == 0
    )


@pytest.mark.parametrize(
    "body",
    (
        "event: data\ndata: not-json\n",
        'event: metadata\ndata: {"tsId":"orphan","properties":{}}\n',
        'event: data\ndata: {"data":[{"tsId":"orphan","value":"not-number"}]}\n',
    ),
)
def test_http_backend_handles_incomplete_or_malformed_signalflow_frames(body):
    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))),
    )

    if "not-json" in body and body.startswith("event: data\ndata: not-json"):
        with pytest.raises(SplunkBackendError):
            backend.collector_snapshot(attempt_context(), NOW, 2.0)
    else:
        assert backend.collector_snapshot(attempt_context(), NOW, 2.0) == snapshot(
            sent=None, failed=None, enqueue_failed=None, queue=None
        )


@pytest.mark.parametrize("retry_after,expected", (("invalid", None), ("-5", 0.0)))
def test_http_backend_safely_parses_retry_after(retry_after, expected):
    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(429, headers={"Retry-After": retry_after}))
        ),
    )

    with pytest.raises(SplunkBackendError) as raised:
        backend.resolve_logs_connection(2.0)

    assert raised.value.retry_after_seconds == expected


def test_preflight_retries_transient_connection_lookup_and_closes_backend():
    backend = FakeBackend()
    backend.connection_outcomes = [SplunkBackendError(status_code=503, transient=True), "connection-fallback"]
    sleep = Mock()
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()),
        core_api=Mock(),
        run=successful_command,
        backend=backend,
        policy=reliability_policy(),
        monotonic=Mock(return_value=0.0),
        sleep=sleep,
        jitter=lambda delay: 0.0,
    )

    provider.preflight()
    provider.close()

    assert backend.connection_calls == 2
    sleep.assert_called_once_with(1.0)
    assert backend.closed is True


@pytest.mark.parametrize("status,kind", ((400, "configuration"), (401, "authentication"), (403, "permission")))
def test_preflight_fails_fast_for_terminal_connection_errors(status, kind):
    backend = FakeBackend()
    backend.connection_outcomes = [SplunkBackendError(status_code=status, transient=False)]
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()),
        core_api=Mock(),
        run=successful_command,
        backend=backend,
    )

    with pytest.raises(ProviderError) as raised:
        provider.preflight()

    assert raised.value.kind == kind


def test_preflight_exhausts_transient_connection_retries_at_deadline():
    backend = FakeBackend()
    backend.connection_outcomes = [SplunkBackendError(status_code=503, transient=True)]
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()),
        core_api=Mock(),
        run=successful_command,
        backend=backend,
        policy=reliability_policy(),
        monotonic=Mock(side_effect=(0.0, 31.0)),
    )

    with pytest.raises(ProviderError) as raised:
        provider.preflight()

    assert raised.value.kind == "transient_exhausted"
    assert backend.connection_calls == 1


def test_readiness_stops_at_overall_deadline():
    backend = FakeBackend()
    backend.query_outcomes["metrics"] = [0]
    provider = prepared_provider(
        backend,
        monotonic=Mock(side_effect=(0.0, 0.0, 31.0)),
    )

    with pytest.raises(ProviderError) as raised:
        provider.wait_until_queryable(attempt_context(), ApplicationScope("social-network", ("social-network",)))

    assert raised.value.kind == "readiness_timeout"
    assert [call[0] for call in backend.query_calls].count("metrics") == 1


def test_readiness_caps_backoff_at_remaining_deadline_without_an_extra_query():
    backend = FakeBackend()
    backend.query_outcomes["metrics"] = [0, 1]
    sleep = Mock()
    provider = prepared_provider(
        backend,
        policy=reliability_policy(
            readiness_timeout_seconds=3.0,
            initial_backoff_seconds=10.0,
            max_backoff_seconds=10.0,
        ),
        sleep=sleep,
    )

    with pytest.raises(ProviderError) as raised:
        provider.wait_until_queryable(attempt_context(), ApplicationScope("social-network", ("social-network",)))

    assert raised.value.kind == "readiness_timeout"
    sleep.assert_called_once_with(3.0)
    assert [call[0] for call in backend.query_calls].count("metrics") == 1


@pytest.mark.parametrize("strict_error", (False, True))
def test_collector_snapshot_failures_are_invalid_after_execution_and_fatal_before(strict_error):
    backend = FakeBackend()
    backend.snapshot_outcomes = [SplunkBackendError(status_code=503, transient=True)]
    provider = prepared_provider(backend, policy=reliability_policy(max_attempts=1))
    scope = ApplicationScope("social-network", ("social-network",))

    if strict_error:
        with pytest.raises(ProviderError) as raised:
            provider.wait_until_queryable(attempt_context(), scope)
        assert raised.value.kind == "transient_exhausted"
    else:
        backend.snapshot_outcomes = [snapshot(), SplunkBackendError(status_code=400, transient=False)]
        provider.wait_until_queryable(attempt_context(), scope)
        delivery = provider.finish_attempt(attempt_context(), scope)
        assert delivery.valid is False
        assert delivery.sent_delta == dict.fromkeys(SIGNALS)
        assert delivery.queue_final_size == dict.fromkeys(SIGNALS, 0)


def test_touched_provider_closes_injected_backend_after_cleanup():
    backend = FakeBackend()
    provider = prepared_provider(backend)

    provider.close()

    assert backend.closed is True


def test_query_backend_is_created_lazily(monkeypatch):
    backend = FakeBackend()
    constructor = Mock(return_value=backend)
    monkeypatch.setattr(splunk_module, "SplunkHttpBackend", constructor)
    config = SplunkConfig.from_env(valid_environment())
    provider = SplunkObservabilityProvider(config, core_api=Mock(), run=successful_command)

    assert provider._query_backend() is backend
    assert provider._query_backend() is backend
    constructor.assert_called_once_with(config)


def test_preflight_exhausts_transient_connection_attempt_limit():
    backend = FakeBackend()
    backend.connection_outcomes = [SplunkBackendError(status_code=503, transient=True)]
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()),
        core_api=Mock(),
        run=successful_command,
        backend=backend,
        policy=reliability_policy(max_attempts=1),
    )

    with pytest.raises(ProviderError) as raised:
        provider.preflight()

    assert raised.value.kind == "transient_exhausted"
    assert backend.connection_calls == 1


def test_preflight_caps_connection_backoff_at_remaining_deadline():
    backend = FakeBackend()
    backend.connection_outcomes = [
        SplunkBackendError(status_code=503, transient=True),
        "must-not-be-queried",
    ]
    sleep = Mock()
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()),
        core_api=Mock(),
        run=successful_command,
        backend=backend,
        policy=reliability_policy(
            readiness_timeout_seconds=3.0,
            initial_backoff_seconds=10.0,
            max_backoff_seconds=10.0,
        ),
        monotonic=Mock(return_value=0.0),
        sleep=sleep,
        jitter=lambda delay: 0.0,
    )

    with pytest.raises(ProviderError) as raised:
        provider.preflight()

    assert raised.value.kind == "transient_exhausted"
    sleep.assert_called_once_with(3.0)
    assert backend.connection_calls == 1


@pytest.mark.parametrize("status,kind", ((400, "configuration"), (401, "authentication"), (403, "permission")))
def test_opening_collector_snapshot_fails_fast_for_terminal_errors(status, kind):
    backend = FakeBackend()
    backend.snapshot_outcomes = [SplunkBackendError(status_code=status, transient=False)]
    provider = prepared_provider(backend)

    with pytest.raises(ProviderError) as raised:
        provider.wait_until_queryable(attempt_context(), ApplicationScope("social-network", ("social-network",)))

    assert raised.value.kind == kind


def test_opening_collector_snapshot_retries_transient_errors():
    backend = FakeBackend()
    backend.snapshot_outcomes = [SplunkBackendError(status_code=503, transient=True), snapshot()]
    sleep = Mock()
    provider = prepared_provider(backend, sleep=sleep)

    report = provider.wait_until_queryable(attempt_context(), ApplicationScope("social-network", ("social-network",)))

    assert report.ready is True
    sleep.assert_called_once_with(1.0)


def test_opening_collector_snapshot_stops_retrying_at_deadline():
    backend = FakeBackend()
    backend.snapshot_outcomes = [SplunkBackendError(status_code=503, transient=True)]
    provider = prepared_provider(
        backend,
        monotonic=Mock(side_effect=(0.0, 0.0, 0.0, 31.0)),
    )

    with pytest.raises(ProviderError) as raised:
        provider.wait_until_queryable(attempt_context(), ApplicationScope("social-network", ("social-network",)))

    assert raised.value.kind == "transient_exhausted"
    assert backend.snapshot_calls == [RUN_ID]


def test_opening_collector_snapshot_caps_backoff_at_remaining_deadline():
    backend = FakeBackend()
    backend.snapshot_outcomes = [SplunkBackendError(status_code=503, transient=True), snapshot()]
    sleep = Mock()
    provider = prepared_provider(
        backend,
        policy=reliability_policy(
            readiness_timeout_seconds=3.0,
            initial_backoff_seconds=10.0,
            max_backoff_seconds=10.0,
        ),
        sleep=sleep,
    )

    with pytest.raises(ProviderError) as raised:
        provider.wait_until_queryable(attempt_context(), ApplicationScope("social-network", ("social-network",)))

    assert raised.value.kind == "transient_exhausted"
    sleep.assert_called_once_with(3.0)
    assert backend.snapshot_calls == [RUN_ID]


def test_queue_drain_caps_backoff_at_remaining_deadline():
    backend = FakeBackend()
    backend.snapshot_outcomes = [snapshot(queue=1), snapshot(queue=2), snapshot(queue=0)]
    sleep = Mock()
    provider = prepared_provider(
        backend,
        policy=reliability_policy(
            drain_timeout_seconds=3.0,
            initial_backoff_seconds=10.0,
            max_backoff_seconds=10.0,
        ),
        sleep=sleep,
    )
    scope = ApplicationScope("social-network", ("social-network",))
    provider.wait_until_queryable(attempt_context(), scope)

    delivery = provider.finish_attempt(attempt_context(), scope)

    assert delivery.drained is False
    assert delivery.queue_final_size == dict.fromkeys(SIGNALS, 2)
    sleep.assert_called_once_with(3.0)
    assert backend.snapshot_calls == [RUN_ID, RUN_ID]
