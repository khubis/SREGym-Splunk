# Contract: SRE Gym Assistant Driver ↔ Assistant v3

## Configuration

| Input | Source | Rule |
|---|---|---|
| Assistant base URL | `ASSISTANT_V3_URL` | Required HTTPS URL, except loopback HTTP in local development; the exact Docker host alias is loopback-equivalent after container rewriting |
| Assistant bearer token | `ASSISTANT_V3_AUTH_TOKEN` | Required; sent only in `Authorization: Bearer ...` |
| Splunk access token | `SF_TOKEN` | Required; sent only as `X-SF-TOKEN` |
| Agent model | `AGENT_MODEL_ID` | Required and sent explicitly |
| Reasoning | `AGENT_REASONING_EFFORT` | Required; one of `low`, `medium`, `high` |
| Prompt profile | `ASSISTANT_V3_PROMPT_PROFILE` | Defaults to immutable `sregym-stratus-diagnosis-v1` |

The `assistant_v3` registration sets `kubernetes_access: false` and `sregym_mcp_access: false`. The launcher must therefore omit the kubeconfig mount/environment and MCP URL/filtered-egress rule for both preflight and run containers. Both registration fields default to `true` for every existing agent. The container credential allowlist adds only the URL and two tokens above. `JUDGE_*`, HEC token, and provider deployment credentials are not Assistant inputs.

An optional host `SSL_CERT_FILE` is a runner/judge transport input, not an Assistant credential. When present for an open-network CLI judge, the runner accepts only a readable regular non-symlink file, mounts that exact file read-only, and points standard TLS client variables at the container path. It never disables certificate verification or mounts a host trust directory.

## Preflight

Preflight occurs before the expensive attempt and must:

1. validate required variables without printing values;
2. validate URL scheme/host and model/reasoning presence;
3. call an authenticated read-only Assistant endpoint to prove connectivity/authentication;
4. distinguish terminal 400/401/403 from transient 408/429/5xx/timeouts; and
5. fail if the requested model is rejected—never omit it and use a server default.

Preflight does not open an Assistant model turn.

## Request

```http
POST {ASSISTANT_V3_URL}/v2/assistant/sessions
Accept: text/event-stream
Content-Type: application/json
Authorization: Bearer <ASSISTANT_V3_AUTH_TOKEN>
X-SF-TOKEN: <SF_TOKEN>
X-Request-ID: <UUID derived deterministically from the anonymous run id>
```

```json
{
  "prompt": "<exact rendered immutable profile>",
  "action_instructions": "Telemetry time window: <started_at> through <ended_at>, inclusive. Investigate using only telemetry within this time window.<optional versioned symptom sentence>",
  "session_id": null,
  "model": "<AGENT_MODEL_ID>",
  "reasoning": "<AGENT_REASONING_EFFORT>"
}
```

`surface` is omitted. `action_instructions` is separate from, and does not alter, the frozen benchmark prompt. The baseline profile contains only the inclusive UTC start and end timestamps plus a direction to stay within them. The separately named symptom-guided exploratory profile appends exactly one reviewed user-observable symptom, with its independent source recorded in the public case recipe; it is not prompt-parity-comparable. Neither profile contains a run identity, namespace, canonical problem ID, mechanism, fix, oracle, grading material, HEC endpoint/token, or internal SRE Gym URL. Assistant tools are not modified or constrained to use an internal run identity. The driver persists the exact selected profile, instructions, and rendered hash before execution, without requiring a manual preview stop for every case.

## Stream Handling

- Parse SSE incrementally, including frames split across network chunks and multi-line `data:` fields.
- `ping` does not count as the first meaningful event.
- Persist each parsed event in receive order before acting on it.
- Retry only failures occurring before the first non-`ping` event. Maximum retries and backoff are bounded; `Retry-After` is capped.
- Once a non-`ping` event is observed, a disconnect/error is terminal and never starts a replacement session automatically.
- `assistant.error` is terminal failure.
- Exactly one `message.complete` is accepted. Multiple conflicting completions, invalid JSON, EOF before completion, or blank final text are ambiguous failures.
- Diagnosis text is `message.complete.final_text`; use `text` only when `final_text` is absent, matching the public API contract.
- `assistant.usage` is authoritative when present. Missing fields remain `null`.
- Any direct Kubernetes connector/tool event is preserved and marks `capability_policy_violation`; the attempt is not labeled Splunk-only comparable.

## Conductor Interaction

1. Fetch `/get_app` once and use only `app_name`, descriptions, and namespace(s) in prompt rendering.
2. Confirm `/status` expects the `diagnosis` stage before starting.
3. Start one fresh Assistant session.
4. On one valid completion, validate the recorded tool events against the execution scope, then `POST /submit` exactly once using the current Conductor diagnosis schema and the completed natural-language diagnosis.
5. Do not submit on preflight, provider, HTTP, parsing, stream, Assistant, blank-output, or capability-policy failure.
6. A retry of `/submit` is permitted only if the Conductor contract provides an idempotency/accepted response that proves it cannot duplicate evaluation; otherwise preserve ambiguity and stop.

## Prompt Profile Contract

`upstream-stratus-v1.yaml` is an exact snapshot with recorded source path, source commit, and SHA-256. `diagnosis-v1.yaml` contains:

```yaml
profile_id: sregym-stratus-diagnosis-v1
reference_sha256: <sha256>
substitutions:
  - id: <stable capability-only id>
    source_exact: <non-empty exact span>
    replacement: <possibly empty span>
    reason: <unavailable capability>
```

Rendering fails unless the reference hash matches and every `source_exact` occurs exactly once. Substitutions are applied in listed order. Only `{app_name}`, `{app_description}`, `{app_namespace}`, and any retained upstream non-diagnostic control value may be filled afterward. No unresolved placeholder is allowed.

The immutable v1 substitution IDs cover only direct Kubernetes enumeration, mitigation-stage wording in a diagnosis-only run, orchestrator submission mechanics, and Stratus round/tool-control mechanics. The renderer emits the final text and a provenance object containing all hashes and IDs.

## Failure Classification

| Condition | Classification | Retry |
|---|---|---|
| missing/invalid config or rejected model | `configuration` | no |
| 401 | `authentication` | no |
| 403 | `permission` | no |
| pre-stream 408/429/5xx/timeout | `transient_exhausted` after bound | bounded |
| malformed event / EOF / disconnect after work | `incomplete_stream` | no |
| `assistant.error` | `assistant_error` | no |
| empty/conflicting completion | `ambiguous_completion` | no |
| forbidden Kubernetes tool observed | `capability_policy_violation` | no |
| explicit absolute tool timestamp outside the supplied window | `telemetry_scope_violation` | no |

All messages are passed through known-secret redaction and contain status/category, not response headers or bodies that can expose credentials.
