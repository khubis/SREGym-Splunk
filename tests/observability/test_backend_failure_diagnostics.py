import json

import httpx
import pytest
from test_splunk import NOW, attempt_context, valid_environment

from sregym.observability.splunk import SplunkBackendError, SplunkConfig, SplunkHttpBackend


@pytest.mark.parametrize("kind", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.ReadError])
@pytest.mark.parametrize("operation", ["getConnections", "signalflow_execute"])
def test_transport_failure_identifies_operation_and_subtype_without_request_data(kind, operation):
    private = "private-credential private-query private-host"

    def fail(request):
        raise kind(private, request=request)

    backend = SplunkHttpBackend(
        SplunkConfig.from_env(valid_environment()),
        http_client=httpx.Client(transport=httpx.MockTransport(fail)),
    )
    with pytest.raises(SplunkBackendError) as caught:
        if operation == "getConnections":
            backend.resolve_logs_connection(2.5)
        else:
            backend.collector_snapshot(attempt_context(), NOW.replace(minute=10), 2.5)
    diagnostic = caught.value.diagnostic
    assert diagnostic == {
        "operation": operation,
        "error_kind": kind.__name__,
        "request_timeout_seconds": 2.5,
    }
    assert caught.value.status_code is None
    assert caught.value.transient is True
    assert private not in json.dumps(diagnostic)
    assert private not in str(caught.value)


def test_backend_diagnostic_rejects_untrusted_strings():
    error = SplunkBackendError(status_code=503, transient=True)
    error.args = ("private-credential",)
    assert error.diagnostic == {}
