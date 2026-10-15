import argparse
import asyncio
import contextlib
import csv
import importlib
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from clients.assistant_v3.client import AssistantV3Config
from clients.assistant_v3.driver import (
    DriverRunConfig,
    _build_action_instructions,
    finalize_attempt_artifacts,
    write_pre_agent_failure,
)
from clients.assistant_v3.prompt import render_prompt
from clients.harness.problem_id import HARNESS_ARTIFACT_ID_ENV, HARNESS_PROBLEM_ID_ENV
from clients.jev.config import configure as configure_jev
from logger import console, init_logger
from sregym.agent_launcher import AgentLauncher
from sregym.agent_registry import get_agent, list_agents
from sregym.conductor.conductor import ALL_STAGES, Conductor, ConductorConfig
from sregym.conductor.conductor_api import request_shutdown, run_api
from sregym.conductor.problem_sets import PROBLEM_SETS
from sregym.observability import create_provider
from sregym.observability.base import (
    ApplicationScope,
    AttemptContext,
    ObservabilityProvider,
    ProviderError,
    serialize_provider_artifact,
)
from sregym.observability.splunk import SplunkHttpBackend
from sregym.phases import read_ledger as read_phase_ledger
from sregym.phases import results_columns as phase_results_columns
from sregym.profile import PROFILES, get_profile, set_profile
from sregym.results.assistant_v3_campaign import (
    _atomic_write as atomic_write_campaign,
)
from sregym.results.assistant_v3_campaign import (
    checkpoint_attempt as checkpoint_assistant_attempt,
)
from sregym.results.resume import complete_resume_rows
from sregym.results.splunk_lite_cases import load_prompt_recipe
from sregym.results.splunk_lite_causal import (
    CaseGateError,
    read_source_cluster_object,
    read_source_events,
    read_source_logs,
    read_source_object,
    read_source_pods,
    wait_for_admission_webhook_pre_agent,
    wait_for_cronjob_pre_agent,
    wait_for_duplicate_pvc_mounts_pre_agent,
    wait_for_edge_pre_agent,
    wait_for_env_shadowing_pre_agent,
    wait_for_finalizer_deadlock_pre_agent,
    wait_for_internal_traffic_policy_pre_agent,
    wait_for_kafka_pre_agent,
    wait_for_mutating_webhook_pre_agent,
    wait_for_namespace_memory_quota_pre_agent,
    wait_for_network_policy_pre_agent,
    wait_for_readiness_pre_agent,
    wait_for_rolling_update_pre_agent,
    wait_for_search_retry_pre_agent,
    wait_for_secret_rotation_pre_agent,
    wait_for_service_dns_pre_agent,
    wait_for_service_wrong_pod_selection_pre_agent,
    wait_for_unschedulable_checkout_pre_agent,
    wait_for_valkey_auth_pre_agent,
    wait_for_wrong_dns_policy_pre_agent,
    wait_for_wrong_service_selector_pre_agent,
)
from sregym.run_artifacts import ArtifactFinalizationError, RunArtifacts
from sregym.service.container_runner import ContainerRunner, ExecInput, get_container_host_bind_address
from sregym.service.internet_policy import EndpointRule, InternetPolicy
from sregym.service.judge_runtime import JUDGE_BACKENDS, managed_judge_backend
from sregym.service.kubectl import ContainerPlatformError
from sregym.traces import postprocess as trace_postprocess
from sregym.traces import store as trace_store

LAUNCHER = AgentLauncher()
logger = logging.getLogger(__name__)
_driver_results: list[dict] = []
_driver_base_dir: Path | None = None
_driver_error: BaseException | None = None
EVALUATION_DRAIN_TIMEOUT_SECONDS = 300
CLEANUP_DRAIN_TIMEOUT_SECONDS = 300


def problem_cleanup_timeout(conductor) -> float:
    """Allow stateful reference recovery its own budget after agent grading."""
    problem = getattr(conductor, "problem", None)
    return getattr(problem, "cleanup_timeout_seconds", CLEANUP_DRAIN_TIMEOUT_SECONDS)


def _http_endpoint(value: str) -> str:
    """Validate an additional endpoint supplied on the command line."""
    try:
        EndpointRule.from_url(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


class BenchmarkCampaignAborted(RuntimeError):
    """The campaign stopped safely after persisting its partial results."""

    def __init__(self, message: str, partial_results: list[dict]):
        super().__init__(message)
        self.partial_results = partial_results


def validate_assistant_campaign(args) -> None:
    """Reject Assistant campaigns whose labels or lifecycle would be incomparable."""
    if args.agent != "assistant_v3":
        return
    if args.use_external_harness:
        raise ValueError("Assistant v3 cannot run through the external harness")
    if args.stages != ["diagnosis"]:
        raise ValueError("Assistant v3 comparison runs must be diagnosis-only")
    lite_cases = frozenset(PROBLEM_SETS["sregym-lite"])
    if args.suite != "sregym-lite" and args.problem not in lite_cases:
        raise ValueError("Assistant v3 v1 supports only registered SREGym-Lite cases")
    if not isinstance(args.model, str) or not args.model.strip():
        raise ValueError("Assistant v3 requires an explicit model")
    if args.reasoning_effort not in {"low", "medium", "high"}:
        raise ValueError("Assistant v3 requires explicit low, medium, or high reasoning")
    if not isinstance(args.judge_model, str) or not args.judge_model.strip():
        raise ValueError("Assistant v3 requires an explicit judge model")
    if not isinstance(args.judge_backend, str) or not args.judge_backend.strip():
        raise ValueError("Assistant v3 requires an explicit judge backend")
    if args.observability_provider != "splunk":
        raise ValueError("Assistant v3 benchmark runs require the Splunk observability provider")
    if getattr(args, "inspect_before_agent", False) and args.suite is not None:
        raise ValueError("Assistant v3 inspection runs require one explicit --problem")


def run_preflight_check(
    agent_name: str,
    container_runner: ContainerRunner | None = None,
    install_script: str | None = None,
) -> None:
    """Run the agent's pre-flight check inside the container."""
    agent_driver_modules = {
        "stratus": "clients.stratus.stratus_agent.driver.driver",
        "claudecode": "clients.claudecode.driver",
        "codex": "clients.codex.driver",
        "copilot": "clients.copilot.driver",
        "opencode": "clients.opencode.driver",
        "gemini": "clients.geminicli.driver",
        "cursor": "clients.cursor.driver",
        "mini-sre": "clients.mini_sre.driver",
        "assistant_v3": "clients.assistant_v3.driver",
    }
    module_path = agent_driver_modules.get(agent_name)
    if not module_path:
        return
    driver_mod = importlib.import_module(module_path)
    if not hasattr(driver_mod, "run_preflight"):
        return
    if container_runner is None:
        logger.warning("No container runner — skipping pre-flight check for '%s'", agent_name)
        return
    check_cmd = f"python3 -c 'from {module_path} import run_preflight; run_preflight()'"
    if install_script and not container_runner.has_prepared_agent_tools:
        check_cmd = f"/opt/sregym/install-scripts/{install_script} > /dev/null 2>&1 && {check_cmd}"
    preflight_env = {}
    if agent_name == "assistant_v3":
        preflight_env = {
            name: os.environ[name]
            for name in ("ASSISTANT_V3_URL", "ASSISTANT_V3_AUTH_TOKEN", "SF_TOKEN")
            if os.environ.get(name)
        }
    try:
        result = container_runner.run_sync(
            ExecInput(command=check_cmd, env=preflight_env, label="preflight", timeout=180)
        )
    except BaseException:
        container_runner.close()
        raise
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())
        container_runner.close()
        raise RuntimeError(f"Pre-flight check failed for '{agent_name}'")


def run_judge_preflight_check() -> None:
    """Validate the judge model and credentials through its LiteLLM backend."""
    from llm_backend.init_backend import get_llm_backend_for_judge

    logger.info("🔍 Running pre-flight check for judge model...")
    try:
        get_llm_backend_for_judge().inference("Say ok.", system_prompt="Reply with exactly 'ok'.")
    except Exception as e:
        logger.error(f"❌ Judge pre-flight check failed: {e}")
        logger.error("Check --judge-model and credentials for the selected judge backend.")
        sys.exit(1)

    logger.info("✅ Judge pre-flight check passed")


def _problem_result_paths(base_dir: Path, agent: str, problem_id: str) -> tuple[Path, Path]:
    final_path = base_dir / agent / problem_id / f"{problem_id}_{agent}_results.csv"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    return final_path.with_name(f"_running_{final_path.name}"), final_path


def get_current_datetime_formatted():
    now = datetime.now()
    formatted_datetime = now.strftime("%m%d_%H%M")
    return formatted_datetime


def _restore_env_var(name: str, previous_value: str | None) -> None:
    if previous_value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous_value


async def _wait_for_operator_inspection(*, problem_id: str, namespace: str, run_id: str) -> None:
    """Pause a live attempt after telemetry readiness and before agent launch."""
    console.log("\n🔎 Inspection gate reached; the fault and telemetry exporters remain active.")
    console.log(f"   Problem: {problem_id}")
    console.log(f"   Namespace: {namespace}")
    console.log(f"   Run/cluster scope: {run_id}")
    try:
        await asyncio.to_thread(input, "Press Enter to launch the agent and continue this same attempt... ")
    except EOFError as exc:
        raise RuntimeError("--inspect-before-agent requires an interactive terminal") from exc


