"""Observability provider selection."""

from sregym.observability.base import NullObservabilityProvider, ObservabilityProvider, ProviderError


def create_provider(name: str) -> ObservabilityProvider:
    """Create a provider without importing optional implementations eagerly."""
    if name == "none":
        return NullObservabilityProvider()
    if name == "splunk":
        raise ProviderError("configuration", "the Splunk observability provider is not installed")
    raise ValueError("unsupported observability provider")


__all__ = ["create_provider"]
