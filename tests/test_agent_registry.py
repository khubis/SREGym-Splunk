from pathlib import Path

import yaml

from sregym.agent_registry import AgentRegistration, get_agent, list_agents, save_agent


def test_legacy_registrations_default_to_existing_capabilities(tmp_path):
    registry = tmp_path / "agents.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "agents": [
                    {
                        "name": "legacy",
                        "kickoff_command": "run-legacy",
                    }
                ]
            }
        )
    )

    registration = get_agent("legacy", registry)

    assert registration is not None
    assert registration.kubernetes_access is True
    assert registration.sregym_mcp_access is True


def test_capability_flags_round_trip_through_registry(tmp_path):
    registry = tmp_path / "agents.yaml"
    registration = AgentRegistration(
        name="restricted",
        kickoff_command="run-restricted",
        kickoff_env={},
        kubernetes_access=False,
        sregym_mcp_access=False,
    )

    save_agent(registration, registry)

    assert get_agent("restricted", registry) == registration


def test_assistant_registration_is_restricted_without_changing_existing_agents():
    registry = Path(__file__).resolve().parents[1] / "agents.yaml"
    agents = list_agents(registry)

    assistant = agents.pop("assistant_v3")
    assert assistant.kickoff_command == "python -m clients.assistant_v3.driver"
    assert assistant.container_isolation is True
    assert assistant.kubernetes_access is False
    assert assistant.sregym_mcp_access is False
    assert all(agent.kubernetes_access and agent.sregym_mcp_access for agent in agents.values())
