# Repeat an Assistant V3 SREGym-Lite pilot

This is the short path from a single Lite case to a resumable 21-case run and a
reviewable evidence package. The wrapper delegates injection, the Assistant call,
ATIF conversion, the benchmark judge, and attempt checkpointing to `main.py`.
It then runs the separate bounded Splunk presence queries, copies the selected
attempts into one folder per case, and writes a campaign `summary.md`. It does **not** change the prompt or agent
tools. `svelte` plus the reviewed time-window/symptom prompt is a local pilot,
not an identical leaderboard comparison.
New attempts require run-scoped Pod-state, application-probe, and container-CPU
metric series before V3 starts. This catches the Kind kubelet-stats TLS failure
that older generic metrics checks missed; it does not retroactively validate
container CPU evidence in already-saved attempts.

## Credentials and target

For shareable documentation, the canonical environment names remain `SF_TOKEN`,
`SPLUNK_O11Y_INGEST_TOKEN`, `SFX_REALM`, `ORG_ID`, and `USER_ID`. Locally,
`--credentials synthetic` maps `SYNTHETIC_SF_TOKEN`,
`SYNTHETIC_SPLUNK_ACCESS_TOKEN`, `SYNTHETIC_REALM`, `SYNTHETIC_ORG_ID`, and
`SYNTHETIC_USER_ID` onto those names **for the child benchmark process**. It
fails if a canonical value is also set to a different value; unset the stale
canonical value before using this profile. In either profile, also provide
`SPLUNK_HOST`, `SPLUNK_HEC_PORT`, `SPLUNK_HEC_TOKEN`,
`SPLUNK_LOGS_CONNECTION_ID`, `ASSISTANT_V3_URL`, and
`ASSISTANT_V3_AUTH_TOKEN`. The synthetic access token must have ingest scope;
the HEC token and Logs connection must target the same logs destination.
Keep secrets in the shell or gitignored `.env`, never in a command argument or
result folder. The wrapper does not source `.env`.

Start the *LangChain Deep Agents Assistant V3* local server separately using the
**same** org/realm/user/token mapping. Its `make run-local-server-v3` target may
source the assistant repo's `.env` and override exported variables; inspect
that behavior or set `ASSISTANT_V3_ENV_FILE=""` and provide the intended
environment explicitly. The wrapper verifies the runner's target and ready
Kubernetes nodes; `main.py` then performs an authenticated Assistant session-list
preflight before injecting a fault. Neither check can attest which org an already-running server
was configured for. Confirm its startup environment uses the same synthetic org,
and use a dedicated eval database as described in
[the full guide](assistant-v3-evaluations.md).

## One-case smoke, then suite

From the SREGym worktree, with the local Assistant V3 server and Kind/Docker
ready, first use a prebuilt image that includes this checkout's adapter. This is
one command **from a prepared environment**, not cluster/server provisioning:

```bash
uv run --no-sync python -m sregym.results.assistant_v3_lite_repeat run \
  --credentials synthetic \
  --problem cronjob_sidecar_blocks_completion_hotel_reservation \
  --agent-image sregym-agent-base:latest \
  --output results/reproductions/cronjob-smoke
```

After reviewing `results/reproductions/cronjob-smoke/summary.md`, run the suite:

```bash
uv run --no-sync python -m sregym.results.assistant_v3_lite_repeat run \
  --credentials synthetic \
  --agent-image sregym-agent-base:latest \
  --output results/reproductions/lite-21
```

The command runs cases sequentially, one attempt each, using the svelte
profile, Assistant V3, GPT-5.6 Luna/medium, the matching API judge, Splunk,
and the reviewed symptom-guided prompt. It reuses the local image rather than
rebuilding it. Rebuild that image with the full-guide command after changing
containerized code; check its image ID before reusing it. The wrapper refuses
to start if the image is absent, another wrapper run holds the lock, fewer than
6 GiB of host memory or 10 GiB of disk are free, or Docker has under 8 GiB
allocated. It also requires Ready nodes in the selected `kubectl` context;
the benchmark runner subsequently checks Assistant authentication before injection.
These are *start gates*, not a guarantee against later pressure;
watch Docker/host memory during the first case. It never prunes resources.
On a host with less available RAM but otherwise low system memory pressure,
an operator can lower only the **single-case** start gate with
`--min-available-gib 4`; the 21-case suite retains the 6 GiB default. Monitor
memory during that smoke and stop if available RAM approaches 2 GiB. This is
an explicit local override, not the recommended repeatable default.

## Recover or review a partial batch

Every attempt is checkpointed in the raw timestamped batch. Keep that batch
until the per-case package is built and reviewed. If the command stops, the
message identifies the raw `results/MMDD_HHMM` batch. Repackage already valid
cases without launching another simulation:

```bash
uv run --no-sync python -m sregym.results.assistant_v3_lite_repeat finalize \
  --credentials synthetic \
  --batch results/MMDD_HHMM \
  --output results/reproductions/lite-21
```

To resume missing cases, pass the previous batch's
`assistant_v3_campaign/resume.csv` to `run`, and include the earlier batch(s)
via `--batch` so final selection sees completed attempts in both batches:

```bash
uv run --no-sync python -m sregym.results.assistant_v3_lite_repeat run \
  --credentials synthetic \
  --resume-csv results/MMDD_HHMM/assistant_v3_campaign/resume.csv \
  --batch results/MMDD_HHMM \
  --output results/reproductions/lite-21
```

The wrapper rejects a nonexistent resume CSV or duplicate valid attempts; it
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

The collector delivery audit also requires run-scoped export counters. If a
partial counter sample is missing at close, it waits at most the two-minute
SignalFlow ingest guard and rechecks that same historical interval. A total
query failure, unresolved counter, counter decrease, send failure, or undrained
queue remains invalid. The raw delivery artifact records initial and settled
counter samples so an excluded score can be diagnosed without assuming that
missing accounting means missing logs.

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

Check the wrapper itself before a campaign:

```bash
uv run --no-sync pytest -q tests/results/test_assistant_v3_lite_repeat.py tests/results/test_lite_case_dossiers.py
uv run --no-sync python -m sregym.results.assistant_v3_lite_repeat --help
```
