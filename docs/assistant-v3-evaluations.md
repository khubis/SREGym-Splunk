# Assistant v3 evaluations with Splunk

This workflow runs the existing Assistant v3 product surface against SREGym-Lite diagnosis cases. SRE Gym deploys and faults the application, exports metrics, traces, container logs, and Kubernetes events to Splunk, waits until all four signals are queryable, and then starts one fresh Assistant session. The existing SRE Gym diagnosis judge remains authoritative.

Use `--profile full` for comparable results. `--profile svelte` is useful for a lower-cost smoke test, but its persisted `comparable` value is `false` and it must not be compared with full-profile results.

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

The Assistant URL must be reachable from the isolated agent container. The Splunk org must have an accessible Logs Observer connection. Set `SPLUNK_LOGS_CONNECTION_ID` when the HEC destination is not the org's default connection; otherwise, the provider selects the accessible default connection, or the first accessible connection when no default exists. Do not add connection, service, scenario, or time-window hints to the model prompt.

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

The commands below use `--force-build` so the container includes this checkout's Assistant adapter and observability integration. Docker reuses unchanged build layers after the first build.

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
- `delivery.valid: true`, zero send/enqueue failure deltas, `drained: true`, and zero final queue sizes;
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
