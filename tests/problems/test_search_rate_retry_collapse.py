import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from kubernetes import client

from sregym.conductor.problems.search_rate_retry_collapse import SearchRateRetryCollapse
from sregym.generators.workload.hotel_search import HotelSearchMetrics, HotelSearchWorkload, KubectlPortForward


class _AppsV1:
    def __init__(self):
        self.replaced = None

    def replace_namespaced_deployment(self, name, namespace, body):
        self.replaced = (name, namespace, body)


def test_problem_disables_the_unrelated_default_application_workload():
    assert SearchRateRetryCollapse.run_default_workload is False


def test_trigger_is_long_enough_for_a_fresh_application_deployment():
    assert SearchRateRetryCollapse.trigger_seconds == 10.0


def test_vulnerable_policy_is_part_of_the_initial_deployment():
    overrides = SearchRateRetryCollapse._vulnerable_deployment_env()

    assert overrides["rate"]["hotel-reserv-rate"]["RATE_BACKEND_QPS_LIMIT"] == "20"
    assert overrides["search"]["hotel-reserv-search"]["RATE_RPC_MAX_ATTEMPTS"] == "3"


def test_rollout_wait_ignores_unrelated_agent_pods():
    problem = SearchRateRetryCollapse.__new__(SearchRateRetryCollapse)
    problem.namespace = "hotel-reservation"
    problem.kubectl = SimpleNamespace(
        exec_command_checked=Mock(),
        wait_for_ready=Mock(),
    )

    problem._wait_for_rollouts()

    problem.kubectl.wait_for_ready.assert_called_once_with(
        "hotel-reservation",
        service_names=["rate", "search"],
    )


def test_replace_container_env_preserves_unrelated_duplicate_entries():
    container = SimpleNamespace(
        name="hotel-reserv-search",
        env=[
            client.V1EnvVar(name="DUPLICATE", value="first"),
            client.V1EnvVar(name="RATE_RPC_MAX_ATTEMPTS", value="1"),
            client.V1EnvVar(name="DUPLICATE", value="second"),
        ],
    )
    deployment = SimpleNamespace(
        spec=SimpleNamespace(template=SimpleNamespace(spec=SimpleNamespace(containers=[container])))
    )
    apps_v1 = _AppsV1()
    problem = SearchRateRetryCollapse.__new__(SearchRateRetryCollapse)
    problem.namespace = "hotel-reservation"
    problem.kubectl = SimpleNamespace(
        get_deployment=lambda *_: deployment,
        apps_v1_api=apps_v1,
    )

    problem._replace_container_env(
        "search",
        "hotel-reserv-search",
        {"RATE_RPC_MAX_ATTEMPTS": "3"},
    )

    replaced = apps_v1.replaced[2]
    env = replaced.spec.template.spec.containers[0].env
    assert [(item.name, item.value) for item in env] == [
        ("DUPLICATE", "first"),
        ("DUPLICATE", "second"),
        ("RATE_RPC_MAX_ATTEMPTS", "3"),
    ]


def test_repeated_injection_fails_before_mutating_cluster():
    problem = SearchRateRetryCollapse.__new__(SearchRateRetryCollapse)
    problem.fault_injected = False
    problem._injection_attempted = True

    with pytest.raises(RuntimeError, match="already active"):
        problem.inject_fault()


def test_injection_only_runs_the_protected_workload_and_trigger():
    problem = SearchRateRetryCollapse.__new__(SearchRateRetryCollapse)
    problem.fault_injected = False
    problem._injection_attempted = False
    problem.workload = SimpleNamespace(start=Mock(), stop=Mock())
    problem._establish_healthy_vulnerable_baseline = Mock()
    problem._apply_trigger_and_verify_sustaining_loop = Mock()

    problem.inject_fault()

    problem.workload.start.assert_called_once_with()
    problem._establish_healthy_vulnerable_baseline.assert_called_once_with()
    problem._apply_trigger_and_verify_sustaining_loop.assert_called_once_with()
    assert problem.fault_injected is True


def test_recovery_applies_an_intentional_safe_policy():
    problem = SearchRateRetryCollapse.__new__(SearchRateRetryCollapse)
    problem.fault_injected = True
    problem._apply_mitigated_policy = Mock()
    problem._wait_for_rollouts = Mock()
    problem.workload = SimpleNamespace(stop=Mock())

    problem.recover_fault()

    problem._apply_mitigated_policy.assert_called_once_with()
    problem._wait_for_rollouts.assert_called_once_with()
    problem.workload.stop.assert_called_once_with()
    assert problem.fault_injected is False


def test_stopped_port_forward_rejects_new_requests(monkeypatch):
    forward = KubectlPortForward("hotel-reservation", "frontend", 5000)
    monkeypatch.setattr(
        "sregym.generators.workload.hotel_search.subprocess.Popen",
        lambda *_args, **_kwargs: pytest.fail("closed port-forward was restarted"),
    )

    forward.stop()

    with pytest.raises(RuntimeError, match="stopping"):
        forward.start(timeout=0.01)


