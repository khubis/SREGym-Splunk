# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-28T17:51:57.173677Z through 2026-09-28T17:54:19.475797Z UTC
- Pre-agent status: `symptom_confirmed`; visibility: `requires_additional_access`
- Evidence: New custom-service pods are stuck in init while the source Deployment allows all old replicas to become unavailable.
- Access gap: The stalled init pod is Splunk-visible, but the Deployment rollingUpdate maxUnavailable/maxSurge strategy is not in the approved pod/event export.
- Remedy: Use authorized read-only Deployment API access, or separately approve Deployment object export.

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
