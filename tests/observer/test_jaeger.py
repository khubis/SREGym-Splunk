import json
from unittest.mock import Mock

from sregym.observer.jaeger.jaeger import Jaeger


def _jaeger_with_recorded_commands():
    jaeger = Jaeger.__new__(Jaeger)
    jaeger.namespace = "observe"
    jaeger.run_cmd = Mock(return_value="")
    return jaeger


def test_external_redirect_restarts_only_trace_emitters_after_services_exist():
    jaeger = _jaeger_with_recorded_commands()
    deployments = {
        "items": [
            {
                "metadata": {"name": "frontend"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {"env": [{"name": "JAEGER_SAMPLE_RATIO", "value": "1"}]}
                            ]
                        }
                    }
                },
            },
            {
                "metadata": {"name": "consul"},
                "spec": {"template": {"spec": {"containers": [{}]}}},
            },
            {
                "metadata": {"name": "payment"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "env": [
                                        {
                                            "name": "OTEL_EXPORTER_OTLP_ENDPOINT",
                                            "value": "http://otel-collector:4317",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                },
            },
        ]
    }
    jaeger.run_cmd.side_effect = lambda command: (
        json.dumps(deployments) if command.endswith("get deployment -o json") else ""
    )

    jaeger.create_external_name_service("hotel-reservation", restart_deployments=True)

    commands = [call.args[0] for call in jaeger.run_cmd.call_args_list]
    restart_index = commands.index(
        "kubectl rollout restart deployment/frontend -n hotel-reservation"
    )
    redirect_indexes = [
        index
        for index, command in enumerate(commands)
        if command.startswith("kubectl create service externalname ")
    ]
    assert redirect_indexes
    assert max(redirect_indexes) < restart_index
    assert any("io.kompose.service=jaeger" in command for command in commands[:restart_index])
    assert "kubectl rollout restart deployment/payment -n hotel-reservation" in commands
    assert not any("deployment/consul" in command for command in commands)


def test_predeploy_redirect_does_not_restart_absent_application_deployments():
    jaeger = _jaeger_with_recorded_commands()

    jaeger.create_external_name_service("train-ticket")

    commands = [call.args[0] for call in jaeger.run_cmd.call_args_list]
    assert not any(command.startswith("kubectl rollout restart deployment ") for command in commands)


def test_trace_redirect_can_preserve_app_local_fault_target_deployment():
    jaeger = _jaeger_with_recorded_commands()
    jaeger.run_cmd.side_effect = lambda command: (
        '{"items":[]}' if command.endswith("get deployment -o json") else ""
    )

    jaeger.create_external_name_service(
        "social-network", restart_deployments=True, preserve_deployments=True,
    )

    commands = [call.args[0] for call in jaeger.run_cmd.call_args_list]
    assert not any(command.startswith("kubectl delete deployment ") for command in commands)
    assert not any(command.startswith("kubectl delete statefulset ") for command in commands)
    assert "kubectl delete svc -n social-network jaeger --ignore-not-found" in commands
    assert any(
        command.startswith("kubectl create service externalname jaeger -n social-network")
        for command in commands
    )
