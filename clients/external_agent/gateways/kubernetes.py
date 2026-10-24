"""Bounded, namespace-scoped Kubernetes HTTP evidence endpoint.

The public tunnel terminates TLS at Cloudflare. This server binds only to
loopback; its direct Kubernetes connection verifies the cluster CA and server.
It is NOT started by importing this module. It never changes cluster RBAC.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import ssl
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from ..credentials import kube_identity, read_current_bearer
from ..evidence import ExtraDenied, projected_read

DNS = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\Z")
NAME = re.compile(r"[a-z0-9](?:[-.a-z0-9]*[a-z0-9])?\Z")
RESOURCES = {
    ("apis", "rbac.authorization.k8s.io", "v1"): {"clusterroles", "clusterrolebindings", "roles", "rolebindings"},
    ("apis", "admissionregistration.k8s.io", "v1"): {"mutatingwebhookconfigurations"},
    ("api", "v1"): {
        "pods",
        "services",
        "endpoints",
        "events",
        "configmaps",
        "persistentvolumeclaims",
        "replicationcontrollers",
        "resourcequotas",
        "limitranges",
        "serviceaccounts",
    },
    ("apis", "apps", "v1"): {"deployments", "replicasets", "statefulsets", "daemonsets"},
    ("apis", "batch", "v1"): {"jobs", "cronjobs"},
    ("apis", "networking.k8s.io", "v1"): {"networkpolicies", "ingresses"},
    ("apis", "events.k8s.io", "v1"): {"events"},
    ("apis", "discovery.k8s.io", "v1"): {"endpointslices"},
    ("apis", "autoscaling", "v1"): {"horizontalpodautoscalers"},
    ("apis", "autoscaling", "v2"): {"horizontalpodautoscalers"},
    ("apis", "policy", "v1"): {"poddisruptionbudgets"},
}
HIDDEN_LABELS = {
    "app": {"load-generator", "locust-fetcher"},
    "job": {"workload"},
    "network-access": {"restricted"},
    "opentelemetry.io/name": {"load-generator"},
}
SAFE_QUERY = {"labelSelector", "fieldSelector", "limit", "continue", "resourceVersion", "pretty"}
LOG_QUERY = {"container", "previous", "timestamps", "tailLines", "limitBytes", "sinceSeconds"}
MAX_BODY = 8 * 1024 * 1024
REVIEWS = {
    "/apis/authentication.k8s.io/v1/selfsubjectreviews": "SelfSubjectReview",
    "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews": "SelfSubjectAccessReview",
    "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews": "SelfSubjectRulesReview",
}
RBAC_KINDS = {
    "clusterrolebindings": "ClusterRoleBinding",
    "clusterroles": "ClusterRole",
    "rolebindings": "RoleBinding",
    "roles": "Role",
}


def rbac_target(path: str, namespace: str):
    """Project collections onto the case's actual four RBAC objects only."""
    prefix = "/apis/rbac.authorization.k8s.io/v1/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix) :].split("/")
    if rest[:1] == ["namespaces"]:
        if len(rest) not in {3, 4} or rest[1] != namespace:
            raise Denied("Foreign RBAC namespace")
        rest = rest[2:]
        if rest[0] not in {"roles", "rolebindings"}:
            raise Denied("Invalid namespaced RBAC resource")
    if len(rest) not in {1, 2} or rest[0] not in RBAC_KINDS:
        raise Denied("Invalid RBAC resource")
    resource = rest[0]
    name = namespace + "-namespace" if resource.startswith("cluster") else "external-readonly"
    if len(rest) == 2 and rest[1] != name:
        raise Denied("RBAC object outside fixture")
    own = prefix + ("" if resource.startswith("cluster") else "namespaces/" + namespace + "/") + resource + "/" + name
    return own, RBAC_KINDS[resource], len(rest) == 1


def rbac_identity(obj, expected):
    policy = {k: obj[k] for k in ("rules", "subjects", "roleRef") if k in obj}
    if (
        obj.get("metadata", {}).get("uid") != expected["uid"]
        or hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest() != expected["rbac_policy_sha256"]
    ):
        raise Denied("Fixture RBAC identity or policy changed")


class Denied(ValueError):
    pass


