"""Focused integrity tests for the local per-case result view."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from sregym.results.lite_case_dossiers import build


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _pilot(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    case_id = "example_case"
    case = root / "cases/splunk-lite" / case_id
    case.mkdir(parents=True)
    (case / "prompt.yaml").write_text("profile_id: example\n", encoding="utf-8")
    (case / "ground_truth.yaml").write_text(
        "problem_id: example_case\noracle_source: sregym/conductor/problems/example.py\n",
        encoding="utf-8",
    )
    oracle = root / "sregym/conductor/problems/example.py"
    oracle.parent.mkdir(parents=True)
    oracle.write_text(
        "class Example:\n"
        "    def __init__(self):\n"
        "        self.root_cause = self.build_structured_root_cause(\n"
        "            component='service-x', description='queue saturation'\n"
        "        )\n",
        encoding="utf-8",
    )
    rubric = root / "sregym/conductor/oracles/llm_as_a_judge/rca_checklists.yaml"
    rubric.parent.mkdir(parents=True)
    rubric.write_text("version: test\n", encoding="utf-8")
    (root / "main.py").write_text(
        "def gate(conductor):\n"
        "    if conductor.problem_id == 'example_case':\n"
        "        return wait_for_example_pre_agent()\n",
        encoding="utf-8",
    )
    verifier = root / "sregym/results/splunk_lite_causal.py"
    verifier.parent.mkdir(parents=True)
    verifier.write_text(
        "def check_example():\n    return True\n\n"
        "def verify_example_pre_agent():\n    return check_example()\n\n"
        "def wait_for_example_pre_agent():\n    return verify_example_pre_agent()\n",
        encoding="utf-8",
    )
    (root / "sregym/results/splunk_lite_evidence.py").write_text(
        "def verify_case_delivery():\n    return True\n",
        encoding="utf-8",
    )
    batch = root / "results/batch1"
    scorecard = batch / "assistant_v3_campaign/scorecard.md"
    scorecard.parent.mkdir(parents=True)
    scorecard.write_text("# Raw scorecard\n", encoding="utf-8")
    run = batch / "assistant_v3" / case_id / "run_1"
    run.parent.mkdir(parents=True, exist_ok=True)
    (run.parent / "phases_attempt1.jsonl").write_text(
        '{"phase":"inject_fault","event":"end","outcome":"ok"}\n',
        encoding="utf-8",
    )
    _json(
        run / "run_metadata.json",
        {
            "classification": "completed",
            "problem_id": case_id,
            "readiness_report": {
                "ready": True,
                "signals": [
                    {"signal": "metrics", "ready": True, "evidence": {"count": 1}},
                ],
            },
        },
    )
    _json(run / "observability/delivery.json", {"valid": True})
    _json(
        run / "assistant_v3/request.json",
        {
            "problem_id": case_id,
            "prompt": "Find the cause.",
            "action_instructions": "Telemetry time window: start through end.",
        },
    )
    _json(run / "assistant_v3/terminal.json", {"outcome": "completed", "submitted": True})
    _json(
        run / "splunk_lite_pre_agent.json",
        {
            "window": {"start": "start", "end": "end"},
            "status": "ready_data_limited",
            "required_signals": ["metrics"],
            "signals": {"metrics": 1},
            "causal": {
                "check_id": "example",
                "status": "symptom_confirmed",
                "signal": "application_metrics",
                "visibility": "partially_splunk_observable",
                "interpretation": "The queue is saturated.",
                "source_metric_count": 1,
                "splunk_metric_count": 1,
                "metric_names": ["queue_depth"],
            },
        },
    )
    _json(run / "splunk_lite_delivery.json", {"checks": {"metrics": {"status": "present", "count": 1}}})
    _json(
        run / "metrics.json",
        {
            "agent_duration_ms": 1000,
            "total_tokens": 20,
            "tool_calls": 2,
            "failed_tool_results": 0,
        },
    )
    (run / "assistant_v3/events.jsonl").write_text('{"event":1}\n', encoding="utf-8")
    _json(run / "trajectory.json", {"steps": []})
    (run / "final_answer.md").write_text("A queue filled.\n", encoding="utf-8")
    with (run / f"{case_id}_results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "Diagnosis.accuracy",
                "Diagnosis.judgment",
                "Diagnosis.submission",
                "Diagnosis.dimensions",
            ],
        )
        writer.writeheader()
        writer.writerow(
            {
                "Diagnosis.accuracy": "78.0",
                "Diagnosis.judgment": "True",
                "Diagnosis.submission": "A queue filled.",
                "Diagnosis.dimensions": "{'D1': {'name': 'Fault Localization', 'score': 1.0}}",
            }
        )
    report = root / "results/pilot.md"
    report.write_text(
        "| Case | Score |\n|---|---:|\n| [Example](batch1/assistant_v3_campaign/scorecard.md) | 78 | details |\n",
        encoding="utf-8",
    )
    return root, report, run


def test_dossier_copies_exact_artifacts_and_separates_evidence(tmp_path: Path) -> None:
    root, report, run = _pilot(tmp_path)
    # A failed neighboring attempt must not replace the selected valid result.
    invalid = run.parent / "run_2"
    _json(invalid / "run_metadata.json", {"classification": "infrastructure_invalid", "problem_id": "example_case"})
    _json(invalid / "observability/delivery.json", {"valid": True})

    folders = build(report, root / "results/by-case", root)

    assert len(folders) == 1
    folder = folders[0]
    assert not (folder / "native_trace.jsonl").is_symlink()
    assert (folder / "native_trace.jsonl").read_bytes() == (run / "assistant_v3/events.jsonl").read_bytes()
    assert (folder / "judge_raw.csv").read_bytes() == (run / "example_case_results.csv").read_bytes()
    assert (folder / "phase_ledger.jsonl").read_bytes() == (run.parent / "phases_attempt1.jsonl").read_bytes()
    assert (folder / "assistant_terminal.json").read_bytes() == (run / "assistant_v3/terminal.json").read_bytes()
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["original_attempt"] == "results/batch1/assistant_v3/example_case/run_1"
    assert len(manifest["artifact_sha256"]["native_trace.jsonl"]) == 64
    assert "Find the cause." in (folder / "starter_prompt.md").read_text()
    assert "Telemetry time window: start through end." in (folder / "starter_prompt.md").read_text()
    assert "queue saturation" in (folder / "benchmark_ground_truth.md").read_text()
    visible = (folder / "splunk_visible_ground_truth.md").read_text()
    assert "not** a separately graded" in visible
    assert "No separate post-grade" in visible
    assert "78/100" in (folder / "rubric_and_metrics.md").read_text()
    verification = (folder / "verification.md").read_text()
    assert "wait_for_example_pre_agent" in verification
    assert "The queue is saturated." in verification
    assert "`queue_depth`" in verification
    assert "metrics | 1 | yes" in verification
    assert (folder / "pre_agent_verifier_source.py").is_file()
    (run / "assistant_v3/events.jsonl").unlink()
    assert (folder / "native_trace.jsonl").is_file()


def test_dossier_refuses_to_overwrite_modified_copy(tmp_path: Path) -> None:
    root, report, _ = _pilot(tmp_path)
    folder = build(report, root / "results/by-case", root)[0]
    (folder / "final_answer.md").write_text("Locally edited.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="modified artifact"):
        build(report, root / "results/by-case", root)


def test_dossier_fails_closed_on_invalid_delivery(tmp_path: Path) -> None:
    root, report, run = _pilot(tmp_path)
    _json(run / "observability/delivery.json", {"valid": False})

    with pytest.raises(ValueError, match="one valid scored attempt"):
        build(report, root / "results/by-case", root)
    assert not (root / "results/by-case").exists()


def test_dossier_fails_closed_on_answer_judge_mismatch(tmp_path: Path) -> None:
    root, report, run = _pilot(tmp_path)
    (run / "final_answer.md").write_text("A different answer.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="judge/final-answer mismatch"):
        build(report, root / "results/by-case", root)
    assert not (root / "results/by-case").exists()


def test_dossier_selects_named_case_in_multi_case_batch(tmp_path: Path) -> None:
    root, report, run = _pilot(tmp_path)
    other = run.parent.parent / "another_case" / "run_1"
    _json(other / "run_metadata.json", {"classification": "completed", "problem_id": "another_case"})
    _json(other / "observability/delivery.json", {"valid": True})
    report.write_text(
        "| Case | Score |\n|---|---:|\n| [example_case](batch1/assistant_v3_campaign/scorecard.md) | 78 | details |\n",
        encoding="utf-8",
    )

    folders = build(report, root / "results/by-case", root)

    assert [folder.name for folder in folders] == ["example_case"]
