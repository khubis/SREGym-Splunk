# Tasks: Observability-Backed Assistant v3 Evaluations

Spec: `./specs/observability-agent-evals/spec.md`
Plan: `./specs/observability-agent-evals/plan.md`
Contracts: `./specs/observability-agent-evals/contracts/`

## Scope Guard

Only files listed in `plan.md` may be changed. Every production change must cite at least one requirement and focused test in its commit body or pull-request checklist. A task begins with a focused failing test, confirms that it fails for the intended missing behavior, and only then adds production code. Commits must be green; the observed red test is recorded in the task/PR notes rather than committed as a broken checkpoint.

No task may modify an SRE Gym scenario, oracle, rubric, pass threshold, workload, fault injector, Assistant-repository file, or Assistant surface. Datadog/Dynatrace support, mitigation, dashboards, leaderboard publication, and broad runner refactors are not latent follow-ups inside these tasks.

## Parallelism and Dependency Map

```text
[quality] A
    ├── [provider] B ──> [telemetry] D ──> [splunk] E ──> [reliability] F
    ├── [security] C ───────────────────────────────┐
    ├── [prompt] G ───────────────┐                │
    └── [client] H ──> [artifacts] I ──> [ATIF] J │
                                  └───────────────> [integration] K
                                                     │
                                           [workflow/docs] L
                                                     │
                                                [live] M
```

- After A, B/C/G/H may proceed independently because they consume frozen contracts.
- D/E/F are sequential infrastructure work and remain independent of Assistant-specific code.
- I/J consume the event schema from H but do not depend on live services.
- K is the first task allowed to join reusable provider code to the Cisco-owned adapter.
- L and M occur only after K is green.

## [quality] Test and Coverage Foundation

- [x] **A. Establish the test-first and coverage gate** — Add dev-only `pytest-cov` and `diff-cover`, lock dependencies, and configure branch coverage without changing runtime dependencies. Add a traceability checklist mapping R1–R9 to test files. Record the clean pre-feature regression baseline, then prove the coverage command fails against one temporary uncovered branch before removing that temporary change. Final gates: 100% statement/branch coverage for new feature modules and `diff-cover coverage.xml --compare-branch upstream/main --fail-under=100`. Planned commit: `test(evals): establish observability integration coverage gates`.

### Task A verification evidence

- Tooling red test: before the dev dependencies were added, pytest rejected `--cov`, `--cov-branch`, and `--cov-fail-under` as unknown arguments.
- Coverage-gate red test: a temporary two-branch probe with only one branch exercised passed its functional test but failed `--cov-fail-under=100` at 67% coverage. The probe was then removed.
- Relevant pre-feature regression baseline: 58 tests passed across runner abort/resume, conductor phase/stage wiring, container hardening, and trace conversion.
- Full `pytest -m "not integration"` baseline cannot currently collect because of five pre-existing environment/test-layout failures: the train-ticket gateway requires a cluster, the flight-ticket runtime endpoint is absent, two duplicate test module names collide during collection, and `clients.test_k8s_agent` is absent. This task does not suppress or alter those tests.

Traceability is a live checklist: each row names the focused test locations that must verify the requirement before K2 can be checked off.

| Requirement | Planned automated test files | Covered |
|---|---|---|
| R1 | `tests/observability/test_base.py`; `tests/clients/test_assistant_v3_driver.py`; existing runner/conductor regression tests | [x] |
| R2 | `tests/clients/test_assistant_v3_prompt.py` | [x] |
| R3 | `tests/observability/test_base.py`; `tests/observability/test_splunk.py`; OTel collector rendering tests | [x] |
| R4 | `tests/clients/test_assistant_v3_client.py`; `tests/clients/test_assistant_v3_driver.py`; registry/launcher/container tests | [x] |
| R5 | `tests/clients/test_assistant_v3_client.py`; `tests/clients/test_assistant_v3_driver.py`; `tests/traces/test_assistant_v3_adapter.py` | [x] |
| R6 | `tests/clients/test_assistant_v3_driver.py`; `tests/traces/test_assistant_v3_adapter.py` | [x] |
| R7 | `tests/observability/test_splunk.py`; `tests/clients/test_assistant_v3_client.py`; runner resume/cleanup tests | [x] |
| R8 | Assistant v3 command/documentation tests; artifact spot-check fixtures | [x] |
| R9 | All focused files above; final new-module coverage and changed-line coverage gates | [x] |

## [provider] Reusable Provider Foundation — Upstream Candidate

