import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

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
    SplunkConfig,
    SplunkObservabilityProvider,
)

RUN_ID = "anon_0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)
ACCESS_TOKEN = "access-token-value"
HEC_TOKEN = "hec-token-value"


def valid_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "SF_TOKEN": ACCESS_TOKEN,
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


@pytest.mark.parametrize(
    "missing",
    ("SF_TOKEN", "SFX_REALM", "SPLUNK_HOST", "SPLUNK_HEC_PORT", "SPLUNK_HEC_TOKEN"),
)
def test_config_requires_every_credential_without_echoing_values(missing):
    environment = valid_environment()
    environment.pop(missing)

    with pytest.raises(ProviderError) as raised:
        SplunkConfig.from_env(environment)

    assert raised.value.kind == "configuration"
    assert missing in str(raised.value)
    assert ACCESS_TOKEN not in str(raised.value)
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
    assert HEC_TOKEN not in encoded


def test_config_defaults_logs_index_to_main():
    assert SplunkConfig.from_env(valid_environment()).hec_index == "main"


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
    assert first.resource_attributes == {"sregym.run.id": RUN_ID}
    core_api.create_namespace.assert_called_once()
    core_api.create_namespaced_secret.assert_called_once()
    secret_call = core_api.create_namespaced_secret.call_args
    assert secret_call.kwargs["namespace"] == NAMESPACE
    secret = secret_call.kwargs["body"]
    assert secret.metadata.name == SECRET_NAME
    assert secret.string_data == {
        "splunk_observability_access_token": ACCESS_TOKEN,
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
        "extraAttributes": {"custom": [{"name": "sregym.run.id", "value": RUN_ID}]},
        "splunkObservability": {"realm": "us0"},
        "splunkPlatform": {
            "endpoint": "https://http-inputs.example.splunkcloud.com:8088/services/collector/event",
            "index": "main",
            "insecureSkipVerify": False,
        },
    }
    rendered_inputs = json.dumps({"command": command, "stdin": stdin, "export": first.endpoint})
    assert ACCESS_TOKEN not in rendered_inputs
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

    provider = SplunkObservabilityProvider(SplunkConfig.from_env(valid_environment()), core_api=Mock(), run=run)

    assert provider.preflight() is None
    assert commands == [(["helm", "version", "--short"], None)]


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
    provider = SplunkObservabilityProvider(SplunkConfig.from_env(valid_environment()), core_api=Mock())

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


def test_prepare_after_close_and_task_f_methods_fail_safely():
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()), core_api=Mock(), run=successful_command
    )
    context = attempt_context()
    scope = ApplicationScope("social-network", ("social-network",))

    with pytest.raises(ProviderError, match="readiness assurance"):
        provider.wait_until_queryable(context, scope)
    with pytest.raises(ProviderError, match="delivery assurance"):
        provider.finish_attempt(context, scope)
    provider.close()
    with pytest.raises(ProviderError, match="already closed"):
        provider.prepare_attempt(context)
