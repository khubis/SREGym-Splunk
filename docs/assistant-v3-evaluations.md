# Assistant v3 evaluations with Splunk

This workflow runs the existing Assistant v3 product surface against SREGym-Lite diagnosis cases. SRE Gym deploys and faults the application, exports metrics, traces, container logs, and Kubernetes events to Splunk, waits until all four signals are queryable, and then starts one fresh Assistant session. The existing SRE Gym diagnosis judge remains authoritative.

Use `--profile full` for comparable results. `--profile svelte` is useful for a lower-cost smoke test, but its persisted `comparable` value is `false` and it must not be compared with full-profile results.

## One case, Lite suite, or the full registry

For a small, safety-gated and resumable local Assistant V3 pilot that also
assembles per-case evidence, use the [Lite repeat wrapper](assistant-v3-lite-repeat.md).

`main.py` is the suite runner; one command can run multiple cases sequentially. `--problem <case-id>` runs one case. `--suite sregym-lite` selects the 21 ordered Lite IDs in `sregym/conductor/problem_sets.py`. With neither selector, the runner uses `sregym/conductor/tasklist.yml` if present, otherwise the entire registered problem set; that broader selection is not a guaranteed laptop-sized or Kind-compatible suite. `--stages diagnosis` limits work to RCA; the default attempts every stage a problem supports. `--n-attempts N` repeats each case, and `--resume <previous-results.csv>` skips completed slots, provided the original CSV still exists. A missing or unreadable resume CSV is currently logged as a warning and the runner continues without those completed slots, so verify the path before launching a repeat campaign.

The ordinary SREGym stack deploys Prometheus, Loki, and Jaeger and waits for application/infrastructure readiness, but it does not perform the case-specific source-to-store evidence checks described here. With `--observability-provider splunk`, the runner adds a four-signal query-readiness gate before V3 and a collector-delivery audit before teardown. The additional 21 case-specific source/Splunk causal checks currently run only for `--agent assistant_v3 --observability-provider splunk --assistant-prompt-arm symptom_guided`; they are evaluator-side checks and do not reveal the oracle to V3. The representative six-class post-run query is a separate command below, not part of the one-command suite lifecycle. Therefore the native suite command runs, judges, and checkpoints the cases, but does **not yet** produce the complete final by-case review package without follow-up steps.

## Prerequisites

Start from the repository root on a machine that meets the [SREGym-Lite requirements](SREGym-Lite.md): Python 3.12 or newer, Docker, KIND, `kubectl`, Helm 4 or newer, `uv`, at least 8 vCPU, 16 GB memory, and 100 GB disk. Initialize the application submodule and Python environment, create the KIND cluster, and confirm its nodes are ready:

```bash
git submodule update --init --recursive
uv sync --group dev
bash kind/setup_kind_cluster.sh
kubectl get nodes
docker info >/dev/null
helm version --short
```

The Assistant URL can be a local v3 server in a separate Assistant checkout (`make run-local-server-v3`, port 8903); it need not be an externally supplied deployment URL. The runner rewrites a loopback Assistant URL for its isolated agent container. Start that server with `SYNTHETIC_SF_TOKEN`, `SYNTHETIC_REALM`, `SYNTHETIC_ORG_ID`, and `SYNTHETIC_USER_ID` mapped to `SF_TOKEN`, `SFX_REALM`, `ORG_ID`, and `USER_ID`, respectively. Use a dedicated local `POSTGRES_DATABASE` for evals so an older Assistant development schema cannot break preflight. Derive the runner bearer credential from the server's generated local-auth JWT without printing it. The Splunk org must have an accessible Logs Observer connection. Set `SPLUNK_LOGS_CONNECTION_ID` when the HEC destination is not the org's default connection; otherwise, the provider selects the accessible default connection, or the first accessible connection when no default exists. Do not add connection, service, scenario, or run-ID hints to the model prompt; the approved time-window instruction is separate.

Keep credentials in the process environment or a gitignored local `.env`. The runner does not automatically source `.env`; review it and load it in the shell before running commands. Never commit it or print its contents.

