from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from atif_converter import ConversionFailedError, ObservationResult, Step, ToolCall, Trajectory, convert, detect_agent
from atif_converter.adapters import assistant_v3
from sregym.traces import convert as run_converter
from sregym.traces import postprocess, store

FIXTURE = Path(__file__).parents[1] / "fixtures" / "assistant_v3" / "golden_infrastructure_invalid"


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _materialize_run(
    tmp_path: Path,
    *,
    events: list[dict[str, Any]] | None = None,
    terminal: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    problem_id: str = "edge_request_filter_cpu_saturation_astronomy_shop",
) -> Path:
    run_dir = tmp_path / "results" / "batch" / "assistant_v3" / problem_id / "run_1"
    shutil.copytree(FIXTURE, run_dir)
    if events is not None:
        event_path = run_dir / "assistant_v3" / "events.jsonl"
        event_path.write_text(
            "".join(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in events),
            encoding="utf-8",
        )
    if terminal is not None:
        _write_json(run_dir / "assistant_v3" / "terminal.json", terminal)
    if metadata is not None:
        _write_json(run_dir / "run_metadata.json", metadata)
    return run_dir


def _event(sequence: int, name: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "offset_ms": float(sequence * 10),
        "event": name,
        "event_id": None,
        "data": data,
        "redacted": False,
    }


def _event_steps(trajectory: Trajectory) -> list[str]:
    names: list[str] = []
    for step in trajectory.steps:
        extra = step.extra or {}
        native = extra.get("assistant_v3")
        if isinstance(native, dict) and isinstance(native.get("event"), str):
            names.append(native["event"])
    return names


def test_detects_and_converts_complete_native_artifacts() -> None:
    event_path = FIXTURE / "assistant_v3" / "events.jsonl"

    assert detect_agent(event_path) == "assistant_v3"
    trajectory = convert(event_path)

    assert trajectory.schema_version == "ATIF-v1.7"
    assert trajectory.agent.name == "assistant_v3"
    assert trajectory.agent.version == "v3-test"
    assert trajectory.agent.model_name == "gpt-5.6-luna"
    assert trajectory.steps[0].source == "user"
    assert trajectory.steps[0].message == "Investigate the application and diagnose the root cause."
    assert [step.step_id for step in trajectory.steps] == list(range(1, len(trajectory.steps) + 1))

    root_call_steps = [
        step for step in trajectory.steps if step.tool_calls and step.tool_calls[0].tool_call_id == "call-1"
    ]
    assert len(root_call_steps) == 1
    observation = root_call_steps[0].observation
    assert observation is not None
    result = observation.results[0]
    assert result.source_call_id == "call-1"
    assert result.content == "failed"
    assert result.extra == {"assistant_v3": {"is_error": True, "sequence": 8}}

    messages = [step.message for step in trajectory.steps if step.source == "agent"]
    assert messages.count("same text") == 2
    assert messages.count("Root cause: dependency saturation.") == 1
    assert trajectory.final_metrics is not None
    assert trajectory.final_metrics.total_prompt_tokens == 100
    assert trajectory.final_metrics.total_completion_tokens == 25
    assert trajectory.final_metrics.extra is not None
    assert trajectory.final_metrics.extra["reasoning_tokens"] is None
    assert trajectory.final_metrics.extra["total_tokens"] == 125
    Trajectory.model_validate(trajectory.to_json_dict())


def test_explicit_agent_override_converts_the_native_file() -> None:
    trajectory = convert(FIXTURE / "assistant_v3" / "events.jsonl", agent="assistant_v3")
    assert trajectory.agent.name == "assistant_v3"


def test_attempt_run_identity_is_independent_from_problem_identity(tmp_path: Path) -> None:
    metadata = json.loads((FIXTURE / "run_metadata.json").read_text(encoding="utf-8"))
    metadata["run_id"] = "anon_fedcba9876543210fedcba9876543210"
    run_dir = _materialize_run(tmp_path, metadata=metadata)

    trajectory = convert(run_dir / "assistant_v3" / "events.jsonl")

    assert trajectory.extra is not None
    assert trajectory.extra["assistant_v3"]["problem_id"] == metadata["problem_id"]


