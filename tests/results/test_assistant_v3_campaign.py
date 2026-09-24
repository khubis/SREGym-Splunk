from __future__ import annotations

import csv
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sregym.results.assistant_v3_campaign import (
    CampaignArtifactError,
    checkpoint_attempt,
    rebuild_campaign,
    record_golden_telemetry_audit,
)


FINAL_ANSWER = "The frontend-proxy WAF regex causes catastrophic backtracking and CPU saturation."


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _materialize_run(tmp_path: Path, *, graded: bool = True, answer: str = FINAL_ANSWER) -> tuple[Path, Path]:
    batch = tmp_path / "results" / "pilot"
    run = batch / "assistant_v3" / "edge_request_filter_cpu_saturation" / "run_1"
    _write_json(
        run / "assistant_v3" / "request.json",
        {
            "schema": "sregym.assistant_v3.request.v1",
            "problem_id": "edge_request_filter_cpu_saturation",
            "action_instructions": (
                "Telemetry time window: 2026-09-24T19:06:41.291000Z through "
                "2026-09-24T19:11:29.088000Z, inclusive. Investigate using only telemetry "
                "within this time window."
            ),
        },
    )
    events = [
        {
            "schema": "sregym.assistant_v3.events.v1",
            "problem_id": "edge_request_filter_cpu_saturation",
        },
        {
            "sequence": 1,
            "offset_ms": 1.0,
            "event": "message.complete",
            "event_id": "complete-1",
            "data": {"final_text": answer},
            "redacted": False,
        },
    ]
    event_path = run / "assistant_v3" / "events.jsonl"
    event_path.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")
    _write_json(
        run / "assistant_v3" / "terminal.json",
        {
            "schema": "sregym.assistant_v3.terminal.v1",
            "outcome": "completed",
            "final_text": answer,
            "submitted": True,
            "submission_count": 1,
        },
    )
    _write_json(
        run / "run_metadata.json",
        {
            "schema": "sregym.assistant_v3.run_metadata.v1",
            "problem_id": "edge_request_filter_cpu_saturation",
            "attempt": 1,
            "classification": "completed",
            "benchmark_profile": "svelte",
            "comparable": False,
        },
    )
    _write_json(run / "trajectory.json", {"schema_version": "ATIF-v1.7"})
    _write_json(run / "metrics.json", {"schema": "sregym.assistant_v3.metrics.v1"})
    if graded:
        result_path = run / "edge_request_filter_cpu_saturation_results.csv"
        with result_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "problem_id",
                    "attempt",
                    "run_status",
                    "Diagnosis.submission",
                    "Diagnosis.accuracy",
                    "Diagnosis.judgment",
                    "Diagnosis.reasoning",
                    "Diagnosis.success",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "problem_id": "edge_request_filter_cpu_saturation",
                    "attempt": 1,
                    "run_status": "complete",
                    "Diagnosis.submission": answer,
                    "Diagnosis.accuracy": "100.0",
                    "Diagnosis.judgment": "True",
                    "Diagnosis.reasoning": "Correctly localized frontend-proxy and identified the pathological WAF regex.",
                    "Diagnosis.success": "True",
                }
            )
    return batch, run


