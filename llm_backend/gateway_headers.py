"""Optional tenant headers for an OpenAI-compatible LLM gateway."""

import os
from collections.abc import Mapping

ORG_ENV = "SREGYM_LLM_GATEWAY_ORG_ID"
SERVICE_ENV = "SREGYM_LLM_GATEWAY_SERVICE_NAME"


def gateway_headers(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    org = source.get(ORG_ENV, "").strip()
    service = source.get(SERVICE_ENV, "").strip()
    if bool(org) != bool(service):
        raise ValueError(f"Set both {ORG_ENV} and {SERVICE_ENV}, or neither")
    return {"X-Org-ID": org, "X-Service-Name": service} if org else {}
