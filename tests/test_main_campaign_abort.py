import importlib.util
import json
import sys
import tempfile

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from sregym.observability.base import AttemptContext
from sregym.run_artifacts import RunArtifacts


def _load_main_module():
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    spec = importlib.util.spec_from_file_location("sregym_benchmark_main_for_test", main_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_driver_wrapper_preserves_partial_results_and_failure(monkeypatch):
    benchmark_main = _load_main_module()
    partial_results = [
        {
            "codex": [
                {
                    "problem_id": "problem",
                    "attempt": 1,
                    "run_status": "incomplete",
                    "incomplete_reason": "cleanup_timeout_after_agent_exit",
                }
            ]
        }
    ]

    def abort_driver(*_args, **_kwargs):
        raise benchmark_main.BenchmarkCampaignAborted("cleanup timed out", partial_results)

    shutdown_called = []
    monkeypatch.setattr(benchmark_main, "driver_loop", abort_driver)
    monkeypatch.setattr(benchmark_main.LAUNCHER, "cleanup_all", lambda: None)
    monkeypatch.setattr(benchmark_main, "request_shutdown", lambda: shutdown_called.append(True))

    benchmark_main._run_driver_and_shutdown(object())

    assert benchmark_main._driver_results == partial_results
    assert isinstance(benchmark_main._driver_error, benchmark_main.BenchmarkCampaignAborted)
    assert shutdown_called == [True]


def test_result_csv_publication_with_results_on_another_filesystem(tmp_path, monkeypatch):
    benchmark_main = _load_main_module()
    shared_memory = Path("/dev/shm")
    if not shared_memory.is_dir() or shared_memory.stat().st_dev == tmp_path.stat().st_dev:
        pytest.skip("requires a second writable filesystem")
    monkeypatch.chdir(tmp_path)
    with tempfile.TemporaryDirectory(dir=shared_memory) as directory:
        partial, final = benchmark_main._problem_result_paths(Path(directory), "autosubmit", "problem")
        partial.write_text("run_status,Mitigation.success\ncomplete,False\n")
        benchmark_main.os.replace(partial, final)
        assert final.read_text() == "run_status,Mitigation.success\ncomplete,False\n"
        assert not partial.exists()


@pytest.mark.parametrize(
    ("stages", "external", "expected_calls"),
    [(None, False, 1), (["diagnosis"], False, 1), (["mitigation"], False, 0), (None, True, 0)],
)
def test_judge_preflight_only_when_diagnosis_can_run(monkeypatch, stages, external, expected_calls):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main.os, "environ", benchmark_main.os.environ.copy())
    calls = []
    monkeypatch.setattr(benchmark_main, "init_logger", lambda: None)
    monkeypatch.setattr(benchmark_main, "_configure_model_environment", lambda args: ("unused", "unused"))
    monkeypatch.setattr(benchmark_main, "set_profile", lambda profile: None)
    monkeypatch.setattr(benchmark_main, "run_judge_preflight_check", lambda: calls.append(True))

    class ReachedConductor(Exception):
        pass

    def stop_before_deployment(**kwargs):
        raise ReachedConductor

    monkeypatch.setattr(benchmark_main, "ConductorConfig", stop_before_deployment)
    args = SimpleNamespace(
        agent=None,
        internet_access="open",
        container_hardening="on",
        profile="full",
        noise=False,
        use_external_harness=external,
        stages=stages,
        baseline=None,
        propagation=None,
    )
    with pytest.raises(ReachedConductor):
        benchmark_main._run_benchmark(args)
    assert len(calls) == expected_calls

def _assistant_args(**changes):
    values = {
        "agent": "assistant_v3",
        "stages": ["diagnosis"],
        "suite": "sregym-lite",
        "problem": None,
        "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
        "judge_model": "fixed-judge",
        "judge_backend": "api",
        "observability_provider": "splunk",
        "profile": "full",
        "use_external_harness": False,
    }
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"stages": None}, "diagnosis-only"),
        ({"stages": ["diagnosis", "mitigation"]}, "diagnosis-only"),
        ({"suite": None, "problem": "not-lite"}, "SREGym-Lite"),
        ({"model": ""}, "model"),
        ({"reasoning_effort": None}, "reasoning"),
        ({"reasoning_effort": "minimal"}, "reasoning"),
        ({"judge_model": None}, "judge model"),
        ({"judge_backend": ""}, "judge backend"),
        ({"observability_provider": "none"}, "Splunk"),
        ({"use_external_harness": True}, "external harness"),
    ],
)
def test_assistant_campaign_requires_explicit_comparable_configuration(changes, message):
    benchmark_main = _load_main_module()
    with pytest.raises(ValueError, match=message):
        benchmark_main.validate_assistant_campaign(_assistant_args(**changes))


