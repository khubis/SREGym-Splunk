"""Exercise the real gateway process with verified-TLS loopback Kubernetes fixtures."""

import base64
import hashlib
import json
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

NS = "social-network-fixture"
PREFIX = "/apis/rbac.authorization.k8s.io/v1/"


def private(path, value):
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)
    path.chmod(0o600)


def obj(kind, name, namespace=None):
    value = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": kind,
        "metadata": {
            "name": name,
            "uid": (namespace or "cluster") + "-" + name,
            **({"namespace": namespace} if namespace else {}),
        },
    }
    if kind.endswith("Binding"):
        value.update(
            subjects=[{"kind": "ServiceAccount", "name": "external-readonly", "namespace": NS}],
            roleRef={
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "ClusterRole" if kind.startswith("Cluster") else "Role",
                "name": name,
            },
        )
    else:
        value["rules"] = [{"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}]
    return value


def record(value, extra=False):
    policy = {k: value[k] for k in ["rules", "subjects", "roleRef"] if k in value}
    return {
        "kind": value["kind"],
        "name": value["metadata"]["name"],
        "uid": value["metadata"]["uid"],
        "rbac_policy_sha256": hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
        **({"namespace": value["metadata"]["namespace"]} if extra else {}),
    }


@pytest.mark.parametrize("extras_first", [False, True])
def test_external_can_read_own_permissions_with_extra_evidence_roles(tmp_path, extras_first):
    cert, key = tmp_path / "ca.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-nodes",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-days",
            "1",
        ],
        capture_output=True,
        check=True,
        timeout=15,
    )
    key.chmod(0o600)
    values = {}
    expected = {}
    own = []
    for kind, plural in [
        ("ClusterRole", "clusterroles"),
        ("ClusterRoleBinding", "clusterrolebindings"),
        ("Role", "roles"),
        ("RoleBinding", "rolebindings"),
    ]:
        namespace = None if kind.startswith("Cluster") else NS
        name = NS + "-namespace" if namespace is None else "external-readonly"
        value = obj(kind, name, namespace)
        path = PREFIX + ("" if namespace is None else "namespaces/" + NS + "/") + plural + "/" + name
        values[path] = value
        own.append(record(value))
        expected[plural] = value["metadata"]["uid"]
    extras = [record(obj(k, "external-evidence-abc", "kube-system"), extra=True) for k in ["Role", "RoleBinding"]]
    observed = []

    class Kubernetes(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            observed.append(self.path)
            assert self.headers["Authorization"] == "Bearer " + "s" * 48
            if self.path == "/api/v1/namespaces/" + NS:
                body = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS, "uid": "ns-uid"}}
            elif self.path in values:
                body = values[self.path]
            else:
                self.send_response(403)
                self.end_headers()
                return
            raw = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    api = ThreadingHTTPServer(("127.0.0.1", 0), Kubernetes)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    api.socket = context.wrap_socket(api.socket, server_side=True)
    thread = threading.Thread(target=api.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        bridge_port = port.getsockname()[1]
    private(
        tmp_path / "fixture.json",
        {"namespace": NS, "status": "case_ready", "objects": extras + own if extras_first else own + extras},
    )
    private(
        tmp_path / "scope.json",
        {"resources": [{"kind": "ConfigMap", "namespace": "kube-system", "name": "coredns", "uid": "config-uid"}]},
    )
    private(tmp_path / "token", "g" * 48)
    private(
        tmp_path / "kubeconfig.json",
        {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "kind-kind",
            "clusters": [
                {
                    "name": "kind-kind",
                    "cluster": {
                        "server": "https://127.0.0.1:" + str(api.server_port),
                        "certificate-authority-data": base64.b64encode(cert.read_bytes()).decode(),
                    },
                }
            ],
            "users": [{"name": "external-readonly", "user": {"token": "s" * 48}}],
            "contexts": [
                {"name": "kind-kind", "context": {"cluster": "kind-kind", "user": "external-readonly", "namespace": NS}}
            ],
        },
    )
    with (tmp_path / "bridge.log").open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "clients.external_agent.gateways.kubernetes",
                "--namespace",
                NS,
                "--namespace-uid",
                "ns-uid",
                "--fixture-receipt",
                str(tmp_path / "fixture.json"),
                "--scope-file",
                str(tmp_path / "scope.json"),
                "--token-file",
                str(tmp_path / "token"),
                "--audit",
                str(tmp_path / "audit.jsonl"),
                "--kubeconfig",
                str(tmp_path / "kubeconfig.json"),
                "--port",
                str(bridge_port),
                "--duration",
                "30",
            ],
            stdout=log,
            stderr=log,
        )
        try:
            base = "http://127.0.0.1:" + str(bridge_port)
            with httpx.Client(timeout=2, trust_env=False) as client:
                for _ in range(80):
                    assert process.poll() is None, (tmp_path / "bridge.log").read_text()
                    try:
                        if client.get(base + "/version").status_code == 401:
                            break
                    except httpx.ConnectError:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail("Gateway process did not bind")
                headers = {"Authorization": "Bearer " + "g" * 48}
                for plural, uid in expected.items():
                    response = client.get(base + PREFIX + plural, headers=headers)
                    assert response.status_code == 200, (plural, response.status_code)
                    assert [x["metadata"]["uid"] for x in response.json()["items"]] == [uid]
                foreign = client.get(base + PREFIX + "namespaces/kube-system/rolebindings", headers=headers)
                assert foreign.status_code == 403
                assert not any("/namespaces/kube-system/" in path for path in observed)
        finally:
            process.terminate()
            process.wait(timeout=5)
            api.shutdown()
            api.server_close()
            thread.join(timeout=3)
