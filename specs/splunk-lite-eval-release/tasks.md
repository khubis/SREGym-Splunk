# Tasks: one-command Splunk SREGym-Lite release

Spec: [spec.md](spec.md)
Plan: [plan.md](plan.md)

## Implementation Tasks

- [x] Preflight the prepared environment - Added fail-first tests and a bounded Kubernetes node check before `run` launches a case; `finalize` remains offline from Docker/cluster checks. The benchmark's existing authenticated Assistant session-list preflight handles V3 access before fault injection. An initial `/readyz` check was removed after source inspection showed the standalone V3 server does not expose it; the guide explains the org-alignment limit.
- [x] Build an honest campaign summary - Added fail-first valid/invalid/missing, score-denominator, evidence-status, link, corrupt-attempt, and secret-free output tests. `summary.md` is generated atomically from saved attempts after the existing dossiers, without altering scores.
- [x] Document and verify the one-command release - The guide covers one/all commands, recovery, report navigation, and resource constraints. Focused wrapper/dossier tests pass (40/40), modified wrapper statement/branch coverage is 100%, compilation and `git diff --check` pass, and a summary was regenerated from a saved valid CronJob attempt. Ruff/Pyright were unavailable because the configured package index returned 401.
- [x] Diagnose and fix late collector accounting - The 2026-09-30 CronJob smoke reached V3 and the judge (33/100) but was correctly excluded as `infrastructure_invalid` because the live log-export counter delta was unknown. Historical run-scoped queries later recovered opening/closing/terminal log counts of 1,936/5,846/6,865 with zero export failures and drained queues. The provider now performs one bounded recheck of the *same* closing interval after SignalFlow's two-minute ingest guard when only part of its export accounting is missing; total query failures and unresolved gaps remain invalid. The delivery artifact also retains initial and settled counter samples. Raw attempt: `results/0930_1148`; summary: `results/reproductions/cronjob-smoke-20260930/summary.md`. Do not retroactively count its 33/100 as a valid result.
- [x] Fresh live release gate - On 2026-09-30, reran `cronjob_sidecar_blocks_completion_hotel_reservation` with the synthetic org and `svelte` profile under the 4 GiB single-case memory gate. Raw batch: `results/0930_1315`; reviewed package: `results/reproductions/cronjob-smoke-20260930-live-fix/summary.md`. Four-signal readiness, the case-specific source/Splunk gate, and six representative post-run signal checks passed. The closing delivery audit was valid: exported metrics=749,320, traces=1,910,903, logs=6,047, zero send/enqueue failures, drained queues. The unchanged judge scored the submitted answer 0/100 because V3 focused on `wrk2-job` rather than `CronJob/audit-log-archiver`; all answer, judge, native/ATIF trace, and proof artifacts were packaged. A V3 subagent provider call failed after roughly 20 minutes, but V3 recovered to a final answer. This run's first closing log counter was present, so the specific late-counter recovery branch remains regression-test verified rather than live-reproduced. No 21-case live rerun has been performed.

## Implementation instructions

1. Work in the existing feature worktree and branch. Preserve unrelated changes; do not clone or create another worktree.
2. Read the spec and plan before editing. Implement tasks in order, writing tests that fail before each behavior change.
3. Do not add full-catalog or standard-profile execution, change case prompts/ground truths, or introduce new runtime dependencies.
4. After each task, run focused tests and mark it complete only when its acceptance conditions pass.
5. After all tasks, run the full targeted verification set and compare the result against every acceptance criterion. Report live gates that remain unproven; do not imply a completed campaign unless it actually ran.
