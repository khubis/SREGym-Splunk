# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-28T22:16:21.818989Z through 2026-09-28T22:19:06.567380Z UTC
- Pre-agent status: `symptom_confirmed`; visibility: `partially_splunk_observable`
- Evidence: The injected fault is established at source; Splunk has the same named counters and sustained rate-service queue backlog.
- Access gap: This check does not prove the post-trigger counter deltas or retry-policy settings in Splunk; no Kubernetes-only gap is asserted.
- Remedy: Compare bounded historical request/attempt deltas and inspect the scoped search/rate pod objects before claiming the full feedback loop.

## Representative post-run Splunk checks

| Signal | Status | Count |
|---|---|---:|
| events | missing | 0 |
| kubernetes_events | missing | 0 |
| logs | present | 1 |
| metrics | present | 1 |
| pods | missing | 0 |
| traces | present | 1 |

## Independent post-grade audit

Status: `partial`. These Splunk metrics support sustained retry amplification and queue saturation in the exact prompt window. They do not independently expose the runtime retry-policy fields or prove the initial burst; the source injection log supplies that context. The agent localized the rate dependency but did not identify this feedback-loop mechanism.

[Query and sanitized samples](postgrade_golden_telemetry.json)
