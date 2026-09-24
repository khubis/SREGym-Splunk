"""Crash-safe Assistant v3 pilot scorecards and post-grade telemetry audits."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from sregym.conductor.problem_sets import SREGYM_LITE_PROBLEMS

CAMPAIGN_DIRNAME = "assistant_v3_campaign"
LEDGER_FILENAME = "progress.jsonl"
SCORECARD_FILENAME = "scorecard.md"
RESUME_FILENAME = "resume.csv"
AUDIT_FILENAME = "golden_telemetry.json"
_AUDIT_STATUSES = frozenset({"confirmed", "partial", "missing", "not_checked"})
_SIGNALS = frozenset({"metrics", "traces", "logs", "kubernetes_events"})
_WINDOW_RE = re.compile(
    r"Telemetry time window:\s*(?P<start>\S+)\s+through\s+(?P<end>\S+?),\s+inclusive\."
)
_SECRET_MARKERS = ("authorization:", "bearer ", "x-sf-token", "hec_token", "token=")


class CampaignArtifactError(ValueError):
    """Raised when result provenance is missing or internally inconsistent."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _append_record(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        written = os.write(descriptor, payload)
        if written != len(payload):
            raise OSError("campaign ledger record was not written completely")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CampaignArtifactError(f"artifact is not readable JSON: {path}") from error
    if not isinstance(value, dict):
        raise CampaignArtifactError(f"artifact is not a JSON object: {path}")
    return value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _message_completion(run_dir: Path) -> tuple[str | None, Path, int | None]:
    path = run_dir / "assistant_v3" / "events.jsonl"
    completions: list[tuple[str, int]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[1:]
    except (OSError, UnicodeError) as error:
        raise CampaignArtifactError(f"Assistant event stream is unavailable: {path}") from error
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise CampaignArtifactError(f"Assistant event stream is malformed: {path}") from error
        if event.get("event") != "message.complete":
            continue
        data = event.get("data")
        text = data.get("final_text", data.get("text")) if isinstance(data, dict) else None
        sequence = event.get("sequence")
        if not isinstance(text, str) or not text.strip() or not isinstance(sequence, int):
            raise CampaignArtifactError("Assistant completion is missing final text or sequence")
        completions.append((text, sequence))
    if len(completions) > 1:
        raise CampaignArtifactError("Assistant event stream contains multiple completions")
    if not completions:
        return None, path, None
    return completions[0][0], path, completions[0][1]


def _final_answer(run_dir: Path) -> tuple[str | None, Path, int | None]:
    terminal_path = run_dir / "assistant_v3" / "terminal.json"
    terminal = _read_json(terminal_path)
    event_text, event_path, sequence = _message_completion(run_dir)
    terminal_text = terminal.get("final_text")
    outcome = terminal.get("outcome")
    if outcome == "completed":
        if terminal.get("submitted") is not True or terminal.get("submission_count") != 1:
            raise CampaignArtifactError("completed Assistant terminal was not submitted exactly once")
        if not isinstance(terminal_text, str) or terminal_text != event_text:
            raise CampaignArtifactError("terminal diagnosis does not match Assistant completion")
        return terminal_text, terminal_path, sequence
    if terminal_text is not None:
        raise CampaignArtifactError("non-completed Assistant terminal contains submitted diagnosis text")
    return event_text, event_path, sequence


def _result_csv(run_dir: Path) -> tuple[Path | None, dict[str, str] | None]:
    for path in sorted(run_dir.glob("*_results.csv")):
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, UnicodeError, csv.Error) as error:
            raise CampaignArtifactError(f"result CSV is unreadable: {path}") from error
        if len(rows) == 1:
            return path, rows[0]
    return None, None