- [x] **B. Add the optional provider contract and opaque attempt identity** — First add failing unit tests for the `none` provider, strict `anon_<32 hex>` validation, collision rejection, distinct resume identities, typed failure taxonomy, and secret-free serialization. Implement `sregym.observability.base`, the factory, and preallocated `RunArtifacts` identity support per `contracts/observability-provider.md`; default-disabled behavior must remain byte/behavior compatible. Requirements: R1, R3, R5, R7, R9. Planned commit: `feat(observability): add optional provider lifecycle`.

  Evidence: the focused suite first failed because `sregym.observability` did not exist. The completed suite has 19 passing tests with 100% statement/branch coverage for the new provider package. The 122-test affected regression matrix passes, including artifact publication, campaign abort/resume, conductor wiring, container hardening, deployment profiles, and trace conversion; changed production lines have 100% diff coverage.

## [security] Agent Capability Isolation — Upstream Candidate

- [x] **C. Enforce per-agent Kubernetes and MCP capabilities** — First add failing registry/launcher/container tests showing an agent with both capability flags disabled receives no kubeconfig mount/environment, MCP URL, MCP filtered-egress rule, or benchmark tool endpoint in preflight and execution; also prove existing registrations default to both capabilities enabled. Implement the two backward-compatible registration fields and register Assistant with both disabled. Requirements: R1, R4, R9. Planned commit: `feat(agents): declare kubernetes and mcp capabilities`.

  Evidence: seven fail-first tests initially failed because the registration/config fields and Assistant registration did not exist. The completed focused suite has eight passing tests. A 201-test affected regression matrix passes across registry, launcher, egress, endpoint, credential, hardening, audit, and campaign behavior; all changed production lines have coverage. Ruff and Pyright pass on every touched Python file.

## [telemetry] Generic OTel Fan-Out — Upstream Candidate

- [x] **D. Add optional central-collector trace fan-out** — First add manifest-rendering tests proving `None` produces the original Jaeger/spanmetrics/Prometheus pipelines and an external export adds exactly one OTLP exporter, resource attributes, and trace fan-out without embedding credentials. Do not duplicate the full Prometheus catalog into the external provider; Kubernetes metrics come from the provider collector. Implement safe structured manifest rendering and deployment using argument arrays/stdin rather than shell interpolation. Requirements: R3, R7, R9; contract: `observability-provider.md`. Planned commit: `feat(telemetry): add optional otlp export fanout`.

  Evidence: five focused tests first failed on the absent renderer and stdin-safe command interface. The completed affected matrix has 90 passing tests. The OTel collector module has 100% statement/branch coverage and the branch has 100% changed-line coverage; Ruff and Pyright pass. Disabled rendering is byte-identical to the committed manifest, while enabled rendering is credential-free and preserves every local pipeline/exporter alongside the added fan-out.

## [splunk] Splunk Provider — Upstream Candidate

- [x] **E. Deploy the pinned Splunk collector securely** — First add failing tests for required environment validation, HTTPS HEC construction, numeric port/index handling, chart version `0.160.0`, bounded resources, idempotent Helm operations, Secret creation through the Kubernetes API, TLS enforcement, and redaction of every supplied secret from commands/errors/artifacts. Implement only the Splunk provider deployment and non-secret values file; do not add Assistant imports. Requirements: R3, R7, R9. Planned commit: `feat(splunk): export sregym telemetry with otel`.

  Evidence: the focused suite first failed during collection because `sregym.observability.splunk` did not exist. The completed provider suite has 67 passing tests and 100% statement/branch coverage across the observability package. The official chart `0.160.0` renders successfully with the committed non-secret values, mounted Secret token references, verified HEC TLS, the expected OTLP gateway service, and one gateway replica. A 157-test affected regression matrix passes; Ruff and Pyright pass on every touched Python file. Commands, stdin values, public metadata, errors, and serialized provider artifacts contain neither of the supplied tokens.

- [x] **F. Implement end-to-end readiness and delivery assurance** — First add deterministic mocked tests for accessible logs-connection selection, four-signal opening/closing queries, attempt scoping, first-visible lag, 408/429/5xx retries, `Retry-After` caps, 400/401/403 fail-fast, retry exhaustion, collector sent/send-failed/enqueue-failed deltas, queue high-water/final size, drain success/timeout, missing-counter invalidation, and secret-free evidence. Implement `wait_until_queryable` and `finish_attempt` exactly as `observability-provider.md`; no LLM call is allowed. Requirements: R3, R5, R6, R7, R9. Planned commit: `feat(splunk): verify telemetry delivery and queue drain`.

  Evidence: the fail-first suite initially failed during collection because the delivery snapshot, reliability policy, backend error, and HTTP backend types did not exist. The completed observability suite has 141 passing tests with 100% statement/branch coverage. It exercises exact accessible-default logs connection selection, established Assistant V3 Logs Observer/APM GraphQL shapes and asynchronous job continuation, four-signal run/namespace/time scoping, safe destination evidence, bounded deadline-aware retry/backoff, opening/closing counter deltas, queue high-water/drain behavior, missing evidence invalidation, and secret redaction without any LLM call. A 754-test cross-feature regression matrix passes; Ruff and Pyright pass; changed production lines across the branch retain 100% diff coverage.

