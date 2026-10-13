import dataclasses
import json

import httpx
import pytest
from test_assistant_v3_client import client_for, complete_stream, streaming_response
from test_assistant_v3_destination_configuration import configuration

from clients.assistant_v3.driver import ConductorClient, execute_assistant_attempt


@pytest.mark.parametrize("provider", ["splunk", "default"])
def test_composite_application_preserves_supported_scope_or_explicit_failure(tmp_path, provider):
    assistant_calls, submissions = [], []

    def assistant_handler(request):
        assistant_calls.append(json.loads(request.content))
        return streaming_response(complete_stream())

    def conductor_handler(request):
        if request.url.path == "/status":
            return httpx.Response(200, json={"stage": "diagnosis"})
        if request.url.path == "/get_app":
            return httpx.Response(
                200, json={"app_name": "Composite", "descriptions": "Distributed app", "namespaces": ["ns-a", "ns-b"]}
            )
        submissions.append(request)
        return httpx.Response(200, json={"status": "success"})

    config = dataclasses.replace(
        configuration(tmp_path, "incident-events", "selected-connection"), observability_provider=provider
    )
    execute_assistant_attempt(
        config,
        conductor=ConductorClient(
            "https://conductor.test", http_client=httpx.Client(transport=httpx.MockTransport(conductor_handler))
        ),
        assistant=client_for(assistant_handler),
    )
    terminal = json.loads((tmp_path / "assistant_v3" / "terminal.json").read_text())
    if provider == "splunk":
        assert assistant_calls == []
        assert submissions == []
        assert terminal["outcome"] == "configuration_error"
    else:
        assert len(assistant_calls) == 1
        assert "incident_scope" not in assistant_calls[0]
        assert len(submissions) == 1
        assert terminal["outcome"] == "completed"
