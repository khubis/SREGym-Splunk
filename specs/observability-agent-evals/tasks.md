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
| R1 | `tests/observability/test_base.py`; `tests/clients/test_assistant_v3_driver.py`; existing runner/conductor regression tests | [ ] |
| R2 | `tests/clients/test_assistant_v3_prompt.py` | [ ] |
| R3 | `tests/observability/test_base.py`; `tests/observability/test_splunk.py`; OTel collector rendering tests | [ ] |
| R4 | `tests/clients/test_assistant_v3_client.py`; `tests/clients/test_assistant_v3_driver.py`; registry/launcher/container tests | [ ] |
| R5 | `tests/clients/test_assistant_v3_client.py`; `tests/clients/test_assistant_v3_driver.py`; `tests/traces/test_assistant_v3_adapter.py` | [ ] |
| R6 | `tests/clients/test_assistant_v3_driver.py`; `tests/traces/test_assistant_v3_adapter.py` | [ ] |
| R7 | `tests/observability/test_splunk.py`; `tests/clients/test_assistant_v3_client.py`; runner resume/cleanup tests | [ ] |
| R8 | Assistant v3 command/documentation tests; artifact spot-check fixtures | [ ] |
| R9 | All focused files above; final new-module coverage and changed-line coverage gates | [ ] |

## [provider] Reusable Provider Foundation — Upstream Candidate

- [x] **B. Add the optional provider contract and opaque attempt identity** — First add failing unit tests for the `none` provider, strict `anon_<32 hex>` validation, collision rejection, distinct resume identities, typed failure taxonomy, and secret-free serialization. Implement `sregym.observability.base`, the factory, and preallocated `RunArtifacts` identity support per `contracts/observability-provider.md`; default-disabled behavior must remain byte/behavior compatible. Requirements: R1, R3, R5, R7, R9. Planned commit: `feat(observability): add optional provider lifecycle`.

  Evidence: the focused suite first failed because `sregym.observability` did not exist. The completed suite has 19 passing tests with 100% statement/branch coverage for the new provider package. The 122-test affected regression matrix passes, including artifact publication, campaign abort/resume, conductor wiring, container hardening, deployment profiles, and trace conversion; changed production lines have 100% diff coverage.

## [security] Agent Capability Isolation — Upstream Candidate

- [x] **C. Enforce per-agent Kubernetes and MCP capabilities** — First add failing registry/launcher/container tests showing an agent with both capability flags disabled receives no kubeconfig mount/environment, MCP URL, MCP filtered-egress rule, or benchmark tool endpoint in preflight and execution; also prove existing registrations default to both capabilities enabled. Implement the two backward-compatible registration fields and register Assistant with both disabled. Requirements: R1, R4, R9. Planned commit: `feat(agents): declare kubernetes and mcp capabilities`.

  Evidence: seven fail-first tests initially failed because the registration/config fields and Assistant registration did not exist. The completed focused suite has eight passing tests. A 201-test affected regression matrix passes across registry, launcher, egress, endpoint, credential, hardening, audit, and campaign behavior; all changed production lines have coverage. Ruff and Pyright pass on every touched Python file.

## [telemetry] Generic OTel Fan-Out — Upstream Candidate

- [ ] **D. Add optional central-collector metric and trace fan-out** — First add manifest-rendering tests proving `None` produces the original Jaeger/spanmetrics/Prometheus pipelines and an external export adds exactly one OTLP exporter, resource attributes, trace fan-out, and Prometheus federation pipeline without embedding credentials. Implement safe structured manifest rendering and deployment using argument arrays/stdin rather than shell interpolation. Requirements: R3, R7, R9; contract: `observability-provider.md`. Planned commit: `feat(telemetry): add optional otlp export fanout`.

## [splunk] Splunk Provider — Upstream Candidate

- [ ] **E. Deploy the pinned Splunk collector securely** — First add failing tests for required environment validation, HTTPS HEC construction, numeric port/index handling, chart version `0.160.0`, bounded resources, idempotent Helm operations, Secret creation through the Kubernetes API, TLS enforcement, and redaction of every supplied secret from commands/errors/artifacts. Implement only the Splunk provider deployment and non-secret values file; do not add Assistant imports. Requirements: R3, R7, R9. Planned commit: `feat(splunk): export sregym telemetry with otel`.

- [ ] **F. Implement end-to-end readiness and delivery assurance** — First add deterministic mocked tests for accessible logs-connection selection, four-signal opening/closing queries, attempt scoping, first-visible lag, 408/429/5xx retries, `Retry-After` caps, 400/401/403 fail-fast, retry exhaustion, collector sent/send-failed/enqueue-failed deltas, queue high-water/final size, drain success/timeout, missing-counter invalidation, and secret-free evidence. Implement `wait_until_queryable` and `finish_attempt` exactly as `observability-provider.md`; no LLM call is allowed. Requirements: R3, R5, R6, R7, R9. Planned commit: `feat(splunk): verify telemetry delivery and queue drain`.

## [prompt] Assistant Prompt Parity — Cisco-Owned

