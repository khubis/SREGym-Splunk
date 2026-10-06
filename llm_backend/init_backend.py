import os

from llm_backend.get_llm_backend import LiteLLMBackend
from llm_backend.gateway_headers import gateway_headers


def get_llm_backend(
    model_name: str,
    api_base: str | None = None,
    api_key: str | None = None,
    provider: str | None = None,
    temperature: float | None = 0.0,
    top_p: float | None = 0.95,
    max_tokens: int | None = None,
    usage_available: bool = True,
    retry: bool = True,
    extra_headers: dict[str, str] | None = None,
) -> LiteLLMBackend:
    """Initialize an LLM backend for the given litellm model string."""
    endpoint_status = "set" if api_base else "unset"
    print(f"🔧 Initializing LLM backend — model: {model_name}, api_base: {endpoint_status}")
    return LiteLLMBackend(
        model_name=model_name,
        api_base=api_base,
        api_key=api_key,
        provider=provider,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        usage_available=usage_available,
        retry=retry,
        extra_headers=extra_headers,
    )


def get_llm_backend_for_agent() -> LiteLLMBackend:
    """Get LLM backend for agent tasks"""
    model_id = os.environ.get("AGENT_MODEL_ID")
    if not model_id:
        raise ValueError("AGENT_MODEL_ID environment variable is not set.")
    return get_llm_backend(
        model_id,
        api_base=os.environ.get("AGENT_API_BASE"),
        api_key=os.environ.get("AGENT_API_KEY"),
    )


def get_llm_backend_for_judge(
    *,
    provider: str | None = None,
    model_name: str | None = None,
    api_base: str | None = None,
    api_key: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
) -> LiteLLMBackend:
    """Get LLM backend for the LLM-as-a-judge evaluator."""
    model_id = model_name or os.environ.get("JUDGE_MODEL_ID")
    if not model_id:
        raise ValueError("A judge model must be passed or set in JUDGE_MODEL_ID.")
    if bridge_url := os.environ.get("SREGYM_JUDGE_BRIDGE_URL"):
        # The selected CLI owns inference, even for Claude/native model names or
        # explicit oracle provider settings. Never fall through to API billing.
        provider, api_base, api_key = "openai", bridge_url, "dummy"
    # GPT-5 deployments reject non-default temperature/top_p; omit both rather
    # than sending the older judge defaults (0.0 and 0.95).
    default_sampling_only = model_id.rsplit("/", 1)[-1].startswith("gpt-5.")
    return get_llm_backend(
        model_id,
        api_base=api_base if api_base is not None else os.environ.get("JUDGE_API_BASE"),
        api_key=api_key if api_key is not None else os.environ.get("JUDGE_API_KEY"),
        provider=provider,
        temperature=None if default_sampling_only else temperature,
        top_p=None if default_sampling_only else 0.95,
        max_tokens=max_tokens,
        usage_available=not bool(bridge_url),
        retry=not bool(bridge_url),
        extra_headers=gateway_headers() if not bridge_url and model_id.startswith("openai/") else None,
    )
