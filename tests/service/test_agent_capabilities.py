import asyncio
import subprocess
from unittest.mock import Mock

import pytest

from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import AgentRegistration
from sregym.service.container_runner import ContainerConfig, ContainerRunner
from sregym.service.internet_policy import EndpointRule, InternetPolicy


def write_kubeconfig(path):
    path.write_text(
        """apiVersion: v1
clusters:
  - cluster:
      server: https://127.0.0.1:16443
    name: sregym
contexts: []
users: []
"""
    )


def test_container_capabilities_default_to_existing_access(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    write_kubeconfig(kubeconfig)
    runner = ContainerRunner(
        ContainerConfig(
            kubeconfig_path=kubeconfig,
            env_vars={"MCP_SERVER_PORT": "19954"},
            internet_policy=InternetPolicy.from_mode("open"),
        )
    )

    env = runner._build_env_vars()
    rules = runner._configured_egress_rules(env)
    args = runner._build_base_docker_args()

    assert runner.config.kubernetes_access is True
    assert runner.config.sregym_mcp_access is True
    assert env["MCP_SERVER_URL"] == "http://host.docker.internal:19954"
    assert EndpointRule("host.docker.internal", 16443, inspect_tools=False) in rules
    assert EndpointRule("host.docker.internal", 19954, inspect_tools=False) in rules
    assert f"{kubeconfig.resolve()}:/root/.kube/config:ro" in args
    assert "KUBECONFIG=/root/.kube/config" in args


def test_restricted_container_omits_kubernetes_and_mcp_inputs(tmp_path):
    kubeconfig = tmp_path / "kubeconfig"
    write_kubeconfig(kubeconfig)
    runner = ContainerRunner(
        ContainerConfig(
            kubeconfig_path=kubeconfig,
            env_vars={
                "KUBECONFIG": "/sensitive/kubeconfig",
                "MCP_SERVER_PORT": "19954",
                "MCP_SERVER_URL": "http://sensitive-mcp:19954",
            },
            internet_policy=InternetPolicy.from_mode("open"),
            kubernetes_access=False,
            sregym_mcp_access=False,
        )
    )

    env = runner._build_env_vars(
        {
            "KUBECONFIG": "/override/kubeconfig",
            "MCP_SERVER_PORT": "29954",
            "MCP_SERVER_URL": "http://override-mcp:29954",
        }
    )
    rules = runner._configured_egress_rules(env)
    args = runner._build_base_docker_args()

    assert "KUBECONFIG" not in env
    assert "MCP_SERVER_PORT" not in env
    assert "MCP_SERVER_URL" not in env
    assert not any(rule.port in {16443, 19954, 29954} for rule in rules)
    assert not any("/root/.kube/config" in argument for argument in args)


def test_launcher_applies_restrictions_to_preflight_and_execution(tmp_path, monkeypatch):
    kubeconfig = tmp_path / "kubeconfig"
    write_kubeconfig(kubeconfig)
    runner = ContainerRunner(
        ContainerConfig(
            kubeconfig_path=kubeconfig,
            env_vars={"MCP_SERVER_PORT": "19954"},
            internet_policy=InternetPolicy.from_mode("filtered", agent_name="stratus", model_id="gpt-5"),
        )
    )
    launcher = AgentLauncher()
    launcher._agent_kubeconfig_path = str(kubeconfig)
    launcher._container_runner = runner
    registration = AgentRegistration(
        name="stratus",
        kickoff_command="run-agent",
        kickoff_env={"MCP_SERVER_PORT": "29954"},
        kubernetes_access=False,
        sregym_mcp_access=False,
    )
    run_sync = Mock(return_value=subprocess.CompletedProcess([], 0))
    run_async = Mock(return_value=Mock())
    monkeypatch.setattr(runner, "run_sync", run_sync)
    monkeypatch.setattr(runner, "run_async", run_async)
    monkeypatch.setattr(launcher, "_pipe_logs", Mock())
    monkeypatch.setattr("sregym.agent_launcher.importlib.import_module", Mock(return_value=Mock(run_preflight=Mock())))

    launcher._run_preflight(registration)
    asyncio.run(launcher._start_containerized(registration))

    preflight = run_sync.call_args.args[0]
    execution = run_async.call_args.args[0]
    assert runner.config.kubeconfig_path is None
    assert runner.config.kubernetes_access is False
    assert runner.config.sregym_mcp_access is False
    for request in (preflight, execution):
        env = runner._build_env_vars(request.env)
        rules = runner._configured_egress_rules(env)
        assert "KUBECONFIG" not in env
        assert "MCP_SERVER_PORT" not in env
        assert "MCP_SERVER_URL" not in env
        assert not any(rule.port in {16443, 19954, 29954} for rule in rules)


def test_restricted_capabilities_require_container_isolation():
    launcher = AgentLauncher()
    registration = AgentRegistration(
        name="unsafe-local-agent",
        kickoff_command="true",
        container_isolation=False,
        kubernetes_access=False,
        sregym_mcp_access=False,
    )

    with pytest.raises(RuntimeError, match="capability restrictions require container isolation"):
        asyncio.run(launcher.ensure_started(registration))


def test_filtered_assistant_allows_only_product_and_conductor_endpoints():
    runner = ContainerRunner(
        ContainerConfig(
            internet_policy=InternetPolicy.from_mode(
                "filtered",
                agent_name="assistant_v3",
                model_id="gpt-5.6-luna",
            ),
            kubernetes_access=False,
            sregym_mcp_access=False,
        )
    )
    environment = {
        "API_PORT": "8000",
        "ASSISTANT_V3_URL": "https://assistant.example.test",
    }

    rules = runner._configured_egress_rules(environment)

    assert set(rules) == {
        EndpointRule("host.docker.internal", 8000, inspect_tools=False),
        EndpointRule("assistant.example.test", 443),
    }


def test_filtered_assistant_requires_explicit_product_endpoint():
    runner = ContainerRunner(
        ContainerConfig(
            internet_policy=InternetPolicy.from_mode(
                "filtered",
                agent_name="assistant_v3",
                model_id="gpt-5.6-luna",
            ),
            kubernetes_access=False,
            sregym_mcp_access=False,
        )
    )

    with pytest.raises(ValueError, match="ASSISTANT_V3_URL"):
        runner._configured_egress_rules({"API_PORT": "8000"})


def test_capability_configuration_without_container_runner_is_safe(monkeypatch):
    launcher = AgentLauncher()
    registration = AgentRegistration(name="stratus", kickoff_command="run-agent")

    launcher._apply_agent_capabilities(registration)
    monkeypatch.setattr("sregym.agent_launcher.importlib.import_module", Mock(return_value=Mock(run_preflight=Mock())))
    with pytest.raises(RuntimeError, match="container runner is required"):
        launcher._run_preflight(registration)