def validate_request(method: str, target: str, namespace: str) -> tuple[str, str | None]:
    """Return normalized target and resource, or reject before upstream I/O."""
    if not DNS.fullmatch(namespace) or len(namespace) > 63:
        raise Denied("Invalid scope")
    u = urlsplit(target)
    if u.scheme or u.netloc or u.fragment or "%" in u.path or "//" in u.path or "\\" in u.path:
        raise Denied("Invalid path")
    parts = tuple(u.path.strip("/").split("/"))
    if not u.path.startswith("/") or any(p in {"", ".", ".."} for p in parts):
        raise Denied("Invalid path")
    pairs = parse_qsl(u.query, keep_blank_values=True, strict_parsing=True)
    q = dict(pairs)
    if len(q) != len(pairs):
        raise Denied("Duplicate query parameters")
    if method == "POST" and u.path in REVIEWS and not q:
        return u.path, "selfreview"
    if method != "GET":
        raise Denied("Only finite reads and non-persistent self reviews are enabled")
    # kubectl adds a client request timeout to ordinary discovery and GETs.
    # Accept only a bounded value, then strip it: upstream reads keep this
    # bridge's own fixed timeout and cannot become streaming requests.
    if "timeout" in q:
        value = q.pop("timeout")
        match = re.fullmatch(r"([1-9][0-9]?)s", value)
        if not match or int(match.group(1)) > 60:
            raise Denied("Client timeout outside bounds")
    if "timeoutSeconds" in q:
        value = q.pop("timeoutSeconds")
        if not value.isdigit() or not 1 <= int(value) <= 30:
            raise Denied("Client timeout outside bounds")
    if "follow" in q:
        if not u.path.endswith("/log") or q.pop("follow") != "false":
            raise Denied("Streaming logs are disabled")
    if u.path.startswith("/apis/rbac.authorization.k8s.io/"):
        if q:
            raise Denied("RBAC query options disabled")
        if rbac_target(u.path, namespace) is None:
            raise Denied("Unsupported RBAC version")
        return u.path, "owned_rbac"
    discovery = parts in {("version",), ("api",), ("apis",), ("api", "v1")} or parts in RESOURCES
    if len(parts) == 2 and parts[0] == "apis" and parts[1] in {key[1] for key in RESOURCES if key[0] == "apis"}:
        discovery = True
    if discovery:
        if q:
            raise Denied("Discovery query parameters are disabled")
        return u.path, None
    if parts in {("api", "v1", "namespaces"), ("api", "v1", "namespaces", namespace)}:
        if q and (
            parts != ("api", "v1", "namespaces")
            or set(q) != {"limit"}
            or not q["limit"].isdigit()
            or not 1 <= int(q["limit"]) <= 500
        ):
            raise Denied("Namespace query parameters are disabled")
        return u.path + ("?" + urlencode(q) if q else ""), "namespaces"
    prefix = parts[:2] if parts[0] == "api" else parts[:3]
    if prefix not in RESOURCES:
        raise Denied("API group outside scope")
    rest = parts[len(prefix) :]
    output_path = u.path
    # Clients commonly start with an all-namespaces collection. The exposed
    # API is a namespace-scoped view: translate the collection to the exact
    # own namespace before upstream I/O. The SA still cannot list globally.
    if len(rest) == 1 and rest[0] in RESOURCES[prefix]:
        rest = ("namespaces", namespace, rest[0])
        output_path = "/" + "/".join(prefix + rest)
    if len(rest) not in {3, 4, 5} or rest[:2] != ("namespaces", namespace):
        raise Denied("Namespace outside scope")
    resource = rest[2]
    if resource not in RESOURCES[prefix] or (len(rest) >= 4 and not NAME.fullmatch(rest[3])):
        raise Denied("Resource outside scope")
    if len(rest) == 5:
        if resource != "pods" or rest[4] != "log" or not q.keys() <= LOG_QUERY:
            raise Denied("Subresource outside scope")
        for k, maximum, default in [
            ("tailLines", 2000, "1000"),
            ("limitBytes", 262144, "131072"),
            ("sinceSeconds", 3600, "3600"),
        ]:
            q.setdefault(k, default)
            if not q[k].isdigit() or not 1 <= int(q[k]) <= maximum:
                raise Denied("Log bounds exceeded")
        for key in {"previous", "timestamps"} & q.keys():
            if q[key] not in {"true", "false"}:
                raise Denied("Invalid boolean")
    else:
        if not q.keys() <= SAFE_QUERY:
            raise Denied("Streaming or unsupported query parameters are disabled")
        q.setdefault("limit", "200") if len(rest) == 3 else None
        if "limit" in q and (not q["limit"].isdigit() or not 1 <= int(q["limit"]) <= 500):
            raise Denied("List bounds exceeded")
    return output_path + ("?" + urlencode(q) if q else ""), resource


