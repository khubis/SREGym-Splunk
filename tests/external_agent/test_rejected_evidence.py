import hashlib
import json
from unittest.mock import Mock

import httpx
import pytest

from clients.external_agent.telemetry import wait_for_telemetry


@pytest.mark.parametrize("status,body", [(502, b""), (401, b'{"private":"secret-value"}'), (500, b"arbitrary-error")])
def test_rejected_status_is_recorded_safely_and_never_retried(status, body):
    request = httpx.Request(
        "GET",
        "https://private.example.invalid/allowed-path?token=secret-value",
        headers={"Authorization": "Bearer secret-value"},
    )
    response = httpx.Response(status, content=body, request=request)
    error = httpx.HTTPStatusError("secret-value", request=request, response=response)
    probe = Mock(side_effect=error)
    records = []
    sleep = Mock()
    with pytest.raises(httpx.HTTPStatusError) as raised:
        wait_for_telemetry(probe, records.append, clock=lambda: 0, sleep=sleep)
    assert raised.value is error and probe.call_count == 1 and not sleep.called
    assert records[-1]["phase"] == "held" and records[-1]["rejected_http_response"] == {
        "reason": "telemetry_http_status_rejected",
        "status_code": status,
        "method": "GET",
        "path_sha256": hashlib.sha256(b"/allowed-path").hexdigest(),
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }
    assert (
        "secret-value" not in json.dumps(records)
        and "private.example.invalid" not in json.dumps(records)
        and "accepted" not in records[-1]
    )
