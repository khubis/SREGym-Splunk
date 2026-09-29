# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-27T22:57:21.205723Z through 2026-09-27T22:59:45.421062Z UTC
- Pre-agent status: `symptom_confirmed`; visibility: `requires_additional_access`
- Evidence: A newly admitted nginx-thrift pod has 16Mi memory request/limit and an OOMKilled container.
- Access gap: Pod objects show the 16Mi OOM symptom, but not the active mutating webhook or Deployment-template discrepancy.
- Remedy: Inspect the Deployment and MutatingWebhookConfigurations through the Kubernetes API, or authorize their export.

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
