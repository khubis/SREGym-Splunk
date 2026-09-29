"""Small, secret-free predicates for fault evidence visible to the eval runner.

The runner may use the oracle to define these checks, but must keep raw objects
and this proof outside the agent-mounted directory until the agent has exited.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import UTC, datetime
from typing import Any, Callable

from sregym.observability.base import ApplicationScope, AttemptContext, SignalName
from sregym.observability.splunk import SplunkBackendError


_REPRESENTATIVE_SIGNALS: tuple[SignalName, ...] = ("metrics", "traces", "logs", "kubernetes_events")


class CausalSourceError(RuntimeError):
    """A source-side read failed without exposing Kubernetes output."""


class CaseGateError(RuntimeError):
    """A required pre-agent evidence check did not become trustworthy."""

    def __init__(self, reason: str, proof: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.proof = proof


def read_source_pods(
    namespace: str,
    *,
    context: str,
    execute: Callable[..., Any] = subprocess.run,
) -> list[dict[str, Any]]:
    """Read namespace pods from one explicitly selected local Kind cluster."""
    if re.fullmatch(r"kind-[A-Za-z0-9_.-]+", context) is None:
        raise CausalSourceError("source context is not an explicit Kind context")
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", namespace) is None:
        raise CausalSourceError("invalid source namespace")
    try:
        completed = execute(
            ["kubectl", "--context", context, "-n", namespace, "get", "pods", "-o", "json"],
            shell=False, capture_output=True, text=True, check=True, timeout=15,
        )
        payload = json.loads(completed.stdout)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError("invalid pod list")
        return items
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        raise CausalSourceError("source pod query failed") from None


def read_source_events(
    namespace: str,
    *,
    context: str,
    execute: Callable[..., Any] = subprocess.run,
) -> list[dict[str, Any]]:
    """Read namespace events for runner-side source/Splunk identity checks."""
    if re.fullmatch(r"kind-[A-Za-z0-9_.-]+", context) is None:
        raise CausalSourceError("source context is not an explicit Kind context")
    if re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", namespace) is None:
        raise CausalSourceError("invalid source namespace")
    try:
        completed = execute(
            ["kubectl", "--context", context, "-n", namespace, "get", "events", "-o", "json"],
            shell=False, capture_output=True, text=True, check=True, timeout=15,
        )
        payload = json.loads(completed.stdout)
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ValueError("invalid event list")
        return items
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        raise CausalSourceError("source event query failed") from None


def read_source_logs(
    namespace: str,
    deployment: str,
    *,
    context: str,
    start: datetime,
    execute: Callable[..., Any] = subprocess.run,
) -> list[str]:
    """Read a bounded, timestamped source log sample from the selected Kind workload."""
    if re.fullmatch(r"kind-[A-Za-z0-9_.-]+", context) is None:
        raise CausalSourceError("source context is not an explicit Kind context")
    if any(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", name) is None for name in (namespace, deployment)):
        raise CausalSourceError("invalid source workload")
    if start.tzinfo is None or start.utcoffset() != UTC.utcoffset(start):
        raise CausalSourceError("source log window must start in UTC")
    start_text = start.isoformat().replace("+00:00", "Z")
    try:
        completed = execute(
            [
                "kubectl", "--context", context, "-n", namespace, "logs",
                f"deployment/{deployment}", f"--since-time={start_text}",
                "--timestamps", "--tail=1000", "--limit-bytes=2097152",
                "--all-pods=true", "--max-log-requests=5",
            ],
            shell=False, capture_output=True, text=True, check=True, timeout=15,
        )
        return completed.stdout.splitlines()
    except (OSError, subprocess.SubprocessError, AttributeError):
        raise CausalSourceError("source log query failed") from None


def read_source_object(
    kind: str,
    name: str,
    *,
    namespace: str,
    context: str,
    execute: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Read one allowlisted Kubernetes object only for runner-side proof."""
    if kind not in {"networkpolicy", "service", "endpoints", "configmap", "resourcequota", "deployment", "persistentvolumeclaim"}:
        raise CausalSourceError("source object kind is not allowlisted")
    if re.fullmatch(r"kind-[A-Za-z0-9_.-]+", context) is None:
        raise CausalSourceError("source context is not an explicit Kind context")
    if any(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", item) is None for item in (namespace, name)):
        raise CausalSourceError("invalid source object identity")
    if kind == "configmap" and (namespace, name) != ("kube-system", "coredns"):
        raise CausalSourceError("source object identity is not allowlisted")
    if kind == "endpoints" and (namespace, name) != ("social-network", "user-service"):
        raise CausalSourceError("source object identity is not allowlisted")
    if kind == "resourcequota" and (namespace, name) != ("hotel-reservation", "memory-limit-quota"):
        raise CausalSourceError("source object identity is not allowlisted")
    if kind == "deployment" and (namespace, name) not in {
        ("astronomy-shop", "product-catalog"), ("social-network", "custom-service"),
    }:
        raise CausalSourceError("source object identity is not allowlisted")
    if kind == "persistentvolumeclaim" and (namespace, name) != ("social-network", "jaeger-pvc"):
        raise CausalSourceError("source object identity is not allowlisted")
    try:
        completed = execute(
            ["kubectl", "--context", context, "-n", namespace, "get", kind, name, "-o", "json"],
            shell=False, capture_output=True, text=True, check=True, timeout=15,
        )
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ValueError("invalid object")
        return payload
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        raise CausalSourceError("source object query failed") from None


def read_source_cluster_object(
    kind: str,
    name: str,
    *,
    context: str,
    execute: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Read only the reviewed cluster-scoped webhook configuration for proof."""
    if (kind, name) != ("validatingwebhookconfiguration", "pod-policy.validation.k8s.io"):
        raise CausalSourceError("source cluster object is not allowlisted")
    if re.fullmatch(r"kind-[A-Za-z0-9_.-]+", context) is None:
        raise CausalSourceError("source context is not an explicit Kind context")
    try:
        completed = execute(
            ["kubectl", "--context", context, "get", kind, name, "-o", "json"],
            shell=False, capture_output=True, text=True, check=True, timeout=15,
        )
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ValueError("invalid object")
        return payload
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        raise CausalSourceError("source cluster object query failed") from None


def _sidecar_pod_identity(value: Any) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    metadata = value.get("metadata")
    spec = value.get("spec")
    status = value.get("status")
    if not isinstance(metadata, dict) or not isinstance(spec, dict) or not isinstance(status, dict):
        return None
    name = metadata.get("name")
    if not isinstance(name, str) or not name.startswith("audit-log-archiver-"):
        return None
    if metadata.get("namespace") != "hotel-reservation":
        return None
    owners = metadata.get("ownerReferences")
    if not isinstance(owners, list):
        return None
    jobs = [item.get("name") for item in owners if isinstance(item, dict) and item.get("kind") == "Job"]
    if len(jobs) != 1 or not isinstance(jobs[0], str) or not jobs[0].startswith("audit-log-archiver-"):
        return None
    containers = spec.get("containers")
    if not isinstance(containers, list):
        return None
    regular = {item.get("name") for item in containers if isinstance(item, dict)}
    if not {"archiver", "fluent-bit-sidecar"}.issubset(regular):
        return None
    statuses = status.get("containerStatuses")
    if not isinstance(statuses, list):
        return None
    by_name = {item.get("name"): item.get("state") for item in statuses if isinstance(item, dict)}
    archiver_state = by_name.get("archiver")
    sidecar_state = by_name.get("fluent-bit-sidecar")
    if not isinstance(archiver_state, dict) or not isinstance(sidecar_state, dict):
        return None
    terminated = archiver_state.get("terminated")
    if not isinstance(terminated, dict) or terminated.get("reason") != "Completed":
        return None
    if not isinstance(sidecar_state.get("running"), dict):
        return None
    return name, jobs[0]


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _created_in_window(pod: dict[str, Any], start: datetime, end: datetime) -> bool:
    metadata = pod.get("metadata")
    created = _timestamp(metadata.get("creationTimestamp")) if isinstance(metadata, dict) else None
    # Kubernetes creation timestamps are rounded to whole seconds, while the
    # runner's injection boundary retains microseconds.
    return created is not None and start.replace(microsecond=0) <= created <= end


def check_cronjob_sidecar(
    source_pods: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Require the completed archiver and running regular sidecar in one pod.

    A generic log/metric/trace count cannot establish this mechanism. The
    Splunk query supplying rows must also enforce the run ID and namespace.
    """
    source_identities = {
        identity for pod in source_pods
        if (identity := _sidecar_pod_identity(pod)) and _created_in_window(pod, start, end)
    }
    matches: list[tuple[str, str, datetime]] = []
    for row in splunk_rows:
        timestamp = _timestamp(row.get("_time"))
        if timestamp is None or not start <= timestamp <= end:
            continue
        raw = row.get("_raw")
        try:
            envelope = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not isinstance(envelope, dict):
            continue
        pod = envelope.get("object", envelope)
        identity = _sidecar_pod_identity(pod)
        if identity is not None and identity in source_identities and _created_in_window(pod, start, end):
            matches.append((*identity, timestamp))
    first = min(matches, key=lambda item: item[2]) if matches else None
    source_jobs = {owner for _, owner in source_identities}
    splunk_jobs = {owner for _, owner, _ in matches}
    return {
        "check_id": "cronjob_regular_sidecar_stays_running",
        "signal": "kubernetes_pod_object",
        "status": (
            "missing_at_source" if not source_identities else
            "missing_in_splunk" if first is None else
            "partial_recurrence" if len(source_jobs) < 2 or len(splunk_jobs) < 2 else
            "confirmed"
        ),
        "source_count": len(source_identities),
        "splunk_count": len(matches),
        "source_job_count": len(source_jobs),
        "splunk_job_count": len(splunk_jobs),
        "matched_pod": first[0] if first else None,
        "matched_at": first[2].isoformat().replace("+00:00", "Z") if first else None,
        "interpretation": "Distinct Jobs show Completed archivers and running regular fluent-bit sidecars.",
    }


def _edge_event(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    if value.get("event") == "request_filter_eval":
        return value
    for key in ("message", "body", "log"):
        nested = value.get(key)
        if isinstance(nested, str):
            event = _edge_event(nested)
            if event is not None:
                return event
    return None


def _is_slow_bad_rule(event: dict[str, Any] | None) -> bool:
    if event is None or event.get("rule") != "^([a-zA-Z]+)*$":
        return False
    length = event.get("candidateLength")
    elapsed = event.get("elapsedSeconds")
    return (
        isinstance(length, (int, float)) and not isinstance(length, bool) and length >= 5000
        and isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and elapsed >= 1
    )


def check_edge_waf_log(
    source_lines: list[str],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Require the same slow pathological request-filter event in source and Splunk."""
    source_matches = []
    for line in source_lines:
        before_event, _, remainder = line.partition("{")
        timestamp = next((parsed for token in before_event.split() if (parsed := _timestamp(token)) is not None), None)
        if timestamp is None or not start <= timestamp <= end:
            continue
        if remainder and _is_slow_bad_rule(_edge_event("{" + remainder)):
            source_matches.append(timestamp)
    splunk_matches = []
    for row in splunk_rows:
        timestamp = _timestamp(row.get("_time"))
        if timestamp is not None and start <= timestamp <= end and _is_slow_bad_rule(_edge_event(row.get("_raw"))):
            splunk_matches.append(timestamp)
    return {
        "check_id": "edge_slow_pathological_request_filter",
        "signal": "container_logs",
        "status": (
            "missing_at_source" if not source_matches else
            "missing_in_splunk" if not splunk_matches else "confirmed"
        ),
        "source_count": len(source_matches),
        "splunk_count": len(splunk_matches),
        "source_first_at": min(source_matches).isoformat().replace("+00:00", "Z") if source_matches else None,
        "splunk_first_at": min(splunk_matches).isoformat().replace("+00:00", "Z") if splunk_matches else None,
        "interpretation": "A near-match request repeatedly spends at least one second in the edge request filter.",
    }


def check_network_policy_symptom(
    source_policy: dict[str, Any] | None,
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Confirm source policy and observable timeouts, without claiming causal visibility."""
    metadata = source_policy.get("metadata") if isinstance(source_policy, dict) else None
    spec = source_policy.get("spec") if isinstance(source_policy, dict) else None
    policy_types = spec.get("policyTypes") if isinstance(spec, dict) else None
    created = _timestamp(metadata.get("creationTimestamp")) if isinstance(metadata, dict) else None
    source_confirmed = bool(
        isinstance(metadata, dict)
        and metadata.get("name") == "deny-all-recommendation"
        and metadata.get("namespace") == "hotel-reservation"
        # The Kubernetes API reports policy creation at whole-second
        # precision; injection start can have microseconds in that same second.
        and created is not None and start.replace(microsecond=0) <= created <= end
        and isinstance(spec, dict)
        and spec.get("podSelector") == {"matchLabels": {"io.kompose.service": "recommendation"}}
        and isinstance(policy_types, list) and len(policy_types) == 2
        and all(isinstance(item, str) for item in policy_types)
        and set(policy_types) == {"Ingress", "Egress"}
        # Kubernetes may omit empty ingress/egress arrays when returning the
        # object, though the submitted policy explicitly had both empty.
        and spec.get("ingress", []) == [] and spec.get("egress", []) == []
    )
    timeout_times = []
    for row in splunk_rows:
        timestamp = _timestamp(row.get("_time"))
        raw = row.get("_raw")
        match = re.search(r"\btimeout\s+(\d+)\b", raw) if isinstance(raw, str) else None
        if timestamp is not None and start <= timestamp <= end and match is not None and int(match.group(1)) > 0:
            timeout_times.append(timestamp)
    return {
        "check_id": "network_policy_source_and_request_timeout",
        "signal": "container_logs",
        "status": (
            "missing_at_source" if not source_confirmed else
            "missing_in_splunk" if not timeout_times else "symptom_confirmed"
        ),
        "source_policy_confirmed": source_confirmed,
        "splunk_timeout_count": len(timeout_times),
        "splunk_first_at": min(timeout_times).isoformat().replace("+00:00", "Z") if timeout_times else None,
        "visibility": "requires_additional_access",
        "access_gap": "The approved pod/event export omits NetworkPolicy selector and ingress/egress rules.",
        "remedy": "Read the NetworkPolicy through Kubernetes, or separately authorize that object type in the receiver.",
    }


_FINALIZER_DENIAL = (
    "cleanup-controller reconciliation failed: "
    "Kubernetes API denied cleanup request with HTTP 403 Forbidden"
)


def _prefixed_log_timestamp(line: str) -> datetime | None:
    for token in line.split()[:4]:
        parsed = _timestamp(token)
        if parsed is not None:
            return parsed
    return None


def check_finalizer_deadlock_logs(
    source_lines: list[str],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Match the controller's precise RBAC denial in source and Splunk logs."""
    source_times = [
        timestamp
        for line in source_lines
        if _FINALIZER_DENIAL in line
        for timestamp in [_prefixed_log_timestamp(line)]
        if timestamp is not None and start <= timestamp <= end
    ]
    splunk_times = [
        timestamp
        for row in splunk_rows
        if isinstance(row.get("_raw"), str) and _FINALIZER_DENIAL in row["_raw"]
        for timestamp in [_timestamp(row.get("_time"))]
        if timestamp is not None and start <= timestamp <= end
    ]
    return {
        "check_id": "cleanup_controller_finalizer_patch_403",
        "signal": "container_logs",
        "status": (
            "missing_at_source" if not source_times else
            "missing_in_splunk" if not splunk_times else "symptom_confirmed"
        ),
        "source_count": len(source_times),
        "splunk_count": len(splunk_times),
        "splunk_first_at": min(splunk_times).isoformat().replace("+00:00", "Z") if splunk_times else None,
        "visibility": "requires_additional_access",
        "access_gap": "The approved export omits the ConfigMap finalizer and the controller ClusterRole verbs.",
        "remedy": "Read the ConfigMap and ClusterRole through Kubernetes, or authorize those object types for export.",
        "interpretation": "The cleanup controller repeatedly receives HTTP 403 while reconciling deletion.",
    }


_KAFKA_LOG_SIGNATURES = {
    "validation": re.compile(r"\brecord validation failed at offset=20\b"),
    "paused": re.compile(r"\bpartition remains paused at offset=20\b"),
}


def check_kafka_poison_logs(
    source_lines: list[str],
    splunk_rows: dict[str, list[dict[str, Any]]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Prove the poison offset and paused partition in both log destinations."""
    source_counts = {name: 0 for name in _KAFKA_LOG_SIGNATURES}
    splunk_counts = {name: 0 for name in _KAFKA_LOG_SIGNATURES}
    first_splunk: list[datetime] = []
    for line in source_lines:
        timestamp = _prefixed_log_timestamp(line)
        if timestamp is None or not start <= timestamp <= end:
            continue
        for name, pattern in _KAFKA_LOG_SIGNATURES.items():
            source_counts[name] += bool(pattern.search(line))
    for name, pattern in _KAFKA_LOG_SIGNATURES.items():
        for row in splunk_rows.get(name, []):
            timestamp = _timestamp(row.get("_time"))
            raw = row.get("_raw")
            if timestamp is not None and start <= timestamp <= end and isinstance(raw, str) and pattern.search(raw):
                splunk_counts[name] += 1
                first_splunk.append(timestamp)
    return {
        "check_id": "kafka_poison_offset_and_paused_partition",
        "signal": "container_logs",
        "status": (
            "missing_at_source" if min(source_counts.values()) == 0 else
            "missing_in_splunk" if min(splunk_counts.values()) == 0 else "confirmed"
        ),
        "source_counts": source_counts,
        "splunk_counts": splunk_counts,
        "splunk_first_at": min(first_splunk).isoformat().replace("+00:00", "Z") if first_splunk else None,
        "visibility": "fully_splunk_observable",
        "interpretation": "The same consumer rejects offset 20 and leaves its partition paused; Kafka offset inspection is corroboration.",
        "optional_corroboration": "Kafka consumer-group offset/lag and the record payload require Kafka data-plane access.",
    }


def _readiness_pod_name(pod: Any) -> str | None:
    if not isinstance(pod, dict):
        return None
    metadata, spec, status = (pod.get(key) for key in ("metadata", "spec", "status"))
    if not isinstance(metadata, dict) or not isinstance(spec, dict) or not isinstance(status, dict):
        return None
    name = metadata.get("name")
    if metadata.get("namespace") != "social-network" or not isinstance(name, str) or not name.startswith("user-service-"):
        return None
    containers = spec.get("containers")
    conditions = status.get("conditions")
    if not isinstance(containers, list) or not isinstance(conditions, list):
        return None
    wrong_probe = any(
        isinstance(container, dict)
        and container.get("name") == "user-service"
        and isinstance(container.get("readinessProbe"), dict)
        and isinstance(container["readinessProbe"].get("httpGet"), dict)
        and container["readinessProbe"]["httpGet"].get("path") == "/healthz"
        and container["readinessProbe"]["httpGet"].get("port") == 8080
        and container["readinessProbe"]["httpGet"].get("scheme", "HTTP") == "HTTP"
        for container in containers
    )
    not_ready = any(
        isinstance(condition, dict) and condition.get("type") == "Ready" and condition.get("status") == "False"
        for condition in conditions
    )
    return name if wrong_probe and not_ready else None


def _matching_pod_rows(
    source_pods: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    identity: Callable[[Any], str | None],
    start: datetime,
    end: datetime,
) -> tuple[set[str], list[tuple[str, datetime]]]:
    source_names = {
        name for pod in source_pods
        if (name := identity(pod)) and _created_in_window(pod, start, end)
    }
    matches: list[tuple[str, datetime]] = []
    for row in splunk_rows:
        timestamp = _timestamp(row.get("_time"))
        if timestamp is None or not start <= timestamp <= end:
            continue
        raw = row.get("_raw")
        try:
            envelope = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not isinstance(envelope, dict):
            continue
        pod = envelope.get("object", envelope)
        name = identity(pod)
        if name in source_names and isinstance(pod, dict) and _created_in_window(pod, start, end):
            matches.append((name, timestamp))
    return source_names, matches


def _topology_pod_identity(value: Any, component: str) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    metadata, spec, status = value.get("metadata"), value.get("spec"), value.get("status")
    if not all(isinstance(part, dict) for part in (metadata, spec, status)):
        return None
    labels = metadata.get("labels")
    if (metadata.get("namespace") != "astronomy-shop" or not isinstance(labels, dict)
            or labels.get("app.kubernetes.io/component") != component
            or status.get("phase") != "Running"):
        return None
    name, node = metadata.get("name"), spec.get("nodeName")
    return (name, node) if isinstance(name, str) and isinstance(node, str) and name and node else None


def check_internal_traffic_policy(
    source_service: dict[str, Any],
    source_pods: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Prove source Local policy and Splunk-visible cross-node caller/backend placement."""
    service_ok = (
        isinstance(source_service, dict)
        and isinstance(source_service.get("metadata"), dict)
        and source_service["metadata"].get("name") == "recommendation"
        and source_service["metadata"].get("namespace") == "astronomy-shop"
        and isinstance(source_service.get("spec"), dict)
        and source_service["spec"].get("internalTrafficPolicy") == "Local"
    )
    source: dict[str, dict[tuple[str, str], dict[str, Any]]] = {"frontend": {}, "recommendation": {}}
    for pod in source_pods:
        for component in source:
            identity = _topology_pod_identity(pod, component)
            if identity and _created_in_window(pod, start, end):
                source[component][identity] = pod
    source_cross_node = any(
        frontend[1] != recommendation[1]
        for frontend in source["frontend"] for recommendation in source["recommendation"]
    )
    matched: dict[str, set[tuple[str, str]]] = {"frontend": set(), "recommendation": set()}
    for row in splunk_rows:
        timestamp = _timestamp(row.get("_time"))
        if timestamp is None or not start <= timestamp <= end:
            continue
        raw = row.get("_raw")
        try:
            envelope = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not isinstance(envelope, dict):
            continue
        pod = envelope.get("object", envelope)
        for component in matched:
            identity = _topology_pod_identity(pod, component)
            if identity in source[component] and _created_in_window(pod, start, end):
                matched[component].add(identity)
    splunk_cross_node = any(
        frontend[1] != recommendation[1]
        for frontend in matched["frontend"] for recommendation in matched["recommendation"]
    )
    return {
        "check_id": "internal_traffic_local_cross_node_topology",
        "signal": "kubernetes_pod_object",
        "status": (
            "missing_at_source" if not service_ok or not source_cross_node else
            "missing_in_splunk" if not splunk_cross_node else "symptom_confirmed"
        ),
        "source_policy_local": service_ok,
        "source_cross_node": source_cross_node,
        "splunk_cross_node": splunk_cross_node,
        "source_count": sum(len(pods) for pods in source.values()),
        "splunk_count": sum(len(pods) for pods in matched.values()),
        "visibility": "requires_additional_access",
        "access_gap": "Service spec.internalTrafficPolicy is not exported by the approved pods/events object receiver; use authorized Kubernetes Service API or separately approved export.",
        "remedy": "Inspect the recommendation Service through an authorized Kubernetes API; export Service objects only after separate approval.",
        "interpretation": "New frontend and recommendation pods are on different nodes; source Service policy is Local, but Splunk only proves placement.",
    }


def check_service_dns_failure(
    source_configmap: dict[str, Any],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Confirm source CoreDNS rule and in-window Splunk failed-request symptom."""
    data = source_configmap.get("data") if isinstance(source_configmap, dict) else None
    corefile = data.get("Corefile") if isinstance(data, dict) else None
    rule = r"template\s+ANY\s+ANY\s+user-service\.social-network\.svc\.cluster\.local\s*\{[^}]*\brcode\s+NXDOMAIN\b"
    source_rule_confirmed = isinstance(corefile, str) and re.search(rule, corefile) is not None
    failure_times = []
    for row in splunk_rows:
        timestamp = _timestamp(row.get("_time"))
        raw = row.get("_raw")
        match = re.search(r"\bNon-2xx or 3xx responses:\s*(\d+)\b", raw) if isinstance(raw, str) else None
        if timestamp is not None and start <= timestamp <= end and match is not None and int(match.group(1)) > 0:
            failure_times.append(timestamp)
    return {
        "check_id": "coredns_nxdomain_source_and_failed_requests",
        "signal": "container_logs",
        "status": (
            "missing_at_source" if not source_rule_confirmed else
            "missing_in_splunk" if not failure_times else "symptom_confirmed"
        ),
        "source_rule_confirmed": source_rule_confirmed,
        "splunk_failed_response_count": len(failure_times),
        "splunk_first_at": min(failure_times).isoformat().replace("+00:00", "Z") if failure_times else None,
        "visibility": "requires_additional_access",
        "access_gap": "The source CoreDNS NXDOMAIN template is in a kube-system ConfigMap excluded from the approved Splunk pod/event export.",
        "remedy": "Use authorized Kubernetes CoreDNS ConfigMap or DNS-query access; separately approve export before claiming Splunk-only mechanism visibility.",
        "interpretation": "The injected CoreDNS rule is present at source and the scoped workload reports non-2xx responses in Splunk; the log is a nonspecific symptom, not direct DNS proof.",
    }


def check_wrong_service_selector(
    source_service: dict[str, Any],
    source_endpoints: dict[str, Any],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Prove the selector/empty-endpoint mechanism at source and symptom in Splunk."""
    service_meta = source_service.get("metadata") if isinstance(source_service, dict) else None
    service_spec = source_service.get("spec") if isinstance(source_service, dict) else None
    selector = service_spec.get("selector") if isinstance(service_spec, dict) else None
    endpoints_meta = source_endpoints.get("metadata") if isinstance(source_endpoints, dict) else None
    source_selector = bool(
        isinstance(service_meta, dict)
        and service_meta.get("name") == "user-service"
        and service_meta.get("namespace") == "social-network"
        and isinstance(selector, dict)
        and selector.get("current_service_name") == "user-service"
    )
    source_empty_endpoints = bool(
        isinstance(endpoints_meta, dict)
        and endpoints_meta.get("name") == "user-service"
        and endpoints_meta.get("namespace") == "social-network"
        and not source_endpoints.get("subsets")
    )
    failed_times = []
    for row in splunk_rows:
        timestamp = _timestamp(row.get("_time"))
        raw = row.get("_raw")
        match = re.search(r"\bNon-2xx or 3xx responses:\s*(\d+)\b", raw) if isinstance(raw, str) else None
        if timestamp is not None and start <= timestamp <= end and match is not None and int(match.group(1)) > 0:
            failed_times.append(timestamp)
    return {
        "check_id": "user_service_selector_source_and_failed_requests",
        "signal": "container_logs",
        "status": (
            "missing_at_source" if not (source_selector and source_empty_endpoints) else
            "missing_in_splunk" if not failed_times else "symptom_confirmed"
        ),
        "source_selector_confirmed": source_selector,
        "source_empty_endpoints": source_empty_endpoints,
        "splunk_failed_response_count": len(failed_times),
        "splunk_first_at": min(failed_times).isoformat().replace("+00:00", "Z") if failed_times else None,
        "visibility": "requires_additional_access",
        "access_gap": "The Service selector and endpoint set are Kubernetes API objects not exported by the approved pod/event receiver.",
        "remedy": "Use authorized read-only Kubernetes Service and Endpoints access, or separately approve their telemetry export.",
        "interpretation": "The faulty selector and empty endpoints are proven at source; Splunk shows failed workload requests but not the selector mechanism.",
    }


def _stalled_custom_service_pod_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    metadata, spec, status = value.get("metadata"), value.get("spec"), value.get("status")
    if not all(isinstance(part, dict) for part in (metadata, spec, status)):
        return None
    name = metadata.get("name")
    if (metadata.get("namespace") != "social-network" or not isinstance(name, str)
            or not name.startswith("custom-service-") or status.get("phase") != "Pending"):
        return None
    init_containers = spec.get("initContainers")
    if not isinstance(init_containers, list) or not any(
        isinstance(container, dict) and container.get("name") == "hang-init"
        and isinstance(container.get("command"), list)
        and "sleep" in " ".join(container["command"])
        and "infinity" in " ".join(container["command"])
        for container in init_containers
    ):
        return None
    return name


def check_rolling_update_misconfigured(
    source_deployment: dict[str, Any],
    source_pods: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Check stalled new pods in Splunk and disruptive rollout strategy at source."""
    metadata = source_deployment.get("metadata") if isinstance(source_deployment, dict) else None
    spec = source_deployment.get("spec") if isinstance(source_deployment, dict) else None
    status = source_deployment.get("status") if isinstance(source_deployment, dict) else None
    strategy = spec.get("strategy") if isinstance(spec, dict) else None
    rolling = strategy.get("rollingUpdate") if isinstance(strategy, dict) else None
    strategy_confirmed = bool(
        isinstance(metadata, dict) and metadata.get("name") == "custom-service"
        and metadata.get("namespace") == "social-network"
        and isinstance(spec, dict) and spec.get("replicas") == 3
        and isinstance(strategy, dict) and strategy.get("type") == "RollingUpdate"
        and isinstance(rolling, dict) and rolling.get("maxUnavailable") == "100%"
        and rolling.get("maxSurge") in {"0%", 0}
        and isinstance(status, dict) and status.get("availableReplicas", 0) == 0
    )
    source_names, matches = _matching_pod_rows(
        source_pods, splunk_rows, identity=_stalled_custom_service_pod_name, start=start, end=end,
    )
    return {
        "check_id": "custom_service_disruptive_rollout_and_stalled_init_pod",
        "signal": "kubernetes_pod_object",
        "status": (
            "missing_at_source" if not strategy_confirmed or not source_names else
            "missing_in_splunk" if not matches else "symptom_confirmed"
        ),
        "source_strategy_confirmed": strategy_confirmed,
        "source_count": len(source_names),
        "splunk_count": len(matches),
        "splunk_first_at": min(timestamp for _, timestamp in matches).isoformat().replace("+00:00", "Z") if matches else None,
        "visibility": "requires_additional_access",
        "access_gap": "The stalled init pod is Splunk-visible, but the Deployment rollingUpdate maxUnavailable/maxSurge strategy is not in the approved pod/event export.",
        "remedy": "Use authorized read-only Deployment API access, or separately approve Deployment object export.",
        "interpretation": "New custom-service pods are stuck in init while the source Deployment allows all old replicas to become unavailable.",
    }


def _wrong_selection_pod_identity(pod: Any) -> str | None:
    if not isinstance(pod, dict):
        return None
    metadata, spec, status = pod.get("metadata"), pod.get("spec"), pod.get("status")
    if not all(isinstance(value, dict) for value in (metadata, spec, status)):
        return None
    labels = metadata.get("labels")
    if metadata.get("namespace") != "hotel-reservation" or not isinstance(labels, dict) or status.get("phase") != "Running":
        return None
    role = labels.get("io.kompose.service")
    if role not in {"frontend", "search"} or labels.get("service-route") != "frontend":
        return None
    expected_port = 5000 if role == "frontend" else 8082
    containers = spec.get("containers")
    if not isinstance(containers, list) or not any(
        isinstance(container, dict) and isinstance(container.get("ports"), list) and any(
            isinstance(port, dict) and port.get("containerPort") == expected_port
            for port in container["ports"]
        ) for container in containers
    ):
        return None
    name = metadata.get("name")
    return f"{role}:{name}" if isinstance(name, str) and name.startswith(f"{role}-") else None


def check_service_wrong_pod_selection(
    source_service: dict[str, Any],
    source_pods: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Show both frontend and search pods match the source Service's faulty selector."""
    metadata = source_service.get("metadata") if isinstance(source_service, dict) else None
    spec = source_service.get("spec") if isinstance(source_service, dict) else None
    ports = spec.get("ports") if isinstance(spec, dict) else None
    service_confirmed = bool(
        isinstance(metadata, dict) and metadata.get("name") == "frontend"
        and metadata.get("namespace") == "hotel-reservation"
        and isinstance(spec, dict) and spec.get("selector") == {"service-route": "frontend"}
        and isinstance(ports, list) and any(
            isinstance(port, dict) and port.get("targetPort") in {5000, "5000"} for port in ports
        )
    )
    source_names, matches = _matching_pod_rows(
        source_pods, splunk_rows, identity=_wrong_selection_pod_identity, start=start, end=end,
    )
    source_roles = {name.split(":", 1)[0] for name in source_names}
    splunk_roles = {name.split(":", 1)[0] for name, _ in matches}
    return {
        "check_id": "frontend_service_selects_search_pod",
        "signal": "kubernetes_pod_object",
        "status": (
            "missing_at_source" if not service_confirmed or source_roles != {"frontend", "search"} else
            "missing_in_splunk" if splunk_roles != {"frontend", "search"} else "symptom_confirmed"
        ),
        "source_service_selector_confirmed": service_confirmed,
        "source_roles": sorted(source_roles),
        "splunk_roles": sorted(splunk_roles),
        "source_count": len(source_names),
        "splunk_count": len(matches),
        "visibility": "requires_additional_access",
        "access_gap": "The source frontend Service selector is not exported by the approved pod/event receiver; Splunk pod objects show both frontend and search have the selected label.",
        "remedy": "Inspect frontend Service and EndpointSlices through an authorized Kubernetes API, or separately approve Service object export.",
        "interpretation": "A frontend Service selector also matches a search pod listening on 8082 rather than frontend's 5000.",
    }


def _memory_failed_create_uid(event: Any) -> str | None:
    if not isinstance(event, dict):
        return None
    metadata, involved = event.get("metadata"), event.get("involvedObject")
    message = event.get("message")
    if not isinstance(metadata, dict) or not isinstance(involved, dict) or not isinstance(message, str):
        return None
    if (metadata.get("namespace") != "hotel-reservation" or event.get("reason") != "FailedCreate"
            or "must specify memory" not in message.lower()
            or involved.get("kind") != "ReplicaSet"
            or not isinstance(involved.get("name"), str)
            or not involved["name"].startswith("search-")):
        return None
    uid = metadata.get("uid")
    return uid if isinstance(uid, str) and uid else None


def check_namespace_memory_quota(
    source_quota: dict[str, Any],
    source_events: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Match a search FailedCreate memory-admission event across source and Splunk."""
    metadata = source_quota.get("metadata") if isinstance(source_quota, dict) else None
    spec = source_quota.get("spec") if isinstance(source_quota, dict) else None
    hard = spec.get("hard") if isinstance(spec, dict) else None
    quota_confirmed = bool(
        isinstance(metadata, dict) and metadata.get("name") == "memory-limit-quota"
        and metadata.get("namespace") == "hotel-reservation"
        and isinstance(hard, dict) and hard.get("memory") == "1Gi"
        and _created_in_window(source_quota, start, end)
    )
    source_uids = {
        uid for event in source_events
        if (uid := _memory_failed_create_uid(event)) and _created_in_window(event, start, end)
    }
    matched = []
    for row in splunk_rows:
        timestamp = _timestamp(row.get("_time"))
        if timestamp is None or not start <= timestamp <= end:
            continue
        raw = row.get("_raw")
        try:
            envelope = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not isinstance(envelope, dict):
            continue
        event = envelope.get("object", envelope)
        if _memory_failed_create_uid(event) in source_uids and _created_in_window(event, start, end):
            matched.append(timestamp)
    return {
        "check_id": "search_failed_create_memory_quota_event",
        "signal": "kubernetes_event_object",
        "status": (
            "missing_at_source" if not quota_confirmed or not source_uids else
            "missing_in_splunk" if not matched else "symptom_confirmed"
        ),
        "source_quota_confirmed": quota_confirmed,
        "source_count": len(source_uids),
        "splunk_count": len(matched),
        "splunk_first_at": min(matched).isoformat().replace("+00:00", "Z") if matched else None,
        "visibility": "requires_additional_access",
        "access_gap": "The quota's hard memory requirement is not exported by the approved pods/events receiver; the admission event is visible in Splunk.",
        "remedy": "Inspect the ResourceQuota and search Deployment template through authorized Kubernetes API, or separately approve their export.",
        "interpretation": "A search ReplicaSet cannot create a replacement pod because admission requires memory declarations.",
    }


_VALKEY_AUTH_ERROR = re.compile(r"\b(?:WRONGPASS|NOAUTH)\b|\bauth(?:entication)?\s+(?:failed|error)\b", re.IGNORECASE)
_CART_CACHE_FAILURE = re.compile(r"\bWasn't able to connect to redis\b|\b(?:WRONGPASS|NOAUTH)\b|\bauth(?:entication)?\s+(?:failed|error)\b", re.IGNORECASE)


def check_valkey_auth_logs(
    source_lines: list[str],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Require the observed cart cache failure in source and Splunk; label auth certainty."""
    source_times = [
        timestamp for line in source_lines
        if _CART_CACHE_FAILURE.search(line)
        for timestamp in [_prefixed_log_timestamp(line)]
        if timestamp is not None and start <= timestamp <= end
    ]
    splunk_times = [
        timestamp for row in splunk_rows
        if isinstance(row.get("_raw"), str) and _CART_CACHE_FAILURE.search(row["_raw"])
        for timestamp in [_timestamp(row.get("_time"))]
        if timestamp is not None and start <= timestamp <= end
    ]
    auth_specific = any(_VALKEY_AUTH_ERROR.search(line) for line in source_lines) and any(
        isinstance(row.get("_raw"), str) and _VALKEY_AUTH_ERROR.search(row["_raw"]) for row in splunk_rows
    )
    return {
        "check_id": "cart_valkey_cache_failure_logs",
        "signal": "container_logs",
        "status": (
            "missing_at_source" if not source_times else
            "missing_in_splunk" if not splunk_times else "symptom_confirmed"
        ),
        "source_count": len(source_times),
        "splunk_count": len(splunk_times),
        "splunk_first_at": min(splunk_times).isoformat().replace("+00:00", "Z") if splunk_times else None,
        "auth_specific_log_present": auth_specific,
        "visibility": "requires_additional_access",
        "access_gap": "Cart authentication errors are log-visible, but the live Valkey requirepass setting is not in the approved Splunk telemetry.",
        "remedy": "Inspect the Valkey runtime configuration through authorized application/Kubernetes access without exporting password values.",
        "interpretation": (
            "Cart logs explicitly show cache authentication failure in source and Splunk."
            if auth_specific else
            "Cart logs show a cache connection failure in source and Splunk; they do not by themselves establish why Valkey refused the connection."
        ),
    }


def _secret_ref_pod_uid(pod: Any) -> str | None:
    if not isinstance(pod, dict):
        return None
    metadata, spec, status = pod.get("metadata"), pod.get("spec"), pod.get("status")
    if not all(isinstance(value, dict) for value in (metadata, spec, status)):
        return None
    name, uid = metadata.get("name"), metadata.get("uid")
    if (metadata.get("namespace") != "astronomy-shop" or not isinstance(name, str)
            or not name.startswith("product-catalog-") or not isinstance(uid, str)
            or not uid or status.get("phase") != "Running"):
        return None
    containers = spec.get("containers")
    if not isinstance(containers, list):
        return None
    for container in containers:
        if not isinstance(container, dict) or container.get("name") != "product-catalog":
            continue
        env = container.get("env")
        if not isinstance(env, list):
            continue
        if any(
            isinstance(item, dict) and item.get("name") == "DB_CONNECTION_STRING"
            and "value" not in item and isinstance(item.get("valueFrom"), dict)
            and item["valueFrom"].get("secretKeyRef") == {
                "name": "product-catalog-db-conn", "key": "DB_CONNECTION_STRING",
            }
            for item in env
        ):
            return uid
    return None


def check_secret_rotation_pod(
    source_deployment: dict[str, Any],
    source_pods: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Match the marked startup-credential pod without reading Secret values."""
    metadata = source_deployment.get("metadata") if isinstance(source_deployment, dict) else None
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    marked_uid = annotations.get("credential-source-pod-uid") if isinstance(annotations, dict) else None
    deployment_ok = bool(
        isinstance(metadata, dict) and metadata.get("name") == "product-catalog"
        and metadata.get("namespace") == "astronomy-shop"
        and isinstance(marked_uid, str) and marked_uid
    )
    source_uids, matches = _matching_pod_rows(
        source_pods, splunk_rows, identity=_secret_ref_pod_uid, start=start, end=end,
    )
    source_confirmed = deployment_ok and marked_uid in source_uids
    matched_times = [timestamp for uid, timestamp in matches if uid == marked_uid]
    return {
        "check_id": "product_catalog_secret_ref_stale_pod_identity",
        "signal": "kubernetes_pod_object",
        "status": (
            "missing_at_source" if not source_confirmed else
            "missing_in_splunk" if not matched_times else "symptom_confirmed"
        ),
        "source_count": int(source_confirmed),
        "splunk_count": len(matched_times),
        "splunk_first_at": min(matched_times).isoformat().replace("+00:00", "Z") if matched_times else None,
        "visibility": "requires_additional_access",
        "access_gap": "Splunk pod objects show the startup Secret reference, but neither the rotated Secret value nor live PostgreSQL credential state is in the approved export.",
        "remedy": "Use authorized Secret metadata and backend-auth state checks without exposing credential values; do not ingest raw Secrets.",
        "interpretation": "The source Deployment marks the currently running product-catalog pod as the credential-source pod, and the same Secret-referencing pod is visible in Splunk.",
    }


def _unschedulable_checkout_name(pod: Any) -> str | None:
    if not isinstance(pod, dict):
        return None
    metadata, spec, status = pod.get("metadata"), pod.get("spec"), pod.get("status")
    if not all(isinstance(value, dict) for value in (metadata, spec, status)):
        return None
    name = metadata.get("name")
    if (metadata.get("namespace") != "astronomy-shop" or not isinstance(name, str)
            or not name.startswith("checkout-") or status.get("phase") != "Pending"
            or spec.get("nodeSelector") != {"kubernetes.io/hostname": "extra-node"}):
        return None
    containers = spec.get("containers")
    if not isinstance(containers, list):
        return None
    for container in containers:
        if not isinstance(container, dict) or container.get("name") != "checkout":
            continue
        env = container.get("env")
        if isinstance(env, list) and any(
            isinstance(item, dict) and item.get("name") == "PRODUCT_CATALOG_ADDR"
            and isinstance(item.get("value"), str) and item["value"].endswith(":8082")
            for item in env
        ):
            return name
    return None


def check_unschedulable_checkout_pod(
    source_pods: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Confirm both independent checkout faults in one new Splunk-visible pod."""
    source_names, matches = _matching_pod_rows(
        source_pods, splunk_rows, identity=_unschedulable_checkout_name, start=start, end=end,
    )
    return {
        "check_id": "checkout_pending_bad_node_and_backend_port",
        "signal": "kubernetes_pod_object",
        "status": (
            "missing_at_source" if not source_names else
            "missing_in_splunk" if not matches else "confirmed"
        ),
        "source_count": len(source_names),
        "splunk_count": len(matches),
        "splunk_first_at": min(timestamp for _, timestamp in matches).isoformat().replace("+00:00", "Z") if matches else None,
        "visibility": "fully_splunk_observable",
        "interpretation": "The same new Pending checkout pod is pinned to nonexistent extra-node and points its product-catalog address to port 8082.",
    }


def _shared_jaeger_claim_identity(pod: Any) -> str | None:
    if not isinstance(pod, dict):
        return None
    metadata, spec, status = pod.get("metadata"), pod.get("spec"), pod.get("status")
    if not all(isinstance(value, dict) for value in (metadata, spec, status)):
        return None
    name, phase = metadata.get("name"), status.get("phase")
    if (metadata.get("namespace") != "social-network" or not isinstance(name, str)
            or not name.startswith("jaeger-") or phase not in {"Running", "Pending"}):
        return None
    volumes = spec.get("volumes")
    if not isinstance(volumes, list) or not any(
        isinstance(volume, dict) and isinstance(volume.get("persistentVolumeClaim"), dict)
        and volume["persistentVolumeClaim"].get("claimName") == "jaeger-pvc" for volume in volumes
    ):
        return None
    affinity = spec.get("affinity")
    anti_affinity = affinity.get("podAntiAffinity") if isinstance(affinity, dict) else None
    required = anti_affinity.get("requiredDuringSchedulingIgnoredDuringExecution") if isinstance(anti_affinity, dict) else None
    if not isinstance(required, list) or not any(
        isinstance(rule, dict) and rule.get("topologyKey") == "kubernetes.io/hostname" for rule in required
    ):
        return None
    return f"{phase}:{name}"


def check_duplicate_pvc_mounts(
    source_pvc: dict[str, Any],
    source_pods: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Prove the shared-claim split rollout, retaining PVC mode as an access gap."""
    metadata = source_pvc.get("metadata") if isinstance(source_pvc, dict) else None
    spec = source_pvc.get("spec") if isinstance(source_pvc, dict) else None
    modes = spec.get("accessModes") if isinstance(spec, dict) else None
    pvc_confirmed = bool(
        isinstance(metadata, dict) and metadata.get("name") == "jaeger-pvc"
        and metadata.get("namespace") == "social-network"
        and isinstance(modes, list) and "ReadWriteOnce" in modes
        and _created_in_window(source_pvc, start, end)
    )
    source_names, matches = _matching_pod_rows(
        source_pods, splunk_rows, identity=_shared_jaeger_claim_identity, start=start, end=end,
    )
    source_phases = {name.split(":", 1)[0] for name in source_names}
    splunk_phases = {name.split(":", 1)[0] for name, _ in matches}
    return {
        "check_id": "jaeger_shared_rwo_claim_split_rollout",
        "signal": "kubernetes_pod_object",
        "status": (
            "missing_at_source" if not pvc_confirmed or source_phases != {"Running", "Pending"} else
            "missing_in_splunk" if splunk_phases != {"Running", "Pending"} else "symptom_confirmed"
        ),
        "source_pvc_rwo_confirmed": pvc_confirmed,
        "source_phases": sorted(source_phases),
        "splunk_phases": sorted(splunk_phases),
        "source_count": len(source_names),
        "splunk_count": len(matches),
        "visibility": "requires_additional_access",
        "access_gap": "Splunk pod objects show shared claim, hostname anti-affinity, and a Pending replica, but not the PVC access mode or topology binding.",
        "remedy": "Inspect PVC/PV and Deployment through authorized Kubernetes API, or separately approve storage-object export.",
        "interpretation": "Two new Jaeger replicas share one claim under hostname anti-affinity; one runs while another remains Pending.",
    }


def _webhook_failed_create_uid(event: Any) -> str | None:
    if not isinstance(event, dict):
        return None
    metadata, involved = event.get("metadata"), event.get("involvedObject")
    message = event.get("message")
    if not isinstance(metadata, dict) or not isinstance(involved, dict) or not isinstance(message, str):
        return None
    if (metadata.get("namespace") != "hotel-reservation" or event.get("reason") != "FailedCreate"
            or "failed calling webhook" not in message.lower()
            or "pod-policy.validation.k8s.io" not in message
            or involved.get("kind") != "ReplicaSet"
            or not isinstance(involved.get("name"), str)
            or not involved["name"].startswith("recommendation-")):
        return None
    uid = metadata.get("uid")
    return uid if isinstance(uid, str) and uid else None


def check_admission_webhook_outage(
    source_webhook: dict[str, Any],
    source_events: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Match a rejected recommendation pod create with the broken source webhook."""
    metadata = source_webhook.get("metadata") if isinstance(source_webhook, dict) else None
    webhooks = source_webhook.get("webhooks") if isinstance(source_webhook, dict) else None
    source_confirmed = bool(
        isinstance(metadata, dict) and metadata.get("name") == "pod-policy.validation.k8s.io"
        and _created_in_window(source_webhook, start, end)
        and isinstance(webhooks, list) and any(
            isinstance(webhook, dict)
            and webhook.get("name") == "pod-policy.validation.k8s.io"
            and webhook.get("failurePolicy") == "Fail"
            and isinstance(webhook.get("namespaceSelector"), dict)
            and webhook["namespaceSelector"].get("matchLabels") == {"kubernetes.io/metadata.name": "hotel-reservation"}
            and isinstance(webhook.get("clientConfig"), dict)
            and isinstance(webhook["clientConfig"].get("service"), dict)
            and webhook["clientConfig"]["service"].get("name") == "pod-policy-webhook"
            and webhook["clientConfig"]["service"].get("namespace") == "policy-system"
            for webhook in webhooks
        )
    )
    source_uids = {
        uid for event in source_events
        if (uid := _webhook_failed_create_uid(event)) and _created_in_window(event, start, end)
    }
    matched = []
    for row in splunk_rows:
        timestamp = _timestamp(row.get("_time"))
        if timestamp is None or not start <= timestamp <= end:
            continue
        raw = row.get("_raw")
        try:
            envelope = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not isinstance(envelope, dict):
            continue
        event = envelope.get("object", envelope)
        if _webhook_failed_create_uid(event) in source_uids and _created_in_window(event, start, end):
            matched.append(timestamp)
    return {
        "check_id": "recommendation_failed_create_broken_admission_webhook",
        "signal": "kubernetes_event_object",
        "status": (
            "missing_at_source" if not source_confirmed or not source_uids else
            "missing_in_splunk" if not matched else "symptom_confirmed"
        ),
        "source_webhook_confirmed": source_confirmed,
        "source_count": len(source_uids),
        "splunk_count": len(matched),
        "splunk_first_at": min(matched).isoformat().replace("+00:00", "Z") if matched else None,
        "visibility": "requires_additional_access",
        "access_gap": "The FailedCreate event is Splunk-visible, but the webhook failure policy and backend Service configuration are not in the approved pod/event export.",
        "remedy": "Inspect ValidatingWebhookConfiguration and backend Service through authorized Kubernetes API, or separately approve their export.",
        "interpretation": "Recommendation ReplicaSet cannot create a replacement pod because a policy webhook call fails.",
    }


def check_readiness_probe_pods(
    source_pods: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Check the faulty probe and NotReady state in the same new pod object."""
    source_names, matches = _matching_pod_rows(
        source_pods, splunk_rows, identity=_readiness_pod_name, start=start, end=end,
    )
    first = min(matches, key=lambda item: item[1]) if matches else None
    return {
        "check_id": "readiness_bad_probe_not_ready_same_pod",
        "signal": "kubernetes_pod_object",
        "status": (
            "missing_at_source" if not source_names else
            "missing_in_splunk" if first is None else "confirmed"
        ),
        "source_count": len(source_names),
        "splunk_count": len(matches),
        "matched_pod": first[0] if first else None,
        "splunk_first_at": first[1].isoformat().replace("+00:00", "Z") if first else None,
        "visibility": "fully_splunk_observable",
        "interpretation": "The same new user-service pod specifies the nonexistent /healthz:8080 readiness probe and remains NotReady.",
    }


def _oom_16mi_nginx_pod_name(pod: Any) -> str | None:
    if not isinstance(pod, dict):
        return None
    metadata, spec, status = pod.get("metadata"), pod.get("spec"), pod.get("status")
    if not all(isinstance(value, dict) for value in (metadata, spec, status)):
        return None
    name = metadata.get("name")
    if metadata.get("namespace") != "social-network" or not isinstance(name, str) or not name.startswith("nginx-thrift-"):
        return None
    containers = spec.get("containers")
    statuses = status.get("containerStatuses")
    if not isinstance(containers, list) or not isinstance(statuses, list):
        return None
    limited = any(
        isinstance(container, dict)
        and container.get("name") == "nginx-thrift"
        and isinstance(container.get("resources"), dict)
        and isinstance(container["resources"].get("requests"), dict)
        and container["resources"]["requests"].get("memory") == "16Mi"
        and isinstance(container["resources"].get("limits"), dict)
        and container["resources"]["limits"].get("memory") == "16Mi"
        for container in containers
    )
    oom = any(
        isinstance(item, dict)
        and item.get("name") == "nginx-thrift"
        and (
            (isinstance(item.get("state"), dict) and isinstance(item["state"].get("terminated"), dict)
             and item["state"]["terminated"].get("reason") == "OOMKilled")
            or (isinstance(item.get("lastState"), dict) and isinstance(item["lastState"].get("terminated"), dict)
                and item["lastState"]["terminated"].get("reason") == "OOMKilled")
        )
        for item in statuses
    )
    return name if limited and oom else None


def check_mutating_webhook_pods(
    source_pods: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Prove the mutated pod symptom without claiming the webhook is exported."""
    source_names, matches = _matching_pod_rows(
        source_pods, splunk_rows, identity=_oom_16mi_nginx_pod_name, start=start, end=end,
    )
    first = min(matches, key=lambda item: item[1]) if matches else None
    return {
        "check_id": "nginx_thrift_16mi_oomkilled_same_pod",
        "signal": "kubernetes_pod_object",
        "status": (
            "missing_at_source" if not source_names else
            "missing_in_splunk" if first is None else "symptom_confirmed"
        ),
        "source_count": len(source_names),
        "splunk_count": len(matches),
        "matched_pod": first[0] if first else None,
        "splunk_first_at": first[1].isoformat().replace("+00:00", "Z") if first else None,
        "visibility": "requires_additional_access",
        "access_gap": "Pod objects show the 16Mi OOM symptom, but not the active mutating webhook or Deployment-template discrepancy.",
        "remedy": "Inspect the Deployment and MutatingWebhookConfigurations through the Kubernetes API, or authorize their export.",
        "interpretation": "A newly admitted nginx-thrift pod has 16Mi memory request/limit and an OOMKilled container.",
    }


def _shadowed_pod_name(pod: Any) -> str | None:
    if not isinstance(pod, dict):
        return None
    metadata, spec = pod.get("metadata"), pod.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        return None
    name = metadata.get("name")
    if metadata.get("namespace") != "astronomy-shop" or not isinstance(name, str) or not name.startswith("frontend-proxy-"):
        return None
    containers = spec.get("containers")
    if not isinstance(containers, list):
        return None
    for container in containers:
        if not isinstance(container, dict) or container.get("name") != "frontend-proxy":
            continue
        env = container.get("env")
        if not isinstance(env, list):
            continue
        values = [item.get("value") for item in env if isinstance(item, dict) and item.get("name") == "FRONTEND_HOST"]
        if values == ["frontend", "localhost"]:
            return name
    return None


def check_env_shadowing_pods(
    source_pods: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Check ordered duplicate frontend host definitions in the same new pod."""
    source_names, matches = _matching_pod_rows(
        source_pods, splunk_rows, identity=_shadowed_pod_name, start=start, end=end,
    )
    first = min(matches, key=lambda item: item[1]) if matches else None
    return {
        "check_id": "frontend_proxy_ordered_duplicate_host_env",
        "signal": "kubernetes_pod_object",
        "status": (
            "missing_at_source" if not source_names else
            "missing_in_splunk" if first is None else "confirmed"
        ),
        "source_count": len(source_names),
        "splunk_count": len(matches),
        "matched_pod": first[0] if first else None,
        "splunk_first_at": first[1].isoformat().replace("+00:00", "Z") if first else None,
        "visibility": "fully_splunk_observable",
        "interpretation": "The same new frontend proxy pod contains the ordered duplicate host environment entries.",
    }


def _wrong_dns_pod_name(pod: Any) -> str | None:
    if not isinstance(pod, dict):
        return None
    metadata, spec = pod.get("metadata"), pod.get("spec")
    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        return None
    name = metadata.get("name")
    if metadata.get("namespace") != "astronomy-shop" or not isinstance(name, str) or not name.startswith("frontend-"):
        return None
    config = spec.get("dnsConfig")
    if spec.get("dnsPolicy") != "None" or not isinstance(config, dict):
        return None
    return name if config.get("nameservers") == ["8.8.8.8"] else None


def check_wrong_dns_policy_pods(
    source_pods: list[dict[str, Any]],
    splunk_rows: list[dict[str, Any]],
    *,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    """Check external DNS policy and resolver on the same new frontend pod."""
    source_names, matches = _matching_pod_rows(
        source_pods, splunk_rows, identity=_wrong_dns_pod_name, start=start, end=end,
    )
    first = min(matches, key=lambda item: item[1]) if matches else None
    return {
        "check_id": "frontend_wrong_dns_policy_external_resolver",
        "signal": "kubernetes_pod_object",
        "status": (
            "missing_at_source" if not source_names else
            "missing_in_splunk" if first is None else "confirmed"
        ),
        "source_count": len(source_names),
        "splunk_count": len(matches),
        "matched_pod": first[0] if first else None,
        "splunk_first_at": first[1].isoformat().replace("+00:00", "Z") if first else None,
        "visibility": "fully_splunk_observable",
        "interpretation": "The same new frontend pod has the non-cluster DNS policy and an external-only resolver.",
    }


def verify_cronjob_pre_agent(
    backend: Any,
    *,
    source_pods: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Return sanitized presence and mechanism proof for one frozen window.

    This function is read-only and does not write to the agent-mounted run dir.
    The caller retries transient empty/search failures and publishes the proof
    only after the agent is no longer able to read that mount.
    """
    signal_counts = _representative_counts(backend, context, scope, connection_id, end, timeout_seconds)
    rows = backend.query_scoped_examples(
        "pods", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("audit-log-archiver",),
    )
    causal = check_cronjob_sidecar(source_pods, rows, start=start, end=end)
    return _case_proof("cronjob_sidecar_blocks_completion_hotel_reservation", context, start, end, signal_counts, causal)


def _representative_counts(
    backend: Any,
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    end: datetime,
    timeout_seconds: float,
) -> dict[str, int]:
    signal_counts = {
        signal: backend.query_signal(signal, context, scope, connection_id, end, timeout_seconds)
        for signal in _REPRESENTATIVE_SIGNALS
    }
    signal_counts.update({
        kind: backend.query_object(kind, context, scope, connection_id, end, timeout_seconds)
        for kind in ("pods", "events")
    })
    return signal_counts


def _case_proof(
    case_id: str,
    context: AttemptContext,
    start: datetime,
    end: datetime,
    signal_counts: dict[str, int],
    causal: dict[str, Any],
    *,
    required_signals: tuple[str, ...] = ("metrics", "traces", "logs", "kubernetes_events", "pods", "events"),
) -> dict[str, Any]:
    status = (
        "missing_at_source" if causal["status"] == "missing_at_source" else
        "missing_delivery" if any(signal_counts[signal] < 1 for signal in required_signals) else
        "missing_causal_telemetry" if causal["status"] not in {"confirmed", "symptom_confirmed"} else
        "ready_data_limited" if causal["status"] == "symptom_confirmed" else "ready"
    )
    return {
        "schema": "sregym.splunk_lite_pre_agent.v1",
        "case_id": case_id,
        "run_id": context.run_id,
        "window": {
            "start": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        },
        "status": status,
        "signals": signal_counts,
        "required_signals": list(required_signals),
        "causal": causal,
        "scope_note": "Run-scoped representative delivery plus case-specific causal evidence; not exhaustive telemetry coverage.",
    }


def verify_edge_pre_agent(
    backend: Any,
    *,
    source_lines: list[str],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = _representative_counts(backend, context, scope, connection_id, end, timeout_seconds)
    rows = backend.query_scoped_examples(
        "logs", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("request_filter_eval",), container_name="frontend-proxy",
    )
    causal = check_edge_waf_log(source_lines, rows, start=start, end=end)
    return _case_proof("edge_request_filter_cpu_saturation", context, start, end, signal_counts, causal)


def verify_network_policy_pre_agent(
    backend: Any,
    *,
    source_policy: dict[str, Any],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = _representative_counts(backend, context, scope, connection_id, end, timeout_seconds)
    rows = backend.query_scoped_examples(
        "logs", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("Socket", "errors"), container_name="wrk2",
    )
    causal = check_network_policy_symptom(source_policy, rows, start=start, end=end)
    if signal_counts["traces"] == 0:
        causal["access_gap"] += " Run-scoped APM trace examples were not queryable at the gate time."
        causal["remedy"] += " Recheck APM indexing and run-tag propagation before claiming trace coverage."
    return _case_proof(
        "network_policy_block", context, start, end, signal_counts, causal,
        # The verified timeout log is this case's observable symptom. Preserve
        # the trace result, but do not make an unqueryable APM search a false
        # claim that the policy symptom itself was absent.
        required_signals=("metrics", "logs"),
    )


def verify_mutating_webhook_pre_agent(
    backend: Any,
    *,
    source_pods: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = _representative_counts(backend, context, scope, connection_id, end, timeout_seconds)
    rows = backend.query_scoped_examples(
        "pods", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("nginx-thrift",),
    )
    causal = check_mutating_webhook_pods(source_pods, rows, start=start, end=end)
    return _case_proof(
        "mutating_webhook_resource_limits_social_network", context, start, end, signal_counts, causal,
    )


def verify_finalizer_deadlock_pre_agent(
    backend: Any,
    *,
    source_lines: list[str],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = _representative_counts(backend, context, scope, connection_id, end, timeout_seconds)
    rows = backend.query_scoped_examples(
        "logs", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("reconciliation", "Forbidden"), container_name="controller",
    )
    causal = check_finalizer_deadlock_logs(source_lines, rows, start=start, end=end)
    if signal_counts["traces"] == 0:
        causal["access_gap"] += " Run-scoped APM trace examples were not queryable at the gate time."
        causal["remedy"] += " Recheck APM indexing and run-tag propagation before claiming trace coverage."
    return _case_proof(
        "finalizer_deadlock_controller_hotel_reservation", context, start, end, signal_counts, causal,
        required_signals=("metrics", "logs"),
    )


def verify_kafka_pre_agent(
    backend: Any,
    *,
    source_lines: list[str],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    # Generic four-signal readiness has already passed. This second gate is
    # specifically about the Kafka clue, so an unrelated APM/metric query must
    # not prevent a valid log-based diagnosis attempt.
    signal_counts = {
        "logs": backend.query_signal("logs", context, scope, connection_id, end, timeout_seconds),
    }
    rows = {
        name: backend.query_scoped_examples(
            "logs", context, scope, connection_id, end, timeout_seconds,
            limit=100, terms=(term, "offset=20"), container_name="orders-validator",
        )
        for name, term in (("validation", "validation"), ("paused", "paused"))
    }
    causal = check_kafka_poison_logs(source_lines, rows, start=start, end=end)
    return _case_proof(
        "kafka_poison_pill_hol_block", context, start, end, signal_counts, causal,
        required_signals=("logs",),
    )


def verify_readiness_pre_agent(
    backend: Any,
    *,
    source_pods: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = _representative_counts(backend, context, scope, connection_id, end, timeout_seconds)
    rows = backend.query_scoped_examples(
        "pods", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("user-service",),
    )
    causal = check_readiness_probe_pods(source_pods, rows, start=start, end=end)
    return _case_proof("readiness_probe_misconfiguration_social_network", context, start, end, signal_counts, causal)


def verify_env_shadowing_pre_agent(
    backend: Any,
    *,
    source_pods: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = _representative_counts(backend, context, scope, connection_id, end, timeout_seconds)
    rows = backend.query_scoped_examples(
        "pods", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("frontend-proxy",),
    )
    causal = check_env_shadowing_pods(source_pods, rows, start=start, end=end)
    return _case_proof("env_variable_shadowing_astronomy_shop", context, start, end, signal_counts, causal)


def verify_wrong_dns_policy_pre_agent(
    backend: Any,
    *,
    source_pods: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = _representative_counts(backend, context, scope, connection_id, end, timeout_seconds)
    rows = backend.query_scoped_examples(
        "pods", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("frontend",),
    )
    causal = check_wrong_dns_policy_pods(source_pods, rows, start=start, end=end)
    return _case_proof("wrong_dns_policy_astronomy_shop", context, start, end, signal_counts, causal)


def wait_for_cronjob_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], list[dict[str, Any]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    """Bound polling for the CronJob source and Splunk recurrence proof."""
    return _wait_for_pre_agent(
        lambda end, timeout: verify_cronjob_pre_agent(
            backend, source_pods=source_reader(), context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        ),
        now=now, sleep=sleep, monotonic=monotonic, attempts=attempts,
        interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def wait_for_edge_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], list[str]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    """Bound polling for the edge request-filter source and Splunk proof."""
    return _wait_for_pre_agent(
        lambda end, timeout: verify_edge_pre_agent(
            backend, source_lines=source_reader(), context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        ),
        now=now, sleep=sleep, monotonic=monotonic, attempts=attempts,
        interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def wait_for_network_policy_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    """Wait for symptom evidence, explicitly marking the inaccessible policy rules."""
    return _wait_for_pre_agent(
        lambda end, timeout: verify_network_policy_pre_agent(
            backend, source_policy=source_reader(), context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        ),
        now=now, sleep=sleep, monotonic=monotonic, attempts=attempts,
        interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def wait_for_mutating_webhook_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], list[dict[str, Any]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    return _wait_for_pre_agent(
        lambda end, timeout: verify_mutating_webhook_pre_agent(
            backend, source_pods=source_reader(), context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        ),
        now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def wait_for_finalizer_deadlock_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], list[str]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    return _wait_for_pre_agent(
        lambda end, timeout: verify_finalizer_deadlock_pre_agent(
            backend, source_lines=source_reader(), context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        ),
        now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def wait_for_kafka_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], list[str]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    return _wait_for_pre_agent(
        lambda end, timeout: verify_kafka_pre_agent(
            backend, source_lines=source_reader(), context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        ),
        now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
        request_timeout_seconds=30.0,
    )


def wait_for_readiness_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], list[dict[str, Any]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    return _wait_for_pre_agent(
        lambda end, timeout: verify_readiness_pre_agent(
            backend, source_pods=source_reader(), context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        ),
        now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def wait_for_env_shadowing_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], list[dict[str, Any]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    return _wait_for_pre_agent(
        lambda end, timeout: verify_env_shadowing_pre_agent(
            backend, source_pods=source_reader(), context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        ),
        now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def wait_for_wrong_dns_policy_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], list[dict[str, Any]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    return _wait_for_pre_agent(
        lambda end, timeout: verify_wrong_dns_policy_pre_agent(
            backend, source_pods=source_reader(), context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        ),
        now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def verify_internal_traffic_policy_pre_agent(
    backend: Any,
    *,
    source_service: dict[str, Any],
    source_pods: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = _representative_counts(backend, context, scope, connection_id, end, timeout_seconds)
    rows = []
    for component in ("frontend", "recommendation"):
        rows.extend(backend.query_scoped_examples(
            "pods", context, scope, connection_id, end, timeout_seconds,
            limit=100, terms=(component,),
        ))
    causal = check_internal_traffic_policy(source_service, source_pods, rows, start=start, end=end)
    return _case_proof(
        "internal_traffic_policy_local_astronomy_shop", context, start, end, signal_counts, causal,
    )


def wait_for_internal_traffic_policy_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], tuple[dict[str, Any], list[dict[str, Any]]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    def verify(end: datetime, timeout: float) -> dict[str, Any]:
        source_service, source_pods = source_reader()
        return verify_internal_traffic_policy_pre_agent(
            backend, source_service=source_service, source_pods=source_pods,
            context=context, scope=scope, connection_id=connection_id,
            start=start, end=end, timeout_seconds=timeout,
        )

    return _wait_for_pre_agent(
        verify, now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def verify_service_dns_pre_agent(
    backend: Any,
    *,
    source_configmap: dict[str, Any],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    # The generic provider readiness gate already checked all four MELT classes.
    # This case-specific gate requires the current workload's log symptom; a
    # later empty APM sample must not erase that earlier delivery receipt.
    signal_counts = {
        "logs": backend.query_signal("logs", context, scope, connection_id, end, timeout_seconds),
    }
    rows = backend.query_scoped_examples(
        "logs", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("Non-2xx",), container_name="wrk2",
    )
    causal = check_service_dns_failure(source_configmap, rows, start=start, end=end)
    return _case_proof(
        "service_dns_resolution_failure_social_network", context, start, end, signal_counts, causal,
        required_signals=("logs",),
    )


def wait_for_service_dns_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    return _wait_for_pre_agent(
        lambda end, timeout: verify_service_dns_pre_agent(
            backend, source_configmap=source_reader(), context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        ),
        now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def verify_service_wrong_pod_selection_pre_agent(
    backend: Any,
    *,
    source_service: dict[str, Any],
    source_pods: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = _representative_counts(backend, context, scope, connection_id, end, timeout_seconds)
    rows = []
    for role in ("frontend", "search"):
        rows.extend(backend.query_scoped_examples(
            "pods", context, scope, connection_id, end, timeout_seconds,
            limit=100, terms=(role,),
        ))
    causal = check_service_wrong_pod_selection(source_service, source_pods, rows, start=start, end=end)
    return _case_proof(
        "service_wrong_pod_selection_hotel_reservation", context, start, end, signal_counts, causal,
    )


def wait_for_service_wrong_pod_selection_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], tuple[dict[str, Any], list[dict[str, Any]]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    def verify(end: datetime, timeout: float) -> dict[str, Any]:
        source_service, source_pods = source_reader()
        return verify_service_wrong_pod_selection_pre_agent(
            backend, source_service=source_service, source_pods=source_pods,
            context=context, scope=scope, connection_id=connection_id,
            start=start, end=end, timeout_seconds=timeout,
        )

    return _wait_for_pre_agent(
        verify, now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def verify_namespace_memory_quota_pre_agent(
    backend: Any,
    *,
    source_quota: dict[str, Any],
    source_events: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = _representative_counts(backend, context, scope, connection_id, end, timeout_seconds)
    rows = backend.query_scoped_examples(
        "events", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("must", "specify", "memory"),
    )
    causal = check_namespace_memory_quota(source_quota, source_events, rows, start=start, end=end)
    return _case_proof("namespace_memory_limit", context, start, end, signal_counts, causal)


def wait_for_namespace_memory_quota_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], tuple[dict[str, Any], list[dict[str, Any]]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    def verify(end: datetime, timeout: float) -> dict[str, Any]:
        source_quota, source_events = source_reader()
        return verify_namespace_memory_quota_pre_agent(
            backend, source_quota=source_quota, source_events=source_events,
            context=context, scope=scope, connection_id=connection_id,
            start=start, end=end, timeout_seconds=timeout,
        )

    return _wait_for_pre_agent(
        verify, now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def verify_valkey_auth_pre_agent(
    backend: Any,
    *,
    source_lines: list[str],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = {
        "logs": backend.query_signal("logs", context, scope, connection_id, end, timeout_seconds),
    }
    rows = []
    for term in ("WRONGPASS", "NOAUTH", "authentication", "redis"):
        rows.extend(backend.query_scoped_examples(
            "logs", context, scope, connection_id, end, timeout_seconds,
            limit=100, terms=(term,), container_name="cart",
        ))
    causal = check_valkey_auth_logs(source_lines, rows, start=start, end=end)
    return _case_proof(
        "valkey_auth_disruption", context, start, end, signal_counts, causal,
        required_signals=("logs",),
    )


def wait_for_valkey_auth_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], list[str]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    return _wait_for_pre_agent(
        lambda end, timeout: verify_valkey_auth_pre_agent(
            backend, source_lines=source_reader(), context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        ),
        now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def verify_secret_rotation_pre_agent(
    backend: Any,
    *,
    source_deployment: dict[str, Any],
    source_pods: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = {
        "pods": backend.query_object("pods", context, scope, connection_id, end, timeout_seconds),
    }
    rows = backend.query_scoped_examples(
        "pods", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("product-catalog",),
    )
    causal = check_secret_rotation_pod(source_deployment, source_pods, rows, start=start, end=end)
    return _case_proof(
        "secret_rotation_stale_env_credentials_astronomy_shop", context, start, end, signal_counts, causal,
        required_signals=("pods",),
    )


def wait_for_secret_rotation_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], tuple[dict[str, Any], list[dict[str, Any]]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    def verify(end: datetime, timeout: float) -> dict[str, Any]:
        source_deployment, source_pods = source_reader()
        return verify_secret_rotation_pre_agent(
            backend, source_deployment=source_deployment, source_pods=source_pods,
            context=context, scope=scope, connection_id=connection_id,
            start=start, end=end, timeout_seconds=timeout,
        )

    return _wait_for_pre_agent(
        verify, now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def verify_unschedulable_checkout_pre_agent(
    backend: Any,
    *,
    source_pods: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = {
        "pods": backend.query_object("pods", context, scope, connection_id, end, timeout_seconds),
    }
    rows = backend.query_scoped_examples(
        "pods", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("checkout",),
    )
    causal = check_unschedulable_checkout_pod(source_pods, rows, start=start, end=end)
    return _case_proof(
        "unschedulable_incorrect_port_assignment", context, start, end, signal_counts, causal,
        required_signals=("pods",),
    )


def wait_for_unschedulable_checkout_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], list[dict[str, Any]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    return _wait_for_pre_agent(
        lambda end, timeout: verify_unschedulable_checkout_pre_agent(
            backend, source_pods=source_reader(), context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        ),
        now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def verify_duplicate_pvc_mounts_pre_agent(
    backend: Any,
    *,
    source_pvc: dict[str, Any],
    source_pods: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = {
        "pods": backend.query_object("pods", context, scope, connection_id, end, timeout_seconds),
    }
    rows = backend.query_scoped_examples(
        "pods", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("jaeger",),
    )
    causal = check_duplicate_pvc_mounts(source_pvc, source_pods, rows, start=start, end=end)
    return _case_proof(
        "duplicate_pvc_mounts_social_network", context, start, end, signal_counts, causal,
        required_signals=("pods",),
    )


def wait_for_duplicate_pvc_mounts_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], tuple[dict[str, Any], list[dict[str, Any]]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    def verify(end: datetime, timeout: float) -> dict[str, Any]:
        source_pvc, source_pods = source_reader()
        return verify_duplicate_pvc_mounts_pre_agent(
            backend, source_pvc=source_pvc, source_pods=source_pods,
            context=context, scope=scope, connection_id=connection_id,
            start=start, end=end, timeout_seconds=timeout,
        )

    return _wait_for_pre_agent(
        verify, now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def verify_admission_webhook_pre_agent(
    backend: Any,
    *,
    source_webhook: dict[str, Any],
    source_events: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = {
        "events": backend.query_object("events", context, scope, connection_id, end, timeout_seconds),
    }
    rows = backend.query_scoped_examples(
        "events", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("failed", "calling", "webhook"),
    )
    causal = check_admission_webhook_outage(source_webhook, source_events, rows, start=start, end=end)
    return _case_proof(
        "admission_webhook_outage_hotel_reservation", context, start, end, signal_counts, causal,
        required_signals=("events",),
    )


def wait_for_admission_webhook_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], tuple[dict[str, Any], list[dict[str, Any]]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    def verify(end: datetime, timeout: float) -> dict[str, Any]:
        source_webhook, source_events = source_reader()
        return verify_admission_webhook_pre_agent(
            backend, source_webhook=source_webhook, source_events=source_events,
            context=context, scope=scope, connection_id=connection_id,
            start=start, end=end, timeout_seconds=timeout,
        )

    return _wait_for_pre_agent(
        verify, now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def verify_wrong_service_selector_pre_agent(
    backend: Any,
    *,
    source_service: dict[str, Any],
    source_endpoints: dict[str, Any],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = {
        "logs": backend.query_signal("logs", context, scope, connection_id, end, timeout_seconds),
    }
    rows = backend.query_scoped_examples(
        "logs", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("Non-2xx",), container_name="wrk2",
    )
    causal = check_wrong_service_selector(source_service, source_endpoints, rows, start=start, end=end)
    return _case_proof(
        "wrong_service_selector_social_network", context, start, end, signal_counts, causal,
        required_signals=("logs",),
    )


def wait_for_wrong_service_selector_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], tuple[dict[str, Any], dict[str, Any]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    def verify(end: datetime, timeout: float) -> dict[str, Any]:
        source_service, source_endpoints = source_reader()
        return verify_wrong_service_selector_pre_agent(
            backend, source_service=source_service, source_endpoints=source_endpoints,
            context=context, scope=scope, connection_id=connection_id,
            start=start, end=end, timeout_seconds=timeout,
        )

    return _wait_for_pre_agent(
        verify, now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def verify_rolling_update_pre_agent(
    backend: Any,
    *,
    source_deployment: dict[str, Any],
    source_pods: list[dict[str, Any]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = {
        "pods": backend.query_object("pods", context, scope, connection_id, end, timeout_seconds),
    }
    rows = backend.query_scoped_examples(
        "pods", context, scope, connection_id, end, timeout_seconds,
        limit=100, terms=("custom-service",),
    )
    causal = check_rolling_update_misconfigured(source_deployment, source_pods, rows, start=start, end=end)
    return _case_proof(
        "rolling_update_misconfigured_social_network", context, start, end, signal_counts, causal,
        required_signals=("pods",),
    )


def wait_for_rolling_update_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], tuple[dict[str, Any], list[dict[str, Any]]]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    def verify(end: datetime, timeout: float) -> dict[str, Any]:
        source_deployment, source_pods = source_reader()
        return verify_rolling_update_pre_agent(
            backend, source_deployment=source_deployment, source_pods=source_pods,
            context=context, scope=scope, connection_id=connection_id,
            start=start, end=end, timeout_seconds=timeout,
        )

    return _wait_for_pre_agent(
        verify, now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


_SEARCH_RETRY_METRICS = (
    "rate_queue_depth", "search_requests_total", "search_rate_attempts_total",
)


def check_search_retry_metrics(
    source_metrics: dict[str, float],
    splunk_metrics: dict[str, float],
) -> dict[str, Any]:
    """Require the post-trigger backlog and its request/attempt counters.

    Cumulative counter values alone do not establish the post-trigger retry
    amplification rate; the benchmark injection separately verifies that at
    source, and the proof labels the Splunk observation as a symptom.
    """
    def present(metrics: dict[str, float]) -> bool:
        return all(
            name in metrics
            and isinstance(metrics[name], (int, float))
            and not isinstance(metrics[name], bool)
            and 0 <= metrics[name] < float("inf")
            for name in _SEARCH_RETRY_METRICS
        )

    source_backlog = present(source_metrics) and source_metrics["rate_queue_depth"] >= 20
    splunk_backlog = present(splunk_metrics) and splunk_metrics["rate_queue_depth"] >= 20
    source_counters = present(source_metrics) and all(source_metrics[name] > 0 for name in _SEARCH_RETRY_METRICS[1:])
    splunk_counters = present(splunk_metrics) and all(splunk_metrics[name] > 0 for name in _SEARCH_RETRY_METRICS[1:])
    return {
        "check_id": "search_rate_post_trigger_backlog_and_attempt_counters",
        "signal": "application_metrics",
        "status": (
            "missing_at_source" if not (source_backlog and source_counters) else
            "missing_in_splunk" if not (splunk_backlog and splunk_counters) else
            "symptom_confirmed"
        ),
        "metric_names": list(_SEARCH_RETRY_METRICS),
        "source_metric_count": sum(name in source_metrics for name in _SEARCH_RETRY_METRICS),
        "splunk_metric_count": sum(name in splunk_metrics for name in _SEARCH_RETRY_METRICS),
        "source_backlog_confirmed": source_backlog,
        "splunk_backlog_confirmed": splunk_backlog,
        "visibility": "partially_splunk_observable",
        "access_gap": "This check does not prove the post-trigger counter deltas or retry-policy settings in Splunk; no Kubernetes-only gap is asserted.",
        "remedy": "Compare bounded historical request/attempt deltas and inspect the scoped search/rate pod objects before claiming the full feedback loop.",
        "interpretation": "The injected fault is established at source; Splunk has the same named counters and sustained rate-service queue backlog.",
    }


def verify_search_retry_pre_agent(
    backend: Any,
    *,
    source_metrics: dict[str, float],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    end: datetime,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    signal_counts = {
        "metrics": backend.query_signal("metrics", context, scope, connection_id, end, timeout_seconds),
    }
    splunk_metrics = backend.query_application_metric_values(
        _SEARCH_RETRY_METRICS, context, scope, end, timeout_seconds,
    )
    causal = check_search_retry_metrics(source_metrics, splunk_metrics)
    return _case_proof(
        "search_rate_retry_collapse_hotel_reservation", context, start, end, signal_counts, causal,
        required_signals=("metrics",),
    )


def wait_for_search_retry_pre_agent(
    backend: Any,
    *,
    source_reader: Callable[[], dict[str, float]],
    context: AttemptContext,
    scope: ApplicationScope,
    connection_id: str,
    start: datetime,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    attempts: int = 60,
    interval_seconds: float = 5.0,
    max_wait_seconds: float = 300.0,
) -> dict[str, Any]:
    def verify(end: datetime, timeout: float) -> dict[str, Any]:
        try:
            source_metrics = source_reader()
        except Exception:
            raise CausalSourceError("source application metric query failed") from None
        return verify_search_retry_pre_agent(
            backend, source_metrics=source_metrics, context=context, scope=scope,
            connection_id=connection_id, start=start, end=end, timeout_seconds=timeout,
        )

    return _wait_for_pre_agent(
        verify, now=now, sleep=sleep, monotonic=monotonic,
        attempts=attempts, interval_seconds=interval_seconds, max_wait_seconds=max_wait_seconds,
    )


def _wait_for_pre_agent(
    verify_once: Callable[[datetime, float], dict[str, Any]],
    *,
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
    attempts: int,
    interval_seconds: float,
    max_wait_seconds: float,
    request_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    if attempts < 1 or interval_seconds <= 0 or max_wait_seconds <= 0 or request_timeout_seconds <= 0:
        raise ValueError("pre-agent retry policy must be positive")
    deadline = monotonic() + max_wait_seconds
    last_proof: dict[str, Any] | None = None
    for index in range(attempts):
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        end = now()
        try:
            last_proof = verify_once(end, min(request_timeout_seconds, max(0.1, remaining / 3)))
        except SplunkBackendError as error:
            if not error.transient:
                reason = "Splunk authentication or query permission failed" if error.status_code in {401, 403} else "Splunk query rejected"
                raise CaseGateError(reason) from None
            last_proof = {"status": "query_error", "schema": "sregym.splunk_lite_pre_agent.v1"}
            if error.status_code is not None:
                last_proof["http_status"] = error.status_code
        except CausalSourceError:
            last_proof = {"status": "source_query_error", "schema": "sregym.splunk_lite_pre_agent.v1"}
        if last_proof.get("status") in {"ready", "ready_data_limited"}:
            return last_proof
        if index < attempts - 1:
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            sleep(min(interval_seconds, remaining))
    raise CaseGateError("pre-agent evidence did not become ready", last_proof)