def _env_status(name: str) -> str:
    return "set" if os.environ.get(name) else "unset"


def _is_opencode_local_model(agent: str | None, model: str) -> bool:
    return agent == "opencode" and model.startswith("local/")


def _normalize_opencode_local_model_for_litellm(model: str) -> str:
    return f"openai/{model.split('/', 1)[1]}"


def _configure_model_environment(args) -> tuple[str, str]:
    agent_model = args.model
    raw_judge_model = args.judge_model or args.model
    reasoning_effort = getattr(args, "reasoning_effort", None)
    normalizes_opencode_local_judge = _is_opencode_local_model(args.agent, raw_judge_model)
    judge_model = (
        _normalize_opencode_local_model_for_litellm(raw_judge_model)
        if normalizes_opencode_local_judge
        else raw_judge_model
    )

    os.environ["AGENT_MODEL_ID"] = agent_model
    os.environ["JUDGE_MODEL_ID"] = judge_model
    if reasoning_effort:
        os.environ["AGENT_REASONING_EFFORT"] = reasoning_effort
    else:
        os.environ.pop("AGENT_REASONING_EFFORT", None)

    if os.environ.get("SREGYM_JUDGE_BRIDGE_URL"):
        return agent_model, judge_model

    if not getattr(args, "judge_model", None) or normalizes_opencode_local_judge:
        if not os.environ.get("JUDGE_API_BASE") and os.environ.get("AGENT_API_BASE"):
            os.environ["JUDGE_API_BASE"] = os.environ["AGENT_API_BASE"]
        if not os.environ.get("JUDGE_API_KEY") and os.environ.get("AGENT_API_KEY"):
            os.environ["JUDGE_API_KEY"] = os.environ["AGENT_API_KEY"]

    if normalizes_opencode_local_judge:
        if not os.environ.get("JUDGE_API_BASE"):
            raise ValueError("AGENT_API_BASE or JUDGE_API_BASE is required to use an OpenCode local model as the judge")
        if not os.environ.get("JUDGE_API_KEY"):
            os.environ["JUDGE_API_KEY"] = "dummy"

    return agent_model, judge_model


@contextlib.contextmanager
def _artifact_environment(run: RunArtifacts):
    previous = {
        "AGENT_LOGS_DIR": os.environ.get("AGENT_LOGS_DIR"),
        HARNESS_ARTIFACT_ID_ENV: os.environ.get(HARNESS_ARTIFACT_ID_ENV),
        HARNESS_PROBLEM_ID_ENV: os.environ.get(HARNESS_PROBLEM_ID_ENV),
    }
    os.environ["AGENT_LOGS_DIR"] = str(run.active_dir.resolve())
    os.environ[HARNESS_ARTIFACT_ID_ENV] = run.artifact_id
    os.environ.pop(HARNESS_PROBLEM_ID_ENV, None)
    try:
        yield
    finally:
        for name, value in previous.items():
            _restore_env_var(name, value)


def _assistant_runtime_environment() -> dict[str, str]:
    """Return only the Assistant credentials/config needed by its isolated driver."""
    names = (
        "ASSISTANT_V3_URL",
        "ASSISTANT_V3_AUTH_TOKEN",
        "SF_TOKEN",
        "AGENT_MODEL_ID",
        "AGENT_REASONING_EFFORT",
        "API_PORT",
    )
    environment = {name: os.environ[name] for name in names if os.environ.get(name)}
    environment["SREGYM_ASSISTANT_DRIVER_CONFIG"] = "/logs/assistant_v3_driver_config.json"
    return environment


def _write_assistant_driver_config(
    run: RunArtifacts,
    conductor: Conductor,
    agent_version: str | None,
    provider: ObservabilityProvider,
    judge_backend: str,
    *,
    prompt_arm: str = "time_only",
) -> None:
    driver_config = _assistant_driver_config(
        run,
        conductor,
        agent_version,
        provider,
        judge_backend,
        prompt_arm=prompt_arm,
    )
    readiness = conductor.observability_readiness
    payload = {
        "run_id": driver_config.run_id,
        "attempt": driver_config.attempt,
        "benchmark_profile": driver_config.benchmark_profile,
        "comparable": driver_config.comparable,
        "judge_model": driver_config.judge_model,
        "judge_backend": driver_config.judge_backend,
        "observability_provider": driver_config.observability_provider,
        "readiness_report": serialize_provider_artifact(readiness) if readiness is not None else None,
        "agent_version": driver_config.agent_version,
        "observability_chart_version": driver_config.observability_chart_version,
        "hec_index": driver_config.hec_index,
        "logs_connection_id": driver_config.logs_connection_id,
        "resolved_logs_connection_id": driver_config.resolved_logs_connection_id,
        "symptom": driver_config.symptom,
        "attempt_started_at": (
            driver_config.attempt_started_at.isoformat().replace("+00:00", "Z")
            if driver_config.attempt_started_at is not None
            else None
        ),
        "telemetry_window_ended_at": (
            driver_config.telemetry_window_ended_at.isoformat().replace("+00:00", "Z")
            if driver_config.telemetry_window_ended_at is not None
            else None
        ),
    }
    path = run.active_dir / "assistant_v3_driver_config.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _assistant_driver_config(
    run: RunArtifacts,
    conductor: Conductor,
    agent_version: str | None,
    provider: ObservabilityProvider,
    judge_backend: str,
    *,
    prompt_arm: str = "time_only",
) -> DriverRunConfig:
    if prompt_arm not in {"time_only", "symptom_guided"}:
        raise ValueError("unknown Assistant prompt arm")
    symptom = None
    if prompt_arm == "symptom_guided":
        if not isinstance(conductor.problem_id, str):
            raise ValueError("symptom-guided prompts require a registered Lite case")
        symptom = load_prompt_recipe(conductor.problem_id).symptom
    configuration = getattr(provider, "configuration", None)
    attempt_context = getattr(conductor, "_observability_context", None)
    readiness = conductor.observability_readiness
    window_ended_at = getattr(conductor, "incident_ended_at", None)
    if window_ended_at is None and readiness:
        window_ended_at = max((signal.checked_at for signal in readiness.signals), default=None)
    return DriverRunConfig(
        run_id=run.artifact_id,
        attempt=run.attempt,
        artifacts_root=run.active_dir,
        benchmark_profile=get_profile(),
        comparable=get_profile() == "full" and symptom is None,
        judge_model=os.environ["JUDGE_MODEL_ID"],
        judge_backend=judge_backend,
        observability_provider=provider.name,
        readiness_report=readiness,
        agent_version=agent_version,
        observability_chart_version="0.160.0" if provider.name == "splunk" else None,
        hec_index=getattr(configuration, "hec_index", None),
        logs_connection_id=getattr(configuration, "logs_connection_id", None),
        resolved_logs_connection_id=getattr(provider, "_connection_id", None),
        attempt_started_at=(
            getattr(conductor, "incident_started_at", None) or getattr(attempt_context, "attempt_started_at", None)
        ),
        telemetry_window_ended_at=window_ended_at,
        symptom=symptom,
    )