def test_subagent_events_preserve_chronology_and_form_an_embedded_trajectory(tmp_path: Path) -> None:
    header = _records(FIXTURE / "assistant_v3" / "events.jsonl")[0]
    native_events = [
        _event(1, "assistant.subagent.started", {"run_id": "sub-1", "name": "investigator"}),
        _event(2, "assistant.subagent.delta", {"run_id": "sub-1", "text": "checking traces"}),
        _event(
            3,
            "assistant.subagent.action",
            {"run_id": "sub-1", "action_id": "action-1", "tool_name": "analyze_table", "status": "running"},
        ),
        _event(
            4,
            "assistant.subagent.complete",
            {"run_id": "sub-1", "name": "investigator", "is_error": False, "text": "found saturation"},
        ),
        _event(5, "message.delta", {"text": "Root cause found."}),
        _event(6, "message.complete", {"final_text": "Root cause found.", "session_id": "session-1"}),
    ]
    terminal = json.loads((FIXTURE / "assistant_v3" / "terminal.json").read_text())
    terminal.update({"final_text": "Root cause found.", "session_id": "session-1"})
    run_dir = _materialize_run(tmp_path, events=[header, *native_events], terminal=terminal)

    trajectory = convert(run_dir / "assistant_v3" / "events.jsonl")

    assert _event_steps(trajectory) == [
        "assistant.subagent.started",
        "assistant.subagent.delta",
        "assistant.subagent.action",
        "assistant.subagent.complete",
        "message.delta",
    ]
    assert trajectory.subagent_trajectories is not None
    assert len(trajectory.subagent_trajectories) == 1
    subagent = trajectory.subagent_trajectories[0]
    assert subagent.session_id == "sub-1"
    assert _event_steps(subagent) == [
        "assistant.subagent.started",
        "assistant.subagent.delta",
        "assistant.subagent.action",
        "assistant.subagent.complete",
    ]
    action_step = subagent.steps[2]
    assert action_step.tool_calls is not None
    assert action_step.tool_calls[0].tool_call_id == "action-1"
    assert trajectory.steps[1].observation is not None
    start_result = trajectory.steps[1].observation.results[0]
    assert start_result.subagent_trajectory_ref is not None
    assert start_result.subagent_trajectory_ref[0].trajectory_id == subagent.trajectory_id
    assert sum(step.message == "Root cause found." for step in trajectory.steps) == 1


def test_incomplete_error_stream_remains_valid_without_a_fabricated_answer(tmp_path: Path) -> None:
    source_records = _records(FIXTURE / "assistant_v3" / "events.jsonl")
    native_events = [
        _event(1, "message.delta", {"text": "Partial investigation"}),
        _event(2, "assistant.error", {"code": "UPSTREAM", "message": "provider unavailable"}),
    ]
    terminal = json.loads((FIXTURE / "assistant_v3" / "terminal.json").read_text())
    terminal.update(
        {
            "outcome": "assistant_error",
            "final_text": None,
            "submitted": False,
            "submission_count": 0,
        }
    )
    metadata = json.loads((FIXTURE / "run_metadata.json").read_text())
    metadata["classification"] = "assistant_error"
    run_dir = _materialize_run(
        tmp_path,
        events=[source_records[0], *native_events],
        terminal=terminal,
        metadata=metadata,
    )

    trajectory = convert(run_dir / "assistant_v3" / "events.jsonl")

    assert [step.message for step in trajectory.steps] == [
        "Investigate the application and diagnose the root cause.",
        "Partial investigation",
        "",
    ]
    assert _event_steps(trajectory) == ["message.delta", "assistant.error"]
    assert trajectory.extra is not None
    assert trajectory.extra["assistant_v3"]["terminal_outcome"] == "assistant_error"
    assert trajectory.extra["assistant_v3"]["submitted"] is False


