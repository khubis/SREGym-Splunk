# What telemetry was verified?

This page describes checks recorded for **this valid attempt**, not a guarantee that every emitted metric, log, span, or Kubernetes object arrived. The checker was run after fault injection and before Assistant V3; its oracle-aware facts were **not** put in the starter prompt.

## Code to inspect or rerun

The runner's `_assistant_case_preflight` in `main.py` dispatches this case to `wait_for_service_dns_pre_agent`, then `verify_service_dns_pre_agent`, then `check_service_dns_failure` in [the shared case verifier](pre_agent_verifier_source.py). The [shared post-run verifier](postrun_verifier_source.py) checks representative signal presence after grading. The source files here are **current code snapshots made when this dossier was built**; earlier runs did not save a verifier-source hash, so they cannot prove byte-for-byte historical code identity. The JSON proofs below are the saved run-time evidence.

## Before V3: provider readiness

The provider's run-scoped readiness check queried metrics, traces, logs, and Kubernetes events. A positive result means at least one scoped example was queryable, not complete delivery.

| Signal | Ready | Observed count |
|---|---|---:|
| metrics | True | 1 |
| traces | True | 1 |
| logs | True | 1 |
| kubernetes_events | True | 1 |

## Before V3: case-specific source/Splunk gate

Gate status: `ready_data_limited`. The incident window was `2026-09-28T06:23:14.651622Z` through `2026-09-28T06:25:29.594902Z`. The table below lists the additional signal-presence checks recorded by this case gate; only rows marked required were launch requirements.

| Signal | Query result | Required for this case gate? |
|---|---:|---|
| logs | 1 | yes |

**Decisive check (`coredns_nxdomain_source_and_failed_requests`):** The injected CoreDNS rule is present at source and the scoped workload reports non-2xx responses in Splunk; the log is a nonspecific symptom, not direct DNS proof.

- Evidence type actually matched: `container_logs`; status: `symptom_confirmed`.
- Source Kind evidence: source_rule_confirmed=True.
- Splunk evidence: splunk_failed_response_count=11.
- Splunk queries match the opaque run ID, namespace, and saved time window; source checks use the selected Kind context and incident window. Raw log and pod bodies were not saved; see [pre_agent_proof.json](pre_agent_proof.json) for sanitized counts, statuses, and timestamps, and the code for exact predicates.

## Why those clues matter—and what remains unproven

- Candidate clue (logs): user-service.social-network.svc.cluster.local resolves NXDOMAIN for callers. Query focus: caller resolver NXDOMAIN for user-service.social-network.svc.cluster.local.
- Candidate clue (other_kubernetes_api): Injected CoreDNS NXDOMAIN template requires kube-system ConfigMap or DNS query access. Query focus: kube-system/coredns ConfigMap template; kube-system logs are excluded from HEC.

The candidate clues above come from the benchmark evidence map; **they are not all claimed as verified**. The actual verified signal and result are in the decisive check above.
Access/coverage gap: The source CoreDNS NXDOMAIN template is in a kube-system ConfigMap excluded from the approved Splunk pod/event export.
Remedy: Use authorized Kubernetes CoreDNS ConfigMap or DNS-query access; separately approve export before claiming Splunk-only mechanism visibility.

## After the run: representative presence and delivery

| Signal/object class | Status | Query count |
|---|---|---:|
| events | present | 1 |
| kubernetes_events | present | 1 |
| logs | present | 1 |
| metrics | present | 1 |
| pods | present | 1 |
| traces | present | 1 |

Collector closing audit: valid=`True`, drained=`True`. See [delivery_audit.json](delivery_audit.json) for sent/failure deltas and final queue sizes. The post-run query is a presence spot check; a zero can mean no matching object changed inside the short incident window, and a one is not a complete event count. The `kubernetes_events` signal and `events` object check are two query views, not proof of distinct event streams.

The [Splunk-visible ground-truth page](splunk_visible_ground_truth.md) records any independent post-grade golden query and separates it from the pre-agent gate.
