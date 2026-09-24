"""Assistant v3 native artifacts -> ATIF v1.7 adapter.

The adapter consumes ``assistant_v3/events.jsonl`` together with its sibling
``request.json`` and ``terminal.json`` plus the run-level
``run_metadata.json``. Raw events stay authoritative; this module performs
only deterministic normalization into ATIF.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ..atif import (
    Agent,
    FinalMetrics,
    Observation,
    ObservationResult,
    Step,
    SubagentTrajectoryRef,
    ToolCall,
    Trajectory,
)

AGENT_NAME = "assistant_v3"
EVENTS_SCHEMA = "sregym.assistant_v3.events.v1"
REQUEST_SCHEMA = "sregym.assistant_v3.request.v1"
TERMINAL_SCHEMA = "sregym.assistant_v3.terminal.v1"
METADATA_SCHEMA = "sregym.assistant_v3.run_metadata.v1"
_TERMINAL_OUTCOMES = frozenset(
    {
        "completed",
        "configuration_error",
        "authentication_error",
        "permission_error",
        "transient_exhausted",
        "incomplete_stream",
        "assistant_error",
        "ambiguous_completion",
        "capability_policy_violation",
        "infrastructure_invalid",
    }
)


@dataclass(frozen=True)
class _NativeEvent:
    sequence: int
    offset_ms: float
    event: str
    event_id: str | None
    data: dict[str, Any]
    redacted: bool


def _read_object(path: Path, schema: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Assistant artifact is not readable JSON: {path.name}") from error
    if not isinstance(payload, dict) or payload.get("schema") != schema:
        raise ValueError(f"Assistant artifact has an invalid schema: {path.name}")
    return payload


def _read_events(path: Path) -> tuple[str, list[_NativeEvent]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise ValueError("Assistant event stream is not readable UTF-8") from error
    if not lines:
        raise ValueError("Assistant event stream is empty")
    try:
        records = [json.loads(line) for line in lines]
    except json.JSONDecodeError as error:
        raise ValueError("Assistant event stream contains malformed JSON") from error
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("Assistant event stream records must be objects")
    header = records[0]
    problem_id = header.get("problem_id")
    if header.get("schema") != EVENTS_SCHEMA or not isinstance(problem_id, str) or not problem_id:
        raise ValueError("Assistant event stream header is invalid")

    events: list[_NativeEvent] = []
    previous_offset = -1.0
    for expected_sequence, record in enumerate(records[1:], start=1):
        sequence = record.get("sequence")
        offset_ms = record.get("offset_ms")
        event = record.get("event")
        event_id = record.get("event_id")
        data = record.get("data")
        redacted = record.get("redacted")
        if sequence != expected_sequence:
            raise ValueError("Assistant event sequence must be contiguous")
        if (
            not isinstance(offset_ms, (int, float))
            or isinstance(offset_ms, bool)
            or not math.isfinite(offset_ms)
            or offset_ms < previous_offset
            or offset_ms < 0
        ):
            raise ValueError("Assistant event offsets must be finite and monotonic")
        if not isinstance(event, str) or not event:
            raise ValueError("Assistant event name must be non-empty")
        if event_id is not None and not isinstance(event_id, str):
            raise ValueError("Assistant event id must be a string or null")
        if not isinstance(data, dict) or not isinstance(redacted, bool):
            raise ValueError("Assistant event payload or redaction marker is invalid")
        events.append(
            _NativeEvent(
                sequence=sequence,
                offset_ms=float(offset_ms),
                event=event,
                event_id=event_id,
                data=data,
                redacted=redacted,
            )
        )
        previous_offset = float(offset_ms)
    return problem_id, events


def _string(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {} if value is None else {"value": value}


def _event_extra(event: _NativeEvent) -> dict[str, Any]:
    return {
        "assistant_v3": {
            "event": event.event,
            "sequence": event.sequence,
            "offset_ms": event.offset_ms,
            "event_id": event.event_id,
            "redacted": event.redacted,
            "data": event.data,
        }
    }


def _optional_token(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _event_message(event: _NativeEvent) -> str:
    if event.event == "assistant.subagent.delta":
        return _string(event.data.get("text"))
    if event.event == "assistant.subagent.complete":
        return _string(event.data.get("text")) or f"Subagent {event.data.get('name') or 'unknown'} completed"
    if event.event == "assistant.subagent.started":
        return f"Subagent {event.data.get('name') or 'unknown'} started"
    if event.event == "assistant.subagent.action":
        name = event.data.get("tool_name") or "action"
        status = event.data.get("status")
        return f"{name}: {status}" if status is not None else str(name)
    return ""


def _subagent_trajectory_id(problem_id: str, run_id: str) -> str:
    return f"assistant_v3/{problem_id}/subagent/{run_id}"


def _build_subagent(
    problem_id: str,
    run_id: str,
    events: list[_NativeEvent],
    *,
    version: str,
    model: str,
) -> Trajectory:
    steps: list[Step] = []
    action_owners: dict[str, Step] = {}
    name = next(
        (
            str(event.data["name"])
            for event in events
            if event.event in {"assistant.subagent.started", "assistant.subagent.complete"} and event.data.get("name")
        ),
        "assistant_v3_subagent",
    )
    for event in events:
        tool_calls: list[ToolCall] | None = None
        action_id: str | None = None
        action_error = False
        if event.event == "assistant.subagent.action":
            candidate_action_id = event.data.get("action_id")
            tool_name = event.data.get("tool_name")
            if (
                isinstance(candidate_action_id, str)
                and candidate_action_id
                and isinstance(tool_name, str)
                and tool_name
            ):
                action_id = candidate_action_id
            if action_id is not None and action_id not in action_owners:
                tool_calls = [
                    ToolCall(
                        tool_call_id=action_id,
                        function_name=cast(str, tool_name),
                        arguments={"status": event.data.get("status")},
                    )
                ]
            action_error = event.data.get("is_error") is True
        step = Step(
            step_id=len(steps) + 1,
            source="agent",
            model_name=model,
            message=_event_message(event),
            tool_calls=tool_calls,
            extra=_event_extra(event),
        )
        steps.append(step)
        if action_id is not None:
            owner = action_owners.setdefault(action_id, step)
            if action_error:
                _attach_result(
                    owner,
                    ObservationResult(
                        source_call_id=action_id,
                        content=_string(event.data.get("message") or event.data.get("status")),
                        extra={"assistant_v3": {"is_error": True, "sequence": event.sequence}},
                    ),
                )
    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=run_id,
        trajectory_id=_subagent_trajectory_id(problem_id, run_id),
        agent=Agent(name=name, version=version, model_name=model),
        steps=steps,
        final_metrics=FinalMetrics(total_steps=len(steps)),
        extra={"assistant_v3": {"parent_problem_id": problem_id, "subagent_run_id": run_id}},
    )


def _append_step(steps: list[Step], **values: Any) -> Step:
    step = Step(step_id=len(steps) + 1, **values)
    steps.append(step)
    return step


def _attach_result(owner: Step, result: ObservationResult) -> None:
    if owner.observation is None:
        owner.observation = Observation(results=[result])
    else:
        owner.observation.results.append(result)


def _conversion_inputs(path: Path) -> tuple[str, list[_NativeEvent], dict[str, Any], dict[str, Any], dict[str, Any]]:
    problem_id, events = _read_events(path)
    request = _read_object(path.with_name("request.json"), REQUEST_SCHEMA)
    terminal = _read_object(path.with_name("terminal.json"), TERMINAL_SCHEMA)
    metadata = _read_object(path.parent.parent / "run_metadata.json", METADATA_SCHEMA)
    # The provider run id is a unique, anonymized telemetry correlation key; it
    # is deliberately independent from the benchmark problem identity.
    identities = (request.get("problem_id"), metadata.get("problem_id"))
    if any(identity != problem_id for identity in identities):
        raise ValueError("Assistant artifact identities do not match")
    if metadata.get("agent_name") != AGENT_NAME:
        raise ValueError("Assistant run metadata has an invalid agent name")
    outcome = terminal.get("outcome")
    submitted = terminal.get("submitted")
    submission_count = terminal.get("submission_count")
    if (
        outcome not in _TERMINAL_OUTCOMES
        or not isinstance(submitted, bool)
        or not isinstance(submission_count, int)
        or isinstance(submission_count, bool)
        or submission_count < 0
        or submitted != (submission_count == 1)
        or (submitted and outcome != "completed")
    ):
        raise ValueError("Assistant terminal state is invalid")
    prompt = request.get("prompt")
    model = metadata.get("resolved_model") or metadata.get("requested_model")
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(model, str) or not model.strip():
        raise ValueError("Assistant request or model metadata is incomplete")
    return problem_id, events, request, terminal, metadata


def convert_file(session_file: Path | str) -> Trajectory:
    """Convert one validated Assistant event stream and companion artifacts."""
    path = Path(session_file)
    problem_id, events, request, terminal, metadata = _conversion_inputs(path)
    version_value = metadata.get("agent_version")
    version = version_value if isinstance(version_value, str) and version_value else "unknown"
    model_value = metadata.get("resolved_model") or metadata.get("requested_model")
    model = str(model_value)

    steps: list[Step] = [Step(step_id=1, source="user", message=request["prompt"])]
    call_owners: dict[str, Step] = {}
    result_ids: set[str] = set()
    event_ids: set[str] = set()
    delta_text: list[str] = []
    delta_steps: list[Step] = []
    completion_event: _NativeEvent | None = None
    usage: dict[str, Any] | None = None
    subagent_events: dict[str, list[_NativeEvent]] = {}
    subagent_ref_steps: dict[str, Step] = {}
    skipped: dict[str, int] = {}

    for event in events:
        if event.event_id is not None:
            if event.event_id in event_ids:
                continue
            event_ids.add(event.event_id)

        if event.event == "message.delta":
            text = _string(event.data.get("text"))
            delta_text.append(text)
            delta_steps.append(
                _append_step(steps, source="agent", model_name=model, message=text, extra=_event_extra(event))
            )
        elif event.event == "message.thinking":
            reasoning = _string(event.data.get("thinking") or event.data.get("text"))
            _append_step(
                steps,
                source="agent",
                model_name=model,
                message="",
                reasoning_content=reasoning or None,
                extra=_event_extra(event),
            )
        elif event.event == "assistant.progress":
            _append_step(
                steps,
                source="agent",
                model_name=model,
                message=_string(event.data.get("message") or event.data.get("text")),
                extra=_event_extra(event),
            )
        elif event.event == "tool.use":
            call_id = event.data.get("id")
            if not isinstance(call_id, str) or not call_id:
                call_id = f"assistant_v3_tool_{event.sequence}"
            if call_id in call_owners:
                continue
            name = event.data.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("Assistant tool call requires a name")
            call = ToolCall(
                tool_call_id=call_id,
                function_name=name,
                arguments=_arguments(event.data.get("input")),
            )
            owner = _append_step(
                steps,
                source="agent",
                model_name=model,
                message="",
                tool_calls=[call],
                extra=_event_extra(event),
            )
            call_owners[call_id] = owner
        elif event.event == "tool.result":
            call_id = event.data.get("tool_use_id")
            if not isinstance(call_id, str) or call_id not in call_owners:
                raise ValueError("Assistant tool result does not reference a known call")
            if call_id in result_ids:
                continue
            result_ids.add(call_id)
            _attach_result(
                call_owners[call_id],
                ObservationResult(
                    source_call_id=call_id,
                    content=_string(event.data.get("content")) or None,
                    extra={
                        "assistant_v3": {
                            "is_error": event.data.get("is_error") is True,
                            "sequence": event.sequence,
                        }
                    },
                ),
            )
        elif event.event == "assistant.usage":
            candidate = event.data.get("usage")
            usage = candidate if isinstance(candidate, dict) else None
        elif event.event == "message.complete":
            if completion_event is not None:
                raise ValueError("Assistant event stream has multiple completion events")
            completion_event = event
        elif event.event == "assistant.error":
            _append_step(steps, source="agent", model_name=model, message="", extra=_event_extra(event))
        elif event.event.startswith("assistant.subagent."):
            root_step = _append_step(
                steps,
                source="agent",
                model_name=model,
                message=_event_message(event),
                extra=_event_extra(event),
            )
            run_id = event.data.get("run_id")
            if isinstance(run_id, str) and run_id:
                subagent_events.setdefault(run_id, []).append(event)
                subagent_ref_steps.setdefault(run_id, root_step)
        else:
            skipped[event.event] = skipped.get(event.event, 0) + 1

    outcome = terminal.get("outcome")
    terminal_text = terminal.get("final_text")
    if outcome == "completed":
        if completion_event is None or not isinstance(terminal_text, str) or not terminal_text.strip():
            raise ValueError("Completed Assistant artifacts require one final response")
        event_text = completion_event.data.get("final_text", completion_event.data.get("text"))
        if event_text != terminal_text:
            raise ValueError("Assistant completion and terminal final text do not match")
        streamed_text = "".join(delta_text)
        if terminal_text == streamed_text and delta_steps:
            delta_extra = delta_steps[-1].extra
            assert delta_extra is not None
            native = delta_extra["assistant_v3"]
            native["completion_sequence"] = completion_event.sequence
            final_step_id = delta_steps[-1].step_id
        else:
            completion_text = terminal_text.removeprefix(streamed_text) if streamed_text else terminal_text
            final_step = _append_step(
                steps,
                source="agent",
                model_name=model,
                message=completion_text,
                extra=_event_extra(completion_event),
            )
            final_step_id = final_step.step_id
    else:
        if completion_event is not None or terminal_text is not None:
            raise ValueError("Incomplete Assistant artifacts must not contain a final response")
        final_step_id = None

    subagents = [
        _build_subagent(problem_id, run_id, native_events, version=version, model=model)
        for run_id, native_events in subagent_events.items()
    ]
    for run_id, ref_step in subagent_ref_steps.items():
        _attach_result(
            ref_step,
            ObservationResult(
                content=f"Delegated to Assistant subagent {run_id}",
                subagent_trajectory_ref=[
                    SubagentTrajectoryRef(
                        trajectory_id=_subagent_trajectory_id(problem_id, run_id),
                        session_id=run_id,
                    )
                ],
            ),
        )

    usage = usage or {}
    metrics_extra = {
        "reasoning_tokens": _optional_token(usage.get("reasoning_tokens")),
        "total_tokens": _optional_token(usage.get("total_tokens")),
    }
    root_extra: dict[str, Any] = {
        "source_schema": EVENTS_SCHEMA,
        "problem_id": problem_id,
        "prompt_profile_id": request.get("prompt_profile_id"),
        "terminal_outcome": outcome,
        "submitted": terminal.get("submitted") is True,
        "completion_step": final_step_id,
        "event_count": len(events),
    }
    if skipped:
        root_extra["unmapped_event_counts"] = skipped
    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=terminal.get("session_id") or problem_id,
        agent=Agent(
            name=AGENT_NAME,
            version=version,
            model_name=model,
            extra={
                "reasoning_effort": metadata.get("resolved_reasoning") or metadata.get("requested_reasoning"),
                "capability_profile": metadata.get("capability_profile"),
            },
        ),
        steps=steps,
        final_metrics=FinalMetrics(
            total_prompt_tokens=_optional_token(usage.get("input_tokens")),
            total_completion_tokens=_optional_token(usage.get("output_tokens")),
            total_steps=len(steps),
            extra=metrics_extra,
        ),
        extra={"assistant_v3": root_extra},
        subagent_trajectories=subagents or None,
    )


__all__ = ["AGENT_NAME", "EVENTS_SCHEMA", "convert_file"]
