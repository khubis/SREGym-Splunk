"""Delete only captured case access UIDs; never sweep storage or reset a cluster."""

import hashlib
import json
import os

from kubernetes import client
from kubernetes.client.rest import ApiException

from .driver import Hold, now, save


def admit_object(actual, captured):
    if actual["metadata"]["uid"] != captured["uid"]:
        raise Hold("Captured access object was replaced")
    policy = {key: actual[key] for key in ["rules", "subjects", "roleRef"] if key in actual}
    expected = captured.get("rbac_policy_sha256") or captured.get("policy_sha256")
    if not expected or hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest() != expected:
        raise Hold("Captured access policy changed")
    rv = actual["metadata"].get("resourceVersion")
    if not rv:
        raise Hold("Fresh resourceVersion precondition is missing")
    return client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=captured["uid"], resource_version=rv))


def delete_access(api, rbac, captured, receipt):
    report = {
        "at": now(),
        "requests": [],
        "scope": "Captured ClusterRoleBinding and ClusterRole UIDs only",
        "general_volume_gc": False,
        "node_or_network_reset": False,
    }
    try:
        fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise Hold("Captured access cleanup is already consumed") from None
    with os.fdopen(fd, "w") as handle:
        json.dump(report, handle)
        handle.flush()
        os.fsync(handle.fileno())
    for kind in ["ClusterRoleBinding", "ClusterRole"]:
        rows = [row for row in captured if row["kind"] == kind and row.get("uid")]
        for row in rows:
            read = rbac.read_cluster_role_binding if kind == "ClusterRoleBinding" else rbac.read_cluster_role
            delete = rbac.delete_cluster_role_binding if kind == "ClusterRoleBinding" else rbac.delete_cluster_role
            try:
                actual = read(row["name"], _request_timeout=8)
            except ApiException as error:
                if error.status == 404:
                    report["requests"].append(
                        {"kind": kind, "name": row["name"], "outcome": "already_absent", "delete_requests": 0}
                    )
                    save(receipt, report)
                    continue
                raise
            options = admit_object(api.sanitize_for_serialization(actual), row)
            entry = {
                "kind": kind,
                "name": row["name"],
                "uid": row["uid"],
                "outcome": "delete_reserved",
                "delete_requests": 1,
            }
            report["requests"].append(entry)
            save(receipt, report)
            try:
                delete(row["name"], body=options, _request_timeout=8)
            except Exception as error:
                entry.update(outcome="uncertain_hold_no_retry", error_type=type(error).__name__)
                save(receipt, report)
                raise
            try:
                read(row["name"], _request_timeout=8)
            except ApiException as error:
                if error.status != 404:
                    raise
            else:
                raise Hold("Access object remains after deletion; do not retry")
            entry["outcome"] = "deleted_verified"
            save(receipt, report)
    report["completed_at"] = now()
    save(receipt, report)
    return report
