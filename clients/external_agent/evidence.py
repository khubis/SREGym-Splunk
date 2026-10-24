"""Project finite read-only requests onto explicitly captured resource UIDs."""

import hashlib
import json
import re
from urllib.parse import parse_qsl, urlsplit


class ExtraDenied(ValueError):
    pass


PLURALS = {
    "ClusterRole": ("rbac.authorization.k8s.io/v1", "clusterroles"),
    "ClusterRoleBinding": ("rbac.authorization.k8s.io/v1", "clusterrolebindings"),
    "MutatingWebhookConfiguration": ("admissionregistration.k8s.io/v1", "mutatingwebhookconfigurations"),
    "ConfigMap": ("v1", "configmaps"),
}


def resource_path(row):
    if row["kind"] not in PLURALS or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", row["name"]):
        raise ExtraDenied("Invalid captured resource name")
    if row.get("namespace") and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", row["namespace"]):
        raise ExtraDenied("Invalid captured namespace")
    if row["kind"] != "ConfigMap" and row.get("namespace"):
        raise ExtraDenied("Unexpected namespace on cluster resource")
    if row["kind"] == "ConfigMap" and (row.get("namespace") != "kube-system" or row["name"] != "coredns"):
        raise ExtraDenied("Unrelated extra ConfigMap")
    api, plural = PLURALS[row["kind"]]
    base = "/api/v1" if api == "v1" else "/apis/" + api
    if row.get("namespace"):
        base += "/namespaces/" + row["namespace"]
    return base + "/" + plural + "/" + row["name"]


def projected_read(method, target, scope, base_objects, get):
    u = urlsplit(target)
    if u.scheme or u.netloc or u.fragment or "%" in u.path or "//" in u.path or "\\" in u.path:
        raise ExtraDenied("Invalid request target")
    rows = scope.get("resources", [])
    if not rows:
        return None
    # The normal dedicated SA Role/ClusterRole remains part of an owned RBAC collection.
    kinds = {r["kind"] for r in rows}
    rows = rows + [r for r in base_objects if r.get("kind") in kinds and r.get("uid")]
    matching = []
    collection = False
    for row in rows:
        path = resource_path(row)
        if u.path == path:
            matching.append(row)
        elif u.path == path.rsplit("/", 1)[0]:
            matching.append(row)
            collection = True
    if not matching:
        return None
    if method != "GET":
        raise ExtraDenied("Extra evidence paths are read-only")
    q = parse_qsl(u.query, keep_blank_values=True, strict_parsing=True)
    if len(dict(q)) != len(q) or set(dict(q)) - {"limit", "pretty", "timeout", "resourceVersion"}:
        raise ExtraDenied("Unsupported or streaming extra query")
    for key, value in q:
        if key == "limit" and (not value.isdigit() or not 1 <= int(value) <= 500):
            raise ExtraDenied("List bound exceeded")
        if key == "timeout" and (not value.endswith("s") or not value[:-1].isdigit() or not 1 <= int(value[:-1]) <= 60):
            raise ExtraDenied("Timeout bound exceeded")
    values = []
    for row in matching:
        if not row.get("uid"):
            continue
        status, ctype, body = get(resource_path(row))
        if status != 200 or "json" not in ctype:
            raise ExtraDenied("Captured resource read unavailable")
        value = json.loads(body)
        if value.get("metadata", {}).get("uid") != row["uid"]:
            raise ExtraDenied("Captured resource UID changed")
        if row.get("namespace") and value["metadata"].get("namespace") != row["namespace"]:
            raise ExtraDenied("Wrong resource namespace")
        expected = row.get("policy_sha256") or row.get("rbac_policy_sha256")
        if expected:
            policy = {k: value[k] for k in ["rules", "subjects", "roleRef"] if k in value}
            if hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest() != expected:
                raise ExtraDenied("Captured policy changed")
        values.append(value)
    if not collection:
        if len(values) != 1:
            raise ExtraDenied("Named extra resource not admitted yet")
        body = values[0]
    else:
        kind = matching[0]["kind"]
        api, _ = PLURALS[kind]
        body = {"apiVersion": api, "kind": kind + "List", "metadata": {}, "items": values}
    return 200, "application/json", json.dumps(body).encode()
