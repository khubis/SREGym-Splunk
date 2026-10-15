import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_main_campaign_abort import _configure_driver_test, _driver_conductor, _fake_run, _load_main_module

from sregym.conductor.conductor import StartProblemResult


def test_failed_gate_publishes_attempt_history_outside_agent_mount_without_starting_agent(monkeypatch, tmp_path):
    main = _load_main_module()
    conductor = _driver_conductor(start_result=StartProblemResult.SUCCESS)
    conductor.problem_id = "cronjob_sidecar_blocks_completion_hotel_reservation"
    conductor.problems.get_problem_ids.side_effect = lambda all=False: [conductor.problem_id]
    conductor.incident_started_at = datetime(2026, 9, 26, 19, tzinfo=UTC)
    conductor.finalize_attempt_status.return_value = "incomplete"
    run = _fake_run(tmp_path)
    launcher = _configure_driver_test(main, monkeypatch, tmp_path, conductor, run)
    published = tmp_path / "published"
    published.mkdir()
    run.finalize_and_publish.return_value = published
    diagnostics = {
        "attempt_count": 2,
        "dropped": 0,
        "observations": [
            {"attempt": 1, "status": "not_ready", "request_timeout_seconds": 10.0, "elapsed_ms": 100.0},
            {"attempt": 2, "status": "query_error", "request_timeout_seconds": 0.1, "elapsed_ms": 100.0},
        ],
    }
    error = main.CaseGateError("pre-agent evidence did not become ready", {"status": "query_error"})
    error.attempt_diagnostics = diagnostics
    monkeypatch.setattr(main, "_assistant_case_preflight", Mock(side_effect=error))
    monkeypatch.setattr(main.asyncio, "to_thread", AsyncMock(side_effect=lambda fn, *a: fn(*a)))
    monkeypatch.setattr(main, "_assistant_driver_config", Mock(return_value=object()))
    monkeypatch.setattr(main, "_assistant_prompt_context", Mock(return_value={}))
    monkeypatch.setattr(main, "write_pre_agent_failure", Mock())
    monkeypatch.setattr(main, "finalize_attempt_artifacts", Mock())
    monkeypatch.setattr(main.trace_postprocess, "write_trajectory", Mock(return_value=None))
    monkeypatch.setattr(main, "checkpoint_assistant_attempt", Mock())
    monkeypatch.setattr(main.AssistantV3Config, "from_env", Mock(return_value=object()))
    with pytest.raises(main.BenchmarkCampaignAborted):
        main.driver_loop(
            conductor, problem_selection=[conductor.problem_id], agent_to_run="assistant_v3",
            observability_provider=SimpleNamespace(name="splunk"), assistant_prompt_arm="symptom_guided",
        )
    launcher.ensure_started.assert_not_awaited()
    assert conductor.results["infrastructure_invalid"] is True
    assert not (run.active_dir / "splunk_lite_pre_agent.json").exists()
    proof = json.loads((published / "splunk_lite_pre_agent.json").read_text())
    assert proof["attempt_diagnostics"] == diagnostics
    assert proof["status"] == "query_error"