def test_assistant_campaign_accepts_lite_suite_and_each_registered_lite_case():
    benchmark_main = _load_main_module()
    benchmark_main.validate_assistant_campaign(_assistant_args())
    for problem in benchmark_main.PROBLEM_SETS["sregym-lite"]:
        benchmark_main.validate_assistant_campaign(_assistant_args(suite=None, problem=problem))


def test_assistant_runtime_environment_is_minimal_and_excludes_provider_and_judge_secrets(monkeypatch):
    benchmark_main = _load_main_module()
    values = {
        "ASSISTANT_V3_URL": "https://assistant.example.test",
        "ASSISTANT_V3_AUTH_TOKEN": "assistant-secret",
        "SF_TOKEN": "sf-secret",
        "AGENT_MODEL_ID": "gpt-5.6-luna",
        "AGENT_REASONING_EFFORT": "medium",
        "API_PORT": "8000",
        "SPLUNK_HEC_TOKEN": "hec-secret",
        "JUDGE_API_KEY": "judge-secret",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    environment = benchmark_main._assistant_runtime_environment()

    assert set(environment) == {
        "ASSISTANT_V3_URL",
        "ASSISTANT_V3_AUTH_TOKEN",
        "SF_TOKEN",
        "AGENT_MODEL_ID",
        "AGENT_REASONING_EFFORT",
        "API_PORT",
        "SREGYM_ASSISTANT_DRIVER_CONFIG",
    }
    assert "hec-secret" not in environment.values()
    assert "judge-secret" not in environment.values()


def test_driver_config_records_opaque_identity_and_comparability_without_secrets(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "get_profile", lambda: "svelte")
    monkeypatch.setenv("JUDGE_MODEL_ID", "fixed-judge")
    run = RunArtifacts.create(
        staging_root=tmp_path / ".runtime",
        results_root=tmp_path / "results",
        problem_id="real-problem-id",
        agent="assistant_v3",
        attempt=2,
    )
    context = AttemptContext(
        run_id=run.artifact_id,
        profile="svelte",
        comparable=False,
        attempt_started_at=datetime.now(UTC),
    )
    conductor = SimpleNamespace(observability_readiness=None, _observability_context=context)
    provider = SimpleNamespace(
        name="splunk",
        configuration=SimpleNamespace(hec_index="main", hec_token="hec-secret"),
        _connection_id="logs-connection",
    )

    benchmark_main._write_assistant_driver_config(run, conductor, "v3", provider, "api")

    payload = json.loads((run.active_dir / "assistant_v3_driver_config.json").read_text())
    assert payload["run_id"] == run.artifact_id
    assert payload["attempt"] == 2
    assert payload["benchmark_profile"] == "svelte"
    assert payload["comparable"] is False
    assert payload["judge_model"] == "fixed-judge"
    assert "real-problem-id" not in json.dumps(payload)
    assert "hec-secret" not in json.dumps(payload)


@pytest.mark.parametrize("platform_failure, expected_attempts", [(True, 1), (False, 3)])
@pytest.mark.parametrize("cleanup_budget", [None, 600])
def test_deployment_retries_only_transient_failures(
    monkeypatch, tmp_path, platform_failure, expected_attempts, cleanup_budget
):
    benchmark_main = _load_main_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(benchmark_main, "get_profile", lambda: "full")
    error_type = benchmark_main.ContainerPlatformError if platform_failure else RuntimeError
    conductor = SimpleNamespace(
        problems=Mock(get_problem_ids=Mock(return_value=["problem"])),
        results={},
        bind_phase_ledger=Mock(),
        clear_cluster_egress_boundary=Mock(),
        start_problem=AsyncMock(side_effect=error_type("image could not start")),
        finish_problem_in_background=Mock(),
        wait_for_submission_work=AsyncMock(),
    )
    if cleanup_budget is not None:
        conductor.problem = SimpleNamespace(cleanup_timeout_seconds=cleanup_budget)

    results = benchmark_main.driver_loop(conductor, use_external_harness=True)

    assert conductor.start_problem.await_count == expected_attempts
    assert conductor.finish_problem_in_background.call_count == expected_attempts
    assert conductor.wait_for_submission_work.await_count == expected_attempts
    assert all(
        call.kwargs["timeout"] == (cleanup_budget or benchmark_main.CLEANUP_DRAIN_TIMEOUT_SECONDS)
        for call in conductor.wait_for_submission_work.await_args_list
    )
    conductor.bind_phase_ledger.assert_called_once()
    assert results == [
        {None: [{"problem_id": "problem", "attempt": 1, "deployment_profile": "full", "deploy_failed": True}]}
    ]