def test_maps_thinking_progress_fallback_tools_and_subagent_errors(tmp_path: Path) -> None:
    header = _records(FIXTURE / "assistant_v3" / "events.jsonl")[0]
    native_events = [
        _event(1, "message.thinking", {"text": "reasoning"}),
        _event(2, "assistant.progress", {}),
        _event(3, "tool.use", {"name": "raw_tool", "input": "raw"}),
        _event(
            4,
            "tool.result",
            {"tool_use_id": "assistant_v3_tool_3", "content": {"answer": 1}, "is_error": False},
        ),
        _event(5, "tool.use", {"id": "no-input", "name": "empty_tool"}),
        _event(
            6,
            "assistant.subagent.action",
            {
                "run_id": "sub-error",
                "action_id": "action-error",
                "tool_name": "analyze_table",
                "status": "running",
            },
        ),
        _event(
            7,
            "assistant.subagent.action",
            {
                "run_id": "sub-error",
                "action_id": "action-error",
                "tool_name": "analyze_table",
                "status": "failed",
                "is_error": True,
            },
        ),
        _event(8, "assistant.subagent.action", {"run_id": "sub-error"}),
        _event(9, "assistant.subagent.heartbeat", {"run_id": "sub-error"}),
        _event(10, "message.delta", {"text": "Root cause"}),
        _event(11, "message.delta", {"text": "ignored duplicate"}),
        _event(12, "message.complete", {"final_text": "Root cause found."}),
    ]
    native_events[9]["event_id"] = "same-event"
    native_events[10]["event_id"] = "same-event"
    terminal = json.loads((FIXTURE / "assistant_v3" / "terminal.json").read_text())
    terminal["final_text"] = "Root cause found."
    run_dir = _materialize_run(tmp_path, events=[header, *native_events], terminal=terminal)

    trajectory = convert(run_dir / "assistant_v3" / "events.jsonl")

    reasoning = next(step for step in trajectory.steps if step.reasoning_content)
    assert reasoning.reasoning_content == "reasoning"
    raw_tool = next(
        step for step in trajectory.steps if step.tool_calls and step.tool_calls[0].function_name == "raw_tool"
    )
    assert raw_tool.tool_calls is not None
    assert raw_tool.tool_calls[0].tool_call_id == "assistant_v3_tool_3"
    assert raw_tool.tool_calls[0].arguments == {"value": "raw"}
    assert raw_tool.observation is not None
    assert raw_tool.observation.results[0].content == '{"answer":1}'
    empty_tool = next(
        step for step in trajectory.steps if step.tool_calls and step.tool_calls[0].function_name == "empty_tool"
    )
    assert empty_tool.tool_calls is not None
    assert empty_tool.tool_calls[0].arguments == {}
    assert trajectory.subagent_trajectories is not None
    subagent = trajectory.subagent_trajectories[0]
    assert subagent.agent.name == "assistant_v3_subagent"
    assert subagent.steps[0].observation is not None
    assert subagent.steps[0].observation.results[0].extra == {"assistant_v3": {"is_error": True, "sequence": 7}}
    assert subagent.steps[1].tool_calls is None
    assert subagent.steps[2].tool_calls is None
    assert subagent.steps[3].message == ""
    assert [step.message for step in trajectory.steps].count("ignored duplicate") == 0
    assert [step.message for step in trajectory.steps][-2:] == ["Root cause", " found."]


def test_result_attachment_appends_to_an_existing_observation() -> None:
    owner = Step(
        step_id=1,
        source="agent",
        message="",
        tool_calls=[ToolCall(tool_call_id="one", function_name="tool", arguments={})],
    )
    assistant_v3._attach_result(owner, ObservationResult(source_call_id="one", content="first"))
    assistant_v3._attach_result(owner, ObservationResult(source_call_id="one", content="second"))

    assert owner.observation is not None
    assert [result.content for result in owner.observation.results] == ["first", "second"]


@pytest.mark.parametrize(
    "mutation",
    ["sequence_gap", "dangling_result", "duplicate_completion", "identity_mismatch", "invalid_tool"],
)
def test_invalid_native_artifacts_fail_schema_and_reference_validation(tmp_path: Path, mutation: str) -> None:
    records = _records(FIXTURE / "assistant_v3" / "events.jsonl")
    metadata = json.loads((FIXTURE / "run_metadata.json").read_text())
    if mutation == "sequence_gap":
        records[1]["sequence"] = 2
    elif mutation == "dangling_result":
        records = [records[0], _event(1, "tool.result", {"tool_use_id": "missing", "content": "x"})]
    elif mutation == "duplicate_completion":
        duplicate = dict(records[-1])
        duplicate.update({"sequence": len(records), "offset_ms": 110.0})
        records.append(duplicate)
    elif mutation == "identity_mismatch":
        metadata["problem_id"] = "different_problem"
    else:
        records[2]["data"]["name"] = ""
    run_dir = _materialize_run(tmp_path, events=records, metadata=metadata)
    event_path = run_dir / "assistant_v3" / "events.jsonl"

    with pytest.raises(ValueError):
        assistant_v3.convert_file(event_path)
    with pytest.raises(ConversionFailedError):
        convert(event_path)


@pytest.mark.parametrize(
    "content",
    [None, "", "{not-json\n", "[]\n", '{"schema":"wrong","problem_id":"anon"}\n'],
)
def test_rejects_missing_empty_malformed_or_wrong_event_stream(tmp_path: Path, content: str | None) -> None:
    event_path = tmp_path / "events.jsonl"
    if content is not None:
        event_path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        assistant_v3.convert_file(event_path)


