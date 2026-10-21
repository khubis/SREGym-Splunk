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

from sregym.conductor.constants import StartProblemResult
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
        force_build=False,
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
        "ASSISTANT_V3_CAPABILITY_PROFILE": "splunk_o11y_read_only_interplex_pilot",
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
        "ASSISTANT_V3_CAPABILITY_PROFILE",
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
    conductor = SimpleNamespace(
        observability_readiness=readiness,
        _observability_context=context,
        incident_started_at=started_at + timedelta(seconds=2),
        incident_ended_at=started_at + timedelta(minutes=3),
    )
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
    assert payload["attempt_started_at"] == "2026-09-24T12:00:02Z"
    assert payload["telemetry_window_ended_at"] == "2026-09-24T12:03:00Z"
    assert "real-problem-id" not in json.dumps(payload)
    assert "hec-secret" not in json.dumps(payload)


def test_symptom_guided_config_uses_case_recipe_and_is_not_prompt_comparable(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    monkeypatch.setattr(benchmark_main, "get_profile", lambda: "full")
    monkeypatch.setenv("JUDGE_MODEL_ID", "fixed-judge")
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef", attempt=1, active_dir=tmp_path)
    conductor = SimpleNamespace(
        problem_id="cronjob_sidecar_blocks_completion_hotel_reservation",
        observability_readiness=None,
        _observability_context=SimpleNamespace(attempt_started_at=datetime(2026, 9, 26, tzinfo=UTC)),
        incident_started_at=datetime(2026, 9, 26, 1, tzinfo=UTC),
    )
    provider = SimpleNamespace(name="splunk", configuration=SimpleNamespace(hec_index="main"))

    config = benchmark_main._assistant_driver_config(
        run,
        conductor,
        "v3",
        provider,
        "api",
        prompt_arm="symptom_guided",
    )

    assert config.symptom == ("A scheduled background task in Hotel Reservation is taking unusually long to finish.")
    assert config.attempt_started_at == conductor.incident_started_at
    assert config.comparable is False


def test_case_preflight_uses_post_baseline_window_and_exact_synthetic_connection(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef", active_dir=tmp_path)
    conductor = SimpleNamespace(
        problem_id="cronjob_sidecar_blocks_completion_hotel_reservation",
        incident_started_at=start,
        app=SimpleNamespace(
            app_name="Hotel Reservation", namespace="hotel-reservation", namespaces=["hotel-reservation"]
        ),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs-connection")
    observed = {}

    class Backend:
        def __init__(self, config):
            assert config is provider.configuration

        def close(self):
            observed["closed"] = True

    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", Backend)
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "read_source_pods", lambda *a, **k: [])

    def wait(backend, **kwargs):
        observed.update(kwargs)
        return {
            "status": "ready",
            "window": {
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
            },
        }

    monkeypatch.setattr(benchmark_main, "wait_for_cronjob_pre_agent", wait)
    result = benchmark_main._assistant_case_preflight(run, conductor, provider)
    assert result["status"] == "ready"
    assert observed["context"].run_id == run.artifact_id
    assert observed["context"].attempt_started_at == start
    assert observed["connection_id"] == "synthetic-logs-connection"
    assert observed["scope"].namespaces == ("hotel-reservation",)
    assert observed["closed"] is True
    assert list(tmp_path.iterdir()) == []


def test_edge_case_preflight_uses_bounded_source_logs_not_cronjob_pods(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="edge_request_filter_cpu_saturation",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Astronomy Shop", namespace="astronomy-shop"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = {}
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main,
        "read_source_logs",
        lambda *a, **k: observed.update(
            namespace=a[0],
            workload=a[1],
            start=k["start"],
            context=k["context"],
        )
        or ["source-event"],
    )

    def edge_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == ["source-event"]
        return {"status": "ready"}

    monkeypatch.setattr(benchmark_main, "wait_for_edge_pre_agent", edge_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider) == {"status": "ready"}
    assert observed == {
        "namespace": "astronomy-shop",
        "workload": "frontend-proxy",
        "start": start,
        "context": "kind-kind",
    }


def test_network_policy_preflight_reads_policy_only_for_runner_side_proof(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="network_policy_block",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Hotel Reservation", namespace="hotel-reservation"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = {}
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main,
        "read_source_object",
        lambda *a, **k: observed.update(
            kind=a[0],
            name=a[1],
            namespace=k["namespace"],
            context=k["context"],
        )
        or {"kind": "NetworkPolicy"},
    )

    def network_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == {"kind": "NetworkPolicy"}
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_network_policy_pre_agent", network_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == {
        "kind": "networkpolicy",
        "name": "deny-all-recommendation",
        "namespace": "hotel-reservation",
        "context": "kind-kind",
    }


def test_kafka_preflight_reads_validator_logs_from_source_only_for_runner_side_proof(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="kafka_poison_pill_hol_block",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Astronomy Shop", namespace="astronomy-shop"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = {}
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main,
        "read_source_logs",
        lambda *a, **k: observed.update(
            namespace=a[0],
            deployment=a[1],
            context=k["context"],
            start=k["start"],
        )
        or ["source validator log"],
    )

    def kafka_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == ["source validator log"]
        return {"status": "ready"}

    monkeypatch.setattr(benchmark_main, "wait_for_kafka_pre_agent", kafka_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready"
    assert observed == {
        "namespace": "astronomy-shop",
        "deployment": "orders-validator",
        "context": "kind-kind",
        "start": start,
    }


def test_mutating_webhook_preflight_reads_social_network_pods_for_runner_side_proof(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="mutating_webhook_resource_limits_social_network",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Social Network", namespace="social-network"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = {}
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main,
        "read_source_pods",
        lambda *a, **k: observed.update(
            namespace=a[0],
            context=k["context"],
        )
        or [{"kind": "Pod"}],
    )

    def webhook_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == [{"kind": "Pod"}]
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_mutating_webhook_pre_agent", webhook_wait, raising=False)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == {"namespace": "social-network", "context": "kind-kind"}


def test_finalizer_deadlock_preflight_reads_controller_logs_for_runner_side_proof(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="finalizer_deadlock_controller_hotel_reservation",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Hotel Reservation", namespace="hotel-reservation"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = {}
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main,
        "read_source_logs",
        lambda *a, **k: observed.update(
            namespace=a[0],
            deployment=a[1],
            context=k["context"],
            start=k["start"],
        )
        or ["source denial log"],
    )

    def finalizer_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == ["source denial log"]
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_finalizer_deadlock_pre_agent", finalizer_wait, raising=False)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == {
        "namespace": "hotel-reservation",
        "deployment": "cleanup-controller",
        "context": "kind-kind",
        "start": start,
    }


def test_readiness_preflight_reads_social_network_pods_for_runner_side_proof(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="readiness_probe_misconfiguration_social_network",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Social Network", namespace="social-network"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = {}
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main,
        "read_source_pods",
        lambda *a, **k: observed.update(
            namespace=a[0],
            context=k["context"],
        )
        or [{"kind": "Pod"}],
    )

    def readiness_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == [{"kind": "Pod"}]
        return {"status": "ready"}

    monkeypatch.setattr(benchmark_main, "wait_for_readiness_pre_agent", readiness_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready"
    assert observed == {"namespace": "social-network", "context": "kind-kind"}


def test_env_shadowing_preflight_reads_astronomy_pods_for_runner_side_proof(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="env_variable_shadowing_astronomy_shop",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Astronomy Shop", namespace="astronomy-shop"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = {}
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main,
        "read_source_pods",
        lambda *a, **k: observed.update(
            namespace=a[0],
            context=k["context"],
        )
        or [{"kind": "Pod"}],
    )

    def shadowing_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == [{"kind": "Pod"}]
        return {"status": "ready"}

    monkeypatch.setattr(benchmark_main, "wait_for_env_shadowing_pre_agent", shadowing_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready"
    assert observed == {"namespace": "astronomy-shop", "context": "kind-kind"}


def test_internal_traffic_preflight_reads_source_service_and_pods(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="internal_traffic_policy_local_astronomy_shop",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Astronomy Shop", namespace="astronomy-shop"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = []
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main, "read_source_object", lambda *a, **k: observed.append((a, k)) or {"kind": "Service"}
    )
    monkeypatch.setattr(
        benchmark_main, "read_source_pods", lambda *a, **k: observed.append((a, k)) or [{"kind": "Pod"}]
    )

    def traffic_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == ({"kind": "Service"}, [{"kind": "Pod"}])
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_internal_traffic_policy_pre_agent", traffic_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == [
        (("service", "recommendation"), {"namespace": "astronomy-shop", "context": "kind-kind"}),
        (("astronomy-shop",), {"context": "kind-kind"}),
    ]


def test_service_dns_preflight_reads_only_source_coredns_configmap(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="service_dns_resolution_failure_social_network",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Social Network", namespace="social-network"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = []
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main, "read_source_object", lambda *a, **k: observed.append((a, k)) or {"kind": "ConfigMap"}
    )

    def dns_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == {"kind": "ConfigMap"}
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_service_dns_pre_agent", dns_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == [
        (("configmap", "coredns"), {"namespace": "kube-system", "context": "kind-kind"}),
    ]


def test_wrong_pod_selection_preflight_reads_source_frontend_service_and_pods(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="service_wrong_pod_selection_hotel_reservation",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Hotel Reservation", namespace="hotel-reservation"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = []
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main, "read_source_object", lambda *a, **k: observed.append((a, k)) or {"kind": "Service"}
    )
    monkeypatch.setattr(
        benchmark_main, "read_source_pods", lambda *a, **k: observed.append((a, k)) or [{"kind": "Pod"}]
    )

    def selection_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == ({"kind": "Service"}, [{"kind": "Pod"}])
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_service_wrong_pod_selection_pre_agent", selection_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == [
        (("service", "frontend"), {"namespace": "hotel-reservation", "context": "kind-kind"}),
        (("hotel-reservation",), {"context": "kind-kind"}),
    ]


