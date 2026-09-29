"""Crash-safe Assistant v3 pilot scorecards and post-grade telemetry audits."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sregym.conductor.problem_sets import SREGYM_LITE_PROBLEMS

CAMPAIGN_DIRNAME = "assistant_v3_campaign"
LEDGER_FILENAME = "progress.jsonl"
SCORECARD_FILENAME = "scorecard.md"
RESUME_FILENAME = "resume.csv"
AUDIT_FILENAME = "golden_telemetry.json"
FINAL_ANSWER_FILENAME = "final_answer.md"
SPLUNK_ASSESSMENT_FILENAME = "splunk_visible_assessment.json"
_AUDIT_STATUSES = frozenset({"confirmed", "partial", "missing", "not_checked"})
_SIGNALS = frozenset({"metrics", "traces", "logs", "kubernetes_events"})
_WINDOW_RE = re.compile(
    r"Telemetry time window:\s*(?P<start>\S+)\s+through\s+(?P<end>\S+?),\s+inclusive\."
)
_ANSWER_TIMESTAMP_RE = re.compile(
    r"(?<![A-Za-z0-9])\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)"
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


def _write_final_answer(run_dir: Path, answer: str | None) -> Path | None:
    """Write one concise derived view of the validated submitted answer."""
    path = run_dir / FINAL_ANSWER_FILENAME
    if answer is None:
        path.unlink(missing_ok=True)
        return None
    _atomic_write(path, (answer.rstrip() + "\n").encode())
    return path


def _causal_proof_summary(causal: dict[str, Any]) -> str:
    """Surface bounded causal counts without copying raw MELT into the scorecard."""
    details: list[str] = []
    check_id = causal.get("check_id")
    if isinstance(check_id, str) and check_id:
        details.append(check_id)
    for key, value in sorted(causal.items()):
        if key.endswith("_count") and isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            details.append(f"{key}={value}")
        elif key.endswith("_confirmed") and isinstance(value, bool):
            details.append(f"{key}={str(value).lower()}")
    interpretation = causal.get("interpretation")
    if isinstance(interpretation, str) and interpretation.strip():
        details.append(interpretation.strip())
    return "; ".join(details) or "Causal check recorded"


def _answer_window_warning(answer: str | None, instructions: Any) -> str | None:
    """Flag explicit answer timestamps outside the saved prompt, without regrading."""
    if answer is None or not isinstance(instructions, str):
        return None
    window = _WINDOW_RE.search(instructions)
    if window is None:
        return None
    try:
        start = datetime.fromisoformat(window.group("start").replace("Z", "+00:00"))
        end = datetime.fromisoformat(window.group("end").replace("Z", "+00:00"))
    except ValueError:
        return None
    cited = []
    for found in _ANSWER_TIMESTAMP_RE.findall(answer):
        try:
            cited.append(datetime.fromisoformat(found.replace("Z", "+00:00")))
        except ValueError:
            continue
    if any(value < start for value in cited):
        return "final answer cites telemetry before the prompt window"
    if any(value > end for value in cited):
        return "final answer cites telemetry after the prompt window"
    return None


def _build_snapshot(batch_dir: Path, run_dir: Path) -> dict[str, Any]:
    metadata = _read_json(run_dir / "run_metadata.json")
    request_path = run_dir / "assistant_v3" / "request.json"
    request = _read_json(request_path)
    answer, _, sequence = _final_answer(run_dir)
    judge, judge_path, result_row = _judge_result(run_dir, answer, sequence)
    assessment_path = run_dir / SPLUNK_ASSESSMENT_FILENAME
    if assessment_path.exists():
        assessment = _read_json(assessment_path)
        if (
            assessment.get("schema") != "sregym.splunk_visible_assessment.v1"
            or assessment.get("case_id") != metadata.get("problem_id")
            or assessment.get("answer_sha256") != (_sha256_text(answer) if answer is not None else None)
            or assessment.get("benchmark_judge_sha256") != (_sha256_file(judge_path) if judge_path else None)
        ):
            raise CampaignArtifactError("Splunk-visible assessment provenance differs from the graded answer")
    else:
        assessment = None
    answer_path = _write_final_answer(run_dir, answer)
    audit_path = run_dir / AUDIT_FILENAME
    audit = _read_json(audit_path) if audit_path.exists() else None
    trajectory_path = run_dir / "trajectory.json"
    if not trajectory_path.exists():
        nested_trajectory = run_dir / "trajectory" / "trajectory.json"
        trajectory_path = nested_trajectory if nested_trajectory.exists() else trajectory_path
    metrics_path = run_dir / "metrics.json"
    if not metrics_path.exists():
        raise CampaignArtifactError("Assistant attempt is missing derived trace metrics")
    metrics = _read_json(metrics_path)
    if metrics.get("schema") != "sregym.assistant_v3.metrics.v1":
        raise CampaignArtifactError("Assistant trace metrics have an unexpected schema")
    for key in ("tool_calls", "failed_tool_results"):
        value = metrics.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise CampaignArtifactError(f"Assistant trace metrics have invalid {key}")
    duration_ms = metrics.get("agent_duration_ms")
    if (
        not isinstance(duration_ms, (int, float))
        or isinstance(duration_ms, bool)
        or not math.isfinite(duration_ms)
        or duration_ms < 0
    ):
        raise CampaignArtifactError("Assistant trace metrics have invalid duration")
    total_tokens = metrics.get("total_tokens")
    if total_tokens is not None and (
        not isinstance(total_tokens, int) or isinstance(total_tokens, bool) or total_tokens < 0
    ):
        raise CampaignArtifactError("Assistant trace metrics have invalid token count")
    problem_id = metadata.get("problem_id") or run_dir.parent.name
    attempt = metadata.get("attempt")
    if not isinstance(problem_id, str) or not problem_id or not isinstance(attempt, int):
        raise CampaignArtifactError("run metadata is missing problem identity or attempt")
    pre_agent_path = run_dir / "splunk_lite_pre_agent.json"
    pre_agent = None
    if request.get("action_profile_id") == "sregym-symptom-window-v1":
        if not pre_agent_path.exists():
            if answer is not None:
                raise CampaignArtifactError("guided attempt lacks ready pre-agent evidence")
            pre_agent_path = None
        else:
            pre_agent = _read_json(pre_agent_path)
            match = _WINDOW_RE.search(str(request.get("action_instructions", "")))
            if (
                pre_agent.get("schema") != "sregym.splunk_lite_pre_agent.v1"
                or pre_agent.get("case_id") != problem_id
                or pre_agent.get("run_id") != metadata.get("run_id")
                or (
                    answer is not None
                    and (
                        match is None
                        or pre_agent.get("status") not in {"ready", "ready_data_limited"}
                        or (
                            pre_agent.get("status") == "ready_data_limited"
                            and (
                                pre_agent.get("causal", {}).get("visibility")
                                not in {"requires_additional_access", "partially_splunk_observable"}
                                or not pre_agent.get("causal", {}).get("access_gap")
                                or not pre_agent.get("causal", {}).get("remedy")
                            )
                        )
                        or pre_agent.get("window") != {"start": match.group("start"), "end": match.group("end")}
                    )
                )
            ):
                raise CampaignArtifactError("guided attempt has mismatched or incomplete pre-agent evidence")
    elif not pre_agent_path.exists():
        pre_agent_path = None
    pre_agent_causal = pre_agent.get("causal") if isinstance(pre_agent, dict) else None
    if not isinstance(pre_agent_causal, dict):
        pre_agent_causal = None
    delivery_path = run_dir / "splunk_lite_delivery.json"
    if delivery_path.exists():
        delivery_proof = _read_json(delivery_path)
        if (
            delivery_proof.get("schema") != "sregym.splunk_lite_delivery.v1"
            or delivery_proof.get("case_id") != problem_id
        ):
            raise CampaignArtifactError("Splunk Lite delivery proof does not match the attempt")
    else:
        delivery_path = None
    record: dict[str, Any] = {
        "schema": "sregym.assistant_v3.campaign_progress.v1",
        "recorded_at": _utc_now(),
        "problem_id": problem_id,
        "attempt": attempt,
        "run_path": _relative(batch_dir, run_dir),
        "status": str(metadata.get("classification") or _read_json(run_dir / "assistant_v3" / "terminal.json").get("outcome")),
        "telemetry_scope_warning": metadata.get("telemetry_scope_warning"),
        "answer_window_warning": _answer_window_warning(answer, request.get("action_instructions")),
        "run_metadata_path": _relative(batch_dir, run_dir / "run_metadata.json"),
        "score": judge.get("score") if judge else None,
        "verdict": judge.get("verdict") if judge else "ungraded",
        "rationale": judge.get("rationale") if judge else "No completed judge result is available.",
        "answer_path": _relative(batch_dir, answer_path),
        "answer_sha256": _sha256_text(answer) if answer is not None else None,
        "judge_path": _relative(batch_dir, judge_path),
        "judge_sha256": _sha256_file(judge_path) if judge_path is not None else None,
        "trajectory_path": _relative(batch_dir, trajectory_path) if trajectory_path.exists() else None,
        "metrics_path": _relative(batch_dir, metrics_path),
        "metrics_sha256": _sha256_file(metrics_path),
        "agent_duration_ms": duration_ms,
        "total_tokens": total_tokens,
        "tool_calls": metrics["tool_calls"],
        "failed_tool_results": metrics["failed_tool_results"],
        "native_trace_path": _relative(batch_dir, run_dir / "assistant_v3" / "events.jsonl"),
        "prompt_path": _relative(batch_dir, request_path),
        "pre_agent_path": _relative(batch_dir, pre_agent_path),
        "pre_agent_sha256": _sha256_file(pre_agent_path) if pre_agent_path is not None else None,
        "splunk_score": assessment.get("score") if assessment is not None else None,
        "splunk_verdict": assessment.get("verdict") if assessment is not None else "unverified",
        "splunk_rationale": assessment.get("rationale") if assessment is not None else "No secondary assessment.",
        "splunk_visibility": (
            assessment.get("visibility") if assessment is not None else
            pre_agent.get("causal", {}).get("visibility", "undetermined") if pre_agent is not None else "undetermined"
        ),
        "splunk_access_gap": (
            assessment.get("access_gap") if assessment is not None else
            " ".join(filter(None, (
                pre_agent.get("causal", {}).get("access_gap"),
                pre_agent.get("causal", {}).get("remedy"),
            ))) if pre_agent is not None else "Not assessed."
        ),
        "splunk_assessment_path": _relative(batch_dir, assessment_path) if assessment is not None else None,
        "splunk_assessment_sha256": _sha256_file(assessment_path) if assessment is not None else None,
        "audit_path": _relative(batch_dir, audit_path) if audit is not None else None,
        "audit_status": (
            str(audit.get("status")) if audit is not None else
            f"pre-agent {pre_agent_causal['status']}" if pre_agent_causal and pre_agent_causal.get("status") else
            "not_checked"
        ),
        "audit_summary": (
            " ".join(
                str(item.get("summary", "")).strip()
                for item in audit.get("evidence", [])
                if isinstance(item, dict) and str(item.get("summary", "")).strip()
            )
            if audit is not None
            else (
                f"{_causal_proof_summary(pre_agent_causal)}; "
                "independent post-grade audit pending."
                if pre_agent_causal is not None else "Golden telemetry has not been checked."
            )
        ),
        "access_note": (
            str(audit.get("access_note", "")).strip()
            if audit is not None
            else (
                "Pending independent post-grade verification."
                if judge is not None
                else "Not assessed because the attempt was not graded."
            )
        ),
        "audit_sha256": _sha256_file(audit_path) if audit is not None else None,
        "delivery_proof_path": _relative(batch_dir, delivery_path),
        "delivery_proof_sha256": _sha256_file(delivery_path) if delivery_path is not None else None,
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
        "Derived from immutable per-attempt artifacts. Pre-agent causal checks and independent post-grade audits are labeled separately; neither changes the score.",
        "",
        "| Incident | Attempt | Status | Score | Reason | Rationale | Runtime / tokens / tools (failed) | Golden telemetry | Access / ingestion note | Splunk-visible score | Splunk visibility / gap | Provenance |",
        "|---|---:|---|---:|---|---|---|---|---|---:|---|---|",
    ]
    for record in records:
        provenance = [
            _link(report_dir, batch_dir, record.get("answer_path"), "answer"),
            _link(report_dir, batch_dir, record.get("prompt_path"), "starter prompt"),
            _link(report_dir, batch_dir, record.get("run_metadata_path"), "run metadata"),
            _link(report_dir, batch_dir, record.get("judge_path"), "judge"),
            _link(report_dir, batch_dir, record.get("native_trace_path"), "native trace"),
            _link(report_dir, batch_dir, record.get("trajectory_path"), "trace"),
            _link(report_dir, batch_dir, record.get("metrics_path"), "metrics"),
            _link(report_dir, batch_dir, record.get("pre_agent_path"), "pre-agent proof"),
            _link(report_dir, batch_dir, record.get("splunk_assessment_path"), "Splunk judge"),
            _link(report_dir, batch_dir, record.get("audit_path"), "telemetry proof"),
            _link(report_dir, batch_dir, record.get("delivery_proof_path"), "delivery"),
        ]
        score = "—" if record.get("score") is None else f"{float(record['score']):.1f}"
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_text(record["problem_id"]),
                    str(record["attempt"]),
                    _markdown_text(
                        record["status"] + (
                            " (" + "; ".join(
                                label for enabled, label in (
                                    (record.get("telemetry_scope_warning"), "scope warning"),
                                    (record.get("answer_window_warning"), "answer-window warning"),
                                ) if enabled
                            ) + ")"
                            if record.get("telemetry_scope_warning") or record.get("answer_window_warning")
                            else ""
                        )
                    ),
                    score,
                    _markdown_text(record["verdict"]),
                    _markdown_text(record["rationale"], limit=220),
                    _markdown_text(
                        f"{float(record['agent_duration_ms']) / 1000:.1f}s / "
                        f"{record['total_tokens'] if record['total_tokens'] is not None else 'unreported'} / "
                        f"{record['tool_calls']} / {record['failed_tool_results']}"
                    ),
                    _markdown_text(
                        f"{record['audit_status']} — {record.get('audit_summary', '')}",
                        limit=260,
                    ),
                    _markdown_text(record.get("access_note"), limit=220),
                    "unverified" if record.get("splunk_score") is None else f"{float(record['splunk_score']):.1f}",
                    _markdown_text(
                        f"{record.get('splunk_visibility', 'undetermined')} — {record.get('splunk_access_gap', '')}",
                        limit=220,
                    ),
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


def record_splunk_visible_assessment(
    batch_dir: Path | str,
    run_dir: Path | str,
    *,
    score: float,
    verdict: str,
    rationale: str,
    visibility: str,
    access_gap: str,
    judge_model: str,
    judge_backend: str,
    raw_judge: Mapping[str, Any],
) -> Path:
    """Save a separately labeled judgment; never alter the benchmark result."""
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 100:
        raise CampaignArtifactError("Splunk-visible score must be a finite value from 0 to 100")
    if visibility not in {
        "fully_splunk_observable", "partially_splunk_observable",
        "requires_additional_access", "undetermined",
    }:
        raise CampaignArtifactError("invalid Splunk visibility classification")
    if not all(isinstance(value, str) and value.strip() for value in (
        verdict, rationale, access_gap, judge_model, judge_backend,
    )):
        raise CampaignArtifactError("Splunk-visible judgment provenance is incomplete")
    if not isinstance(raw_judge, Mapping):
        raise CampaignArtifactError("raw Splunk-visible judge result must be an object")
    run = Path(run_dir).resolve()
    answer, _, sequence = _final_answer(run)
    benchmark_judge, judge_path, _ = _judge_result(run, answer, sequence)
    if answer is None or benchmark_judge is None or judge_path is None:
        raise CampaignArtifactError("Splunk-visible judgment requires a graded Assistant answer")
    metadata = _read_json(run / "run_metadata.json")
    payload = {
        "schema": "sregym.splunk_visible_assessment.v1",
        "rubric_version": "splunk-visible-rca-v1",
        "case_id": metadata.get("problem_id"),
        "answer_sha256": _sha256_text(answer),
        "benchmark_judge_sha256": _sha256_file(judge_path),
        "score": float(score),
        "verdict": verdict.strip(),
        "rationale": rationale.strip(),
        "visibility": visibility,
        "access_gap": access_gap.strip(),
        "judge_model": judge_model.strip(),
        "judge_backend": judge_backend.strip(),
        "raw_judge": dict(raw_judge),
    }
    serialized = _json_bytes(payload)
    if any(marker in serialized.decode().lower() for marker in _SECRET_MARKERS):
        raise CampaignArtifactError("Splunk-visible judgment contains credential-like text")
    path = run / SPLUNK_ASSESSMENT_FILENAME
    _atomic_write(path, serialized)
    checkpoint_attempt(batch_dir, run)
    return path


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
    access_note: str,
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
    if not isinstance(access_note, str) or not access_note.strip():
        raise CampaignArtifactError("golden telemetry access note is required")
    if any(marker in access_note.lower() for marker in _SECRET_MARKERS):
        raise CampaignArtifactError("golden telemetry access note contains credential-like text")
    graded_at = datetime.fromtimestamp(judge_path.stat().st_mtime, tz=UTC)
    if query_time < graded_at:
        raise CampaignArtifactError("golden telemetry query must occur after grading")
    normalized_evidence: list[dict[str, Any]] = []
    for item in evidence:
        signal = item.get("signal")
        query = item.get("query")
        count = item.get("count")
        summary = item.get("summary")
        samples = item.get("samples", [])
        if (
            signal not in _SIGNALS
            or not isinstance(query, str)
            or not query.strip()
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 0
            or not isinstance(summary, str)
            or not summary.strip()
            or not isinstance(samples, list)
            or any(not isinstance(sample, str) or not sample.strip() for sample in samples)
            or len(samples) > 20
        ):
            raise CampaignArtifactError("golden telemetry evidence is invalid")
        proof_text = " ".join([query, summary, *samples]).lower()
        if any(marker in proof_text for marker in _SECRET_MARKERS):
            raise CampaignArtifactError("golden telemetry evidence contains credential-like text")
        normalized = {"signal": signal, "query": query, "count": count, "summary": summary}
        if samples:
            normalized["samples"] = samples
        normalized_evidence.append(normalized)
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
        "access_note": access_note.strip(),
        "evidence": normalized_evidence,
    }
    path = run / AUDIT_FILENAME
    _atomic_write(path, _json_bytes(audit))
    checkpoint_attempt(batch, run)
    return path
