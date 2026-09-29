# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-28T05:18:58.787831Z through 2026-09-28T05:20:38.492650Z UTC
- Pre-agent status: `symptom_confirmed`; visibility: `requires_additional_access`
- Evidence: The source Deployment marks the currently running product-catalog pod as the credential-source pod, and the same Secret-referencing pod is visible in Splunk.
- Access gap: Splunk pod objects show the startup Secret reference, but neither the rotated Secret value nor live PostgreSQL credential state is in the approved export.
- Remedy: Use authorized Secret metadata and backend-auth state checks without exposing credential values; do not ingest raw Secrets.

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
