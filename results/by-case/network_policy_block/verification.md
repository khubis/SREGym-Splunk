# What telemetry was verified?

This page describes checks recorded for **this valid attempt**, not a guarantee that every emitted metric, log, span, or Kubernetes object arrived. The checker was run after fault injection and before Assistant V3; its oracle-aware facts were **not** put in the starter prompt.

## Code to inspect or rerun

The runner's `_assistant_case_preflight` in `main.py` dispatches this case to `wait_for_network_policy_pre_agent`, then `verify_network_policy_pre_agent`, then `check_network_policy_symptom` in [the shared case verifier](pre_agent_verifier_source.py). The [shared post-run verifier](postrun_verifier_source.py) checks representative signal presence after grading. The source files here are **current code snapshots made when this dossier was built**; earlier runs did not save a verifier-source hash, so they cannot prove byte-for-byte historical code identity. The JSON proofs below are the saved run-time evidence.

## Before V3: provider readiness

The provider's run-scoped readiness check queried metrics, traces, logs, and Kubernetes events. A positive result means at least one scoped example was queryable, not complete delivery.

| Signal | Ready | Observed count |
|---|---|---:|
| metrics | True | 1 |
| traces | True | 1 |
| logs | True | 1 |
| kubernetes_events | True | 1 |

## Before V3: case-specific source/Splunk gate

Gate status: `ready_data_limited`. The incident window was `2026-09-27T22:01:08.368776Z` through `2026-09-27T22:03:28.781109Z`. The table below lists the additional signal-presence checks recorded by this case gate; only rows marked required were launch requirements.

| Signal | Query result | Required for this case gate? |
|---|---:|---|
| events | 1 | no |
| kubernetes_events | 1 | no |
| logs | 1 | yes |
| metrics | 1 | yes |
| pods | 1 | no |
| traces | 0 | no |

**Decisive check (`network_policy_source_and_request_timeout`):** The source deny-all NetworkPolicy was confirmed and four scoped request-timeout log rows were found in Splunk; the policy rules themselves were not exported.

- Evidence type actually matched: `container_logs`; status: `symptom_confirmed`.
- Source Kind evidence: source_policy_confirmed=True.
- Splunk evidence: splunk_timeout_count=4.
- Splunk queries match the opaque run ID, namespace, and saved time window; source checks use the selected Kind context and incident window. Raw log and pod bodies were not saved; see [pre_agent_proof.json](pre_agent_proof.json) for sanitized counts, statuses, and timestamps, and the code for exact predicates.

## Why those clues matter—and what remains unproven

- Candidate clue (traces): Recommendation request paths fail or time out while pods stay running. Query focus: recommendation spans and dependent caller errors.
- Candidate clue (other_kubernetes_api): The exact deny-all-recommendation selector and empty ingress/egress rules are not pod or event objects. Query focus: NetworkPolicy deny-all-recommendation spec and io.kompose.service=recommendation selector via networking.k8s.io API.

The candidate clues above come from the benchmark evidence map; **they are not all claimed as verified**. The actual verified signal and result are in the decisive check above.
Access/coverage gap: The approved pod/event export omits NetworkPolicy selector and ingress/egress rules. Run-scoped APM trace examples were not queryable at the gate time.
Remedy: Read the NetworkPolicy through Kubernetes, or separately authorize that object type in the receiver. Recheck APM indexing and run-tag propagation before claiming trace coverage.

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
