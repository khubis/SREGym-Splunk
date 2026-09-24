import importlib.util
import json
import runpy
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from sregym.observability.base import AttemptContext, ProviderError, ReadinessReport, SignalReadiness
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
    started_at = datetime(2026, 9, 24, 12, tzinfo=UTC)
    context = AttemptContext(
        run_id=run.artifact_id,
        profile="svelte",
        comparable=False,
        attempt_started_at=started_at,
    )
    readiness = ReadinessReport(
        run_id=run.artifact_id,
        ready=True,
        signals=tuple(
            SignalReadiness(
                signal=signal,
                ready=True,
                checked_at=started_at + timedelta(seconds=offset),
                evidence={"count": 1},
            )
            for offset, signal in enumerate(("metrics", "traces", "logs", "kubernetes_events"), start=1)
        ),
    )
    conductor = SimpleNamespace(observability_readiness=readiness, _observability_context=context)
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
    assert payload["attempt_started_at"] == "2026-09-24T12:00:00Z"
    assert payload["telemetry_window_ended_at"] == "2026-09-24T12:00:04Z"
    assert "real-problem-id" not in json.dumps(payload)
    assert "hec-secret" not in json.dumps(payload)


def test_assistant_preflight_forwards_only_assistant_credentials(monkeypatch):
    benchmark_main = _load_main_module()
    expected = {
        "ASSISTANT_V3_URL": "https://assistant.example.test",
        "ASSISTANT_V3_AUTH_TOKEN": "assistant-token",
        "SF_TOKEN": "splunk-token",
    }
    for name, value in expected.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SPLUNK_HEC_TOKEN", "must-not-be-forwarded")
    runner = SimpleNamespace(
        has_prepared_agent_tools=True,
        run_sync=Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr="")),
    )

    benchmark_main.run_preflight_check("assistant_v3", container_runner=runner)

    request = runner.run_sync.call_args.args[0]
    assert request.env == expected
    assert request.label == "preflight"


def test_assistant_prompt_context_requires_an_application_and_uses_public_metadata():
    benchmark_main = _load_main_module()
    with pytest.raises(RuntimeError, match="metadata is unavailable"):
        benchmark_main._assistant_prompt_context(SimpleNamespace(app=None))

    context = benchmark_main._assistant_prompt_context(
        SimpleNamespace(
            app=SimpleNamespace(
                app_name="Astronomy Shop",
                description="Demo application",
                namespace="otel-demo",
                namespaces=[],
            )
        )
    )
    assert context == {
        "app_name": "Astronomy Shop",
        "app_description": "Demo application",
        "app_namespace": "otel-demo",
    }


def test_driver_requires_an_agent_without_external_harness(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main.asyncio, "sleep", AsyncMock())
    with pytest.raises(ValueError, match="agent_to_run is required"):
        benchmark_main.driver_loop(SimpleNamespace(), agent_to_run=None)


def _driver_conductor(*, start_result, readiness=None):
    conductor = MagicMock()
    conductor.problems.get_problem_ids.side_effect = lambda all=False: ["problem"]
    conductor.results = {}
    conductor.start_problem = AsyncMock(side_effect=start_result if isinstance(start_result, Exception) else None)
    if not isinstance(start_result, Exception):
        conductor.start_problem.return_value = start_result
    conductor.wait_for_submission_work = AsyncMock()
    conductor.close_submissions.return_value = False
    conductor.submission_stage = "done"
    conductor.stage_sequence = [{"name": "diagnosis"}]
    conductor.phases = None
    conductor.observability_readiness = readiness
    conductor.observability_delivery = None
    conductor.finalize_attempt_status.return_value = "complete"
    return conductor


def _fake_run(tmp_path):
    active_dir = tmp_path / "active"
    active_dir.mkdir()
    return SimpleNamespace(
        artifact_id="anon_0123456789abcdef0123456789abcdef",
        attempt=1,
        active_dir=active_dir,
        network_audit_path=active_dir / "network.json",
        save_network_audit=Mock(),
        finalize_and_publish=Mock(return_value=None),
    )


