# Observability-Backed Assistant v3 Evaluations

## Overview

Enable an operator to run Cisco Assistant v3 as a diagnosis agent against SREGym-Lite while the benchmark environment exports its telemetry to Splunk. The integration must preserve SRE Gym's benchmark task and diagnosis rubric, produce inspectable and comparable run artifacts, and establish reusable observability-provider behavior without turning the first version into a general multi-vendor platform.

## User Stories

- As an Assistant engineer, I want to run one SREGym-Lite diagnosis with a simple documented workflow so that I can validate the complete integration before launching a suite.
- As an evaluation owner, I want Assistant v3 to receive the same benchmark-visible task as comparable SRE Gym agents so that prompt differences do not invalidate the comparison.
- As an evaluation owner, I want the existing SRE Gym diagnosis score plus a small set of operational metrics so that I can distinguish answer quality from runtime behavior.
- As an engineer debugging a run, I want complete raw and normalized artifacts, including partial-failure artifacts, so that I can reconstruct what happened without rerunning the incident.
- As an open-source maintainer, I want observability-provider behavior separated from Cisco-specific agent behavior so that reusable pieces can be contributed independently.
- As a reviewer, I want every production change traceable to an approved requirement and an automated test so that the first merge request remains understandable and bounded.
- As an evaluation owner, I want a crash-resilient per-case scorecard and a post-run proof that root-cause telemetry existed so that poor agent diagnoses can be distinguished from missing benchmark data.
- As an evaluation owner, I want the same executable, case-specific evidence gate on every rerun so that I know whether Assistant had the observations needed for a defensible RCA before it starts.

## Requirements

### R1. Benchmark compatibility

- The system MUST support diagnosis-only runs for every registered SREGym-Lite case without case-specific adapter code.
- Comparison runs MUST use the normal leaderboard-comparable environment profile. Resource-reduced smoke runs MAY be supported but MUST be labeled non-comparable.
- The integration MUST use the existing SRE Gym problem lifecycle, diagnosis submission path, ground truth, and diagnosis judge without changing their scoring behavior.
- The system MUST NOT expose the scenario identifier, injected fault, expected root cause, or grading material to Assistant v3.

### R2. Prompt parity and provenance

- Assistant v3 MUST receive a frozen, named prompt profile derived from an upstream benchmark prompt and preserving the same diagnosis task, application context, autonomy instruction, and evaluation objective.
- The derived profile MAY replace or remove only capability-dependent plumbing that is unavailable to Assistant v3, such as agent-specific tool names, direct Kubernetes access, benchmark submission mechanics, mitigation instructions, and agent-specific turn-control instructions.
- Prompt adaptation MUST be subtractive and minimal: text unrelated to an unavailable capability MUST remain unchanged in wording and order, apart from runtime field substitution.
- Every capability-dependent difference from the upstream reference MUST be documented and mechanically reviewable; such differences MUST NOT add diagnostic information or make the task easier.
- The baseline model-visible benchmark prompt MUST NOT add a faulty service name, symptom hint, investigation method, likely fault type, or rubric-derived guidance that is absent from the selected upstream profile. The 21-case operator pilot MAY use the separately versioned symptom profile in R13; its scores cannot be aggregated with baseline or called leaderboard-comparable.
- Provider-specific routing and run-isolation information MUST NOT change the benchmark prompt. A separate trusted execution instruction MAY provide only the inclusive UTC start and end of the incident telemetry window.
- Every attempt MUST preserve the exact rendered prompt and enough provenance to identify the prompt profile and detect later prompt drift.
- A prompt change, including a change to capability substitutions, MUST create a new identifiable profile rather than silently changing the meaning of existing results.

### R3. Optional observability export

