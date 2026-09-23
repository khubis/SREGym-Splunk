"""The conductor's phase instrumentation.

Two properties matter beyond the ledger's own tests: an unbound ledger must not
change behaviour anywhere (cli.py, the external harness and most tests never
bind one), and a phase that raises must still leave a written-down end time --
that is the whole reason the ledger exists.
"""

import asyncio
import threading
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from sregym.conductor import conductor as conductor_mod
from sregym.conductor.conductor import Conductor
from sregym.observability.base import (
    ApplicationScope,
    AttemptContext,
    DeliveryReport,
    ExternalOtlpExport,
    NullObservabilityProvider,
    ProviderError,
    ReadinessReport,
    SignalReadiness,
)
from sregym.phases import read_ledger, summarize


@pytest.fixture
def bare():
    """A Conductor with no cluster-touching __init__ and no ledger bound."""
    c = Conductor.__new__(Conductor)
    c.phases = None
    c.problem_id = "demo"
    c.logger = conductor_mod.logging.getLogger("test.phase_wiring")
    return c


def test_unbound_ledger_is_a_transparent_no_op(bare):
    with bare._phase("deploy"):
        pass
    bare._mark("stage:diagnosis", "start")  # must not raise either


def test_unbound_ledger_does_not_suppress_exceptions(bare):
    """A no-op context manager that swallowed would silently change control flow."""
    with pytest.raises(RuntimeError), bare._phase("deploy"):
        raise RuntimeError("boom")


def test_binding_stamps_problem_id_into_every_record(bare, tmp_path):
    bare.bind_phase_ledger(tmp_path / "phases.jsonl", attempt=2, agent="stratus")
    with bare._phase("deploy"):
        pass
    records = read_ledger(tmp_path / "phases.jsonl")
    assert records, "expected boundary records"
    for rec in records:
        assert rec["problem_id"] == "demo"
        assert rec["attempt"] == 2
        assert rec["agent"] == "stratus"


def test_a_failing_phase_is_recorded_then_reraised(bare, tmp_path):
    bare.bind_phase_ledger(tmp_path / "phases.jsonl")
    with pytest.raises(ValueError), bare._phase("deploy"):
        raise ValueError("deploy blew up")

    summary = summarize(read_ledger(tmp_path / "phases.jsonl"))
    assert summary["deploy"]["outcome"] == "error"
    assert "deploy blew up" in summary["deploy"]["error"]
    assert summary["deploy"]["end_ts"], "a failed phase still needs an end time"


def test_stage_boundaries_come_from_two_separate_call_sites(bare, tmp_path):
    """The agent stage opens in _advance_to_next_stage and closes at evaluation."""
    bare.bind_phase_ledger(tmp_path / "phases.jsonl")
    bare._mark("stage:diagnosis", "start")
    bare._mark("stage:diagnosis", "end", outcome="submitted")

    summary = summarize(read_ledger(tmp_path / "phases.jsonl"))
    assert summary["stage:diagnosis"]["outcome"] == "submitted"
    assert summary["stage:diagnosis"]["start_ts"]
    assert summary["stage:diagnosis"]["end_ts"]


def test_rebinding_per_attempt_keeps_ledgers_separate(bare, tmp_path):
    """Each attempt gets its own file rather than appending to a shared one."""
    for attempt in (1, 2):
        bare.bind_phase_ledger(tmp_path / f"phases_attempt{attempt}.jsonl", attempt=attempt)
        with bare._phase("deploy"):
            pass
    for attempt in (1, 2):
        records = read_ledger(tmp_path / f"phases_attempt{attempt}.jsonl")
        assert {r["attempt"] for r in records} == {attempt}


def test_a_stage_open_at_teardown_gets_an_end_record(bare, tmp_path):
    """An agent that exits without submitting still closes its stage.

    Otherwise the reader has to infer the end from the next phase's start,
    which is the synthesis the ledger exists to avoid.
    """
    bare.bind_phase_ledger(tmp_path / "phases.jsonl")
    bare.stage_sequence = [{"name": "diagnosis"}, {"name": "mitigation"}]
    bare._mark("stage:diagnosis", "start")
    # What _finish_problem does once it sees the stage still open.
    open_stage = "diagnosis"
    if open_stage in {s["name"] for s in bare.stage_sequence}:
        bare._mark(f"stage:{open_stage}", "end", outcome="no_submission")

    summary = summarize(read_ledger(tmp_path / "phases.jsonl"))
    assert summary["stage:diagnosis"]["outcome"] == "no_submission"
    assert summary["stage:diagnosis"]["end_ts"]


