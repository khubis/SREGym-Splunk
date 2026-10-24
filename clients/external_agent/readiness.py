"""External pre-deployment guard orchestration; original predicate stays authoritative."""

from .driver import Hold


def wait_for_preservation(check, record, *, clock, sleep, limit=120, interval=5):
    if limit <= 0 or interval <= 0:
        raise ValueError("Positive bounded pre-deployment timing is required")
    started = clock()
    deadline = started + limit
    observations = []
    while True:
        if clock() >= deadline:
            record({"phase": "held", "elapsed_seconds": clock() - started, "observations": observations})
            raise Hold("External pre-deployment readiness deadline exceeded")
        try:
            current = check()
        except Hold as error:
            if str(error) != "An original protected Pod lost readiness":
                raise
            observations.append({"elapsed_seconds": clock() - started, "reason": "protected_readiness_pending"})
            record({"phase": "waiting", "elapsed_seconds": clock() - started, "observations": list(observations)})
            sleep(min(interval, max(0, deadline - clock())))
            continue
        if clock() >= deadline:
            record({"phase": "held", "elapsed_seconds": clock() - started, "observations": observations})
            raise Hold("External pre-deployment readiness deadline exceeded")
        record({"phase": "ready", "elapsed_seconds": clock() - started, "observations": observations})
        return current


PENDING_MESSAGES = {"An original protected Pod lost readiness", "An unrelated original Pod lost readiness"}


def guard_with_wait(check, record, *, clock, sleep, limit=120, interval=5):
    if limit <= 0 or interval <= 0:
        raise ValueError("Positive bounded timing required")
    started = clock()
    deadline = started + limit
    observations = []
    while clock() < deadline:
        try:
            value = check()
        except Hold as error:
            if str(error) not in PENDING_MESSAGES:
                raise
            observations.append(
                {
                    "elapsed_seconds": clock() - started,
                    "reason": "protected_pod_readiness_pending",
                    "source_message": str(error),
                }
            )
            record({"phase": "waiting", "elapsed_seconds": clock() - started, "observations": list(observations)})
            sleep(min(interval, max(0, deadline - clock())))
            continue
        if clock() >= deadline:
            break
        record({"phase": "ready", "elapsed_seconds": clock() - started, "observations": observations})
        return value
    record({"phase": "held", "elapsed_seconds": clock() - started, "observations": observations})
    raise Hold("External strict preservation readiness deadline exceeded")
