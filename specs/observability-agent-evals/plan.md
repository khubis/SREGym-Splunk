# Technical Plan: Observability-Backed Assistant v3 Evaluations

Spec: `./specs/observability-agent-evals/spec.md`

## Technical Approach

### 1. Add one optional observability-provider lifecycle

Introduce a deliberately small provider contract with `preflight`, `prepare_attempt`, `wait_until_queryable`, and `finish_attempt`. `main.py` owns campaign/attempt orchestration; `Conductor` invokes preparation after stale application cleanup and before its telemetry stack and baseline are deployed. The default `none` provider is a no-op, preserving every existing agent path (R1, R3, R7).

Each attempt receives a cryptographically random `anon_<32 hex>` identity before deployment. The identity is shared by `RunArtifacts` and the provider, but the real problem ID is never given to the provider or agent-owned artifact environment. The provider adds this identity as `k8s.cluster.name`/`sregym.run.id` to exported signals. `RunArtifacts.create` accepts the preallocated identity after validating its shape and rejecting collisions, so deploy/readiness failures and successful runs use the same identity without overwriting prior attempts (R1, R3, R5, R7).

### 2. Implement Splunk as the first provider, not as runner-specific branching

The Splunk provider installs the official Splunk OpenTelemetry Collector Helm chart, pinned to `0.160.0` for reproducibility. Version `0.161.0` is intentionally not the v1 pin because it introduced Kubernetes semantic-convention breaking changes one day before this plan was written. Upgrades are separate reviewed changes.

The collector chart supplies Kubernetes metrics, container logs, and Kubernetes events. A generic optional fan-out in SRE Gym's existing central OTel collector preserves Jaeger and Prometheus while additionally:

- exporting the existing application traces over OTLP to the Splunk gateway; and
- scraping SRE Gym's central Prometheus federation endpoint and exporting application/span/Kubernetes metrics over OTLP.

The chart is configured from committed non-secret values plus a pre-created Kubernetes Secret. Tokens are passed through the Kubernetes API, never Helm command arguments, rendered values, prompts, or logs. `SF_TOKEN` remains the user/API query token while `SPLUNK_O11Y_INGEST_TOKEN` is the collector's least-privilege org ingest token. `SPLUNK_HOST` and `SPLUNK_HEC_PORT` form the HTTPS HEC endpoint; `SPLUNK_HEC_INDEX` defaults to `main`, must be allowed by the HEC token, and is recorded. TLS verification is on and has no v1 disable flag (R3, R7).

Before Assistant starts, the provider waits for collector rollout and polls Splunk for one metric, trace, container log, and Kubernetes event matching the opaque identity and attempt time window. It uses the same Splunk access context and resolves the Logs Observer connection with Assistant's rule: accessible default first, otherwise first accessible connection. Success produces a per-signal readiness report; any missing signal yields `infrastructure_invalid` and prevents `AgentLauncher.ensure_started`. Transport 429/5xx/timeouts retry with bounded exponential backoff and jitter; 400/401/403 fail immediately. No readiness check invokes an LLM (R3, R6, R7).

At attempt end, the provider takes a second end-to-end snapshot, reads the collector's sent/send-failed/enqueue-failed/queue-size counters, and waits for queues to drain within a fixed deadline before teardown. A delivery report records first-visible lag per signal, counter deltas, queue high-water/final size, and drain status. Any exporter/enqueue failure, missing closing signal, or undrained queue marks the attempt infrastructure-invalid for diagnosis-rate aggregation while preserving the Assistant and judge artifacts. This gives a strong bounded delivery guarantee without claiming record-for-record completeness (R3, R5, R6, R7).

### 3. Register Assistant v3 as an ordinary SRE Gym agent

Add `assistant_v3` to `agents.yaml`. Its driver uses the current Conductor `/get_app`, `/status`, and `/submit` contracts and calls the existing Assistant `POST /v2/assistant/sessions` SSE endpoint. The request always sets `session_id: null`, sends the CLI-selected model and reasoning explicitly, and omits `surface`, thereby using the existing product-default surface. No Assistant-repository production file or benchmark-specific surface is added (R1, R4).