| Environment name | Required purpose |
|---|---|
| `ASSISTANT_V3_URL` | Existing Assistant v3 base URL; HTTPS except loopback development |
| `ASSISTANT_V3_AUTH_TOKEN` | Assistant bearer token |
| `SF_TOKEN` | Splunk Observability Cloud user/API token used by Assistant and provider queries |
| `SPLUNK_O11Y_INGEST_TOKEN` | Splunk Observability Cloud org token with `INGEST` scope, used only by the collector |
| `SFX_REALM` | Splunk Observability Cloud realm |
| `SPLUNK_HOST` | HEC hostname only, without scheme, port, or path |
| `SPLUNK_HEC_PORT` | Numeric TLS HEC port |
| `SPLUNK_HEC_TOKEN` | HEC token for container logs and Kubernetes events |
| `SPLUNK_HEC_INDEX` | Optional HEC index; defaults to `main` and must be allowed by the HEC token |
| `SPLUNK_LOGS_CONNECTION_ID` | Optional Logs Observer connection ID for the HEC destination; use it when that destination is not the org default |
| `JUDGE_API_BASE` | Judge endpoint when the selected API backend requires one |
| `JUDGE_API_KEY` | Judge credential when the selected API backend requires one |
| `SSL_CERT_FILE` | Optional readable CA bundle for Dockerized CLI judges behind enterprise TLS inspection |

Choose one fixed judge model for the campaign and expose its identifier as the non-secret shell variable `JUDGE_MODEL`. Keep this value and `--judge-backend` unchanged across results being compared.

The commands below use `--force-build` so the container includes this checkout's Assistant adapter and observability integration. Docker reuses unchanged build layers while their cache remains. After one successful local build, later runs may replace `--force-build` with `--agent-image sregym-agent-base:latest` to reuse that exact local image without rebuilding; verify its image ID first and rebuild after changing containerized code. Omitting both flags selects SREGym's published image, which does not contain this fork's Assistant driver. Never combine the two flags.

## Preflight

The normal benchmark command performs provider, judge, and Assistant preflights before the first expensive attempt. These read-only commands isolate configuration or connectivity failures beforehand:

```bash
kubectl auth can-i create secrets --all-namespaces
uv run python -c 'from clients.assistant_v3.driver import run_preflight; run_preflight()'
uv run python -c 'from sregym.observability import create_provider; p = create_provider("splunk"); p.preflight(); p.close()'
JUDGE_MODEL_ID="$JUDGE_MODEL" uv run python -c 'from main import run_judge_preflight_check; run_judge_preflight_check()'
```

These checks validate access but do not prove telemetry delivery. Each real attempt separately verifies four-signal readiness after fault injection and performs a closing delivery audit before teardown. The opening gate allows up to six minutes for the slower APM indexing path while retaining bounded retries; Assistant does not start unless all four signals are queryable.

## Run one comparable case

Start with the application/APM-oriented Lite case:

```bash
uv run main.py \
  --problem edge_request_filter_cpu_saturation \
  --stages diagnosis \
  --profile full \
  --agent assistant_v3 \
  --model gpt-5.6-luna \
  --reasoning-effort medium \
  --judge-model "$JUDGE_MODEL" \
  --judge-backend api \
  --observability-provider splunk \
  --allow-agent-endpoint "$ASSISTANT_V3_URL" \
  --force-build
```

The other planned acceptance case, `readiness_probe_misconfiguration_social_network`, uses the same command shape and exercises Kubernetes readiness/events more directly. Do not change its prompt or add a service/time hint.

### Pause one live attempt for operator inspection

For an auditable walkthrough, add `--inspect-before-agent` to a single-problem
command running in an interactive terminal. The runner injects the fault, waits
until metrics, traces, logs, and Kubernetes events are queryable in Splunk, then
prints the opaque run identity and pauses before creating the Assistant session.
The application, fault, workload, and exporters remain active while paused.

Inspect the live Kind cluster and the exact Splunk time/run scope, then press
Enter in the benchmark terminal. The same attempt continues through Assistant
V3, grading, delivery audit, artifact publication, and cleanup. This option is
rejected for suites, external-harness mode, and non-interactive terminals.

```bash
uv run main.py \
  --problem cronjob_sidecar_blocks_completion_hotel_reservation \
  --stages diagnosis \
  --profile full \
  --agent assistant_v3 \
  --model gpt-5.6-luna \
  --reasoning-effort medium \
  --judge-model "$JUDGE_MODEL" \
  --judge-backend api \
  --observability-provider splunk \
  --allow-agent-endpoint "$ASSISTANT_V3_URL" \
  --inspect-before-agent \
  --force-build
```

## Run a non-comparable svelte smoke

Use this only to validate wiring with fewer resources:

