"""Observability provider selection."""

from sregym.observability.base import NullObservabilityProvider, ObservabilityProvider


def create_provider(name: str) -> ObservabilityProvider:
    """Create a provider without importing optional implementations eagerly."""
    if name == "none":
        return NullObservabilityProvider()
    if name == "splunk":
        from sregym.observability.splunk import SplunkConfig, SplunkObservabilityProvider

        return SplunkObservabilityProvider(SplunkConfig.from_env())
    raise ValueError("unsupported observability provider")


__all__ = ["create_provider"]
