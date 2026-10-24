import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from kubernetes import client
from kubernetes.client.rest import ApiException

from clients.external_agent.cleanup import admit_object, delete_access
from clients.external_agent.driver import Hold

ACTUAL = {
    "metadata": {"uid": "captured-uid", "resourceVersion": "123"},
    "rules": [{"apiGroups": [""], "resources": ["namespaces"], "resourceNames": ["case"], "verbs": ["get"]}],
}
CAPTURED = {
    "kind": "ClusterRole",
    "name": "case-namespace",
    "uid": "captured-uid",
    "rbac_policy_sha256": hashlib.sha256(json.dumps({"rules": ACTUAL["rules"]}, sort_keys=True).encode()).hexdigest(),
}


class RBAC:
    def __init__(self, failure=False):
        self.deleted = []
        self.failure = failure

    def read_cluster_role(self, name, **kwargs):
        if self.deleted:
            raise ApiException(status=404)
        return ACTUAL

    def delete_cluster_role(self, name, **kwargs):
        self.deleted.append((name, kwargs))
        if self.failure:
            raise TimeoutError()

    read_cluster_role_binding = read_cluster_role
    delete_cluster_role_binding = delete_cluster_role


class CleanupTests(unittest.TestCase):
    def test_both_fresh_uid_and_resource_version_are_required(self):
        options = admit_object(ACTUAL, CAPTURED)
        self.assertEqual(options.preconditions.uid, "captured-uid")
        self.assertEqual(options.preconditions.resource_version, "123")

    def test_same_name_replacement_is_never_deleted(self):
        with self.assertRaises(Hold):
            admit_object({**ACTUAL, "metadata": {"uid": "replacement", "resourceVersion": "124"}}, CAPTURED)

    def test_changed_access_policy_is_never_deleted(self):
        with self.assertRaises(Hold):
            admit_object({**ACTUAL, "rules": [{"verbs": ["*"], "resources": ["*"]}]}, CAPTURED)

    def test_success_is_read_back_and_no_second_cleanup_is_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            rbac = RBAC()
            api = client.ApiClient()
            path = Path(root) / "cleanup.json"
            result = delete_access(api, rbac, [CAPTURED], path)
            self.assertEqual(result["requests"][0]["outcome"], "deleted_verified")
            self.assertFalse(result["general_volume_gc"])
            with self.assertRaises(Hold):
                delete_access(api, rbac, [CAPTURED], path)
            self.assertEqual(len(rbac.deleted), 1)
            api.close()

    def test_unknown_delete_response_holds_without_retry_or_expansion(self):
        with tempfile.TemporaryDirectory() as root:
            rbac = RBAC(failure=True)
            api = client.ApiClient()
            path = Path(root) / "cleanup.json"
            with self.assertRaises(TimeoutError):
                delete_access(api, rbac, [CAPTURED, {**CAPTURED, "name": "second"}], path)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["requests"][0]["outcome"], "uncertain_hold_no_retry")
            self.assertEqual(len(rbac.deleted), 1)
            api.close()


if __name__ == "__main__":
    unittest.main()