The agent registry gains two backward-compatible capability flags, `kubernetes_access` and `sregym_mcp_access`, both defaulting to `true`. Assistant sets both to `false`, so its isolated client container receives neither the SRE Gym kubeconfig nor MCP URL/egress rule; existing agents are unchanged. The adapter forwards only its API URL, Assistant bearer token, and Splunk access token. The initial org must also have the expected Splunk Logs connection and no direct Kubernetes connector for comparable runs. A forbidden direct-Kubernetes tool event is retained and marks the result `capability_policy_violation`/non-comparable rather than being silently scored as a Splunk-only run (R4).

Primary v1 commands explicitly use `gpt-5.6-luna` with `medium` reasoning, matching the current Assistant product default found in the Assistant repository. The adapter remains model-agnostic. `--judge-model` and `--judge-backend` are mandatory for Assistant comparison runs and persisted separately; no agent or judge model may inherit a misleading default (R4, R6).

### 4. Make prompt parity executable, not a review convention

Freeze `clients/stratus/configs/diagnosis_agent_prompts.yaml` from baseline commit `3f6bd041231715184f962cedb33cfbcefa9b5b3d` as the `stratus-diagnosis-v1` reference. The Assistant profile stores versioned exact-match substitutions rather than a hand-edited second prompt. The renderer verifies the reference SHA-256, requires every source span to occur exactly once, applies only the approved substitutions, then substitutes the public app name, description, and namespace from `/get_app` (R2).

The approved substitutions are limited to:

1. removing the instruction to enumerate pods/deployments directly;
2. removing the mitigation-stage description from a diagnosis-only run;
3. replacing orchestrator/`submit_tool` mechanics with returning a natural-language diagnosis; and
4. removing Stratus-specific round, forced-step, thinking/tool-call protocol text.

Everything else remains byte-for-byte and in order. The rendered prompt, profile ID, reference commit/hash, substitution IDs, and rendered hash are persisted. A reference or substitution change requires a new profile ID (R2, R8).

### 5. Preserve the native stream, then normalize deterministically