def test_namespace_memory_preflight_reads_quota_and_events(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="namespace_memory_limit",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Hotel Reservation", namespace="hotel-reservation"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = []
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main, "read_source_object", lambda *a, **k: observed.append((a, k)) or {"kind": "ResourceQuota"}
    )
    monkeypatch.setattr(
        benchmark_main, "read_source_events", lambda *a, **k: observed.append((a, k)) or [{"kind": "Event"}]
    )

    def quota_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == ({"kind": "ResourceQuota"}, [{"kind": "Event"}])
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_namespace_memory_quota_pre_agent", quota_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == [
        (("resourcequota", "memory-limit-quota"), {"namespace": "hotel-reservation", "context": "kind-kind"}),
        (("hotel-reservation",), {"context": "kind-kind"}),
    ]


def test_valkey_auth_preflight_reads_astronomy_cart_logs(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="valkey_auth_disruption",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Astronomy Shop", namespace="astronomy-shop"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = []
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(benchmark_main, "read_source_logs", lambda *a, **k: observed.append((a, k)) or ["auth error"])

    def auth_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == ["auth error"]
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_valkey_auth_pre_agent", auth_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == [
        (("astronomy-shop", "cart"), {"context": "kind-kind", "start": start}),
    ]


def test_secret_rotation_preflight_reads_deployment_and_pods_without_secret(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="secret_rotation_stale_env_credentials_astronomy_shop",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Astronomy Shop", namespace="astronomy-shop"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = []
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main, "read_source_object", lambda *a, **k: observed.append((a, k)) or {"kind": "Deployment"}
    )
    monkeypatch.setattr(
        benchmark_main, "read_source_pods", lambda *a, **k: observed.append((a, k)) or [{"kind": "Pod"}]
    )

    def rotation_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == ({"kind": "Deployment"}, [{"kind": "Pod"}])
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_secret_rotation_pre_agent", rotation_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == [
        (("deployment", "product-catalog"), {"namespace": "astronomy-shop", "context": "kind-kind"}),
        (("astronomy-shop",), {"context": "kind-kind"}),
    ]