## [prompt] Assistant Prompt Parity — Cisco-Owned

- [x] **G. Freeze and mechanically derive the Assistant diagnosis prompt** — First add failing tests for reference SHA-256, source commit/path, exact-once substitutions, unchanged text/order outside approved spans, profile-version changes, allowed placeholders, unresolved placeholders, and rejection of case IDs, fault/oracle text, added service/time/symptom hints, or routing metadata. Add the exact Stratus v1 snapshot, substitution manifest, and renderer. Requirements: R1, R2, R8, R9; contract: `assistant-v3.md`. Planned commit: `feat(assistant-v3): add immutable sregym diagnosis prompt`.

  Evidence: the fail-first test initially failed during collection because `clients.assistant_v3.prompt` did not exist. The pinned snapshot exactly matches the loaded system/user/summary content at upstream commit `3f6bd041231715184f962cedb33cfbcefa9b5b3d`; both the source-file SHA-256 and combined starter-prompt SHA-256 are verified. Seven ordered substitutions remove only direct Kubernetes enumeration, Stratus tool/round mechanics, mitigation-only wording, downstream-stage coupling, and orchestrator submission mechanics. The focused suite has 56 passing tests with 100% statement/branch coverage, including unchanged-character ordering, exact-once replacement, immutable profile versioning, placeholder restrictions, and explicit rejection of case/fault/oracle/service/time/symptom/routing additions. A 213-test Stratus/client/trace regression matrix and an 810-test cross-feature matrix pass; the built wheel contains the renderer and both YAML assets without packaging warnings; Ruff and Pyright pass; branch changed-line coverage remains 100%.

## [client] Assistant API and Native Events — Cisco-Owned

- [x] **H. Build the authenticated Assistant SSE client** — First add secret-free fixtures and failing tests covering split chunks, multi-line data, pings, every relevant root/tool/usage/subagent event, `final_text` fallback, explicit model/reasoning, `session_id:null`, omitted surface, 401/403 fail-fast, bounded pre-stream retry, capped `Retry-After`, disconnect after work with no retry, `assistant.error`, malformed/blank/conflicting completion, credential redaction across chunk boundaries, and forbidden Kubernetes-tool detection. Implement connectivity preflight and streaming with existing `httpx`; do not add a runtime dependency. Requirements: R4, R5, R6, R7, R9. Planned commit: `feat(assistant-v3): add resilient session stream client`.

  Evidence: the fail-first suite initially failed during collection because `clients.assistant_v3.client` did not exist; a later focused red test also proved malformed UTF-8 was incorrectly treated as retryable before it was classified as an incomplete stream. The completed focused suite has 72 passing tests with 100% statement/branch coverage. It verifies the exact fresh-session request and headers, authenticated read-only preflight, all native event families, incremental split/multi-line SSE parsing, complete error cleanup capture, bounded pre-stream retry with capped `Retry-After`, no retry after meaningful work, unambiguous completion rules, secret redaction in payloads and event IDs across chunk boundaries, and direct-Kubernetes policy detection without rejecting Splunk-backed `o11y_k8s_*` tools. Ruff and Pyright pass, no runtime dependency was added, and the cross-feature plus changed-line coverage gates pass.

## [artifacts] Deterministic Results — Cisco-Owned

