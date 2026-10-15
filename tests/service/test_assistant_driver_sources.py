from pathlib import Path

import pytest

from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import AgentRegistration
from sregym.service.container_runner import ContainerConfig, ContainerRunner
from sregym.service.internet_policy import InternetPolicy


def test_cached_image_uses_current_read_only_assistant_driver(tmp_path):
    source = tmp_path / "assistant_v3"
    source.mkdir()
    (source / "driver.py").write_text("# current driver\n")
    config = ContainerConfig(
        image="sregym-agent-base:latest",
        assistant_source_path=source,
        kubernetes_access=False,
        sregym_mcp_access=False,
        forward_host_credentials=False,
        codex_auth="none",
        internet_policy=InternetPolicy.from_mode("open"),
    )
    runner = ContainerRunner(config)
    args = runner._build_base_docker_args()
    assert f"{source.resolve()}:/opt/sregym/clients/assistant_v3:ro" in args
    assert config.image == "sregym-agent-base:latest"


def test_missing_current_driver_does_not_fall_back_to_cached_code(tmp_path):
    config = ContainerConfig(
        assistant_source_path=tmp_path / "missing",
        kubernetes_access=False,
        sregym_mcp_access=False,
        forward_host_credentials=False,
        codex_auth="none",
        internet_policy=InternetPolicy.from_mode("open"),
    )
    with pytest.raises(ValueError, match="Assistant driver source"):
        ContainerRunner(config)._build_base_docker_args()


def test_launcher_selects_source_only_for_assistant():
    launcher = AgentLauncher()
    launcher._container_runner = ContainerRunner(ContainerConfig())
    launcher.configure_agent_capabilities(AgentRegistration(name="assistant_v3"))
    source = launcher._container_runner.config.assistant_source_path
    assert source == Path(__file__).resolve().parents[2] / "clients" / "assistant_v3"
    launcher.configure_agent_capabilities(AgentRegistration(name="codex"))
    assert launcher._container_runner.config.assistant_source_path is None
