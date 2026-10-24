"""Local lifetime and same-identity credential refresh for the retained tunnel.

No automatic investigation retry, new RBAC grants, or shared service changes.
The bridge only reloads the bearer token; endpoint/CA/context/user stay pinned.
"""

import copy
import json
import os
import stat
from datetime import UTC, datetime


def now():
    return datetime.now(UTC).isoformat()


def private_config(path):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) & 0o077:
        raise ValueError("Private owned service-account configuration required")
    raw = path.read_bytes()
    if len(raw) > 65536:
        raise ValueError("Configuration bound exceeded")
    return json.loads(raw)


def config_identity(config):
    d = copy.deepcopy(config)
    if len(d.get("clusters", [])) != 1 or len(d.get("users", [])) != 1 or len(d.get("contexts", [])) != 1:
        raise ValueError("Only one fixed Kubernetes identity is supported")
    credentials = d["users"][0]["user"]
    if set(credentials) != {"token"}:
        raise ValueError("Only a service-account token is supported")
    token = credentials["token"]
    if not isinstance(token, str) or len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("Invalid service-account token")
    cluster = d["clusters"][0]["cluster"]
    if (
        set(cluster) != {"server", "certificate-authority-data"}
        or not cluster["server"].startswith("https://")
        or not cluster["certificate-authority-data"]
    ):
        raise ValueError("Fixed verified TLS endpoint required")
    d["users"][0]["user"]["token"] = "[rotating same-identity bearer]"
    return d


def kube_identity(path):
    return config_identity(private_config(path))


def read_current_bearer(path, pinned_identity):
    cfg = private_config(path)
    if config_identity(cfg) != pinned_identity:
        raise ValueError("Kubernetes endpoint, CA, context or identity changed")
    return "Bearer " + cfg["users"][0]["user"]["token"]
