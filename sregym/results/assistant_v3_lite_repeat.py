"""Small, fail-closed entry point for repeatable Assistant V3 Lite pilots.

The benchmark runner remains responsible for injection, agent execution, grading,
and per-attempt checkpoints. This module only validates the target and host,
starts one sequential batch, and packages already completed attempts.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

import psutil

from sregym.conductor.problem_sets import SREGYM_LITE_PROBLEMS
from sregym.results.assistant_v3_campaign import checkpoint_attempt
from sregym.results.lite_case_dossiers import build as build_dossiers


class RepeatError(ValueError):
    """Unsafe or incomplete pilot setup or artifact selection."""


_ALIASES = {
    "SYNTHETIC_SF_TOKEN": "SF_TOKEN",
    "SYNTHETIC_SPLUNK_ACCESS_TOKEN": "SPLUNK_O11Y_INGEST_TOKEN",
    "SYNTHETIC_REALM": "SFX_REALM",
    "SYNTHETIC_ORG_ID": "ORG_ID",
    "SYNTHETIC_USER_ID": "USER_ID",
}
_REQUIRED = (
    "SF_TOKEN",
    "SPLUNK_O11Y_INGEST_TOKEN",
    "SFX_REALM",
    "ORG_ID",
    "USER_ID",
    "SPLUNK_HOST",
    "SPLUNK_HEC_PORT",
    "SPLUNK_HEC_TOKEN",
    "SPLUNK_LOGS_CONNECTION_ID",
    "ASSISTANT_V3_URL",
    "ASSISTANT_V3_AUTH_TOKEN",
    "JUDGE_API_KEY",
    "JUDGE_API_BASE",
)
_DEFAULT_JUDGE_MODEL = "azure/gpt-5.6-luna"
_JUDGE_MODELS = {_DEFAULT_JUDGE_MODEL, "openai/gpt-5.6-luna"}
_GIB = 1024**3
_EXPECTED_CHECKS = frozenset({"metrics", "traces", "logs", "kubernetes_events", "pods", "events"})
_SUMMARY_LINKS = {
    "README.md": "review",
    "starter_prompt.md": "prompt",
    "final_answer.md": "answer",
    "benchmark_ground_truth.md": "oracle",
    "splunk_visible_ground_truth.md": "Splunk limits",
    "judge_raw.csv": "judge",
    "verification.md": "evidence",
    "atif_trace.json": "ATIF",
    "native_trace.jsonl": "native trace",
    "rubric_and_metrics.md": "metrics",
}


def resolve_environment(source: Mapping[str, str], *, profile: str) -> dict[str, str]:
    """Map the local alias profile without silently mixing two target orgs."""
    if profile not in {"canonical", "synthetic"}:
        raise RepeatError("credentials must be canonical or synthetic")
    result = dict(source)
    if profile == "synthetic":
        missing_aliases = [name for name in _ALIASES if not result.get(name)]
        if missing_aliases:
            raise RepeatError("missing required environment: " + ", ".join(missing_aliases))
        for alias, canonical in _ALIASES.items():
            if result.get(canonical) and result[canonical] != result[alias]:
                raise RepeatError(f"conflicting {canonical} and {alias}; unset one profile")
            result[canonical] = result[alias]
    missing = [name for name in _REQUIRED if not result.get(name)]
    if missing:
        raise RepeatError("missing required environment: " + ", ".join(missing))
    judge_model = result.get("SREGYM_LITE_JUDGE_MODEL", _DEFAULT_JUDGE_MODEL).strip()
    if judge_model not in _JUDGE_MODELS:
        raise RepeatError("SREGYM_LITE_JUDGE_MODEL must be azure/gpt-5.6-luna or openai/gpt-5.6-luna")
    result["SREGYM_LITE_JUDGE_MODEL"] = judge_model
    service = result.get("SREGYM_LLM_GATEWAY_SERVICE_NAME", "").strip()
    gateway_org = result.get("SREGYM_LLM_GATEWAY_ORG_ID", "").strip()
    if service:
        if judge_model != "openai/gpt-5.6-luna":
            raise RepeatError("gateway headers require SREGYM_LITE_JUDGE_MODEL=openai/gpt-5.6-luna")
        if gateway_org and gateway_org != result["ORG_ID"]:
            raise RepeatError("gateway org differs from the target Splunk org")
        result["SREGYM_LLM_GATEWAY_ORG_ID"] = result["ORG_ID"]
        result["SREGYM_LLM_GATEWAY_SERVICE_NAME"] = service
    elif gateway_org:
        raise RepeatError("SREGYM_LLM_GATEWAY_ORG_ID requires SREGYM_LLM_GATEWAY_SERVICE_NAME")
    return result


def prepare_helm_environment(environment: dict[str, str], repository: Path) -> None:
    """Keep unrelated user chart repositories out of benchmark dependency updates."""
    root = repository / ".local/helm"
    config = Path(environment.setdefault("HELM_REPOSITORY_CONFIG", str(root / "repositories.yaml")))
    cache = Path(environment.setdefault("HELM_REPOSITORY_CACHE", str(root / "repository")))
    config.parent.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)


def check_headroom(
    *, available_bytes: int, free_disk_bytes: int, docker_memory_bytes: int, min_available_gib: float = 6.0
) -> None:
    """Warn on host memory; require disk/Docker capacity, never auto-prune."""
    if not 4.0 <= min_available_gib <= 6.0:
        raise RepeatError("memory floor must be between 4 and 6 GiB")
    if available_bytes < min_available_gib * _GIB:
        print(
            f"Warning: {available_bytes / _GIB:.1f} GiB available host memory is below "
            f"the {min_available_gib:g} GiB advisory threshold; continuing. "
            "Monitor host/Docker memory; this snapshot does not predict case requirements.",
            file=sys.stderr, flush=True,
        )
    if free_disk_bytes < 10 * _GIB:
        raise RepeatError("less than 10 GiB free disk")
    if docker_memory_bytes < 8 * _GIB:
        raise RepeatError("Docker is allocated less than 8 GiB memory")


def suite_command(
    *,
    repository: Path,
    agent_image: str,
    problem: str | None,
    resume_csv: Path | None,
    judge_model: str = _DEFAULT_JUDGE_MODEL,
) -> list[str]:
    if problem is not None and problem not in SREGYM_LITE_PROBLEMS:
        raise RepeatError(f"not a Lite case: {problem}")
    if resume_csv is not None and not resume_csv.is_file():
        raise RepeatError(f"resume CSV does not exist: {resume_csv}")
    if not agent_image.strip():
        raise RepeatError("agent image tag is required")
    command = [
        sys.executable,
        str(repository / "main.py"),
        "--problem" if problem else "--suite",
        problem or "sregym-lite",
        "--stages",
        "diagnosis",
        "--profile",
        "svelte",
        "--agent",
        "assistant_v3",
        "--model",
        "gpt-5.6-luna",
        "--reasoning-effort",
        "medium",
        "--judge-model",
        judge_model,
        "--judge-backend",
        "api",
        "--observability-provider",
        "splunk",
        "--assistant-prompt-arm",
        "symptom_guided",
        "--agent-image",
        agent_image,
        "--n-attempts",
        "1",
        "--agent-timeout",
        "1200",
    ]
    if resume_csv:
        command += ["--resume", str(resume_csv)]
    return command


def _valid(run: Path, case_id: str) -> bool:
    try:
        metadata = json.loads((run / "run_metadata.json").read_text(encoding="utf-8"))
        delivery = json.loads((run / "observability/delivery.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        metadata.get("problem_id") == case_id
        and metadata.get("classification") == "completed"
        and metadata.get("observability_provider") == "splunk"
        and delivery.get("valid") is True
    )


def select_valid_runs(batches: Sequence[Path], expected_ids: Sequence[str]) -> tuple[dict[str, Path], list[str]]:
    if len(set(expected_ids)) != len(expected_ids):
        raise RepeatError("expected case IDs must be distinct")
    selected: dict[str, Path] = {}
    for batch in batches:
        for case_id in expected_ids:
            for run in sorted((batch / "assistant_v3" / case_id).glob("run_*")):
                if not _valid(run, case_id):
                    continue
                if case_id in selected:
                    raise RepeatError(f"multiple valid attempts for {case_id}; choose one batch")
                selected[case_id] = run
    return selected, [case for case in expected_ids if case not in selected]


def validate_selected_identity(selected: Mapping[str, Path], environment: Mapping[str, str]) -> None:
    """Reject scored attempts produced under a different reviewed Lite configuration."""
    expected = {
        "observability_provider": "splunk",
        "benchmark_profile": "svelte",
        "logs_connection_id": environment["SPLUNK_LOGS_CONNECTION_ID"],
        "requested_model": "gpt-5.6-luna",
        "requested_reasoning": "medium",
        "judge_model": environment.get("SREGYM_LITE_JUDGE_MODEL", _DEFAULT_JUDGE_MODEL),
    }
    for case_id, run in selected.items():
        try:
            metadata = json.loads((run / "run_metadata.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RepeatError(f"unreadable attempt identity for {case_id}") from error
        for field, value in expected.items():
            if metadata.get(field) != value:
                raise RepeatError(f"attempt {case_id} has mismatched {field} identity")


def write_selection_report(report: Path, selected: Mapping[str, Path], missing: Sequence[str]) -> None:
    """The named-case scorecard links are also the dossier builder's input."""
    lines = [
        "# Assistant V3 Lite selection",
        "",
        "One valid attempt per listed case. These svelte, symptom-guided results are not leaderboard-comparable.",
        "",
        "| Case | Benchmark score /100 | Raw scorecard |",
        "|---|---:|---|",
    ]
    for case_id, run in selected.items():
        with (run / f"{case_id}_results.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        if len(rows) != 1:
            raise RepeatError(f"expected one judge row for {case_id}")
        score = float(rows[0]["Diagnosis.accuracy"])
        scorecard = run.parents[2] / "assistant_v3_campaign/scorecard.md"
        if not scorecard.is_file():
            raise RepeatError(f"missing raw scorecard for {case_id}")
        relative = os.path.relpath(scorecard, report.parent)
        lines.append(f"| [{case_id}]({relative}) | {score:g} | saved judge CSV in case dossier |")
    lines += ["", f"Completed: {len(selected)}. Missing: {len(missing)}.", ""]
    if missing:
        lines += ["Missing cases: " + ", ".join(missing), ""]
    report.parent.mkdir(parents=True, exist_ok=True)
    temporary = report.with_name(f".{report.name}.tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(report)


def _safe_status(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]*", value) else "unverified"


def write_campaign_summary(
    report: Path, selected: Mapping[str, Path], expected: Sequence[str], batches: Sequence[Path]
) -> None:
    """Summarize saved outcomes without grading or silently omitting failed cases."""
    lines = [
        "# Assistant V3 SREGym-Lite campaign",
        "",
        "This `svelte`, symptom-guided, Splunk-only pilot is **not leaderboard-comparable**. "
        "Scores are from the unchanged benchmark judge; Splunk-visible numeric grading is unverified.",
        "",
        "| Case | Outcome | Benchmark /100 | Pre-agent causal check | Evidence and raw results |",
        "|---|---|---:|---|---|",
    ]
    scores: list[float] = []
    invalid_count = 0
    for case_id in expected:
        if case_id in selected:
            folder = report.parent / "by-case" / case_id
            required_files = (*_SUMMARY_LINKS, "manifest.json", "pre_agent_proof.json")
            if any(not (folder / name).is_file() for name in required_files):
                raise RepeatError(f"incomplete case dossier for {case_id}")
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            proof = json.loads((folder / "pre_agent_proof.json").read_text(encoding="utf-8"))
            if manifest.get("case_id") != case_id:
                raise RepeatError(f"case dossier identity mismatch for {case_id}")
            score = float(manifest["benchmark_score"])
            if not 0 <= score <= 100:
                raise RepeatError(f"invalid benchmark score in case dossier for {case_id}")
            scores.append(score)
            causal = proof.get("causal", {})
            evidence = _safe_status(causal.get("status"))
            links = " · ".join(f"[{label}](by-case/{case_id}/{name})" for name, label in _SUMMARY_LINKS.items())
            lines.append(f"| {case_id} | valid | {score:g} | {evidence} | {links} |")
            continue
        attempts = [
            run
            for batch in batches
            for run in sorted((batch / "assistant_v3" / case_id).glob("run_*"))
            if run.is_dir()
        ]
        if not attempts:
            lines.append(f"| {case_id} | missing | — | — | — |")
            continue
        invalid_count += 1
        latest = attempts[-1]
        metadata_path = latest / "run_metadata.json"
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                metadata = {}
            status = _safe_status(metadata.get("classification")) if isinstance(metadata, dict) else "unverified"
            if status == "unverified":
                status = "incomplete"
            if status == "completed":
                status = "delivery_invalid"
            link = f"[attempt metadata]({os.path.relpath(metadata_path, report.parent)})"
        else:
            status, link = "incomplete", "—"
        lines.append(f"| {case_id} | {status} | — | — | {link} |")
    attempt_label = "attempt" if len(scores) == 1 else "attempts"
    mean = (
        f"{sum(scores) / len(scores):g}/100 across {len(scores)} valid {attempt_label}"
        if scores else "unavailable (0 valid attempts)"
    )
    lines[4:4] = [
        f"Benchmark mean: **{mean}**. Invalid: {invalid_count}; "
        f"missing: {len(expected) - len(scores) - invalid_count}.",
        "A confirmed causal check is representative evidence, not proof of complete MELT delivery.",
        "",
    ]
    report.parent.mkdir(parents=True, exist_ok=True)
    temporary = report.with_name(f".{report.name}.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(report)


def _host_preflight(repository: Path, agent_image: str, min_available_gib: float = 6.0) -> None:
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.MemTotal}}"], capture_output=True, text=True, timeout=20, check=True
        )
        docker_memory = int(result.stdout.strip())
        subprocess.run(["docker", "image", "inspect", agent_image], capture_output=True, timeout=20, check=True)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise RepeatError("Docker must be running with the prebuilt agent image available") from error
    check_headroom(
        available_bytes=psutil.virtual_memory().available,
        free_disk_bytes=shutil.disk_usage(repository).free,
        docker_memory_bytes=docker_memory,
        min_available_gib=min_available_gib,
    )