```bash
uv run main.py \
  --problem edge_request_filter_cpu_saturation \
  --stages diagnosis \
  --profile svelte \
  --agent assistant_v3 \
  --model gpt-5.6-luna \
  --reasoning-effort medium \
  --judge-model "$JUDGE_MODEL" \
  --judge-backend api \
  --observability-provider splunk \
  --allow-agent-endpoint "$ASSISTANT_V3_URL" \
  --force-build
```

Confirm `run_metadata.json` contains `"benchmark_profile": "svelte"` and `"comparable": false`. A successful smoke does not establish leaderboard comparability.

## Run or resume SREGym-Lite

Run the full Lite suite only after both planned full-profile cases pass:

```bash
uv run main.py \
  --suite sregym-lite \
  --stages diagnosis \
  --profile full \
  --agent assistant_v3 \
  --model gpt-5.6-luna \
  --reasoning-effort medium \
  --judge-model "$JUDGE_MODEL" \
  --judge-backend api \
  --observability-provider splunk \
  --allow-agent-endpoint "$ASSISTANT_V3_URL" \
  --force-build
```

The campaign aggregate is `results/<batch>/assistant_v3_ALL_results.csv`. Resume into a new batch with the same model, reasoning, judge, profile, provider, stages, and attempt count:

```bash
RESULTS_CSV=results/<batch>/assistant_v3_ALL_results.csv
uv run main.py \
  --suite sregym-lite \
  --stages diagnosis \
  --profile full \
  --agent assistant_v3 \
  --model gpt-5.6-luna \
  --reasoning-effort medium \
  --judge-model "$JUDGE_MODEL" \
  --judge-backend api \
  --observability-provider splunk \
  --allow-agent-endpoint "$ASSISTANT_V3_URL" \
  --force-build \
  --resume "$RESULTS_CSV"
```

Resume imports completed attempt slots and reruns incomplete ones. It creates new attempt identities and never reuses an Assistant conversation or mixes telemetry between attempts.

## Artifacts

Each published attempt is stored at:

```text
results/<batch>/assistant_v3/<problem_id>/run_<attempt>/
```

The important files are:

| Path | Meaning |
|---|---|
| `assistant_v3/request.json` | Exact rendered prompt, prompt hashes/provenance, requested model/reasoning |
| `assistant_v3/events.jsonl` | Ordered redacted native Assistant events |
| `assistant_v3/terminal.json` | Final diagnosis, session outcome, resolved model/reasoning, submission count |
| `run_metadata.json` | Profile/comparability, capability, model, judge, provider, readiness, and classification |
| `metrics.json` | Duration, first event, reported tokens, tool calls/errors, terminal outcome |
| `observability/delivery.json` | Opening/closing visibility, lag, exporter failures, queues, drain, validity |
| `failure.json` | Machine-readable failure classification when an attempt is not valid |
| `trajectory/trajectory.json` | Validated canonical ATIF trajectory |
| `<problem_id>_results.csv` | Existing SRE Gym diagnosis judge result and flattened attempt metadata |

The expected capability profile is `splunk_o11y_read_only_no_direct_kubernetes`. `requested_model`, `resolved_model`, `requested_reasoning`, `resolved_reasoning`, `judge_model`, and `judge_backend` must match the intended campaign; do not compare mislabeled or silently changed configurations.

## Read-only spot check

Set `RUN_DIR` to one published attempt, then run the block below. It reads only the stable artifact fields needed for review. It does not read environment variables, raw telemetry payloads, credentials, or oracle data, and it does not modify the run.

