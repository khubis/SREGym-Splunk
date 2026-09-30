# Technical plan: one-command Splunk SREGym-Lite release

Spec: [spec.md](spec.md)

## Approach

Keep `main.py` as the owner of case deployment, provider readiness, Assistant execution, judge calls, and per-attempt checkpointing. Keep `sregym.results.assistant_v3_lite_repeat` as a thin operator entry point. It already chooses one or all 21 Lite cases, pins `svelte` and the reviewed prompt arm, enforces the synthetic credential mapping and resource gate, and packages valid attempts through `lite_case_dossiers`. Do not add another orchestrator or duplicate the judge.

Finish three missing operator contracts:

1. Before launching the first case, test the selected Kubernetes context/nodes with a short timeout. Continue to check Docker/image, memory, disk, and required environment as today. The existing `main.py` authenticated Assistant session-list preflight runs before fault injection; do not add an unauthenticated `/readyz` call, because the standalone V3 server does not expose that route. Neither check attests the Assistant server's Splunk org; documentation must require starting it with the same synthetic mapping and a dedicated eval database.
2. Preserve `selection.md` as the machine-readable input to the existing dossier builder. After dossier construction, atomically generate `summary.md`: one row per requested case, status (`valid`, classified invalid, or missing), score only for valid cases, run-scoped pre-agent causal status/visibility, and links to each case's prompt, answer, oracle, Splunk evidence, judge CSV, verification, native/ATIF traces, and metrics. An aggregate mean uses only valid scores and prints its denominator. Never infer a numeric Splunk-visible score. Invalid rows link to saved attempt metadata, not a fabricated dossier.
3. Test one-case, 21-case selection, preflight failures, partial/invalid summaries, answer/judge consistency (already enforced in dossier construction), resume/finalize and secret-free output. Update the short repeat guide with the exact one-command invocation and output navigation. Reuse the existing live single-case package for a non-Docker report rehearsal; run a fresh live smoke only when the resource preflight and local services are healthy. A full 21-case live validation requires adequate host headroom and must not be claimed otherwise.

The summary reads saved attempt artifacts; it never calls the agent or judge again. Its only new data interpretation is a conservative status/visibility label from the saved pre-agent proof and attempt metadata. This boundary can later serve a standard-profile Lite run without changing case verification or dossier format. Full-catalog qualification remains separate.

## Boundaries and files

- [operator workflow] Modify `sregym/results/assistant_v3_lite_repeat.py`: bounded preflight and summary writer. Do not touch `main.py`, provider lifecycle, scenario definitions, or Assistant tools.
- [tests] Modify `tests/results/test_assistant_v3_lite_repeat.py`: fail-first unit/CLI coverage for all new branches and one/all/partial states. Existing `tests/results/test_lite_case_dossiers.py` protects packaged provenance.
- [docs] Modify `docs/assistant-v3-lite-repeat.md`: commands, prerequisites, recovery, summary semantics, local-memory caveat.
- [spec] This spec and its tasks record the scoped release. No new runtime dependency or contract file is needed; all changes remain in one operator/reporting domain.

## Data flow

1. CLI validates credentials, output location, host/image, and Kubernetes node readiness; the benchmark runner then validates authenticated Assistant access before injection.
2. Existing `main.py` runs one case or the ordered Lite set; each attempt is checkpointed independently.
3. Existing finalization selects exactly one valid attempt per case, retries transient post-run Splunk queries, and packages valid dossiers.
4. The new summary reads the selected dossiers and all attempt metadata to show valid, invalid, and missing outcomes without changing any judge score.
5. On interruption, `finalize` and `--resume-csv` reuse the original batches; report generation is idempotent and atomic.

## Constraints

- Always: tests fail first; preserve the existing worktree/feature branch and unrelated files; keep secrets out of reports; do not automatically prune Docker or cluster resources.
- Ask first: new runtime dependencies, changing benchmark score/rubric or prompt, extending Assistant V3 beyond Lite, or attempting an unsafe live campaign on a resource-constrained host.
- Never: claim that representative Splunk checks prove exhaustive MELT delivery or that `svelte`/symptom-guided results are leaderboard-comparable.

## Verification

Focused wrapper/dossier tests, lint and type checks for changed code, a read-only rebuild from an existing valid saved batch, and (when preflight permits) one fresh live case. Full-suite live validation is an explicit release gate, not something unit tests can substitute for.