- Enabling an external observability provider MUST be optional and MUST NOT change the default behavior of existing SRE Gym agents or local telemetry consumers.
- The first version MUST export the telemetry needed for Splunk-based investigation: application and Kubernetes metrics, distributed traces, container logs, and Kubernetes events.
- Each attempt MUST be isolated with an opaque run identity that is present across exported signals but reveals no grading information.
- The run MUST verify that its required telemetry is queryable before starting Assistant v3. A readiness failure MUST be reported as an invalid infrastructure attempt rather than a failed diagnosis.
- The run MUST record bounded delivery evidence at attempt end, including collector export/enqueue failures, queue drain status, and observed signal lag. Queue drain may be proven by zero occupancy in an ingestion-complete interval beginning after workload quiescence; a latest sample can be nonzero because collector self-metrics continue to arrive. Missing required evidence, reported drops, or a queue never observed empty after quiescence MUST make the attempt infrastructure-invalid without deleting its agent or judge artifacts.
- Credentials and secret values MUST never appear in committed content, prompts, logs, normalized traces, score records, or human-readable summaries.
- The reusable provider behavior MUST remain independent of Assistant v3; the Assistant integration MUST remain usable with any correctly prepared Splunk environment.

### R4. Assistant v3 execution

- Assistant v3 MUST run through its existing product behavior and existing surface; this feature MUST NOT add a benchmark-specific Assistant surface.
- Every attempt MUST start a fresh Assistant session and MUST NOT receive direct Kubernetes access, SRE Gym internal tools, prior-attempt conversation state, or benchmark oracle data.
- Every result MUST identify the agent's observable capability profile so that a Splunk-only Assistant run is not presented as having the same data access as an agent with direct Kubernetes and local benchmark tools.
- The tested model and reasoning configuration MUST be fixed for a campaign, passed explicitly when supported, and recorded from the resolved runtime configuration; an unavailable requested model MUST fail validation rather than silently fall back.
- The integration MUST capture the complete Assistant event stream and submit exactly one completed diagnosis to SRE Gym.
- An incomplete or ambiguous Assistant response MUST NOT be submitted as though it were a valid completed diagnosis.
- Agent, model, prompt profile, benchmark profile, case, attempt, and run identity MUST be recorded accurately and MUST NOT silently fall back to misleading defaults.

### R5. Artifacts and trace consistency

- A completed attempt MUST preserve the raw Assistant event stream, submitted diagnosis, existing SRE Gym grading result, run metadata, deterministic metrics, and a validated canonical trajectory.
- The canonical trajectory MUST retain ordered user, assistant, tool-call, tool-result, usage, error, and subagent information when the source stream provides it.
- Partial and invalid attempts MUST preserve all artifacts received before failure plus a machine-readable failure classification.
- Artifact generation MUST be deterministic and idempotent: processing the same raw inputs again MUST produce the same normalized output without duplicating logical events.
- Artifacts MUST remain compatible with SRE Gym's existing result organization and inspection workflows.

### R6. Evaluation and minimal deterministic metrics

- The existing SRE Gym diagnosis rubric and pass threshold MUST remain the authoritative answer-quality score for v1.
- The judge model and backend MUST be configured explicitly, held constant across compared campaigns, and recorded separately from the investigated agent's model configuration.
- V1 MUST additionally report wall-clock agent duration, time to first non-keepalive Assistant event, token usage when reported, total tool calls, tool results explicitly reported as errors, and terminal Assistant outcome.
- Missing source data MUST be represented as unavailable, never inferred as zero.
- Deterministic metrics MUST be derivable from preserved run artifacts and MUST NOT require another model call.
- Infrastructure-invalid attempts MUST be excluded from diagnosis pass-rate calculations while remaining visible in reliability reporting.

### R7. Failure handling and recovery

- Configuration and connectivity MUST be validated before an expensive benchmark attempt begins, with actionable failures that do not disclose secrets.
- Transient pre-execution network failures MUST use bounded retries. Terminal authentication, validation, and permission failures MUST fail fast.
- Ambiguous failures after Assistant execution may have begun MUST preserve the partial trace and MUST NOT launch an unbounded or potentially duplicate attempt automatically.
- A failed attempt MUST NOT prevent later cases or an explicitly resumed campaign from running after cleanup has completed safely.
- Re-running or resuming MUST not overwrite a prior attempt or mix telemetry, traces, or scores between attempts.

