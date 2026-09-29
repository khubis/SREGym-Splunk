> **Root cause:** The Hotel Reservation `recommendation` pod was stopped, and its ReplicaSet could not create a replacement because the `pod-policy.validation.k8s.io` admission webhook referenced a missing `pod-policy-webhook` Service in the `policy-system` namespace. This reduced available recommendation capacity while requests continued.

**Status:** likely cause

**Confidence:** medium

**Why this conclusion:** At 2026-09-28T06:06:32.968Z, kubelet stopped the `hotel-reserv-recommendation` container in pod `recommendation-8488cd7dc7-8jpfs`. Immediately afterward, ReplicaSet `recommendation-8488cd7dc7` repeatedly reported `FailedCreate` because `pod-policy-webhook.policy-system.svc` was not found, with failures continuing through 06:07:55.059Z. The recommendation service still received traffic and reported 182 requests with zero errors, making an application-level error storm or demand cessation less likely than a Kubernetes replacement-capacity failure.

**Impact:** Recommendation capacity was reduced during the bounded window. The aggregate recommendation counter remained active but dipped during the period; the metric was available only at an effective 60-second resolution. User-level impact was not directly measured.

**Calibration:** Not calibrated — 2026-09-28T06:06:30.890892Z–2026-09-28T06:08:28.265467Z. Problem onset, recovery, and episode completeness were not established.

**Next check:** Restore and validate the `pod-policy-webhook` Service and its ready endpoints in `policy-system`, then verify that the recommendation ReplicaSet reaches its desired ready-pod count.
