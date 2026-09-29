> **Root cause:** cleanup-controller repeatedly attempted controller-owned cleanup of ConfigMap reservation-cleanup-token in hotel-reservation and the Kubernetes API rejected those attempts with HTTP 403 Forbidden, preventing successful reconciliation.

**Status:** mechanism found but trigger missing

**Confidence:** medium

**Why this conclusion:** The `cleanup-controller` started successfully and remained active, with no observed restart pattern. From 2026-09-27T23:19:02.469Z through 2026-09-27T23:20:59.309Z, 59 cleanup attempts for the terminating `reservation-cleanup-token` ConfigMap were immediately followed by Kubernetes API `403 Forbidden` failures. The reservation, search, profile, and rate services showed zero APM errors in the same window, making an application-service or database-latency fault a weaker explanation. The telemetry does not identify the exact denied Kubernetes verb, resource/subresource, ServiceAccount, Role, or RoleBinding.

**Impact:** 59 failed cleanup reconciliations were observed; cleanup delay duration and number of affected reservations were not directly measured.

**Calibration:** Not calibrated — 2026-09-27T23:18:55.946611Z–2026-09-27T23:21:00.408157Z. Problem onset, recovery, and episode completeness were not established because no cleanup-specific metric or exact cleanup operation signal was available.

**Caveat:** The change that initiated the authorization failures remains unknown.

**Next check:** Obtain a bounded Kubernetes audit or RBAC authorization record for `cleanup-controller` targeting `reservation-cleanup-token` to identify the denied verb and resource/subresource.