- [x] **I. Persist native artifacts and calculate deterministic metrics** — First add golden tests for `request.json`, ordered `events.jsonl`, `terminal.json`, `run_metadata.json`, `metrics.json`, `failure.json`, and `observability/delivery.json`; cover atomic overwrite, byte-identical reprocessing, duplicate stable IDs, distinct identical text, missing usage as `null`, unique tool/error counts, keepalive exclusion, partial streams, post-execution infrastructure invalidation, and secret scanning. Implement artifact writers/metric derivation per `artifacts.md`. Requirements: R4–R7, R9. Planned commit: `feat(assistant-v3): persist deterministic evaluation artifacts`.

  Evidence: the fail-first test initially failed during collection because `clients.assistant_v3.driver` did not exist. The completed focused suite has 58 passing tests and 100% statement/branch coverage. Golden fixtures prove exact, newline-terminated serialization for all seven native artifact paths; reprocessing is byte-identical, raw duplicate records are preserved, derived stable IDs are deduplicated, missing usage remains `null`, keepalives do not affect first-event latency, and partial streams remain diagnosable. Invalid post-execution delivery preserves the completed Assistant diagnosis while marking the attempt infrastructure-invalid and excluding it from diagnosis pass rate. Atomic replacement failures retain prior files, stale optional artifacts are removed safely, and configured secrets are rejected before any write. The 899-test affected regression matrix passes with one expected skip; Ruff and Pyright pass, the branch retains 100% changed-line coverage, and the built wheel contains the driver and prompt assets.

## [traces] ATIF Normalization — Separable Adapter Commit

- [x] **J. Convert Assistant event streams to validated ATIF** — First add failing fixtures/tests for format detection, request/user step, ordered assistant content, tool-call/result links, explicit errors, usage, subagent chronology, final response de-duplication, incomplete streams, schema validation, deterministic serialization, SRE Gym path metadata, and SQLite ingestion. Implement the adapter and minimal converter/dispatch registrations without changing existing adapters. Requirements: R5, R6, R9; contract: `artifacts.md`. Planned commit: `feat(traces): normalize assistant v3 trajectories`.

  Evidence: the fail-first test initially failed during collection because `atif_converter.adapters.assistant_v3` did not exist. The completed focused suite has 33 passing tests and 100% statement/branch coverage for the standalone adapter. It verifies content-based and explicit dispatch; strict companion/event schema validation; the exact request as the first user step; ordered deltas, thinking, progress, errors, and subagent events; exact tool-call/result links; stable-ID de-duplication; terminal usage; prefix-safe final-response de-duplication; partial streams without fabricated answers; embedded chronological subagent trajectories; deterministic `trajectory.json`; canonical SRE Gym metadata; and validated SQLite file ingestion/round-trip. The complete trace suite has 257 passing tests with one expected skip, and the 932-test affected regression matrix passes with one expected skip. Ruff and Pyright pass, the branch retains 100% changed-line coverage, and the built wheel contains the adapter.

## [integration] Runner and Driver Integration

- [x] **K. Wire the provider gate and Assistant driver into the existing lifecycle** — First add runner/driver integration tests with fake provider, fake Assistant SSE server, and fake Conductor proving: explicit diagnosis-only/model/reasoning/judge validation; provider preparation before baseline; readiness before agent launch; one fresh session; exact `/get_app` metadata only; exactly one valid `/submit`; no submission for invalid streams; closing delivery audit before teardown; infrastructure-invalid pass-rate exclusion; artifact publication for pre/post-agent failures; cleanup; resume; full/svelte comparability labels; and no behavior change for an existing agent with provider `none`. Then implement the smallest `main.py`/Conductor/driver/registry glue. Requirements: R1–R9. Planned commit: `feat(evals): run assistant v3 against sregym telemetry`.

  Evidence: fail-first tests initially failed on the absent Conductor provider hooks, Assistant orchestration types, campaign validation, and filtered Assistant endpoint rule. The finished lifecycle prepares external export after stale-app removal and before deployment/baseline, requires four-signal query readiness after fault injection and before launch, audits closing delivery before cleanup, and preserves the original zero-argument collector path for existing agents using provider `none`. The Assistant driver uses only `/status`, one `/get_app`, one fresh explicit-model/reasoning SSE session, and at most one diagnosis `/submit`; invalid streams and ambiguous submissions are preserved without submission/retry. Pre-agent and post-agent infrastructure failures publish secret-scanned artifacts, invalid delivery is excluded from diagnosis pass rate, full/svelte comparability is explicit, and each redacted event is durably spooled before terminal interpretation. The driver suite has 99 passing tests at 100% statement/branch coverage; 186 focused runner/provider/driver regressions and a 1,338-test affected matrix pass with one expected skip. Ruff and focused Pyright checks pass. The repository-wide collection and cross-module coverage commands remain intentionally deferred to K2; the unmodified suite currently has two known collection constraints outside this task (`clients.test_k8s_agent` is absent, and duplicate `test_kafka_producer_leak` module names collide under default import mode).

