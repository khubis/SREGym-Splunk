# What telemetry was verified?

This page describes checks recorded for **this valid attempt**, not a guarantee that every emitted metric, log, span, or Kubernetes object arrived. The checker was run after fault injection and before Assistant V3; its oracle-aware facts were **not** put in the starter prompt.

## Code to inspect or rerun

The runner's `_assistant_case_preflight` in `main.py` dispatches this case to `wait_for_service_wrong_pod_selection_pre_agent`, then `verify_service_wrong_pod_selection_pre_agent`, then `check_service_wrong_pod_selection` in [the shared case verifier](pre_agent_verifier_source.py). The [shared post-run verifier](postrun_verifier_source.py) checks representative signal presence after grading. The source files here are **current code snapshots made when this dossier was built**; earlier runs did not save a verifier-source hash, so they cannot prove byte-for-byte historical code identity. The JSON proofs below are the saved run-time evidence.

## Before V3: provider readiness

The provider's run-scoped readiness check queried metrics, traces, logs, and Kubernetes events. A positive result means at least one scoped example was queryable, not complete delivery.

| Signal | Ready | Observed count |
|---|---|---:|
| metrics | True | 1 |
| traces | True | 1 |
| logs | True | 1 |
| kubernetes_events | True | 1 |

## Before V3: case-specific source/Splunk gate

Gate status: `ready_data_limited`. The incident window was `2026-09-28T04:13:49.059376Z` through `2026-09-28T04:16:10.808424Z`. The table below lists the additional signal-presence checks recorded by this case gate; only rows marked required were launch requirements.

| Signal | Query result | Required for this case gate? |
|---|---:|---|
| events | 1 | yes |
| kubernetes_events | 1 | yes |
| logs | 1 | yes |
| metrics | 1 | yes |
| pods | 1 | yes |
| traces | 1 | yes |

**Decisive check (`frontend_service_selects_search_pod`):** A frontend Service selector also matches a search pod listening on 8082 rather than frontend's 5000.

- Evidence type actually matched: `kubernetes_pod_object`; status: `symptom_confirmed`.
- Source Kind evidence: source_count=2.
- Splunk evidence: splunk_count=3; splunk_roles=['frontend', 'search'].
- Splunk queries match the opaque run ID, namespace, and saved time window; source checks use the selected Kind context and incident window. Raw log and pod bodies were not saved; see [pre_agent_proof.json](pre_agent_proof.json) for sanitized counts, statuses, and timestamps, and the code for exact predicates.

## Why those clues matter—and what remains unproven

- Candidate clue (traces): Frontend Service intermittently routes to search pods listening on 8082 rather than 5000. Query focus: frontend request failures correlated with search backend/port.
- Candidate clue (other_kubernetes_api): service-route=frontend selector includes search pods in frontend Service endpoints. Query focus: frontend Service spec.selector, EndpointSlice targets, and search pod labels/ports.

The candidate clues above come from the benchmark evidence map; **they are not all claimed as verified**. The actual verified signal and result are in the decisive check above.
Access/coverage gap: The source frontend Service selector is not exported by the approved pod/event receiver; Splunk pod objects show both frontend and search have the selected label.
Remedy: Inspect frontend Service and EndpointSlices through an authorized Kubernetes API, or separately approve Service object export.

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