def test_checkpoint_writes_durable_scorecard_with_raw_provenance(tmp_path: Path) -> None:
    batch, run = _materialize_run(tmp_path)

    report = checkpoint_attempt(batch, run)

    text = report.read_text(encoding="utf-8")
    assert "| edge_request_filter_cpu_saturation | 1 | completed | 100.0 | True |" in text
    assert "Correctly localized frontend-proxy" in text
    assert "final_answer.md" in text
    assert "edge_request_filter_cpu_saturation_results.csv" in text
    assert "trajectory.json" in text
    assert "not_checked" in text
    ledger = (batch / "assistant_v3_campaign" / "progress.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(ledger) == 1
    record = json.loads(ledger[0])
    assert record["answer_sha256"]
    assert record["judge_sha256"]
    assert (run / "final_answer.md").read_text(encoding="utf-8") == FINAL_ANSWER + "\n"

    checkpoint_attempt(batch, run)
    assert len((batch / "assistant_v3_campaign" / "progress.jsonl").read_text().splitlines()) == 1


def test_checkpoint_rejects_a_judged_submission_that_differs_from_completion(tmp_path: Path) -> None:
    batch, run = _materialize_run(tmp_path)
    result_path = run / "edge_request_filter_cpu_saturation_results.csv"
    rows = list(csv.DictReader(result_path.open(newline="", encoding="utf-8")))
    rows[0]["Diagnosis.submission"] = "A different answer"
    with result_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(CampaignArtifactError, match="submitted diagnosis does not match"):
        checkpoint_attempt(batch, run)

    assert not (batch / "assistant_v3_campaign" / "progress.jsonl").exists()


def test_rebuild_ignores_only_a_truncated_final_ledger_record(tmp_path: Path) -> None:
    batch, run = _materialize_run(tmp_path)
    report = checkpoint_attempt(batch, run)
    ledger = batch / "assistant_v3_campaign" / "progress.jsonl"
    with ledger.open("ab") as handle:
        handle.write(b'{"schema":"truncated"')
        handle.flush()
        os.fsync(handle.fileno())

    rebuilt = rebuild_campaign(batch)

    assert rebuilt == report
    assert "edge_request_filter_cpu_saturation" in rebuilt.read_text(encoding="utf-8")


def test_golden_audit_requires_grading_and_records_post_grade_evidence(tmp_path: Path) -> None:
    ungraded_batch, ungraded_run = _materialize_run(tmp_path / "ungraded", graded=False)
    queried_at = datetime.now(UTC)
    evidence = [
        {
            "signal": "logs",
            "query": 'search index=main "frontend-proxy" "request_filter_eval"',
            "count": 12,
            "summary": "The faulty proxy emitted request-filter evaluation records.",
            "samples": ["request_filter_eval rule=catastrophic-regex duration=1s"],
        }
    ]
    with pytest.raises(CampaignArtifactError, match="graded result"):
        record_golden_telemetry_audit(
            ungraded_batch,
            ungraded_run,
            expected_root_cause="frontend-proxy pathological WAF regex",
            status="confirmed",
            queried_at=queried_at,
            evidence=evidence,
            access_note="Not assessed because this fixture is intentionally ungraded.",
        )

    batch, run = _materialize_run(tmp_path / "graded")
    result_path = run / "edge_request_filter_cpu_saturation_results.csv"
    graded_at = datetime.fromtimestamp(result_path.stat().st_mtime, tz=UTC)
    with pytest.raises(CampaignArtifactError, match="after grading"):
        record_golden_telemetry_audit(
            batch,
            run,
            expected_root_cause="frontend-proxy pathological WAF regex",
            status="confirmed",
            queried_at=graded_at - timedelta(seconds=1),
            evidence=evidence,
            access_note="The causal evidence is directly available in Splunk.",
        )

    audit_path = record_golden_telemetry_audit(
        batch,
        run,
        expected_root_cause="frontend-proxy pathological WAF regex",
        status="confirmed",
        queried_at=graded_at + timedelta(seconds=1),
        evidence=evidence,
        access_note="The causal evidence is directly available in Splunk; Kubernetes access is not required.",
    )

    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["status"] == "confirmed"
    assert audit["window"] == {
        "start": "2026-09-24T19:06:41.291000Z",
        "end": "2026-09-24T19:11:29.088000Z",
    }
    assert audit["expected_root_cause_sha256"]
    assert "Kubernetes access is not required" in audit["access_note"]
    assert audit["evidence"] == evidence
    scorecard = batch / "assistant_v3_campaign" / "scorecard.md"
    scorecard_text = scorecard.read_text(encoding="utf-8")
    assert "confirmed" in scorecard_text
    assert "faulty proxy emitted request-filter" in scorecard_text
    assert "telemetry proof" in scorecard_text
    assert "Kubernetes access is not required" in scorecard_text
    assert "| edge_request_filter_cpu_saturation | 1 | completed | 100.0 | True |" in scorecard_text
