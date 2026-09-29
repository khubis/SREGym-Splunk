# Splunk-visible ground truth and limits

This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.
The pre-agent check ran before V3 and does not use the oracle to guide its prompt.

- Incident window: 2026-09-28T21:52:52.706377Z through 2026-09-28T21:55:25.748907Z UTC
- Pre-agent status: `symptom_confirmed`; visibility: `requires_additional_access`
- Evidence: Two new Jaeger replicas share one claim under hostname anti-affinity; one runs while another remains Pending.
- Access gap: Splunk pod objects show shared claim, hostname anti-affinity, and a Pending replica, but not the PVC access mode or topology binding.
- Remedy: Inspect PVC/PV and Deployment through authorized Kubernetes API, or separately approve storage-object export.

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

Status: `partial`. Splunk pod objects demonstrate the split rollout but do not prove PVC ReadWriteOnce mode or PV topology binding. The pre-agent source Kubernetes proof confirmed ReadWriteOnce. Authorized PVC/PV and Deployment inspection, or approved storage-object export, is needed for the complete mechanism.

[Query and sanitized samples](postgrade_golden_telemetry.json)
