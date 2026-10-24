import json
import tempfile
import unittest
from pathlib import Path

import httpx
from mcp.types import CallToolResult, TextContent

from clients.external_agent.driver import ConductorHTTP, Hold, investigate, prompt, terminal_answer

CONTEXT = {
    "app_name": "Synthetic shop",
    "app_description": "Online shop.",
    "app_namespace": "pilot-app",
    "symptom": "Requests are failing.",
    "incident_started_at": "2030-01-01T00:00:00Z",
}
BINDING = {
    "namespace": "pilot-app",
    "namespace_uid": "uid-example",
    "kubernetes_connection": "pilot-kube",
    "grafana_connection": "pilot-grafana",
    "prometheus_connection": "pilot-prometheus",
}


def response(value=None, error=None):
    return CallToolResult(content=[TextContent(type="text", text=error or json.dumps(value))], isError=bool(error))


class Session:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


class Conductor:
    def __init__(self, failure=None):
        self.answers = []
        self.failure = failure

    def require_diagnosis(self):
        pass

    def submit_diagnosis(self, answer):
        self.answers.append(answer)
        if self.failure:
            raise self.failure


class DriverTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, session, conductor=None, **kwargs):
        conductor = conductor or Conductor()
        with tempfile.TemporaryDirectory() as root:
            out = Path(root)
            try:
                result = await investigate(session, conductor, out, CONTEXT, BINDING, **kwargs)
            except Exception:
                self.saved = json.loads((out / "driver-state.json").read_text())
                raise
            self.saved = json.loads((out / "driver-state.json").read_text())
            self.assertEqual(result, self.saved)
        return conductor

    async def test_actual_null_answer_terminal_timeline_submits_once(self):
        session = Session(
            [
                response({"slug": "ISS-1002", "agent_started": True}),
                response(
                    {
                        "slug": "ISS-1002",
                        "running": False,
                        "answer": None,
                        "timeline": [
                            {"role": "user", "text": "incident"},
                            {"role": "assistant", "text": "Observed diagnosis"},
                        ],
                    }
                ),
            ]
        )
        conductor = await self.run_case(session)
        self.assertEqual(conductor.answers, ["Observed diagnosis"])
        self.assertEqual(self.saved["phase"], "submitted")
        self.assertEqual(self.saved["final_answer_source"], "timeline")
        self.assertEqual([c[0] for c in session.calls], ["create_issue", "get_issue"])

    async def test_lost_create_response_reserves_uncertainty_without_replacement(self):
        session = Session([TimeoutError()])
        with self.assertRaises(TimeoutError):
            await self.run_case(session)
        self.assertTrue(self.saved["needs_reconciliation"])
        self.assertEqual(self.saved["create_requests"], 1)
        self.assertEqual(len(session.calls), 1)

    async def test_rejected_calibration_is_ungraded_without_retry(self):
        session = Session([response(error="External agent is calibrating to your environment")])
        with self.assertRaises(Hold):
            await self.run_case(session)
        self.assertEqual(self.saved["submission_requests"], 0)
        self.assertFalse(self.saved["needs_reconciliation"])
        self.assertNotIn("accuracy", self.saved)
        self.assertEqual(len(session.calls), 1)

    async def test_foreign_issue_never_reaches_submit(self):
        conductor = Conductor()
        with self.assertRaises(Hold):
            await self.run_case(
                Session(
                    [
                        response({"slug": "ISS-1002"}),
                        response({"slug": "ISS-1001", "running": False, "answer": "foreign"}),
                    ]
                ),
                conductor,
            )
        self.assertEqual(conductor.answers, [])

    async def test_terminal_without_answer_is_held(self):
        from unittest.mock import AsyncMock, patch

        clock = [0]

        class AdvancingSession(Session):
            async def call_tool(self, name, arguments, **kwargs):
                if name == "get_issue":
                    clock[0] += 31
                return await super().call_tool(name, arguments, **kwargs)

        empty = response({"slug": "ISS-1002", "running": False, "answer": None, "timeline": []})
        session = AdvancingSession([response({"slug": "ISS-1002"}), empty, empty, empty])
        with (
            patch("clients.external_agent.driver.asyncio.sleep", new=AsyncMock()),
            self.assertRaisesRegex(Hold, "bounded answer-publication grace"),
        ):
            await self.run_case(session, clock=lambda: clock[0])
        self.assertEqual(self.saved["submission_requests"], 0)
        self.assertEqual(self.saved["terminal_empty_observations"], 3)
        self.assertEqual([x[0] for x in session.calls].count("create_issue"), 1)

    async def test_terminal_flag_before_final_timeline_only_rereads_same_issue(self):
        from unittest.mock import AsyncMock, patch

        session = Session(
            [
                response({"slug": "ISS-1002"}),
                response({"slug": "ISS-1002", "running": False, "answer": None, "timeline": []}),
                response(
                    {
                        "slug": "ISS-1002",
                        "running": False,
                        "answer": None,
                        "timeline": [{"role": "assistant", "text": "late complete diagnosis"}],
                    }
                ),
            ]
        )
        with patch("clients.external_agent.driver.asyncio.sleep", new=AsyncMock()):
            conductor = await self.run_case(session)
        self.assertEqual(conductor.answers, ["late complete diagnosis"])
        self.assertEqual([x[0] for x in session.calls], ["create_issue", "get_issue", "get_issue"])
        self.assertEqual(self.saved["terminal_empty_observations"], 1)
        self.assertEqual(self.saved["create_requests"], self.saved["submission_requests"])
        self.assertEqual(self.saved["budget_seconds"], 1200)

    async def test_multiple_empty_reads_never_create_a_replacement(self):
        from unittest.mock import AsyncMock, patch

        empty = response({"slug": "ISS-1002", "running": False, "answer": None, "timeline": []})
        session = Session(
            [
                response({"slug": "ISS-1002"}),
                empty,
                empty,
                response({"slug": "ISS-1002", "running": False, "answer": "published answer"}),
            ]
        )
        with patch("clients.external_agent.driver.asyncio.sleep", new=AsyncMock()):
            conductor = await self.run_case(session)
        self.assertEqual(conductor.answers, ["published answer"])
        self.assertEqual(self.saved["terminal_empty_observations"], 2)
        self.assertEqual([x[0] for x in session.calls].count("create_issue"), 1)

    async def test_provider_error_is_not_hidden_by_publication_grace(self):
        session = Session(
            [
                response({"slug": "ISS-1002"}),
                response({"slug": "ISS-1002", "running": False, "error": "provider failure"}),
            ]
        )
        with self.assertRaisesRegex(Hold, "Provider reported"):
            await self.run_case(session)
        self.assertEqual(self.saved["submission_requests"], 0)
        self.assertEqual(len(session.calls), 2)

    async def test_publication_grace_never_extends_the_original_deadline(self):
        from unittest.mock import AsyncMock, patch

        clock = [0]

        class AdvancingSession(Session):
            async def call_tool(self, name, arguments, **kwargs):
                if name == "get_issue":
                    clock[0] += 10
                return await super().call_tool(name, arguments, **kwargs)

        empty = response({"slug": "ISS-1002", "running": False, "answer": None, "timeline": []})
        session = AdvancingSession([response({"slug": "ISS-1002"}), empty])
        with (
            patch("clients.external_agent.driver.asyncio.sleep", new=AsyncMock()),
            self.assertRaisesRegex(Hold, "deadline expired"),
        ):
            await self.run_case(session, budget_seconds=10, clock=lambda: clock[0])
        self.assertEqual(self.saved["submission_requests"], 0)
        self.assertEqual(len(session.calls), 2)

    async def test_lost_submission_is_not_repeated(self):
        conductor = Conductor(TimeoutError())
        with self.assertRaises(TimeoutError):
            await self.run_case(
                Session(
                    [
                        response({"slug": "ISS-1002"}),
                        response({"slug": "ISS-1002", "running": False, "answer": "diagnosis"}),
                    ]
                ),
                conductor,
            )
        self.assertEqual(conductor.answers, ["diagnosis"])
        self.assertTrue(self.saved["needs_reconciliation"])
        self.assertEqual(self.saved["submission_requests"], 1)

    async def test_guard_failure_happens_before_any_request(self):
        session = Session([])

        def fail():
            raise Hold("namespace replaced")

        with self.assertRaises(Hold):
            await self.run_case(session, guard=fail)
        self.assertEqual(session.calls, [])

    async def test_existing_attempt_is_never_reopened(self):
        session = Session([])
        with tempfile.TemporaryDirectory() as root:
            out = Path(root)
            (out / "driver-state.json").write_text('{"phase":"creation_pending"}')
            with self.assertRaises(Hold):
                await investigate(session, Conductor(), out, CONTEXT, BINDING)
            self.assertEqual(json.loads((out / "driver-state.json").read_text()), {"phase": "creation_pending"})
        self.assertEqual(session.calls, [])

    async def test_expired_budget_does_not_dispatch(self):
        session = Session([])
        with self.assertRaises(Hold):
            await self.run_case(session, budget_seconds=0)
        self.assertEqual(session.calls, [])

    def test_running_timeline_is_never_a_terminal_answer(self):
        self.assertEqual(
            terminal_answer(
                {"running": True, "answer": "partial", "timeline": [{"role": "assistant", "text": "partial"}]}
            ),
            (None, None),
        )

    def test_structured_answer_takes_precedence(self):
        self.assertEqual(
            terminal_answer({"running": False, "answer": "final", "timeline": [{"role": "assistant", "text": "old"}]}),
            ("final", "answer"),
        )

    def test_internal_fault_context_is_rejected(self):
        for field in ["problem_id", "ground_truth", "oracle", "expected_answer", "benchmark_log"]:
            with self.assertRaises(Hold):
                prompt({**CONTEXT, field: "private"}, BINDING)

    def test_namespace_mismatch_is_rejected(self):
        with self.assertRaises(Hold):
            prompt(CONTEXT, {**BINDING, "namespace": "foreign"})

    def test_grading_endpoint_cannot_be_remote(self):
        with self.assertRaises(Hold):
            ConductorHTTP("https://agent.example.invalid")

    def test_local_http_ack_must_confirm_exact_stage(self):
        conductor = ConductorHTTP("http://127.0.0.1:18980")
        conductor.client.close()
        calls = []

        def handler(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "200", "stage": "mitigation"})

        conductor.client = httpx.Client(base_url="http://127.0.0.1:18980", transport=httpx.MockTransport(handler))
        with self.assertRaises(Hold):
            conductor.submit_diagnosis("answer")
        with self.assertRaises(Hold):
            conductor.submit_diagnosis("answer")
        self.assertEqual(len(calls), 1)
        conductor.close()


if __name__ == "__main__":
    unittest.main()
