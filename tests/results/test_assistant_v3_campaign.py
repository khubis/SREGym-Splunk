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
            "run_id": "anon_0123456789abcdef0123456789abcdef",
        },
    )
    _write_json(run / "trajectory.json", {"schema_version": "ATIF-v1.7"})
    _write_json(
        run / "metrics.json",
        {
            "schema": "sregym.assistant_v3.metrics.v1",
            "agent_duration_ms": 1200.0,
            "total_tokens": 125,
            "tool_calls": 2,
            "failed_tool_results": 1,
        },
    )
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
    assert "[metrics](" in text
    assert "125" in text
    assert "2 / 1" in text
    assert "not_checked" in text
    assert "Pending independent post-grade verification." in text
    ledger = (batch / "assistant_v3_campaign" / "progress.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(ledger) == 1
    record = json.loads(ledger[0])
    assert record["answer_sha256"]
    assert record["judge_sha256"]
    assert record["metrics_sha256"]
    assert record["total_tokens"] == 125
    assert record["tool_calls"] == 2
    assert record["failed_tool_results"] == 1
    assert (run / "final_answer.md").read_text(encoding="utf-8") == FINAL_ANSWER + "\n"

    checkpoint_attempt(batch, run)
    assert len((batch / "assistant_v3_campaign" / "progress.jsonl").read_text().splitlines()) == 1


def test_guided_scope_drift_is_visible_with_metadata_provenance(tmp_path: Path) -> None:
    batch, run = _materialize_run(tmp_path)
    metadata_path = run / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["telemetry_scope_warning"] = "Assistant queried telemetry outside the instructed time window."
    _write_json(metadata_path, metadata)

    scorecard = checkpoint_attempt(batch, run).read_text(encoding="utf-8")

    record = json.loads((batch / "assistant_v3_campaign" / "progress.jsonl").read_text().splitlines()[0])
    assert record["telemetry_scope_warning"] == metadata["telemetry_scope_warning"]
    assert "completed (scope warning)" in scorecard
    assert "[run metadata](" in scorecard


def test_answer_timestamp_outside_prompt_window_is_flagged_without_changing_score(tmp_path: Path) -> None:
    answer = FINAL_ANSWER + " The episode continued through 2026-09-24T19:12:50Z."
    batch, run = _materialize_run(tmp_path, answer=answer)

    scorecard = checkpoint_attempt(batch, run).read_text(encoding="utf-8")

    record = json.loads((batch / "assistant_v3_campaign" / "progress.jsonl").read_text().splitlines()[0])
    assert record["score"] == 100.0
    assert record["answer_window_warning"] == "final answer cites telemetry after the prompt window"
    assert "answer-window warning" in scorecard


@pytest.mark.parametrize(
    "bad_metrics",
    [
        None,
        {"schema": "unknown", "agent_duration_ms": 1, "total_tokens": 1, "tool_calls": 1, "failed_tool_results": 0},
        {"schema": "sregym.assistant_v3.metrics.v1", "agent_duration_ms": -1, "total_tokens": 1, "tool_calls": 1, "failed_tool_results": 0},
        {"schema": "sregym.assistant_v3.metrics.v1", "agent_duration_ms": 1, "total_tokens": -1, "tool_calls": 1, "failed_tool_results": 0},
        {"schema": "sregym.assistant_v3.metrics.v1", "agent_duration_ms": 1, "total_tokens": 1, "tool_calls": -1, "failed_tool_results": 0},
        {"schema": "sregym.assistant_v3.metrics.v1", "agent_duration_ms": 1, "total_tokens": 1, "tool_calls": 1, "failed_tool_results": -1},
    ],
)
def test_checkpoint_rejects_missing_or_invalid_derived_metrics(tmp_path: Path, bad_metrics: dict | None) -> None:
    batch, run = _materialize_run(tmp_path)
    metrics_path = run / "metrics.json"
    if bad_metrics is None:
        metrics_path.unlink()
    else:
        _write_json(metrics_path, bad_metrics)

    with pytest.raises(CampaignArtifactError):
        checkpoint_attempt(batch, run)


def test_ungraded_attempt_explains_why_access_was_not_assessed(tmp_path: Path) -> None:
    batch, run = _materialize_run(tmp_path, graded=False)

    report = checkpoint_attempt(batch, run)

    text = report.read_text(encoding="utf-8")
    assert "Not assessed because the attempt was not graded." in text
    assert "Pending independent post-grade verification." not in text


def test_scorecard_links_separate_run_scoped_delivery_proof(tmp_path: Path) -> None:
    batch, run = _materialize_run(tmp_path)
    _write_json(run / "splunk_lite_delivery.json", {
        "schema": "sregym.splunk_lite_delivery.v1",
        "case_id": "edge_request_filter_cpu_saturation",
        "checks": {"metrics": {"status": "present", "count": 1}},
        "oracle_evidence": "unverified",
    })

    report = checkpoint_attempt(batch, run)
    text = report.read_text(encoding="utf-8")
    assert "[delivery](" in text
    assert "splunk_lite_delivery.json" in text
    ledger = (batch / "assistant_v3_campaign" / "progress.jsonl").read_text().splitlines()
    assert json.loads(ledger[0])["delivery_proof_sha256"]


def test_guided_attempt_requires_ready_pre_agent_proof_and_links_exact_prompt(tmp_path: Path) -> None:
    batch, run = _materialize_run(tmp_path)
    request_path = run / "assistant_v3" / "request.json"
    request = json.loads(request_path.read_text())
    request["action_profile_id"] = "sregym-symptom-window-v1"
    request_path.write_text(json.dumps(request))
    with pytest.raises(CampaignArtifactError, match="pre-agent evidence"):
        checkpoint_attempt(batch, run)

    _write_json(run / "splunk_lite_pre_agent.json", {
        "schema": "sregym.splunk_lite_pre_agent.v1",
        "case_id": "edge_request_filter_cpu_saturation",
        "status": "missing_delivery",
    })
    with pytest.raises(CampaignArtifactError, match="pre-agent evidence"):
        checkpoint_attempt(batch, run)

    _write_json(run / "splunk_lite_pre_agent.json", {
        "schema": "sregym.splunk_lite_pre_agent.v1",
        "case_id": "edge_request_filter_cpu_saturation",
        "run_id": "anon_0123456789abcdef0123456789abcdef",
        "status": "ready",
        "window": {"start": "2026-09-24T19:06:41.291000Z", "end": "2026-09-24T19:11:29.088000Z"},
    })
    mismatched = json.loads((run / "splunk_lite_pre_agent.json").read_text())
    mismatched["run_id"] = "anon_11111111111111111111111111111111"
    _write_json(run / "splunk_lite_pre_agent.json", mismatched)
    with pytest.raises(CampaignArtifactError, match="pre-agent evidence"):
        checkpoint_attempt(batch, run)
    mismatched["run_id"] = "anon_0123456789abcdef0123456789abcdef"
    mismatched["causal"] = {
        "status": "confirmed",
        "check_id": "slow_request_filter_event",
        "interpretation": "The exact pathological filter event was present in source and Splunk logs.",
        "source_count": 85,
        "splunk_count": 27,
    }
    mismatched["signals"] = {"metrics": 1, "traces": 1, "logs": 1, "pods": 1, "events": 1}
    _write_json(run / "splunk_lite_pre_agent.json", mismatched)
    report = checkpoint_attempt(batch, run)
    text = report.read_text()
    assert "[starter prompt](" in text
    assert "[native trace](" in text
    assert "[pre-agent proof](" in text
    assert "splunk_lite_pre_agent.json" in text
    assert "pre-agent confirmed" in text
    assert "source_count=85" in text
    assert "splunk_count=27" in text
    assert "independent post-grade audit pending" in text


def test_ungraded_guided_pre_agent_failure_keeps_proof_but_never_gets_a_score(tmp_path: Path) -> None:
    batch, run = _materialize_run(tmp_path, graded=False)
    request_path = run / "assistant_v3" / "request.json"
    request = json.loads(request_path.read_text())
    request["action_profile_id"] = "sregym-symptom-window-v1"
    request_path.write_text(json.dumps(request))
    terminal_path = run / "assistant_v3" / "terminal.json"
    terminal = json.loads(terminal_path.read_text())
    terminal.update({"outcome": "pre_agent_failure", "final_text": None, "submitted": False, "submission_count": 0})
    terminal_path.write_text(json.dumps(terminal))
    events_path = run / "assistant_v3" / "events.jsonl"
    events = events_path.read_text().splitlines()
    events_path.write_text(events[0] + "\n")
    _write_json(run / "splunk_lite_pre_agent.json", {
        "schema": "sregym.splunk_lite_pre_agent.v1",
        "case_id": "edge_request_filter_cpu_saturation",
        "run_id": "anon_0123456789abcdef0123456789abcdef",
        "status": "query_error",
    })
    report = checkpoint_attempt(batch, run)
    text = report.read_text()
    assert "[pre-agent proof](" in text
    assert "unverified" in text
    assert "100.0" not in text


def test_guided_data_limited_case_requires_explicit_access_gap(tmp_path: Path) -> None:
    batch, run = _materialize_run(tmp_path)
    request_path = run / "assistant_v3" / "request.json"
    request = json.loads(request_path.read_text())
    request["action_profile_id"] = "sregym-symptom-window-v1"
    request_path.write_text(json.dumps(request))
    proof = {
        "schema": "sregym.splunk_lite_pre_agent.v1",
        "case_id": "edge_request_filter_cpu_saturation",
        "run_id": "anon_0123456789abcdef0123456789abcdef",
        "status": "ready_data_limited",
        "window": {"start": "2026-09-24T19:06:41.291000Z", "end": "2026-09-24T19:11:29.088000Z"},
        "causal": {"visibility": "fully_splunk_observable"},
    }
    _write_json(run / "splunk_lite_pre_agent.json", proof)
    with pytest.raises(CampaignArtifactError, match="pre-agent evidence"):
        checkpoint_attempt(batch, run)

    proof["causal"]["visibility"] = "requires_additional_access"
    _write_json(run / "splunk_lite_pre_agent.json", proof)
    with pytest.raises(CampaignArtifactError, match="pre-agent evidence"):
        checkpoint_attempt(batch, run)
    proof["causal"]["access_gap"] = "Policy rules are absent from the approved pod/event export."
    proof["causal"]["remedy"] = "Authorize NetworkPolicy objects or use read-only Kubernetes access."
    _write_json(run / "splunk_lite_pre_agent.json", proof)
    report = checkpoint_attempt(batch, run)
    text = report.read_text()
    assert "[pre-agent proof](" in text
    assert "Policy rules are absent" in text
    assert "Authorize NetworkPolicy" in text


def test_guided_partial_metric_evidence_can_be_checkpointed_with_explicit_gap(tmp_path: Path) -> None:
    batch, run = _materialize_run(tmp_path)
    request_path = run / "assistant_v3" / "request.json"
    request = json.loads(request_path.read_text())
    request["action_profile_id"] = "sregym-symptom-window-v1"
    _write_json(request_path, request)
    _write_json(run / "splunk_lite_pre_agent.json", {
        "schema": "sregym.splunk_lite_pre_agent.v1",
        "case_id": "edge_request_filter_cpu_saturation",
        "run_id": "anon_0123456789abcdef0123456789abcdef",
        "status": "ready_data_limited",
        "window": {"start": "2026-09-24T19:06:41.291000Z", "end": "2026-09-24T19:11:29.088000Z"},
        "causal": {
            "visibility": "partially_splunk_observable",
            "access_gap": "Named counters are visible, but this check does not establish their incident-time deltas.",
            "remedy": "Compare bounded historical request and attempt deltas.",
        },
    })

    scorecard = checkpoint_attempt(batch, run).read_text(encoding="utf-8")

    assert "Named counters are visible" in scorecard
    assert "Compare bounded historical" in scorecard


def test_scorecard_keeps_secondary_splunk_score_separate_from_benchmark_score(tmp_path: Path) -> None:
    batch, run = _materialize_run(tmp_path)
    from sregym.results.assistant_v3_campaign import record_splunk_visible_assessment

    assessment = record_splunk_visible_assessment(
        batch, run,
        score=75.0,
        verdict="partial",
        rationale="The answer identifies the affected frontend but misses the request-filter mechanism.",
        visibility="fully_splunk_observable",
        access_gap="No extra access required for the RCA; pod/template access would confirm the configuration.",
        judge_model="test-judge",
        judge_backend="api",
        raw_judge={"score": 75.0, "critique": "Partial causal account"},
    )
    saved = json.loads(assessment.read_text())
    assert saved["score"] == 75.0
    assert saved["answer_sha256"]
    report = checkpoint_attempt(batch, run)
    text = report.read_text()
    assert "Splunk-visible score" in text
    assert "75.0" in text
    assert "[Splunk judge](" in text
    assert "| edge_request_filter_cpu_saturation | 1 | completed | 100.0 | True |" in text
    assert (run / "edge_request_filter_cpu_saturation_results.csv").read_text().find("75.0") == -1


def test_secondary_splunk_assessment_rejects_changed_answer_and_out_of_range_score(tmp_path: Path) -> None:
    batch, run = _materialize_run(tmp_path)
    from sregym.results.assistant_v3_campaign import record_splunk_visible_assessment

    arguments = dict(
        score=101.0, verdict="partial", rationale="Some support", visibility="undetermined",
        access_gap="Needs review", judge_model="test-judge", judge_backend="api", raw_judge={},
    )
    with pytest.raises(CampaignArtifactError, match="score"):
        record_splunk_visible_assessment(batch, run, **arguments)
    assert not (run / "splunk_visible_assessment.json").exists()

    arguments["score"] = 75.0
    record_splunk_visible_assessment(batch, run, **arguments)
    result_path = run / "edge_request_filter_cpu_saturation_results.csv"
    contents = result_path.read_text()
    result_path.write_text(contents.replace("Correctly localized frontend-proxy", "Changed judge rationale"))
    with pytest.raises(CampaignArtifactError, match="assessment provenance"):
        checkpoint_attempt(batch, run)


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
