# Technical Plan: Observability-Backed Assistant v3 Evaluations

Spec: `./specs/observability-agent-evals/spec.md`

## Technical Approach

### 1. Add one optional observability-provider lifecycle

Introduce a deliberately small provider contract with `preflight`, `prepare_attempt`, `wait_until_queryable`, and `finish_attempt`. `main.py` owns campaign/attempt orchestration; `Conductor` invokes preparation after stale application cleanup and before its telemetry stack and baseline are deployed. The default `none` provider is a no-op, preserving every existing agent path (R1, R3, R7).

Each attempt receives a cryptographically random `anon_<32 hex>` identity before deployment. The identity is shared by `RunArtifacts` and the provider, but the real problem ID is never given to the provider or agent-owned artifact environment. The provider adds this identity as `k8s.cluster.name`/`sregym.run.id` to exported signals. `RunArtifacts.create` accepts the preallocated identity after validating its shape and rejecting collisions, so deploy/readiness failures and successful runs use the same identity without overwriting prior attempts (R1, R3, R5, R7).

### 2. Implement Splunk as the first provider, not as runner-specific branching

The Splunk provider installs the official Splunk OpenTelemetry Collector Helm chart, pinned to `0.160.0` for reproducibility. Version `0.161.0` is intentionally not the v1 pin because it introduced Kubernetes semantic-convention breaking changes one day before this plan was written. Upgrades are separate reviewed changes.

The collector chart supplies Kubernetes metrics, container logs, and Kubernetes events. SRE Gym's existing Prometheus remains the authoritative application-metric scraper. The Splunk gateway performs one bounded application-only scrape of Prometheus's `/federate` endpoint rather than rediscovering or individually configuring application endpoints. Federation selects application-target series from the existing Prometheus catalog, excludes Kubernetes/infrastructure scrape jobs already covered by the chart, and adds the opaque run resource attributes plus a source marker. It must not use a hand-maintained metric-name allowlist, forward the unfiltered Prometheus catalog, or create a second Kubernetes-metrics path.

A generic optional fan-out in SRE Gym's existing central OTel collector preserves Jaeger and Prometheus while additionally exporting the existing application traces over OTLP to the Splunk gateway.

Metric readiness requires both a run-scoped Kubernetes metric from the chart and the standardized `probe_success` sentinel for the active application namespace, marked as originating from SRE Gym Prometheus. This verifies the actual source-to-Splunk path without maintaining per-application metric names. The delivery audit continues to use the gateway's sent/failure/queue counters, so missing application points, exporter failures, or an undrained metrics queue invalidate the attempt rather than silently reducing the agent's evidence.

The chart is configured from committed non-secret values plus a pre-created Kubernetes Secret. Tokens are passed through the Kubernetes API, never Helm command arguments, rendered values, prompts, or logs. `SF_TOKEN` remains the user/API query token while `SPLUNK_O11Y_INGEST_TOKEN` is the collector's least-privilege org ingest token. `SPLUNK_HOST` and `SPLUNK_HEC_PORT` form the HTTPS HEC endpoint; `SPLUNK_HEC_INDEX` defaults to `main`, must be allowed by the HEC token, and is recorded. TLS verification is on and has no v1 disable flag (R3, R7).

Before Assistant starts, the provider waits for collector rollout and polls Splunk for one metric, trace, container log, and Kubernetes event matching the opaque identity and attempt time window. It uses the same Splunk access context and resolves the Logs Observer connection with Assistant's rule: accessible default first, otherwise first accessible connection. Success produces a per-signal readiness report; any missing signal yields `infrastructure_invalid` and prevents `AgentLauncher.ensure_started`. Transport 429/5xx/timeouts retry with bounded exponential backoff and jitter; 400/401/403 fail immediately. No readiness check invokes an LLM (R3, R6, R7).

