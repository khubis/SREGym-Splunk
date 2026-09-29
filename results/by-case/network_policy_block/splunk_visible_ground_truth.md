# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-27T22:01:08.368776Z through 2026-09-27T22:03:28.781109Z UTC
- Pre-agent status: `symptom_confirmed`; visibility: `requires_additional_access`
- Evidence: See pre_agent_proof.json.
- Access gap: The approved pod/event export omits NetworkPolicy selector and ingress/egress rules. Run-scoped APM trace examples were not queryable at the gate time.
- Remedy: Read the NetworkPolicy through Kubernetes, or separately authorize that object type in the receiver. Recheck APM indexing and run-tag propagation before claiming trace coverage.

## Representative post-run Splunk checks

| Signal | Status | Count |
|---|---|---:|
| events | present | 1 |
| kubernetes_events | present | 1 |
| logs | present | 1 |
| metrics | present | 1 |
| pods | present | 1 |
| traces | present | 1 |

## Independent post-grade audit

No separate post-grade golden-telemetry API audit was saved for this attempt. Use the [pre-agent proof](pre_agent_proof.json); do not treat it as exhaustive parity.