def _configure_driver_test(benchmark_main, monkeypatch, tmp_path, conductor, run):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(benchmark_main.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(benchmark_main, "get_profile", lambda: "full")
    monkeypatch.setattr(benchmark_main, "list_agents", lambda **kwargs: {"assistant_v3": {}})
    monkeypatch.setattr(benchmark_main.RunArtifacts, "create", Mock(return_value=run))
    launcher = MagicMock()
    launcher._procs = {}
    launcher.container_image = "agent-image"
    launcher.ensure_started = AsyncMock(return_value=None)
    launcher.internet_policy_result.return_value = {}
    monkeypatch.setattr(benchmark_main, "LAUNCHER", launcher)
    conductor.app = SimpleNamespace(
        app_name="Astronomy Shop", description="Demo application", namespace="otel-demo"
    )
    return launcher


def test_provider_readiness_failure_publishes_pre_agent_artifacts(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    error = ProviderError("readiness_timeout", "signals unavailable")
    conductor = _driver_conductor(start_result=error)
    run = _fake_run(tmp_path)
    launcher = _configure_driver_test(benchmark_main, monkeypatch, tmp_path, conductor, run)
    provider = SimpleNamespace(name="splunk")
    driver_config = object()
    monkeypatch.setattr(benchmark_main, "_assistant_driver_config", Mock(return_value=driver_config))
    monkeypatch.setattr(benchmark_main, "_assistant_prompt_context", Mock(return_value={}))
    write_failure = Mock()
    monkeypatch.setattr(benchmark_main, "write_pre_agent_failure", write_failure)
    monkeypatch.setattr(benchmark_main.AssistantV3Config, "from_env", Mock(return_value=object()))

    results = benchmark_main.driver_loop(
        conductor,
        problem_selection=["problem"],
        agent_to_run="assistant_v3",
        observability_provider=provider,
    )

    assert conductor.start_problem.await_count == 1
    assert conductor.observability_readiness is None
    assert conductor.results["infrastructure_invalid"] is True
    assert conductor.results["included_in_diagnosis_pass_rate"] is False
    write_failure.assert_called_once()
    run.finalize_and_publish.assert_called_once()
    launcher.ensure_started.assert_not_awaited()
    assert results[0]["assistant_v3"][0]["deploy_failed"] is True


def test_successful_assistant_attempt_configures_agent_and_finalizes_artifacts(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    conductor = _driver_conductor(start_result=benchmark_main.StartProblemResult.SUCCESS)
    run = _fake_run(tmp_path)
    launcher = _configure_driver_test(benchmark_main, monkeypatch, tmp_path, conductor, run)
    registration = SimpleNamespace(agent_version="v3", kickoff_env={"EXISTING": "value"})
    monkeypatch.setattr(benchmark_main, "get_agent", Mock(return_value=registration))
    write_config = Mock()
    write_failure = Mock()
    finalize = Mock()
    monkeypatch.setattr(benchmark_main, "_write_assistant_driver_config", write_config)
    monkeypatch.setattr(benchmark_main, "_assistant_runtime_environment", lambda: {"ASSISTANT": "configured"})
    monkeypatch.setattr(benchmark_main, "_assistant_driver_config", Mock(return_value=object()))
    monkeypatch.setattr(benchmark_main, "_assistant_prompt_context", Mock(return_value={}))
    monkeypatch.setattr(benchmark_main, "write_pre_agent_failure", write_failure)
    monkeypatch.setattr(benchmark_main, "finalize_attempt_artifacts", finalize)
    monkeypatch.setattr(benchmark_main.AssistantV3Config, "from_env", Mock(return_value=object()))
    provider = SimpleNamespace(name="splunk")

    results = benchmark_main.driver_loop(
        conductor,
        problem_selection=["problem"],
        agent_to_run="assistant_v3",
        observability_provider=provider,
    )

    write_config.assert_called_once_with(run, conductor, "v3", provider, "api")
    assert registration.kickoff_env == {"EXISTING": "value", "ASSISTANT": "configured"}
    launcher.ensure_started.assert_awaited_once_with(registration)
    write_failure.assert_called_once()
    finalize.assert_called_once()
    row = results[0]["assistant_v3"][0]
    assert row["observability_provider"] == "splunk"
    assert row["comparable"] is True


def test_benchmark_closes_provider_after_suite_api_shutdown(monkeypatch):
    benchmark_main = _load_main_module()
    provider = MagicMock(name="provider", name_for_error="splunk")
    provider.name = "none"
    monkeypatch.setattr(benchmark_main, "create_provider", Mock(return_value=provider))
    monkeypatch.setattr(benchmark_main, "validate_assistant_campaign", Mock())
    monkeypatch.setattr(benchmark_main, "_configure_model_environment", Mock(return_value=("model", "judge")))
    monkeypatch.setattr(benchmark_main, "set_profile", Mock())
    monkeypatch.setattr(benchmark_main, "Conductor", Mock(return_value=MagicMock()))
    monkeypatch.setattr(benchmark_main, "run_api", Mock())
    thread = MagicMock()
    monkeypatch.setattr(benchmark_main.threading, "Thread", Mock(return_value=thread))
    launcher = MagicMock()
    monkeypatch.setattr(benchmark_main, "LAUNCHER", launcher)
    args = SimpleNamespace(
        agent="stratus",
        model="model",
        judge_model="judge",
        reasoning_effort=None,
        jev_model=None,
        observability_provider="none",
        internet_access="open",
        allow_agent_endpoint=[],
        container_hardening="on",
        profile="full",
        noise=False,
        use_external_harness=True,
        stages=["diagnosis"],
        baseline=None,
        force_build=False,
        suite="sregym-lite",
        problem=None,
        n_attempts=1,
        agent_timeout=10,
        resume=None,
    )

    assert benchmark_main._run_benchmark(args) == []
    provider.preflight.assert_called_once()
    provider.close.assert_called_once()
    thread.start.assert_called_once()
    thread.join.assert_called_once_with(timeout=5)


def test_cli_help_builds_the_observability_provider_option(monkeypatch, capsys):
    main_path = Path(__file__).resolve().parents[1] / "main.py"
    monkeypatch.setattr(sys, "argv", [str(main_path), "--help"])
    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(main_path), run_name="__main__")
    assert raised.value.code == 0
    assert "--observability-provider {none,splunk}" in capsys.readouterr().out


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
