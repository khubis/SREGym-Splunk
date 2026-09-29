# Contract: Assistant Native Artifacts ↔ SRE Gym Results/ATIF

## Canonical Run Layout

Before publication, files live below the opaque active directory. After existing semantic publication they live at:

```text
results/<batch>/assistant_v3/<problem_id>/run_<attempt>/
├── assistant_v3/
│   ├── request.json
│   ├── events.jsonl
│   └── terminal.json
├── run_metadata.json
├── metrics.json
├── failure.json                 # only for non-success terminal outcomes
├── observability/
│   └── delivery.json
├── splunk_lite_delivery.json   # optional post-run representative query proof
├── splunk_lite_pre_agent.json   # published only after agent exit; never agent-mounted while running
├── splunk_visible_assessment.json # post-grade secondary result, or unverified
├── trajectory/
│   └── trajectory.json          # validated ATIF 1.7
├── <existing SRE Gym judge/result artifacts>
└── <problem_id>_results.csv
```

Files are UTF-8, end with one newline, use stable key ordering for derived JSON, and are written with temporary-file plus atomic replace. Re-running normalization replaces `metrics.json` and `trajectory.json`; it never appends or duplicates logical events.

## `request.json`

Contains no headers or credentials:

```json
{
  "schema": "sregym.assistant_v3.request.v1",
  "problem_id": "anon_<id>",
  "prompt": "<rendered prompt>",
  "action_instructions": "<inclusive UTC telemetry time window>",
  "prompt_profile_id": "sregym-stratus-diagnosis-v1",
  "prompt_sha256": "<sha256>",
  "reference_sha256": "<sha256>",
  "substitution_ids": ["<stable-id>"],
  "requested_model": "gpt-5.6-luna",
  "requested_reasoning": "medium",
  "session_id": null,
  "surface": null
}
```

The existing artifact publisher may replace only the semantic `problem_id` from opaque to canonical. Prompt text must never contain either ID.

For a symptom-guided exploratory run, `prompt_profile_id` continues to identify the unchanged frozen diagnosis body, while a separate `action_profile_id: sregym-symptom-window-v1` identifies the added instruction. `action_instructions` includes only the actual UTC window and one reviewed user-observable symptom. `comparable` is false even with a full deployment profile. A time-only baseline request omits the additional action-profile field and symptom. Both preserve the exact frozen diagnosis body and its hash.

## `events.jsonl`

The first line is a header:

```json
{"schema":"sregym.assistant_v3.events.v1","problem_id":"anon_<id>"}
```

Each following record is:

```json
{
  "sequence": 1,
  "offset_ms": 125.4,
  "event": "tool.use",
  "event_id": "optional-server-id",
  "data": {},
  "redacted": false
}
```

`sequence` is contiguous and receive-ordered. `offset_ms` is monotonic from the first request attempt and is the source for latency metrics. Payload data is the parsed SSE JSON object. Known secret values are replaced atomically with `[REDACTED]`; `redacted` is true when any replacement occurred. Authorization/request headers are never records.

Raw records are not deduplicated. Derived consumers deduplicate only when a stable event/tool/action ID proves two records represent the same logical event; identical text without a stable ID is not sufficient.

## `terminal.json`

```json
{
  "schema": "sregym.assistant_v3.terminal.v1",
  "outcome": "completed",
  "session_id": "<server session id or null>",
  "final_text": "<submitted diagnosis or null>",
  "submitted": true,
  "submission_count": 1,
  "resolved_model": "<reported model or requested model only when server confirms>",
  "resolved_reasoning": "<reported reasoning or requested reasoning only when server confirms>"
}
```

Allowed outcomes are `completed`, `configuration_error`, `authentication_error`, `permission_error`, `transient_exhausted`, `incomplete_stream`, `assistant_error`, `ambiguous_completion`, `capability_policy_violation`, `telemetry_scope_violation`, and `infrastructure_invalid`. Only `completed` may have `submitted: true`, and then `submission_count` must equal one.

## `run_metadata.json`

Must include:

- schema version, canonical agent name, agent version when known;
- run/attempt identity, case and attempt after trusted publication;
- benchmark profile and `comparable` boolean;
- capability profile exactly `splunk_o11y_read_only_no_direct_kubernetes` for a valid primary run;
- requested and resolved agent model/reasoning;
- judge model/backend, stored separately;
- prompt provenance and hashes;
- observability provider name, chart version, HEC index, logs connection ID, readiness report;
- UTC attempt/agent start and end times; and
- terminal/failure classification.

No token, raw Authorization header, HEC endpoint query string, telemetry payload body, oracle, or ground-truth field is allowed.

## `observability/delivery.json`

Contains the serialized `DeliveryReport` from `contracts/observability-provider.md`: opening and closing four-signal readiness, first-visible lag, collector sent/send-failed/enqueue-failed deltas, queue high-water/final sizes, drain status, and overall validity. Missing required counters are `null`; the only exception is a documented sparse Collector failure counter whose absent series has an initial value of zero and whose matching sent and queue series are both present in the same successful snapshot. It contains counts and safe identifiers only, not signal payloads.