### R8. Operator workflow and spot checking

- The project MUST document a minimal workflow to validate prerequisites, run one SREGym-Lite case, run the Lite suite, resume an interrupted campaign, and locate the resulting artifacts.
- A one-case run MUST be possible without editing source code or benchmark scenario definitions.
- A reviewer MUST be able to inspect one attempt and directly compare the rendered prompt, Assistant diagnosis, tool activity, normalized trajectory, and SRE Gym judge result.
- Comparable and non-comparable runs MUST be visibly distinguishable in both command output and persisted metadata.

### R9. Test-first delivery and traceability

- Before feature behavior is implemented, automated tests MUST demonstrate the missing behavior by failing for the expected reason.
- Each production behavior added or changed MUST trace to at least one requirement and automated test. Production changes with no traceable requirement are out of scope; requirements with no verification are incomplete.
- Tests MUST cover successful execution, prompt drift detection, secret redaction, telemetry-readiness failure, transient retry exhaustion, partial Assistant streams, duplicate-event prevention, deterministic metric calculation, and canonical trajectory validation.
- Existing agents, prompts, scoring, and trace conversion MUST have regression coverage demonstrating that the optional integration does not change their default behavior.
- Live credentialed smoke runs MAY complement automated tests but MUST NOT be the only verification of any requirement.

### R10. Cross-run isolation and ten-case pilot

- Every exported signal MUST carry the same opaque run identity as `k8s.cluster.name`, `sregym.run.id`, and the deployment-environment resource attribute used by Splunk APM.
- The opaque identity is harness-only delivery metadata. It MUST NOT be included in Assistant instructions, added as an Assistant tool constraint, or required in Assistant tool queries.
- In the baseline arm, Assistant v3 MUST receive a separate execution instruction containing only the inclusive UTC start and end of the incident telemetry window. It MUST contain no run identity, namespace, scenario, or diagnosis hint and MUST leave the frozen benchmark prompt unchanged.
- Before submission, the driver MUST fail closed with `telemetry_scope_violation` when a recorded tool call contains an explicit absolute timestamp outside the supplied window. The trace MUST remain available and the attempt MUST be excluded from diagnosis scoring. The harness MUST NOT reject an attempt merely because a foreign run identity appears in tool output.
- The first pilot MUST run the first ten cases in `SREGYM_LITE_PROBLEMS`, in their registered order, once each and sequentially. Reports MUST show every attempt and aggregate diagnosis scores only across valid attempts.

### R11. Durable scorecard and trusted telemetry audit

- A completed Assistant answer MUST be submitted and graded as-is even when it selects unrelated telemetry that is present inside the supplied time window. Such behavior is an agent-quality result, not infrastructure invalidation.
- After each terminal case, the campaign MUST durably record case status, original benchmark score and judge rationale, separate Splunk-visible score or `unverified`, final-answer provenance, raw outputs from both judges, and golden-telemetry status. A crash MUST NOT erase previously published cases, and rebuilding the report MUST be deterministic.
- The campaign MUST maintain one concise Markdown table linking to the exact request, raw saved final answer, complete original judge output, separate Splunk-visible assessment, normalized and native traces, pre-agent evidence proof, and delivery audit for every attempted case. A value not yet measured MUST be labeled `unverified`, never shown as zero.
- Before Assistant starts, a trusted verifier MAY read oracle-side case checks and query source telemetry and Splunk inside the proposed incident window. Its facts and results MUST remain outside the Assistant request, tools, and environment. After grading, a reviewer MUST adjudicate whether the verified observations support the benchmark component, mechanism, and impact; the review MUST record sanitized queries/counts/timestamps and `confirmed`, `partial`, `missing`, or `not_checked` without changing the original score.
- The first execution batch MUST contain the first ten registered Lite cases, one sequential attempt each. The remaining eleven are a later batch.