def test_unschedulable_checkout_preflight_reads_astronomy_pods(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="unschedulable_incorrect_port_assignment",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Astronomy Shop", namespace="astronomy-shop"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = []
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main, "read_source_pods", lambda *a, **k: observed.append((a, k)) or [{"kind": "Pod"}]
    )

    def checkout_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == [{"kind": "Pod"}]
        return {"status": "ready"}

    monkeypatch.setattr(benchmark_main, "wait_for_unschedulable_checkout_pre_agent", checkout_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready"
    assert observed == [(("astronomy-shop",), {"context": "kind-kind"})]


def test_duplicate_pvc_preflight_reads_jaeger_pvc_and_pods(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="duplicate_pvc_mounts_social_network",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Social Network", namespace="social-network"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = []
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main,
        "read_source_object",
        lambda *a, **k: observed.append((a, k)) or {"kind": "PersistentVolumeClaim"},
    )
    monkeypatch.setattr(
        benchmark_main, "read_source_pods", lambda *a, **k: observed.append((a, k)) or [{"kind": "Pod"}]
    )

    def pvc_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == ({"kind": "PersistentVolumeClaim"}, [{"kind": "Pod"}])
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_duplicate_pvc_mounts_pre_agent", pvc_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == [
        (("persistentvolumeclaim", "jaeger-pvc"), {"namespace": "social-network", "context": "kind-kind"}),
        (("social-network",), {"context": "kind-kind"}),
    ]


