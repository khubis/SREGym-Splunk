import hashlib
import json

import pytest

from clients.external_agent.gateways.kubernetes import (
    REVIEWS,
    Denied,
    filter_body,
    rbac_identity,
    rbac_target,
    review_body,
    secret_file,
    validate_request,
)

NS = "external-pilot-fixture"


def test_identity_review_is_only_nonpersistent_self_introspection():
    path = "/apis/authentication.k8s.io/v1/selfsubjectreviews"
    assert validate_request("POST", path, NS) == (path, "selfreview")
    assert review_body(path, b'{"kind":"SelfSubjectReview"}', NS) == {
        "kind": "SelfSubjectReview",
        "apiVersion": "authentication.k8s.io/v1",
        "spec": {},
    }


@pytest.mark.parametrize(
    "obj",
    [
        {"spec": {"user": "admin"}},
        {"status": {"userInfo": {"username": "admin"}}},
        {"metadata": {"name": "persist-me"}},
        {"kind": "TokenReview"},
        {"apiVersion": "wrong/v1"},
        {"spec": ["wrong"]},
        [],
    ],
)
def test_identity_review_rejects_user_controlled_claims(obj):
    with pytest.raises(Denied):
        review_body("/apis/authentication.k8s.io/v1/selfsubjectreviews", json.dumps(obj).encode(), NS)


def test_permission_reviews_do_not_grant_or_execute_target_verb():
    path = "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews"
    obj = {"spec": {"resourceAttributes": {"namespace": NS, "verb": "delete", "resource": "pods"}}}
    assert review_body(path, json.dumps(obj).encode(), NS)["spec"] == obj["spec"]
    obj["spec"]["user"] = "admin"
    with pytest.raises(Denied):
        review_body(path, json.dumps(obj).encode(), NS)


def test_rules_review_is_only_own_namespace():
    path = "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews"
    assert review_body(path, json.dumps({"spec": {"namespace": NS}}).encode(), NS)["spec"] == {"namespace": NS}
    with pytest.raises(Denied):
        review_body(path, b'{"spec":{"namespace":"kube-system"}}', NS)


@pytest.mark.parametrize("target", [p + "?dryRun=All" for p in REVIEWS])
def test_review_query_options_rejected(target):
    with pytest.raises(Denied):
        validate_request("POST", target, NS)


def test_review_body_bound():
    with pytest.raises(Denied):
        review_body(next(iter(REVIEWS)), b" " * 4097, NS)


@pytest.mark.parametrize(
    "path",
    [
        "/api?timeout=32s",
        "/apis?timeout=30s",
        "/api/v1?timeout=32s",
        "/apis/apps/v1?timeout=32s",
        "/version?timeout=32s",
    ],
)
def test_standard_kubernetes_discovery_timeout_is_stripped(path):
    normalized, _ = validate_request("GET", path, NS)
    assert normalized == path.split("?")[0]


def test_bounded_client_timeout_does_not_change_namespace_or_log_scope():
    normalized, _ = validate_request("GET", f"/api/v1/namespaces/{NS}/pods?timeout=32s&limit=100", NS)
    assert "timeout" not in normalized and "limit=100" in normalized
    normalized, _ = validate_request(
        "GET", f"/api/v1/namespaces/{NS}/pods/connectivity/log?follow=false&timeout=30s", NS
    )
    assert "follow" not in normalized and "timeout" not in normalized and "limitBytes=" in normalized
    with pytest.raises(Denied):
        validate_request("GET", "/api/v1/namespaces/kube-system/pods?timeout=32s", NS)


@pytest.mark.parametrize(
    "query", ["timeout=0s", "timeout=999s", "timeout=-1s", "timeout=32s&watch=true", "timeout=32s&timeout=30s"]
)
def test_discovery_timeout_is_finite_and_cannot_enable_streams(query):
    with pytest.raises(Denied):
        validate_request("GET", "/api?" + query, NS)


@pytest.mark.parametrize(
    "method,target",
    [
        ("POST", f"/api/v1/namespaces/{NS}/pods"),
        ("DELETE", f"/api/v1/namespaces/{NS}/pods/a"),
        ("PATCH", f"/apis/apps/v1/namespaces/{NS}/deployments/a"),
        ("GET", f"/api/v1/namespaces/{NS}/secrets"),
        ("GET", "/api/v1/namespaces/kube-system/pods"),
        ("GET", "/api/v1/secrets"),
        ("GET", "/api/v1/nodes"),
        ("GET", f"/api/v1/namespaces/{NS}/pods/a/exec"),
        ("GET", f"/api/v1/namespaces/{NS}/pods/a/portforward"),
        ("GET", f"/api/v1/namespaces/{NS}/pods/a/proxy"),
        ("GET", f"/api/v1/namespaces/{NS}/pods?watch=true"),
        ("GET", f"/api/v1/namespaces/{NS}/pods/a/log?follow=true"),
        ("GET", f"/api/v1/namespaces/{NS}/pods/a/log?limitBytes=9999999"),
        ("GET", f"/api/v1/namespaces/{NS}/pods?limit=0"),
        ("GET", "/api/v1/namespaces?limit=501"),
        ("GET", "/api/v1/namespaces?watch=true"),
        ("GET", f"/api/v1/namespaces/{NS}/pods?limit=1&limit=500"),
        ("GET", f"/api/v1/namespaces/{NS}/../kube-system/pods"),
        ("GET", f"/api/v1/namespaces/{NS}/%2e%2e/kube-system/pods"),
        ("GET", f"/api/v1/namespaces/{NS}/%252e%252e/kube-system/pods"),
        ("GET", f"/api/v1//namespaces/{NS}/pods"),
        ("GET", f"https://elsewhere.invalid/api/v1/namespaces/{NS}/pods"),
        ("GET", f"/api/v1/namespaces/{NS}/pods#fragment"),
        ("GET", "/apis/rbac.authorization.k8s.io/v1/namespaces/kube-system/roles"),
        ("GET", f"/api/v1/namespaces/{NS}/serviceaccounts/a/token"),
    ],
)
def test_denies_before_upstream(method, target):
    with pytest.raises((Denied, ValueError)):
        validate_request(method, target, NS)


