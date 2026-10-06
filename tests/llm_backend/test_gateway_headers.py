import pytest

from llm_backend.gateway_headers import gateway_headers


def test_gateway_headers_are_opt_in() -> None:
    assert gateway_headers({}) == {}
    assert gateway_headers({
        "SREGYM_LLM_GATEWAY_ORG_ID": "org",
        "SREGYM_LLM_GATEWAY_SERVICE_NAME": "sregym",
    }) == {"X-Org-ID": "org", "X-Service-Name": "sregym"}


def test_gateway_headers_require_both_values() -> None:
    with pytest.raises(ValueError, match="Set both"):
        gateway_headers({"SREGYM_LLM_GATEWAY_ORG_ID": "org"})
