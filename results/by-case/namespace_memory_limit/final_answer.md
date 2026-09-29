> **Finding:** During the `search` rollout, the old pod was replaced by `search-67459c68b5-9pvxs`, which remained unready; the Deployment consequently had zero ready pods. The exact readiness failure trigger is unavailable.

**Status:** mechanism found but trigger missing

**Confidence:** low

**Why this conclusion:** In cluster `anon_049fb41a2bc039f9894771f022dbcd09`, the `search` Deployment had one ready pod at **2026-09-28T04:31:00Z**, then the replacement pod was not ready by **04:31:10Z**. Restart count stayed at zero, and both the previous and replacement pods used the same image, weakening crash-loop and image-regression explanations. The available telemetry establishes a rollout-induced readiness failure, but the pod event, probe output, and application startup logs needed to identify the precise trigger were unavailable.

**Impact:** The `search` Deployment reached **0 ready pods** during the requested window, so the Hotel Reservation search rollout did not become ready.

**Calibration:** Not calibrated — **2026-09-28T04:31:02.704731Z–2026-09-28T04:33:47.563618Z**. Problem onset, recovery, and episode completeness were not established.

**Caveat:** The exact readiness cause—such as a failing readiness/startup probe, dependency initialization failure, or rollout configuration issue—cannot be distinguished because the deployment-history query timed out and the bounded search-container logs, Kubernetes events, and APM service-health queries returned no usable rows.

**Next check:** Inspect Kubernetes pod conditions and readiness/startup probe output for `search-67459c68b5-9pvxs` and its rollout revision.
