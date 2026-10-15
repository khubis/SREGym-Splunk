from types import SimpleNamespace as NS

import pytest

from sregym.conductor.problems import mutating_webhook_resource_limits as module


def owner(kind, name, uid, controller=True):
    return NS(kind=kind, name=name, uid=uid, controller=controller)


def replica_set(name="current-rs", uid="rs-current", revision="2", deployment_uid="dep-uid"):
    return NS(
        metadata=NS(
            name=name,
            uid=uid,
            deletion_timestamp=None,
            annotations={"deployment.kubernetes.io/revision": revision},
            owner_references=[owner("Deployment", "nginx-thrift", deployment_uid)],
        )
    )


def pod(
    name="current",
    uid="pod-current",
    rs_uid="rs-current",
    rs_name="current-rs",
    deleting=False,
    memory="256Mi",
    request="128Mi",
    reason=None,
    last_reason=None,
):
    return NS(
        metadata=NS(
            name=name,
            uid=uid,
            deletion_timestamp="now" if deleting else None,
            owner_references=[owner("ReplicaSet", rs_name, rs_uid)],
        ),
        spec=NS(
            containers=[NS(name="nginx-thrift", resources=NS(requests={"memory": request}, limits={"memory": memory}))]
        ),
        status=NS(
            container_statuses=[
                NS(
                    name="nginx-thrift",
                    state=NS(terminated=NS(reason=reason) if reason else None),
                    last_state=NS(terminated=NS(reason=last_reason) if last_reason else None),
                )
            ]
        ),
    )


def replacement(**kwargs):
    return pod(name="replacement", uid="pod-replacement", request="16Mi", memory="16Mi", reason="OOMKilled", **kwargs)


class CoreApi:
    def __init__(self, initial, following):
        self.initial = initial
        self.following = following
        self.deleted = []
        self.reads = 0

    def list_namespaced_pod(self, namespace, label_selector, **kwargs):
        assert namespace == "social-network"
        assert label_selector == "service=nginx-thrift"
        if not self.deleted:
            return NS(items=self.initial)
        value = self.following[min(self.reads, len(self.following) - 1)]
        self.reads += 1
        return NS(items=value)

    def delete_namespaced_pod(self, name, namespace, body, **kwargs):
        self.deleted.append((name, namespace, body))


def problem(monkeypatch, initial=None, following=None, sets=None):
    instance = module.MutatingWebhookResourceLimits.__new__(module.MutatingWebhookResourceLimits)
    instance.namespace = "social-network"
    instance.faulty_service = "nginx-thrift"
    instance.fault_injected = False
    deployment = NS(
        metadata=NS(name="nginx-thrift", uid="dep-uid", annotations={"deployment.kubernetes.io/revision": "2"})
    )
    apps = NS(
        list_namespaced_replica_set=lambda namespace, **kwargs: NS(
            items=sets if sets is not None else [replica_set(), replica_set("old-rs", "rs-old", "1")]
        )
    )
    instance.kubectl = NS(get_deployment=lambda name, namespace: deployment, apps_v1_api=apps)
    instance.core_api = CoreApi(
        initial if initial is not None else [pod()], following if following is not None else [[replacement()]]
    )
    instance.admission_api = NS(create_mutating_webhook_configuration=lambda body: None)
    instance._patch_target_deployment_resources = lambda: None
    instance._ensure_webhook_backend = lambda: None
    instance._install_decoy_webhooks = lambda: None
    instance._build_webhook_body = lambda: {"metadata": {}}
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds
        assert clock[0] < 1000, "injection observation must be bounded"

    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module.time, "sleep", sleep)
    return instance


@pytest.mark.parametrize("old_first", [True, False])
def test_injection_replaces_current_owned_pod_independent_of_list_order(monkeypatch, old_first):
    old = pod("old", "pod-old", "rs-old", "old-rs", deleting=True)
    current = pod()
    instance = problem(monkeypatch, initial=[old, current] if old_first else [current, old])
    instance.inject_fault()
    assert instance.core_api.deleted[0][0:2] == ("current", "social-network")
    assert instance.core_api.deleted[0][2].preconditions.uid == "pod-current"
    assert instance.core_api.reads >= 1
    assert instance.fault_injected is True


@pytest.mark.parametrize(
    "initial",
    [[], [pod(deleting=True)], [pod(rs_uid="unrelated")], [pod(uid=None)], [pod(), pod("second", "second-uid")]],
)
def test_injection_refuses_missing_or_ambiguous_live_current_target(monkeypatch, initial):
    instance = problem(monkeypatch, initial=initial)
    with pytest.raises(RuntimeError):
        instance.inject_fault()
    assert instance.core_api.deleted == []
    assert instance.fault_injected is False


@pytest.mark.parametrize(
    "sets",
    [
        [],
        [replica_set(revision="1")],
        [replica_set(deployment_uid="different-deployment")],
        [replica_set(), replica_set("other-rs", "other-uid")],
    ],
)
def test_injection_requires_unique_current_revision_and_exact_deployment_owner(monkeypatch, sets):
    instance = problem(monkeypatch, sets=sets)
    with pytest.raises(RuntimeError):
        instance.inject_fault()
    assert instance.core_api.deleted == []
    assert instance.fault_injected is False


@pytest.mark.parametrize(
    "following",
    [
        [[pod()]],
        [[pod(name="replacement", uid="pod-replacement")]],
        [[pod(name="replacement", uid="pod-replacement", memory="16Mi", request="16Mi")]],
        [[replacement(rs_uid="unrelated")]],
        [[replacement(deleting=True)]],
        [[pod(name="different-name", uid="pod-current", memory="16Mi", request="16Mi", reason="OOMKilled")]],
    ],
)
def test_injection_does_not_succeed_without_new_owned_mutated_oom_pod(monkeypatch, following):
    instance = problem(monkeypatch, following=following)
    with pytest.raises(RuntimeError):
        instance.inject_fault()
    assert instance.fault_injected is False


def test_injection_waits_for_both_mutation_and_target_container_oom(monkeypatch):
    mutated = pod(name="replacement", uid="pod-replacement", memory="16Mi", request="16Mi")
    complete = pod(name="replacement", uid="pod-replacement", memory="16Mi", request="16Mi", last_reason="OOMKilled")
    instance = problem(monkeypatch, following=[[], [mutated], [complete]])
    instance.inject_fault()
    assert instance.core_api.reads == 3
    assert instance.fault_injected is True


def test_another_container_oom_does_not_satisfy_injection(monkeypatch):
    pending = pod(name="replacement", uid="pod-replacement", memory="16Mi", request="16Mi")
    pending.status.container_statuses.append(
        NS(name="sidecar", state=NS(terminated=NS(reason="OOMKilled")), last_state=None)
    )
    instance = problem(monkeypatch, following=[[pending]])
    with pytest.raises(RuntimeError):
        instance.inject_fault()
    assert instance.fault_injected is False
