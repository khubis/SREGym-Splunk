# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-28T17:34:28.054142Z through 2026-09-28T17:36:36.116930Z UTC
- Pre-agent status: `symptom_confirmed`; visibility: `requires_additional_access`
- Evidence: The faulty selector and empty endpoints are proven at source; Splunk shows failed workload requests but not the selector mechanism.
- Access gap: The Service selector and endpoint set are Kubernetes API objects not exported by the approved pod/event receiver.
- Remedy: Use authorized read-only Kubernetes Service and Endpoints access, or separately approve their telemetry export.

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