<!-- BEGIN ASSISTANT_V3_SPOT_CHECK -->
```bash
RUN_DIR=results/<batch>/assistant_v3/<problem_id>/run_<attempt>
uv run python - "$RUN_DIR" <<'PY'
import csv
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
if not root.is_dir():
    raise SystemExit(f"run directory not found: {root}")

def read_json(relative, required=True):
    path = root / relative
    if not path.exists():
        if required:
            raise SystemExit(f"required artifact not found: {path}")
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"artifact is not a JSON object: {path}")
    return value

request_path = root / "assistant_v3" / "request.json"
request = read_json("assistant_v3/request.json")
terminal = read_json("assistant_v3/terminal.json")
metadata = read_json("run_metadata.json")
metrics = read_json("metrics.json")
delivery = read_json("observability/delivery.json")
failure = read_json("failure.json", required=False)
csv_paths = sorted(root.glob("*_results.csv"))
if len(csv_paths) != 1:
    raise SystemExit(f"expected one per-attempt results CSV under {root}, found {len(csv_paths)}")
with csv_paths[0].open(newline="", encoding="utf-8") as handle:
    rows = list(csv.DictReader(handle))
if len(rows) != 1:
    raise SystemExit(f"expected one result row in {csv_paths[0]}, found {len(rows)}")
judge = rows[0]

summary = {
    "prompt": {"sha256": request.get("prompt_sha256"), "text_path": str(request_path)},
    "diagnosis": terminal.get("final_text"),
    "tools": {"calls": metrics.get("tool_calls"), "failed_results": metrics.get("failed_tool_results")},
    "delivery": {
        key: delivery.get(key)
        for key in (
            "valid",
            "drained",
            "first_visible_lag_ms",
            "send_failed_delta",
            "enqueue_failed_delta",
            "queue_high_water",
            "queue_final_size",
            "queue_drain_minimum",
        )
    },
    "judge": {
        "success": judge.get("Diagnosis.success"),
        "judgment": judge.get("Diagnosis.judgment"),
        "accuracy": judge.get("Diagnosis.accuracy"),
    },
    "configuration": {
        key: metadata.get(key)
        for key in (
            "benchmark_profile",
            "comparable",
            "capability_profile",
            "requested_model",
            "resolved_model",
            "requested_reasoning",
            "resolved_reasoning",
            "judge_model",
            "judge_backend",
            "observability_provider",
        )
    },
    "failure": {
        "classification": failure.get("classification", metadata.get("classification")),
        "phase": failure.get("phase"),
        "safe_message": failure.get("safe_message"),
        "cleanup_status": failure.get("cleanup_status"),
        "included_in_diagnosis_pass_rate": failure.get(
            "included_in_diagnosis_pass_rate", metadata.get("included_in_diagnosis_pass_rate")
        ),
    },
}
print(json.dumps(summary, indent=2, sort_keys=True))
PY
```
<!-- END ASSISTANT_V3_SPOT_CHECK -->

For a comparable valid attempt, verify all of the following:

- profile `full`, `comparable: true`, and capability `splunk_o11y_read_only_no_direct_kubernetes`;
- intended requested/resolved agent model and reasoning plus the fixed judge model/backend;
- one non-empty diagnosis, `terminal_outcome: completed`, and exactly one submission in `terminal.json`;
- four ready signals in both the opening and closing reports;
- `delivery.valid: true`, zero send/enqueue failure deltas, and `drained: true` (either zero final queue sizes or zero post-workload-stop queue minima; collector self-metrics may create a new in-flight point after a drain);
- a plausible non-negative first-visible lag for every signal; and
- a populated `Diagnosis.success`/judge result in the per-attempt CSV.

`first_visible_lag_ms` measures bounded source-to-query visibility, not total incident age. The failure deltas and queue evidence demonstrate bounded collector delivery; they do not claim record-for-record completeness. OpenTelemetry Collector failure counters are sparse before their first failure, so an absent failure series is normalized to its initial zero only when the same successful snapshot contains the matching sent and queue series. Otherwise a `null` required counter is unavailable evidence and makes delivery invalid.

## Failure recovery

| Classification or symptom | Meaning and next action |
|---|---|
| Configuration/authentication/permission preflight failure | Correct environment or access. No diagnosis attempt should be counted. |
| `readiness_timeout` or `transient_exhausted` before launch | Assistant was not started. Check collector rollout, Splunk connection/query access, signal routing, and recorded readiness evidence; then resume. |
| `incomplete_stream`, `assistant_error`, or `ambiguous_completion` | Inspect `events.jsonl` and `failure.json`. No partial diagnosis is submitted and the session is not retried automatically after work begins. |
| `capability_policy_violation` | A direct Kubernetes tool was observed. Preserve the trace, but do not label the run Splunk-only comparable. |
| `infrastructure_invalid` after execution | Preserve diagnosis and judge evidence. Inspect `delivery.json`; `included_in_diagnosis_pass_rate` must be `false`. Fix delivery before comparing quality. |
| Cleanup failure or timeout | The campaign stops rather than starting another case against uncertain cluster state. Restore a safe cluster state and resume from the aggregate CSV. |
| Artifact publication failure | Inspect the reported `.runtime` staging path. Do not delete it until the partial evidence is understood. |

Do not repair a failed run by editing its scenario, oracle, prompt, profile label, or artifacts. Fix the infrastructure/configuration and create or resume a new attempt so the original evidence remains auditable.