At attempt end, the provider takes a second end-to-end snapshot, reads the collector's sent/send-failed/enqueue-failed/queue-size counters, and waits for queues to drain within a fixed deadline before teardown. Since the collector continuously exports its own metrics, a latest one-minute sample may show one in-flight point even when the queue emptied between scrapes. A separate bounded queue-minimum query starts after workload quiescence and uses only ingestion-complete data; an observed zero for each exporter proves a drain without accepting an earlier zero. The delivery report records first-visible lag per signal, counter deltas, queue high-water/final size, post-stop minima, and drain status. Any exporter/enqueue failure, missing closing signal, or queue never observed empty after quiescence marks the attempt infrastructure-invalid for diagnosis-rate aggregation while preserving the Assistant and judge artifacts. This is a bounded delivery check, not record-for-record completeness (R3, R5, R6, R7).

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

### 7. Add a small fail-closed isolation envelope

Reuse the existing opaque run identity only for harness-level delivery verification rather than adding a replay or tenancy system. Do not expose it in Assistant instructions or modify Assistant tools to require it. Keep the frozen benchmark prompt byte-identical, but pass separate Assistant `action_instructions` containing only the inclusive UTC start and end of the incident telemetry window.

After Assistant completes and before `/submit`, scan recorded tool calls for explicit absolute timestamps outside that window. Reject such attempts as `telemetry_scope_violation`, preserve their artifacts, and exclude them from diagnosis scoring. Do not reject foreign run identifiers in tool output. Then run the first ten entries of `SREGYM_LITE_PROBLEMS` once each, sequentially, using the existing resume and reporting paths (R10).

### 8. Persist batch progress and separate pre-agent proof from post-grade interpretation

After every terminal attempt, append one fsynced JSONL progress record and atomically regenerate a concise Markdown scorecard from published artifacts. Each row contains status, the unchanged benchmark score/verdict/rationale, a separately labeled Splunk-visible score or `unverified`, golden-telemetry status, access gap/remedy, and relative links to exact request, raw final answer, both raw judge outputs, native/normalized traces, scripted proof, and delivery audit. Startup/resume rebuilds the same files, ignoring only a truncated final JSONL line, so a crash cannot erase earlier cases (R11).

The trusted case verifier may use oracle-side checks **before** Assistant starts to prove that the needed observations are in the source and Splunk. It retains sanitized queries, counts, timestamps, and status in runner memory while Assistant runs; the run directory is mounted as `/logs` in the agent container, so oracle-aware proof cannot be written there before agent exit. The verifier does not supply oracle facts to the prompt or agent tools. After the unchanged judge finishes, a reviewer interprets the proof against the oracle and records `confirmed|partial|missing|not_checked`; this never changes the benchmark score. The 21 local cases run sequentially under the svelte profile and remain explicitly non-comparable; full-profile comparison runs require a larger host (R1, R11).

### 9. Complete Lite evidence campaign (R12–R15)

First make the pinned chart's object collection explicit: `clusterReceiver.k8sObjects` contains only pod and event watches; disable the chart's separate event receiver, and verify the rendered ConfigMap, exporter, and least-privilege RBAC. Keep this generic provider change in its own commit. An internal collector allowlist informed the choice of objects; its endpoints and credentials are not copied.

Use `cases/splunk-lite/<registered-case-id>/` for small, reviewable case contracts: `prompt.yaml` contains public metadata, one independently observable symptom, and a named prompt profile; `ground_truth.yaml` maps oracle facts to executable source and Splunk checks, expected patterns, discriminating rationale, and `essential|full_oracle_only` status. All 21 cases use one shared typed CLI verifier and one schema validator, not 21 copied programs. This is an executable check *for every case* through its manifest. Its first layer checks representative metrics, traces, logs, pod objects, and event objects; its second layer checks causal evidence, because generic presence cannot prove solvability. Live sanitized proof belongs under gitignored attempt artifacts, not the source tree. Capture bounded source Prometheus/Jaeger/container/Kubernetes observations and compare them to same-window Splunk observations **before** the agent starts; report missing-at-source, missing-in-Splunk, query-error, and unverified separately. If source data was not captured, mark source comparison `unverified`, never infer success from a Splunk count. Separately audit collector counters/queues/lag. Unexpected absence of an essential causal observation stops a valid scored attempt; a predeclared inaccessible full-oracle fact can instead produce a clearly labeled `data_limited` run. A manually reviewed evidence map is mandatory before a case is called fully observable.

