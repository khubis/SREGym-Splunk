# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-27T20:34:12.208355Z through 2026-09-27T20:36:31.711651Z UTC
- Pre-agent status: `confirmed`; visibility: `undetermined`
- Evidence: Distinct Jobs show Completed archivers and running regular fluent-bit sidecars.
- Access gap: None established by this check.
- Remedy: No additional access remedy specified.

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