def review_body(path: str, raw: bytes, namespace: str) -> dict:
    """Validate non-persistent self-introspection; no impersonation or grants."""
    if path not in REVIEWS or len(raw) > 4096:
        raise Denied("Invalid self review") from None
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeError):
        raise Denied("Invalid self review") from None
    kind = REVIEWS[path]
    version = "/".join(path.split("/")[2:4])
    if not isinstance(obj, dict) or not obj.keys() <= {"apiVersion", "kind", "metadata", "spec"}:
        raise Denied("Unsupported self review fields")
    if (
        obj.get("apiVersion", version) != version
        or obj.get("kind", kind) != kind
        or obj.get("metadata") not in (None, {})
    ):
        raise Denied("Invalid self review identity")
    spec = obj.get("spec") or {}
    if not isinstance(spec, dict):
        raise Denied("Invalid self review spec")
    if kind == "SelfSubjectReview":
        if spec:
            raise Denied("Identity review has no configurable fields")
    elif kind == "SelfSubjectRulesReview":
        if spec != {"namespace": namespace}:
            raise Denied("Rules review outside namespace scope")
    else:
        if set(spec) != {"resourceAttributes"} or not isinstance(spec["resourceAttributes"], dict):
            raise Denied("Only resource permission checks are enabled")
        attrs = spec["resourceAttributes"]
        fields = {"namespace", "verb", "group", "version", "resource", "subresource", "name"}
        if not attrs.keys() <= fields or not {"verb", "resource"} <= attrs.keys():
            raise Denied("Unsupported permission check")
        if any(not isinstance(v, str) or len(v) > 253 or any(ord(c) < 32 for c in v) for v in attrs.values()):
            raise Denied("Invalid permission check values")
        # This asks only whether the fixed dedicated SA could act. It never
        # executes the requested verb, reads that resource or changes RBAC.
    return {"apiVersion": version, "kind": kind, "spec": spec}


def visible(obj: dict, namespace: str) -> bool:
    meta = obj.get("metadata") or {}
    return meta.get("namespace", namespace) == namespace and not any(
        meta.get("labels", {}).get(k) in values for k, values in HIDDEN_LABELS.items()
    )


def filter_body(obj: dict, namespace: str, resource: str | None) -> dict:
    if resource == "namespaces":
        if "items" in obj:
            return {**obj, "items": [x for x in obj["items"] if x.get("metadata", {}).get("name") == namespace]}
        if obj.get("metadata", {}).get("name") != namespace:
            raise Denied("Namespace outside scope")
        return obj
    if resource is None:
        if obj.get("kind") == "APIGroupList":
            groups = {k[1] for k in RESOURCES if k[0] == "apis"}
            return {**obj, "groups": [g for g in obj.get("groups", []) if g.get("name") in groups]}
        if obj.get("kind") == "APIResourceList":
            key = (
                ("api", obj.get("groupVersion"))
                if "/" not in obj.get("groupVersion", "")
                else ("apis", *obj["groupVersion"].split("/"))
            )
            allowed = RESOURCES.get(key, set()) | ({"namespaces"} if key == ("api", "v1") else set())
            return {
                **obj,
                "resources": [
                    r for r in obj.get("resources", []) if r.get("name") in allowed or r.get("name") == "pods/log"
                ],
            }
        return obj
    if "items" in obj:
        return {**obj, "items": [x for x in obj["items"] if visible(x, namespace)]}
    if not visible(obj, namespace):
        raise Denied("Object outside scope")
    return obj