Establish baseline readiness before opening the incident window; freeze the start near fault injection and the end only after the fault is visible and bounded Splunk ingestion has settled. The next case cannot start until this window is closed and its source/collector state is cleaned up. Render the frozen benchmark-derived diagnosis body with the current inclusive UTC window in trusted `action_instructions`. For the operator pilot, add one reviewed, short user-observable symptom from `prompt.yaml` through a separately versioned symptom profile; do not leak mechanism, fix, oracle, case ID, or opaque run ID. This arm is non-parity, including when run on a full deployment profile. The time-only baseline remains separate. Save prompt/profile/hash automatically, without a per-case manual preview stop. After the unchanged judge grades the exact final answer, produce a separately versioned Splunk-visible assessment using only verified reachable evidence; neither assessment can change the official score. Per-case report rows link to prompt, answer, native/normalized trace, both judges, source/Splunk proof, and access-gap assessment.

The small `splunk-visible-rca-v1` secondary rubric assesses the saved answer against independently verified, Splunk-reachable facts in three areas: component localization, causal mechanism, and scope/impact. The assessment must cite the fact IDs it considered, mark an area `not_assessable` when its necessary facts were not verified, and return `unverified` rather than normalize a score when there is no meaningful causal path. Otherwise it reports a separate 0–100 score plus concise reasons and the raw secondary judge response/model/backend. This is a diagnostic score for the Splunk-only capability envelope, never a replacement for SRE Gym's original D1/D2/D3 judgment.

Execute 21 cases sequentially in registry order using the existing campaign ledger. Before each case, check remaining host memory/disk and Docker health; preserve partial artifacts and stop safely on resource pressure. Reuse completed, valid incident ingestion for repeated Assistant attempts only when the same scoped window and source/Splunk evidence are retained. After each case, show its saved final answer and scores, checkpoint first, then clean up and recheck resources. Svelte local runs and symptom-guided runs remain non-comparable for different reasons; move full-profile time-only comparisons to a larger host if 16 GiB cannot support them. Do not publish score aggregates until case-level evidence, scoring provenance, and comparability checks pass.

### One-case review gate: CronJob sidecar (not yet passed under the new workflow)

Use `cronjob_sidecar_blocks_completion_hotel_reservation` to validate P2/P3 before the other 20 cases. A proposed non-parity symptom is “A scheduled background task in Hotel Reservation is taking unusually long to finish.” It names a visible impact, not the sidecar, CronJob configuration, or fix; the case recipe must cite the pre-diagnosis observation that supports this wording. The actual UTC start/end are filled only after a stable baseline, fault injection, visible Job accumulation, and ingestion settling.

The case manifest's minimum causal checks are: (1) a pod object whose primary `archiver` terminated `Completed` while `fluent-bit-sidecar` remained `Running`, with both listed as regular containers; (2) at least two distinct affected Job owners or an independently queryable active-without-success Job trend, establishing recurrence rather than one transient pod. For each, the shared verifier checks the Kubernetes/source state and the same-window Splunk destination and stores only sanitized facts/counts. Generic metrics, traces, container logs, and pod/event-object presence are checked separately and do **not** substitute for those two causal checks. The exact CronJob `jobTemplate` is a full-oracle confirmation fact currently available via `kubectl get cronjob`, not through the approved pod/event object receiver; a later object-watch addition would need separate authorization.

| Example check | Source and destination proof | Why it matters |
|---|---|---|
| Delivery classes | Source traffic/telemetry plus run/window-scoped Splunk metrics, traces, container logs, pod objects, and event objects | Detects broad routing gaps, but does not establish this RCA. |
| Primary done, sidecar alive (essential) | Same affected pod in Kubernetes and Splunk pod-object JSON: `archiver=terminated/Completed`, `fluent-bit-sidecar=running`, both under regular `spec.containers` | Distinguishes a sidecar-held Job from an archiver still processing or failing. |
| Repeated unfinished Jobs (essential) | Source Jobs plus at least two distinct Job owners with that pod pattern in Splunk, or a verified active-without-success Job metric trend | Distinguishes recurring scheduler accumulation from a single transient pod. |
| CronJob template (full-oracle confirmation) | Source `kubectl get cronjob ... -o json`; currently no authorized CronJob object export | Confirms the exact faulty template; note this visibility gap without pretending it reached Splunk. |

