from llm_backend.init_backend import get_llm_backend_for_judge


def test_gpt5_judge_omits_unsupported_sampling_parameters() -> None:
    backend = get_llm_backend_for_judge(model_name="azure/gpt-5.6-luna")

    assert backend.temperature is None
    assert backend.top_p is None


def test_other_judge_retains_existing_sampling_parameters() -> None:
    backend = get_llm_backend_for_judge(model_name="azure/gpt-4o")

    assert backend.temperature == 0.0
    assert backend.top_p == 0.95