def test_port_forward_stop_interrupts_a_pending_start(monkeypatch):
    forward = KubectlPortForward("hotel-reservation", "frontend", 5000)
    spawned = threading.Event()
    outcomes = []

    class FakeProcess:
        stderr = None

        def __init__(self):
            self.stopped = False

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True

        def wait(self, timeout):
            return 0

        def kill(self):
            self.stopped = True

    def fake_popen(*_args, **_kwargs):
        spawned.set()
        return FakeProcess()

    monkeypatch.setattr("sregym.generators.workload.hotel_search.subprocess.Popen", fake_popen)
    monkeypatch.setattr(forward, "_healthy", lambda: False)
    starter = threading.Thread(target=lambda: outcomes.append(_start_outcome(forward)), daemon=True)
    starter.start()
    assert spawned.wait(timeout=1)
    stopper = threading.Thread(target=forward.stop, daemon=True)
    stopper.start()
    stopper.join(timeout=0.4)
    if stopper.is_alive():
        starter.join(timeout=2)
        stopper.join(timeout=2)
    assert not stopper.is_alive(), "shutdown must not wait through the full port-forward start timeout"
    starter.join(timeout=1)
    assert len(outcomes) == 1 and outcomes[0].endswith("is stopping")


def _start_outcome(forward):
    try:
        forward.start(timeout=1)
    except RuntimeError as error:
        return str(error)
    except TimeoutError:
        return "timeout"
    return "started"


def test_stopped_hotel_workload_does_not_launch_new_requests():
    workload = HotelSearchWorkload("hotel-reservation")
    workload._stop.set()
    workload.frontend = SimpleNamespace(start=Mock(side_effect=AssertionError("request started after stop")))

    workload._request()

    workload.frontend.start.assert_not_called()


def test_port_forward_can_be_explicitly_reopened(monkeypatch):
    forward = KubectlPortForward("hotel-reservation", "frontend", 5000)
    process = SimpleNamespace(poll=lambda: None, terminate=Mock(), wait=Mock(), stderr=None)
    monkeypatch.setattr(
        "sregym.generators.workload.hotel_search.subprocess.Popen",
        lambda *_args, **_kwargs: process,
    )
    monkeypatch.setattr(forward, "_healthy", lambda: forward.process is not None)

    forward.stop()
    forward.reopen()

    assert forward.start(timeout=0.1) > 0
    forward.stop()


def test_port_forward_reset_is_recoverable_but_not_after_shutdown():
    forward = KubectlPortForward("hotel-reservation", "frontend", 5000)
    process = SimpleNamespace(poll=lambda: None, terminate=Mock(), wait=Mock(), stderr=None)
    forward.process = process

    forward.reset()

    process.terminate.assert_called_once_with()
    assert forward.process is None
    forward.stop()
    with pytest.raises(RuntimeError, match="stopping"):
        forward.reset()


def test_port_forward_does_not_return_a_tunnel_closed_during_health_check(monkeypatch):
    forward = KubectlPortForward("hotel-reservation", "frontend", 5000)
    forward.local_port = 12345

    def close_during_check():
        forward._stopping.set()
        return True

    monkeypatch.setattr(forward, "_healthy", close_during_check)

    with pytest.raises(RuntimeError, match="stopping"):
        forward.start()


def test_metrics_read_resets_failed_tunnel_without_permanently_stopping_it(monkeypatch):
    metrics = HotelSearchMetrics("hotel-reservation")
    forward = SimpleNamespace(start=Mock(return_value=12345), reset=Mock(), stop=Mock())
    metrics.forwards["search"] = forward
    response = SimpleNamespace(text="hotel_search_requests_total 3", raise_for_status=Mock())
    getter = Mock(side_effect=[requests.Timeout("stale tunnel"), response])
    monkeypatch.setattr("sregym.generators.workload.hotel_search.requests.get", getter)

    assert metrics.read("search") == {"hotel_search_requests_total": 3.0}
    assert forward.start.call_count == 2
    forward.reset.assert_called_once_with()
    forward.stop.assert_not_called()


def test_request_counts_shutdown_tunnel_error_as_failed_request():
    workload = HotelSearchWorkload("hotel-reservation")
    workload.frontend = SimpleNamespace(start=Mock(side_effect=RuntimeError("port-forward is stopping")))

    workload._request()

    assert len(workload._events) == 1
    assert workload._events[0][1] is False


def test_workload_restart_explicitly_reopens_its_tunnels():
    workload = HotelSearchWorkload("hotel-reservation")
    workload.frontend = SimpleNamespace(reopen=Mock(), start=Mock(), stop=Mock())
    workload.metrics = SimpleNamespace(reopen=Mock(), close=Mock())
    workload._schedule = Mock()

    workload.start()
    workload.stop()
    workload.start()
    workload.stop()

    assert workload.frontend.reopen.call_count == 2
    assert workload.metrics.reopen.call_count == 2