- [x] **K2. Run the complete offline verification matrix** — Run focused suites for every preceding task, all existing trace/runner/container tests, the full non-integration test suite, Ruff, Pyright, ATIF validation, secret scans, 100% new-module branch coverage, and 100% changed-line coverage. Fix only defects within the approved files; any required scope expansion pauses for approval. Produce the final requirement-to-test and changed-production-file-to-test table. Planned commit only if fixes are necessary: `fix(evals): close observability integration verification gaps`.

  Evidence: K2 added focused orchestration tests for previously unexecuted provider readiness, pre-agent publication, Assistant launch/finalization, provider shutdown, Conductor delivery-failure, and optional OTel-export branches; no production behavior changed. The final affected matrix has 1,386 passing tests with one expected skip. The full runnable non-integration matrix has 2,404 passing tests, three expected skips, and nine integration deselections. The exact new-module gates report 100% statement/branch coverage for `clients.assistant_v3`, `sregym.observability`, and `atif_converter.adapters.assistant_v3`; a stricter repository-instrumented report gives every changed production file 100% changed-line coverage. The complete trace/ATIF suite has 257 passing tests with one expected skip. Changed files pass Ruff and applicable Pyright checks, and a `detect-secrets` scan reports no candidates or tracked `.env` files.

  The unmodified upstream tree still prevents an unqualified repository-wide green command: three import/environment collection failures, four tests failing in untouched application/Kafka/kubectl-tool code, three Ruff findings in untouched files, and broad pre-existing Pyright debt. K2 does not suppress or repair those unrelated failures; the passing full matrix excludes only those named baseline files, and `--import-mode=importlib` avoids the upstream duplicate test-module-name collision.

- [x] **K3. Close credentialed live-runtime contract gaps before case execution** — Add fail-first coverage for Assistant's UUID request-header requirement, Docker Desktop's exact host alias, agent-image packaging of the observability runtime, capability restrictions before preflight, fork-local image selection, Codex subscription policy state, and explicit enterprise CA trust. Keep TLS verification enabled and mount only exact files. Requirements: R4, R7, R8; contracts: `assistant-v3.md`, `observability-provider.md`. Planned commit: `fix(evals): align live assistant v3 runtime contracts`.

  Evidence: focused tests failed independently for every missing behavior before implementation. The live Assistant container preflight reaches the standalone v3 endpoint after rewriting loopback to `host.docker.internal` and sends a deterministic UUID request header. The Codex judge preflight makes a real `gpt-5.6-luna` call with selected subscription auth, signed managed-policy caches, and a read-only macOS CA bundle; TLS verification remains enabled. The authenticated Splunk provider preflight resolves the Logs Observer connection with the same explicit trust bundle. The focused client/driver/service matrix has 290 passing tests; the broader affected matrix has 707 passing tests and restores 100% changed-line coverage. Ruff and Pyright pass for every touched Python file. The first live case remains the next gate.

- [x] **K4. Prevent observability collectors from competing for application host ports** — Keep the Splunk node agent for container log collection and the cluster receiver for Kubernetes events, but disable the node agent's OTLP, Jaeger, and Zipkin host ports so benchmark applications can schedule their own OTel agents. Requirements: R3, R7; contract: `observability-provider.md`. Planned commit: `fix(observability): avoid collector host-port collisions`.

  Evidence: the first cached full-profile deployment showed all three Astronomy Shop OTel agent pods unschedulable because the Splunk DaemonSet owned host port 4317. The fail-first values test rejected the original chart overrides. The corrected chart renders the Splunk agent and gateway with zero `hostPort` entries, while leaving file-log and Kubernetes-event collection enabled. All 141 observability tests pass and Ruff is clean.

### Final requirement-to-test traceability

| Requirement | Final automated evidence |
|---|---|
| R1 | `tests/test_main_campaign_abort.py`; `tests/conductor/test_phase_wiring.py`; `tests/test_deployment_profiles.py`; Lite selection regressions |
| R2 | `tests/clients/test_assistant_v3_prompt.py` reference hash, ordered substitutions, provenance, and forbidden-hint cases |
| R3 | `tests/observability/test_base.py`; `tests/observability/test_otel_collector.py`; `tests/observability/test_splunk.py`; `tests/test_infrastructure_reuse.py` |
| R4 | `tests/clients/test_assistant_v3_client.py`; `tests/clients/test_assistant_v3_driver.py`; `tests/service/test_agent_capabilities.py`; `tests/test_main_campaign_abort.py` |
| R5 | Assistant client/driver golden artifacts plus `tests/traces/test_assistant_v3_adapter.py` and the complete trace suite |
| R6 | Driver metric golden cases, delivery invalidation cases, ATIF usage/error mapping, and explicit judge metadata tests |
| R7 | Splunk retry/drain tests, Assistant stream retry/failure tests, campaign abort/resume, Conductor cleanup, and provider-close tests |
| R8 | CLI option/help test, deterministic artifact fixtures, tested operator workflow, and direct prompt/diagnosis/tool/delivery inspection coverage |
| R9 | Fail-first evidence in A–K, focused suites, secret scan, 100% new-module branch coverage, and 100% changed-production-line coverage |

