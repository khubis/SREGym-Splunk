"""Bounded collection readiness; no fault, API investigation or grade retries."""

import hashlib

import httpx

from .driver import Hold


class TelemetryPending(Hold):
    def __init__(self, reason, **counts):
        super().__init__(reason)
        self.evidence = {"reason": reason, **counts}


def metric_identity(metrics, actual_uids):
    if not actual_uids:
        raise TelemetryPending("live_pod_cohort_empty")
    metric_uids = {row["metric"].get("uid") for row in metrics}
    if not metrics:
        raise TelemetryPending("metric_samples_empty", metric_samples=0)
    if not metric_uids or None in metric_uids or "" in metric_uids:
        raise TelemetryPending("metric_uid_label_missing", metric_samples=len(metrics))
    if not metric_uids <= actual_uids:
        raise TelemetryPending(
            "metric_uid_outside_live_cohort",
            metric_samples=len(metrics),
            live_pods=len(actual_uids),
            unmatched_uids=len(metric_uids - actual_uids),
        )
    return metric_uids


def wait_for_telemetry(probe, record, *, clock, sleep, limit=120, interval=2):
    if limit <= 0 or interval <= 0:
        raise ValueError("Positive bounded setup timing is required")
    start = clock()
    deadline = start + limit
    observations = []
    while clock() < deadline:
        try:
            result = probe()
        except httpx.HTTPStatusError as rejected:
            response = rejected.response
            record(
                {
                    "phase": "held",
                    "limit_seconds": limit,
                    "observations": observations,
                    "rejected_http_response": {
                        "reason": "telemetry_http_status_rejected",
                        "status_code": response.status_code,
                        "method": rejected.request.method,
                        "path_sha256": hashlib.sha256(rejected.request.url.path.encode()).hexdigest(),
                        "body_bytes": len(response.content),
                        "body_sha256": hashlib.sha256(response.content).hexdigest(),
                    },
                }
            )
            raise
        except (TelemetryPending, httpx.TimeoutException) as pending:
            evidence = (
                pending.evidence
                if isinstance(pending, TelemetryPending)
                else {"reason": "telemetry_transport_timeout", "error_type": type(pending).__name__}
            )
            observations.append({"elapsed_seconds": round(clock() - start, 3), **evidence})
            record({"phase": "waiting", "limit_seconds": limit, "observations": observations})
            remaining = deadline - clock()
            if remaining > 0:
                sleep(min(interval, remaining))
        else:
            if clock() >= deadline:
                break
            receipt = {
                "phase": "ready",
                "elapsed_seconds": round(clock() - start, 3),
                "limit_seconds": limit,
                "observations": observations,
                "accepted": result,
            }
            record(receipt)
            return result
    record({"phase": "held", "limit_seconds": limit, "observations": observations})
    raise Hold("Telemetry did not meet the unchanged live-UID/log predicate within its setup deadline")