The driver writes a versioned JSONL event stream under the current opaque artifact directory. Records preserve SSE order, event name, server event ID when present, payload, and monotonic offset from request start. Known credential values are redacted before persistence, including across chunk boundaries. The driver submits exactly one `message.complete.final_text` (falling back to `text` only per Assistant's public contract); EOF, `assistant.error`, blank completion, or conflicting terminal events never submit (R4, R5, R7).

A dedicated ATIF adapter maps the saved stream to the existing validated trajectory model. Stable source IDs deduplicate repeated transport events while preserving distinct calls, results, subagent events, errors, and usage. A deterministic metrics pass over the same JSONL produces duration, first non-keepalive event latency, reported token counts or `null`, unique tool calls, explicit tool errors, and terminal outcome. Reprocessing atomically replaces derived files and is byte-stable (R5, R6).

### 6. Deliver in test-first, cherry-pickable slices

For each implementation slice, add and run the focused test first and record the expected failure, then add production code; committed checkpoints remain green. Reusable commits come first and contain no Assistant imports. Cisco-specific commits consume only the documented contracts, allowing the provider/OTLP work to be upstreamed independently (R9).

Coverage is enforced with dev-only `pytest-cov` and `diff-cover`: 100% statement and branch coverage for every new feature module, plus 100% changed-line coverage against `upstream/main`. Every changed branch in legacy integration files must have a focused regression test. Generated/vendored files are excluded; live credentials are never required for unit coverage (R9).

## System Boundaries

- `[runner]` SRE Gym CLI, attempt lifecycle, model/judge validation, artifact publication.
- `[infra/reusable]` Provider contract, Splunk collector deployment, OTLP/Prometheus fan-out, readiness/failure taxonomy.
- `[agent/cisco]` Assistant v3 HTTP/SSE client, prompt renderer, Conductor submission driver.
- `[traces/reusable]` ATIF dispatch and deterministic post-processing; the Assistant event mapping is agent-specific.
- `[external]` Splunk Observability Cloud, Splunk Platform HEC/Logs Observer, and the existing Assistant v3 session API.
- `[assistant repo]` Read-only contract reference only. No v1 source changes.

## File Tree

### New Files

```text
sregym/observability/
├── __init__.py                         [infra/reusable] Provider factory
├── base.py                             [infra/reusable] Context, results, errors, null provider
└── splunk.py                           [infra/reusable] Helm deployment and query readiness
sregym/observer/splunk/values.yaml      [infra/reusable] Non-secret pinned chart overrides

clients/assistant_v3/
├── __init__.py                         [agent/cisco]
├── client.py                           [agent/cisco] Authenticated SSE client and retry policy
├── driver.py                           [agent/cisco] Conductor-to-Assistant orchestration
├── prompt.py                           [agent/cisco] Frozen profile validation/rendering
└── prompts/
    ├── upstream-stratus-v1.yaml        [agent/cisco] Exact upstream snapshot
    └── diagnosis-v1.yaml               [agent/cisco] Versioned allowed substitutions

atif_converter/adapters/assistant_v3.py [traces] Assistant JSONL to ATIF mapping
docs/assistant-v3-evaluations.md         [docs] Preflight, one case, Lite, resume, spot check

tests/observability/test_base.py         [tests]
tests/observability/test_splunk.py       [tests]
tests/clients/test_assistant_v3_client.py [tests]
tests/clients/test_assistant_v3_driver.py [tests]
tests/clients/test_assistant_v3_prompt.py [tests]
tests/traces/test_assistant_v3_adapter.py [tests]
tests/fixtures/assistant_v3/             [tests] Secret-free success/failure SSE fixtures
```

### Modified Files

- `main.py` `[runner]` — provider selection/lifecycle, explicit Assistant campaign validation, readiness gate.
- `agents.yaml` `[runner]` — register `assistant_v3` only.
- `sregym/agent_registry.py`, `sregym/agent_launcher.py` `[runner]` — backward-compatible direct-Kubernetes/MCP capability flags and enforcement.
- `sregym/conductor/conductor.py` `[runner/infra]` — invoke provider preparation at the safe pre-deploy seam.
- `sregym/observer/otel_collector/otel_collector.py` and `otel-collector.yaml` `[infra/reusable]` — optional OTLP trace/metric fan-out; disabled output remains unchanged.
- `sregym/run_artifacts.py` `[runner]` — validated preallocated opaque identity.
- `sregym/service/container_runner.py` `[runner]` — enforce per-agent Kubernetes/MCP exposure and allowlist only required Assistant variables; continue stripping judge credentials.
- `atif_converter/adapters/__init__.py`, `atif_converter/converter.py`, `sregym/traces/convert.py` `[traces]` — register Assistant detection/dispatch.
- `pyproject.toml`, `uv.lock` `[tests]` — dev-only coverage tooling and package discovery for the new modules.
- Existing focused test files may receive regression cases where that is clearer than creating another file; no unrelated production module is in scope.

## Data Flow

1. CLI validates diagnosis-only Lite selection, explicit agent/judge models, credentials, Helm/Kubernetes access, Assistant connectivity, and provider configuration.
2. Runner allocates an opaque attempt identity and binds it to artifacts/provider metadata without exposing the problem ID.
3. Conductor removes leftovers; provider installs/upgrades the pinned collector with a pre-created Secret and the opaque identity.
4. SRE Gym deploys Prometheus, Jaeger, its OTel collector with optional fan-out, the app/workload, baseline, and fault through the unchanged lifecycle.
5. Provider polls each required Splunk signal. Failure records an infrastructure-invalid artifact set and skips agent launch.
6. Assistant driver calls `/get_app`, renders and records the frozen prompt, then opens one fresh explicit-model SSE session on the existing surface.
7. Driver persists redacted ordered events. A single valid terminal diagnosis is posted once to `/submit`; SRE Gym's existing judge evaluates it unchanged.
8. Provider performs the closing signal check, records collector counter deltas, waits boundedly for queue drain, and writes the delivery report. Existing cleanup then runs without retrying the agent.
9. Artifact publication canonicalizes the opaque ID. A failed delivery audit preserves Assistant/judge evidence but excludes the attempt from diagnosis-rate aggregation.
10. ATIF conversion and deterministic metrics run atomically; SQLite ingestion and current result browsing continue unchanged.

## Test and Requirement Traceability

| Requirement | Automated evidence |
|---|---|
| R1 | All registered Lite cases use one driver; diagnosis-only lifecycle/submission regression; disabled provider snapshot |
| R2 | Reference hash, exact-once substitution, preserved-order diff, rendering, oracle/hint rejection tests |
| R3 | Null provider regression; four-signal readiness; opaque identity; Secret/command redaction; TLS, collector-counter, and queue-drain tests |
| R4 | Request-shape, fresh-session, explicit model/reasoning, no-surface, no-kubeconfig, one-submit tests |
| R5 | SSE fixtures, partial artifact tests, ATIF schema/order/subagent/error tests, idempotent byte comparison |
| R6 | Golden metrics fixtures for usage present/missing, duplicate IDs, tool errors, terminal outcomes; judge metadata test |
| R7 | 429/5xx exhaustion, 400/401/403 fail-fast, mid-stream disconnect/no retry, cleanup/resume tests |
| R8 | CLI command tests and a scripted artifact spot-check using success plus invalid fixtures |
| R9 | 100% new-module branch coverage, 100% changed-line report, production-change-to-test review table |

Live acceptance starts with `edge_request_filter_cpu_saturation` and `readiness_probe_misconfiguration_social_network`, then expands to the Lite suite. The svelte profile is smoke-only and must persist `comparable: false`; the normal profile persists `comparable: true`.

## Constraints & Boundaries

### ✅ Always

- Keep the work on `feature/observability-agent-evals` in this existing clean worktree.
- Add a failing focused test before each behavior, then implement only enough to pass it.
- Use one opaque identity and one prompt/model/judge record per attempt.
- Preserve existing SRE Gym telemetry, scoring, artifact layout, and default commands when the provider is disabled.
- Use safe argument arrays/Kubernetes clients; never interpolate secrets into shell commands.
- Run focused tests, the full relevant suite, Ruff, Pyright, ATIF validation, and coverage gates before each commit.
- Keep reusable provider commits free of imports from `clients/assistant_v3` or the Assistant repository.

### ⚠️ Ask First

- Any runtime dependency beyond the pinned Helm chart; this plan adds only dev coverage tools.
- Any Assistant-repository source change, new Assistant surface, or new model-visible capability.
- Any change to scenarios, prompts outside the named profile, judge rubric/threshold, or existing agent behavior.
- Any additional telemetry credential or relaxation of TLS verification.
- Any production file not listed above.

### 🚫 Never

- Pass a kubeconfig, SRE Gym MCP URL, scenario ID, fault, oracle, or grading text to Assistant.
- Put tokens in Helm values, command lines, prompts, event artifacts, normalized traces, or test fixtures.
- Retry an Assistant session after any non-keepalive response event has been received.
- Submit partial/ambiguous output, silently fall back to a different model, or count infrastructure-invalid attempts as diagnosis failures.
- add Datadog/Dynatrace abstractions, new scenarios, mitigation, UI, leaderboard publication, or broad runner refactors in v1.

## Dependencies

- Existing Python 3.12, `httpx`, `pydantic`, `PyYAML`, Kubernetes, Helm, and ATIF libraries already present in the repository.
- Official [Splunk OpenTelemetry Collector Helm chart](https://github.com/signalfx/splunk-otel-collector-chart), pinned to `0.160.0`. The chart supports a caller-created Secret and adds `k8s.cluster.name` to metrics, traces, and logs.
- Existing Assistant v3 SSE API (`POST /v2/assistant/sessions`) and existing SRE Gym Conductor API.
- Dev-only: `pytest-cov` and `diff-cover`, locked in `uv.lock`; no new runtime Python package.

Interface details are normative in `contracts/observability-provider.md`, `contracts/assistant-v3.md`, and `contracts/artifacts.md`.
