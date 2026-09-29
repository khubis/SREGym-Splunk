# What telemetry was verified?

This page describes checks recorded for **this valid attempt**, not a guarantee that every emitted metric, log, span, or Kubernetes object arrived. The checker was run after fault injection and before Assistant V3; its oracle-aware facts were **not** put in the starter prompt.

## Code to inspect or rerun

The runner's `_assistant_case_preflight` in `main.py` dispatches this case to `wait_for_search_retry_pre_agent`, then `verify_search_retry_pre_agent`, then `check_search_retry_metrics` in [the shared case verifier](pre_agent_verifier_source.py). The [shared post-run verifier](postrun_verifier_source.py) checks representative signal presence after grading. The source files here are **current code snapshots made when this dossier was built**; earlier runs did not save a verifier-source hash, so they cannot prove byte-for-byte historical code identity. The JSON proofs below are the saved run-time evidence.

## Before V3: provider readiness

The provider's run-scoped readiness check queried metrics, traces, logs, and Kubernetes events. A positive result means at least one scoped example was queryable, not complete delivery.

| Signal | Ready | Observed count |
|---|---|---:|
| metrics | True | 1 |
| traces | True | 1 |
| logs | True | 1 |
| kubernetes_events | True | 1 |

## Before V3: case-specific source/Splunk gate

Gate status: `ready_data_limited`. The incident window was `2026-09-28T22:16:21.818989Z` through `2026-09-28T22:19:06.567380Z`. The table below lists the additional signal-presence checks recorded by this case gate; only rows marked required were launch requirements.

| Signal | Query result | Required for this case gate? |
|---|---:|---|
| metrics | 1 | yes |

**Decisive check (`search_rate_post_trigger_backlog_and_attempt_counters`):** The injected fault is established at source; Splunk has the same named counters and sustained rate-service queue backlog.

- Evidence type actually matched: `application_metrics`; status: `symptom_confirmed`.
- Source Kind evidence: source_metric_count=3.
- Splunk evidence: splunk_metric_count=3; splunk_backlog_confirmed=True.
- Exact named metrics queried: `rate_queue_depth`, `search_requests_total`, `search_rate_attempts_total`.
- Splunk queries match the opaque run ID, namespace, and saved time window; source checks use the selected Kind context and incident window. Raw log and pod bodies were not saved; see [pre_agent_proof.json](pre_agent_proof.json) for sanitized counts, statuses, and timestamps, and the code for exact predicates.

## Why those clues matter—and what remains unproven

- Candidate clue (traces): A transient burst fills rate's backend queue, then search retries expired rate calls. Query focus: search-to-rate spans show repeated attempts, deadline exceeded, and post-burst persistence.
- Candidate clue (metrics): Internal rate requests stay above backend capacity after external traffic normalizes. Query focus: search/rate request rate, queue length, error/latency, and external workload rate over time.

The candidate clues above come from the benchmark evidence map; **they are not all claimed as verified**. The actual verified signal and result are in the decisive check above.
Access/coverage gap: This check does not prove the post-trigger counter deltas or retry-policy settings in Splunk; no Kubernetes-only gap is asserted.
Remedy: Compare bounded historical request/attempt deltas and inspect the scoped search/rate pod objects before claiming the full feedback loop.

## After the run: representative presence and delivery

| Signal/object class | Status | Query count |
|---|---|---:|
| events | missing | 0 |
| kubernetes_events | missing | 0 |
| logs | present | 1 |
| metrics | present | 1 |
| pods | missing | 0 |
| traces | present | 1 |

Collector closing audit: valid=`True`, drained=`True`. See [delivery_audit.json](delivery_audit.json) for sent/failure deltas and final queue sizes. The post-run query is a presence spot check; a zero can mean no matching object changed inside the short incident window, and a one is not a complete event count. The `kubernetes_events` signal and `events` object check are two query views, not proof of distinct event streams.

The [Splunk-visible ground-truth page](splunk_visible_ground_truth.md) records any independent post-grade golden query and separates it from the pre-agent gate.