An invalid post-execution delivery report changes the attempt classification to `infrastructure_invalid` and sets `included_in_diagnosis_pass_rate: false`; it does not remove the Assistant stream, diagnosis, or judge result.

## `splunk_lite_delivery.json`

Optional, atomically written after a registered Lite attempt. Stores the exact incident UTC window, opaque run ID, configured Logs connection, namespace, and bounded count/status for metrics, traces, container logs, Kubernetes events, pod objects, and event objects. It never stores returned payload bodies, tokens, or backend exception text. `present` means a representative same-window query found a result; it is **not** root-cause proof or exhaustive delivery assurance. `missing` and `query_error` remain distinct. `source_comparison` and `oracle_evidence` remain `unverified` until their separate case-specific reviews. The scorecard links the file and hashes it without altering the benchmark score.

## `splunk_lite_pre_agent.json` and secondary assessment

The shared verifier checks pre-agent evidence from each case's reviewed manifest. Its oracle-aware result stays in runner memory (or a separate trusted directory) while Assistant runs: the agent container mounts its run-artifact directory as `/logs`, so writing this proof there before agent exit would leak the answer. Only after the agent has exited is `splunk_lite_pre_agent.json` atomically published into the run directory. It contains the frozen window, destination identity, manifest version/hash, representative signal results, and each causal check's fact ID, sanitized source and Splunk queries, bounded count/timestamp/expected-pattern match, status (`present`, `missing_at_source`, `missing_in_splunk`, `query_error`, or `unverified`), and `essential|full_oracle_only` classification. Raw telemetry payloads, secrets, oracle answer text, and full API responses are never persisted or passed to Assistant. A symptom-guided result without a matching `ready` proof is not a valid scored result.

`splunk_visible_assessment.json` is produced only after the unchanged benchmark judge. It records a separately versioned rubric, judge backend/model, the reviewed set of verified reachable facts, raw secondary judge output/provenance, score or `unverified`, visibility classification, missing facts, and a concrete path to close each gap (approved object receiver addition versus specific Kubernetes/API access). It cannot replace or mutate the benchmark judge file. The scorecard links both raw judge outputs, the raw saved Assistant final answer, request, native/normalized trace, and these proofs; absent fields remain `unverified`.

## `metrics.json`

```json
{
  "schema": "sregym.assistant_v3.metrics.v1",
  "agent_duration_ms": 1234.5,
  "time_to_first_event_ms": 120.0,
  "input_tokens": 100,
  "output_tokens": 25,
  "reasoning_tokens": null,
  "total_tokens": 125,
  "tool_calls": 3,
  "failed_tool_results": 1,
  "terminal_outcome": "completed"
}
```

- First event excludes `ping`.
- Token fields come only from the terminal aggregate `assistant.usage`; absent values are `null`, never zero.
- `tool_calls` counts unique root `tool.use` IDs plus unique subagent action IDs that represent a tool call. Forwarded compatibility copies with the same owner ID count once.
- `failed_tool_results` counts unique explicit `is_error: true` results. Error-looking text is not inferred.
- `agent_duration_ms` spans request start through terminal event/failure and remains available for failed streams.

## `failure.json`

Contains schema, classification, safe message, last sequence, retry count, provider/agent phase, and cleanup status. It contains no traceback by default and no secret-bearing request/response body. Infrastructure-invalid attempts are marked `included_in_diagnosis_pass_rate: false`; agent failures remain visible according to existing SRE Gym result semantics.

## ATIF Mapping

- Request prompt → first user step.
- `message.delta`/`message.thinking`/`assistant.progress` → ordered assistant content blocks supported by ATIF; unsupported display metadata goes under namespaced `extra.assistant_v3`.
- `tool.use` → tool call with stable call ID/name/input.
- `tool.result` → observation linked to the exact call ID; preserve `is_error`.
- Subagent started/delta/action/complete → subagent trajectory/reference information without flattening away order.
- `assistant.usage` → trajectory/final metrics when fields are present.
- `message.complete` → final assistant response; do not append text already represented as deltas twice.
- `assistant.error` and incomplete terminal states → error metadata; no fabricated assistant answer.

The adapter validates contiguous ATIF step IDs, tool-result references, one final response at most, agent/model metadata, and schema version. Two conversions of the same source files must serialize to byte-identical `trajectory.json`.

## Existing Result Compatibility

Existing judge outputs and per-attempt CSV remain authoritative. New summary fields may be flattened into the existing snapshot only when namespaced (`assistant_v3.*` or `observability.*`). Current trace post-processing and SQLite ingestion remain non-fatal if an incomplete stream cannot form a valid ATIF trajectory; `failure.json` and source events must still publish.