### R12. Splunk-visible ground truth for the complete Lite registry

- The campaign MUST cover the 21 identifiers in `SREGYM_LITE_PROBLEMS` without inventing a new benchmark case or changing its oracle, injected fault, or judge.
- Each case MUST have one reviewable Splunk case directory containing a public prompt recipe and an oracle-side evidence map with executable, declarative checks. One shared verifier MUST execute the 21 manifests by case ID; copied case-specific verifier programs are out of scope. Case files MUST distinguish benchmark facts, expected Splunk signals, observed proof, and uncertainty; an absent signal MUST NOT be described as confirmed.
- Before Assistant starts, the verifier MUST query representative metrics, traces, container logs, pod objects, and event objects, plus the case's explicitly identified *causal* observations. Each causal check MUST state which RCA fact it supports, why that fact helps discriminate this fault from plausible alternatives, the source-side and Splunk-side query/expected result, and whether the fact is essential to a Splunk-only diagnosis or only to full-oracle confirmation. Generic signal presence alone MUST NOT pass a causal-evidence gate.
- The verifier MUST use the exact incident UTC window and target Splunk org/Logs Observer connection, capture sanitized query/provenance/count/lag evidence per signal, and independently compare source telemetry with Splunk for a bounded representative sample. It MUST flag missing-at-source, missing-in-Splunk, query error, unverified, and export failure separately. It MUST NOT claim exhaustive event/point parity from a few samples.
- An unexpected missing essential causal observation MUST prevent an attempt from being counted as a valid scored Splunk investigation; the runner MUST retain a resumable infrastructure/data-gap record. A predeclared Kubernetes-only or otherwise inaccessible fact MAY leave a case runnable as `data_limited`, but its benchmark score MUST be reported separately from fully observable cases and its Splunk-visible assessment MUST use only verified reachable facts.
- Each case MUST receive an evidence-backed classification: `fully_splunk_observable`, `partially_splunk_observable`, `requires_additional_access`, or `undetermined`. The report MUST identify missing facts, whether pod/event object collection can close each gap, or the specific additional Kubernetes/API access required. This classification MUST remain independent of the Assistant answer.
- The original benchmark score MUST remain unchanged and separately labeled. For each case with a reviewed, verified visibility map, a secondary Splunk-visible assessment MUST measure answer support against only verified available evidence, record its own rubric/version and judge configuration, and MUST NOT be called a leaderboard score or silently excuse an incorrect benchmark answer. Until that map is verified, the secondary score MUST be `unverified`, not fabricated.

### R13. Minimal per-case prompt adaptation and fairness

- Preserve the frozen upstream-derived diagnosis prompt body and its capability substitutions. Each case's public recipe MUST identify the application and prescribe a runtime inclusive UTC incident window; actual timestamps are injected only after they are known. The window MUST start after application baseline readiness, cover visible fault evidence, allow bounded post-fault ingestion settling, and not overlap the next case's prompt window. It MUST NOT include run ID, case name, fault mechanism, benchmark oracle, or post-run evidence.
- The 21-case operator pilot MUST use a separately named, versioned **symptom-guided exploratory** profile: retain the same frozen diagnosis body, then add only the window and one short, pre-diagnosis, user-observable symptom per case. The symptom MUST NOT name an injected mechanism, faulty configuration, or expected fix; it MUST be mechanically checked and its pre-diagnosis source recorded before the case runs. The operator approved applying this same rule across all 21 cases without a per-case prompt pause. The time-window-only baseline remains available as a separate arm. Symptom-guided results MUST be labeled non-parity and never pooled with or advertised as the benchmark's prompt-equivalent leaderboard results.
- The campaign MUST render and save the exact prompt automatically; it MUST NOT require a manual prompt-preview stop before every case. The saved prompt and profile hash MUST be linked from the scorecard for post-run review.
- Store the exact rendered prompt, action instruction, profile/hash, time window, and target org identity (not credentials) with each attempt. The public prompt and trusted oracle/evidence files, including pre-agent proofs, MUST be separated so Assistant cannot read the latter.

