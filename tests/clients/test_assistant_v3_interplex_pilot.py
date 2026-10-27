"""Interplex pilot is explicit and cannot silently become a baseline run."""

import json
from dataclasses import replace

import httpx
import pytest
from test_assistant_v3_client import complete_stream, config, streaming_response

from clients.assistant_v3.client import (
    INTERPLEX_CAPABILITY_PROFILE,
    AssistantV3Client,
    AssistantV3Error,
)
from clients.assistant_v3.driver import ArtifactError, ConductorClient, DriverRunConfig, execute_assistant_attempt

PROFILE = INTERPLEX_CAPABILITY_PROFILE
SCOPE = {"cluster": "sregym-kind-local", "namespace": "shop"}
RUN_ID = "anon_0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize(
    "profile,tool,args,accepted",
    [
        (None, "k8s_get_pod", {"clusterName": SCOPE["cluster"], "namespace": "shop"}, False),
        (PROFILE, "k8s_get_pod", {"clusterName": SCOPE["cluster"], "namespace": "shop"}, True),
        (PROFILE, "k8s_delete_pod", {"clusterName": SCOPE["cluster"], "namespace": "shop"}, False),
        (PROFILE, "kubectl", {"clusterName": SCOPE["cluster"]}, False),
        (PROFILE, "k8s_get_pod", {"clusterName": "other", "namespace": "shop"}, False),
        (PROFILE, "k8s_get_pod", {"clusterName": SCOPE["cluster"], "namespace": "chaos-mesh"}, False),
        (PROFILE, "k8s_get_pod", {}, False),
    ],
)
def test_known_reads_require_opt_in_and_matching_requested_scope(profile, tool, args, accepted):
    action = {"status": "running", "tool_name": tool, "input": args}
    stream = ("event: assistant.subagent.action\ndata: " + json.dumps(action) + "\n\n").encode()
    cfg = config(**({"capability_profile": profile} if profile else {}))
    client = AssistantV3Client(
        cfg,
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _: streaming_response(stream, complete_stream()))
        ),
    )
    if accepted:
        assert (
            client.run_session(prompt="Investigate", request_id="test", incident_scope=SCOPE).final_text == "diagnosis"
        )
    else:
        with pytest.raises(AssistantV3Error) as error:
            client.run_session(prompt="Investigate", request_id="test", incident_scope=SCOPE)
        assert error.value.kind == "capability_policy_violation"


def test_pilot_declares_public_scope_and_records_distinct_metadata(tmp_path):
    requests = []

    def assistant_handler(request):
        requests.append(json.loads(request.content))
        return streaming_response(complete_stream())

    def conductor_handler(request):
        if request.url.path == "/status":
            return httpx.Response(200, json={"stage": "diagnosis"})
        if request.url.path == "/get_app":
            return httpx.Response(200, json={"app_name": "Shop", "descriptions": "Shop", "namespaces": ["shop"]})
        return httpx.Response(200, json={"status": "success"})

    cfg = DriverRunConfig(
        RUN_ID,
        1,
        tmp_path,
        "svelte",
        False,
        "judge",
        "api",
        "splunk",
        None,
        cluster_name=SCOPE["cluster"],
        capability_profile=PROFILE,
    )
    execute_assistant_attempt(
        cfg,
        conductor=ConductorClient(
            "https://conductor.test", http_client=httpx.Client(transport=httpx.MockTransport(conductor_handler))
        ),
        assistant=AssistantV3Client(
            config(capability_profile=PROFILE),
            http_client=httpx.Client(transport=httpx.MockTransport(assistant_handler)),
        ),
    )
    metadata = json.loads((tmp_path / "run_metadata.json").read_text())
    assert metadata["capability_profile"] == PROFILE
    assert metadata["comparable"] is False
    assert '"cluster": "sregym-kind-local"' in requests[0]["action_instructions"]
    assert "Live Kubernetes observations" in requests[0]["action_instructions"]
    assert RUN_ID not in requests[0]["prompt"]
    with pytest.raises(ArtifactError, match="comparable=false"):
        replace(cfg, comparable=True).validate()
    with pytest.raises(ArtifactError, match="stable cluster"):
        replace(cfg, cluster_name=None).validate()


def test_pilot_trace_omits_reasoning_events():
    hidden = b'event: message.thinking\ndata: {"text":"private reasoning"}\n\n'
    client = AssistantV3Client(
        config(capability_profile=PROFILE),
        http_client=httpx.Client(
            transport=httpx.MockTransport(lambda _: streaming_response(hidden, complete_stream()))
        ),
    )
    result = client.run_session(prompt="Investigate", request_id="test", incident_scope=SCOPE)
    assert "private reasoning" not in str(result.events)
    assert result.final_text == "diagnosis"
