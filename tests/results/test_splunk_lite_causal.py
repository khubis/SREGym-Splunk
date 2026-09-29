"""The CronJob causal gate must find the same-pod mechanism without leaking objects."""

import json
import subprocess
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from sregym.observability.base import AttemptContext
from sregym.observability.splunk import SplunkBackendError
from sregym.results.splunk_lite_causal import (
    CaseGateError,
    CausalSourceError,
    check_admission_webhook_outage,
    check_cronjob_sidecar,
    check_duplicate_pvc_mounts,
    check_edge_waf_log,
    check_env_shadowing_pods,
    check_finalizer_deadlock_logs,
    check_internal_traffic_policy,
    check_kafka_poison_logs,
    check_mutating_webhook_pods,
    check_namespace_memory_quota,
    check_network_policy_symptom,
    check_readiness_probe_pods,
    check_rolling_update_misconfigured,
    check_search_retry_metrics,
    check_secret_rotation_pod,
    check_service_dns_failure,
    check_service_wrong_pod_selection,
    check_unschedulable_checkout_pod,
    check_valkey_auth_logs,
    check_wrong_dns_policy_pods,
    check_wrong_service_selector,
    read_source_cluster_object,
    read_source_events,
    read_source_logs,
    read_source_object,
    read_source_pods,
    verify_admission_webhook_pre_agent,
    verify_cronjob_pre_agent,
    verify_duplicate_pvc_mounts_pre_agent,
    verify_edge_pre_agent,
    verify_env_shadowing_pre_agent,
    verify_finalizer_deadlock_pre_agent,
    verify_internal_traffic_policy_pre_agent,
    verify_kafka_pre_agent,
    verify_mutating_webhook_pre_agent,
    verify_namespace_memory_quota_pre_agent,
    verify_network_policy_pre_agent,
    verify_readiness_pre_agent,
    verify_rolling_update_pre_agent,
    verify_search_retry_pre_agent,
    verify_secret_rotation_pre_agent,
    verify_service_dns_pre_agent,
    verify_service_wrong_pod_selection_pre_agent,
    verify_unschedulable_checkout_pre_agent,
    verify_valkey_auth_pre_agent,
    verify_wrong_dns_policy_pre_agent,
    verify_wrong_service_selector_pre_agent,
    wait_for_admission_webhook_pre_agent,
    wait_for_cronjob_pre_agent,
    wait_for_duplicate_pvc_mounts_pre_agent,
    wait_for_edge_pre_agent,
    wait_for_env_shadowing_pre_agent,
    wait_for_finalizer_deadlock_pre_agent,
    wait_for_internal_traffic_policy_pre_agent,
    wait_for_kafka_pre_agent,
    wait_for_mutating_webhook_pre_agent,
    wait_for_namespace_memory_quota_pre_agent,
    wait_for_network_policy_pre_agent,
    wait_for_readiness_pre_agent,
    wait_for_rolling_update_pre_agent,
    wait_for_search_retry_pre_agent,
    wait_for_secret_rotation_pre_agent,
    wait_for_service_dns_pre_agent,
    wait_for_service_wrong_pod_selection_pre_agent,
    wait_for_unschedulable_checkout_pre_agent,
    wait_for_valkey_auth_pre_agent,
    wait_for_wrong_dns_policy_pre_agent,
    wait_for_wrong_service_selector_pre_agent,
)

START = datetime(2026, 9, 26, 19, 1, tzinfo=UTC)
END = datetime(2026, 9, 26, 19, 5, tzinfo=UTC)
CONTEXT = AttemptContext("anon_0123456789abcdef0123456789abcdef", "svelte", False, START)


def _pod(*, sidecar_running=True, regular=True, job="audit-log-archiver-123"):
    pod = {
        "metadata": {
            "name": f"{job}-abc", "namespace": "hotel-reservation",
            "creationTimestamp": "2026-09-26T19:01:30Z",
            "ownerReferences": [{"kind": "Job", "name": job}],
        },
        "spec": {"containers": [{"name": "archiver"}]},
        "status": {"containerStatuses": [
            {"name": "archiver", "state": {"terminated": {"reason": "Completed"}}},
            {"name": "fluent-bit-sidecar", "state": {"running": {"startedAt": "2026-09-26T19:01:00Z"}}}
            if sidecar_running else
            {"name": "fluent-bit-sidecar", "state": {"terminated": {"reason": "Completed"}}},
        ]},
    }
    container_section = "containers" if regular else "initContainers"
    pod["spec"].setdefault(container_section, []).append({
        "name": "fluent-bit-sidecar", "env": [{"value": "do-not-persist-secret"}],
    })
    return pod


def _row(pod, *, timestamp="2026-09-26T19:02:05Z"):
    return {"_time": timestamp, "_raw": json.dumps({"object": {"kind": "Pod", **pod}})}


def test_cronjob_causal_gate_confirms_same_pod_states_and_saves_only_safe_proof():
    pod = _pod()
    second = _pod(job="audit-log-archiver-124")
    result = check_cronjob_sidecar([pod, second], [_row(pod), _row(second)], start=START, end=END)
    assert result["status"] == "confirmed"
    assert result["source_count"] == 2
    assert result["splunk_count"] == 2
    assert result["source_job_count"] == 2
    assert result["splunk_job_count"] == 2
    assert result["matched_pod"] == "audit-log-archiver-123-abc"
    assert result["matched_at"] == "2026-09-26T19:02:05Z"
    assert "do-not-persist-secret" not in json.dumps(result)


