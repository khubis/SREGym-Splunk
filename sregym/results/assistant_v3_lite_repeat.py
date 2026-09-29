"""Small, fail-closed entry point for repeatable Assistant V3 Lite pilots.

The benchmark runner remains responsible for injection, agent execution, grading,
and per-attempt checkpoints. This module only validates the target and host,
starts one sequential batch, and packages already completed attempts.
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import shutil
import subprocess
import sys
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
)
_GIB = 1024**3
_EXPECTED_CHECKS = frozenset({"metrics", "traces", "logs", "kubernetes_events", "pods", "events"})


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
    return result


def check_headroom(
    *, available_bytes: int, free_disk_bytes: int, docker_memory_bytes: int, min_available_gib: float = 6.0
) -> None:
    """Conservative 16 GiB laptop gate; never auto-prune user resources."""
    if not 4.0 <= min_available_gib <= 6.0:
        raise RepeatError("memory floor must be between 4 and 6 GiB")
    if available_bytes < min_available_gib * _GIB:
        raise RepeatError(f"less than {min_available_gib:g} GiB available host memory")
    if free_disk_bytes < 10 * _GIB:
        raise RepeatError("less than 10 GiB free disk")
    if docker_memory_bytes < 8 * _GIB:
        raise RepeatError("Docker is allocated less than 8 GiB memory")


def suite_command(*, repository: Path, agent_image: str, problem: str | None, resume_csv: Path | None) -> list[str]:
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
        "azure/gpt-5.6-luna",
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


def _batches(repository: Path) -> set[Path]:
    if not (repository / "results").exists():
        return set()
    return {
        path
        for path in (repository / "results").iterdir()
        if path.is_dir() and len(path.name) == 9 and path.name[4] == "_" and path.name.replace("_", "").isdigit()
    }


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
    if not selected:
        raise RepeatError("no completed valid attempts to package")
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
    build_dossiers(report, output / "by-case", repository)
    return len(selected), len(missing)


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
    parser.add_argument("--min-available-gib", type=float, default=6.0)
    args = parser.parse_args(argv)
    try:
        environment = resolve_environment(os.environ, profile=args.credentials)
        repository = args.repository.resolve(strict=True)
        output = args.output.resolve()
        if output == repository / "results" or not output.is_relative_to(repository / "results"):
            raise RepeatError("output must be a named folder under repository/results")
        expected = [args.problem] if args.problem else list(SREGYM_LITE_PROBLEMS)
        batches = [path.resolve(strict=True) for path in args.batch]
        if args.command == "run":
            if args.min_available_gib < 6.0 and args.problem is None:
                raise RepeatError("reduced memory floor is permitted only for a single-case smoke")
            _host_preflight(repository, args.agent_image, args.min_available_gib)
            lock_path = repository / "results/.assistant_v3_lite.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("w") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RepeatError("another Lite wrapper run is already active") from error
                if (repository / "results" / datetime.now().strftime("%m%d_%H%M")).exists():
                    raise RepeatError("a raw batch already exists for this minute; wait before running")
                command = suite_command(
                    repository=repository,
                    agent_image=args.agent_image,
                    problem=args.problem,
                    resume_csv=args.resume_csv,
                )
                command += ["--allow-agent-endpoint", environment["ASSISTANT_V3_URL"]]
                before = _batches(repository)
                result = subprocess.run(command, cwd=repository, env=environment, check=False)
                created = _batches(repository) - before
                if len(created) != 1:
                    raise RepeatError("runner did not create exactly one new raw batch; inspect results")
                batches.append(created.pop())
                if result.returncode:
                    print(
                        f"Runner stopped; raw attempt retained at {batches[-1]}. "
                        "Use finalize for completed cases, then resume.",
                        file=sys.stderr,
                    )
        else:
            result = None
        count, missing = finalize(repository, output, batches, expected, environment)
        print(f"Packaged {count} valid case(s); {missing} missing. Open {output / 'by-case/README.md'}")
        return 1 if (result is not None and result.returncode) or missing else 0
    except (RepeatError, OSError, ValueError, KeyError) as error:
        print(f"Pilot wrapper: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