@pytest.mark.parametrize("mode", ["missing", "malformed", "wrong_schema"])
def test_rejects_missing_malformed_or_wrong_schema_companion_artifact(tmp_path: Path, mode: str) -> None:
    run_dir = _materialize_run(tmp_path)
    request_path = run_dir / "assistant_v3" / "request.json"
    if mode == "missing":
        request_path.unlink()
    elif mode == "malformed":
        request_path.write_text("{bad", encoding="utf-8")
    else:
        _write_json(request_path, {"schema": "wrong"})

    with pytest.raises(ValueError):
        assistant_v3.convert_file(run_dir / "assistant_v3" / "events.jsonl")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("offset_ms", True),
        ("event", ""),
        ("event_id", 7),
        ("data", []),
        ("redacted", 0),
    ],
)
def test_rejects_invalid_native_event_fields(tmp_path: Path, field: str, value: object) -> None:
    records = _records(FIXTURE / "assistant_v3" / "events.jsonl")
    records[1][field] = value
    run_dir = _materialize_run(tmp_path, events=records)

    with pytest.raises(ValueError):
        assistant_v3.convert_file(run_dir / "assistant_v3" / "events.jsonl")


@pytest.mark.parametrize("mutation", ["agent", "prompt", "model", "terminal"])
def test_rejects_invalid_required_metadata_and_terminal_state(tmp_path: Path, mutation: str) -> None:
    metadata = json.loads((FIXTURE / "run_metadata.json").read_text())
    terminal = json.loads((FIXTURE / "assistant_v3" / "terminal.json").read_text())
    run_dir = _materialize_run(tmp_path)
    if mutation == "agent":
        metadata["agent_name"] = "other"
        _write_json(run_dir / "run_metadata.json", metadata)
    elif mutation == "prompt":
        request = json.loads((FIXTURE / "assistant_v3" / "request.json").read_text())
        request["prompt"] = ""
        _write_json(run_dir / "assistant_v3" / "request.json", request)
    elif mutation == "model":
        metadata["resolved_model"] = None
        metadata["requested_model"] = ""
        _write_json(run_dir / "run_metadata.json", metadata)
    else:
        terminal["submitted"] = False
        _write_json(run_dir / "assistant_v3" / "terminal.json", terminal)

    with pytest.raises(ValueError):
        assistant_v3.convert_file(run_dir / "assistant_v3" / "events.jsonl")


@pytest.mark.parametrize("mutation", ["missing", "mismatch", "incomplete_with_final"])
def test_rejects_inconsistent_completion_state(tmp_path: Path, mutation: str) -> None:
    records = _records(FIXTURE / "assistant_v3" / "events.jsonl")
    terminal = json.loads((FIXTURE / "assistant_v3" / "terminal.json").read_text())
    metadata = json.loads((FIXTURE / "run_metadata.json").read_text())
    if mutation == "missing":
        records = records[:-1]
    elif mutation == "mismatch":
        records[-1]["data"]["final_text"] = "different"
    else:
        terminal["outcome"] = "assistant_error"
        terminal["submitted"] = False
        terminal["submission_count"] = 0
        metadata["classification"] = "assistant_error"
    run_dir = _materialize_run(tmp_path, events=records, terminal=terminal, metadata=metadata)

    with pytest.raises(ValueError):
        assistant_v3.convert_file(run_dir / "assistant_v3" / "events.jsonl")


def test_sregym_conversion_adds_canonical_path_metadata_and_submission_boundary(tmp_path: Path) -> None:
    run_dir = _materialize_run(tmp_path)
    (run_dir / "assistant_v3_results_edge_request_filter_cpu_saturation_astronomy_shop.json").write_text(
        json.dumps({"success": True}), encoding="utf-8"
    )

    trajectory = run_converter.convert_run(run_dir)

    assert trajectory is not None
    assert trajectory.extra is not None
    assert trajectory.trajectory_id == ("batch/assistant_v3/edge_request_filter_cpu_saturation_astronomy_shop/run_1")
    assert trajectory.extra["sregym"] == {
        "application": "Astronomy Shop",
        "diagnosis_submitted_step": len(trajectory.steps),
        "problem_id": "edge_request_filter_cpu_saturation_astronomy_shop",
        "results_path": "batch/assistant_v3/edge_request_filter_cpu_saturation_astronomy_shop/run_1",
        "run": 1,
        "submitted": True,
    }


def test_postprocess_is_byte_deterministic_and_sqlite_roundtrips(tmp_path: Path) -> None:
    run_dir = _materialize_run(tmp_path)

    first_path = postprocess.write_trajectory(run_dir)
    assert first_path is not None
    first = first_path.read_bytes()
    second_path = postprocess.write_trajectory(run_dir)
    assert second_path == first_path
    assert second_path is not None
    assert second_path.read_bytes() == first

    payload = json.loads(first)
    trajectory = Trajectory.model_validate(payload)
    database = tmp_path / "traces.db"
    stored_id = store.ingest_trajectory_file(first_path, database)
    assert stored_id == trajectory.trajectory_id
    restored = store.get(trajectory.trajectory_id or "", database)
    assert restored is not None
    assert restored.to_json_dict() == trajectory.to_json_dict()
