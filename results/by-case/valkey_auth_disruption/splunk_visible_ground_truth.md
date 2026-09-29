# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-28T04:59:19.417229Z through 2026-09-28T05:00:39.869280Z UTC
- Pre-agent status: `symptom_confirmed`; visibility: `requires_additional_access`
- Evidence: Cart logs show a cache connection failure in source and Splunk; they do not by themselves establish why Valkey refused the connection.
- Access gap: Cart authentication errors are log-visible, but the live Valkey requirepass setting is not in the approved Splunk telemetry.
- Remedy: Inspect the Valkey runtime configuration through authorized application/Kubernetes access without exporting password values.

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
