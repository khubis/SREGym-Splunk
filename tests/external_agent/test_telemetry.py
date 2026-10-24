import unittest
from unittest.mock import Mock

from clients.external_agent.driver import Hold
from clients.external_agent.telemetry import TelemetryPending, metric_identity, wait_for_telemetry


class TelemetryWaitTests(unittest.TestCase):
    def test_identity_rejects_each_missing_or_stale_form(self):
        cases = [
            ([], {"live"}, "metric_samples_empty"),
            ([{"metric": {}}], {"live"}, "metric_uid_label_missing"),
            ([{"metric": {"uid": "old"}}], {"live"}, "metric_uid_outside_live_cohort"),
            ([{"metric": {"uid": "live"}}], set(), "live_pod_cohort_empty"),
        ]
        for metrics, live, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(TelemetryPending) as c:
                metric_identity(metrics, live)
            self.assertEqual(c.exception.evidence["reason"], reason)

    def test_same_predicate_accepts_a_current_subset(self):
        self.assertEqual(metric_identity([{"metric": {"uid": "live"}}], {"live", "also-live"}), {"live"})

    def run_wait(self, probe, limit=5):
        current = [0]

        def sleep(seconds):
            current[0] += seconds

        records = []
        return wait_for_telemetry(probe, records.append, clock=lambda: current[0], sleep=sleep, limit=limit), records

    def test_stale_and_changing_cohort_then_fresh_is_admitted(self):
        probe = Mock(
            side_effect=[
                TelemetryPending("metric_uid_outside_live_cohort"),
                TelemetryPending("live_pod_cohort_changed_during_collection"),
                {"fresh": True},
            ]
        )
        result, receipts = self.run_wait(probe)
        self.assertTrue(result["fresh"])
        self.assertEqual(receipts[-1]["phase"], "ready")
        self.assertEqual(len(receipts[-1]["observations"]), 2)

    def test_timeout_keeps_failure_and_never_admits(self):
        with self.assertRaises(Hold):
            self.run_wait(Mock(side_effect=TelemetryPending("metric_samples_empty")))

    def test_non_readiness_errors_are_not_retried(self):
        probe = Mock(side_effect=Hold("namespace identity changed"))
        with self.assertRaises(Hold):
            self.run_wait(probe)
        self.assertEqual(probe.call_count, 1)

    def test_positive_returned_after_deadline_is_rejected(self):
        clock = [0]
        records = []

        def slow_probe():
            clock[0] = 6
            return {"fresh": True}

        with self.assertRaises(Hold):
            wait_for_telemetry(slow_probe, records.append, clock=lambda: clock[0], sleep=lambda _: None, limit=5)
        self.assertEqual(records[-1]["phase"], "held")
        self.assertNotIn("accepted", records[-1])


if __name__ == "__main__":
    unittest.main()
