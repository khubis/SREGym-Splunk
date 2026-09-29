"""Reusable, bounded Splunk delivery check for a registered Lite attempt.

This proves only that representative run-scoped signals can be queried. It does
not infer the oracle or claim complete telemetry coverage; those require the
separate, post-grade case evidence review.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import yaml

from sregym.conductor.problem_sets import SREGYM_LITE_PROBLEMS
from sregym.observability.base import ApplicationScope, AttemptContext, SignalName
from sregym.observability.splunk import SIGNALFLOW_INGESTION_LAG, SplunkConfig, SplunkHttpBackend
from sregym.results.assistant_v3_campaign import _atomic_write
from sregym.results.splunk_lite_cases import load_prompt_recipe

CASE_ROOT = Path(__file__).resolve().parents[2] / "cases" / "splunk-lite"
METADATA_ROOT = Path(__file__).resolve().parents[1] / "service" / "metadata"
EVIDENCE_FILE = "splunk_lite_delivery.json"
_WINDOW = re.compile(r"Telemetry time window:\s*(\S+)\s+through\s+(\S+?),\s+inclusive\.")
_METADATA_FILE = {
    "hotel_reservation": "hotel-reservation.json",
    "social_network": "social-network.json",
    "astronomy_shop": "astronomy-shop.json",
}
_SIGNALS: tuple[SignalName, ...] = ("metrics", "traces", "logs", "kubernetes_events")
_OBJECTS = ("pods", "events")


class EvidenceError(ValueError):
    """The attempt is not safe or complete enough to query as a Lite case."""


class EvidenceBackend(Protocol):
    def query_signal(
        self, signal: SignalName, context: AttemptContext, scope: ApplicationScope,
        connection_id: str, checked_at: datetime, timeout_seconds: float,
    ) -> int: ...

    def query_object(
        self, kind: str, context: AttemptContext, scope: ApplicationScope,
        connection_id: str, checked_at: datetime, timeout_seconds: float,
    ) -> int: ...


def _json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError(f"missing or invalid attempt artifact: {path.name}") from error
    if not isinstance(value, dict):
        raise EvidenceError(f"attempt artifact is not an object: {path.name}")
    return value


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError("invalid incident UTC window") from error
    if not value.endswith("Z") or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise EvidenceError("incident window must use UTC Z timestamps")
    return parsed


def verify_case_delivery(
    run_dir: Path | str,
    *,
    backend: EvidenceBackend,
    expected_connection: str,
    timeout_seconds: float = 10.0,
    query_attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Query six bounded signal classes and atomically save sanitized counts."""
    if query_attempts < 1:
        raise EvidenceError("query_attempts must be positive")
    run = Path(run_dir).resolve()
    metadata = _json_object(run / "run_metadata.json")
    request = _json_object(run / "assistant_v3" / "request.json")
    case_id = metadata.get("problem_id")
    if case_id not in SREGYM_LITE_PROBLEMS:
        raise EvidenceError("attempt is not a registered Lite case")
    if metadata.get("observability_provider") != "splunk":
        raise EvidenceError("attempt did not use the Splunk provider")
    if metadata.get("logs_connection_id") != expected_connection or not expected_connection:
        raise EvidenceError("attempt Logs connection differs from the requested target")
    case_dir = CASE_ROOT / case_id
    prompt_recipe = yaml.safe_load((case_dir / "prompt.yaml").read_text(encoding="utf-8"))
    if request.get("prompt_profile_id") != prompt_recipe["profile_id"] or prompt_recipe.get("hint") is not None:
        raise EvidenceError("attempt prompt differs from the approved baseline recipe")
    instructions = request.get("action_instructions")
    if not isinstance(instructions, str):
        raise EvidenceError("attempt has no exact incident window")
    action_profile = request.get("action_profile_id")
    if action_profile == "sregym-symptom-window-v1":
        symptom = load_prompt_recipe(case_id).symptom
        if not instructions.endswith(f"\nObserved symptom: {symptom}") or instructions.count("Observed symptom:") != 1:
            raise EvidenceError("attempt does not contain the exact reviewed symptom")
    elif action_profile is not None or "Observed symptom:" in instructions:
        raise EvidenceError("attempt has an unapproved symptom profile")
    match = _WINDOW.search(instructions)
    if match is None:
        raise EvidenceError("attempt has no exact incident window")
    start_text, end_text = match.groups()
    start, end = _utc(start_text), _utc(end_text)
    if start >= end:
        raise EvidenceError("incident window is empty or reversed")
    run_id = metadata.get("run_id")
    if not isinstance(run_id, str):
        raise EvidenceError("attempt has no opaque run identity")
    if run_id in str(request.get("prompt", "")) or run_id in instructions:
        raise EvidenceError("opaque run identity leaked into Assistant instructions")
    app_id = prompt_recipe["application"]
    app = _json_object(METADATA_ROOT / _METADATA_FILE[app_id])
    namespace = app.get("Namespace")
    if not isinstance(namespace, str) or not namespace:
        raise EvidenceError("application namespace is unavailable")
    context = AttemptContext(run_id, str(metadata.get("benchmark_profile")), bool(metadata.get("comparable")), start)
    scope = ApplicationScope(app_name=str(app["Name"]), namespaces=(namespace,))

    checks: dict[str, dict[str, int | str | None]] = {}
    for signal in _SIGNALS + _OBJECTS:
        for attempt in range(query_attempts):
            try:
                if signal in _OBJECTS:
                    count = backend.query_object(signal, context, scope, expected_connection, end, timeout_seconds)
                else:
                    # SignalFlow subtracts its ingest-lag guard from `checked_at`.
                    # Offset only metrics so its actual query stops at the saved
                    # incident end, not two minutes before a short incident began.
                    query_end = end + SIGNALFLOW_INGESTION_LAG if signal == "metrics" else end
                    count = backend.query_signal(signal, context, scope, expected_connection, query_end, timeout_seconds)
            except Exception:  # Backend details may contain credentials or telemetry payloads.
                checks[signal] = {"status": "query_error", "count": None}
            else:
                if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    checks[signal] = {"status": "query_error", "count": None}
                else:
                    checks[signal] = {"status": "present" if count else "missing", "count": count}
            if checks[signal]["status"] == "present" or attempt == query_attempts - 1:
                break
            sleep(1.0)

    proof = {
        "schema": "sregym.splunk_lite_delivery.v1",
        "case_id": case_id,
        "run_id": run_id,
        "attempt": metadata.get("attempt"),
        "window": {"start": start_text, "end": end_text},
        "logs_connection_id": expected_connection,
        "namespace": namespace,
        "checks": checks,
        "source_comparison": "unverified",
        "oracle_evidence": "unverified",
        "scope_note": "Representative run-scoped presence checks only; not exhaustive or oracle proof.",
    }
    path = run / EVIDENCE_FILE
    _atomic_write(path, (json.dumps(proof, sort_keys=True, indent=2) + "\n").encode())
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Query bounded Splunk Lite delivery evidence")
    parser.add_argument("--run-dir", type=Path, required=True)
    arguments = parser.parse_args()
    configuration = SplunkConfig.from_env()
    if not configuration.logs_connection_id:
        parser.error("SPLUNK_LOGS_CONNECTION_ID must name the target Logs connection")
    backend = SplunkHttpBackend(configuration)
    try:
        path = verify_case_delivery(
            arguments.run_dir, backend=backend,
            expected_connection=configuration.logs_connection_id,
        )
    finally:
        backend.close()
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
