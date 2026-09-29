# What telemetry was verified?

This page describes checks recorded for **this valid attempt**, not a guarantee that every emitted metric, log, span, or Kubernetes object arrived. The checker was run after fault injection and before Assistant V3; its oracle-aware facts were **not** put in the starter prompt.

## Code to inspect or rerun

The runner's `_assistant_case_preflight` in `main.py` dispatches this case to `wait_for_internal_traffic_policy_pre_agent`, then `verify_internal_traffic_policy_pre_agent`, then `check_internal_traffic_policy` in [the shared case verifier](pre_agent_verifier_source.py). The [shared post-run verifier](postrun_verifier_source.py) checks representative signal presence after grading. The source files here are **current code snapshots made when this dossier was built**; earlier runs did not save a verifier-source hash, so they cannot prove byte-for-byte historical code identity. The JSON proofs below are the saved run-time evidence.

## Before V3: provider readiness

The provider's run-scoped readiness check queried metrics, traces, logs, and Kubernetes events. A positive result means at least one scoped example was queryable, not complete delivery.

| Signal | Ready | Observed count |
|---|---|---:|
| metrics | True | 1 |
| traces | True | 1 |
| logs | True | 1 |
| kubernetes_events | True | 1 |

## Before V3: case-specific source/Splunk gate

Gate status: `ready_data_limited`. The incident window was `2026-09-28T03:46:13.118664Z` through `2026-09-28T03:48:36.778055Z`. The table below lists the additional signal-presence checks recorded by this case gate; only rows marked required were launch requirements.

| Signal | Query result | Required for this case gate? |
|---|---:|---|
| events | 1 | yes |
| kubernetes_events | 1 | yes |
| logs | 1 | yes |
| metrics | 1 | yes |
| pods | 1 | yes |
| traces | 1 | yes |

**Decisive check (`internal_traffic_local_cross_node_topology`):** New frontend and recommendation pods are on different nodes; source Service policy is Local, but Splunk only proves placement.

- Evidence type actually matched: `kubernetes_pod_object`; status: `symptom_confirmed`.
- Source Kind evidence: source_count=2; source_cross_node=True; source_policy_local=True.
- Splunk evidence: splunk_count=2; splunk_cross_node=True.
- Splunk queries match the opaque run ID, namespace, and saved time window; source checks use the selected Kind context and incident window. Raw log and pod bodies were not saved; see [pre_agent_proof.json](pre_agent_proof.json) for sanitized counts, statuses, and timestamps, and the code for exact predicates.

## Why those clues matter—and what remains unproven

- Candidate clue (traces): Recommendation routing fails only for affected topology. Query focus: recommendation request errors by caller/node.
- Candidate clue (pods): frontend and recommendation pods run on different nodes. Query focus: frontend and recommendation pod spec.nodeName.
- Candidate clue (other_kubernetes_api): recommendation Service internalTrafficPolicy is Local and cannot be read from pod/event objects. Query focus: recommendation Service spec.internalTrafficPolicy and endpoints.

The candidate clues above come from the benchmark evidence map; **they are not all claimed as verified**. The actual verified signal and result are in the decisive check above.
Access/coverage gap: Service spec.internalTrafficPolicy is not exported by the approved pods/events object receiver; use authorized Kubernetes Service API or separately approved export.
Remedy: Inspect the recommendation Service through an authorized Kubernetes API; export Service objects only after separate approval.

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