def wait_for_host_preflight(
    repository: Path,
    agent_image: str,
    min_available_gib: float,
    *,
    retries: int = 11,
    interval_seconds: float = 30,
) -> None:
    """Wait at most five minutes for transient host-memory pressure only."""
    if retries < 1 or interval_seconds <= 0:
        raise RepeatError("resource wait settings must be positive")
    for attempt in range(retries):  # pragma: no branch - final attempt returns or raises
        try:
            _host_preflight(repository, agent_image, min_available_gib)
            return
        except RepeatError as error:
            if not str(error).endswith("GiB available host memory") or attempt == retries - 1:
                raise
            time.sleep(interval_seconds)


def _prepared_runtime_preflight() -> None:
    """Reject an unready cluster; main.py performs authenticated Assistant preflight."""
    try:
        response = subprocess.run(
            ["kubectl", "get", "nodes", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        nodes = json.loads(response.stdout)["items"]
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise RepeatError("Kubernetes nodes are not ready or selected context is unavailable") from error
    if not isinstance(nodes, list) or not nodes or any(
        not isinstance(node, dict)
        or not any(
            isinstance(condition, dict)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", [])
        )
        for node in nodes
    ):
        raise RepeatError("Kubernetes nodes are not ready")


def _batches(repository: Path) -> set[Path]:
    if not (repository / "results").exists():
        return set()
    return {
        path
        for path in (repository / "results").iterdir()
        if path.is_dir() and len(path.name) == 9 and path.name[4] == "_" and path.name.replace("_", "").isdigit()
    }


def _campaign_fingerprint(
    expected: Sequence[str], environment: Mapping[str, str], profile: str, agent_image: str
) -> str:
    """Bind saved progress to non-secret target and execution identity."""
    identity = {
        "cases": list(expected),
        "profile": profile,
        "org": environment["ORG_ID"],
        "realm": environment["SFX_REALM"],
        "logs_connection": environment["SPLUNK_LOGS_CONNECTION_ID"],
        "logs_host": environment["SPLUNK_HOST"],
        "logs_port": environment["SPLUNK_HEC_PORT"],
        "logs_index": environment.get("SPLUNK_HEC_INDEX", "main"),
        "assistant_url": environment["ASSISTANT_V3_URL"],
        "agent_image": agent_image,
        "deployment_profile": "svelte",
        "prompt_arm": "symptom_guided",
        "agent_model": "gpt-5.6-luna",
        "judge_model": environment.get("SREGYM_LITE_JUDGE_MODEL", _DEFAULT_JUDGE_MODEL),
        "judge_endpoint": environment["JUDGE_API_BASE"],
        "judge_gateway_service": environment.get("SREGYM_LLM_GATEWAY_SERVICE_NAME", ""),
        "agent_timeout_seconds": 1200,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def save_campaign(output: Path, state: Mapping[str, object]) -> None:
    """Replace the small, credential-free checkpoint durably."""
    output.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(state, sort_keys=True, indent=2) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=".campaign.", dir=output)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output / "campaign.json")
        directory_descriptor = os.open(output, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def load_campaign(
    output: Path,
    repository: Path,
    expected: Sequence[str],
    environment: Mapping[str, str],
    profile: str,
    agent_image: str,
) -> dict[str, object]:
    """Read a prior checkpoint or create an empty one; never adopt unrelated output."""
    fingerprint = _campaign_fingerprint(expected, environment, profile, agent_image)
    path = output / "campaign.json"
    if not path.is_file():
        if output.exists() and any(output.iterdir()):
            raise RepeatError("output already contains artifacts without a campaign record")
        return {"version": 1, "fingerprint": fingerprint, "batches": [], "pending": None}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RepeatError("campaign record is unreadable") from error
    if not isinstance(state, dict) or state.get("version") != 1 or state.get("fingerprint") != fingerprint:
        raise RepeatError("campaign record belongs to a different target or configuration")
    batches = state.get("batches")
    if not isinstance(batches, list) or any(
        not isinstance(name, str)
        or re.fullmatch(r"\d{4}_\d{4}", name) is None
        or not (repository / "results" / name).is_dir()
        for name in batches
    ) or len(batches) != len(set(batches)):
        raise RepeatError("campaign record contains an invalid batch path")
    pending = state.get("pending")
    if pending is not None and (
        not isinstance(pending, dict)
        or pending.get("case_id") not in expected
        or not isinstance(pending.get("before"), list)
        or any(not isinstance(name, str) or re.fullmatch(r"\d{4}_\d{4}", name) is None for name in pending["before"])
    ):
        raise RepeatError("campaign record contains invalid pending work")
    return state


def recover_pending(output: Path, repository: Path, state: dict[str, object]) -> dict[str, object]:
    """Associate an interrupted child's one new raw batch, or fail on ambiguity."""
    pending = state.get("pending")
    if pending is None:
        return state
    assert isinstance(pending, dict)
    before = set(pending["before"])
    created = sorted(path.name for path in _batches(repository) if path.name not in before)
    if len(created) > 1:
        raise RepeatError("multiple raw batches appeared during interrupted case; inspect before resuming")
    batches = state["batches"]
    assert isinstance(batches, list)
    if created and created[0] not in batches:
        batches.append(created[0])
    state["pending"] = None
    save_campaign(output, state)
    return state


def _needs_postrun_query(run: Path, expected_connection: str) -> bool:
    metadata = json.loads((run / "run_metadata.json").read_text(encoding="utf-8"))
    if metadata.get("logs_connection_id") != expected_connection:
        raise RepeatError(f"attempt Logs connection differs from target: {run.parent.name}")
    path = run / "splunk_lite_delivery.json"
    if not path.is_file():
        return True
    proof = json.loads(path.read_text(encoding="utf-8"))
    if proof.get("logs_connection_id") != expected_connection:
        raise RepeatError(f"saved post-run proof targets another Logs connection: {run.parent.name}")
    if proof.get("case_id") != metadata.get("problem_id") or proof.get("run_id") != metadata.get("run_id"):
        raise RepeatError(f"saved post-run proof belongs to another attempt: {run.parent.name}")
    checks = proof.get("checks", {})
    return set(checks) != _EXPECTED_CHECKS or any(
        item.get("status") not in {"present", "missing"} for item in checks.values()
    )


def finalize(
    repository: Path, output: Path, batches: Sequence[Path], expected: Sequence[str], environment: Mapping[str, str]
) -> tuple[int, int]:
    """Preserve partial progress; query missing proof and build case dossiers."""
    if not batches:
        raise RepeatError("at least one raw batch is required")
    selected, missing = select_valid_runs(batches, expected)
    from sregym.observability.splunk import SplunkConfig, SplunkHttpBackend
    from sregym.results.splunk_lite_evidence import verify_case_delivery

    pending = [run for run in selected.values() if _needs_postrun_query(run, environment["SPLUNK_LOGS_CONNECTION_ID"])]
    if pending:
        backend = SplunkHttpBackend(SplunkConfig.from_env(environment))
        try:
            for run in pending:
                verify_case_delivery(run, backend=backend, expected_connection=environment["SPLUNK_LOGS_CONNECTION_ID"])
        finally:
            backend.close()
    for run in selected.values():
        if _needs_postrun_query(run, environment["SPLUNK_LOGS_CONNECTION_ID"]):
            raise RepeatError(f"post-run query failed for {run.parent.name}; retry finalize")
        checkpoint_attempt(run.parents[2], run)
    report = output / "selection.md"
    write_selection_report(report, selected, missing)
    if selected:
        build_dossiers(report, output / "by-case", repository)
    write_campaign_summary(output / "summary.md", selected, expected, batches)
    return len(selected), len(missing)


def run_campaign(
    repository: Path,
    output: Path,
    expected: Sequence[str],
    environment: Mapping[str, str],
    *,
    profile: str,
    agent_image: str,
    min_available_gib: float,
) -> int:
    """Execute one case per child, checkpointing and reporting after every case."""
    state = load_campaign(output, repository, expected, environment, profile, agent_image)
    state = recover_pending(output, repository, state)
    save_campaign(output, state)
    batches = [repository / "results" / name for name in state["batches"]]
    if batches:
        selected, _ = select_valid_runs(batches, expected)
        validate_selected_identity(selected, environment)
        finalize(repository, output, batches, expected, environment)
    else:
        write_selection_report(output / "selection.md", {}, expected)
        write_campaign_summary(output / "summary.md", {}, expected, [])
    selected, _ = select_valid_runs(batches, expected)
    for case_id in expected:
        if case_id in selected:
            continue
        wait_for_host_preflight(repository, agent_image, min_available_gib)
        _prepared_runtime_preflight()
        before = {path.name for path in _batches(repository)}
        if datetime.now().strftime("%m%d_%H%M") in before:
            raise RepeatError("a raw batch already exists for this minute; wait before running")
        state["pending"] = {"case_id": case_id, "before": sorted(before)}
        save_campaign(output, state)
        command = suite_command(
            repository=repository,
            agent_image=agent_image,
            problem=case_id,
            resume_csv=None,
            judge_model=environment.get("SREGYM_LITE_JUDGE_MODEL", _DEFAULT_JUDGE_MODEL),
        )
        command += ["--allow-agent-endpoint", environment["ASSISTANT_V3_URL"]]
        result = subprocess.run(command, cwd=repository, env=environment, check=False)
        state = recover_pending(output, repository, state)
        batches = [repository / "results" / name for name in state["batches"]]
        if not batches or batches[-1].name in before:
            raise RepeatError(f"runner created no raw batch for {case_id}; inspect results before resuming")
        selected, _ = select_valid_runs(batches, expected)
        validate_selected_identity(selected, environment)
        finalize(repository, output, batches, expected, environment)
        selected, _ = select_valid_runs(batches, expected)
        print(f"Finished {case_id}: {'valid' if case_id in selected else 'incomplete'}. Open {output / 'summary.md'}")
        if result.returncode or case_id not in selected:
            print(f"Campaign stopped after {case_id}; rerun the same command to continue.", file=sys.stderr)
            return 1
    print(f"Packaged {len(selected)} valid case(s). Open {output / 'summary.md'}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "finalize"))
    parser.add_argument("--credentials", choices=("canonical", "synthetic"), default="canonical")
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch", type=Path, action="append", default=[])
    parser.add_argument("--problem", choices=SREGYM_LITE_PROBLEMS)
    parser.add_argument("--resume-csv", type=Path)
    parser.add_argument("--agent-image", default="sregym-agent-base:latest")
    parser.add_argument(
        "--min-available-gib", type=float, default=6.0,
        help="host-memory warning threshold only (4–6 GiB); low memory does not block or wait",
    )
    args = parser.parse_args(argv)
    try:
        environment = resolve_environment(os.environ, profile=args.credentials)
        repository = args.repository.resolve(strict=True)
        prepare_helm_environment(environment, repository)
        output = args.output.resolve()
        if output == repository / "results" or not output.is_relative_to(repository / "results"):
            raise RepeatError("output must be a named folder under repository/results")
        expected = [args.problem] if args.problem else list(SREGYM_LITE_PROBLEMS)
        batches = [path.resolve(strict=True) for path in args.batch]
        if args.command == "run":
            if args.resume_csv or batches:
                raise RepeatError("run resumes from its output directory; do not pass --batch or --resume-csv")
            lock_path = repository / "results/.assistant_v3_lite.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RepeatError("another Lite wrapper run is already active") from error
                return run_campaign(
                    repository, output, expected, environment,
                    profile=args.credentials,
                    agent_image=args.agent_image,
                    min_available_gib=args.min_available_gib,
                )
        count, missing = finalize(repository, output, batches, expected, environment)
        print(f"Packaged {count} valid case(s); {missing} without valid scores. Open {output / 'summary.md'}")
        return 1 if missing else 0
    except (RepeatError, OSError, ValueError, KeyError) as error:
        print(f"Pilot wrapper: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