### Changed production file-to-test traceability

| Production files | Focused automated tests |
|---|---|
| `agents.yaml`; `sregym/agent_registry.py`; `sregym/agent_launcher.py` | `tests/test_agent_registry.py`; `tests/service/test_agent_capabilities.py` |
| `sregym/service/container_runner.py` | `tests/service/test_agent_capabilities.py`; existing container, endpoint, hardening, credential, and internet-audit suites |
| `sregym/observability/__init__.py`; `sregym/observability/base.py`; `sregym/run_artifacts.py` | `tests/observability/test_base.py`; runner artifact and resume regressions |
| `sregym/observability/splunk.py`; `sregym/observer/splunk/values.yaml` | `tests/observability/test_splunk.py` |
| `sregym/observer/otel_collector/otel_collector.py` | `tests/observability/test_otel_collector.py`; `tests/test_infrastructure_reuse.py` |
| `sregym/observer/jaeger/jaeger.py` | `tests/observer/test_jaeger.py`; `tests/test_infrastructure_reuse.py` |
| `clients/assistant_v3/prompt.py`; immutable prompt assets | `tests/clients/test_assistant_v3_prompt.py` |
| `clients/assistant_v3/client.py` | `tests/clients/test_assistant_v3_client.py` |
| `clients/assistant_v3/driver.py` | `tests/clients/test_assistant_v3_driver.py`; `tests/test_main_campaign_abort.py` |
| `main.py` | `tests/test_main_campaign_abort.py`; `tests/test_deployment_profiles.py`; runner abort/resume and judge/container lifecycle suites |
| `sregym/conductor/conductor.py` | `tests/conductor/test_phase_wiring.py`; `tests/test_infrastructure_reuse.py`; existing conductor stage/submission regressions |
| `atif_converter/adapters/assistant_v3.py`; `atif_converter/converter.py`; `sregym/traces/convert.py` | `tests/traces/test_assistant_v3_adapter.py`; complete `tests/traces` suite and SQLite round-trip cases |

## [workflow/docs] Operator Experience

- [x] **L. Document reproducible commands and a no-code spot check** — Add tested documentation for prerequisites, environment names without values, preflight, one full-profile case, svelte non-comparable smoke, Lite suite, resume, artifact locations, delivery report interpretation, capability/model/judge metadata, and failure recovery. Provide a small read-only inspection command that prints prompt hash/text location, final diagnosis, tool/error totals, delivery validity/lag/drop/queue evidence, judge result, and failure classification without exposing credentials. Requirements: R6–R9. Planned commit: `docs(evals): add assistant v3 benchmark workflow`.

  Evidence: three documentation-contract tests first failed because the workflow guide did not exist. The completed guide provides copyable full-profile, explicitly non-comparable svelte, Lite-suite, and resume commands; identifies only environment-variable names; documents stable artifacts, comparability metadata, bounded delivery evidence, and failure recovery; and preserves the benchmark prompt, scenario, oracle, and judge behavior. Its embedded standard-library spot check is executed against both valid and infrastructure-invalid golden runs and reports prompt provenance, diagnosis, tool/error totals, delivery lag/failure/queue evidence, judge output, configuration, and failure classification without reading or printing credentials or modifying artifacts. The complete Assistant driver suite has 102 passing tests with 100% statement/branch coverage; Ruff and Pyright pass for the touched Python test file.

## [live] Credentialed Acceptance (No Product-Code Commit)

- [ ] **M. Validate two live Lite cases, then the suite gate** — With user-supplied gitignored credentials, run `edge_request_filter_cpu_saturation` and `readiness_probe_misconfiguration_social_network` using `--stages diagnosis --profile full --agent assistant_v3 --model gpt-5.6-luna --reasoning-effort medium` plus an explicit fixed judge model/backend and Splunk provider. For each, verify four opening/closing signals, zero send/enqueue failures, drained queues, reasonable recorded lag, exact prompt provenance, one Assistant session/submission, unchanged judge behavior, complete artifacts, and no forbidden capability. Run a svelte smoke only as non-comparable. Start the full Lite suite only after both full-profile cases pass; preserve failures rather than editing scenarios or prompts around them.