def test_cronjob_causal_gate_distinguishes_missing_source_from_missing_splunk():
    pod = _pod()
    assert check_cronjob_sidecar([], [_row(pod)], start=START, end=END)["status"] == "missing_at_source"
    assert check_cronjob_sidecar([pod], [], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_cronjob_sidecar([pod], [_row(pod)], start=START, end=END)["status"] == "partial_recurrence"


def test_cronjob_causal_gate_rejects_partial_mismatched_and_out_of_window_rows():
    pod = _pod()
    other = _pod(sidecar_running=False)
    assert check_cronjob_sidecar([pod], [_row(other)], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_cronjob_sidecar([pod], [_row(_pod(regular=False))], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_cronjob_sidecar(
        [pod], [_row(pod, timestamp="2026-09-26T18:59:59Z")], start=START, end=END,
    )["status"] == "missing_in_splunk"
    assert check_cronjob_sidecar([pod], [{"_raw": "{"}], start=START, end=END)["status"] == "missing_in_splunk"


def test_cronjob_causal_gate_rejects_preexisting_pods_even_if_updated_in_window():
    pod = _pod()
    pod["metadata"]["creationTimestamp"] = "2026-09-26T18:45:00Z"
    assert check_cronjob_sidecar([pod], [_row(pod)], start=START, end=END)["status"] == "missing_at_source"


def test_source_reader_uses_explicit_kind_context_and_never_persists_raw_pods():
    commands = []

    def execute(args, **kwargs):
        commands.append((args, kwargs))
        return type("Completed", (), {"stdout": json.dumps({"items": [_pod()]})})()

    pods = read_source_pods("hotel-reservation", context="kind-kind", execute=execute)
    assert len(pods) == 1
    assert commands[0][0] == [
        "kubectl", "--context", "kind-kind", "-n", "hotel-reservation", "get", "pods", "-o", "json",
    ]
    assert commands[0][1]["shell"] is False


@pytest.mark.parametrize("context,namespace", [
    ("prod-context", "hotel-reservation"), ("kind-kind", "Bad Namespace"),
])
def test_source_reader_rejects_unsafe_targets(context, namespace):
    with pytest.raises(CausalSourceError):
        read_source_pods(namespace, context=context, execute=lambda *a, **k: None)


@pytest.mark.parametrize("output", ["{}", "[]", '{"items":[null]}', "{" ] )
def test_source_reader_rejects_invalid_output_without_exposing_it(output):
    def execute(*_args, **_kwargs):
        return type("Completed", (), {"stdout": output})()
    with pytest.raises(CausalSourceError, match="source pod query failed") as raised:
        read_source_pods("hotel-reservation", context="kind-kind", execute=execute)
    assert output not in str(raised.value)


def test_source_reader_redacts_subprocess_errors():
    def execute(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="private-data")

    with pytest.raises(CausalSourceError) as raised:
        read_source_pods("hotel-reservation", context="kind-kind", execute=execute)
    assert "private-data" not in str(raised.value)


@pytest.mark.parametrize("mutation", [
    "not_dict", "missing_metadata", "wrong_name", "wrong_namespace", "missing_owners",
    "wrong_owner", "missing_containers", "init_sidecar", "missing_statuses",
    "missing_archiver_status", "wrong_completion", "stopped_sidecar",
])
def test_cronjob_causal_gate_rejects_incomplete_pod_mechanisms(mutation):
    pod = deepcopy(_pod())
    if mutation == "not_dict":
        pod = None
    elif mutation == "missing_metadata":
        pod.pop("metadata")
    elif mutation == "wrong_name":
        pod["metadata"]["name"] = "another-pod"
    elif mutation == "wrong_namespace":
        pod["metadata"]["namespace"] = "wrong"
    elif mutation == "missing_owners":
        pod["metadata"].pop("ownerReferences")
    elif mutation == "wrong_owner":
        pod["metadata"]["ownerReferences"] = [{"kind": "Deployment", "name": "frontend"}]
    elif mutation == "missing_containers":
        pod["spec"].pop("containers")
    elif mutation == "init_sidecar":
        pod["spec"]["containers"].pop()
    elif mutation == "missing_statuses":
        pod["status"].pop("containerStatuses")
    elif mutation == "missing_archiver_status":
        pod["status"]["containerStatuses"].pop(0)
    elif mutation == "wrong_completion":
        pod["status"]["containerStatuses"][0]["state"]["terminated"]["reason"] = "Error"
    elif mutation == "stopped_sidecar":
        pod["status"]["containerStatuses"][1]["state"] = {"terminated": {"reason": "Completed"}}
    assert check_cronjob_sidecar([pod], [], start=START, end=END)["status"] == "missing_at_source"


@pytest.mark.parametrize("row", [
    {"_time": "bad timestamp", "_raw": "{}"},
    {"_time": 123, "_raw": "{}"},
    {"_time": "2026-09-26T19:02:05Z", "_raw": "{"},
    {"_time": "2026-09-26T19:02:05Z", "_raw": "[]"},
])
def test_cronjob_causal_gate_rejects_malformed_splunk_rows(row):
    assert check_cronjob_sidecar([_pod()], [row], start=START, end=END)["status"] == "missing_in_splunk"


def test_pre_agent_check_requires_three_modalities_and_same_pod_causal_evidence():
    class Backend:
        def __init__(self, *, traces=1, rows=None):
            self.traces = traces
            self.rows = [_row(_pod())] if rows is None else rows
            self.signals = []
            self.objects = []

        def query_signal(self, signal, *args):
            self.signals.append(signal)
            return self.traces if signal == "traces" else 1

        def query_object(self, kind, *args):
            self.objects.append(kind)
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "pods"
            assert kwargs["terms"] == ("audit-log-archiver",)
            return self.rows

    second = _pod(job="audit-log-archiver-124")
    good = Backend(rows=[_row(_pod()), _row(second)])
    proof = verify_cronjob_pre_agent(
        good, source_pods=[_pod(), second], context=CONTEXT, scope=object(),
        connection_id="logs-connection", start=START, end=END,
    )
    assert proof["status"] == "ready"
    assert proof["signals"] == {"metrics": 1, "traces": 1, "logs": 1, "kubernetes_events": 1,
                                "pods": 1, "events": 1}
    assert proof["causal"]["status"] == "confirmed"
    assert good.signals == ["metrics", "traces", "logs", "kubernetes_events"]
    assert good.objects == ["pods", "events"]
    assert "do-not-persist-secret" not in json.dumps(proof)

    assert verify_cronjob_pre_agent(
        Backend(traces=0), source_pods=[_pod()], context=CONTEXT, scope=object(),
        connection_id="logs-connection", start=START, end=END,
    )["status"] == "missing_delivery"
    assert verify_cronjob_pre_agent(
        Backend(rows=[]), source_pods=[_pod()], context=CONTEXT, scope=object(),
        connection_id="logs-connection", start=START, end=END,
    )["status"] == "missing_causal_telemetry"


def test_pre_agent_wait_retries_lag_without_persisting_oracle_to_agent_mount():
    class Backend:
        calls = 0

        def query_signal(self, signal, *args):
            return 1

        def query_object(self, kind, *args):
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return []
            return [_row(_pod()), _row(_pod(job="audit-log-archiver-124"))]

    moments = iter([END, END.replace(minute=6)])
    sleeps = []
    proof = wait_for_cronjob_pre_agent(
        Backend(), source_reader=lambda: [_pod(), _pod(job="audit-log-archiver-124")],
        context=CONTEXT, scope=object(), connection_id="logs-connection", start=START,
        now=lambda: next(moments), sleep=sleeps.append, attempts=2,
    )
    assert proof["status"] == "ready"
    assert proof["window"]["end"] == "2026-09-26T19:06:00Z"
    assert sleeps == [5.0]


def test_pre_agent_wait_fails_fast_on_nontransient_auth_error():
    class Backend:
        def query_signal(self, *args):
            raise SplunkBackendError(status_code=401, transient=False)

    with pytest.raises(CaseGateError, match="authentication"):
        wait_for_cronjob_pre_agent(
            Backend(), source_reader=lambda: [_pod()], context=object(), scope=object(),
            connection_id="logs-connection", start=START, now=lambda: END,
            sleep=lambda _: None, attempts=3,
        )


def test_pre_agent_wait_retries_transient_query_and_source_failures_then_exhausts():
    class Backend:
        def query_signal(self, *args):
            raise SplunkBackendError(status_code=503, transient=True)

    source_attempts = 0

    def source_reader():
        nonlocal source_attempts
        source_attempts += 1
        if source_attempts == 1:
            raise CausalSourceError("private-data")
        return [_pod()]

    with pytest.raises(CaseGateError, match="did not become ready") as raised:
        wait_for_cronjob_pre_agent(
            Backend(), source_reader=source_reader, context=object(), scope=object(),
            connection_id="logs-connection", start=START, now=lambda: END,
            sleep=lambda _: None, attempts=2,
        )
    assert source_attempts == 2
    assert raised.value.proof == {
        "status": "query_error",
        "schema": "sregym.splunk_lite_pre_agent.v1",
        "http_status": 503,
    }
    assert "private-data" not in str(raised.value)


def test_pre_agent_timeout_without_http_status_remains_sanitized():
    class Backend:
        def query_signal(self, *args):
            raise SplunkBackendError(status_code=None, transient=True)

    with pytest.raises(CaseGateError) as raised:
        wait_for_cronjob_pre_agent(
            Backend(), source_reader=lambda: [_pod()], context=object(), scope=object(),
            connection_id="logs-connection", start=START, now=lambda: END,
            sleep=lambda _: None, attempts=1,
        )
    assert raised.value.proof == {
        "status": "query_error", "schema": "sregym.splunk_lite_pre_agent.v1",
    }


def test_pre_agent_wait_rejects_invalid_retry_policy_and_non_auth_rejection():
    with pytest.raises(ValueError, match="retry policy"):
        wait_for_cronjob_pre_agent(
            object(), source_reader=lambda: [], context=object(), scope=object(),
            connection_id="logs-connection", start=START, attempts=0,
        )


def test_pre_agent_wait_obeys_wall_clock_deadline_even_when_attempt_budget_remains():
    class Backend:
        calls = 0

        def query_signal(self, *args):
            self.calls += 1
            return 0

        def query_object(self, *args):
            return 0

        def query_scoped_examples(self, *args, **kwargs):
            return []

    ticks = iter([0.0, 0.0, 301.0])
    backend = Backend()
    with pytest.raises(CaseGateError, match="did not become ready"):
        wait_for_cronjob_pre_agent(
            backend, source_reader=lambda: [_pod()], context=CONTEXT, scope=object(),
            connection_id="logs-connection", start=START, now=lambda: END,
            sleep=lambda _: None, attempts=60, monotonic=lambda: next(ticks), max_wait_seconds=300,
        )
    assert backend.calls == 4

    ticks = iter([0.0, 301.0])
    with pytest.raises(CaseGateError) as expired:
        wait_for_cronjob_pre_agent(
            backend, source_reader=lambda: [_pod()], context=CONTEXT, scope=object(),
            connection_id="logs-connection", start=START, now=lambda: END,
            sleep=lambda _: None, attempts=60, monotonic=lambda: next(ticks), max_wait_seconds=300,
        )
    assert expired.value.proof is None


def test_edge_waf_check_requires_faulty_rule_and_slow_near_match_in_both_sources():
    payload = {
        "event": "request_filter_eval", "rule": "^([a-zA-Z]+)*$",
        "candidateLength": 5001, "elapsedSeconds": 1,
    }
    line = "2026-09-26T19:02:05Z " + json.dumps(payload)
    row = {"_time": "2026-09-26T19:02:06Z", "_raw": json.dumps(payload)}
    proof = check_edge_waf_log([line], [row], start=START, end=END)
    assert proof["status"] == "confirmed"
    assert proof["source_count"] == 1
    assert proof["splunk_count"] == 1
    assert "^([a-zA-Z]+)*$" not in json.dumps(proof)
    prefixed = "[frontend-proxy-abc/frontend-proxy] " + line
    assert check_edge_waf_log([prefixed], [row], start=START, end=END)["status"] == "confirmed"

    assert check_edge_waf_log([line], [], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_edge_waf_log([], [row], start=START, end=END)["status"] == "missing_at_source"
    fast = {**payload, "elapsedSeconds": 0}
    assert check_edge_waf_log([line], [{"_time": row["_time"], "_raw": json.dumps(fast)}],
                              start=START, end=END)["status"] == "missing_in_splunk"


def test_source_log_reader_is_explicitly_scoped_and_bounded():
    commands = []

    def execute(args, **kwargs):
        commands.append((args, kwargs))
        return type("Completed", (), {"stdout": "2026-09-26T19:02:05Z {}\n"})()

    rows = read_source_logs(
        "astronomy-shop", "frontend-proxy", context="kind-kind", start=START,
        execute=execute,
    )
    assert rows == ["2026-09-26T19:02:05Z {}"]
    assert commands[0][0] == [
        "kubectl", "--context", "kind-kind", "-n", "astronomy-shop", "logs",
        "deployment/frontend-proxy", "--since-time=2026-09-26T19:01:00Z", "--timestamps", "--tail=1000",
        "--limit-bytes=2097152", "--all-pods=true", "--max-log-requests=5",
    ]
    assert commands[0][1]["shell"] is False


@pytest.mark.parametrize("namespace,deployment,context,start", [
    ("astronomy-shop", "frontend-proxy", "prod", START),
    ("bad namespace", "frontend-proxy", "kind-kind", START),
    ("astronomy-shop", "bad/deployment", "kind-kind", START),
    ("astronomy-shop", "frontend-proxy", "kind-kind", datetime(2026, 9, 26, 19)),
])
def test_source_log_reader_rejects_unsafe_scope(namespace, deployment, context, start):
    with pytest.raises(CausalSourceError):
        read_source_logs(namespace, deployment, context=context, start=start, execute=lambda *a, **k: None)


def test_source_log_reader_redacts_backend_failure():
    def execute(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="private-log")

    with pytest.raises(CausalSourceError, match="source log query failed") as raised:
        read_source_logs("astronomy-shop", "frontend-proxy", context="kind-kind", start=START, execute=execute)
    assert "private-log" not in str(raised.value)


@pytest.mark.parametrize("payload", [
    {"event": "other", "rule": "^([a-zA-Z]+)*$", "candidateLength": 5001, "elapsedSeconds": 1},
    {"event": "request_filter_eval", "rule": "safe", "candidateLength": 5001, "elapsedSeconds": 1},
    {"event": "request_filter_eval", "rule": "^([a-zA-Z]+)*$", "candidateLength": 2, "elapsedSeconds": 1},
    {"event": "request_filter_eval", "rule": "^([a-zA-Z]+)*$", "candidateLength": True, "elapsedSeconds": 1},
    {"event": "request_filter_eval", "rule": "^([a-zA-Z]+)*$", "candidateLength": 5001, "elapsedSeconds": True},
])
def test_edge_causal_check_rejects_nondiscriminating_events(payload):
    line = "2026-09-26T19:02:05Z " + json.dumps(payload)
    row = {"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps(payload)}
    assert check_edge_waf_log([line], [row], start=START, end=END)["status"] == "missing_at_source"


def test_edge_causal_check_accepts_nested_splunk_message_but_rejects_malformed_rows():
    event = {"event": "request_filter_eval", "rule": "^([a-zA-Z]+)*$",
             "candidateLength": 5001, "elapsedSeconds": 1}
    source = ["2026-09-26T19:02:05Z " + json.dumps(event)]
    nested = {"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"message": json.dumps(event)})}
    assert check_edge_waf_log(source, [nested], start=START, end=END)["status"] == "confirmed"
    nested_fallback = {"_time": "2026-09-26T19:02:05Z", "_raw": {
        "message": "{}", "body": json.dumps(event),
    }}
    assert check_edge_waf_log(source, [nested_fallback], start=START, end=END)["status"] == "confirmed"
    bad_rows = [
        {"_time": "2026-09-26T19:02:05Z", "_raw": "{"},
        {"_time": "2026-09-26T19:02:05Z", "_raw": "[]"},
        {"_time": "2026-09-26T18:45:00Z", "_raw": json.dumps(event)},
    ]
    assert check_edge_waf_log(source, bad_rows, start=START, end=END)["status"] == "missing_in_splunk"
    assert check_edge_waf_log(["not a timestamp {" + json.dumps(event)], [nested],
                              start=START, end=END)["status"] == "missing_at_source"


def test_edge_pre_agent_check_requires_representative_melt_and_causal_log():
    event = {"event": "request_filter_eval", "rule": "^([a-zA-Z]+)*$",
             "candidateLength": 5001, "elapsedSeconds": 1}
    line = "2026-09-26T19:02:05Z " + json.dumps(event)

    class Backend:
        def query_signal(self, signal, *args):
            return 1

        def query_object(self, kind, *args):
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "logs"
            assert kwargs["terms"] == ("request_filter_eval",)
            assert kwargs["container_name"] == "frontend-proxy"
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps(event)}]

    proof = verify_edge_pre_agent(
        Backend(), source_lines=[line], context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )
    assert proof["status"] == "ready"
    assert proof["case_id"] == "edge_request_filter_cpu_saturation"
    assert proof["causal"]["status"] == "confirmed"
    assert proof["signals"]["traces"] == 1


def test_edge_pre_agent_wait_uses_shared_polling_policy():
    event = {"event": "request_filter_eval", "rule": "^([a-zA-Z]+)*$",
             "candidateLength": 5001, "elapsedSeconds": 1}
    line = "2026-09-26T19:02:05Z " + json.dumps(event)

    class Backend:
        def query_signal(self, *args):
            return 1

        def query_object(self, *args):
            return 1

        def query_scoped_examples(self, *args, **kwargs):
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps(event)}]

    proof = wait_for_edge_pre_agent(
        Backend(), source_reader=lambda: [line], context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )
    assert proof["status"] == "ready"


def test_network_policy_case_records_source_policy_and_splunk_timeout_as_data_limited():
    policy = {
        "metadata": {"name": "deny-all-recommendation", "namespace": "hotel-reservation",
                     "creationTimestamp": "2026-09-26T19:01:30Z"},
        "spec": {"podSelector": {"matchLabels": {"io.kompose.service": "recommendation"}},
                 "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []},
    }
    row = {"_time": "2026-09-26T19:02:05Z", "_raw": "Socket errors: connect 0, read 0, write 0, timeout 186"}
    proof = check_network_policy_symptom(policy, [row], start=START, end=END)
    assert proof["status"] == "symptom_confirmed"
    assert proof["source_policy_confirmed"] is True
    assert proof["splunk_timeout_count"] == 1
    assert proof["visibility"] == "requires_additional_access"
    assert "deny-all-recommendation" not in json.dumps(proof)
    api_normalized = deepcopy(policy)
    del api_normalized["spec"]["ingress"]
    del api_normalized["spec"]["egress"]
    assert check_network_policy_symptom(api_normalized, [row], start=START, end=END)["status"] == "symptom_confirmed"
    second_precision = deepcopy(policy)
    second_precision["metadata"]["creationTimestamp"] = "2026-09-26T19:01:30Z"
    fractional_start = datetime(2026, 9, 26, 19, 1, 30, 500000, tzinfo=UTC)
    assert check_network_policy_symptom(second_precision, [row], start=fractional_start, end=END)["status"] == "symptom_confirmed"
    too_early = deepcopy(policy)
    too_early["metadata"]["creationTimestamp"] = "2026-09-26T19:01:29Z"
    assert check_network_policy_symptom(too_early, [row], start=fractional_start, end=END)["status"] == "missing_at_source"
    assert check_network_policy_symptom(None, [row], start=START, end=END)["status"] == "missing_at_source"
    assert check_network_policy_symptom(policy, [], start=START, end=END)["status"] == "missing_in_splunk"
    malformed = deepcopy(policy)
    malformed["spec"]["policyTypes"] = None
    assert check_network_policy_symptom(malformed, [row], start=START, end=END)["status"] == "missing_at_source"
    zero_timeout = {"_time": "2026-09-26T19:02:06Z", "_raw": "Socket errors: timeout 0"}
    assert check_network_policy_symptom(policy, [zero_timeout, row], start=START, end=END)["splunk_timeout_count"] == 1


def test_source_object_reader_is_read_only_and_explicitly_scoped():
    calls = []

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        return type("Completed", (), {"stdout": '{"kind":"NetworkPolicy"}'})()

    assert read_source_object(
        "networkpolicy", "deny-all-recommendation", namespace="hotel-reservation",
        context="kind-kind", execute=execute,
    ) == {"kind": "NetworkPolicy"}
    assert calls[0][0] == [
        "kubectl", "--context", "kind-kind", "-n", "hotel-reservation", "get",
        "networkpolicy", "deny-all-recommendation", "-o", "json",
    ]
    assert calls[0][1]["shell"] is False
    with pytest.raises(CausalSourceError):
        read_source_object("secret", "db", namespace="hotel-reservation", context="kind-kind", execute=execute)
    with pytest.raises(CausalSourceError):
        read_source_object("networkpolicy", "deny-all-recommendation", namespace="hotel-reservation", context="prod", execute=execute)
    with pytest.raises(CausalSourceError):
        read_source_object("networkpolicy", "bad_name", namespace="hotel-reservation", context="kind-kind", execute=execute)

    def malformed_object(args, **kwargs):
        return type("Completed", (), {"stdout": "[]"})()

    with pytest.raises(CausalSourceError):
        read_source_object("networkpolicy", "deny-all-recommendation", namespace="hotel-reservation", context="kind-kind", execute=malformed_object)

    def failed_object(args, **kwargs):
        raise OSError("API unavailable")

    with pytest.raises(CausalSourceError, match="source object query failed"):
        read_source_object("networkpolicy", "deny-all-recommendation", namespace="hotel-reservation", context="kind-kind", execute=failed_object)


def test_source_service_reader_is_read_only_and_scoped():
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return type("Completed", (), {"stdout": '{"kind":"Service","spec":{"internalTrafficPolicy":"Local"}}'})()

    service = read_source_object("service", "recommendation", namespace="astronomy-shop", context="kind-kind", execute=execute)
    assert service["spec"]["internalTrafficPolicy"] == "Local"
    assert calls == [["kubectl", "--context", "kind-kind", "-n", "astronomy-shop", "get", "service", "recommendation", "-o", "json"]]


def test_source_reader_allows_only_coredns_configmap_and_not_arbitrary_configmaps():
    def execute(args, **kwargs):
        assert args == ["kubectl", "--context", "kind-kind", "-n", "kube-system", "get", "configmap", "coredns", "-o", "json"]
        return type("Completed", (), {"stdout": '{"data":{"Corefile":"template ANY ANY user-service.social-network.svc.cluster.local { rcode NXDOMAIN }"}}'})()

    assert "NXDOMAIN" in read_source_object("configmap", "coredns", namespace="kube-system", context="kind-kind", execute=execute)["data"]["Corefile"]
    with pytest.raises(CausalSourceError):
        read_source_object("configmap", "other", namespace="kube-system", context="kind-kind", execute=execute)
    with pytest.raises(CausalSourceError):
        read_source_object("configmap", "coredns", namespace="social-network", context="kind-kind", execute=execute)


def test_source_events_reader_uses_explicit_kind_context():
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return type("Completed", (), {"stdout": '{"items":[{"kind":"Event"}]}'})()

    assert read_source_events("hotel-reservation", context="kind-kind", execute=execute) == [{"kind": "Event"}]
    assert calls == [["kubectl", "--context", "kind-kind", "-n", "hotel-reservation", "get", "events", "-o", "json"]]
    with pytest.raises(CausalSourceError):
        read_source_events("hotel-reservation", context="production", execute=execute)


def test_source_cluster_object_reader_only_allows_approved_webhook():
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return type("Completed", (), {"stdout": '{"kind":"ValidatingWebhookConfiguration"}'})()

    assert read_source_cluster_object(
        "validatingwebhookconfiguration", "pod-policy.validation.k8s.io", context="kind-kind", execute=execute,
    ) == {"kind": "ValidatingWebhookConfiguration"}
    assert calls == [["kubectl", "--context", "kind-kind", "get", "validatingwebhookconfiguration", "pod-policy.validation.k8s.io", "-o", "json"]]
    with pytest.raises(CausalSourceError):
        read_source_cluster_object("validatingwebhookconfiguration", "other", context="kind-kind", execute=execute)
    with pytest.raises(CausalSourceError):
        read_source_cluster_object("secret", "pod-policy.validation.k8s.io", context="kind-kind", execute=execute)


def test_admission_webhook_gate_matches_failed_create_event_and_source_configuration():
    webhook = {"metadata": {"name": "pod-policy.validation.k8s.io", "creationTimestamp": "2026-09-26T19:01:30Z"},
               "webhooks": [{"name": "pod-policy.validation.k8s.io", "failurePolicy": "Fail",
                             "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "hotel-reservation"}},
                             "clientConfig": {"service": {"name": "pod-policy-webhook", "namespace": "policy-system"}}}]}
    event = {"metadata": {"uid": "event-123", "namespace": "hotel-reservation", "creationTimestamp": "2026-09-26T19:02:05Z"},
             "reason": "FailedCreate", "message": "failed calling webhook pod-policy.validation.k8s.io: service not found",
             "involvedObject": {"kind": "ReplicaSet", "name": "recommendation-abc"}}
    proof = check_admission_webhook_outage(webhook, [event], [_row(event)], start=START, end=END)
    assert proof["status"] == "symptom_confirmed"
    assert proof["source_count"] == proof["splunk_count"] == 1
    assert proof["remedy"]
    assert "event-123" not in json.dumps(proof)
    assert check_admission_webhook_outage(webhook, [event], [], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_admission_webhook_outage(webhook, [], [_row(event)], start=START, end=END)["status"] == "missing_at_source"
    wrong = deepcopy(webhook)
    wrong["webhooks"][0]["failurePolicy"] = "Ignore"
    assert check_admission_webhook_outage(wrong, [event], [_row(event)], start=START, end=END)["status"] == "missing_at_source"
    malformed = deepcopy(webhook)
    malformed["webhooks"][0]["clientConfig"]["service"] = None
    assert check_admission_webhook_outage(malformed, [event], [_row(event)], start=START, end=END)["status"] == "missing_at_source"


def test_admission_webhook_pre_agent_queries_scoped_event_object():
    webhook = {"metadata": {"name": "pod-policy.validation.k8s.io", "creationTimestamp": "2026-09-26T19:01:30Z"},
               "webhooks": [{"name": "pod-policy.validation.k8s.io", "failurePolicy": "Fail",
                             "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "hotel-reservation"}},
                             "clientConfig": {"service": {"name": "pod-policy-webhook", "namespace": "policy-system"}}}]}
    event = {"metadata": {"uid": "event-123", "namespace": "hotel-reservation", "creationTimestamp": "2026-09-26T19:02:05Z"},
             "reason": "FailedCreate", "message": "failed calling webhook pod-policy.validation.k8s.io: service not found",
             "involvedObject": {"kind": "ReplicaSet", "name": "recommendation-abc"}}

    class Backend:
        def query_object(self, kind, *args):
            assert kind == "events"
            return 1
        def query_signal(self, *args):
            raise AssertionError("generic readiness already checked MELT")
        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "events"
            assert kwargs["terms"] == ("failed", "calling", "webhook")
            return [_row(event)]

    assert verify_admission_webhook_pre_agent(
        Backend(), source_webhook=webhook, source_events=[event], context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, end=END,
    )["status"] == "ready_data_limited"
    assert wait_for_admission_webhook_pre_agent(
        Backend(), source_reader=lambda: (webhook, [event]), context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"


def test_wrong_service_selector_gate_requires_source_empty_endpoints_and_splunk_failures():
    service = {"metadata": {"name": "user-service", "namespace": "social-network"},
               "spec": {"selector": {"app": "user-service", "current_service_name": "user-service"}}}
    endpoints = {"metadata": {"name": "user-service", "namespace": "social-network"}, "subsets": []}
    rows = [{"_time": "2026-09-26T19:02:05Z", "_raw": "Non-2xx or 3xx responses: 12"}]
    proof = check_wrong_service_selector(service, endpoints, rows, start=START, end=END)
    assert proof["status"] == "symptom_confirmed"
    assert proof["source_empty_endpoints"] is True
    assert proof["splunk_failed_response_count"] == 1
    assert proof["remedy"]
    assert check_wrong_service_selector(service, endpoints, [], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_wrong_service_selector(service, {**endpoints, "subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}, rows, start=START, end=END)["status"] == "missing_at_source"
    assert check_wrong_service_selector({**service, "spec": {"selector": {"app": "user-service"}}}, endpoints, rows, start=START, end=END)["status"] == "missing_at_source"


def test_wrong_service_selector_endpoint_reader_is_exactly_scoped():
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return type("Completed", (), {"stdout": '{"kind":"Endpoints","subsets":[]}'})()

    assert read_source_object(
        "endpoints", "user-service", namespace="social-network", context="kind-kind", execute=execute,
    )["subsets"] == []
    assert calls == [
        ["kubectl", "--context", "kind-kind", "-n", "social-network", "get", "endpoints", "user-service", "-o", "json"]
    ]
    with pytest.raises(CausalSourceError):
        read_source_object("endpoints", "other-service", namespace="social-network", context="kind-kind", execute=execute)


def test_wrong_service_selector_pre_agent_queries_scoped_workload_failures():
    service = {"metadata": {"name": "user-service", "namespace": "social-network"},
               "spec": {"selector": {"current_service_name": "user-service"}}}
    endpoints = {"metadata": {"name": "user-service", "namespace": "social-network"}, "subsets": []}

    class Backend:
        def query_signal(self, signal, *args):
            assert signal == "logs"
            return 1
        def query_object(self, *args):
            raise AssertionError("generic readiness already checked objects")
        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "logs"
            assert kwargs["terms"] == ("Non-2xx",)
            assert kwargs["container_name"] == "wrk2"
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": "Non-2xx or 3xx responses: 12"}]

    assert verify_wrong_service_selector_pre_agent(
        Backend(), source_service=service, source_endpoints=endpoints, context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, end=END,
    )["status"] == "ready_data_limited"
    assert wait_for_wrong_service_selector_pre_agent(
        Backend(), source_reader=lambda: (service, endpoints), context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"


def test_rolling_update_gate_matches_stalled_init_pod_and_source_strategy():
    deployment = {
        "metadata": {"name": "custom-service", "namespace": "social-network"},
        "spec": {"replicas": 3, "strategy": {"type": "RollingUpdate", "rollingUpdate": {
            "maxUnavailable": "100%", "maxSurge": "0%",
        }}},
        "status": {"availableReplicas": 0},
    }
    pod = {
        "metadata": {"name": "custom-service-new", "namespace": "social-network",
                     "creationTimestamp": "2026-09-26T19:01:45Z"},
        "spec": {"initContainers": [{"name": "hang-init", "command": ["/bin/sh", "-c", "sleep infinity"]}]},
        "status": {"phase": "Pending", "initContainerStatuses": [{"name": "hang-init", "state": {"running": {}}}]},
    }
    rows = [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": pod})}]
    proof = check_rolling_update_misconfigured(deployment, [pod], rows, start=START, end=END)
    assert proof["status"] == "symptom_confirmed"
    assert proof["source_count"] == proof["splunk_count"] == 1
    assert proof["visibility"] == "requires_additional_access"
    assert proof["remedy"]
    assert check_rolling_update_misconfigured(deployment, [pod], [], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_rolling_update_misconfigured({**deployment, "status": {"availableReplicas": 2}}, [pod], rows, start=START, end=END)["status"] == "missing_at_source"


def test_rolling_update_pre_agent_queries_custom_service_pod_objects():
    deployment = {"metadata": {"name": "custom-service", "namespace": "social-network"},
                  "spec": {"replicas": 3, "strategy": {"type": "RollingUpdate", "rollingUpdate": {
                      "maxUnavailable": "100%", "maxSurge": "0%"}}}, "status": {"availableReplicas": 0}}
    pod = {"metadata": {"name": "custom-service-new", "namespace": "social-network",
                        "creationTimestamp": "2026-09-26T19:01:45Z"},
           "spec": {"initContainers": [{"name": "hang-init", "command": ["sleep", "infinity"]}]},
           "status": {"phase": "Pending"}}

    class Backend:
        def query_object(self, kind, *args):
            assert kind == "pods"
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "pods" and kwargs["terms"] == ("custom-service",)
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": pod})}]

    assert verify_rolling_update_pre_agent(
        Backend(), source_deployment=deployment, source_pods=[pod], context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, end=END,
    )["status"] == "ready_data_limited"
    assert wait_for_rolling_update_pre_agent(
        Backend(), source_reader=lambda: (deployment, [pod]), context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"


def test_namespace_memory_quota_gate_matches_same_failed_create_event_in_splunk():
    quota = {"metadata": {"name": "memory-limit-quota", "namespace": "hotel-reservation", "creationTimestamp": "2026-09-26T19:01:30Z"},
             "spec": {"hard": {"memory": "1Gi"}}}
    event = {"metadata": {"uid": "event-123", "namespace": "hotel-reservation", "creationTimestamp": "2026-09-26T19:02:05Z"},
             "reason": "FailedCreate", "message": "Error creating: pods is forbidden: must specify memory", "involvedObject": {"name": "search-123", "kind": "ReplicaSet"}}
    rows = [_row(event)]
    proof = check_namespace_memory_quota(quota, [event], rows, start=START, end=END)
    assert proof["status"] == "symptom_confirmed"
    assert proof["source_count"] == proof["splunk_count"] == 1
    assert proof["remedy"]
    assert "event-123" not in json.dumps(proof)
    assert check_namespace_memory_quota(quota, [event], [], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_namespace_memory_quota(quota, [], rows, start=START, end=END)["status"] == "missing_at_source"
    assert check_namespace_memory_quota({**quota, "spec": {"hard": {}}}, [event], rows, start=START, end=END)["status"] == "missing_at_source"
    wrong = deepcopy(event)
    wrong["message"] = "unrelated scheduling failure"
    assert check_namespace_memory_quota(quota, [event], [_row(wrong)], start=START, end=END)["status"] == "missing_in_splunk"


def test_namespace_memory_quota_pre_agent_queries_events():
    quota = {"metadata": {"name": "memory-limit-quota", "namespace": "hotel-reservation", "creationTimestamp": "2026-09-26T19:01:30Z"},
             "spec": {"hard": {"memory": "1Gi"}}}
    event = {"metadata": {"uid": "event-123", "namespace": "hotel-reservation", "creationTimestamp": "2026-09-26T19:02:05Z"},
             "reason": "FailedCreate", "message": "must specify memory", "involvedObject": {"name": "search-123", "kind": "ReplicaSet"}}

    class Backend:
        def query_signal(self, *args):
            return 1
        def query_object(self, *args):
            return 1
        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "events"
            assert kwargs["terms"] == ("must", "specify", "memory")
            return [_row(event)]

    assert verify_namespace_memory_quota_pre_agent(
        Backend(), source_quota=quota, source_events=[event], context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, end=END,
    )["status"] == "ready_data_limited"
    assert wait_for_namespace_memory_quota_pre_agent(
        Backend(), source_reader=lambda: (quota, [event]), context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"


def test_valkey_auth_gate_requires_same_error_signature_at_source_and_splunk():
    source = ["2026-09-26T19:02:01Z cart cache request failed: WRONGPASS invalid username-password pair"]
    rows = [{"_time": "2026-09-26T19:02:05Z", "_raw": "cart cache request failed: WRONGPASS invalid username-password pair"}]
    proof = check_valkey_auth_logs(source, rows, start=START, end=END)
    assert proof["status"] == "symptom_confirmed"
    assert proof["source_count"] == proof["splunk_count"] == 1
    assert proof["remedy"]
    assert "username-password" not in json.dumps(proof)
    assert check_valkey_auth_logs(source, [], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_valkey_auth_logs([], rows, start=START, end=END)["status"] == "missing_at_source"
    assert check_valkey_auth_logs(source, [{"_time": "2026-09-26T19:00:00Z", "_raw": rows[0]["_raw"]}], start=START, end=END)["status"] == "missing_in_splunk"


def test_valkey_gate_accepts_observed_cart_redis_connectivity_symptom_without_claiming_auth_proof():
    source = ["[pod/cart-abc/cart] 2026-09-26T19:02:01Z Wasn't able to connect to redis"]
    rows = [{"_time": "2026-09-26T19:02:05Z", "_raw": "Wasn't able to connect to redis"}]
    proof = check_valkey_auth_logs(source, rows, start=START, end=END)
    assert proof["status"] == "symptom_confirmed"
    assert proof["source_count"] == proof["splunk_count"] == 1
    assert proof["visibility"] == "requires_additional_access"
    assert "authentication" not in proof["interpretation"].lower()


def test_valkey_auth_pre_agent_queries_cart_auth_logs_only():
    source = ["2026-09-26T19:02:01Z cart cache request failed: WRONGPASS"]

    class Backend:
        def query_signal(self, signal, *args):
            assert signal == "logs"
            return 1
        def query_object(self, *args):
            raise AssertionError("generic readiness already checked objects")
        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "logs"
            assert kwargs["terms"] in {("WRONGPASS",), ("NOAUTH",), ("authentication",), ("redis",)}
            assert kwargs["container_name"] == "cart"
            return ([{"_time": "2026-09-26T19:02:05Z", "_raw": "cart cache request failed: WRONGPASS"}]
                    if kwargs["terms"] == ("WRONGPASS",) else [])

    assert verify_valkey_auth_pre_agent(
        Backend(), source_lines=source, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )["status"] == "ready_data_limited"
    assert wait_for_valkey_auth_pre_agent(
        Backend(), source_reader=lambda: source, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"


def test_valkey_pre_agent_accepts_scoped_redis_connectivity_log():
    source = ["[pod/cart-abc/cart] 2026-09-26T19:02:01Z Wasn't able to connect to redis"]

    class Backend:
        def query_signal(self, signal, *args):
            assert signal == "logs"
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "logs" and kwargs["container_name"] == "cart"
            return ([{"_time": "2026-09-26T19:02:05Z", "_raw": "Wasn't able to connect to redis"}]
                    if kwargs["terms"] == ("redis",) else [])

    proof = verify_valkey_auth_pre_agent(
        Backend(), source_lines=source, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )
    assert proof["status"] == "ready_data_limited"
    assert proof["causal"]["auth_specific_log_present"] is False


def test_secret_rotation_gate_confirms_stale_source_uid_and_secret_ref_without_values():
    deployment = {"metadata": {"name": "product-catalog", "namespace": "astronomy-shop",
                               "annotations": {"credential-source-pod-uid": "source-pod-uid"}}}
    pod = {"metadata": {"name": "product-catalog-abc", "uid": "source-pod-uid", "namespace": "astronomy-shop",
                        "creationTimestamp": "2026-09-26T19:01:45Z"},
           "spec": {"containers": [{"name": "product-catalog", "env": [{"name": "DB_CONNECTION_STRING",
                                                                       "valueFrom": {"secretKeyRef": {"name": "product-catalog-db-conn", "key": "DB_CONNECTION_STRING"}}}]}]},
           "status": {"phase": "Running"}}
    proof = check_secret_rotation_pod(deployment, [pod], [_row(pod)], start=START, end=END)
    assert proof["status"] == "symptom_confirmed"
    assert proof["source_count"] == proof["splunk_count"] == 1
    assert proof["remedy"]
    assert "source-pod-uid" not in json.dumps(proof)
    assert "DB_CONNECTION_STRING" not in json.dumps(proof)
    assert check_secret_rotation_pod(deployment, [pod], [], start=START, end=END)["status"] == "missing_in_splunk"
    wrong_deployment = deepcopy(deployment)
    wrong_deployment["metadata"]["annotations"]["credential-source-pod-uid"] = "other-uid"
    assert check_secret_rotation_pod(wrong_deployment, [pod], [_row(pod)], start=START, end=END)["status"] == "missing_at_source"
    wrong_pod = deepcopy(pod)
    wrong_pod["spec"]["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]["name"] = "other-secret"
    assert check_secret_rotation_pod(deployment, [wrong_pod], [_row(pod)], start=START, end=END)["status"] == "missing_at_source"


def test_secret_rotation_pre_agent_queries_only_scoped_pod_objects():
    deployment = {"metadata": {"name": "product-catalog", "namespace": "astronomy-shop",
                               "annotations": {"credential-source-pod-uid": "source-pod-uid"}}}
    pod = {"metadata": {"name": "product-catalog-abc", "uid": "source-pod-uid", "namespace": "astronomy-shop",
                        "creationTimestamp": "2026-09-26T19:01:45Z"},
           "spec": {"containers": [{"name": "product-catalog", "env": [{"name": "DB_CONNECTION_STRING",
                                                                       "valueFrom": {"secretKeyRef": {"name": "product-catalog-db-conn", "key": "DB_CONNECTION_STRING"}}}]}]},
           "status": {"phase": "Running"}}

    class Backend:
        def query_object(self, kind, *args):
            assert kind == "pods"
            return 1
        def query_signal(self, *args):
            raise AssertionError("generic readiness already checked MELT")
        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "pods"
            assert kwargs["terms"] == ("product-catalog",)
            return [_row(pod)]

    assert verify_secret_rotation_pre_agent(
        Backend(), source_deployment=deployment, source_pods=[pod], context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, end=END,
    )["status"] == "ready_data_limited"
    assert wait_for_secret_rotation_pre_agent(
        Backend(), source_reader=lambda: (deployment, [pod]), context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"


def test_unschedulable_checkout_gate_requires_both_faults_in_same_new_pod():
    pod = {"metadata": {"name": "checkout-abc", "namespace": "astronomy-shop", "creationTimestamp": "2026-09-26T19:01:45Z"},
           "spec": {"nodeSelector": {"kubernetes.io/hostname": "extra-node"},
                    "containers": [{"name": "checkout", "env": [{"name": "PRODUCT_CATALOG_ADDR", "value": "product-catalog:8082"}]}]},
           "status": {"phase": "Pending"}}
    proof = check_unschedulable_checkout_pod([pod], [_row(pod)], start=START, end=END)
    assert proof["status"] == "confirmed"
    assert proof["visibility"] == "fully_splunk_observable"
    assert proof["source_count"] == proof["splunk_count"] == 1
    assert check_unschedulable_checkout_pod([pod], [], start=START, end=END)["status"] == "missing_in_splunk"
    wrong = deepcopy(pod)
    wrong["spec"]["containers"][0]["env"][0]["value"] = "product-catalog:8080"
    assert check_unschedulable_checkout_pod([wrong], [_row(pod)], start=START, end=END)["status"] == "missing_at_source"
    wrong_node = deepcopy(pod)
    wrong_node["spec"]["nodeSelector"] = {}
    assert check_unschedulable_checkout_pod([wrong_node], [_row(pod)], start=START, end=END)["status"] == "missing_at_source"
    assert check_unschedulable_checkout_pod([pod], [_row(wrong)], start=START, end=END)["status"] == "missing_in_splunk"


def test_unschedulable_checkout_pre_agent_queries_scoped_pod_object():
    pod = {"metadata": {"name": "checkout-abc", "namespace": "astronomy-shop", "creationTimestamp": "2026-09-26T19:01:45Z"},
           "spec": {"nodeSelector": {"kubernetes.io/hostname": "extra-node"},
                    "containers": [{"name": "checkout", "env": [{"name": "PRODUCT_CATALOG_ADDR", "value": "product-catalog:8082"}]}]},
           "status": {"phase": "Pending"}}

    class Backend:
        def query_object(self, kind, *args):
            assert kind == "pods"
            return 1
        def query_signal(self, *args):
            raise AssertionError("generic readiness already checked MELT")
        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "pods"
            assert kwargs["terms"] == ("checkout",)
            return [_row(pod)]

    assert verify_unschedulable_checkout_pre_agent(
        Backend(), source_pods=[pod], context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )["status"] == "ready"
    assert wait_for_unschedulable_checkout_pre_agent(
        Backend(), source_reader=lambda: [pod], context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready"


def test_duplicate_pvc_gate_matches_running_and_pending_jaeger_pods_with_shared_claim():
    pvc = {"metadata": {"name": "jaeger-pvc", "namespace": "social-network", "creationTimestamp": "2026-09-26T19:01:30Z"},
           "spec": {"accessModes": ["ReadWriteOnce"]}}

    def pod(name, phase):
        return {"metadata": {"name": name, "namespace": "social-network", "creationTimestamp": "2026-09-26T19:01:45Z"},
                "spec": {"volumes": [{"name": "jaeger-volume", "persistentVolumeClaim": {"claimName": "jaeger-pvc"}}],
                         "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
                             {"topologyKey": "kubernetes.io/hostname", "labelSelector": {"matchExpressions": []}}]}}},
                "status": {"phase": phase}}

    running, pending = pod("jaeger-a", "Running"), pod("jaeger-b", "Pending")
    proof = check_duplicate_pvc_mounts(pvc, [running, pending], [_row(running), _row(pending)], start=START, end=END)
    assert proof["status"] == "symptom_confirmed"
    assert proof["source_count"] == proof["splunk_count"] == 2
    assert proof["visibility"] == "requires_additional_access"
    assert proof["remedy"]
    assert check_duplicate_pvc_mounts(pvc, [running, pending], [_row(running)], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_duplicate_pvc_mounts(pvc, [running], [_row(running), _row(pending)], start=START, end=END)["status"] == "missing_at_source"
    assert check_duplicate_pvc_mounts({**pvc, "spec": {"accessModes": ["ReadWriteMany"]}}, [running, pending], [_row(running), _row(pending)], start=START, end=END)["status"] == "missing_at_source"


def test_duplicate_pvc_pre_agent_queries_jaeger_pod_objects():
    pvc = {"metadata": {"name": "jaeger-pvc", "namespace": "social-network", "creationTimestamp": "2026-09-26T19:01:30Z"},
           "spec": {"accessModes": ["ReadWriteOnce"]}}
    pods = [
        {"metadata": {"name": f"jaeger-{phase}", "namespace": "social-network", "creationTimestamp": "2026-09-26T19:01:45Z"},
         "spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "jaeger-pvc"}}],
                  "affinity": {"podAntiAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{"topologyKey": "kubernetes.io/hostname"}]}}},
         "status": {"phase": phase}}
        for phase in ("Running", "Pending")
    ]

    class Backend:
        def query_object(self, kind, *args):
            assert kind == "pods"
            return 1
        def query_signal(self, *args):
            raise AssertionError("generic readiness already checked MELT")
        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "pods"
            assert kwargs["terms"] == ("jaeger",)
            return [_row(pod) for pod in pods]

    assert verify_duplicate_pvc_mounts_pre_agent(
        Backend(), source_pvc=pvc, source_pods=pods, context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, end=END,
    )["status"] == "ready_data_limited"
    assert wait_for_duplicate_pvc_mounts_pre_agent(
        Backend(), source_reader=lambda: (pvc, pods), context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"


def test_service_dns_gate_requires_current_coredns_rule_and_scoped_failed_response_symptom():
    configmap = {"data": {"Corefile": "template ANY ANY user-service.social-network.svc.cluster.local {\n rcode NXDOMAIN\n }"}}
    rows = [{"_time": "2026-09-26T19:02:05Z", "_raw": "Non-2xx or 3xx responses: 12"}]
    proof = check_service_dns_failure(configmap, rows, start=START, end=END)
    assert proof["status"] == "symptom_confirmed"
    assert proof["visibility"] == "requires_additional_access"
    assert proof["source_rule_confirmed"] is True
    assert proof["splunk_failed_response_count"] == 1
    assert "Corefile" not in json.dumps(proof)
    assert check_service_dns_failure(configmap, [], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_service_dns_failure(configmap, [{"_time": "2026-09-26T19:00:00Z", "_raw": "Non-2xx or 3xx responses: 12"}], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_service_dns_failure(configmap, [{"_time": "2026-09-26T19:02:05Z", "_raw": "Non-2xx or 3xx responses: 0"}], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_service_dns_failure({"data": {"Corefile": "kubernetes cluster.local"}}, rows, start=START, end=END)["status"] == "missing_at_source"


def test_service_dns_pre_agent_queries_scoped_workload_failed_responses():
    configmap = {"data": {"Corefile": "template ANY ANY user-service.social-network.svc.cluster.local { rcode NXDOMAIN }"}}

    class Backend:
        def query_signal(self, signal, *args):
            assert signal == "logs"
            return 1
        def query_object(self, *args):
            raise AssertionError("generic readiness already checked object delivery")
        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "logs"
            assert kwargs["container_name"] == "wrk2"
            assert kwargs["terms"] == ("Non-2xx",)
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": "Non-2xx or 3xx responses: 12"}]

    assert verify_service_dns_pre_agent(
        Backend(), source_configmap=configmap, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )["status"] == "ready_data_limited"
    assert wait_for_service_dns_pre_agent(
        Backend(), source_reader=lambda: configmap, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"


def test_wrong_pod_selection_gate_requires_both_new_backend_types_in_splunk():
    service = {"metadata": {"name": "frontend", "namespace": "hotel-reservation"},
               "spec": {"selector": {"service-route": "frontend"}, "ports": [{"targetPort": 5000}]}}

    def pod(role, port):
        return {"metadata": {"name": f"{role}-abc", "namespace": "hotel-reservation", "creationTimestamp": "2026-09-26T19:01:45Z",
                             "labels": {"service-route": "frontend", "io.kompose.service": role}},
                "spec": {"containers": [{"ports": [{"containerPort": port}]}]}, "status": {"phase": "Running"}}

    frontend, search = pod("frontend", 5000), pod("search", 8082)
    proof = check_service_wrong_pod_selection(service, [frontend, search], [_row(frontend), _row(search)], start=START, end=END)
    assert proof["status"] == "symptom_confirmed"
    assert proof["visibility"] == "requires_additional_access"
    assert proof["splunk_count"] == 2
    assert proof["remedy"]
    assert check_service_wrong_pod_selection(service, [frontend, search], [_row(frontend)], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_service_wrong_pod_selection(service, [frontend], [_row(frontend), _row(search)], start=START, end=END)["status"] == "missing_at_source"
    assert check_service_wrong_pod_selection({**service, "spec": {"selector": {"io.kompose.service": "frontend"}}}, [frontend, search], [_row(frontend), _row(search)], start=START, end=END)["status"] == "missing_at_source"


def test_wrong_pod_selection_pre_agent_queries_scoped_pod_objects():
    service = {"metadata": {"name": "frontend", "namespace": "hotel-reservation"},
               "spec": {"selector": {"service-route": "frontend"}, "ports": [{"targetPort": 5000}]}}
    frontend = {"metadata": {"name": "frontend-abc", "namespace": "hotel-reservation", "creationTimestamp": "2026-09-26T19:01:45Z",
                             "labels": {"service-route": "frontend", "io.kompose.service": "frontend"}},
                "spec": {"containers": [{"ports": [{"containerPort": 5000}]}]}, "status": {"phase": "Running"}}
    search = {"metadata": {"name": "search-abc", "namespace": "hotel-reservation", "creationTimestamp": "2026-09-26T19:01:45Z",
                           "labels": {"service-route": "frontend", "io.kompose.service": "search"}},
              "spec": {"containers": [{"ports": [{"containerPort": 8082}]}]}, "status": {"phase": "Running"}}

    class Backend:
        def query_signal(self, *args):
            return 1
        def query_object(self, *args):
            return 1
        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "pods"
            assert kwargs["terms"] in {("frontend",), ("search",)}
            return [_row(frontend)] if kwargs["terms"] == ("frontend",) else [_row(search)]

    assert verify_service_wrong_pod_selection_pre_agent(
        Backend(), source_service=service, source_pods=[frontend, search], context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, end=END,
    )["status"] == "ready_data_limited"
    assert wait_for_service_wrong_pod_selection_pre_agent(
        Backend(), source_reader=lambda: (service, [frontend, search]), context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"


def test_internal_traffic_policy_requires_matching_cross_node_pods_and_source_service():
    def pod(component, node):
        return {
            "metadata": {"name": f"{component}-abc-123", "namespace": "astronomy-shop", "creationTimestamp": "2026-09-26T19:01:45Z",
                         "labels": {"app.kubernetes.io/component": component}},
            "spec": {"nodeName": node}, "status": {"phase": "Running"},
        }

    frontend, recommendation = pod("frontend", "kind-worker2"), pod("recommendation", "kind-worker")
    rows = [_row(frontend), _row(recommendation)]
    service = {"metadata": {"name": "recommendation", "namespace": "astronomy-shop"},
               "spec": {"internalTrafficPolicy": "Local"}}
    proof = check_internal_traffic_policy(service, [frontend, recommendation], rows, start=START, end=END)
    assert proof["status"] == "symptom_confirmed"
    assert proof["visibility"] == "requires_additional_access"
    assert proof["source_cross_node"] is True
    assert proof["splunk_cross_node"] is True
    assert proof["splunk_count"] == 2
    assert proof["remedy"]
    assert "kind-worker" not in json.dumps(proof)
    assert check_internal_traffic_policy(service, [frontend, recommendation], rows[:1], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_internal_traffic_policy(service, [frontend, recommendation], [], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_internal_traffic_policy(service, [frontend, pod("recommendation", "kind-worker2")], rows, start=START, end=END)["status"] == "missing_at_source"
    assert check_internal_traffic_policy({**service, "spec": {"internalTrafficPolicy": "Cluster"}}, [frontend, recommendation], rows, start=START, end=END)["status"] == "missing_at_source"


def test_internal_traffic_policy_gate_queries_source_pods_and_scoped_splunk_pods():
    frontend = {"metadata": {"name": "frontend-abc", "namespace": "astronomy-shop", "creationTimestamp": "2026-09-26T19:01:45Z",
                             "labels": {"app.kubernetes.io/component": "frontend"}}, "spec": {"nodeName": "worker2"}, "status": {"phase": "Running"}}
    recommendation = {"metadata": {"name": "recommendation-abc", "namespace": "astronomy-shop", "creationTimestamp": "2026-09-26T19:01:45Z",
                                   "labels": {"app.kubernetes.io/component": "recommendation"}}, "spec": {"nodeName": "worker1"}, "status": {"phase": "Running"}}
    service = {"metadata": {"name": "recommendation", "namespace": "astronomy-shop"}, "spec": {"internalTrafficPolicy": "Local"}}

    class Backend:
        def query_signal(self, *args):
            return 1
        def query_object(self, *args):
            return 1
        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "pods"
            assert kwargs["terms"] in {("frontend",), ("recommendation",)}
            return [_row(frontend)] if kwargs["terms"] == ("frontend",) else [_row(recommendation)]

    proof = verify_internal_traffic_policy_pre_agent(
        Backend(), source_service=service, source_pods=[frontend, recommendation], context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, end=END,
    )
    assert proof["status"] == "ready_data_limited"
    assert wait_for_internal_traffic_policy_pre_agent(
        Backend(), source_reader=lambda: (service, [frontend, recommendation]), context=CONTEXT,
        scope=object(), connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"


def test_network_policy_pre_agent_gate_allows_explicitly_data_limited_run():
    policy = {
        "metadata": {"name": "deny-all-recommendation", "namespace": "hotel-reservation",
                     "creationTimestamp": "2026-09-26T19:01:30Z"},
        "spec": {"podSelector": {"matchLabels": {"io.kompose.service": "recommendation"}},
                 "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []},
    }

    class Backend:
        def query_signal(self, signal, *args):
            return 1

        def query_object(self, kind, *args):
            return 0  # This case need not emit a causal pod/event object.

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "logs"
            assert kwargs["container_name"] == "wrk2"
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": "Socket errors: timeout 186"}]

    proof = verify_network_policy_pre_agent(
        Backend(), source_policy=policy, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )
    assert proof["status"] == "ready_data_limited"
    assert proof["causal"]["visibility"] == "requires_additional_access"
    assert proof["signals"]["pods"] == 0


def test_network_policy_gate_records_unqueryable_apm_traces_without_hiding_verified_timeout_logs():
    policy = {
        "metadata": {"name": "deny-all-recommendation", "namespace": "hotel-reservation",
                     "creationTimestamp": "2026-09-26T19:01:30Z"},
        "spec": {"podSelector": {"matchLabels": {"io.kompose.service": "recommendation"}},
                 "policyTypes": ["Ingress", "Egress"]},
    }

    class Backend:
        def query_signal(self, signal, *args):
            return 0 if signal == "traces" else 1

        def query_object(self, kind, *args):
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": "Socket errors: timeout 186"}]

    proof = verify_network_policy_pre_agent(
        Backend(), source_policy=policy, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )
    assert proof["status"] == "ready_data_limited"
    assert proof["signals"]["traces"] == 0
    assert "traces" not in proof["required_signals"]
    assert "APM" in proof["causal"]["access_gap"]


def test_network_policy_wait_accepts_data_limited_status_as_terminal():
    policy = {
        "metadata": {"name": "deny-all-recommendation", "namespace": "hotel-reservation",
                     "creationTimestamp": "2026-09-26T19:01:30Z"},
        "spec": {"podSelector": {"matchLabels": {"io.kompose.service": "recommendation"}},
                 "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []},
    }

    class Backend:
        def query_signal(self, *args):
            return 1

        def query_object(self, *args):
            return 0

        def query_scoped_examples(self, *args, **kwargs):
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": "Socket errors: timeout 186"}]

    proof = wait_for_network_policy_pre_agent(
        Backend(), source_reader=lambda: policy, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )
    assert proof["status"] == "ready_data_limited"


def test_mutating_webhook_gate_requires_same_new_oomkilled_16mi_pod_in_source_and_splunk():
    pod = {
        "metadata": {"name": "nginx-thrift-abc", "namespace": "social-network",
                     "creationTimestamp": "2026-09-26T19:01:30Z"},
        "spec": {"containers": [{"name": "nginx-thrift", "resources": {
            "requests": {"memory": "16Mi"}, "limits": {"memory": "16Mi"},
        }}]},
        "status": {"containerStatuses": [{"name": "nginx-thrift", "restartCount": 1,
                                          "lastState": {"terminated": {"reason": "OOMKilled"}}}]},
    }
    row = {"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": pod})}
    causal = check_mutating_webhook_pods([pod], [row], start=START, end=END)
    assert causal["status"] == "symptom_confirmed"
    fractional_start = datetime(2026, 9, 26, 19, 1, 30, 500000, tzinfo=UTC)
    assert check_mutating_webhook_pods([pod], [row], start=fractional_start, end=END)["status"] == "symptom_confirmed"
    assert causal["source_count"] == causal["splunk_count"] == 1
    assert causal["visibility"] == "requires_additional_access"
    assert "webhook" in causal["access_gap"].lower()
    with_cpu = deepcopy(pod)
    with_cpu["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "100m"
    with_cpu["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "500m"
    assert check_mutating_webhook_pods(
        [with_cpu], [{"_time": row["_time"], "_raw": json.dumps({"object": with_cpu})}],
        start=START, end=END,
    )["status"] == "symptom_confirmed"
    assert check_mutating_webhook_pods([pod], [], start=START, end=END)["status"] == "missing_in_splunk"
    wrong = deepcopy(pod)
    wrong["spec"]["containers"][0]["resources"]["limits"]["memory"] = "256Mi"
    assert check_mutating_webhook_pods([wrong], [row], start=START, end=END)["status"] == "missing_at_source"
    for malformed in (None, {}, {"metadata": {}, "spec": {}},
                      {**pod, "metadata": {**pod["metadata"], "namespace": "other"}},
                      {**pod, "spec": {"containers": None}}):
        assert check_mutating_webhook_pods([malformed], [row], start=START, end=END)["status"] == "missing_at_source"


def test_mutating_webhook_pre_agent_gate_records_oom_evidence_and_access_gap():
    pod = {
        "metadata": {"name": "nginx-thrift-abc", "namespace": "social-network",
                     "creationTimestamp": "2026-09-26T19:01:30Z"},
        "spec": {"containers": [{"name": "nginx-thrift", "resources": {
            "requests": {"memory": "16Mi"}, "limits": {"memory": "16Mi"},
        }}]},
        "status": {"containerStatuses": [{"name": "nginx-thrift", "restartCount": 1,
                                          "lastState": {"terminated": {"reason": "OOMKilled"}}}]},
    }

    class Backend:
        def query_signal(self, signal, *args):
            return 1

        def query_object(self, kind, *args):
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "pods"
            assert kwargs["terms"] == ("nginx-thrift",)
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": pod})}]

    proof = verify_mutating_webhook_pre_agent(
        Backend(), source_pods=[pod], context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )
    assert proof["status"] == "ready_data_limited"
    assert proof["causal"]["splunk_count"] == 1
    assert wait_for_mutating_webhook_pre_agent(
        Backend(), source_reader=lambda: [pod], context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"


def test_finalizer_deadlock_gate_requires_same_403_controller_log_in_source_and_splunk():
    message = "cleanup-controller reconciliation failed: Kubernetes API denied cleanup request with HTTP 403 Forbidden"
    source = [f"2026-09-26T19:02:05Z {message}"]
    row = {"_time": "2026-09-26T19:02:06Z", "_raw": message}
    causal = check_finalizer_deadlock_logs(source, [row], start=START, end=END)
    assert causal["status"] == "symptom_confirmed"
    prefixed = [f"[cleanup-controller-abc/cleanup-controller] {source[0]}"]
    assert check_finalizer_deadlock_logs(prefixed, [row], start=START, end=END)["status"] == "symptom_confirmed"
    assert causal["source_count"] == causal["splunk_count"] == 1
    assert causal["visibility"] == "requires_additional_access"
    assert "ClusterRole" in causal["access_gap"]
    assert check_finalizer_deadlock_logs(source, [], start=START, end=END)["status"] == "missing_in_splunk"
    assert check_finalizer_deadlock_logs([], [row], start=START, end=END)["status"] == "missing_at_source"
    assert check_finalizer_deadlock_logs([message], [row], start=START, end=END)["status"] == "missing_at_source"
    unrelated = {"_time": row["_time"], "_raw": "unrelated service HTTP 403 Forbidden"}
    assert check_finalizer_deadlock_logs(source, [unrelated], start=START, end=END)["status"] == "missing_in_splunk"


def test_finalizer_deadlock_pre_agent_proof_records_log_symptom_and_kubernetes_gap():
    message = "cleanup-controller reconciliation failed: Kubernetes API denied cleanup request with HTTP 403 Forbidden"

    class Backend:
        def query_signal(self, signal, *args):
            return 0 if signal == "traces" else 1

        def query_object(self, kind, *args):
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "logs"
            assert kwargs["container_name"] == "controller"
            return [{"_time": "2026-09-26T19:02:06Z", "_raw": message}]

    source = [f"2026-09-26T19:02:05Z {message}"]
    proof = verify_finalizer_deadlock_pre_agent(
        Backend(), source_lines=source, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )
    assert proof["status"] == "ready_data_limited"
    assert proof["signals"]["traces"] == 0
    assert "traces" not in proof["required_signals"]
    class TraceBackend(Backend):
        def query_signal(self, signal, *args):
            return 1

    with_traces = verify_finalizer_deadlock_pre_agent(
        TraceBackend(), source_lines=source, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )
    assert with_traces["status"] == "ready_data_limited"
    assert "APM" not in with_traces["causal"]["access_gap"]
    assert wait_for_finalizer_deadlock_pre_agent(
        Backend(), source_reader=lambda: source, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"

    class Backend:
        def query_signal(self, *args):
            raise SplunkBackendError(status_code=400, transient=False)

    with pytest.raises(CaseGateError, match="query rejected"):
        wait_for_cronjob_pre_agent(
            Backend(), source_reader=lambda: [_pod()], context=object(), scope=object(),
            connection_id="logs-connection", start=START, now=lambda: END,
            sleep=lambda _: None, attempts=2,
        )


def test_kafka_gate_requires_both_offset_specific_failure_and_pause_in_source_and_splunk():
    failure = "2026-09-26T19:02:03Z ERROR record validation failed at offset=20: invalid JSON"
    pause = "2026-09-26T19:02:04Z ERROR partition remains paused at offset=20"
    rows = {
        "validation": [{"_time": "2026-09-26T19:02:05Z", "_raw": failure}],
        "paused": [{"_time": "2026-09-26T19:02:06Z", "_raw": pause}],
    }
    proof = check_kafka_poison_logs([failure, pause], rows, start=START, end=END)
    assert proof["status"] == "confirmed"
    assert proof["visibility"] == "fully_splunk_observable"
    assert proof["source_counts"] == {"validation": 1, "paused": 1}
    assert proof["splunk_counts"] == {"validation": 1, "paused": 1}
    assert check_kafka_poison_logs([failure], rows, start=START, end=END)["status"] == "missing_at_source"
    assert check_kafka_poison_logs([failure, pause], {"validation": rows["validation"]}, start=START, end=END)["status"] == "missing_in_splunk"
    wrong_offset = "2026-09-26T19:02:06Z ERROR partition remains paused at offset=19"
    assert check_kafka_poison_logs([failure, wrong_offset], rows, start=START, end=END)["status"] == "missing_at_source"
    assert check_kafka_poison_logs([failure, pause], {
        "validation": rows["validation"], "paused": [{"_time": "2026-09-26T19:02:06Z", "_raw": wrong_offset}],
    }, start=START, end=END)["status"] == "missing_in_splunk"
    assert check_kafka_poison_logs(["not timestamped", failure, pause], rows, start=START, end=END)["source_counts"] == {"validation": 1, "paused": 1}


def test_kafka_gate_accepts_timestamped_kubectl_all_pods_prefix():
    failure = "2026-09-26T19:02:03Z ERROR record validation failed at offset=20: invalid JSON"
    pause = "2026-09-26T19:02:04Z ERROR partition remains paused at offset=20"
    prefix = "[orders-validator-abc/orders-validator] "
    rows = {
        "validation": [{"_time": "2026-09-26T19:02:05Z", "_raw": failure}],
        "paused": [{"_time": "2026-09-26T19:02:06Z", "_raw": pause}],
    }
    proof = check_kafka_poison_logs([prefix + failure, prefix + pause], rows, start=START, end=END)
    assert proof["status"] == "confirmed"
    assert proof["source_counts"] == {"validation": 1, "paused": 1}


def test_kafka_pre_agent_fetches_two_independent_log_signatures():
    failure = "record validation failed at offset=20: invalid JSON"
    pause = "partition remains paused at offset=20"

    class Backend:
        def query_signal(self, *args):
            return 1

        def query_object(self, *args):
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "logs"
            assert kwargs["container_name"] == "orders-validator"
            terms = kwargs["terms"]
            if terms == ("validation", "offset=20"):
                return [{"_time": "2026-09-26T19:02:05Z", "_raw": failure}]
            assert terms == ("paused", "offset=20")
            return [{"_time": "2026-09-26T19:02:06Z", "_raw": pause}]

    proof = verify_kafka_pre_agent(
        Backend(), source_lines=["2026-09-26T19:02:03Z " + failure, "2026-09-26T19:02:04Z " + pause],
        context=CONTEXT, scope=object(), connection_id="synthetic-logs", start=START, end=END,
    )
    assert proof["status"] == "ready"
    assert proof["causal"]["check_id"] == "kafka_poison_offset_and_paused_partition"
    waited = wait_for_kafka_pre_agent(
        Backend(), source_reader=lambda: ["2026-09-26T19:02:03Z " + failure,
                                          "2026-09-26T19:02:04Z " + pause],
        context=CONTEXT, scope=object(), connection_id="synthetic-logs", start=START,
        now=lambda: END, sleep=lambda _: None, attempts=1,
    )
    assert waited["status"] == "ready"


def test_kafka_causal_gate_does_not_repeat_noncausal_signal_queries_after_provider_readiness():
    failure = "record validation failed at offset=20: invalid JSON"
    pause = "partition remains paused at offset=20"

    class Backend:
        def query_signal(self, signal, *args):
            assert signal == "logs"
            return 1

        def query_object(self, *args):
            raise AssertionError("Kafka causal gate must not re-query unrelated objects")

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "logs"
            content = failure if kwargs["terms"][0] == "validation" else pause
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": content}]

    proof = verify_kafka_pre_agent(
        Backend(), source_lines=["2026-09-26T19:02:03Z " + failure,
                                 "2026-09-26T19:02:04Z " + pause],
        context=CONTEXT, scope=object(), connection_id="synthetic-logs", start=START, end=END,
    )
    assert proof["status"] == "ready"
    assert proof["required_signals"] == ["logs"]
    assert proof["signals"] == {"logs": 1}


def test_kafka_pre_agent_accepts_causal_logs_from_a_bounded_slow_splunk_search():
    failure = "record validation failed at offset=20: invalid JSON"
    pause = "partition remains paused at offset=20"
    timeouts = []

    class Backend:
        def query_signal(self, signal, *args):
            assert signal == "logs"
            timeouts.append(args[-1])
            if args[-1] < 15.0:
                raise SplunkBackendError(status_code=None, transient=True)
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "logs"
            content = failure if kwargs["terms"][0] == "validation" else pause
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": content}]

    proof = wait_for_kafka_pre_agent(
        Backend(), source_reader=lambda: ["2026-09-26T19:02:03Z " + failure,
                                          "2026-09-26T19:02:04Z " + pause],
        context=CONTEXT, scope=object(), connection_id="synthetic-logs", start=START,
        now=lambda: END, sleep=lambda _: None, attempts=3, max_wait_seconds=60.0,
    )
    assert proof["status"] == "ready"
    assert proof["causal"]["check_id"] == "kafka_poison_offset_and_paused_partition"
    assert timeouts and all(15.0 <= timeout <= 30.0 for timeout in timeouts)


def test_readiness_case_requires_wrong_probe_and_not_ready_in_same_new_pod_at_source_and_splunk():
    pod = {
        "metadata": {"name": "user-service-abc-123", "namespace": "social-network",
                     "creationTimestamp": "2026-09-26T19:01:45Z"},
        "spec": {"containers": [{"name": "user-service", "readinessProbe": {
            "httpGet": {"path": "/healthz", "port": 8080},
        }}]},
        "status": {"conditions": [{"type": "Ready", "status": "False"}]},
    }
    rows = [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": pod})}]
    proof = check_readiness_probe_pods([pod], rows, start=START, end=END)
    assert proof["status"] == "confirmed"
    assert proof["visibility"] == "fully_splunk_observable"
    assert proof["matched_pod"] == "user-service-abc-123"
    defaulted = deepcopy(pod)
    defaulted["spec"]["containers"][0]["readinessProbe"]["httpGet"]["scheme"] = "HTTP"
    assert check_readiness_probe_pods(
        [defaulted], [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": defaulted})}],
        start=START, end=END,
    )["status"] == "confirmed"
    assert check_readiness_probe_pods([pod], [], start=START, end=END)["status"] == "missing_in_splunk"
    ready = deepcopy(pod)
    ready["status"]["conditions"][0]["status"] = "True"
    assert check_readiness_probe_pods([ready], rows, start=START, end=END)["status"] == "missing_at_source"
    wrong_probe = deepcopy(pod)
    wrong_probe["spec"]["containers"][0]["readinessProbe"]["httpGet"]["path"] = "/health"
    assert check_readiness_probe_pods([pod], [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": wrong_probe})}], start=START, end=END)["status"] == "missing_in_splunk"
    malformed = deepcopy(pod)
    malformed["status"] = None
    wrong_name = deepcopy(pod)
    wrong_name["metadata"]["name"] = "frontend-123"
    no_conditions = deepcopy(pod)
    no_conditions["status"]["conditions"] = None
    for candidate in (None, malformed, wrong_name, no_conditions):
        assert check_readiness_probe_pods([candidate], rows, start=START, end=END)["status"] == "missing_at_source"
    bad_rows = [
        {"_time": "2026-09-26T19:00:00Z", "_raw": json.dumps({"object": pod})},
        {"_time": "2026-09-26T19:02:00Z", "_raw": "{bad json"},
        {"_time": "2026-09-26T19:02:01Z", "_raw": "[]"},
    ]
    assert check_readiness_probe_pods([pod], bad_rows, start=START, end=END)["status"] == "missing_in_splunk"


def test_readiness_pre_agent_queries_scoped_pod_object():
    pod = {
        "metadata": {"name": "user-service-abc-123", "namespace": "social-network",
                     "creationTimestamp": "2026-09-26T19:01:45Z"},
        "spec": {"containers": [{"name": "user-service", "readinessProbe": {
            "httpGet": {"path": "/healthz", "port": 8080},
        }}]},
        "status": {"conditions": [{"type": "Ready", "status": "False"}]},
    }

    class Backend:
        def query_signal(self, *args):
            return 1

        def query_object(self, *args):
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "pods"
            assert kwargs["terms"] == ("user-service",)
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": pod})}]

    proof = verify_readiness_pre_agent(
        Backend(), source_pods=[pod], context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )
    assert proof["status"] == "ready"
    assert wait_for_readiness_pre_agent(
        Backend(), source_reader=lambda: [pod], context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready"


def test_env_shadowing_requires_ordered_duplicate_values_in_same_new_pod_at_source_and_splunk():
    pod = {
        "metadata": {"name": "frontend-proxy-abc-123", "namespace": "astronomy-shop",
                     "creationTimestamp": "2026-09-26T19:01:45Z"},
        "spec": {"containers": [{"name": "frontend-proxy", "env": [
            {"name": "FRONTEND_HOST", "value": "frontend"},
            {"name": "OTHER", "value": "x"},
            {"name": "FRONTEND_HOST", "value": "localhost"},
        ]}]},
        "status": {"phase": "Running"},
    }
    rows = [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": pod})}]
    proof = check_env_shadowing_pods([pod], rows, start=START, end=END)
    assert proof["status"] == "confirmed"
    assert proof["visibility"] == "fully_splunk_observable"
    assert proof["matched_pod"] == "frontend-proxy-abc-123"
    assert "localhost" not in json.dumps(proof)
    assert check_env_shadowing_pods([pod], [], start=START, end=END)["status"] == "missing_in_splunk"
    no_shadow = deepcopy(pod)
    no_shadow["spec"]["containers"][0]["env"].pop()
    assert check_env_shadowing_pods([no_shadow], rows, start=START, end=END)["status"] == "missing_at_source"
    wrong_order = deepcopy(pod)
    wrong_order["spec"]["containers"][0]["env"].reverse()
    assert check_env_shadowing_pods([pod], [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": wrong_order})}], start=START, end=END)["status"] == "missing_in_splunk"
    malformed = deepcopy(pod)
    malformed["spec"] = None
    wrong_name = deepcopy(pod)
    wrong_name["metadata"]["name"] = "frontend-123"
    no_containers = deepcopy(pod)
    no_containers["spec"]["containers"] = None
    wrong_container = deepcopy(pod)
    wrong_container["spec"]["containers"][0]["name"] = "frontend"
    no_env = deepcopy(pod)
    no_env["spec"]["containers"][0]["env"] = None
    for candidate in (None, malformed, wrong_name, no_containers, wrong_container, no_env):
        assert check_env_shadowing_pods([candidate], rows, start=START, end=END)["status"] == "missing_at_source"


def test_env_shadowing_pre_agent_queries_only_scoped_pod_object():
    pod = {
        "metadata": {"name": "frontend-proxy-abc-123", "namespace": "astronomy-shop",
                     "creationTimestamp": "2026-09-26T19:01:45Z"},
        "spec": {"containers": [{"name": "frontend-proxy", "env": [
            {"name": "FRONTEND_HOST", "value": "frontend"},
            {"name": "FRONTEND_HOST", "value": "localhost"},
        ]}]},
    }

    class Backend:
        def query_signal(self, *args):
            return 1

        def query_object(self, *args):
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "pods"
            assert kwargs["terms"] == ("frontend-proxy",)
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": pod})}]

    assert verify_env_shadowing_pre_agent(
        Backend(), source_pods=[pod], context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )["status"] == "ready"
    assert wait_for_env_shadowing_pre_agent(
        Backend(), source_reader=lambda: [pod], context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready"


def test_wrong_dns_policy_requires_external_resolver_in_same_new_frontend_pod_at_source_and_splunk():
    pod = {
        "metadata": {"name": "frontend-abc-123", "namespace": "astronomy-shop",
                     "creationTimestamp": "2026-09-26T19:01:45Z"},
        "spec": {"dnsPolicy": "None", "dnsConfig": {"nameservers": ["8.8.8.8"], "searches": []}},
    }
    rows = [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": pod})}]
    proof = check_wrong_dns_policy_pods([pod], rows, start=START, end=END)
    assert proof["status"] == "confirmed"
    assert proof["visibility"] == "fully_splunk_observable"
    assert check_wrong_dns_policy_pods([pod], [], start=START, end=END)["status"] == "missing_in_splunk"
    correct = deepcopy(pod)
    correct["spec"]["dnsPolicy"] = "ClusterFirst"
    assert check_wrong_dns_policy_pods([correct], rows, start=START, end=END)["status"] == "missing_at_source"
    assert check_wrong_dns_policy_pods([pod], [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": correct})}], start=START, end=END)["status"] == "missing_in_splunk"
    malformed = deepcopy(pod)
    malformed["spec"] = None
    wrong_name = deepcopy(pod)
    wrong_name["metadata"]["name"] = "other-123"
    for candidate in (None, malformed, wrong_name):
        assert check_wrong_dns_policy_pods([candidate], rows, start=START, end=END)["status"] == "missing_at_source"


def test_wrong_dns_policy_pre_agent_queries_only_scoped_pod_object():
    pod = {
        "metadata": {"name": "frontend-abc-123", "namespace": "astronomy-shop",
                     "creationTimestamp": "2026-09-26T19:01:45Z"},
        "spec": {"dnsPolicy": "None", "dnsConfig": {"nameservers": ["8.8.8.8"], "searches": []}},
    }

    class Backend:
        def query_signal(self, *args):
            return 1

        def query_object(self, *args):
            return 1

        def query_scoped_examples(self, kind, *args, **kwargs):
            assert kind == "pods"
            assert kwargs["terms"] == ("frontend",)
            return [{"_time": "2026-09-26T19:02:05Z", "_raw": json.dumps({"object": pod})}]

    assert verify_wrong_dns_policy_pre_agent(
        Backend(), source_pods=[pod], context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )["status"] == "ready"
    assert wait_for_wrong_dns_policy_pre_agent(
        Backend(), source_reader=lambda: [pod], context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready"


def test_search_retry_gate_requires_backlog_and_three_named_metrics_at_source_and_splunk():
    source = {"rate_queue_depth": 42, "search_requests_total": 900,
              "search_rate_attempts_total": 1600}
    splunk = {"rate_queue_depth": 36, "search_requests_total": 880,
              "search_rate_attempts_total": 1530}
    proof = check_search_retry_metrics(source, splunk)
    assert proof["status"] == "symptom_confirmed"
    assert proof["source_backlog_confirmed"] is True
    assert proof["splunk_backlog_confirmed"] is True
    assert "ratio" not in json.dumps(proof).lower()  # cumulative values do not prove post-trigger amplification
    assert check_search_retry_metrics({**source, "rate_queue_depth": 0}, splunk)["status"] == "missing_at_source"
    assert check_search_retry_metrics(source, {**splunk, "rate_queue_depth": 0})["status"] == "missing_in_splunk"
    assert check_search_retry_metrics(source, {"rate_queue_depth": 36})["status"] == "missing_in_splunk"


def test_search_retry_pre_agent_queries_only_scoped_application_metrics():
    source = {"rate_queue_depth": 42, "search_requests_total": 900,
              "search_rate_attempts_total": 1600}

    class Backend:
        def query_signal(self, signal, *args):
            assert signal == "metrics"
            return 1

        def query_application_metric_values(self, names, context, scope, end, timeout):
            assert names == ("rate_queue_depth", "search_requests_total", "search_rate_attempts_total")
            assert context == CONTEXT and end == END
            return {"rate_queue_depth": 36, "search_requests_total": 880,
                    "search_rate_attempts_total": 1530}

    proof = verify_search_retry_pre_agent(
        Backend(), source_metrics=source, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, end=END,
    )
    assert proof["status"] == "ready_data_limited"
    assert proof["required_signals"] == ["metrics"]
    assert wait_for_search_retry_pre_agent(
        Backend(), source_reader=lambda: source, context=CONTEXT, scope=object(),
        connection_id="synthetic-logs", start=START, now=lambda: END,
        sleep=lambda _: None, attempts=1,
    )["status"] == "ready_data_limited"