def _assistant_case_preflight(
    run: RunArtifacts,
    conductor: Conductor,
    provider: ObservabilityProvider,
) -> dict:
    """Prove one reviewed case without writing oracle facts into agent-mounted logs."""
    if conductor.problem_id not in {
        "cronjob_sidecar_blocks_completion_hotel_reservation",
        "admission_webhook_outage_hotel_reservation",
        "duplicate_pvc_mounts_social_network",
        "edge_request_filter_cpu_saturation",
        "env_variable_shadowing_astronomy_shop",
        "finalizer_deadlock_controller_hotel_reservation",
        "internal_traffic_policy_local_astronomy_shop",
        "kafka_poison_pill_hol_block",
        "mutating_webhook_resource_limits_social_network",
        "namespace_memory_limit",
        "network_policy_block",
        "readiness_probe_misconfiguration_social_network",
        "service_dns_resolution_failure_social_network",
        "service_wrong_pod_selection_hotel_reservation",
        "secret_rotation_stale_env_credentials_astronomy_shop",
        "wrong_dns_policy_astronomy_shop",
        "wrong_service_selector_social_network",
        "rolling_update_misconfigured_social_network",
        "search_rate_retry_collapse_hotel_reservation",
        "valkey_auth_disruption",
        "unschedulable_incorrect_port_assignment",
    }:
        raise CaseGateError("this Lite case has no reviewed executable causal check")
    start = conductor.incident_started_at
    app = conductor.app
    connection_id = getattr(provider, "_connection_id", None)
    configuration = getattr(provider, "configuration", None)
    if start is None or app is None or not connection_id or configuration is None:
        raise CaseGateError("the Splunk case gate is missing its incident or destination scope")
    try:
        selected = subprocess.run(
            ["kubectl", "config", "current-context"],
            shell=False,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise CaseGateError("the local Kind source context is unavailable") from None
    if re.fullmatch(r"kind-[A-Za-z0-9_.-]+", selected) is None:
        raise CaseGateError("the selected source context is not a local Kind cluster")
    backend = SplunkHttpBackend(configuration)
    try:
        common = {
            "context": AttemptContext(run.artifact_id, get_profile(), False, start),
            "scope": ApplicationScope(app.app_name, tuple(getattr(app, "namespaces", None) or [app.namespace])),
            "connection_id": connection_id,
            "start": start,
        }
        if conductor.problem_id == "cronjob_sidecar_blocks_completion_hotel_reservation":
            return wait_for_cronjob_pre_agent(
                backend,
                source_reader=lambda: read_source_pods(app.namespace, context=selected),
                **common,
            )
        if conductor.problem_id == "duplicate_pvc_mounts_social_network":
            return wait_for_duplicate_pvc_mounts_pre_agent(
                backend,
                source_reader=lambda: (
                    read_source_object(
                        "persistentvolumeclaim", "jaeger-pvc", namespace=app.namespace, context=selected
                    ),
                    read_source_pods(app.namespace, context=selected),
                ),
                **common,
            )
        if conductor.problem_id == "admission_webhook_outage_hotel_reservation":
            return wait_for_admission_webhook_pre_agent(
                backend,
                source_reader=lambda: (
                    read_source_cluster_object(
                        "validatingwebhookconfiguration",
                        "pod-policy.validation.k8s.io",
                        context=selected,
                    ),
                    read_source_events(app.namespace, context=selected),
                ),
                **common,
            )
        if conductor.problem_id == "internal_traffic_policy_local_astronomy_shop":
            return wait_for_internal_traffic_policy_pre_agent(
                backend,
                source_reader=lambda: (
                    read_source_object("service", "recommendation", namespace=app.namespace, context=selected),
                    read_source_pods(app.namespace, context=selected),
                ),
                **common,
            )
        if conductor.problem_id == "service_dns_resolution_failure_social_network":
            return wait_for_service_dns_pre_agent(
                backend,
                source_reader=lambda: read_source_object(
                    "configmap",
                    "coredns",
                    namespace="kube-system",
                    context=selected,
                ),
                **common,
            )
        if conductor.problem_id == "service_wrong_pod_selection_hotel_reservation":
            return wait_for_service_wrong_pod_selection_pre_agent(
                backend,
                source_reader=lambda: (
                    read_source_object("service", "frontend", namespace=app.namespace, context=selected),
                    read_source_pods(app.namespace, context=selected),
                ),
                **common,
            )
        if conductor.problem_id == "wrong_service_selector_social_network":
            return wait_for_wrong_service_selector_pre_agent(
                backend,
                source_reader=lambda: (
                    read_source_object("service", "user-service", namespace=app.namespace, context=selected),
                    read_source_object("endpoints", "user-service", namespace=app.namespace, context=selected),
                ),
                **common,
            )
        if conductor.problem_id == "rolling_update_misconfigured_social_network":
            return wait_for_rolling_update_pre_agent(
                backend,
                source_reader=lambda: (
                    read_source_object("deployment", "custom-service", namespace=app.namespace, context=selected),
                    read_source_pods(app.namespace, context=selected),
                ),
                **common,
            )
        if conductor.problem_id == "search_rate_retry_collapse_hotel_reservation":
            return wait_for_search_retry_pre_agent(
                backend,
                source_reader=lambda: conductor.problem.workload.metrics.snapshot(),
                **common,
            )
        if conductor.problem_id == "namespace_memory_limit":
            return wait_for_namespace_memory_quota_pre_agent(
                backend,
                source_reader=lambda: (
                    read_source_object(
                        "resourcequota", "memory-limit-quota", namespace=app.namespace, context=selected
                    ),
                    read_source_events(app.namespace, context=selected),
                ),
                **common,
            )
        if conductor.problem_id == "valkey_auth_disruption":
            return wait_for_valkey_auth_pre_agent(
                backend,
                source_reader=lambda: read_source_logs(
                    app.namespace,
                    "cart",
                    context=selected,
                    start=start,
                ),
                **common,
            )
        if conductor.problem_id == "secret_rotation_stale_env_credentials_astronomy_shop":
            return wait_for_secret_rotation_pre_agent(
                backend,
                source_reader=lambda: (
                    read_source_object("deployment", "product-catalog", namespace=app.namespace, context=selected),
                    read_source_pods(app.namespace, context=selected),
                ),
                **common,
            )
        if conductor.problem_id == "unschedulable_incorrect_port_assignment":
            return wait_for_unschedulable_checkout_pre_agent(
                backend,
                source_reader=lambda: read_source_pods(app.namespace, context=selected),
                **common,
            )
        if conductor.problem_id == "network_policy_block":
            return wait_for_network_policy_pre_agent(
                backend,
                source_reader=lambda: read_source_object(
                    "networkpolicy",
                    "deny-all-recommendation",
                    namespace=app.namespace,
                    context=selected,
                ),
                **common,
            )
        if conductor.problem_id == "mutating_webhook_resource_limits_social_network":
            return wait_for_mutating_webhook_pre_agent(
                backend,
                source_reader=lambda: read_source_pods(app.namespace, context=selected),
                **common,
            )
        if conductor.problem_id == "finalizer_deadlock_controller_hotel_reservation":
            return wait_for_finalizer_deadlock_pre_agent(
                backend,
                source_reader=lambda: read_source_logs(
                    app.namespace,
                    "cleanup-controller",
                    context=selected,
                    start=start,
                ),
                **common,
            )
        if conductor.problem_id == "kafka_poison_pill_hol_block":
            return wait_for_kafka_pre_agent(
                backend,
                source_reader=lambda: read_source_logs(
                    app.namespace,
                    "orders-validator",
                    context=selected,
                    start=start,
                ),
                **common,
            )
        if conductor.problem_id == "readiness_probe_misconfiguration_social_network":
            return wait_for_readiness_pre_agent(
                backend,
                source_reader=lambda: read_source_pods(app.namespace, context=selected),
                **common,
            )
        if conductor.problem_id == "env_variable_shadowing_astronomy_shop":
            return wait_for_env_shadowing_pre_agent(
                backend,
                source_reader=lambda: read_source_pods(app.namespace, context=selected),
                **common,
            )
        if conductor.problem_id == "wrong_dns_policy_astronomy_shop":
            return wait_for_wrong_dns_policy_pre_agent(
                backend,
                source_reader=lambda: read_source_pods(app.namespace, context=selected),
                **common,
            )
        return wait_for_edge_pre_agent(
            backend,
            source_reader=lambda: read_source_logs(
                app.namespace,
                "frontend-proxy",
                context=selected,
                start=start,
            ),
            **common,
        )
    except CaseGateError:
        raise
    except Exception:
        raise CaseGateError("pre-agent verifier failed unexpectedly") from None
    finally:
        backend.close()


def _assistant_prompt_context(conductor: Conductor) -> dict[str, str]:
    app = conductor.app
    if app is None:
        raise RuntimeError("Assistant prompt metadata is unavailable before application setup")
    namespaces = getattr(app, "namespaces", None) or [app.namespace]
    return {
        "app_name": app.app_name,
        "app_description": str(app.description),
        "app_namespace": ", ".join(namespaces),
    }


def _assistant_inspection_preview(
    run: RunArtifacts,
    conductor: Conductor,
    provider: ObservabilityProvider,
    judge_backend: str,
    *,
    prompt_arm: str = "time_only",
) -> tuple[str, str]:
    """Render the same public-metadata prompt and UTC scope used by the driver."""
    config = _assistant_driver_config(
        run,
        conductor,
        None,
        provider,
        judge_backend,
        prompt_arm=prompt_arm,
    )
    if config.attempt_started_at is None or config.telemetry_window_ended_at is None:
        raise RuntimeError("Assistant inspection requires a completed telemetry readiness window")
    prompt = render_prompt(_assistant_prompt_context(conductor)).text
    instructions = _build_action_instructions(
        config.attempt_started_at,
        config.telemetry_window_ended_at,
        symptom=config.symptom,
    )
    return prompt, instructions


def driver_loop(
    conductor: Conductor,
    problem_selection: Sequence[str] | None = None,
    agent_to_run: str | None = None,
    use_external_harness: bool = False,
    n_attempts: int = 1,
    agent_timeout: int = 1800,
    resume_csv: str | None = None,
    judge_backend: str = "api",
    observability_provider: ObservabilityProvider | None = None,
    inspect_before_agent: bool = False,
    assistant_prompt_arm: str = "time_only",
):
    """
    Deploy each problem and wait for HTTP grading via POST /submit.
    Returns a list of flattened dicts with results per problem.

    Args:
        conductor: The Conductor instance
        problem_selection: Optional ordered problem IDs to run. If omitted, use the registry's default selection.
        agent_to_run: Agent name to run (required unless use_external_harness is True).
        use_external_harness: If True, inject fault and exit without running evaluation logic.
        n_attempts: Number of end-to-end attempts to run each problem.
        resume_csv: Path to a previous results CSV to resume from (skip completed problems).
        inspect_before_agent: Pause after telemetry readiness and before starting the agent.
    """

    async def driver():
        provider = observability_provider or create_provider("none")
        base_dir = Path("results") / get_current_datetime_formatted()
        base_dir.mkdir(parents=True, exist_ok=True)
        global _driver_base_dir
        _driver_base_dir = base_dir
        # give the API a moment to bind
        await asyncio.sleep(1)

        # Verify agent exists in registry (skip if using external harness)
        if use_external_harness:
            # An uncleanly terminated filtered run can leave the cluster-wide
            # boundary behind. External harnesses provide their own isolation,
            # so do not let stale local-run state affect them.
            conductor.clear_cluster_egress_boundary()
        else:
            if agent_to_run is None:
                raise ValueError("agent_to_run is required unless using an external harness")
            available_agents = list_agents(path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml").keys()
            if agent_to_run not in available_agents:
                console.log(f"⚠️ Agent '{agent_to_run}' not found in registry. Available agents: {available_agents}")
                sys.exit(1)

            console.log(f"Starting agent now: {agent_to_run}")
            conductor.register_agent(agent_to_run)

            # Start K8s API proxy to hide chaos engineering namespaces from the agent
            console.log("🔒 Starting Kubernetes API proxy to hide chaos namespaces...")
            conductor.start_k8s_proxy()
            LAUNCHER.set_agent_kubeconfig(conductor.get_agent_kubeconfig_path())

        all_results_for_agent = []

        async def finish_problem_with_deadline(timeout_reason: str) -> bool:
            """Run safety cleanup without letting a stuck Kubernetes call hang the campaign."""
            try:
                conductor.finish_problem_in_background()
                await conductor.wait_for_submission_work(timeout=problem_cleanup_timeout(conductor))
            except TimeoutError:
                conductor.abandon_submission_work()
                conductor.results["cleanup_timed_out"] = True
                conductor.record_incomplete_attempt(timeout_reason)
                console.log(
                    "⛔ Conductor cleanup did not finish before the cleanup deadline; "
                    "aborting without starting another attempt"
                )
                return False
            except Exception as exc:
                conductor.results["cleanup_failed"] = True
                conductor.results["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                conductor.record_incomplete_attempt("cleanup_failed")
                console.log(f"⛔ Conductor cleanup raised: {exc}")
                return False
            return not conductor.results.get("cleanup_failed", False)

        # Get all problem IDs and apply an explicit CLI selection when supplied.
        problem_ids = conductor.problems.get_problem_ids()
        all_problem_ids = conductor.problems.get_problem_ids(all=True)
        if problem_selection is not None:
            unknown_problem_ids = set(problem_selection) - set(all_problem_ids)
            if unknown_problem_ids:
                console.log(
                    f"⚠️  Problems not found in registry: {sorted(unknown_problem_ids)}. "
                    f"Available problems: {all_problem_ids}"
                )
                sys.exit(1)
            problem_ids = list(problem_selection)
            if len(problem_ids) == 1:
                console.log(f"🎯 Running single problem: {problem_ids[0]}")
            else:
                console.log(f"🎯 Running selected benchmark suite: {len(problem_ids)} problems")

        # sanity check: are there any specified problem ids that do not exist in the registry?
        unknown_problem_ids = set(problem_ids) - set(all_problem_ids)
        if unknown_problem_ids:
            console.log(
                f"⚠️  These problem ids do not exist in the registry and they will be skipped: {unknown_problem_ids}"
            )
        for unknown_problem_id in unknown_problem_ids:
            problem_ids.remove(unknown_problem_id)

        # Resume support: load completed problems from previous CSV and pre-seed results
        from collections import Counter

        completed_problems: set[str] = set()
        completed_attempts: dict[str, set[int]] = {}
        attempt_counts: Counter[str] = Counter()
        if resume_csv:
            try:
                with open(resume_csv, newline="") as f:
                    reader = csv.DictReader(f)
                    resume_rows = list(reader)

                complete_rows = complete_resume_rows(resume_rows, n_attempts)

                for (pid, attempt_number), row in complete_rows.items():
                    completed_attempts.setdefault(pid, set()).add(attempt_number)
                    all_results_for_agent.append(row)

                attempt_counts = Counter({pid: len(attempts) for pid, attempts in completed_attempts.items()})
                completed_problems = {pid for pid, count in attempt_counts.items() if count >= n_attempts}
                console.log(
                    f"📋 Resuming from {resume_csv}: {len(completed_problems)} problems already done; "
                    "incomplete attempts will be rerun"
                )
            except Exception as e:
                console.log(f"⚠️  Failed to load resume CSV: {e}")

        # Bar tracks attempts (problems × n_attempts), not problems, so the
        # 0/N total reflects total work even when n_attempts > 1.
        already_done = sum(min(attempt_counts.get(p, 0), n_attempts) for p in problem_ids)
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
        )
        task_id = progress.add_task(
            f"[cyan]Benchmarking {agent_to_run or 'agent'}",
            total=len(problem_ids) * n_attempts,
            completed=already_done,
        )
        progress.start()

        for pid in problem_ids:
            if pid in completed_problems:
                console.log(f"⏭️  Skipping already-completed problem: {pid}")
                continue

            conductor.problem_id = pid

            # Keep partial results on the destination filesystem so publication
            # remains atomic when results/ is a container bind mount.
            tmp_path, final_csv_path = _problem_result_paths(base_dir, str(agent_to_run), pid)

            attempts_to_run = [
                attempt for attempt in range(1, n_attempts + 1) if attempt not in completed_attempts.get(pid, set())
            ]
            for attempt_position, attempt in enumerate(attempts_to_run):
                abort_campaign_after_attempt = False
                progress.update(
                    task_id,
                    description=f"[cyan]Benchmarking {agent_to_run or 'agent'} — {pid} (attempt {attempt}/{n_attempts})",
                )
                console.log(f"\n🔍 Starting problem: {pid} (Attempt {attempt} of {n_attempts})")

                run = None
                if provider.name != "none" or agent_to_run == "assistant_v3":
                    run = RunArtifacts.create(
                        staging_root=Path(".runtime"),
                        results_root=base_dir,
                        problem_id=pid,
                        agent=agent_to_run or "agent",
                        attempt=attempt,
                    )
                    conductor.bind_observability_attempt(
                        provider,
                        AttemptContext(
                            run_id=run.artifact_id,
                            profile=get_profile(),
                            comparable=get_profile() == "full" and assistant_prompt_arm == "time_only",
                            attempt_started_at=datetime.now(UTC),
                        ),
                    )

                # Bind the phase ledger before the first phase runs. It cannot
                # live inside the run directory: RunArtifacts.create() happens
                # after deploy, and deploy is a phase we want recorded. Sitting
                # beside the run directories keeps it per-attempt and out of the
                # way of artifact publication.
                #
                # The models are recorded too: without them a ledger is only
                # interpretable next to the log that produced it. Read from the
                # environment, where _configure_model_environment already put
                # them, so agent and judge cannot drift apart.
                phases_path = Path(base_dir) / (agent_to_run or "agent") / pid / f"phases_attempt{attempt}.jsonl"
                conductor.bind_phase_ledger(
                    phases_path,
                    attempt=attempt,
                    agent=agent_to_run,
                    model=os.environ.get("AGENT_MODEL_ID"),
                    judge_model=os.environ.get("JUDGE_MODEL_ID"),
                    judge_backend=judge_backend,
                )

                # Retry start_problem up to 3 times to handle transient deploy failures
                max_deploy_retries = 3
                result = None
                start_error: Exception | None = None
                deploy_cleanup_failed = False
                for deploy_attempt in range(1, max_deploy_retries + 1):
                    try:
                        result = await conductor.start_problem()
                        break  # Success — exit retry loop
                    except Exception as e:
                        start_error = e
                        console.log(
                            f"❌ start_problem failed for '{pid}' "
                            f"(deploy attempt {deploy_attempt}/{max_deploy_retries}): {e}"
                        )
                        if isinstance(e, ContainerPlatformError):
                            console.log(
                                "⛔ Image platform failures require a compatible image; skipping deploy retries"
                            )
                            break
                        if isinstance(e, ProviderError):
                            console.log("⛔ Observability readiness failed; skipping deploy retries")
                            conductor.results["infrastructure_invalid"] = True
                            conductor.results["included_in_diagnosis_pass_rate"] = False
                            conductor.results["observability_error"] = e.kind
                            conductor.observability_readiness = e.readiness_report
                            break
                        if deploy_attempt < max_deploy_retries:
                            console.log("🧹 Cleaning up before retry...")
                            cleanup_succeeded = await finish_problem_with_deadline(
                                "cleanup_timeout_after_deploy_failure"
                            )
                            if not cleanup_succeeded:
                                deploy_cleanup_failed = True
                                console.log("⛔ Cleanup failed; refusing to retry deployment against uncertain state")
                                break
                            console.log(f"🔄 Retrying start_problem for '{pid}'...")
                        else:
                            console.log(
                                f"⛔ All {max_deploy_retries} deploy attempts failed for '{pid}', skipping this attempt"
                            )
                            result = None

                if result is None:
                    # The inner retry loop already logged the failure. Don't crash the
                    # entire driver — record the failure, clean up cluster state, and
                    # move on to the next problem so the benchmark can keep making progress.
                    if not deploy_cleanup_failed:
                        deploy_cleanup_failed = not await finish_problem_with_deadline(
                            "cleanup_timeout_after_deploy_failure"
                        )
                    if deploy_cleanup_failed and conductor.results.get("run_status") != "incomplete":
                        conductor.record_incomplete_attempt("cleanup_failed")
                    snapshot = {
                        "problem_id": pid,
                        "attempt": attempt,
                        "deployment_profile": get_profile(),
                        "deploy_failed": True,
                    }
                    for stage, outcome in conductor.results.items():
                        if isinstance(outcome, dict):
                            for key, value in outcome.items():
                                snapshot[f"{stage}.{key}"] = value
                        else:
                            snapshot[stage] = outcome
                    if run is not None and agent_to_run == "assistant_v3":
                        driver_config = _assistant_driver_config(
                            run,
                            conductor,
                            None,
                            provider,
                            judge_backend,
                            prompt_arm=assistant_prompt_arm,
                        )
                        write_pre_agent_failure(
                            driver_config,
                            assistant_configuration=AssistantV3Config.from_env(),
                            safe_message=(
                                str(start_error)
                                if isinstance(start_error, ProviderError)
                                else "Benchmark infrastructure failed before Assistant launch"
                            ),
                            prompt_context=_assistant_prompt_context(conductor),
                        )
                        failure_fields = sorted({key for row in [*all_results_for_agent, snapshot] for key in row})
                        published_run_dir = run.finalize_and_publish(
                            snapshot=snapshot,
                            fieldnames=failure_fields,
                            ownership_image=LAUNCHER.container_image,
                        )
                        scorecard_path = checkpoint_assistant_attempt(base_dir, published_run_dir)
                        logger.info(f"📊 Assistant v3 campaign scorecard updated at {scorecard_path}")
                    all_results_for_agent.append(snapshot)
                    fieldnames = sorted({key for row in all_results_for_agent for key in row})
                    with open(tmp_path, "w", newline="") as csvfile:
                        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                        writer.writeheader()
                        writer.writerows(all_results_for_agent)
                    if deploy_cleanup_failed:
                        os.replace(tmp_path, final_csv_path)
                        progress.advance(task_id, len(attempts_to_run) - attempt_position)
                        progress.stop()
                        if not use_external_harness:
                            conductor.stop_k8s_proxy()
                        if conductor.results.get("cleanup_timed_out"):
                            deploy_abort_reason = (
                                "Benchmark cleanup did not terminate during deployment; "
                                "later attempts were not started against uncertain state."
                            )
                        else:
                            deploy_abort_reason = (
                                "Benchmark cleanup failed during deployment; "
                                "later attempts were not started against uncertain state."
                            )
                        raise BenchmarkCampaignAborted(
                            deploy_abort_reason,
                            [{agent_to_run: all_results_for_agent}],
                        )
                    console.log(f"⏭️  Skipping remaining attempts for '{pid}' and moving to next problem")
                    # Account for this attempt + remaining skipped attempts on the bar.
                    progress.advance(task_id, len(attempts_to_run) - attempt_position)
                    break

                # If using external harness, fault is injected - exit now
                if use_external_harness:
                    console.log(f"✅ Fault injected for problem '{pid}'. Exiting for external harness.")
                    progress.stop()
                    return []

                assert agent_to_run is not None

                if run is None:
                    run = RunArtifacts.create(
                        staging_root=base_dir / ".runtime",
                        results_root=base_dir,
                        problem_id=pid,
                        agent=agent_to_run,
                        attempt=attempt,
                    )
                case_preflight_proof = None
                case_preflight_failed = False
                if (
                    agent_to_run == "assistant_v3"
                    and provider.name == "splunk"
                    and assistant_prompt_arm == "symptom_guided"
                ):
                    try:
                        case_preflight_proof = await asyncio.to_thread(
                            _assistant_case_preflight,
                            run,
                            conductor,
                            provider,
                        )
                        ended = str(case_preflight_proof["window"]["end"])
                        conductor.incident_ended_at = datetime.fromisoformat(ended.replace("Z", "+00:00"))
                        if case_preflight_proof["status"] == "ready_data_limited":
                            console.log(
                                f"✅ Pre-agent evidence checks passed for {pid}; causal mechanism requires additional access"
                            )
                        else:
                            console.log(f"✅ Pre-agent causal and delivery checks passed for {pid}")
                    except CaseGateError as error:
                        case_preflight_failed = True
                        case_preflight_proof = {
                            **(error.proof or {}),
                            "schema": "sregym.splunk_lite_pre_agent.v1",
                            "case_id": pid,
                            "run_id": run.artifact_id,
                            "status": (error.proof or {}).get("status", "query_error"),
                        }
                        conductor.results["infrastructure_invalid"] = True
                        conductor.results["included_in_diagnosis_pass_rate"] = False
                        conductor.results["observability_error"] = "case_evidence"
                        conductor.record_incomplete_attempt("pre_agent_evidence_failed")
                        console.log(f"⛔ Pre-agent evidence gate failed for {pid}: {error}")
                        cleanup_succeeded = await finish_problem_with_deadline("cleanup_timeout_after_case_gate")
                        abort_campaign_after_attempt = True
                        if not cleanup_succeeded:
                            conductor.results["cleanup_failed"] = True
                if inspect_before_agent and not case_preflight_failed:
                    app = conductor.app
                    if app is None:
                        raise RuntimeError("Inspection requires a deployed application")
                    if agent_to_run == "assistant_v3":
                        prompt, instructions = _assistant_inspection_preview(
                            run,
                            conductor,
                            provider,
                            judge_backend,
                            prompt_arm=assistant_prompt_arm,
                        )
                        console.log(f"\nAssistant v3 starter prompt:\n{prompt}")
                        console.log(f"\nAssistant v3 action instructions:\n{instructions}")
                    await _wait_for_operator_inspection(
                        problem_id=pid,
                        namespace=app.namespace,
                        run_id=run.artifact_id,
                    )
                agent_proc = None
                conductor.agent_timeout_seconds = agent_timeout

                if conductor.stage_sequence and not case_preflight_failed:
                    with _artifact_environment(run):
                        reg = get_agent(
                            agent_to_run,
                            path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml",
                        )
                        if reg:
                            if agent_to_run == "assistant_v3":
                                _write_assistant_driver_config(
                                    run,
                                    conductor,
                                    reg.agent_version,
                                    provider,
                                    judge_backend,
                                    prompt_arm=assistant_prompt_arm,
                                )
                                reg.kickoff_env = {
                                    **(reg.kickoff_env or {}),
                                    **_assistant_runtime_environment(),
                                }
                            agent_proc = await LAUNCHER.ensure_started(reg)
                else:
                    console.log("⏩ Agent launch skipped; waiting only for bounded cleanup")
                    if conductor.close_submissions():
                        try:
                            await conductor.wait_for_submission_work(timeout=problem_cleanup_timeout(conductor))
                        except TimeoutError:
                            abort_campaign_after_attempt = True
                            conductor.abandon_submission_work()
                            conductor.results["cleanup_timed_out"] = True
                            conductor.record_incomplete_attempt("cleanup_timeout_without_agent_stages")
                        except Exception as exc:
                            abort_campaign_after_attempt = True
                            conductor.results["cleanup_failed"] = True
                            conductor.results["cleanup_error"] = f"{type(exc).__name__}: {exc}"
                            conductor.record_incomplete_attempt("cleanup_failed")

                timed_out = False
                agent_start_time = time.monotonic()
                conductor.agent_deadline_monotonic = agent_start_time + agent_timeout
                while not abort_campaign_after_attempt and conductor.submission_stage != "done":
                    if time.monotonic() > conductor.agent_deadline_monotonic:
                        timed_out = True
                        console.log(f"⏰ Agent timeout ({agent_timeout}s) exceeded, killing agent")
                        submission_work = conductor.close_submissions()
                        LAUNCHER.cleanup_agent(agent_to_run)
                        conductor.results["timed_out"] = True
                        conductor.results["agent_timeout_seconds"] = agent_timeout
                        evaluation_failed = False
                        if submission_work:
                            console.log("⏳ Waiting for the accepted submission evaluation to complete...")
                            try:
                                await conductor.wait_for_submission_evaluations(
                                    timeout=EVALUATION_DRAIN_TIMEOUT_SECONDS
                                )
                            except TimeoutError:
                                evaluation_failed = True
                                abort_campaign_after_attempt = True
                                conductor.abandon_submission_work()
                                console.log(
                                    "⛔ Submission evaluation did not finish before the evaluation deadline; "
                                    "aborting the campaign without concurrent cluster cleanup"
                                )
                            except Exception as e:
                                evaluation_failed = True
                                console.log(f"⚠️  Conductor evaluation raised: {e}")
                        if abort_campaign_after_attempt:
                            reason = "evaluation_timeout_after_agent_timeout"
                        else:
                            reason = "evaluation_failed_after_agent_timeout" if evaluation_failed else "agent_timeout"
                        if evaluation_failed or conductor.missing_submission_stages():
                            conductor.record_incomplete_attempt(reason)
                        if not abort_campaign_after_attempt:
                            console.log("🧹 Running conductor cleanup after agent timeout...")
                            cleanup_succeeded = await finish_problem_with_deadline(
                                "cleanup_timeout_after_agent_timeout"
                            )
                            abort_campaign_after_attempt = not cleanup_succeeded
                        break

                    tracked_proc = LAUNCHER._procs.get(agent_to_run) or agent_proc
                    if tracked_proc:
                        tracked_proc.proc.poll()
                        if tracked_proc.proc.returncode is not None:
                            agent_proc = tracked_proc
                            console.log(f"⚠️  Agent process exited with return code {tracked_proc.proc.returncode}")
                            submission_work = conductor.close_submissions()
                            evaluation_failed = False
                            if submission_work:
                                console.log("⏳ Waiting for conductor evaluation to complete...")
                                try:
                                    await conductor.wait_for_submission_evaluations(
                                        timeout=EVALUATION_DRAIN_TIMEOUT_SECONDS
                                    )
                                except TimeoutError:
                                    evaluation_failed = True
                                    abort_campaign_after_attempt = True
                                    conductor.abandon_submission_work()
                                    console.log(
                                        "⛔ Submission evaluation did not finish before the evaluation deadline; "
                                        "aborting the campaign without concurrent cluster cleanup"
                                    )
                                except Exception as e:
                                    evaluation_failed = True
                                    console.log(f"⚠️  Conductor evaluation raised: {e}")
                            missing_stages = conductor.missing_submission_stages()
                            if abort_campaign_after_attempt or evaluation_failed or missing_stages:
                                if abort_campaign_after_attempt:
                                    reason = "evaluation_timeout_after_agent_exit"
                                elif evaluation_failed:
                                    reason = "evaluation_failed_after_agent_exit"
                                else:
                                    reason = "agent_exited_before_all_stages_completed"
                                conductor.record_incomplete_attempt(
                                    reason,
                                    agent_return_code=tracked_proc.proc.returncode,
                                )
                            if not abort_campaign_after_attempt:
                                console.log("🧹 Running conductor cleanup after agent exit...")
                                cleanup_succeeded = await finish_problem_with_deadline(
                                    "cleanup_timeout_after_agent_exit"
                                )
                                abort_campaign_after_attempt = not cleanup_succeeded
                            break
                    await asyncio.sleep(1)

                # The final evaluator sets stage=done during cleanup just
                # before its worker future returns. Consume that future before
                # freezing the attempt snapshot or starting another attempt.
                if not abort_campaign_after_attempt and conductor.close_submissions():
                    try:
                        await conductor.wait_for_submission_work(timeout=problem_cleanup_timeout(conductor))
                    except TimeoutError:
                        abort_campaign_after_attempt = True
                        conductor.abandon_submission_work()
                        conductor.results["cleanup_timed_out"] = True
                        conductor.record_incomplete_attempt("cleanup_timeout_after_stage_completion")
                        console.log(
                            "⛔ Conductor cleanup did not finish before the cleanup deadline; "
                            "aborting without starting another attempt"
                        )
                    except Exception as e:
                        console.log(f"⚠️  Conductor cleanup raised after stage completion: {e}")
                        conductor.results["cleanup_failed"] = True
                        conductor.results["cleanup_error"] = f"{type(e).__name__}: {e}"
                        conductor.record_incomplete_attempt("cleanup_failed")

                # Fold the phase ledger into the results so infra-vs-agent time
                # is a column rather than something to reconstruct from logs.
                # Read from the file, not from memory, so a phase that ended in
                # a process that later died is still counted.
                if conductor.phases is not None:
                    conductor.results.update(phase_results_columns(read_phase_ledger(conductor.phases.path)))

                run_status = conductor.finalize_attempt_status()
                if conductor.results.get("cleanup_failed"):
                    abort_campaign_after_attempt = True
                status_icon = "✅" if run_status == "complete" else "⚠️"
                console.log(f"{status_icon} {run_status.capitalize()} {pid}: results={conductor.results}", markup=False)
                # Wait for agent process to complete naturally before cleanup
                # This allows the agent to finish saving trajectories and other cleanup tasks
                if not use_external_harness:
                    agent_proc = LAUNCHER._procs.get(agent_to_run) or agent_proc
                    if agent_proc and not timed_out and not abort_campaign_after_attempt:
                        console.log("⏳ Waiting for agent process to complete...")
                        timeout = 60  # seconds
                        elapsed = 0
                        while elapsed < timeout:
                            agent_proc.proc.poll()
                            if agent_proc.proc.returncode is not None:
                                console.log(f"✅ Agent process completed with return code {agent_proc.proc.returncode}")
                                break
                            await asyncio.sleep(1)
                            elapsed += 1
                        else:
                            console.log(f"⚠️  Agent process did not complete within {timeout}s, will force cleanup")

                    # Publication must happen only after cleanup_agent has reaped
                    # the tracked docker client or shell process tree.
                    LAUNCHER.cleanup_agent(agent_to_run)
                    console.log(f"🧹 Cleaned up agent process for {agent_to_run}")

                (run.active_dir / "assistant_v3_driver_config.json").unlink(missing_ok=True)
                if agent_to_run == "assistant_v3":
                    if not (run.active_dir / "run_metadata.json").exists():
                        write_pre_agent_failure(
                            _assistant_driver_config(
                                run,
                                conductor,
                                None,
                                provider,
                                judge_backend,
                                prompt_arm=assistant_prompt_arm,
                            ),
                            assistant_configuration=AssistantV3Config.from_env(),
                            safe_message=(
                                "Pre-agent evidence gate failed; Assistant was not launched"
                                if case_preflight_failed
                                else "Assistant driver exited without a complete artifact set"
                            ),
                            prompt_context=_assistant_prompt_context(conductor),
                        )
                    finalize_attempt_artifacts(
                        run.active_dir,
                        delivery=conductor.observability_delivery,
                        cleanup_status="failed" if conductor.results.get("cleanup_failed") else "completed",
                        infrastructure_error=(
                            "Observability delivery audit failed"
                            if conductor.results.get("observability_error") and conductor.observability_delivery is None
                            else None
                        ),
                    )

                snapshot = {
                    "problem_id": pid,
                    "attempt": attempt,
                    "deployment_profile": get_profile(),
                    "judge_backend": judge_backend,
                }
                if provider.name != "none" or agent_to_run == "assistant_v3":
                    snapshot["observability_provider"] = provider.name
                    snapshot["comparable"] = get_profile() == "full" and assistant_prompt_arm == "time_only"
                if os.environ.get("AGENT_JEV_MODEL"):
                    snapshot["jev_model"] = os.environ["AGENT_JEV_MODEL"]
                internet_audit = LAUNCHER.internet_policy_result(agent_proc)
                snapshot.update(
                    {key: value for key, value in internet_audit.items() if key != "blocked_request_details"}
                )
                try:
                    run.save_network_audit(internet_audit)
                except OSError:
                    logger.exception("Could not save the network audit for %s attempt %s", pid, attempt)
                    snapshot["internet_audit_error"] = "could not save blocked-request records"
                for stage, outcome in conductor.results.items():
                    if isinstance(outcome, dict):
                        for k, v in outcome.items():
                            snapshot[f"{stage}.{k}"] = v
                    else:
                        snapshot[stage] = outcome

                fieldnames = sorted({key for row in [*all_results_for_agent, snapshot] for key in row})
                ownership_image = LAUNCHER.container_image
                try:
                    published_run_dir = run.finalize_and_publish(
                        snapshot=snapshot,
                        fieldnames=fieldnames,
                        ownership_image=ownership_image,
                    )
                except ArtifactFinalizationError as exc:
                    snapshot["artifact_finalization_failed"] = True
                    snapshot["artifact_staging_path"] = str(run.active_dir)
                    if run.network_audit_path.exists():
                        snapshot["internet_audit_staging_path"] = str(run.network_audit_path)
                    logger.error(
                        "Artifact finalization failed for %s attempt %s; staging retained at %s: %s",
                        pid,
                        attempt,
                        run.active_dir,
                        exc,
                    )
                    published_run_dir = None

                all_results_for_agent.append(snapshot)
                fieldnames = sorted({key for row in all_results_for_agent for key in row})
                with open(tmp_path, "w", newline="") as csvfile:
                    writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(all_results_for_agent)

                if published_run_dir is not None:
                    if case_preflight_proof is not None:
                        if (
                            case_preflight_proof.get("case_id") != pid
                            or case_preflight_proof.get("run_id") != run.artifact_id
                        ):
                            raise ValueError("pre-agent proof identity differs from the published attempt")
                        # This oracle-side proof must never enter the agent mount
                        # or the opaque artifact canonicalizer.
                        atomic_write_campaign(
                            published_run_dir / "splunk_lite_pre_agent.json",
                            (json.dumps(case_preflight_proof, sort_keys=True, indent=2) + "\n").encode(),
                        )
                    logger.info(
                        f"⏳ Attempt {attempt} of {n_attempts} for problem {pid} complete - "
                        f"Intermediate results written to {tmp_path}; artifacts published to {published_run_dir}"
                    )
                    # Normalize the agent's raw logs into an ATIF trajectory.json
                    # right after publication. Non-fatal: a conversion failure or
                    # an unsupported tool must never break the run.
                    trajectory_path = trace_postprocess.write_trajectory(published_run_dir)
                    if trajectory_path is not None:
                        logger.info(f"📝 ATIF trajectory written to {trajectory_path}")
                        try:
                            trace_store.ingest_trajectory_file(
                                trajectory_path,
                                base_dir.parent / trace_store.DEFAULT_DB_PATH,  # results/traces.db
                            )
                        except Exception as exc:  # defensive: never abort a run
                            logger.warning(f"⚠️ ATIF trajectory DB ingest failed: {exc}")
                    else:
                        logger.warning(f"⚠️ ATIF trajectory conversion skipped for {published_run_dir}")
                    if agent_to_run == "assistant_v3":
                        scorecard_path = checkpoint_assistant_attempt(base_dir, published_run_dir)
                        logger.info(f"📊 Assistant v3 campaign scorecard updated at {scorecard_path}")

                if attempt == attempts_to_run[-1] or abort_campaign_after_attempt:
                    os.replace(tmp_path, final_csv_path)
                    if abort_campaign_after_attempt:
                        logger.error(
                            f"⛔ Problem {pid} for agent {agent_to_run} stopped incomplete. "
                            f"Partial results written to {final_csv_path}"
                        )
                    else:
                        logger.info(
                            f"✅ Problem {pid} for agent {agent_to_run} complete! Results written to {final_csv_path}"
                        )

                progress.advance(task_id)

                if abort_campaign_after_attempt:
                    if conductor.results.get("cleanup_failed"):
                        abort_reason = (
                            "Benchmark cleanup failed; later attempts were not started against uncertain state."
                        )
                    elif conductor.results.get("cleanup_timed_out"):
                        abort_reason = "Benchmark cleanup did not terminate; later attempts were not started against uncertain state."
                    elif case_preflight_failed:
                        abort_reason = "Case evidence was not trustworthy; later attempts were not started."
                    else:
                        abort_reason = (
                            "A submission evaluator did not terminate safely. "
                            "The next benchmark startup will clean the remaining fault state."
                        )
                    console.log(f"⛔ Benchmark stopped: {abort_reason}")
                    progress.stop()
                    conductor.stop_k8s_proxy()
                    raise BenchmarkCampaignAborted(
                        abort_reason,
                        [{agent_to_run: all_results_for_agent}],
                    )

        progress.stop()

        # Stop K8s API proxy when all problems are done
        if not use_external_harness:
            console.log("🔓 Stopping Kubernetes API proxy...")
            conductor.stop_k8s_proxy()

        return [{agent_to_run: all_results_for_agent}]

    return asyncio.run(driver())


def _run_driver_and_shutdown(
    conductor: Conductor,
    problem_selection: Sequence[str] | None = None,
    agent_to_run: str | None = None,
    use_external_harness: bool = False,
    n_attempts: int = 1,
    agent_timeout: int = 1800,
    resume_csv: str | None = None,
    judge_backend: str = "api",
    observability_provider: ObservabilityProvider | None = None,
    inspect_before_agent: bool = False,
    assistant_prompt_arm: str = "time_only",
):
    """Run the benchmark driver, stash results, then tell the API to exit."""
    global _driver_error, _driver_results
    try:
        results = driver_loop(
            conductor,
            problem_selection=problem_selection,
            agent_to_run=agent_to_run,
            use_external_harness=use_external_harness,
            n_attempts=n_attempts,
            agent_timeout=agent_timeout,
            resume_csv=resume_csv,
            judge_backend=judge_backend,
            observability_provider=observability_provider,
            inspect_before_agent=inspect_before_agent,
            assistant_prompt_arm=assistant_prompt_arm,
        )
        _driver_results = results
    except BenchmarkCampaignAborted as exc:
        _driver_results = exc.partial_results
        _driver_error = exc
        logger.error("Benchmark campaign aborted: %s", exc)
    except BaseException as exc:
        _driver_error = exc
        logger.exception("Driver thread crashed")
    finally:
        LAUNCHER.cleanup_all()
        request_shutdown()


def main(args):
    configure_jev(args)
    init_logger()
    backend = "api" if args.use_external_harness else getattr(args, "judge_backend", "api")
    with managed_judge_backend(backend, force_build=args.force_build) as agent_image:
        return _run_benchmark(args, judge_backend=backend, agent_image=agent_image)


def resolve_agent_image(
    requested_image: str | None, *, judge_image: str | None, force_build: bool
) -> tuple[str | None, bool]:
    """Select an explicit, already-built image without changing the judge bridge."""
    if requested_image is not None:
        if not requested_image.strip():
            raise ValueError("--agent-image must name a non-empty image")
        if force_build:
            raise ValueError("--agent-image cannot be combined with --force-build")
        if judge_image is not None:
            raise ValueError("--agent-image cannot override the judge bridge image")
        return requested_image, False
    return judge_image, force_build and judge_image is None


def _run_benchmark(args, *, judge_backend: str = "api", agent_image: str | None = None):
    global _driver_error, _driver_results
    _driver_error = None
    _driver_results = []

    validate_assistant_campaign(args)
    selected_agent_image, build_agent_image = resolve_agent_image(
        getattr(args, "agent_image", None),
        judge_image=agent_image,
        force_build=args.force_build,
    )
    agent_model, judge_model = _configure_model_environment(args)
    observability_provider = create_provider(getattr(args, "observability_provider", "none"))
    observability_provider.preflight()
    internet_policy = InternetPolicy.from_mode(
        args.internet_access,
        agent_name=args.agent,
        model_id=agent_model,
        additional_allowed_endpoints=getattr(args, "allow_agent_endpoint", ()),
    )
    harden_container = args.container_hardening == "on"

    set_profile(args.profile)

    if args.noise:
        logger.info("Noise injection enabled.")
    os.environ["API_HOSTNAME"] = "0.0.0.0"
    # Host-facing ports are defaults, not constants: a run has to be able to
    # step around whatever else is already listening on the workstation.
    # Assigning unconditionally here would clobber the caller's value, which
    # then surfaces far away as a connection to the wrong service.
    os.environ.setdefault("API_PORT", "8000")
    os.environ.setdefault("MCP_SERVER_PORT", "9954")
    # Derived, never duplicated: a literal here silently disagrees with
    # MCP_SERVER_PORT, and consumers prefer the URL over the parts.
    os.environ["MCP_SERVER_URL"] = f"http://127.0.0.1:{os.environ['MCP_SERVER_PORT']}"

    logger.info(
        f"🔧 Config — agent: {args.agent}, agent_model: {agent_model}, "
        f"judge_backend: {judge_backend}, judge_model: {judge_model}, "
        f"reasoning_effort: {getattr(args, 'reasoning_effort', None) or 'agent default'}, "
        f"jev_model: {getattr(args, 'jev_model', None) or 'disabled'}, "
        f"deployment_profile: {get_profile()}, "
        f"internet_access: {internet_policy.mode.value}, "
        f"container_hardening: {args.container_hardening}, "
        f"agent_api_base: {_env_status('AGENT_API_BASE')}, judge_api_base: {_env_status('JUDGE_API_BASE')}"
    )

    # Only build/check agent container image if the agent requires it
    agent_reg = (
        get_agent(args.agent, path=Path(os.path.dirname(os.path.abspath(__file__))) / "agents.yaml")
        if args.agent
        else None
    )
    if (
        internet_policy.is_filtered
        and not args.use_external_harness
        and agent_reg
        and not agent_reg.container_isolation
    ):
        raise RuntimeError(
            f"Agent '{agent_reg.name}' does not use container isolation. "
            "Run it with --internet-access open or enable container isolation."
        )

    # Mitigation-only runs use the problem's executable oracle, not the
    # diagnosis judge. They must also work with key-free agents like autosubmit.
    if not args.use_external_harness and (args.stages is None or "diagnosis" in args.stages):
        run_judge_preflight_check()

    k8s_proxy_listen_host = (
        get_container_host_bind_address()
        if internet_policy.is_filtered and not args.use_external_harness
        else "127.0.0.1"
    )
    conductor_config = ConductorConfig(
        deploy_loki=not args.use_external_harness,
        enable_noise=args.noise,
        internet_policy=internet_policy,
        k8s_proxy_listen_host=k8s_proxy_listen_host,
        k8s_proxy_listen_port=int(os.environ.get("K8S_PROXY_PORT", "16443")),
        block_workload_creation=internet_policy.is_filtered,
        stages=tuple(args.stages) if args.stages else None,
        baseline_override_s=getattr(args, "baseline", None),
        propagation_override_s=getattr(args, "propagation", None),
    )
    LAUNCHER.set_internet_policy(conductor_config.internet_policy)
    LAUNCHER.set_container_hardening(harden_container)

    try:
        if not args.use_external_harness:
            if not agent_reg or agent_reg.container_isolation:
                LAUNCHER.enable_container_isolation(
                    force_build=build_agent_image,
                    k8s_proxy_port=conductor_config.k8s_proxy_listen_port,
                    image=selected_agent_image,
                )
            if agent_reg and LAUNCHER._container_runner is not None:
                LAUNCHER.configure_agent_capabilities(agent_reg)
                LAUNCHER._container_runner.prepare_agent_tools(agent_reg.install_script, agent_reg.agent_version)
            run_preflight_check(
                args.agent,
                container_runner=LAUNCHER._container_runner,
                install_script=agent_reg.install_script if agent_reg else None,
            )
    except BaseException:
        LAUNCHER.cleanup_all()
        raise

    conductor = Conductor(config=conductor_config)

    suite = getattr(args, "suite", None)
    problem_selection = (args.problem,) if args.problem else PROBLEM_SETS.get(suite) if isinstance(suite, str) else None
    if suite:
        assert problem_selection is not None
        logger.info(f"Running {suite}: {len(problem_selection)} problems")

    # Start the driver in the background; it will call request_shutdown() when finished
    driver_thread = threading.Thread(
        target=_run_driver_and_shutdown,
        args=(
            conductor,
            problem_selection,
            args.agent,
            args.use_external_harness,
            args.n_attempts,
            args.agent_timeout,
            args.resume,
        ),
        name="driver",
        kwargs={
            "judge_backend": judge_backend,
            "observability_provider": observability_provider,
            "inspect_before_agent": getattr(args, "inspect_before_agent", False),
            "assistant_prompt_arm": getattr(args, "assistant_prompt_arm", "time_only"),
        },
        daemon=True,
    )
    driver_thread.start()

    # Start the Conductor HTTP API in the MAIN thread (blocking)
    try:
        run_api(conductor)
    except KeyboardInterrupt:
        # If interrupted, still try to shut down cleanly
        LAUNCHER.cleanup_all()
        request_shutdown()
    finally:
        # Stop any remaining agent containers/processes
        LAUNCHER.cleanup_all()
        if not args.use_external_harness:
            conductor.stop_k8s_proxy()
        observability_provider.close()

        # Stop noise manager if it was enabled
        if args.noise:
            try:
                from sregym.generators.noise.manager import get_noise_manager

                logger.info("Stopping noise manager...")
                get_noise_manager().stop()
            except Exception as e:
                logger.error(f"⚠️ Error stopping noise manager: {e}")

        # Give driver a moment to finish setting results
        driver_thread.join(timeout=5)

    # When API shuts down, collect results from driver
    results = _driver_results

    if results:
        aggregated = {}
        for entry in results:
            for agent_name, agent_rows in entry.items():
                aggregated.setdefault(agent_name, []).extend(agent_rows)

        for agent_name, agent_results in aggregated.items():
            fieldnames = sorted({key for row in agent_results for key in row})
            out_dir = _driver_base_dir if _driver_base_dir else Path("results")
            csv_path = out_dir / f"{agent_name}_ALL_results.csv"
            with open(csv_path, "w", newline="") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(agent_results)
            if _driver_error is None:
                logger.info(f"✅ Benchmark complete! Results for {agent_name} written to {csv_path}")
            else:
                logger.error(f"⛔ Partial benchmark results for {agent_name} written to {csv_path}")
    else:
        logger.warning("⚠️ No results to write.")

    if _driver_error is not None:
        if __name__ == "__main__":
            raise SystemExit(1) from _driver_error
        raise RuntimeError("Benchmark campaign did not complete") from _driver_error

    if __name__ == "__main__":
        # separate run, use exit
        sys.exit(0)
    else:
        # function call run, return results
        return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run SREGym benchmark suite")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--problem",
        type=str,
        default=None,
        help="Run only a specific problem by its ID (e.g., 'target_port')",
    )
    selection.add_argument(
        "--suite",
        choices=tuple(PROBLEM_SETS),
        default=None,
        help="Run a named problem set (e.g., 'sregym-lite')",
    )
    # Deliberately outside the selection group: which stages run is independent
    # of which problems run, so --stages composes with both --problem and
    # --suite.
    parser.add_argument(
        "--stages",
        nargs="+",
        choices=ALL_STAGES,
        default=None,
        help=(
            "Stages to attempt, in order (default: every stage the problem supports). "
            "Use '--stages diagnosis' to skip mitigation entirely. Naming a stage the "
            "problem has no oracle for is an error rather than a silent skip."
        ),
    )
    parser.add_argument(
        "--agent",
        type=str,
        default="stratus",
        help="Agent to run (default: stratus)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="gpt-5",
        help="LiteLLM model string (e.g. anthropic/claude-sonnet-4-6-20250627, gpt-5, gemini/gemini-2.5-pro)",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default=None,
        help="Model for the LLM-as-a-judge evaluator (defaults to --model if not set)",
    )
    parser.add_argument(
        "--judge-backend",
        choices=JUDGE_BACKENDS,
        default="api",
        help="Judge access: existing API endpoint (default), or a CLI using the existing agent subscription setup",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"),
        default=None,
        help="Reasoning effort for Codex, Copilot, OpenCode, and Claude Code (uses the agent default when omitted)",
    )
    parser.add_argument(
        "--jev-model",
        default=None,
        help="Enable experimental Jev decision support for Codex (e.g. jev-latest). Requires TYPESAFE_API_KEY and --force-build.",
    )
    parser.add_argument(
        "--use-external-harness", action="store_true", help="For use in external harnesses, deploy the fault and exit."
    )
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        default=os.environ.get("SREGYM_PROFILE", "full"),
        help=(
            "Deployment profile (independent of --suite). 'full' (default) deploys the standard "
            "stack. 'svelte' additionally drops components no part of SREGym reads — astronomy-shop's "
            "bundled OpenSearch/Grafana/Jaeger, Prometheus Alertmanager/Pushgateway, the OpenEBS NDM "
            "stack — and shortens metric retention. 'svelte' changes what an agent can observe, so "
            "its results are not comparable with 'full'."
        ),
    )
    parser.add_argument(
        "--noise",
        action="store_true",
        help="Enable transient noise injection via Chaos Mesh during problem runs",
    )
    parser.add_argument(
        "--observability-provider",
        choices=("none", "splunk"),
        default="none",
        help="Optional external observability destination (default: none)",
    )
    parser.add_argument(
        "--internet-access",
        choices=("filtered", "open"),
        default="filtered",
        help=(
            "Agent internet policy. Filtered mode allows only the selected model provider and local SREGym services."
        ),
    )
    parser.add_argument(
        "--allow-agent-endpoint",
        action="append",
        default=[],
        metavar="URL",
        type=_http_endpoint,
        help=("Allow one additional HTTP(S) endpoint in filtered mode. Repeat this option for multiple endpoints."),
    )
    parser.add_argument(
        "--container-hardening",
        choices=("on", "off"),
        default="on",
        help=(
            "Agent container hardening. 'on' (default) drops every Linux capability except "
            "DAC_OVERRIDE and sets no-new-privileges. Use 'off' for agents that need to install "
            "tooling mid-run: apt-get cannot drop to the _apt user without setuid/setgid."
        ),
    )
    parser.add_argument(
        "--n-attempts",
        type=int,
        default=1,
        help="Number of attempts to run each problem (default: 1)",
    )
    parser.add_argument(
        "--force-build",
        action="store_true",
        help="Force rebuild the agent Docker image even if it already exists (use after updating dependencies or build scripts)",
    )
    parser.add_argument(
        "--agent-image",
        default=None,
        help="Use an existing agent image tag without rebuilding (for example, sregym-agent-base:latest)",
    )
    parser.add_argument(
        "--agent-timeout",
        type=int,
        default=1800,
        help="Agent timeout in seconds after deployment (default: 1800)",
    )
    parser.add_argument(
        "--inspect-before-agent",
        action="store_true",
        help=(
            "After fault injection and observability readiness, pause this single-problem run "
            "for live Kubernetes and Splunk inspection until Enter is pressed."
        ),
    )
    parser.add_argument(
        "--assistant-prompt-arm",
        choices=("time_only", "symptom_guided"),
        default="time_only",
        help="Assistant v3 prompt scope; symptom_guided adds one reviewed, non-parity alert symptom",
    )
    parser.add_argument(
        "--resume",
        type=str,
        default=None,
        help="Resume from a previous results CSV file. Problems already in the CSV will be skipped.",
    )
    parser.add_argument(
        "--baseline",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Override per-problem baseline duration (seconds of steady-state traffic before fault injection)",
    )
    parser.add_argument(
        "--propagation",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Override per-problem propagation duration (seconds to wait after fault injection, before the agent starts)",
    )
    args = parser.parse_args()

    if args.n_attempts is not None and args.n_attempts < 1:
        parser.error("--n-attempts must be a positive integer")
    if args.baseline is not None and args.baseline < 0:
        parser.error("--baseline must be a non-negative integer")
    if args.propagation is not None and args.propagation < 0:
        parser.error("--propagation must be a non-negative integer")
    if args.use_external_harness and args.suite:
        parser.error("--use-external-harness cannot be used with --suite; use --problem instead")
    if args.inspect_before_agent and args.problem is None:
        parser.error("--inspect-before-agent requires one explicit --problem")
    if args.inspect_before_agent and args.use_external_harness:
        parser.error("--inspect-before-agent cannot be combined with --use-external-harness")
    if args.inspect_before_agent and not sys.stdin.isatty():
        parser.error("--inspect-before-agent requires an interactive terminal")

    main(args)
