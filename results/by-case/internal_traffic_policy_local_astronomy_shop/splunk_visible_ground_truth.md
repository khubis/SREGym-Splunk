# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-28T03:46:13.118664Z through 2026-09-28T03:48:36.778055Z UTC
- Pre-agent status: `symptom_confirmed`; visibility: `requires_additional_access`
- Evidence: New frontend and recommendation pods are on different nodes; source Service policy is Local, but Splunk only proves placement.
- Access gap: Service spec.internalTrafficPolicy is not exported by the approved pods/events object receiver; use authorized Kubernetes Service API or separately approved export.
- Remedy: Inspect the recommendation Service through an authorized Kubernetes API; export Service objects only after separate approval.

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