## Complete-Lite evidence campaign (work in progress)

`cases/splunk-lite/<case-id>/` now contains one public `prompt.yaml` recipe and one separate oracle-side `ground_truth.yaml` for every registered Lite case. These evidence maps are **candidates**, explicitly marked `pending_live_verification`; do not interpret them as confirmed Splunk-visible ground truth. The baseline recipe preserves `sregym-stratus-diagnosis-v1` and adds only the actual incident UTC window in the separate execution instruction. No run ID, scenario name, or oracle detail belongs in the Assistant prompt. An optional symptom hint would be a different, non-parity prompt profile and is not part of this baseline.

After an attempt has produced `run_metadata.json` and `assistant_v3/request.json`, run the common bounded delivery check (substitute the actual attempt directory):

```sh
set -a
source "${ASSISTANT_REPO:?Set ASSISTANT_REPO}/.env"
set +a
export SF_TOKEN="$SYNTHETIC_SF_TOKEN"
export SPLUNK_O11Y_INGEST_TOKEN="$SYNTHETIC_SPLUNK_ACCESS_TOKEN"
export SFX_REALM="$SYNTHETIC_REALM"
export SPLUNK_LOGS_CONNECTION_ID='<your-accessible-logs-connection-id>'
export SPLUNK_HEC_INDEX=main
uv run python -m sregym.results.splunk_lite_evidence --run-dir /absolute/path/to/attempt
```

The command checks the saved UTC window, prompt profile, and Logs connection before querying six representative run-scoped signal classes. It writes `splunk_lite_delivery.json` atomically inside the attempt and records only statuses/counts, not event bodies or credentials. Rerun `checkpoint_attempt` after it so the scorecard links the proof. `missing` is a query result; `query_error` is not evidence of absence. Even six `present` statuses do **not** prove that root-cause telemetry or every source signal reached Splunk: source-versus-destination and each case's oracle evidence still need separate post-grade review. The original benchmark judge score remains authoritative and unchanged.

For metrics, the post-run verifier compensates for the Splunk backend's two-minute SignalFlow ingest-lag guard so the *actual* historical query ends at the prompt's UTC end. Without that offset, incidents shorter than two minutes produced a false `missing` even when opening and closing collector audits were green. The original false-missing proofs remain only in the older raw-run archive in macOS Trash; the active by-case folders retain the corrected checks and their limitation notes.

The live, case-specific source-to-Splunk checks are reusable Python code in `sregym/results/splunk_lite_causal.py`, dispatched by `_assistant_case_preflight` in `main.py`. Their regression fixtures are in `tests/results/test_splunk_lite_causal.py` and `tests/test_main_campaign_abort.py`. Run them with `uv run pytest -q tests/results/test_splunk_lite_causal.py tests/test_main_campaign_abort.py`. Each benchmark attempt calls its reviewed check after fault injection and generic collector readiness, before V3 launches. The checker queries only the scoped Kind source and synthetic Splunk connection for the saved incident window; the sanitized `splunk_lite_pre_agent.json` records the check ID, source/Splunk counts, visibility classification, and exact UTC window. The raw query bodies and credentials are not persisted. A new simulation is required to rerun a live source comparison after teardown; the post-run six-class delivery command above can be rerun against retained Splunk data within retention. A case without a reviewed executable checker fails closed and must not receive a score. These checks are representative causal evidence, not a claim of exhaustive metric/log/trace parity.

For the current resource-limited pilot, run one case at a time after sourcing the private `.env` and mapping the `SYNTHETIC_*` values to `SF_TOKEN`, `SFX_REALM`, `ORG_ID`, `USER_ID`, and `SPLUNK_O11Y_INGEST_TOKEN` (do not print or commit values). The exact repeatable invocation is:

```sh
uv run main.py --problem <case-id> --stages diagnosis --profile svelte \
  --agent assistant_v3 --model gpt-5.6-luna --reasoning-effort medium \
  --judge-model azure/gpt-5.6-luna --judge-backend api \
  --observability-provider splunk --allow-agent-endpoint "$ASSISTANT_V3_URL" \
  --assistant-prompt-arm symptom_guided --agent-image sregym-agent-base:latest
```

This invocation automatically performs the source/Splunk case check and records its proof. If that check fails, preserve the incomplete attempt, fix the checker or telemetry path with a regression test, and launch a fresh attempt; never attach a score to the failed one. The earlier full-profile suite command is a separate, future comparability target, not the command used for these `svelte` pilot results.