## [isolation/pilot] Cross-Run Isolation and Ten-Case Pilot

- [x] **N1. Tag delivery internally without changing agent behavior** — First add focused tests, then export the existing opaque run identity for harness-level ingestion and delivery verification only. Do not include it in Assistant instructions or require it in Assistant tool calls. Requirement: R10.
- [x] **N2. Give Assistant only a time window and fail closed** — First add focused tests, then send separate non-diagnostic `action_instructions` containing only the inclusive UTC start and end timestamps; before submission, reject an explicit absolute tool timestamp outside that window as `telemetry_scope_violation`. Preserve artifacts and do not reject identifiers found in tool output. Requirement: R10; contracts: `assistant-v3.md`, `artifacts.md`.
- [x] **N3. Verify one end-to-end local case** — The svelte `edge_request_filter_cpu_saturation` smoke exported all four signals, ran Assistant v3 to completion, preserved its final answer/trajectory, and recovered the unchanged judge result after fixing subsecond scope-window precision. The shared org contained unrelated K41/K45 telemetry; the completed diagnosis was graded as-is at 33/100 and remains non-comparable.
- [x] **N4. Add crash-safe batch reporting and post-grade telemetry audit** — First add failing tests for per-attempt checkpointing, deterministic Markdown rebuild, valid provenance links, truncated-ledger recovery, and proof that oracle-aware Splunk queries execute only after grading and never alter the score. Keep the report to one table plus linked raw artifacts. Requirements: R5–R8, R11.
  Evidence: focused tests failed before the reporting module existed, then passed with append-and-fsync JSONL checkpoints, atomic Markdown/CSV rebuilds, answer-to-judge identity validation, hash-backed provenance, and post-grade-only audit enforcement. The 176-test affected regression suite and Pyright pass. Live reconstruction preserved the existing 33/100 score and a subsequent exact-window Splunk query confirmed 169 causal filter-evaluation records without changing that score. Ruff was unavailable from the authenticated package index in this environment.
- [ ] **N5. Run Lite batch 1 (first ten cases)** — Run the first ten entries of `SREGYM_LITE_PROBLEMS` once each and sequentially with one workload at a time. After every case, publish artifacts, checkpoint the scorecard, perform the post-grade golden-telemetry audit, and verify memory/disk headroom before continuing. Resume rather than duplicate after interruption; preserve agent mistakes and infrastructure failures exactly as observed.
- [ ] **N6. Run Lite batch 2 (remaining eleven cases)** — Start only after reviewing batch 1 integrity and resource behavior. Use the identical model, judge, prompt profile, reporting contract, and audit procedure.

## [telemetry reliability] Trace Routing Correction

- [x] **O1. Make application trace routing reliable before workload traffic** — First add a failing lifecycle regression proving that, for applications whose local Jaeger service must be replaced after deployment, trace-emitting Deployments are restarted and ready after the centralized OTel redirect and before the workload starts. Then make the redirect remove app-local Jaeger workloads across their known labels/names, restart only Deployments whose pod templates declare Jaeger/OpenTelemetry configuration, wait for readiness, and preserve the existing pre-deploy Train Ticket path. Verify focused conductor/Jaeger tests, the affected regression matrix, and one live Hotel Reservation smoke in which expected services reach both the central collector and Splunk APM with no exporter failures. Do not change scenarios, workloads, prompts, rubrics, or Assistant behavior. Requirements: R3, R7, R9. Planned commit: `fix(telemetry): route application traces before workload`.

  Evidence: the lifecycle and redirect tests first failed because the post-deploy redirect neither restarted clients nor waited for readiness. A second fail-first regression caught the unsafe broad restart that reset Consul and lost concurrent service registrations. The final implementation deletes known app-local Jaeger workloads, recreates the centralized ExternalName services, restarts only Jaeger/OpenTelemetry-configured Deployments, and waits before workload start; the Train Ticket pre-deploy path remains unchanged. The affected 272-test regression matrix passes. In the live Hotel Reservation smoke, the central collector exposed spans for all eight expected services (`frontend`, `geo`, `profile`, `rate`, `recommendation`, `reservation`, `search`, `user`), and every service had a searchable Splunk APM trace in the post-fix window. Collector evidence recorded 8,305 spans sent, zero send failures, zero enqueue failures, and a final trace queue size of zero. Python compilation and `git diff --check` pass; Ruff and Pyright are unavailable in this environment.