@pytest.mark.parametrize(
    "target",
    [
        "/version",
        "/api",
        "/api/v1",
        "/apis",
        "/apis/apps",
        "/apis/apps/v1",
        "/api/v1/namespaces?limit=1",
        f"/api/v1/namespaces/{NS}",
        f"/api/v1/namespaces/{NS}/pods",
        f"/api/v1/namespaces/{NS}/events?limit=100",
        f"/apis/apps/v1/namespaces/{NS}/deployments",
        f"/api/v1/namespaces/{NS}/pods/example/log?container=app",
    ],
)
def test_valid_finite_reads(target):
    path, _ = validate_request("GET", target, NS)
    assert path.startswith("/")


@pytest.mark.parametrize("resource", ["roles", "rolebindings", "clusterroles", "clusterrolebindings"])
def test_rbac_collection_projects_to_exact_fixture_get(resource):
    path = "/apis/rbac.authorization.k8s.io/v1/" + resource
    assert validate_request("GET", path, NS)[1] == "owned_rbac"
    target, kind, is_list = rbac_target(path, NS)
    assert is_list and kind in {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}
    assert target.endswith("/" + (NS + "-namespace" if resource.startswith("cluster") else "external-readonly"))


@pytest.mark.parametrize(
    "path",
    [
        "/apis/rbac.authorization.k8s.io/v2/clusterrolebindings",
        "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/cluster-admin",
        "/apis/rbac.authorization.k8s.io/v1/clusterroles/cluster-admin",
        "/apis/rbac.authorization.k8s.io/v1/namespaces/kube-system/rolebindings",
        "/apis/rbac.authorization.k8s.io/v1/rolebindings?limit=1",
    ],
)
def test_foreign_rbac_and_unrecognized_options_rejected(path):
    with pytest.raises(Denied):
        validate_request("GET", path, NS)


def test_rbac_identity_and_policy_are_both_fixed():
    obj = {"metadata": {"uid": "own-uid"}, "rules": [{"verbs": ["get"], "resources": ["pods"]}]}
    expected = {
        "uid": "own-uid",
        "rbac_policy_sha256": hashlib.sha256(json.dumps({"rules": obj["rules"]}, sort_keys=True).encode()).hexdigest(),
    }
    rbac_identity(obj, expected)
    obj["metadata"]["uid"] = "replacement"
    with pytest.raises(Denied):
        rbac_identity(obj, expected)
    obj["metadata"]["uid"] = "own-uid"
    obj["rules"][0]["verbs"] = ["*"]
    with pytest.raises(Denied):
        rbac_identity(obj, expected)


def test_logs_have_bounds_without_caller_bounds():
    path, _ = validate_request("GET", f"/api/v1/namespaces/{NS}/pods/example/log", NS)
    assert "limitBytes=131072" in path and "tailLines=1000" in path and "sinceSeconds=3600" in path


@pytest.mark.parametrize(
    "prefix,resource",
    [("/api/v1", "pods"), ("/api/v1", "services"), ("/apis/apps/v1", "deployments"), ("/apis/batch/v1", "jobs")],
)
def test_global_collection_is_projected_before_any_upstream_read(prefix, resource):
    path, kind = validate_request("GET", prefix + "/" + resource + "?timeout=32s&limit=100", NS)
    assert path == prefix + "/namespaces/" + NS + "/" + resource + "?limit=100"
    assert kind == resource


def test_collection_projection_never_grants_cluster_resources_or_foreign_namespace():
    for path in [
        "/api/v1/nodes",
        "/api/v1/secrets",
        "/api/v1/namespaces/kube-system/pods",
        "/apis/apps/v1/namespaces/kube-system/deployments",
    ]:
        with pytest.raises(Denied):
            validate_request("GET", path, NS)


def test_foreign_and_synthetic_objects_hidden():
    good = {"metadata": {"namespace": NS, "name": "application"}}
    foreign = {"metadata": {"namespace": "kube-system", "name": "foreign"}}
    synthetic = {"metadata": {"namespace": NS, "labels": {"job": "workload"}}}
    assert filter_body({"items": [good, foreign, synthetic]}, NS, "pods")["items"] == [good]
    with pytest.raises(Denied):
        filter_body(synthetic, NS, "pods")


def test_only_scoped_namespace_visible():
    result = filter_body(
        {"items": [{"metadata": {"name": NS}}, {"metadata": {"name": "kube-system"}}]}, NS, "namespaces"
    )
    assert len(result["items"]) == 1


def test_secrets_and_exec_removed_from_discovery():
    body = {
        "kind": "APIResourceList",
        "groupVersion": "v1",
        "resources": [{"name": n} for n in ["pods", "pods/log", "pods/exec", "secrets", "nodes"]],
    }
    assert [r["name"] for r in filter_body(body, NS, None)["resources"]] == ["pods", "pods/log"]


def test_secret_file_permissions(tmp_path):
    key = tmp_path / "token"
    key.write_text("a" * 48)
    key.chmod(0o600)
    assert secret_file(key) == "a" * 48
    key.chmod(0o644)
    with pytest.raises(ValueError):
        secret_file(key)
    link = tmp_path / "link"
    link.symlink_to(key)
    with pytest.raises(ValueError):
        secret_file(link)