The 2026-09-26 svelte pilot proves only the first pod-object pattern and six representative signal classes. Its original 0/100 score, saved answer, and judge remain untouched; the Job trend, fault-centered window, symptom profile, pre-agent causal gate, and secondary Splunk-visible score are not yet verified. The first conforming rerun must produce those artifacts and be recorded as the first case of the 21-case sequence; report its answer and checks, then continue without a manual prompt-approval pause if all gates pass.

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
docs/assistant-v3-lite-evidence-map.md   [docs] Code-derived case evidence and access-gap map
sregym/results/assistant_v3_campaign.py  [agent/cisco] Durable provenance and dual-score scorecard
sregym/results/splunk_lite_cases.py      [agent/cisco] Validated public prompt recipes
sregym/results/splunk_lite_causal.py     [agent/cisco] Bounded source/Splunk causal checks
sregym/results/splunk_lite_evidence.py   [agent/cisco] Representative delivery and case proof artifacts

tests/observability/test_base.py         [tests]
tests/observability/test_splunk.py       [tests]
tests/clients/test_assistant_v3_client.py [tests]
tests/clients/test_assistant_v3_driver.py [tests]
tests/clients/test_assistant_v3_prompt.py [tests]
tests/traces/test_assistant_v3_adapter.py [tests]
tests/fixtures/assistant_v3/             [tests] Secret-free success/failure SSE fixtures
cases/splunk-lite/<registered-case-id>/  [case contracts] 21 prompt/evidence manifests; no copied verifier code
tests/results/test_splunk_lite_cases.py   [tests] Registry, schema, and hint safety
tests/results/test_splunk_lite_evidence.py [tests] Source/Splunk causal gate and sanitized proof
tests/results/test_splunk_lite_causal.py  [tests] Case discriminators and bounded retry
tests/results/test_assistant_v3_campaign.py [tests] Scorecard integrity and provenance
tests/test_main_campaign_abort.py         [tests] Pre-agent gate and campaign abort integration
```

### Modified Files

- `main.py` `[runner]` — provider selection/lifecycle, explicit Assistant campaign validation, readiness gate.
- `agents.yaml` `[runner]` — register `assistant_v3` only.
- `sregym/agent_registry.py`, `sregym/agent_launcher.py` `[runner]` — backward-compatible direct-Kubernetes/MCP capability flags and enforcement.
- `sregym/conductor/conductor.py` `[runner/infra]` — invoke provider preparation at the safe pre-deploy seam.
- `sregym/observer/otel_collector/otel_collector.py` and `otel-collector.yaml` `[infra/reusable]` — optional OTLP trace fan-out; disabled output remains unchanged.
- `sregym/observer/jaeger/jaeger.py` `[infra/reusable]` — replace app-local Jaeger endpoints and restart only trace-emitting Deployments before workload traffic so clients resolve the centralized collector reliably without disrupting databases, caches, or service discovery.
- `sregym/run_artifacts.py` `[runner]` — validated preallocated opaque identity.
- `sregym/service/container_runner.py` `[runner]` — enforce per-agent Kubernetes/MCP exposure and allowlist only required Assistant variables; continue stripping judge credentials.
- `atif_converter/adapters/__init__.py`, `atif_converter/converter.py`, `sregym/traces/convert.py` `[traces]` — register Assistant detection/dispatch.
- `sregym/results/splunk_lite_evidence.py` `[runner/reusable]` — extend the existing six-signal presence checker with one manifest-driven causal source/Splunk gate; no case-specific Python branches.
- `sregym/results/assistant_v3_campaign.py` `[results]` — link the raw prompt, both judges, causal proof, and access-gap status in the crash-safe scorecard.
- `clients/assistant_v3/driver.py` and its named prompt assets `[agent/cisco]` — add the separately versioned symptom profile without altering the frozen baseline profile.
- `pyproject.toml`, `uv.lock` `[tests]` — dev-only coverage tooling and package discovery for the new modules.
- Existing focused test files may receive regression cases where that is clearer than creating another file; no unrelated production module is in scope.
- `tests/observer/test_jaeger.py` may be added for the redirect/restart contract; `tests/test_infrastructure_reuse.py` may receive the corresponding Conductor ordering regression.

## Data Flow

1. CLI validates diagnosis-only Lite selection, explicit agent/judge models, credentials, Helm/Kubernetes access, Assistant connectivity, and provider configuration.
2. Runner allocates an opaque attempt identity and binds it to artifacts/provider metadata without exposing the problem ID.
3. Conductor removes leftovers; provider installs/upgrades the pinned collector with a pre-created Secret and the opaque identity.
4. SRE Gym deploys Prometheus, Jaeger, its OTel collector with optional trace fan-out, the app/workload, baseline, and fault through the unchanged lifecycle; the Splunk gateway federates application-only series from the existing Prometheus service while its chart receivers continue to own Kubernetes metrics.
5. After baseline readiness, record the fault-centered start, inject the fault, observe its visible symptom, wait for bounded indexing, and freeze a non-overlapping end. Provider polls required Splunk signals.
6. The shared case verifier executes representative and case-causal source/Splunk checks within the frozen window. It stores sanitized harness-only proof. An unexpected missing essential signal prevents a valid scored launch; an explicitly mapped inaccessible fact marks the attempt `data_limited`.
7. Assistant driver calls `/get_app`, renders and records the chosen frozen-body prompt with window and, for the separately labeled exploratory arm, one reviewed symptom; it opens one fresh explicit-model SSE session on the existing surface.
8. Driver persists redacted ordered events. A single valid terminal diagnosis is posted once to `/submit`; SRE Gym's existing judge evaluates it unchanged.
9. Provider performs the closing signal check, records collector counter deltas, waits boundedly for queue drain, and writes the delivery report. A separate Splunk-visible judge considers only reviewed, verified available evidence and never changes the benchmark result.
10. Artifact publication canonicalizes the opaque ID. Failed delivery preserves Assistant/judge evidence but excludes the attempt from valid diagnosis-rate aggregation. ATIF conversion and deterministic metrics run atomically.
11. The runner checkpoints the linked scorecard, reports that case's final answer and both score statuses, then cleans up and checks resources before advancing. A post-grade reviewer may add oracle interpretation without editing the original score.

## Test and Requirement Traceability

| Requirement | Automated evidence |
|---|---|
| R1 | All registered Lite cases use one driver; diagnosis-only lifecycle/submission regression; disabled provider snapshot |
| R2 | Reference hash, exact-once substitution, preserved-order diff, baseline rendering and hint rejection; separate versioned symptom-profile safety/provenance tests |
| R3 | Null provider regression; four-signal readiness; opaque identity; Secret/command redaction; TLS, collector-counter, and queue-drain tests |
| R4 | Request-shape, fresh-session, explicit model/reasoning, no-surface, no-kubeconfig, one-submit tests |
| R5 | SSE fixtures, partial artifact tests, ATIF schema/order/subagent/error tests, idempotent byte comparison |
| R6 | Golden metrics fixtures for usage present/missing, duplicate IDs, tool errors, terminal outcomes; judge metadata test |
| R7 | 429/5xx exhaustion, 400/401/403 fail-fast, mid-stream disconnect/no retry, cleanup/resume tests |
| R8 | CLI command tests and a scripted artifact spot-check using success plus invalid fixtures |
| R9 | 100% new-module branch coverage, 100% changed-line report, production-change-to-test review table |
| R11–R15 | Fail-first case-manifest/schema tests; shared verifier source/Splunk query and missing/error tests; frozen-window non-overlap and symptom-profile tests; oracle isolation; scorecard provenance/rebuild and dual-grade separation tests; one live case reviewed before suite execution |

Live acceptance for the new workflow starts with one reviewed CronJob case, then expands only after its scripted evidence gate and prompt/scorecard provenance are checked together. The svelte deployment profile is resource-reduced, and the symptom-guided prompt profile is non-parity; both distinctions must persist independently. A normal deployment profile alone does not make a symptom-guided run leaderboard-comparable.

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
