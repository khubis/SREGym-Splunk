from llm_backend.init_backend import get_llm_backend_for_judge


def test_gpt5_judge_omits_unsupported_sampling_parameters() -> None:
    backend = get_llm_backend_for_judge(model_name="azure/gpt-5.6-luna")

    assert backend.temperature is None
    assert backend.top_p is None


def test_other_judge_retains_existing_sampling_parameters() -> None:
    backend = get_llm_backend_for_judge(model_name="azure/gpt-4o")

    assert backend.temperature == 0.0
    assert backend.top_p == 0.95


def test_gateway_judge_sends_tenant_headers(monkeypatch) -> None:
    monkeypatch.setenv("SREGYM_LLM_GATEWAY_ORG_ID", "org")
    monkeypatch.setenv("SREGYM_LLM_GATEWAY_SERVICE_NAME", "sregym")

    backend = get_llm_backend_for_judge(model_name="openai/gpt-5.6-luna")

    assert backend.extra_headers == {"X-Org-ID": "org", "X-Service-Name": "sregym"}


def test_non_gateway_judge_does_not_receive_gateway_headers(monkeypatch) -> None:
    monkeypatch.setenv("SREGYM_LLM_GATEWAY_ORG_ID", "org")
    monkeypatch.setenv("SREGYM_LLM_GATEWAY_SERVICE_NAME", "sregym")

    backend = get_llm_backend_for_judge(model_name="azure/gpt-5.6-luna")

    assert backend.extra_headers == {}