### R14. Approved Kubernetes object telemetry only

- The Splunk collector MUST replace the chart's default object list with watches of **pods and events only**, matching the supplied reference configuration. It MUST NOT collect the chart-default NetworkPolicies or CRDs, or add deployments, secrets, ConfigMaps, or other objects without separate approval.
- The object receiver MUST have the required `get/list/watch` RBAC for pods and events without adding permissions for other new object types. The pinned chart's existing cluster-metrics RBAC may remain, but must not be mistaken for evidence that extra objects are exported. Events MUST have one intentional export path, not duplicate records from both the chart's event receiver and object receiver. Existing logs, application/Kubernetes metrics, and traces MUST remain enabled.
- Rendered-chart tests MUST verify object list, mode, event pipeline/exporter, RBAC, credential secrecy, and absence of duplicate Kubernetes metrics/events. Live checks MUST confirm pod objects and events can be queried through the configured logs connection in the exact window. Object bodies and saved proofs MUST not expose secret or credential values.

### R15. Sequential, resumable 21-case execution

- Run one scenario at a time, with cleanup and memory/disk checks before the next; do not start a new case when the 16 GiB local budget is unsafe. Use the existing fsynced ledger and immutable attempt artifacts so crashes preserve completed work and resume never silently overwrites or duplicates a case.
- For each case, in order: preflight host and credentials; deploy and establish baseline; inject and establish a non-overlapping fault-centered window; run the scripted source/Splunk generic and causal evidence gate; render/save the named prompt; run one fresh Assistant session; grade the exact terminal answer with the unchanged judge; perform separate Splunk-visible assessment and access-gap review; atomically checkpoint linked artifacts; report the final answer and scores; clean up and recheck resources. Agent/tool failures and infrastructure gaps MUST be visible as different statuses.
- Full-profile runs are required for a leaderboard-comparable claim. Resource-reduced local runs are useful operational evidence but MUST be labeled non-comparable. A case without proven expected evidence or complete grading MUST NOT count as a valid scored comparison.

## Non-Goals

- Mitigation or mutation of the benchmark environment by Assistant v3.
- A full leaderboard submission, published performance claim, or research-paper result in v1.
- An exhaustive model or reasoning-effort sweep; v1 validates one current product-default primary configuration, with any additional model calibration treated as optional follow-up evidence.
- Implementing Datadog, Dynatrace, or another provider; v1 only establishes reusable behavior and implements Splunk.
- Adding or modifying SRE Gym scenarios, fault injectors, workload generators, noise models, ground truth, or the existing diagnosis rubric.
- Adding a benchmark-specific Assistant surface, changing Assistant's internal reasoning behavior, or teaching Assistant case-specific knowledge.
- Providing diagnostic hints in the baseline comparison arm; the operator pilot's short symptom is a separately labeled exploratory arm under R13, not benchmark prompt parity.
- A telemetry replay subsystem, a new Assistant surface, or changes to Assistant v3's product tools.
- Running the full non-Lite suite as an acceptance gate for v1.
- Concurrent multi-org or multi-provider campaigns, a hosted evaluation service, dashboard UI, or automated leaderboard publication.
- Broad refactoring of SRE Gym's runner, telemetry stack, artifact system, or existing agent clients.
- Committing, copying, or managing users' long-lived Splunk or Assistant credentials.
- Using the trusted golden-telemetry audit to modify, excuse, rescore, or retry a completed agent diagnosis, or exposing oracle-side checks to Assistant.
- Automatically fabricating per-case telemetry proofs or Splunk-visible answers from the benchmark oracle; re-labeling a secondary assessment as the official benchmark score.
- Collecting Kubernetes object kinds beyond pods and events under the supplied authorization.

## Acceptance Criteria

