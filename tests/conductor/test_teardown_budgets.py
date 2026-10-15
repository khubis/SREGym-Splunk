"""Exercise the real background teardown without any cluster or telemetry calls."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from test_submission_lifecycle import _conductor

from sregym.conductor import conductor as conductor_module


def _teardown(delivery, recover):
    conductor = _conductor()
    conductor.problem_id = "example"
    conductor.results["Diagnosis"] = {"accuracy": 67}
    conductor._observability_provider = SimpleNamespace(name="splunk")
    conductor.finish_observability = delivery
    conductor.problem = SimpleNamespace(
        recover_fault=recover,
        app=SimpleNamespace(cleanup=lambda: None),
    )
    return conductor


def test_delivery_and_cleanup_each_get_their_own_bounded_budget(tmp_path):
    def bounded_work():
        time.sleep(0.12)

    conductor = _teardown(bounded_work, bounded_work)
    conductor.bind_phase_ledger(tmp_path / "phases.jsonl", attempt=1)

    async def run():
        future = conductor.finish_problem_in_background()
        try:
            await conductor.wait_for_submission_work(timeout=0.2)
        finally:
            future.result(timeout=2)

    asyncio.run(run())
    assert conductor.submission_stage == "done"
    assert conductor.results["Diagnosis"] == {"accuracy": 67}
    assert not conductor.results.get("cleanup_failed", False)


@pytest.mark.parametrize("stuck_phase", ["delivery", "cleanup"])
def test_either_stuck_phase_remains_bounded_and_abandoned_work_cannot_continue(stuck_phase):
    release = threading.Event()
    entered = threading.Event()
    cleaned = threading.Event()

    def blocked():
        entered.set()
        release.wait(2)

    conductor = _teardown(
        blocked if stuck_phase == "delivery" else lambda: None,
        blocked if stuck_phase == "cleanup" else lambda: None,
    )
    conductor.problem.app.cleanup = cleaned.set

    async def run():
        future = conductor.finish_problem_in_background()
        try:
            assert entered.wait(1)
            with pytest.raises(TimeoutError):
                await conductor.wait_for_submission_work(timeout=0.04)
            conductor.abandon_submission_work()
        finally:
            release.set()
            future.result(timeout=2)

    asyncio.run(run())
    assert not cleaned.is_set()
    assert conductor.submission_stage == "aborted"
    assert conductor.results["Diagnosis"] == {"accuracy": 67}


def test_late_waiter_cannot_restart_a_cleanup_budget():
    release = threading.Event()
    entered = threading.Event()

    def blocked():
        entered.set()
        release.wait(2)

    conductor = _teardown(lambda: None, blocked)

    async def run():
        future = conductor.finish_problem_in_background()
        try:
            assert entered.wait(1)
            await asyncio.sleep(0.08)
            with pytest.raises(TimeoutError):
                await conductor.wait_for_submission_work(timeout=0.04)
            conductor.abandon_submission_work()
        finally:
            release.set()
            future.result(timeout=2)

    asyncio.run(run())
    assert conductor.submission_stage == "aborted"


def test_separate_budget_wait_propagates_the_original_worker_failure():
    def interrupt():
        raise SystemExit("worker stopped")

    conductor = _teardown(interrupt, lambda: None)

    async def run():
        conductor.finish_problem_in_background()
        with pytest.raises(RuntimeError, match="Cleanup terminated with SystemExit"):
            await conductor.wait_for_submission_work(timeout=1)

    asyncio.run(run())


def test_failure_completed_during_a_phase_poll_is_not_lost():
    def interrupt():
        time.sleep(0.02)
        raise SystemExit("worker stopped during poll")

    conductor = _teardown(interrupt, lambda: None)

    async def run():
        conductor.finish_problem_in_background()
        with pytest.raises(RuntimeError, match="Cleanup terminated with SystemExit"):
            await conductor.wait_for_submission_work(timeout=1)

    asyncio.run(run())


def test_first_wait_during_cleanup_cannot_forget_an_expired_delivery_phase():
    entered_cleanup = threading.Event()
    release = threading.Event()

    def delayed_delivery():
        time.sleep(0.08)

    def cleanup():
        entered_cleanup.set()
        release.wait(2)

    conductor = _teardown(delayed_delivery, cleanup)

    async def run():
        future = conductor.finish_problem_in_background()
        timer = threading.Timer(0.01, release.set)
        try:
            assert entered_cleanup.wait(1)
            timer.start()
            with pytest.raises(TimeoutError):
                await conductor.wait_for_submission_work(timeout=0.04)
        finally:
            release.set()
            timer.join(timeout=1)
            future.result(timeout=2)

    asyncio.run(run())


@pytest.mark.parametrize("slow_phase", ["delivery", "cleanup"])
def test_late_first_wait_rejects_an_expired_phase_even_after_worker_completion(slow_phase):
    def slow():
        time.sleep(0.08)

    conductor = _teardown(
        slow if slow_phase == "delivery" else lambda: None, slow if slow_phase == "cleanup" else lambda: None
    )

    async def run():
        future = conductor.finish_problem_in_background()
        future.result(timeout=2)
        with pytest.raises(TimeoutError):
            await conductor.wait_for_submission_work(timeout=0.04)

    asyncio.run(run())


def test_late_timeout_retains_a_completed_worker_failure_and_the_expired_phase():
    def slow_delivery():
        time.sleep(0.08)

    def failed_cleanup():
        raise SystemExit("cleanup worker failed after slow delivery")

    conductor = _teardown(slow_delivery, failed_cleanup)

    async def run():
        future = conductor.finish_problem_in_background()
        assert isinstance(future.exception(timeout=2), RuntimeError)
        with pytest.raises(RuntimeError, match="Cleanup terminated with SystemExit"):
            await conductor.wait_for_submission_work(timeout=0.04)
        assert conductor.results["teardown_timeout_phase"] == "delivery"
        assert conductor._submit_future is None

    asyncio.run(run())


@pytest.mark.parametrize("failed_phase", ["delivery", "cleanup"])
def test_failed_final_phase_retains_its_expired_budget_and_original_error(failed_phase):
    def delayed_failure():
        time.sleep(0.08)
        raise SystemExit("worker failed after its deadline")

    conductor = _teardown(
        delayed_failure if failed_phase == "delivery" else lambda: None,
        delayed_failure if failed_phase == "cleanup" else lambda: None,
    )

    async def run():
        future = conductor.finish_problem_in_background()
        assert isinstance(future.exception(timeout=2), RuntimeError)
        with pytest.raises(RuntimeError, match="Cleanup terminated with SystemExit"):
            await conductor.wait_for_submission_work(timeout=0.04)
        assert conductor.results["teardown_timed_out"] is True
        assert conductor.results["teardown_timeout_phase"] == failed_phase
        assert conductor._submit_future is None

    asyncio.run(run())


@pytest.mark.parametrize("worker", ["safety_cleanup", "final_submission"])
def test_waiter_missing_cleanup_transition_does_not_apply_a_stale_delivery_deadline(monkeypatch, worker):
    # Replay 134 seconds before cleanup plus 168 seconds of cleanup without
    # waiting in real time or making any infrastructure calls.
    clock = [0.0]
    delivery_entered = threading.Event()
    release_delivery = threading.Event()
    cleanup_finished = threading.Event()
    release_worker = threading.Event()

    def delivery():
        delivery_entered.set()
        release_delivery.wait(2)
        clock[0] = 134.0

    def recover():
        clock[0] = 302.0

    conductor = _teardown(delivery, recover)
    original_info = conductor.logger.info

    def info(message, *args, **kwargs):
        if message == "[STAGE] Teardown complete":
            cleanup_finished.set()
            release_worker.wait(2)
        original_info(message, *args, **kwargs)

    monkeypatch.setattr(conductor_module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    monkeypatch.setattr(conductor.logger, "info", info)
    real_sleep = asyncio.sleep
    polls = 0

    async def controlled_poll(_delay):
        nonlocal polls
        polls += 1
        if polls == 1:
            release_delivery.set()
            assert cleanup_finished.wait(1)
        else:
            release_worker.set()
            await real_sleep(0.001)

    monkeypatch.setattr(asyncio, "sleep", controlled_poll)

    async def run():
        if worker == "final_submission":
            conductor.stage_sequence = conductor.stage_sequence[:1]
            await conductor.submit("diagnosis", expected_stage="diagnosis")
            future = conductor._submit_future
        else:
            future = conductor.finish_problem_in_background()
        try:
            assert delivery_entered.wait(1)
            await conductor.wait_for_submission_work(timeout=300)
        finally:
            release_delivery.set()
            release_worker.set()
            future.result(timeout=2)

    asyncio.run(run())
    assert conductor.submission_stage == "done"
    assert conductor._submit_future is None
    assert not conductor.results.get("teardown_timed_out", False)


@pytest.mark.parametrize("failed_phase", ["delivery", "cleanup"])
def test_final_submission_worker_retains_failed_phase_timeout_and_evaluator_error(failed_phase):
    def delayed_failure():
        time.sleep(0.08)
        raise SystemExit("final-stage teardown failed after its deadline")

    conductor = _teardown(
        delayed_failure if failed_phase == "delivery" else lambda: None,
        delayed_failure if failed_phase == "cleanup" else lambda: None,
    )
    conductor.stage_sequence = conductor.stage_sequence[:1]

    async def run():
        await conductor.submit("diagnosis", expected_stage="diagnosis")
        future = conductor._submit_future
        assert isinstance(future.exception(timeout=2), RuntimeError)
        with pytest.raises(RuntimeError, match="Evaluator terminated with SystemExit"):
            await conductor.wait_for_submission_work(timeout=0.04)
        assert conductor.results["teardown_timed_out"] is True
        assert conductor.results["teardown_timeout_phase"] == failed_phase
        assert conductor._submit_future is None

    asyncio.run(run())


def test_worker_completion_between_snapshot_and_wait_preserves_error_and_timeout(monkeypatch):
    clock = [0.0]
    delivery_entered = threading.Event()
    release_delivery = threading.Event()
    completed = threading.Event()

    def delivery():
        delivery_entered.set()
        release_delivery.wait(2)
        clock[0] = 2.0
        raise SystemExit("delivery failed after its deadline")

    conductor = _teardown(delivery, lambda: None)
    monkeypatch.setattr(conductor_module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))
    original_lock = conductor._submission_lock
    waiter_thread = threading.current_thread()
    handoff = [False]

    class HandoffLock:
        def __enter__(self):
            return original_lock.__enter__()

        def __exit__(self, *args):
            original_lock.__exit__(*args)
            if threading.current_thread() is waiter_thread and handoff[0]:
                handoff[0] = False
                release_delivery.set()
                assert completed.wait(1)

    async def run():
        future = conductor.finish_problem_in_background()
        future.add_done_callback(lambda _: completed.set())
        try:
            assert delivery_entered.wait(1)
            conductor._submission_lock = HandoffLock()
            handoff[0] = True
            with pytest.raises(RuntimeError, match="Cleanup terminated with SystemExit"):
                await conductor.wait_for_submission_work(timeout=1)
            assert conductor.results["teardown_timed_out"] is True
            assert conductor.results["teardown_timeout_phase"] == "delivery"
            assert conductor._submit_future is None
        finally:
            release_delivery.set()
            assert isinstance(future.exception(timeout=2), RuntimeError)

    asyncio.run(run())