- [ ] **G. Freeze and mechanically derive the Assistant diagnosis prompt** — First add failing tests for reference SHA-256, source commit/path, exact-once substitutions, unchanged text/order outside approved spans, profile-version changes, allowed placeholders, unresolved placeholders, and rejection of case IDs, fault/oracle text, added service/time/symptom hints, or routing metadata. Add the exact Stratus v1 snapshot, substitution manifest, and renderer. Requirements: R1, R2, R8, R9; contract: `assistant-v3.md`. Planned commit: `feat(assistant-v3): add immutable sregym diagnosis prompt`.

## [client] Assistant API and Native Events — Cisco-Owned

- [ ] **H. Build the authenticated Assistant SSE client** — First add secret-free fixtures and failing tests covering split chunks, multi-line data, pings, every relevant root/tool/usage/subagent event, `final_text` fallback, explicit model/reasoning, `session_id:null`, omitted surface, 401/403 fail-fast, bounded pre-stream retry, capped `Retry-After`, disconnect after work with no retry, `assistant.error`, malformed/blank/conflicting completion, credential redaction across chunk boundaries, and forbidden Kubernetes-tool detection. Implement connectivity preflight and streaming with existing `httpx`; do not add a runtime dependency. Requirements: R4, R5, R6, R7, R9. Planned commit: `feat(assistant-v3): add resilient session stream client`.

## [artifacts] Deterministic Results — Cisco-Owned

- [ ] **I. Persist native artifacts and calculate deterministic metrics** — First add golden tests for `request.json`, ordered `events.jsonl`, `terminal.json`, `run_metadata.json`, `metrics.json`, `failure.json`, and `observability/delivery.json`; cover atomic overwrite, byte-identical reprocessing, duplicate stable IDs, distinct identical text, missing usage as `null`, unique tool/error counts, keepalive exclusion, partial streams, post-execution infrastructure invalidation, and secret scanning. Implement artifact writers/metric derivation per `artifacts.md`. Requirements: R4–R7, R9. Planned commit: `feat(assistant-v3): persist deterministic evaluation artifacts`.

## [traces] ATIF Normalization — Separable Adapter Commit

- [ ] **J. Convert Assistant event streams to validated ATIF** — First add failing fixtures/tests for format detection, request/user step, ordered assistant content, tool-call/result links, explicit errors, usage, subagent chronology, final response de-duplication, incomplete streams, schema validation, deterministic serialization, SRE Gym path metadata, and SQLite ingestion. Implement the adapter and minimal converter/dispatch registrations without changing existing adapters. Requirements: R5, R6, R9; contract: `artifacts.md`. Planned commit: `feat(traces): normalize assistant v3 trajectories`.

## [integration] Runner and Driver Integration

- [ ] **K. Wire the provider gate and Assistant driver into the existing lifecycle** — First add runner/driver integration tests with fake provider, fake Assistant SSE server, and fake Conductor proving: explicit diagnosis-only/model/reasoning/judge validation; provider preparation before baseline; readiness before agent launch; one fresh session; exact `/get_app` metadata only; exactly one valid `/submit`; no submission for invalid streams; closing delivery audit before teardown; infrastructure-invalid pass-rate exclusion; artifact publication for pre/post-agent failures; cleanup; resume; full/svelte comparability labels; and no behavior change for an existing agent with provider `none`. Then implement the smallest `main.py`/Conductor/driver/registry glue. Requirements: R1–R9. Planned commit: `feat(evals): run assistant v3 against sregym telemetry`.

- [ ] **K2. Run the complete offline verification matrix** — Run focused suites for every preceding task, all existing trace/runner/container tests, the full non-integration test suite, Ruff, Pyright, ATIF validation, secret scans, 100% new-module branch coverage, and 100% changed-line coverage. Fix only defects within the approved files; any required scope expansion pauses for approval. Produce the final requirement-to-test and changed-production-file-to-test table. Planned commit only if fixes are necessary: `fix(evals): close observability integration verification gaps`.

## [workflow/docs] Operator Experience

- [ ] **L. Document reproducible commands and a no-code spot check** — Add tested documentation for prerequisites, environment names without values, preflight, one full-profile case, svelte non-comparable smoke, Lite suite, resume, artifact locations, delivery report interpretation, capability/model/judge metadata, and failure recovery. Provide a small read-only inspection command that prints prompt hash/text location, final diagnosis, tool/error totals, delivery validity/lag/drop/queue evidence, judge result, and failure classification without exposing credentials. Requirements: R6–R9. Planned commit: `docs(evals): add assistant v3 benchmark workflow`.

## [live] Credentialed Acceptance (No Product-Code Commit)

- [ ] **M. Validate two live Lite cases, then the suite gate** — With user-supplied gitignored credentials, run `edge_request_filter_cpu_saturation` and `readiness_probe_misconfiguration_social_network` using `--stages diagnosis --profile full --agent assistant_v3 --model gpt-5.6-luna --reasoning-effort medium` plus an explicit fixed judge model/backend and Splunk provider. For each, verify four opening/closing signals, zero send/enqueue failures, drained queues, reasonable recorded lag, exact prompt provenance, one Assistant session/submission, unchanged judge behavior, complete artifacts, and no forbidden capability. Run a svelte smoke only as non-comparable. Start the full Lite suite only after both full-profile cases pass; preserve failures rather than editing scenarios or prompts around them.

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