After one single-case smoke passes, the same pilot configuration can run all 21 Lite cases sequentially with one runner command (start Assistant V3 and complete the environment/preflight steps above first):

```sh
uv run main.py --suite sregym-lite --stages diagnosis --profile svelte \
  --agent assistant_v3 --model gpt-5.6-luna --reasoning-effort medium \
  --judge-model azure/gpt-5.6-luna --judge-backend api \
  --observability-provider splunk --allow-agent-endpoint "$ASSISTANT_V3_URL" \
  --assistant-prompt-arm symptom_guided --agent-image sregym-agent-base:latest
```

Do not run this and the single-case command concurrently on the same Kind cluster. The suite checkpoints each terminal attempt and stops on an untrustworthy case evidence gate or failed cleanup; inspect the precise failure before resuming. A successful runner exit still does not imply that the separate post-run six-class checks and by-case export have been completed.

The verifier retries an empty asynchronous Splunk search up to three times before recording it as missing. The first CronJob smoke on 2026-09-26 showed why: its first APM search poll returned zero, its second returned a trace. The run's exact 4,012-character prompt is in `assistant_v3/request.json`; a hand-copied paraphrase is not an authoritative prompt artifact.

The Splunk chart now watches pod and event objects only and uses one HEC object-log pipeline; the former event receiver is disabled to avoid duplicate event export. Its query checks use `sourcetype="kube:object:pods"` and `sourcetype="kube:object:events"` in the explicitly selected Logs connection. Offline rendered-chart tests and one collector-only live pod/event smoke passed; sanitized query proof is at `results/collector-smokes/pod-event-watch-2026-09-26.json`. That smoke does not prove a complete Lite incident or source-to-destination parity. Check host memory, disk, credentials, and per-case evidence gates before any rerun. Resource-reduced local runs remain non-comparable.

The checkpointed 2026-09-27–28 first-pass campaign report is `results/assistant-v3-lite-pilot-2026-09-27.md`, with one valid scored attempt for each of 21 cases and per-case links to exact answers, judge critiques, native/ATIF traces, derived metrics, source/Splunk causal proofs, and representative delivery checks. Earlier pre-agent failures were excluded and their raw attempts moved to macOS Trash; only fresh attempts passing the corrected checks count. The `results/` directory is local and Git-ignored; preserve or export it before removing this worktree.

For case-by-case review, use the canonical local `results/by-case/<case-id>/` dossiers. They were built from the original raw runs and verified before the numbered run folders were moved to macOS Trash. The consolidated report now links directly to them. For a future campaign, run `python -m sregym.results.lite_case_dossiers --report <raw-campaign-report> --output <case-output>` **before** removing its raw run folders; this older campaign cannot be rebuilt from its rewritten, by-case-only report.

Each folder has a readable view of the exact saved starter prompt, the canonical benchmark oracle source expression, a separately labeled Splunk-visible evidence assessment, the rubric and execution metrics, and verified copies of the selected answer, native/ATIF traces, raw judge CSV, phase ledger, terminal submission, and delivery proofs. A local manifest records original source paths and SHA-256 hashes; its historical `original_attempt` path no longer exists under `results/` because those run folders were moved to Trash. The builder checks for one completed delivery-valid attempt, matching case IDs, report/judge score, and exact answer/judge submission before publishing; it does not alter raw traces. Most cases have a pre-agent causal proof but **not** an independent post-grade golden-telemetry audit, and no case has a verified separate numeric Splunk-visible score. The generated dossiers are Git-ignored; preserve or export them before removing the worktree.

Start with each folder's `verification.md` to see, in plain language, the provider-readiness checks, the case-specific pre-agent RCA clue and its saved source/Splunk result, and the post-run metrics/traces/logs/Kubernetes-object presence checks. The executable checkers are shared across cases in `sregym/results/splunk_lite_causal.py` and `sregym/results/splunk_lite_evidence.py`; each dossier contains current source snapshots (`pre_agent_verifier_source.py`, `postrun_verifier_source.py`) alongside its saved run-time JSON proof (`pre_agent_proof.json`, `postrun_signals.json`, `delivery_audit.json`). The snapshots are for code inspection or rerunning, **not** proof of the exact historical source version: these attempts did not save a verifier-source hash when they ran. Presence checks and collector-drain status do not prove complete source-to-Splunk delivery or that every oracle clue was available. The walkthrough explicitly separates confirmed checks from unverified candidate clues.