def test_a_cleanly_closed_stage_is_not_closed_twice(bare, tmp_path):
    """submission_stage still names the stage after it was evaluated.

    Closing on that alone wrote a second end and produced a phantom
    `stage:diagnosis#2` entry in the results.
    """
    bare.bind_phase_ledger(tmp_path / "phases.jsonl")
    bare.stage_sequence = [{"name": "diagnosis"}]
    bare._mark("stage:diagnosis", "start")
    bare._mark("stage:diagnosis", "end", outcome="submitted")

    assert not bare._phase_is_open("stage:diagnosis")
    # The teardown guard must now decline to close it again.
    if bare._phase_is_open("stage:diagnosis"):
        bare._mark("stage:diagnosis", "end", outcome="no_submission")

    summary = summarize(read_ledger(tmp_path / "phases.jsonl"))
    assert list(summary) == ["stage:diagnosis"], "no phantom #2 entry"
    assert summary["stage:diagnosis"]["outcome"] == "submitted"


def test_an_abandoned_stage_is_still_open_and_gets_closed(bare, tmp_path):
    bare.bind_phase_ledger(tmp_path / "phases.jsonl")
    bare.stage_sequence = [{"name": "mitigation"}]
    bare._mark("stage:mitigation", "start")

    assert bare._phase_is_open("stage:mitigation")
    bare._mark("stage:mitigation", "end", outcome="no_submission")

    summary = summarize(read_ledger(tmp_path / "phases.jsonl"))
    assert summary["stage:mitigation"]["outcome"] == "no_submission"
    assert summary["stage:mitigation"]["duration_s"] is not None


def test_phase_is_open_is_false_without_a_ledger(bare):
    bare.phases = None
    assert bare._phase_is_open("stage:diagnosis") is False


def _ready(run_id):
    checked_at = datetime.now(UTC)
    return ReadinessReport(
        run_id=run_id,
        signals=tuple(
            SignalReadiness(signal=name, ready=True, checked_at=checked_at, evidence={})
            for name in ("metrics", "traces", "logs", "kubernetes_events")
        ),
        ready=True,
    )


class RecordingProvider:
    name = "splunk"

    def __init__(self, events):
        self.events = events

    def preflight(self):
        self.events.append("preflight")

    def prepare_attempt(self, context):
        self.events.append("prepare")
        return ExternalOtlpExport(
            endpoint="splunk-otel-collector-agent.splunk-monitoring.svc:4317",
            run_id=context.run_id,
            resource_attributes={"k8s.cluster.name": context.run_id},
        )

    def wait_until_queryable(self, context, scope):
        self.events.append(("ready", scope))
        return _ready(context.run_id)

    def finish_attempt(self, context, scope):
        self.events.append(("finish", scope))
        opening = _ready(context.run_id)
        return DeliveryReport(
            run_id=context.run_id,
            opening=opening,
            closing=opening,
            first_visible_lag_ms=dict.fromkeys(("metrics", "traces", "logs", "kubernetes_events"), 1.0),
            sent_delta=dict.fromkeys(("metrics", "traces", "logs", "kubernetes_events"), 1),
            send_failed_delta=dict.fromkeys(("metrics", "traces", "logs", "kubernetes_events"), 0),
            enqueue_failed_delta=dict.fromkeys(("metrics", "traces", "logs", "kubernetes_events"), 0),
            queue_high_water=dict.fromkeys(("metrics", "traces", "logs", "kubernetes_events"), 1),
            queue_final_size=dict.fromkeys(("metrics", "traces", "logs", "kubernetes_events"), 0),
            drained=True,
            valid=True,
        )

    def close(self):
        self.events.append("close")