Live target note: source `/Users/khubishah/Documents/assistant/.env` and derive the run configuration only from its `SYNTHETIC_*` variables. Map `SYNTHETIC_REALM`, `SYNTHETIC_ORG_ID`, and `SYNTHETIC_USER_ID` to the Assistant runtime identity. Live preflight on 2026-09-24 verified `SYNTHETIC_SPLUNK_ACCESS_TOKEN` for both target-realm query access and OTLP ingest, so map it to `SF_TOKEN` and `SPLUNK_O11Y_INGEST_TOKEN`. `SYNTHETIC_SF_TOKEN` returned 401 for both target-realm endpoints and must not replace the verified token unless its intended role or value is corrected. Never fall back to the non-synthetic org variables for this campaign.

Live N3 note (2026-09-24): after correcting logs access, the svelte smoke exported and queried all four signals and Assistant completed. Its time-bounded investigation selected unrelated in-window K41/K45 telemetry from the shared org and scored 33/100 against the WAF-regex oracle. Treat this as observed agent behavior, not infrastructure invalidation. Full-profile execution exceeded the 16 GB local Docker budget, so the ten local cases remain sequential svelte/non-comparable runs unless moved to a larger host.

## Commit and Upstream Strategy

- Commits A–F contain no `clients.assistant_v3` import and are candidates for an upstream PR in order: coverage foundation, provider lifecycle, capability declarations, OTLP fan-out, secure Splunk export, delivery verification.
- Commits G–I are Cisco-owned Assistant integration and can remain on the fork.
- Commit J is mechanically separable; offer it upstream only if maintainers want the Assistant format supported.
- Commit K is the thin composition layer and should remain last, making either side easy to rebase or cherry-pick.
- L is documentation for the fork initially; split generic provider instructions from Cisco-specific commands if upstreaming.
- Do not squash reusable and Cisco-owned commits together before upstream review.

## Implementation Instructions

For the implementing agent or fresh session:

1. Use the existing worktree `/Users/khubishah/Documents/SREGym-observability-evals` and branch `feature/observability-agent-evals`; do not create another worktree or branch.
2. Run `git status --short --branch` and verify no unexpected user changes. Run `git fetch upstream` read-only, but do not rebase/merge away from the reviewed baseline without approval because the frozen prompt provenance names commit `3f6bd041231715184f962cedb33cfbcefa9b5b3d`.
3. Read `spec.md`, `plan.md`, and all three contracts in full. Do not deviate from the contracts silently.
4. Keep credentials only in a gitignored local `.env` or shell environment. If the original SREGym checkout has the intended `.env`, a symlink may be created with `ln -s ../SREGym/.env .env`; never copy or commit it, and never source the Assistant repository's `.env` implicitly.
5. Implement tasks in dependency order. Parallel sessions may take B/C/G/H after A, but each session owns only its listed files and contract.
6. For every task: write the focused test first, run it and confirm the expected failure, implement the smallest compliant change, rerun focused tests, then run affected regressions and coverage.
7. After each task, check its referenced requirements and contracts. If production code cannot be traced to one, remove it or pause for scope approval.
8. Before every commit run at minimum:

   ```sh
   uv run ruff check <touched-python-files>
   uv run pyright <touched-python-files>
   uv run pytest <focused-test-files> -q
   ```

9. At K2 run:

   ```sh
   uv run pytest -m "not integration" --cov=clients.assistant_v3 --cov=sregym.observability --cov=atif_converter.adapters.assistant_v3 --cov-branch --cov-report=term-missing --cov-report=xml --cov-fail-under=100
   uv run diff-cover coverage.xml --compare-branch upstream/main --fail-under=100
   uv run ruff check .
   uv run pyright
   ```

10. Use the planned commit messages and keep each commit green. Never include `.env`, tokens, live telemetry payloads, result artifacts containing customer data, or generated credentials.
11. For live acceptance, use the documented command shape and substitute the team-selected fixed judge model:

    ```sh
    uv run main.py \
      --problem edge_request_filter_cpu_saturation \
      --stages diagnosis \
      --profile full \
      --agent assistant_v3 \
      --model gpt-5.6-luna \
      --reasoning-effort medium \
      --judge-model "$SREGYM_JUDGE_MODEL" \
      --judge-backend api \
      --observability-provider splunk \
      --allow-agent-endpoint "$ASSISTANT_V3_URL"
    ```

12. After implementation, review every acceptance criterion in `spec.md`, attach the test/delivery evidence, and list any gap. Do not label a live result comparable unless profile, capability, prompt, model, judge, readiness, and delivery checks all pass.