- [ ] Given a clean supported environment and valid configuration, when an operator runs one SREGym-Lite case in diagnosis-only mode, then telemetry becomes queryable in Splunk, Assistant v3 completes one fresh investigation, one diagnosis is graded by the unchanged SRE Gym judge, and the run finishes with a complete artifact set.
- [ ] Given the upstream reference prompt and fixed application metadata, when the Assistant prompt is rendered, then its diagnosis task and benchmark-visible information match the reference, every difference is an approved capability substitution, and it contains no case identifier, faulty-service hint, fault mechanism, oracle text, or added troubleshooting guidance.
- [ ] Given the frozen upstream reference and derived Assistant profile, when their static text is compared, then every removed or replaced span maps to an explicitly unavailable capability and all unrelated text remains unchanged and in the same order.
- [ ] Given two attempts of the same case, when their data and artifacts are inspected, then they have distinct opaque identities and no trace, telemetry, score, or conversation state is mixed between them.
- [ ] Given a required telemetry signal that never becomes queryable, when readiness expires, then Assistant is not started and the attempt is recorded as infrastructure-invalid rather than diagnosis-failed.
- [ ] Given an attempt that completed Assistant execution, when delivery auditing finds exporter/enqueue failures, missing closing signals, unavailable required counters, or a queue that does not drain by the deadline, then the attempt and all artifacts remain visible but it is excluded from diagnosis pass-rate aggregation as infrastructure-invalid.
- [ ] Given a transient connection failure before Assistant execution, when retry limits are reached, then the attempt stops with a classified, secret-free failure and can be resumed later.
- [ ] Given a stream that ends after partial Assistant output but before a completed diagnosis, when the attempt terminates, then no partial diagnosis is submitted and the received events remain inspectable.
- [ ] Given a completed raw event stream, when it is normalized twice, then both canonical trajectories are equivalent, valid, ordered, and contain no duplicated logical events.
- [ ] Given a run whose provider omits token usage, when deterministic metrics are produced, then token usage is marked unavailable while observed duration and tool-call metrics remain correct.
- [ ] Given the integration is disabled, when an existing SRE Gym agent runs, then its prompt, telemetry access, scoring, artifact handling, and command behavior remain unchanged.
- [ ] Given an Assistant v3 comparison result, when its metadata is inspected, then it clearly identifies the Splunk-only capability profile and does not imply direct Kubernetes or local SRE Gym tool access.
- [ ] Given a campaign configured for a particular model and reasoning level, when its attempts run or configuration validation fails, then no attempt is silently executed or labeled as a different model configuration.
- [ ] Given two campaigns whose Assistant model configurations are being compared, when their scores are inspected, then both identify the same explicitly configured judge model and backend rather than inheriting different agent-model defaults.
- [ ] Given a resource-reduced smoke run, when results are displayed or inspected, then they are explicitly labeled non-comparable and cannot be mistaken for a normal comparison run.
- [ ] Given one successful and one invalid attempt, when a reviewer performs the documented spot check, then the reviewer can identify the exact prompt, submitted answer, judge result, tool totals, failure classification, and source trace without reading implementation code.
- [ ] Given the proposed change set, when traceability is reviewed, then every production change maps to an approved requirement and test, and no implementation outside the approved scope is present.
- [ ] Given non-overlapping current and older SRE Gym telemetry in one Splunk org, when an attempt runs, then the Assistant receives only the current incident's start and end timestamps, and an explicit query outside that window is preserved but rejected before grading.
- [ ] Given the ten-case pilot command, when it completes or is resumed, then it contains exactly the first ten registered Lite cases, one sequential attempt per case, and a report that separates valid scores from infrastructure or scope failures.
- [ ] Given a crash after any completed case, when the batch is resumed or its report is rebuilt, then every prior final answer, judge critique, score, trajectory, and telemetry-audit result remains available and no completed case is duplicated.
- [ ] Given a completed diagnosis, when the trusted reviewer interprets the pre-agent causal checks and runs any additional post-grade queries, then their facts remain outside the Assistant context, are saved with the exact window and sanitized evidence, and do not alter the original score or Assistant artifacts.
- [ ] Given a case manifest, when the shared verifier runs before Assistant, then every generic signal check and every essential causal check is executed against both the incident window and specified destination; each causal check names the RCA fact it supports and yields source-present/Splunk-present, missing-at-source, missing-in-Splunk, query-error, or unverified evidence with a sanitized query and bounded result.
- [ ] Given generic metrics/traces/logs/objects are present but a promised causal observation is absent from Splunk, when the evidence gate finishes, then the attempt is not counted as a valid scored Splunk investigation and the precise missing fact and remediation are saved for resume.
- [ ] Given an independently observable symptom recipe, when the exploratory prompt is rendered, then the frozen diagnosis body is unchanged, the only additions are the actual non-overlapping UTC window and one reviewed user-facing symptom, and the artifact is marked non-parity. Given the baseline recipe, no symptom is added.
- [ ] Given a case's completed Assistant and judge run, when the scorecard is rebuilt, then it links the exact request, raw final answer, native and normalized traces, raw benchmark judge, separate versioned Splunk-visible judge or `unverified`, scripted causal proof, and the specific missing-data/access remedy; the original score is never overwritten.
- [ ] Given the pinned Helm chart, when the overrides are rendered, then only pod and event objects are watched, the chart's separate event receiver is disabled, and the object pipeline uses the configured HEC exporter without altering metrics/traces/container logs.
- [ ] Given the Lite registry, when case contracts are validated, then exactly 21 one-to-one case directories exist, each has a public recipe for the time-only baseline and reviewed symptom-guided profile plus an oracle-side executable evidence map, and no oracle data can enter the Assistant request.
- [ ] Given source telemetry and a completed Splunk ingestion window, when a case verifier runs, then it saves bounded, sanitized, same-window proof with separate statuses for present, missing, query error, and unverified; a missing source signal cannot be misclassified as a Splunk delivery failure.
- [ ] Given two adjacent cases in one org, when their prompt windows are frozen, then both start after their own baseline, include their visible fault and ingestion-settling period, and do not overlap; if this cannot be established, the later agent does not start.
- [ ] Given a graded Assistant answer and verified Splunk evidence, when secondary assessment runs, then its rubric/version and visibility classification are linked beside but never overwrite the benchmark score; a claim of full Splunk observability includes the evidence and any remaining access gap.
- [ ] Given a local resource limit or interrupted 21-case campaign, when execution stops and resumes, then completed attempts remain durably linked, no case is silently duplicated, and no svelte result is represented as a full-profile leaderboard comparison.