def test_provider_prepares_before_telemetry_deploy_and_readiness_precedes_launch(bare):
    events = []
    provider = RecordingProvider(events)
    context = AttemptContext(
        run_id="anon_0123456789abcdef0123456789abcdef",
        profile="full",
        comparable=True,
        attempt_started_at=datetime.now(UTC),
    )
    bare.app = SimpleNamespace(app_name="Astronomy Shop", namespace="otel-demo", namespaces=["otel-demo"])
    bare.otel_collector = SimpleNamespace(deploy=lambda export=None: events.append(("deploy", export)))
    bare.bind_observability_attempt(provider, context)

    bare.prepare_observability()
    bare.otel_collector.deploy(bare.observability_export)
    report = bare.wait_for_observability()
    events.append("launch")

    assert events[0] == "prepare"
    assert events[1][0] == "deploy"
    assert events[1][1].run_id == context.run_id
    assert events[2] == ("ready", ApplicationScope("Astronomy Shop", ("otel-demo",)))
    assert events[3] == "launch"
    assert report.ready is True


def test_conductor_initializes_observability_state(monkeypatch):
    for name in (
        "ProblemRegistry",
        "KubeCtl",
        "Prometheus",
        "Jaeger",
        "OtelCollector",
        "Loki",
        "AppRegistry",
        "KubernetesAPIProxy",
        "ClusterEgressBoundary",
    ):
        monkeypatch.setattr(conductor_mod, name, Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr(conductor_mod, "MCPServer", Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr(conductor_mod, "KhaosController", Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr(conductor_mod, "DmFlakeyManager", Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr(conductor_mod, "ClusterStateManager", Mock(return_value=SimpleNamespace()))

    conductor = Conductor()

    assert conductor._observability_provider is None
    assert conductor._observability_context is None
    assert conductor.observability_export is None
    assert conductor.observability_readiness is None
    assert conductor.observability_delivery is None


def test_application_scope_requires_a_deployed_application(bare):
    bare.app = None
    with pytest.raises(RuntimeError, match="before application deployment"):
        bare._application_scope()


def test_delivery_audit_runs_once_before_destructive_cleanup(bare):
    events = []
    provider = RecordingProvider(events)
    context = AttemptContext(
        run_id="anon_0123456789abcdef0123456789abcdef",
        profile="full",
        comparable=True,
        attempt_started_at=datetime.now(UTC),
    )
    bare.app = SimpleNamespace(app_name="Astronomy Shop", namespace="otel-demo")
    bare.bind_observability_attempt(provider, context)

    report = bare.finish_observability()
    events.append("teardown")
    assert bare.finish_observability() is report

    assert events == [
        ("finish", ApplicationScope("Astronomy Shop", ("otel-demo",))),
        "teardown",
    ]


def test_null_provider_binding_preserves_disabled_collector_behavior(bare):
    context = AttemptContext(
        run_id="anon_0123456789abcdef0123456789abcdef",
        profile="full",
        comparable=True,
        attempt_started_at=datetime.now(UTC),
    )
    bare.app = SimpleNamespace(app_name="Astronomy Shop", namespace="otel-demo")
    bare.bind_observability_attempt(NullObservabilityProvider(), context)

    assert bare.prepare_observability() is None
    assert bare.observability_export is None
    assert bare.wait_for_observability() is None
    assert bare.finish_observability() is None


def _finish_ready_bare(bare, events):
    bare._submission_lock = threading.RLock()
    bare._submission_generation = 1
    bare._aborted_submission_generations = set()
    bare.submission_stage = "diagnosis"
    bare.stage_sequence = []
    bare._accepting_submissions = True
    bare._attempt_closed = False
    bare.results = {}
    bare._cleanup_sync = lambda generation: events.append("cleanup")


def test_finish_problem_audits_delivery_before_cleanup(bare):
    events = []
    _finish_ready_bare(bare, events)
    provider = RecordingProvider(events)
    context = AttemptContext(
        run_id="anon_0123456789abcdef0123456789abcdef",
        profile="full",
        comparable=True,
        attempt_started_at=datetime.now(UTC),
    )
    bare.app = SimpleNamespace(app_name="app", namespace="namespace")
    bare.bind_observability_attempt(provider, context)

    bare._finish_problem()

    assert events[0][0] == "finish"
    assert events[1] == "cleanup"


def test_finish_problem_cleans_up_after_classified_delivery_failure(bare):
    events = []
    _finish_ready_bare(bare, events)
    provider = RecordingProvider(events)
    provider.finish_attempt = Mock(side_effect=ProviderError("cleanup", "delivery unavailable"))
    context = AttemptContext(
        run_id="anon_0123456789abcdef0123456789abcdef",
        profile="full",
        comparable=True,
        attempt_started_at=datetime.now(UTC),
    )
    bare.app = SimpleNamespace(app_name="app", namespace="namespace")
    bare.bind_observability_attempt(provider, context)

    bare._finish_problem()

    assert events == ["cleanup"]
    assert bare.results["infrastructure_invalid"] is True
    assert bare.results["included_in_diagnosis_pass_rate"] is False
    assert bare.results["observability_error"] == "cleanup"


def test_finish_problem_marks_an_invalid_delivery_before_cleanup(bare):
    events = []
    _finish_ready_bare(bare, events)
    provider = RecordingProvider(events)
    context = AttemptContext(
        run_id="anon_0123456789abcdef0123456789abcdef",
        profile="full",
        comparable=True,
        attempt_started_at=datetime.now(UTC),
    )
    bare.app = SimpleNamespace(app_name="app", namespace="namespace")
    valid = provider.finish_attempt(context, ApplicationScope("app", ("namespace",)))
    events.clear()
    provider.finish_attempt = Mock(return_value=replace(valid, valid=False))
    bare.bind_observability_attempt(provider, context)

    bare._finish_problem()

    assert events == ["cleanup"]
    assert bare.results["infrastructure_invalid"] is True
    assert bare.results["included_in_diagnosis_pass_rate"] is False


def test_finish_problem_cleans_up_after_unexpected_delivery_failure(bare):
    events = []
    _finish_ready_bare(bare, events)
    provider = RecordingProvider(events)
    provider.finish_attempt = Mock(side_effect=RuntimeError("unexpected"))
    context = AttemptContext(
        run_id="anon_0123456789abcdef0123456789abcdef",
        profile="full",
        comparable=True,
        attempt_started_at=datetime.now(UTC),
    )
    bare.app = SimpleNamespace(app_name="app", namespace="namespace")
    bare.bind_observability_attempt(provider, context)

    bare._finish_problem()

    assert events == ["cleanup"]
    assert bare.results["infrastructure_invalid"] is True
    assert bare.results["included_in_diagnosis_pass_rate"] is False
    assert bare.results["observability_error"] == "cleanup"


def test_start_problem_places_provider_prepare_before_baseline_and_readiness_after_fault(
    bare,
    monkeypatch: pytest.MonkeyPatch,
):
    events = []
    app = SimpleNamespace(app_name="app", namespace="namespace", description="description")
    problem = SimpleNamespace(
        app=app,
        baseline_duration_s=1,
        requires_khaos=lambda: False,
    )
    bare.problem_id = "problem"
    bare.problems = SimpleNamespace(get_problem_instance=lambda problem_id: problem)
    bare.kubectl = SimpleNamespace(is_emulated_cluster=lambda: False)
    bare._submission_lock = threading.RLock()
    bare._submission_generation = 0
    bare._aborted_submission_generations = set()
    bare._pending_submission_stages = {}
    bare._submit_future = None
    bare.config = SimpleNamespace(baseline_override_s=None, enable_noise=False)
    bare.close_submissions = lambda: False
    bare.dependency_check = lambda commands: None
    bare.fix_kubernetes = lambda: events.append("fix")
    bare.get_problem_stages = lambda: None
    bare._build_stage_sequence = lambda: None
    bare.undeploy_app = lambda: events.append("undeploy")
    bare.deploy_app = lambda: events.append("deploy")

    def advance(start_index):
        events.append("fault")
        bare.submission_stage = "diagnosis"

    bare._advance_to_next_stage = advance
    monkeypatch.setattr(conductor_mod, "DetectionOracle", lambda selected_problem: object())

    async def baseline_sleep(seconds):
        events.append("baseline")

    monkeypatch.setattr(conductor_mod.asyncio, "sleep", baseline_sleep)
    provider = RecordingProvider(events)
    context = AttemptContext(
        run_id="anon_0123456789abcdef0123456789abcdef",
        profile="full",
        comparable=True,
        attempt_started_at=datetime.now(UTC),
    )
    bare.bind_observability_attempt(provider, context)

    assert asyncio.run(bare.start_problem()).value == "success"

    assert events == [
        "fix",
        "undeploy",
        "prepare",
        "deploy",
        "baseline",
        "fault",
        ("ready", ApplicationScope("app", ("namespace",))),
    ]
