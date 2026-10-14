import json
import logging
import subprocess
from unittest.mock import Mock

import pytest
from test_splunk import ACCESS_TOKEN, HEC_TOKEN, INGEST_TOKEN, attempt_context, valid_environment

from sregym.conductor.conductor import Conductor
from sregym.observability.base import ProviderError
from sregym.observability.splunk import SplunkConfig, SplunkObservabilityProvider


@pytest.mark.parametrize(
    "output,reason",
    [
        ("Error: context deadline exceeded", "deadline_exceeded"),
        ("Error: Kubernetes cluster unreachable: dial tcp: connection refused", "connection_refused"),
        ("Error: forbidden: cannot create deployments", "permission_denied"),
        ("Error: x509: certificate signed by unknown authority", "tls_verification"),
        ("Error: another operation (install/upgrade/rollback) is in progress", "operation_in_progress"),
        ("unclassified private server detail", "unclassified"),
    ],
)
def test_failed_install_retains_exit_and_safe_reason_in_the_native_phase_ledger(tmp_path, output, reason):
    private = f"{ACCESS_TOKEN} {INGEST_TOKEN} {HEC_TOKEN} arbitrary-private-payload"
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()),
        core_api=Mock(),
        run=lambda command, stdin: subprocess.CompletedProcess(
            command, 7, stdout=private, stderr=f"{output} {private}"
        ),
    )
    conductor = Conductor.__new__(Conductor)
    conductor.logger = logging.getLogger("installer-diagnostics-test")
    conductor.problem_id = "example"
    conductor.bind_observability_attempt(provider, attempt_context())
    path = tmp_path / "phases.jsonl"
    conductor.bind_phase_ledger(path, attempt=1)
    with pytest.raises(ProviderError) as raised, conductor._phase("observability_prepare"):
        conductor.prepare_observability()
    assert raised.value.kind == "configuration"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    failure = next(row for row in rows if row["event"] == "end")
    assert failure["outcome"] == "error"
    assert "exit_code=7" in failure["error"]
    assert f"reason={reason}" in failure["error"]
    encoded = path.read_text() + str(raised.value) + repr(raised.value)
    assert all(secret not in encoded for secret in [ACCESS_TOKEN, INGEST_TOKEN, HEC_TOKEN, "arbitrary-private-payload"])
    assert "unclassified private server detail" not in encoded
    assert len(failure["error"]) < 300


@pytest.mark.parametrize(
    "error,reason",
    [
        (FileNotFoundError("private executable"), "executable_unavailable"),
        (subprocess.TimeoutExpired(["helm", HEC_TOKEN], 3, stderr=INGEST_TOKEN), "command_timeout"),
    ],
)
def test_runner_failure_retains_safe_classification_without_exception_payload(error, reason):
    provider = SplunkObservabilityProvider(
        SplunkConfig.from_env(valid_environment()), core_api=Mock(), run=Mock(side_effect=error)
    )
    with pytest.raises(ProviderError) as raised:
        provider.prepare_attempt(attempt_context())
    assert f"reason={reason}" in str(raised.value)
    assert "private executable" not in str(raised.value)
    assert INGEST_TOKEN not in str(raised.value)
    assert HEC_TOKEN not in str(raised.value)
