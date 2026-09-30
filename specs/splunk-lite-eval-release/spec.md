# One-command Splunk SREGym-Lite evaluation release

## Overview

Turn the existing Assistant V3/Splunk Lite pilot into a repeatable team workflow. From a prepared environment, an operator runs one named case or all 21 Lite cases with one command and receives a trustworthy, reviewable result package. This release finishes the Lite workflow; qualifying the larger SREGym catalog is a separate milestone.

## User stories

- As an agent developer, I can run one Lite case while iterating or all 21 for a campaign without manually chaining ingestion, agent execution, judging, and report generation.
- As a reviewer, I can tell which cases produced valid scores, inspect exactly what Assistant V3 saw and answered, and distinguish missing Splunk evidence from an agent failure.
- As an operator, I can resume after a crash without losing completed attempts, duplicating scores, or risking a memory or disk crash through automatic cleanup.

## Requirements

1. A single evaluation invocation MUST select either one registered Lite case or the complete ordered Lite set of 21. An unknown case or unsupported suite MUST fail before deploying a workload.
2. The invocation MUST validate the configured Splunk destination, Assistant V3 endpoint, required credentials, benchmark image, cluster readiness, and resource headroom before beginning. It MUST not silently switch organizations or start an Assistant server configured for an unknown organization.
3. Cases MUST run sequentially. Each attempt MUST retain a distinct, non-overlapping incident time window and the exact reviewed, symptom-guided prompt sent to a fresh Assistant V3 session.
4. Before Assistant V3 starts, the workflow MUST save bounded, run-scoped representative metrics, traces, logs, and Kubernetes-object checks, plus the case-specific causal checks needed to assess Splunk-visible root-cause evidence. It MUST distinguish present, missing at source, missing in Splunk, query error, and unverified. A failed essential gate MUST not become a valid diagnosis score.
5. For a valid attempt, the workflow MUST run Assistant V3, submit only its actual final diagnosis to the unchanged benchmark judge, and save the raw answer, raw judge output, native and normalized traces, and deterministic tool, token-availability, and duration metrics. A judge failure MUST remain a visible incomplete attempt, not a zero diagnosis score.
6. Results MUST be durably recorded after each attempt. A resumed campaign MUST preserve prior valid results, avoid silently selecting the best of multiple attempts, and identify missing or invalid cases.
7. The final package MUST include a concise campaign summary and one reviewable folder per selected case. The summary MUST show valid benchmark scores separately from invalid or missing attempts and link to the exact prompt, answer, benchmark ground truth, judge rationale, pre-agent evidence, delivery audit, and trace. Each case MUST explain what was and was not observable in Splunk and what additional data or access would close any gap.
8. A Splunk-visible numeric score MUST be shown only if it comes from an explicitly versioned and validated separate rubric; otherwise it MUST say `unverified`. The benchmark judge score MUST remain unchanged. Reports MUST clearly label this resource-reduced, symptom-guided pilot as non-leaderboard-comparable.
9. Output and errors MUST not disclose credentials or unbounded telemetry. Interrupted runs MUST preserve their partial artifacts and provide a documented recovery path.
10. The documented workflow MUST be executable by a second developer from a prepared environment. Automated tests MUST cover selection, preflight failures, evidence-gate classifications, judge/answer consistency, artifact links, partial recovery, and report aggregation; a live one-case smoke and a complete Lite campaign MUST be recorded before declaring this release ready.

## Non-goals

- Running or claiming support for the full SREGym problem catalog, including cases requiring host-level fault injection or additional applications. That needs a case-compatibility and infrastructure qualification milestone before a `full` suite option is offered.
- Automatically provisioning a cluster, Splunk organization, or Assistant V3 server. “One command” begins after those prerequisites are prepared and verified.
- Proving byte-for-byte completeness of every telemetry stream or changing the benchmark oracle, scenario definitions, Assistant tools, or remediation stage.
- Claiming leaderboard parity for the current resource-reduced, symptom-guided Splunk-only evaluation.

## Acceptance criteria

- [ ] Given a prepared environment and one Lite case ID, one invocation produces exactly one selected case folder and a summary with its valid score or explicit failure state.
- [ ] Given no case ID, the same invocation attempts the 21 Lite cases sequentially and reports valid, invalid, and missing counts without treating an invalid attempt as a diagnosis failure.
- [ ] Given missing essential Splunk evidence or an unavailable Assistant endpoint, the attempt is classified and retained, but no valid score is reported.
- [ ] Given a crash after a completed case, resumption retains that case's exact answer, judge result, evidence, and trace and does not rerun or silently replace it.
- [ ] Given a completed case, a reviewer can navigate from its summary row to the prompt, answer, oracle, raw judge critique, bounded pre-agent checks, delivery audit, and both traces without relying on this chat.
- [ ] Given two independent developers using the same prepared environment, both can follow the documented command and identify all required prerequisites and recovery steps.
- [ ] Given the release test run, all targeted automated tests pass, one live smoke is reviewed, and a full Lite result package is produced on a host with adequate headroom.

## Assumptions

- [ASSUMPTION] The first release finishes the existing diagnosis-only Lite pilot rather than adding a full-catalog mode before non-Lite compatibility is known.
- The operator selected the existing symptom-guided, resource-reduced configuration as the only supported profile for this release; a later standard-profile workflow should reuse these building blocks after its own validation.
- [ASSUMPTION] A prepared Assistant V3 server, cluster, and Splunk destination are operator responsibilities; the command verifies rather than provisions them.

## Open questions

None for this release.
