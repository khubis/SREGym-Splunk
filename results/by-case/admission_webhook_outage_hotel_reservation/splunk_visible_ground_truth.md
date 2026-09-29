# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-28T06:06:30.890892Z through 2026-09-28T06:08:28.265467Z UTC
- Pre-agent status: `symptom_confirmed`; visibility: `requires_additional_access`
- Evidence: Recommendation ReplicaSet cannot create a replacement pod because a policy webhook call fails.
- Access gap: The FailedCreate event is Splunk-visible, but the webhook failure policy and backend Service configuration are not in the approved pod/event export.
- Remedy: Inspect ValidatingWebhookConfiguration and backend Service through authorized Kubernetes API, or separately approve their export.

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
