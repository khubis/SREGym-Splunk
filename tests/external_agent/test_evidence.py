import json
from unittest.mock import Mock

import pytest

from clients.external_agent.evidence import ExtraDenied, projected_read, resource_path


def role(name="captured", uid="uid"):
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": name, "uid": uid},
        "rules": [{"verbs": ["get"], "resources": ["configmaps"], "apiGroups": [""]}],
    }


def getter(values):
    return Mock(side_effect=lambda path: (200, "application/json", json.dumps(values[path]).encode()))


def test_collection_fetches_only_exact_captured_objects():
    rows = [{"kind": "ClusterRole", "name": "captured", "uid": "uid"}]
    base = [{"kind": "ClusterRole", "name": "own", "uid": "own-uid"}]
    values = {resource_path(rows[0]): role(), resource_path(base[0]): role("own", "own-uid")}
    get = getter(values)
    res = projected_read(
        "GET", "/apis/rbac.authorization.k8s.io/v1/clusterroles?limit=500", {"resources": rows}, base, get
    )
    assert {x["metadata"]["uid"] for x in json.loads(res[2])["items"]} == {"uid", "own-uid"}
    assert get.call_count == 2 and all("/clusterroles/" in x.args[0] for x in get.call_args_list)


def test_uncaptured_or_replaced_object_is_not_served():
    row = {"kind": "ClusterRole", "name": "captured", "uid": None}
    with pytest.raises(ExtraDenied):
        projected_read("GET", resource_path(row), {"resources": [row]}, [], Mock())
    row["uid"] = "uid"
    with pytest.raises(ExtraDenied):
        projected_read(
            "GET", resource_path(row), {"resources": [row]}, [], getter({resource_path(row): role(uid="replacement")})
        )


@pytest.mark.parametrize(
    "method,query",
    [
        ("POST", ""),
        ("PATCH", ""),
        ("DELETE", ""),
        ("GET", "?watch=true"),
        ("GET", "?limit=1000"),
        ("GET", "?limit=1&limit=2"),
    ],
)
def test_no_mutations_streams_or_unbounded_lists(method, query):
    row = {"kind": "ClusterRole", "name": "captured", "uid": "uid"}
    get = Mock()
    with pytest.raises(ExtraDenied):
        projected_read(method, resource_path(row) + query, {"resources": [row]}, [], get)
    get.assert_not_called()


def test_foreign_paths_do_not_match_extra_scope():
    row = {"kind": "ConfigMap", "namespace": "kube-system", "name": "coredns", "uid": "uid"}
    get = Mock()
    assert projected_read("GET", "/api/v1/namespaces/kube-system/secrets", {"resources": [row]}, [], get) is None
    get.assert_not_called()


def test_policy_change_rejected():
    row = {"kind": "ClusterRole", "name": "captured", "uid": "uid", "policy_sha256": "0" * 64}
    with pytest.raises(ExtraDenied):
        projected_read("GET", resource_path(row), {"resources": [row]}, [], getter({resource_path(row): role()}))


@pytest.mark.parametrize(
    "row",
    [
        {"kind": "ConfigMap", "namespace": "foreign", "name": "private"},
        {"kind": "ClusterRole", "name": "../../secrets"},
        {"kind": "ClusterRole", "name": "owned", "namespace": "foreign"},
    ],
)
def test_scope_cannot_become_arbitrary_protected_read(row):
    with pytest.raises(ExtraDenied):
        resource_path(row)