def secret_file(path: Path) -> str:
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) & 0o077:
        raise ValueError("Credential must be an owned regular file with mode 0600")
    value = path.read_text().strip()
    if len(value) < 32 or any(c.isspace() for c in value):
        raise ValueError("Invalid credential file")
    return value


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--namespace", required=True)
    p.add_argument("--scope-file", type=Path)
    p.add_argument("--namespace-uid", required=True)
    p.add_argument("--fixture-receipt", type=Path, required=True)
    p.add_argument("--token-file", type=Path, required=True)
    p.add_argument("--audit", type=Path, required=True)
    p.add_argument("--context", default="kind-kind")
    p.add_argument(
        "--kubeconfig",
        type=Path,
        required=True,
        help="Private service-account kubeconfig; admin client certificates are refused",
    )
    p.add_argument("--port", type=int, default=18984)
    p.add_argument("--duration", type=int, default=2700)
    args = p.parse_args()
    if not 0 <= args.duration <= 3600 or not DNS.fullmatch(args.namespace):
        p.error("Invalid lifetime or namespace")
    token = secret_file(args.token_file)
    fixture = json.loads(args.fixture_receipt.read_text())
    if (
        fixture.get("namespace") != args.namespace
        or fixture.get("status") != "case_ready"
        or fixture.get("cleanup_complete")
    ):
        raise ValueError("Case binding is not current")
    # Extra evidence may add another Role/RoleBinding in kube-system. The
    # projected permission collections must use only the four application
    # access objects, independent of their position in the captured receipt.
    rbac_expected = {
        x["kind"]: x
        for x in fixture["objects"]
        if x["kind"] in RBAC_KINDS.values()
        and x.get("name") == (args.namespace + "-namespace" if x["kind"].startswith("Cluster") else "external-readonly")
        and (
            x.get("namespace") is None
            if x["kind"].startswith("Cluster")
            else x.get("namespace") in (None, args.namespace)
        )
    }
    if len(rbac_expected) != 4 or any(not x.get("rbac_policy_sha256") for x in rbac_expected.values()):
        raise ValueError("Complete own RBAC identities are required")
    import httpx
    from kubernetes import config

    pinned_kube_identity = kube_identity(args.kubeconfig)
    api = config.new_client_from_config(config_file=str(args.kubeconfig), context=args.context, persist_config=False)
    cfg = api.configuration
    if not cfg.verify_ssl or not cfg.ssl_ca_cert:
        raise ValueError("Verified Kubernetes TLS is required")
    if cfg.cert_file or cfg.key_file or not cfg.get_api_key_with_prefix("authorization"):
        raise ValueError("A dedicated service-account bearer identity is required")
    ctx = ssl.create_default_context(cafile=cfg.ssl_ca_cert)
    if cfg.cert_file and cfg.key_file:
        ctx.load_cert_chain(cfg.cert_file, cfg.key_file)
    auth = cfg.get_api_key_with_prefix("authorization")
    headers = {"Accept": "application/json"}
    if auth:
        headers["Authorization"] = auth
    client = httpx.Client(
        base_url=cfg.host, verify=ctx, headers=headers, trust_env=False, follow_redirects=False, timeout=15
    )
    expires = time.monotonic() + args.duration if args.duration else float("inf")
    audit_lock = threading.Lock()

    def request(method: str, path: str, payload=None):
        with client.stream(
            method,
            path,
            json=payload,
            headers={"Authorization": read_current_bearer(args.kubeconfig, pinned_kube_identity)},
        ) as response:
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise Denied("Response exceeded limit")
            return response.status_code, response.headers.get("content-type", "application/json"), bytes(body)

    def get(path: str):
        return request("GET", path)

    def guard():
        status, _, body = get("/api/v1/namespaces/" + args.namespace)
        if status != 200 or json.loads(body).get("metadata", {}).get("uid") != args.namespace_uid:
            raise Denied("Namespace identity changed or is unavailable")

    guard()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def send(self, status, body, content_type="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            with audit_lock, args.audit.open("a") as f:
                parts = urlsplit(self.path).path.strip("/").split("/")
                group = (
                    "namespaced"
                    if "namespaces" in parts and len(parts) > parts.index("namespaces") + 2
                    else "discovery_or_cluster"
                )
                known = (
                    {r for resources in RESOURCES.values() for r in resources}
                    | {"nodes", "namespaces", "secrets", "log", "version"}
                    | set(RBAC_KINDS)
                    | {path.rsplit("/", 1)[-1] for path in REVIEWS}
                )
                resource = next((part for part in reversed(parts) if part in known), "other")
                f.write(
                    json.dumps(
                        {
                            "at": time.time(),
                            "method": self.command,
                            "path_sha256": hashlib.sha256(self.path.encode()).hexdigest(),
                            "route_class": group,
                            "resource": resource,
                            "status": status,
                            "bytes": len(body),
                            "tunnel_header_present": bool(self.headers.get("Cf-Ray")),
                        }
                    )
                    + "\n"
                )

        def handle_request(self):
            status, body, ctype = 503, b'{"error":"upstream unavailable"}', "application/json"
            try:
                supplied = self.headers.get("Authorization", "")
                if not secrets.compare_digest(supplied, "Bearer " + token):
                    self.send(401, b'{"error":"authentication required"}')
                    return
                if time.monotonic() >= expires:
                    self.send(410, b'{"error":"pilot access expired"}')
                    return
                if args.scope_file and args.scope_file.exists():
                    scope = json.loads(args.scope_file.read_text())
                    guard()
                    extra = projected_read(self.command, self.path, scope, list(rbac_expected.values()), get)
                    if extra is not None:
                        guard()
                        self.send(extra[0], extra[2], extra[1])
                        return
                path, resource = validate_request(self.command, self.path, args.namespace)
                guard()
                if resource == "owned_rbac":
                    own_path, kind, is_list = rbac_target(path, args.namespace)
                    status, ctype, body = get(own_path)
                    if status != 200:
                        raise Denied("Owned RBAC read failed")
                    obj = json.loads(body)
                    rbac_identity(obj, rbac_expected[kind])
                    guard()
                    if is_list:
                        body = json.dumps(
                            {
                                "apiVersion": "rbac.authorization.k8s.io/v1",
                                "kind": kind + "List",
                                "metadata": {},
                                "items": [obj],
                            }
                        ).encode()
                    self.send(200, body, ctype)
                    return
                if resource == "selfreview":
                    length = self.headers.get("Content-Length", "")
                    if self.headers.get("Transfer-Encoding") or not length.isdigit() or not 1 <= int(length) <= 4096:
                        raise Denied("Self review body bounds exceeded")
                    payload = review_body(path, self.rfile.read(int(length)), args.namespace)
                    status, ctype, body = request("POST", path, payload)
                    guard()
                    if status not in {200, 201}:
                        body = json.dumps({"kind": "Status", "status": "Failure", "code": status}).encode()
                    self.send(status, body, ctype)
                    return
                if urlsplit(path).path.endswith("/log"):
                    obj_status, _, pod_body = get(urlsplit(path).path.removesuffix("/log"))
                    if obj_status != 200 or not visible(json.loads(pod_body), args.namespace):
                        raise Denied("Pod outside scope")
                if urlsplit(path).path == "/api/v1/namespaces":
                    status, ctype, own_namespace = get("/api/v1/namespaces/" + args.namespace)
                    body = (
                        json.dumps(
                            {
                                "apiVersion": "v1",
                                "kind": "NamespaceList",
                                "metadata": {},
                                "items": [json.loads(own_namespace)],
                            }
                        ).encode()
                        if status == 200
                        else own_namespace
                    )
                else:
                    status, ctype, body = get(path)
                if status == 200 and "json" in ctype:
                    body = json.dumps(filter_body(json.loads(body), args.namespace, resource)).encode()
                elif status == 200 and not urlsplit(path).path.endswith("/log"):
                    raise Denied("Unexpected response format")
                elif status != 200:
                    body, ctype = (
                        json.dumps({"kind": "Status", "status": "Failure", "code": status}).encode(),
                        "application/json",
                    )
                guard()
            except (Denied, ExtraDenied, ValueError):
                status, body, ctype = 403, b'{"error":"request outside pilot scope"}', "application/json"
            except Exception:
                status, body, ctype = 503, b'{"error":"upstream unavailable"}', "application/json"
            self.send(status, body, ctype)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)

    def expire():
        time.sleep(args.duration)
        server.shutdown()

    if args.duration:
        threading.Thread(target=expire, daemon=True).start()
    print(json.dumps({"state": "loopback_ready", "port": args.port, "duration_seconds": args.duration}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        client.close()
        api.close()


if __name__ == "__main__":
    main()
