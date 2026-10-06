# Run Assistant V3 against Splunk with SREGym-Lite

This fork's wrapper runs one or all 21 Lite cases sequentially: inject a fault,
send metrics/traces/logs/events to your Splunk org, check representative causal
telemetry before launching Assistant V3, collect its diagnosis and ATIF trace,
grade it with the unchanged benchmark judge, and package the results by case.
It uses the `svelte` deployment profile and a time-window/symptom prompt, so
the scores are a **local pilot, not leaderboard-comparable**. The wrapper does
not provision Docker, Kind, the Splunk org, or the separate Assistant server.

**Fast path:** prepare Kind and build the image (step 1), fill
`.env.splunk-lite` (step 2), start Assistant V3 and mint its local JWT (step 3),
then use the single evaluation command in step 4. Repeat that command with the
same output directory to resume valid cases.

## 1. Prepare the benchmark checkout

Install Python 3.12+, Docker, Kind, kubectl, Helm 4+, and uv from the
[repository requirements](../README.md#requirements). Keep the corporate VPN
connected for Splunk and lab0 Gateway access. Cisco developers using the
Splunk Artifactory configuration must authenticate before dependency setup:

```bash
dev-login artifactory >/dev/null
```

The redirect keeps the login command's credential exports out of terminal
logs. An Artifactory HTTP 401 during `uv sync` requires refreshing this login;
it is separate from the Splunk query token and gateway access.

Use `khubis/SREGym` (the fork with this Splunk adapter), not
`SREGym/SREGym` upstream (which has no Splunk adapter). For a fresh
SSH-authenticated clone after this documentation is merged:

```bash
git clone --branch main --recurse-submodules git@github.com:khubis/SREGym.git
cd SREGym
```

From that checkout (or an existing checkout on the fork's `main` branch):

```bash
git submodule update --init --recursive
uv sync --group dev
docker info >/dev/null
kubectl get nodes                 # all nodes must be Ready
bash docker/agents/build.sh      # builds sregym-agent-base:latest from this checkout
```

If there is no Kind cluster yet, follow [Kind setup](../kind/README.md) first.
The image must be rebuilt after changing containerized code. Do not use the
published SREGym agent image for this fork's Assistant adapter.

The wrapper uses `.local/helm/repositories.yaml` and `.local/helm/repository`
for chart repositories and their cache. This prevents unrelated repositories
in your global Helm configuration from blocking a benchmark dependency update.
The directories are created automatically; Helm downloads missing chart
dependencies during deployment. Explicit `HELM_REPOSITORY_CONFIG` and
`HELM_REPOSITORY_CACHE` exports override these defaults.

## 2. Configure the Splunk target and judge

Copy the [fill-in template](../.env.splunk-lite.example) to the gitignored
`.env.splunk-lite` in this checkout and fill its Splunk blanks. The JWT can be
exported in step 3. Do not overwrite an existing private file or commit secrets.

```bash
cp -n .env.splunk-lite.example .env.splunk-lite
# Edit .env.splunk-lite: fill the Splunk values; keep the gateway defaults.
chmod 600 .env.splunk-lite
```

| Variables | Where to get them / purpose |
|---|---|
| `SYNTHETIC_SF_TOKEN`, `SYNTHETIC_REALM`, `SYNTHETIC_ORG_ID`, `SYNTHETIC_USER_ID` | Query token and identity for the **same** synthetic Splunk Observability org. |
| `SYNTHETIC_SPLUNK_ACCESS_TOKEN` | Org access token with `INGEST` scope for the collector; this is not the SF query token. |
| `SPLUNK_HOST`, `SPLUNK_HEC_PORT`, `SPLUNK_HEC_TOKEN`, `SPLUNK_LOGS_CONNECTION_ID` | Logs HEC destination and the Logs Observer connection pointing to it. The host is a hostname only. |
| `ASSISTANT_V3_URL`, `ASSISTANT_V3_AUTH_TOKEN` | Running v3 server URL and its raw bearer JWT (generate below; do not include `Bearer ` in the value). |
| `SREGYM_LITE_JUDGE_MODEL`, `JUDGE_API_BASE`, `JUDGE_API_KEY` | The template uses the lab0 LLM Gateway (`openai/gpt-5.6-luna` and `/openai/v1`). Its API key is an unused LiteLLM placeholder, not an Azure secret. |
| `SREGYM_LLM_GATEWAY_SERVICE_NAME` | Gateway attribution name (`sregym`). The wrapper derives `X-Org-ID` from the selected Splunk org; do not enter that ID twice. |

`SPLUNK_LOGS_CONNECTION_ID` is the **ID of the Logs Observer connection in
this org**, not the HEC token. That connection must point to the same HEC
destination that receives this run's logs and Kubernetes events.

`uv run --env-file .env.splunk-lite` loads this private file for the one-line
commands below. The wrapper does **not** load it by itself. With `--credentials
synthetic`, it maps the five `SYNTHETIC_*` values to canonical `SF_TOKEN`,
`SPLUNK_O11Y_INGEST_TOKEN`, `SFX_REALM`, `ORG_ID`, and `USER_ID` only for the
benchmark child. If your shell also has one of those canonical names set to a
different value, the wrapper fails rather than risk using two orgs; unset the
stale canonical value. Start from a clean shell so previously exported
`SYNTHETIC_*` variables do not override the private file. `SPLUNK_HEC_INDEX`
is optional (`main` by default) and must be permitted by the HEC token. Never
print or commit token values.

The old direct lab0 Azure endpoint may return HTTP 403 from a laptop even
with VPN connected. The gateway is the intended local route; follow the
[internal LLM Gateway onboarding guide](https://splunk.atlassian.net/wiki/spaces/PROD/pages/1079904469389/Onboarding+onto+LLM-Gateway)
if its URL is unreachable. The template's judge endpoint includes `/openai/v1`.
Assistant V3's separate gateway setting below uses the root URL instead.
If you have an approved non-gateway endpoint, set
`SREGYM_LITE_JUDGE_MODEL=azure/gpt-5.6-luna`, supply its authorized
`JUDGE_API_BASE` and `JUDGE_API_KEY`, and leave
`SREGYM_LLM_GATEWAY_SERVICE_NAME` empty. Use a different output directory;
the wrapper will not mix judge routes in one campaign.

## 3. Start Assistant V3 separately

This is the **LangChain Deep Agents V3** server from a separate `assistant`
checkout, not another Assistant implementation. Its location is independent of
this SREGym checkout (especially when using a Codex worktree). Install its
dependencies using its `README.md`. Copy the companion
[Assistant environment template](../.env.assistant-v3-splunk.example) into
that checkout as `.env.sregym`. Set its `SF_TOKEN`, `SFX_REALM`, `ORG_ID`, and
`USER_ID` to the **same values** as the synthetic variables above.

The companion template requires the Assistant V3 gateway embedding opt-in change
in [Assistant MR !3836](https://cd.splunkdev.com/observability/ai/assistant/-/merge_requests/3836)
(internal Splunk access required). Until it is merged, use its published branch:

```bash
git fetch origin codex/assistant-v3-gateway-embeddings
git switch codex/assistant-v3-gateway-embeddings
```

Run these in the separate Assistant checkout; preserve any local edits before
switching branches. The live qualification used this branch. Do not assume an
older Assistant checkout recognizes the embedding configuration. After merge,
a main checkout containing the MR is sufficient.

There are two independently configured model connections:

| Connection | Configuration / requirement |
|---|---|
| V3 chat | The template enables the lab0 LLM Gateway and GPT-5.6 Luna/medium. |
| Semantic-memory embeddings | `ASSISTANT_V3_MEMORY_USE_LLM_GATEWAY_SERVICE=true` opts in to the gateway. `LLM_GATEWAY_SERVICE_URL` is the root URL; the template selects `text-embedding-3-large` (3072 dimensions). |

For the DNS gateway route above, no lab0 Kubernetes proxy, separate lab0 SF
token, or direct Azure API key is needed. Confirm gateway access through the
onboarding guide and run the embedding preflight below. The SDK's placeholder
key is not authentication; gateway access controls must authenticate/authorize
callers. Org/service headers are attribution, not a security boundary.

Use a **fresh dedicated memory database** for large-model vectors. Do not reuse
a database containing small-model vectors. `AIMEMORY_DB_NAME` and
`MEMORY_PG_DATABASE` must match; the V3 Make target exports the former as the
latter. This opt-in does not change the separate legacy documentation-search
embedding path. The template also gives DeepEval an explicit read-only cache
location, avoiding a startup error caused by an empty generated cache path.

Do not set `ASSISTANT_V3_OPENAI_BASE_URL` when gateway mode is enabled; V3
rejects that combination. The Assistant server derives its own gateway
`X-Org-ID` and `X-Service-Name` headers from its authenticated identity.
Other Assistant dependencies still follow that repo's setup guide. In a
separate terminal (substitute the absolute path to this SREGym checkout):

```bash
cd /absolute/path/to/assistant
cp -n /absolute/path/to/SREGym/.env.assistant-v3-splunk.example .env.sregym
# Edit .env.sregym: fill the synthetic identity and choose fresh eval DB names.
chmod 600 .env.sregym
set -a
source .env.sregym
set +a
python3 scripts/local_auth_jwt.py >/dev/null || exit 1  # refresh SF_TOKEN if HTTP 401
uv run --locked python -c 'import asyncio; from src.server.assistant_v3.semantic_memory.embedder import SemanticMemoryEmbedder; e=SemanticMemoryEmbedder(); v=asyncio.run(e.embed_one("SREGym setup check")); assert len(v)==e.dimension; print("Embedding preflight passed:", len(v), "dimensions")' || exit 1
make -s postgres-up
docker exec aiassistantdb createdb -U postgres sregym_gateway_eval
ASSISTANT_V3_ENV_FILE=.env.sregym make -B run-local-server-v3
```

The command above assumes you created `assistant/.env.sregym`; the Assistant
repo ignores `.env*`. It listens on `http://127.0.0.1:8903` by default.
Specifying `ASSISTANT_V3_ENV_FILE` prevents an unrelated `assistant/.env` from
silently replacing the synthetic org. `-B` regenerates cached local server
configuration so stale generated values cannot override the new settings.
The companion template selects dedicated session, semantic-memory,
and tool databases rather than reusing development memories from other
incidents. `postgres-up` creates the tool and semantic-memory databases from
the loaded env; the explicit `createdb` creates the session database.

Run `createdb` once; an “already exists” response on later runs means the
database can be reused. Keep the generated configuration and local JWT files
private. The embedding preflight must pass before deploying a case. Wait for
`Application startup complete` before running the evaluation. Use a fresh
semantic-memory database name for a clean campaign; simply reusing the same
database across campaigns is not memory isolation. Per-case memory isolation
within a suite is not automatically enforced by this runner.

The qualified startup also used `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`
because this laptop already had the required Hugging Face models cached. These
optional flags avoid download/certificate retries; do not enable them on a
first-time setup without cached models. No alternate server entry point or
benchmark-specific Assistant tool surface was used.

Back in the SREGym shell, point to that Assistant checkout and load the private
file only to mint the JWT from the **same synthetic identity**.
The Assistant helper refreshes its private `assistant/.local/local_auth.jwt`
cache when needed. Command substitution keeps the token out of terminal output:

```bash
ASSISTANT_REPO=/absolute/path/to/assistant
set -a; source .env.splunk-lite; set +a
export ASSISTANT_V3_AUTH_TOKEN="$(
  cd "$ASSISTANT_REPO" &&
  SF_TOKEN="$SYNTHETIC_SF_TOKEN" SFX_REALM="$SYNTHETIC_REALM" \
    ORG_ID="$SYNTHETIC_ORG_ID" USER_ID="$SYNTHETIC_USER_ID" \
    python3 scripts/local_auth_jwt.py
)"
```

The nonempty shell JWT takes precedence over the blank template entry when
`uv --env-file` loads it. Alternatively, put the generated JWT into the
private `.env.splunk-lite` and refresh it if it expires. Never put it in the
tracked example.

If either JWT command returns HTTP 401, refresh the synthetic **SF query token**
in both private env files before proceeding. Replacing the HEC token or the
collector's INGEST token will not fix this authentication failure.

The runner checks that it can authenticate to Assistant before injecting a
fault, but cannot inspect which org an already-running Assistant server uses.
Confirm its startup configuration matches this file. Refresh the JWT if it
expires during a long suite.

## 4. Smoke-test one case, then run the suite

This is one command **after** the setup above, not a provisioning command:

```bash
uv run --env-file .env.splunk-lite --no-sync python -m sregym.results.assistant_v3_lite_repeat run --credentials synthetic --problem cronjob_sidecar_blocks_completion_hotel_reservation --output results/reproductions/cronjob-smoke
```

After reviewing `results/reproductions/cronjob-smoke/summary.md`, run the suite:

```bash
uv run --env-file .env.splunk-lite --no-sync python -m sregym.results.assistant_v3_lite_repeat run --credentials synthetic --output results/reproductions/lite-21
```

The image flag is omitted because the wrapper defaults to the locally built
`sregym-agent-base:latest`. Use `--agent-image` only if you built a different
tag. The one-line command starts the **evaluation**, not the Assistant server
or Kind. Changing the judge route/model creates a different campaign identity;
use a new output directory instead of trying to resume an old direct-Azure run.

The command runs cases sequentially in separate benchmark child processes,
one attempt each, using the svelte
profile, Assistant V3, GPT-5.6 Luna/medium, the matching API judge, Splunk,
and the reviewed symptom-guided prompt. Assistant execution is limited to
1,200 seconds per case; grading and cleanup can take additional time. A timeout
is an incomplete attempt, never a zero diagnosis score. It reuses the local image rather than
rebuilding it. Rebuild that image with the full-guide command after changing
containerized code; check its image ID before reusing it. The wrapper refuses
to start if the image is absent, another wrapper run holds the lock, fewer than
10 GiB of disk are free, or Docker has under 8 GiB
allocated. It also requires Ready nodes in the selected `kubectl` context;
the benchmark runner subsequently checks Assistant authentication before injection.
These checks are repeated before **every** case. Host available memory below
6 GiB prints a **warning only**; it does not block or wait. The existing
`--min-available-gib` option changes that advisory threshold, not a safety floor.
Available memory is a point-in-time estimate, not a measurement of Docker's
remaining capacity or a prediction of the next case's needs. Monitor host/Docker
memory and stop if pressure becomes severe. Disk/Docker checks remain start
gates, not a guarantee against later pressure. The wrapper never prunes resources.

## Recover or review a partial batch

Every attempt is checkpointed in its raw timestamped batch. The wrapper also
writes `campaign.json` in the output folder before launch, records each new
batch there, and updates `summary.md` after each case. Keep raw batches until
the package is reviewed. If a case fails, inspect its saved artifacts, address
the cause, then run the **same command with the same `--output`**. It skips
previously valid cases, retains invalid attempts, and retries only unfinished
cases on that later invocation. It refuses a changed org, realm, Logs
connection, Assistant URL, image tag, case selection, or model configuration;
use a new output folder for a new campaign. If a crash happens before any
case checkpoint, inspect the raw batch and Kubernetes cleanup before retrying.

To rebuild a report from known raw batches without launching a simulation:

```bash
uv run --env-file .env.splunk-lite --no-sync python -m sregym.results.assistant_v3_lite_repeat finalize \
  --credentials synthetic \
  --batch results/MMDD_HHMM \
  --output results/reproductions/lite-21
```

To resume the same campaign, repeat the original `run` command:

```bash
uv run --env-file .env.splunk-lite --no-sync python -m sregym.results.assistant_v3_lite_repeat run --credentials synthetic --output results/reproductions/lite-21
```

`run` does not accept manual `--batch` or `--resume-csv` arguments; those are
legacy lower-level runner controls. The wrapper rejects duplicate valid attempts; it
does not silently pick a favorable result. It also refuses a post-run Splunk
query error. A *missing* signal is kept as missing in the saved proof, not
converted into success. Open `summary.md` first: it reports valid benchmark
scores separately from classified invalid or missing attempts, with a mean
whose denominator is only valid attempts. Even an all-invalid batch gets a
summary. `selection.md` is the raw selection record; `by-case/<case-id>/`
contains the prompt, benchmark oracle, saved
pre-agent causal proof, representative post-run metrics/traces/logs/object
queries, final answer, native and ATIF traces, raw judge result, metrics, and
SHA-256 provenance manifest. `verification.md` distinguishes confirmed
telemetry from candidate clues. This is a representative presence and causal
gate, **not** exhaustive ingestion parity. Splunk-visible numeric grading and
independent post-grade oracle audit remain separate review work; the wrapper
does not claim them as complete.

The campaign record contains only a fingerprint of non-secret target and run
settings, not tokens. Saved scored attempts are checked for the selected Logs
connection, `svelte` profile, model, reasoning effort, and judge before reuse.
The authenticated Assistant preflight proves connectivity, **not** the org
configured inside an independently started V3 server. The operator must still
confirm its startup environment uses the intended synthetic org. This is the
remaining identity limitation; the wrapper does not claim automatic server-org
attestation.

The collector delivery audit also requires run-scoped export counters. If a
partial counter sample is missing at close, it waits at most the two-minute
SignalFlow ingest guard and rechecks that same historical interval. A total
query failure, unresolved counter, counter decrease, send failure, or undrained
queue remains invalid. The raw delivery artifact records initial and settled
counter samples so an excluded score can be diagnosed without assuming that
missing accounting means missing logs.

A read token can also be revoked or rotated while a long case runs. An opening
evidence check does not prove that closing queries will still be authorized.
If `delivery_audit.json` has `valid: false`, closing `4xx` signals, and unknown
counter deltas, check the current `SF_TOKEN` (or `SYNTHETIC_SF_TOKEN`) against
SignalFlow and the configured Logs connection before resuming. Keep the score
excluded, refresh the token, and rerun the same command/output after confirming
the Assistant server uses the same org. Do not change the prompt or bypass the
delivery gate to salvage a provisional judge score.

Each valid summary row links directly to its exact answer, benchmark oracle,
unchanged judge CSV, pre-agent verification, and native/ATIF traces. Its
"pre-agent causal check" column is bounded evidence, not proof that every
emitted telemetry item reached Splunk. Invalid rows link to raw attempt
metadata; they have no manufactured score or dossier. Retain the raw batch
until the package is inspected, and use the resume commands above after an
interruption.

This release supports the 21 Lite cases with the `svelte` deployment profile
only. The larger case catalog and standard-profile execution require separate
qualification; neither is implied by this pilot command.

The 2026-10-01 **direct-Azure** local qualification package is at
`results/reproductions/lite-21-hardened-20260930-v2/summary.md`: 21 valid
cases, none missing, 43.05/100 mean. Case 21's selected attempt has all four
closing signal checks ready, drained collector queues, and zero send/enqueue
failures. Earlier invalid attempts remain in timestamped raw batches, not in
the selected per-case score. This is a local pilot result, not a leaderboard
comparison or proof of exhaustive MELT completeness.
It predates this gateway setup. The gateway smoke qualification below covers
one case, not a rerun of the 21-case suite or a comparable performance result.

### Gateway quickstart verification status (2026-10-05)

Earlier local CronJob attempts reached these checkpoints:

- Judge gateway request succeeded; the Assistant chat gateway made real tool calls.
- Deployment initially failed on an unrelated global Helm repository. Checkout-local
  Helm repository configuration fixed that failure on the next attempt.
- Opening metrics, traces, logs, and object checks, including representative
  causal evidence, passed before Assistant execution.
- Assistant then failed in semantic-memory embeddings because its separate
  connection was unconfigured. No final answer or valid score was produced.

These failures motivated the Assistant V3 opt-in gateway embedding fix. A real
gateway/pgvector memory round trip passed with 3072-dimensional vectors in the
fresh database `sregym_gateway_memory_20261005`. Earlier invalid attempts remain
preserved; they were not converted into scores.

The next CronJob attempt **completed the full workflow** using Assistant from
the fixed worktree, GPT-5.6 Luna/medium, and the template's gateway settings:

- Pre-agent metrics, traces, logs, and Kubernetes event checks passed. The causal
  check matched two source/Splunk Job pods with Completed archivers and running
  regular sidecars. This is representative evidence, not exhaustive delivery proof.
- A terminal final answer was submitted exactly once. The raw judge submission
  and the final ATIF message match that answer byte-for-byte.
- The unchanged judge scored **0/100**: V3 diagnosed recommendation-service
  connectivity instead of the CronJob sidecar fault. Workflow qualification is
  not a claim of diagnosis accuracy; five tool errors remain visible in the trace.
- Native JSONL and ATIF-v1.7 traces are saved (51 ATIF steps), alongside the prompt,
  raw judge critique, verification receipts, and metrics: 259.8 s, 29 tool calls,
  5 failed tool results, and 1,504,307 reported tokens.
- The closing audit was valid and drained: zero send/enqueue failure deltas and
  zero final queue sizes for all four signal pipelines.
- Repeating the identical command/output packaged the existing valid case with
  no new raw batch or simulation. Cleanup completed normally.

The local, gitignored package is
`results/reproductions/gateway-quickstart-smoke/summary.md`; the selected attempt
is linked under its `by-case/` folder, with raw provenance in `results/1005_1734`.
This constrained-host smoke used `--min-available-gib 4`; the documented command
defaults to 6 GiB. It does not qualify every laptop, the complete suite, or the
gateway's production authentication/authorization controls. The required
Assistant change is published in MR !3836; use its branch until merged.

### Sequential suite-path smoke (2026-10-05)

With host-memory checking changed to warning-only, the suite CLI selected all
21 cases without `--problem`. A local test-only boundary stopped it before case
three, after the first two completed cleanup and checkpointing. CronJob (Hotel
Reservation) scored 89/100; edge/WAF (Astronomy Shop) scored 44/100. Both passed
the representative pre-agent evidence gates and closing delivery audits; native
and ATIF traces were saved, and each final answer matched its judge submission.
The local package is `results/reproductions/lite-suite-smoke-20261005/summary.md`.
The remaining 19 cases were intentionally not run: this qualifies the sampled
sequential workflow, not the full suite or a performance comparison.

Check the wrapper itself before a campaign:

```bash
uv run --no-sync pytest -q tests/results/test_assistant_v3_lite_repeat.py tests/results/test_lite_case_dossiers.py tests/problems/test_search_rate_retry_collapse.py
uv run --no-sync python -m sregym.results.assistant_v3_lite_repeat --help
```

Do not run the unfiltered `tests/problems` tree against an active Kind cluster:
it includes live integration tests that deploy applications. The focused
wrapper, dossier, and `test_search_rate_retry_collapse.py` tests above are
non-live. If a case reports `cleanup_timeout_after_agent_exit`, its provisional
judge score is excluded; inspect and reconcile only that case's task-owned
cluster resources, then rerun the same campaign command after the underlying
cleanup fault is fixed. The original incomplete attempt remains in its raw batch.