## Assumptions

- [ASSUMPTION] V1 comparison runs are diagnosis-only and use the normal environment profile; the resource-reduced profile is only for local smoke testing.
- [ASSUMPTION] The supplied Splunk access token, HEC destination, HEC token, and existing logs connection are authorized for the required signals and test environment.
- [ASSUMPTION] Assistant v3's existing session interface exposes a terminal diagnosis, ordered tool events, errors, usage when available, and stable run identifiers sufficient for faithful trace preservation.
- [ASSUMPTION] V1's primary product-relevant arm explicitly pins the current Assistant v3 product default, presently `gpt-5.6-luna` with `medium` reasoning, while keeping the reusable adapter model-agnostic. Any additional model calibration arm is optional and is not required for v1 acceptance.
- [ASSUMPTION] A zero-tool completed answer is a valid observable agent outcome unless independent evidence shows the agent lacked telemetry access; it is not automatically classified as infrastructure-invalid.
- [ASSUMPTION] Initial live validation will use `edge_request_filter_cpu_saturation` and `readiness_probe_misconfiguration_social_network` because together they exercise application/APM signals and Kubernetes readiness/events before attempting the complete Lite suite.

## Open Questions

- None. The technical plan will select and freeze the most appropriate upstream diagnosis prompt, then enumerate the minimal capability substitutions required for Assistant v3.