def test_admission_webhook_preflight_reads_cluster_webhook_and_namespace_events(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="admission_webhook_outage_hotel_reservation",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Hotel Reservation", namespace="hotel-reservation"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = []
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main,
        "read_source_cluster_object",
        lambda *a, **k: observed.append((a, k)) or {"kind": "ValidatingWebhookConfiguration"},
    )
    monkeypatch.setattr(
        benchmark_main, "read_source_events", lambda *a, **k: observed.append((a, k)) or [{"kind": "Event"}]
    )

    def webhook_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == ({"kind": "ValidatingWebhookConfiguration"}, [{"kind": "Event"}])
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_admission_webhook_pre_agent", webhook_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == [
        (("validatingwebhookconfiguration", "pod-policy.validation.k8s.io"), {"context": "kind-kind"}),
        (("hotel-reservation",), {"context": "kind-kind"}),
    ]


def test_wrong_dns_policy_preflight_reads_astronomy_pods_for_runner_side_proof(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="wrong_dns_policy_astronomy_shop",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Astronomy Shop", namespace="astronomy-shop"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = {}
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main,
        "read_source_pods",
        lambda *a, **k: observed.update(
            namespace=a[0],
            context=k["context"],
        )
        or [{"kind": "Pod"}],
    )

    def dns_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == [{"kind": "Pod"}]
        return {"status": "ready"}

    monkeypatch.setattr(benchmark_main, "wait_for_wrong_dns_policy_pre_agent", dns_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready"
    assert observed == {"namespace": "astronomy-shop", "context": "kind-kind"}


def test_wrong_service_selector_preflight_reads_exact_service_and_endpoints(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="wrong_service_selector_social_network",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Social Network", namespace="social-network"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = []
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(benchmark_main, "read_source_object", lambda *a, **k: observed.append((a, k)) or {"kind": a[0]})

    def selector_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == ({"kind": "service"}, {"kind": "endpoints"})
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_wrong_service_selector_pre_agent", selector_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == [
        (("service", "user-service"), {"namespace": "social-network", "context": "kind-kind"}),
        (("endpoints", "user-service"), {"namespace": "social-network", "context": "kind-kind"}),
    ]


def test_rolling_update_preflight_reads_source_strategy_and_pods(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="rolling_update_misconfigured_social_network",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Social Network", namespace="social-network"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    observed = []
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        benchmark_main, "read_source_object", lambda *a, **k: observed.append((a, k)) or {"kind": "Deployment"}
    )
    monkeypatch.setattr(
        benchmark_main, "read_source_pods", lambda *a, **k: observed.append((a, k)) or [{"kind": "Pod"}]
    )

    def rolling_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == ({"kind": "Deployment"}, [{"kind": "Pod"}])
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_rolling_update_pre_agent", rolling_wait)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"
    assert observed == [
        (("deployment", "custom-service"), {"namespace": "social-network", "context": "kind-kind"}),
        (("social-network",), {"context": "kind-kind"}),
    ]


def test_search_retry_preflight_reads_live_source_metrics_without_exposing_oracle(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    source = {"rate_queue_depth": 42, "search_requests_total": 900, "search_rate_attempts_total": 1600}
    conductor = SimpleNamespace(
        problem_id="search_rate_retry_collapse_hotel_reservation",
        incident_started_at=start,
        app=SimpleNamespace(app_name="Hotel Reservation", namespace="hotel-reservation"),
        problem=SimpleNamespace(workload=SimpleNamespace(metrics=SimpleNamespace(snapshot=lambda: source))),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs")
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: None))

    def search_wait(backend, **kwargs):
        assert kwargs["source_reader"]() == source
        return {"status": "ready_data_limited"}

    monkeypatch.setattr(benchmark_main, "wait_for_search_retry_pre_agent", search_wait, raising=False)
    assert benchmark_main._assistant_case_preflight(run, conductor, provider)["status"] == "ready_data_limited"


def test_case_preflight_fails_closed_for_unreviewed_case_or_non_kind_context(monkeypatch):
    benchmark_main = _load_main_module()
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(problem_id="unreviewed_lite_case")
    with pytest.raises(benchmark_main.CaseGateError, match="no reviewed"):
        benchmark_main._assistant_case_preflight(run, conductor, object())

    conductor = SimpleNamespace(
        problem_id="cronjob_sidecar_blocks_completion_hotel_reservation",
        incident_started_at=datetime(2026, 9, 26, 19, tzinfo=UTC),
        app=SimpleNamespace(namespace="hotel-reservation", app_name="Hotel Reservation"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs-connection")
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="prod\n"))
    with pytest.raises(benchmark_main.CaseGateError, match="Kind"):
        benchmark_main._assistant_case_preflight(run, conductor, provider)


def test_case_preflight_redacts_unexpected_backend_errors_and_closes_backend(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = SimpleNamespace(artifact_id="anon_0123456789abcdef0123456789abcdef")
    conductor = SimpleNamespace(
        problem_id="cronjob_sidecar_blocks_completion_hotel_reservation",
        incident_started_at=start,
        app=SimpleNamespace(namespace="hotel-reservation", app_name="Hotel Reservation"),
    )
    provider = SimpleNamespace(configuration=object(), _connection_id="synthetic-logs-connection")
    closed = []
    monkeypatch.setattr(benchmark_main.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="kind-kind\n"))
    monkeypatch.setattr(
        benchmark_main, "SplunkHttpBackend", lambda _: SimpleNamespace(close=lambda: closed.append(True))
    )
    monkeypatch.setattr(
        benchmark_main,
        "wait_for_cronjob_pre_agent",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("credential-like-private-error")),
    )
    with pytest.raises(benchmark_main.CaseGateError, match="verifier failed") as raised:
        benchmark_main._assistant_case_preflight(run, conductor, provider)
    assert "credential-like-private-error" not in str(raised.value)
    assert closed == [True]


