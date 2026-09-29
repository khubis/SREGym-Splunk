# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-27T23:18:55.946611Z through 2026-09-27T23:21:00.408157Z UTC
- Pre-agent status: `symptom_confirmed`; visibility: `requires_additional_access`
- Evidence: The cleanup controller repeatedly receives HTTP 403 while reconciling deletion.
- Access gap: The approved export omits the ConfigMap finalizer and the controller ClusterRole verbs. Run-scoped APM trace examples were not queryable at the gate time.
- Remedy: Read the ConfigMap and ClusterRole through Kubernetes, or authorize those object types for export. Recheck APM indexing and run-tag propagation before claiming trace coverage.

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