def _float_or_none(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise CampaignArtifactError(f"judge score is not numeric: {value!r}") from error


def _judge_result(
    run_dir: Path, answer: str | None, completion_sequence: int | None
) -> tuple[dict[str, Any] | None, Path | None, dict[str, str] | None]:
    recovery_path = run_dir / "recovery" / "diagnosis_evaluation.json"
    if recovery_path.exists():
        value = _read_json(recovery_path)
        source = value.get("source")
        if not isinstance(source, dict) or source.get("sequence") != completion_sequence:
            raise CampaignArtifactError("recovered evaluation does not reference the completed answer")
        return (
            {
                "score": _float_or_none(value.get("accuracy")),
                "verdict": str(value.get("judgment", "")),
                "rationale": str(value.get("reasoning", "")),
                "success": value.get("success"),
            },
            recovery_path,
            None,
        )

    path, row = _result_csv(run_dir)
    if path is None or row is None or not str(row.get("Diagnosis.accuracy", "")).strip():
        return None, path, row
    submitted = row.get("Diagnosis.submission")
    if answer is None or submitted != answer:
        raise CampaignArtifactError("submitted diagnosis does not match the completed Assistant answer")
    return (
        {
            "score": _float_or_none(row.get("Diagnosis.accuracy")),
            "verdict": str(row.get("Diagnosis.judgment", "")),
            "rationale": str(row.get("Diagnosis.reasoning", "")),
            "success": str(row.get("Diagnosis.success", "")).strip().lower() == "true",
        },
        path,
        row,
    )


def _relative(batch_dir: Path, path: Path | None) -> str | None:
    return os.path.relpath(path.resolve(), batch_dir.resolve()) if path is not None else None


def _build_snapshot(batch_dir: Path, run_dir: Path) -> dict[str, Any]:
    metadata = _read_json(run_dir / "run_metadata.json")
    answer, answer_path, sequence = _final_answer(run_dir)
    judge, judge_path, result_row = _judge_result(run_dir, answer, sequence)
    audit_path = run_dir / AUDIT_FILENAME
    audit = _read_json(audit_path) if audit_path.exists() else None
    trajectory_path = run_dir / "trajectory.json"
    if not trajectory_path.exists():
        nested_trajectory = run_dir / "trajectory" / "trajectory.json"
        trajectory_path = nested_trajectory if nested_trajectory.exists() else trajectory_path
    problem_id = metadata.get("problem_id") or run_dir.parent.name
    attempt = metadata.get("attempt")
    if not isinstance(problem_id, str) or not problem_id or not isinstance(attempt, int):
        raise CampaignArtifactError("run metadata is missing problem identity or attempt")
    record: dict[str, Any] = {
        "schema": "sregym.assistant_v3.campaign_progress.v1",
        "recorded_at": _utc_now(),
        "problem_id": problem_id,
        "attempt": attempt,
        "run_path": _relative(batch_dir, run_dir),
        "status": str(metadata.get("classification") or _read_json(run_dir / "assistant_v3" / "terminal.json").get("outcome")),
        "score": judge.get("score") if judge else None,
        "verdict": judge.get("verdict") if judge else "ungraded",
        "rationale": judge.get("rationale") if judge else "No completed judge result is available.",
        "answer_path": _relative(batch_dir, answer_path),
        "answer_sha256": _sha256_text(answer) if answer is not None else None,
        "judge_path": _relative(batch_dir, judge_path),
        "judge_sha256": _sha256_file(judge_path) if judge_path is not None else None,
        "trajectory_path": _relative(batch_dir, trajectory_path) if trajectory_path.exists() else None,
        "audit_path": _relative(batch_dir, audit_path) if audit is not None else None,
        "audit_status": str(audit.get("status")) if audit is not None else "not_checked",
        "audit_sha256": _sha256_file(audit_path) if audit is not None else None,
        "result_row": result_row,
    }
    fingerprint_value = {key: value for key, value in record.items() if key not in {"recorded_at", "result_row"}}
    record["fingerprint"] = _sha256_text(json.dumps(fingerprint_value, sort_keys=True, separators=(",", ":")))
    return record


def _read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw_lines = path.read_bytes().splitlines()
    except OSError as error:
        raise CampaignArtifactError(f"campaign ledger is unreadable: {path}") from error
    records: list[dict[str, Any]] = []
    for index, line in enumerate(raw_lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if index == len(raw_lines) - 1:
                break
            raise CampaignArtifactError("campaign ledger contains a malformed non-terminal record") from None
        if not isinstance(value, dict) or value.get("schema") != "sregym.assistant_v3.campaign_progress.v1":
            raise CampaignArtifactError("campaign ledger contains an invalid record")
        records.append(value)
    return records


def _latest_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, int, str], dict[str, Any]] = {}
    for record in records:
        key = (str(record["problem_id"]), int(record["attempt"]), str(record["run_path"]))
        latest[key] = record
    order = {problem_id: index for index, problem_id in enumerate(SREGYM_LITE_PROBLEMS)}
    return sorted(
        latest.values(),
        key=lambda item: (order.get(str(item["problem_id"]), len(order)), int(item["attempt"]), str(item["run_path"])),
    )


def _markdown_text(value: Any, *, limit: int | None = None) -> str:
    text = " ".join(str(value or "").split()).replace("|", "\\|")
    if limit is not None and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _link(report_dir: Path, batch_dir: Path, relative_path: str | None, label: str) -> str | None:
    if not relative_path:
        return None
    target = (batch_dir / relative_path).resolve()
    return f"[{label}]({os.path.relpath(target, report_dir.resolve())})"


def _render_scorecard(batch_dir: Path, records: list[dict[str, Any]]) -> bytes:
    report_dir = batch_dir / CAMPAIGN_DIRNAME
    lines = [
        "# Assistant v3 SREGym-Lite Pilot",
        "",
        "Derived from immutable per-attempt artifacts. Golden telemetry is checked only after grading and never changes the score.",
        "",
        "| Incident | Attempt | Status | Score | Reason | Rationale | Golden telemetry | Provenance |",
        "|---|---:|---|---:|---|---|---|---|",
    ]
    for record in records:
        provenance = [
            _link(report_dir, batch_dir, record.get("answer_path"), "answer"),
            _link(report_dir, batch_dir, record.get("judge_path"), "judge"),
            _link(report_dir, batch_dir, record.get("trajectory_path"), "trace"),
            _link(report_dir, batch_dir, record.get("audit_path"), "audit"),
        ]
        score = "—" if record.get("score") is None else f"{float(record['score']):.1f}"
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_text(record["problem_id"]),
                    str(record["attempt"]),
                    _markdown_text(record["status"]),
                    score,
                    _markdown_text(record["verdict"]),
                    _markdown_text(record["rationale"], limit=220),
                    _markdown_text(record["audit_status"]),
                    " · ".join(item for item in provenance if item) or "—",
                ]
            )
            + " |"
        )
    lines.extend(["", f"Cases recorded: {len(records)}", ""])
    return "\n".join(lines).encode()