def test_assistant_preflight_forwards_only_assistant_credentials(monkeypatch):
    benchmark_main = _load_main_module()
    expected = {
        "ASSISTANT_V3_CAPABILITY_PROFILE": "splunk_o11y_read_only_interplex_pilot",
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


def test_assistant_inspection_preview_uses_exact_runtime_window(monkeypatch):
    benchmark_main = _load_main_module()
    start = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
    end = datetime(2026, 9, 26, 18, 5, tzinfo=UTC)
    monkeypatch.setattr(
        benchmark_main,
        "_assistant_driver_config",
        Mock(return_value=SimpleNamespace(attempt_started_at=start, telemetry_window_ended_at=end, symptom=None)),
    )
    monkeypatch.setattr(
        benchmark_main,
        "_assistant_prompt_context",
        Mock(
            return_value={
                "app_name": "Hotel Reservation",
                "app_description": "Demo",
                "app_namespace": "hotel-reservation",
            }
        ),
    )

    prompt, instruction = benchmark_main._assistant_inspection_preview(object(), object(), object(), "api")

    assert "Hotel Reservation" in prompt
    assert "Telemetry time window: 2026-09-26T18:00:00Z through 2026-09-26T18:05:00Z" in instruction
    assert "run ID" not in instruction


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
    conductor.app = SimpleNamespace(app_name="Astronomy Shop", description="Demo application", namespace="otel-demo")
    return launcher


def test_provider_readiness_failure_publishes_pre_agent_artifacts(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    error = ProviderError("readiness_timeout", "signals unavailable")
    conductor = _driver_conductor(start_result=error)
    run = _fake_run(tmp_path)
    published_run = tmp_path / "results" / "batch" / "assistant_v3" / "problem" / "run_1"
    published_run.mkdir(parents=True)
    run.finalize_and_publish.return_value = published_run
    launcher = _configure_driver_test(benchmark_main, monkeypatch, tmp_path, conductor, run)
    provider = SimpleNamespace(name="splunk")
    driver_config = object()
    monkeypatch.setattr(benchmark_main, "_assistant_driver_config", Mock(return_value=driver_config))
    monkeypatch.setattr(benchmark_main, "_assistant_prompt_context", Mock(return_value={}))
    write_failure = Mock()
    checkpoint = Mock()
    monkeypatch.setattr(benchmark_main, "write_pre_agent_failure", write_failure)
    monkeypatch.setattr(benchmark_main, "checkpoint_assistant_attempt", checkpoint)
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
    checkpoint.assert_called_once()
    assert checkpoint.call_args.args[1] == published_run
    launcher.ensure_started.assert_not_awaited()
    assert results[0]["assistant_v3"][0]["deploy_failed"] is True


def test_successful_assistant_attempt_configures_agent_and_finalizes_artifacts(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    conductor = _driver_conductor(start_result=StartProblemResult.SUCCESS)
    run = _fake_run(tmp_path)
    launcher = _configure_driver_test(benchmark_main, monkeypatch, tmp_path, conductor, run)
    registration = SimpleNamespace(agent_version="v3", kickoff_env={"EXISTING": "value"})
    monkeypatch.setattr(benchmark_main, "get_agent", Mock(return_value=registration))
    write_config = Mock()
    write_failure = Mock()
    finalize = Mock()
    published_run = tmp_path / "results" / "batch" / "assistant_v3" / "problem" / "run_1"
    published_run.mkdir(parents=True)
    run.finalize_and_publish.return_value = published_run
    checkpoint = Mock()
    monkeypatch.setattr(benchmark_main, "_write_assistant_driver_config", write_config)
    monkeypatch.setattr(benchmark_main, "_assistant_runtime_environment", lambda: {"ASSISTANT": "configured"})
    monkeypatch.setattr(benchmark_main, "_assistant_driver_config", Mock(return_value=object()))
    monkeypatch.setattr(benchmark_main, "_assistant_prompt_context", Mock(return_value={}))
    monkeypatch.setattr(benchmark_main, "write_pre_agent_failure", write_failure)
    monkeypatch.setattr(benchmark_main, "finalize_attempt_artifacts", finalize)
    monkeypatch.setattr(benchmark_main.trace_postprocess, "write_trajectory", Mock(return_value=None))
    monkeypatch.setattr(benchmark_main, "checkpoint_assistant_attempt", checkpoint, raising=False)
    monkeypatch.setattr(benchmark_main.AssistantV3Config, "from_env", Mock(return_value=object()))
    provider = SimpleNamespace(name="splunk")

    results = benchmark_main.driver_loop(
        conductor,
        problem_selection=["problem"],
        agent_to_run="assistant_v3",
        observability_provider=provider,
    )

    write_config.assert_called_once_with(run, conductor, "v3", provider, "api", prompt_arm="time_only")
    assert registration.kickoff_env == {"EXISTING": "value", "ASSISTANT": "configured"}
    launcher.ensure_started.assert_awaited_once_with(registration)
    write_failure.assert_called_once()
    finalize.assert_called_once()
    row = results[0]["assistant_v3"][0]
    assert row["observability_provider"] == "splunk"
    assert row["comparable"] is True
    checkpoint.assert_called_once()
    assert checkpoint.call_args.args[1] == published_run


def test_guided_case_gate_freezes_window_then_publishes_proof_after_agent_cleanup(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    conductor = _driver_conductor(start_result=StartProblemResult.SUCCESS)
    conductor.problem_id = "cronjob_sidecar_blocks_completion_hotel_reservation"
    conductor.problems.get_problem_ids.side_effect = lambda all=False: [conductor.problem_id]
    conductor.incident_started_at = datetime(2026, 9, 26, 19, tzinfo=UTC)
    run = _fake_run(tmp_path)
    published_run = tmp_path / "results" / "batch" / "assistant_v3" / conductor.problem_id / "run_1"
    published_run.mkdir(parents=True)
    run.finalize_and_publish.return_value = published_run
    launcher = _configure_driver_test(benchmark_main, monkeypatch, tmp_path, conductor, run)
    registration = SimpleNamespace(agent_version="v3", kickoff_env={})
    monkeypatch.setattr(benchmark_main, "get_agent", Mock(return_value=registration))
    monkeypatch.setattr(benchmark_main, "_write_assistant_driver_config", Mock())
    monkeypatch.setattr(benchmark_main, "_assistant_runtime_environment", lambda: {})
    monkeypatch.setattr(benchmark_main, "_assistant_driver_config", Mock(return_value=object()))
    monkeypatch.setattr(benchmark_main, "_assistant_prompt_context", Mock(return_value={}))
    monkeypatch.setattr(benchmark_main, "write_pre_agent_failure", Mock())
    monkeypatch.setattr(benchmark_main, "finalize_attempt_artifacts", Mock())
    monkeypatch.setattr(benchmark_main.trace_postprocess, "write_trajectory", Mock(return_value=None))
    monkeypatch.setattr(benchmark_main, "checkpoint_assistant_attempt", Mock())
    monkeypatch.setattr(benchmark_main.AssistantV3Config, "from_env", Mock(return_value=object()))
    proof = {
        "schema": "sregym.splunk_lite_pre_agent.v1",
        "status": "ready",
        "case_id": conductor.problem_id,
        "run_id": run.artifact_id,
        "window": {"start": "2026-09-26T19:00:00Z", "end": "2026-09-26T19:05:00Z"},
    }
    gate = Mock(return_value=proof)
    monkeypatch.setattr(benchmark_main, "_assistant_case_preflight", gate)
    monkeypatch.setattr(benchmark_main.asyncio, "to_thread", AsyncMock(side_effect=lambda fn, *a: fn(*a)))
    write_proof = benchmark_main.atomic_write_campaign

    def checked_write(path, payload):
        assert launcher.cleanup_agent.called
        write_proof(path, payload)

    monkeypatch.setattr(benchmark_main, "atomic_write_campaign", checked_write)

    benchmark_main.driver_loop(
        conductor,
        problem_selection=[conductor.problem_id],
        agent_to_run="assistant_v3",
        observability_provider=SimpleNamespace(name="splunk"),
        assistant_prompt_arm="symptom_guided",
    )
    gate.assert_called_once()
    launcher.ensure_started.assert_awaited_once()
    assert conductor.incident_ended_at == datetime(2026, 9, 26, 19, 5, tzinfo=UTC)
    assert not (run.active_dir / "splunk_lite_pre_agent.json").exists()
    assert json.loads((published_run / "splunk_lite_pre_agent.json").read_text()) == proof


def test_guided_case_gate_failure_records_unscored_attempt_and_never_launches_agent(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    conductor = _driver_conductor(start_result=StartProblemResult.SUCCESS)
    conductor.problem_id = "cronjob_sidecar_blocks_completion_hotel_reservation"
    conductor.problems.get_problem_ids.side_effect = lambda all=False: [conductor.problem_id]
    conductor.incident_started_at = datetime(2026, 9, 26, 19, tzinfo=UTC)
    conductor.finalize_attempt_status.return_value = "incomplete"
    conductor.record_incomplete_attempt.side_effect = lambda reason: conductor.results.update(
        {
            "run_status": "incomplete",
            "incomplete_reason": reason,
        }
    )
    run = _fake_run(tmp_path)
    launcher = _configure_driver_test(benchmark_main, monkeypatch, tmp_path, conductor, run)
    published = tmp_path / "published"
    published.mkdir()
    run.finalize_and_publish.return_value = published
    monkeypatch.setattr(
        benchmark_main,
        "_assistant_case_preflight",
        Mock(
            side_effect=benchmark_main.CaseGateError(
                "pre-agent evidence did not become ready",
                {"status": "missing_causal_telemetry"},
            )
        ),
    )
    monkeypatch.setattr(benchmark_main.asyncio, "to_thread", AsyncMock(side_effect=lambda fn, *a: fn(*a)))
    monkeypatch.setattr(benchmark_main, "_assistant_driver_config", Mock(return_value=object()))
    monkeypatch.setattr(benchmark_main, "_assistant_prompt_context", Mock(return_value={}))
    monkeypatch.setattr(benchmark_main, "write_pre_agent_failure", Mock())
    monkeypatch.setattr(benchmark_main, "finalize_attempt_artifacts", Mock())
    monkeypatch.setattr(benchmark_main.trace_postprocess, "write_trajectory", Mock(return_value=None))
    checkpoint = Mock()
    monkeypatch.setattr(benchmark_main, "checkpoint_assistant_attempt", checkpoint)
    monkeypatch.setattr(benchmark_main.AssistantV3Config, "from_env", Mock(return_value=object()))

    with pytest.raises(benchmark_main.BenchmarkCampaignAborted, match="not trustworthy"):
        benchmark_main.driver_loop(
            conductor,
            problem_selection=[conductor.problem_id],
            agent_to_run="assistant_v3",
            observability_provider=SimpleNamespace(name="splunk"),
            assistant_prompt_arm="symptom_guided",
        )
    launcher.ensure_started.assert_not_awaited()
    conductor.finish_problem_in_background.assert_called()
    assert conductor.results["infrastructure_invalid"] is True
    assert conductor.results["incomplete_reason"] == "pre_agent_evidence_failed"
    assert not (run.active_dir / "splunk_lite_pre_agent.json").exists()
    assert json.loads((published / "splunk_lite_pre_agent.json").read_text())["status"] == "missing_causal_telemetry"
    checkpoint.assert_called_once()


def test_inspection_gate_waits_after_readiness_before_starting_assistant(monkeypatch, tmp_path):
    benchmark_main = _load_main_module()
    conductor = _driver_conductor(start_result=StartProblemResult.SUCCESS)
    run = _fake_run(tmp_path)
    launcher = _configure_driver_test(benchmark_main, monkeypatch, tmp_path, conductor, run)
    registration = SimpleNamespace(agent_version="v3", kickoff_env={})
    monkeypatch.setattr(benchmark_main, "get_agent", Mock(return_value=registration))
    monkeypatch.setattr(benchmark_main, "_write_assistant_driver_config", Mock())
    monkeypatch.setattr(benchmark_main, "_assistant_runtime_environment", lambda: {})
    monkeypatch.setattr(benchmark_main, "_assistant_driver_config", Mock(return_value=object()))
    monkeypatch.setattr(benchmark_main, "_assistant_prompt_context", Mock(return_value={}))
    monkeypatch.setattr(benchmark_main, "_assistant_inspection_preview", Mock(return_value=("prompt", "window")))
    monkeypatch.setattr(benchmark_main, "write_pre_agent_failure", Mock())
    monkeypatch.setattr(benchmark_main, "finalize_attempt_artifacts", Mock())
    monkeypatch.setattr(benchmark_main.trace_postprocess, "write_trajectory", Mock(return_value=None))
    monkeypatch.setattr(benchmark_main, "checkpoint_assistant_attempt", Mock(), raising=False)
    monkeypatch.setattr(benchmark_main.AssistantV3Config, "from_env", Mock(return_value=object()))
    gate = AsyncMock()
    monkeypatch.setattr(benchmark_main, "_wait_for_operator_inspection", gate)

    benchmark_main.driver_loop(
        conductor,
        problem_selection=["problem"],
        agent_to_run="assistant_v3",
        observability_provider=SimpleNamespace(name="splunk"),
        inspect_before_agent=True,
    )

    gate.assert_awaited_once_with(
        problem_id="problem",
        namespace="otel-demo",
        run_id=run.artifact_id,
    )
    launcher.ensure_started.assert_awaited_once_with(registration)


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
    output = capsys.readouterr().out
    assert "--observability-provider {none,splunk}" in output
    assert "--inspect-before-agent" in output
    assert "--agent-image" in output


def test_agent_image_selection_reuses_explicit_local_image_without_rebuild():
    benchmark_main = _load_main_module()

    assert benchmark_main.resolve_agent_image("sregym-agent-base:latest", judge_image=None, force_build=False) == (
        "sregym-agent-base:latest",
        False,
    )
    with pytest.raises(ValueError, match="cannot be combined"):
        benchmark_main.resolve_agent_image("sregym-agent-base:latest", judge_image=None, force_build=True)
    with pytest.raises(ValueError, match="judge bridge"):
        benchmark_main.resolve_agent_image("sregym-agent-base:latest", judge_image="bridge-image", force_build=False)


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
