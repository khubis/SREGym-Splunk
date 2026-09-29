> **Finding:** The user-timeline-service process started at 2026-09-28T17:34:28.870Z, but the available evidence does not establish that startup caused nginx-thrift connection refusals, that user-service was healthy, or what triggered the startup.

**Status:** insufficient evidence

**Confidence:** low

**Why this conclusion:** nginx-thrift logged connection refusals to `user-timeline-service.social-network.svc.cluster.local:9090`, followed by Thrift timeouts and additional connection-refused failures. A `user-timeline-service` startup message occurred shortly afterward, making a startup/listener gap plausible, but no readiness, listener-bind, termination, restart, or deployment-trigger evidence was available to prove causation. Separately, `user-service` produced 99 OpenTelemetry export errors because `otel-collector:4317` could not be resolved; this is an observed telemetry fault, not proof of the application request failure.

**Impact:** Within the requested window, nginx-thrift recorded 2 initial connection refusals to `user-timeline-service`, 86 later compose-post connection-refused records, and 20 Thrift transport timeouts. APM recorded 103 `user-service` requests with zero reported errors, followed by no recorded requests for approximately the final 108 seconds.

**Calibration:** Not calibrated — 2026-09-28T17:34:28.054142Z through 2026-09-28T17:36:36.116930Z. Problem onset, recovery, and episode completeness were not established.

**Caveat:** The nginx-thrift refusal interval is not independently evidenced in this ledger

**Next check:** Capture a direct pre-start workload-control or runtime-readiness record immediately before or spanning 2026-09-28T17:34:28.870Z to determine whether startup created the listener gap and what triggered it.