def _write_resume_csv(path: Path, records: list[dict[str, Any]]) -> None:
    rows = [record["result_row"] for record in records if isinstance(record.get("result_row"), dict)]
    if not rows:
        path.unlink(missing_ok=True)
        return
    fields = sorted({key for row in rows for key in row})
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def rebuild_campaign(batch_dir: Path | str) -> Path:
    """Rebuild the Markdown scorecard and resume CSV from the durable ledger."""
    batch = Path(batch_dir).resolve()
    campaign_dir = batch / CAMPAIGN_DIRNAME
    records = _latest_records(_read_ledger(campaign_dir / LEDGER_FILENAME))
    scorecard = campaign_dir / SCORECARD_FILENAME
    _atomic_write(scorecard, _render_scorecard(batch, records))
    _write_resume_csv(campaign_dir / RESUME_FILENAME, records)
    return scorecard


def checkpoint_attempt(batch_dir: Path | str, run_dir: Path | str) -> Path:
    """Append one idempotent attempt snapshot, then atomically rebuild reports."""
    batch = Path(batch_dir).resolve()
    run = Path(run_dir).resolve()
    record = _build_snapshot(batch, run)
    ledger = batch / CAMPAIGN_DIRNAME / LEDGER_FILENAME
    records = _read_ledger(ledger)
    prior = next(
        (
            item
            for item in reversed(records)
            if item.get("problem_id") == record["problem_id"]
            and item.get("attempt") == record["attempt"]
            and item.get("run_path") == record["run_path"]
        ),
        None,
    )
    if prior is None or prior.get("fingerprint") != record["fingerprint"]:
        _append_record(ledger, record)
    return rebuild_campaign(batch)


def _parse_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CampaignArtifactError("queried_at must be timezone-aware")
    return value.astimezone(UTC)


def _telemetry_window(run_dir: Path) -> dict[str, str]:
    request = _read_json(run_dir / "assistant_v3" / "request.json")
    instructions = request.get("action_instructions")
    match = _WINDOW_RE.search(instructions) if isinstance(instructions, str) else None
    if match is None:
        raise CampaignArtifactError("Assistant request does not contain a parseable telemetry window")
    return {"start": match.group("start"), "end": match.group("end")}


def record_golden_telemetry_audit(
    batch_dir: Path | str,
    run_dir: Path | str,
    *,
    expected_root_cause: str,
    status: str,
    queried_at: datetime,
    evidence: list[dict[str, Any]],
) -> Path:
    """Persist a sanitized root-cause-aware audit only after diagnosis grading."""
    if status not in _AUDIT_STATUSES or status == "not_checked":
        raise CampaignArtifactError("golden telemetry status must be confirmed, partial, or missing")
    batch = Path(batch_dir).resolve()
    run = Path(run_dir).resolve()
    answer, _, sequence = _final_answer(run)
    judge, judge_path, _ = _judge_result(run, answer, sequence)
    if judge is None or judge_path is None:
        raise CampaignArtifactError("golden telemetry audit requires a graded result")
    query_time = _parse_utc(queried_at)
    graded_at = datetime.fromtimestamp(judge_path.stat().st_mtime, tz=UTC)
    if query_time < graded_at:
        raise CampaignArtifactError("golden telemetry query must occur after grading")
    normalized_evidence: list[dict[str, Any]] = []
    for item in evidence:
        signal = item.get("signal")
        query = item.get("query")
        count = item.get("count")
        summary = item.get("summary")
        if (
            signal not in _SIGNALS
            or not isinstance(query, str)
            or not query.strip()
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            or not isinstance(summary, str)
            or not summary.strip()
        ):
            raise CampaignArtifactError("golden telemetry evidence is invalid")
        if any(marker in query.lower() or marker in summary.lower() for marker in _SECRET_MARKERS):
            raise CampaignArtifactError("golden telemetry evidence contains credential-like text")
        normalized_evidence.append(
            {"signal": signal, "query": query, "count": count, "summary": summary}
        )
    audit = {
        "schema": "sregym.golden_telemetry_audit.v1",
        "problem_id": _read_json(run / "run_metadata.json").get("problem_id"),
        "queried_at": query_time.isoformat().replace("+00:00", "Z"),
        "graded_artifact": judge_path.name,
        "graded_artifact_sha256": _sha256_file(judge_path),
        "window": _telemetry_window(run),
        "status": status,
        "expected_root_cause": expected_root_cause,
        "expected_root_cause_sha256": _sha256_text(expected_root_cause),
        "evidence": normalized_evidence,
    }
    path = run / AUDIT_FILENAME
    _atomic_write(path, _json_bytes(audit))
    checkpoint_attempt(batch, run)
    return path
