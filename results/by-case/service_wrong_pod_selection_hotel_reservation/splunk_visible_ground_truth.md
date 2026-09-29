# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-28T04:13:49.059376Z through 2026-09-28T04:16:10.808424Z UTC
- Pre-agent status: `symptom_confirmed`; visibility: `requires_additional_access`
- Evidence: A frontend Service selector also matches a search pod listening on 8082 rather than frontend's 5000.
- Access gap: The source frontend Service selector is not exported by the approved pod/event receiver; Splunk pod objects show both frontend and search have the selected label.
- Remedy: Inspect frontend Service and EndpointSlices through an authorized Kubernetes API, or separately approve Service object export.

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
