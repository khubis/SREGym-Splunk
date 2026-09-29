"""Fail-closed contracts for the lightweight Lite repeat wrapper."""

from __future__ import annotations

import csv
import json
import runpy
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from sregym.results import assistant_v3_lite_repeat as repeat


def _environment() -> dict[str, str]:
    return {
        "SYNTHETIC_SF_TOKEN": "query-secret",
        "SYNTHETIC_SPLUNK_ACCESS_TOKEN": "ingest-secret",
        "SYNTHETIC_REALM": "rc0",
        "SYNTHETIC_ORG_ID": "synthetic-org",
        "SYNTHETIC_USER_ID": "synthetic-user",
        "SPLUNK_HOST": "logs.example.test",
        "SPLUNK_HEC_PORT": "8088",
        "SPLUNK_HEC_TOKEN": "hec-secret",
        "SPLUNK_LOGS_CONNECTION_ID": "connection123",
        "ASSISTANT_V3_URL": "http://127.0.0.1:8903",
        "ASSISTANT_V3_AUTH_TOKEN": "assistant-secret",
    }


def test_synthetic_mapping_requires_all_values_and_rejects_conflicts() -> None:
    mapped = repeat.resolve_environment(_environment(), profile="synthetic")
    assert mapped["SF_TOKEN"] == "query-secret"
    assert mapped["SPLUNK_O11Y_INGEST_TOKEN"] == "ingest-secret"
    assert mapped["ORG_ID"] == "synthetic-org"

    missing = _environment()
    del missing["SYNTHETIC_USER_ID"]
    with pytest.raises(repeat.RepeatError, match="SYNTHETIC_USER_ID"):
        repeat.resolve_environment(missing, profile="synthetic")

    conflict = _environment()
    conflict["SF_TOKEN"] = "different-secret"
    with pytest.raises(repeat.RepeatError, match="conflicting SF_TOKEN"):
        repeat.resolve_environment(conflict, profile="synthetic")


def test_canonical_profile_does_not_use_synthetic_fallback() -> None:
    with pytest.raises(repeat.RepeatError, match="missing required environment"):
        repeat.resolve_environment(_environment(), profile="canonical")


def test_canonical_profile_accepts_complete_names_without_aliases() -> None:
    source = _environment()
    for alias, canonical in repeat._ALIASES.items():
        source[canonical] = source.pop(alias)
    assert repeat.resolve_environment(source, profile="canonical")["ORG_ID"] == "synthetic-org"


def test_unknown_credential_profile_fails() -> None:
    with pytest.raises(repeat.RepeatError, match="credentials"):
        repeat.resolve_environment({}, profile="auto")


def test_headroom_gate_fails_closed() -> None:
    gib = 1024**3
    with pytest.raises(repeat.RepeatError, match="memory"):
        repeat.check_headroom(available_bytes=2 * gib, free_disk_bytes=20 * gib, docker_memory_bytes=10 * gib)
    with pytest.raises(repeat.RepeatError, match="disk"):
        repeat.check_headroom(available_bytes=7 * gib, free_disk_bytes=2 * gib, docker_memory_bytes=10 * gib)
    with pytest.raises(repeat.RepeatError, match="Docker"):
        repeat.check_headroom(available_bytes=7 * gib, free_disk_bytes=20 * gib, docker_memory_bytes=4 * gib)


def test_single_case_memory_override_keeps_a_four_gib_floor() -> None:
    gib = 1024**3
    repeat.check_headroom(
        available_bytes=4.2 * gib, free_disk_bytes=20 * gib, docker_memory_bytes=10 * gib,
        min_available_gib=4.0,
    )
    with pytest.raises(repeat.RepeatError, match="memory"):
        repeat.check_headroom(
            available_bytes=3.9 * gib, free_disk_bytes=20 * gib, docker_memory_bytes=10 * gib,
            min_available_gib=4.0,
        )
    with pytest.raises(repeat.RepeatError, match="memory floor"):
        repeat.check_headroom(
            available_bytes=7 * gib, free_disk_bytes=20 * gib, docker_memory_bytes=10 * gib,
            min_available_gib=3.0,
        )


def test_suite_command_is_sequential_pilot_configuration(tmp_path: Path) -> None:
    command = repeat.suite_command(
        repository=tmp_path,
        agent_image="sregym-agent-base:latest",
        problem=None,
        resume_csv=None,
    )
    assert command[command.index("--suite") + 1] == "sregym-lite"
    assert command[command.index("--profile") + 1] == "svelte"
    assert command[command.index("--assistant-prompt-arm") + 1] == "symptom_guided"
    assert "--force-build" not in command

    with pytest.raises(repeat.RepeatError, match="resume CSV"):
        repeat.suite_command(
            repository=tmp_path,
            agent_image="sregym-agent-base:latest",
            problem=None,
            resume_csv=tmp_path / "missing.csv",
        )
    resume = tmp_path / "resume.csv"
    resume.write_text("problem_id\n")
    resumed = repeat.suite_command(
        repository=tmp_path, agent_image="image:tag", problem="network_policy_block", resume_csv=resume
    )
    assert resumed[resumed.index("--problem") + 1] == "network_policy_block"
    assert resumed[resumed.index("--resume") + 1] == str(resume)
    with pytest.raises(repeat.RepeatError, match="not a Lite case"):
        repeat.suite_command(repository=tmp_path, agent_image="image:tag", problem="unknown", resume_csv=None)
    with pytest.raises(repeat.RepeatError, match="image tag"):
        repeat.suite_command(repository=tmp_path, agent_image=" ", problem=None, resume_csv=None)


def _run(batch: Path, case_id: str, *, valid: bool = True) -> Path:
    run = batch / "assistant_v3" / case_id / "run_1"
    (run / "observability").mkdir(parents=True)
    (run / "run_metadata.json").write_text(
        json.dumps(
            {
                "problem_id": case_id,
                "classification": "completed" if valid else "infrastructure_invalid",
                "observability_provider": "splunk",
                "logs_connection_id": "connection123",
                "run_id": "run123",
            }
        )
    )
    (run / "observability/delivery.json").write_text(json.dumps({"valid": valid}))
    return run


def _checks(status: str) -> dict[str, dict[str, object]]:
    return {
        signal: {"status": status, "count": 1 if status == "present" else None}
        for signal in ("metrics", "traces", "logs", "kubernetes_events", "pods", "events")
    }


def test_valid_run_selection_keeps_partial_progress_and_rejects_duplicates(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    good = _run(first, "case_a")
    _run(first, "case_b", valid=False)
    selected, missing = repeat.select_valid_runs([first], ["case_a", "case_b"])
    assert selected == {"case_a": good}
    assert missing == ["case_b"]

    _run(second, "case_a")
    with pytest.raises(repeat.RepeatError, match="multiple valid attempts"):
        repeat.select_valid_runs([first, second], ["case_a"])
    with pytest.raises(repeat.RepeatError, match="distinct"):
        repeat.select_valid_runs([first], ["case_a", "case_a"])
    assert repeat.select_valid_runs([tmp_path / "absent"], ["case_a"]) == ({}, ["case_a"])
    invalid = first / "assistant_v3/case_c/run_1"
    invalid.mkdir(parents=True)
    assert repeat.select_valid_runs([first], ["case_c"]) == ({}, ["case_c"])


def test_selection_report_binds_existing_attempts_without_secrets(tmp_path: Path) -> None:
    batch = tmp_path / "results" / "batch1"
    run = _run(batch, "case_a")
    with (run / "case_a_results.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Diagnosis.accuracy"])
        writer.writeheader()
        writer.writerow({"Diagnosis.accuracy": "78.0"})
    scorecard = batch / "assistant_v3_campaign/scorecard.md"
    scorecard.parent.mkdir()
    scorecard.write_text("# scorecard\n")
    report = tmp_path / "results/reproductions/pilot/selection.md"

    repeat.write_selection_report(report, {"case_a": run}, ["case_b"])

    text = report.read_text()
    assert "[case_a]" in text and "78" in text and "case_b" in text
    assert "secret" not in text


def test_selection_report_refuses_missing_or_ambiguous_judge(tmp_path: Path) -> None:
    batch = tmp_path / "results/batch1"
    run = _run(batch, "case_a")
    report = tmp_path / "results/reproductions/pilot/selection.md"
    (run / "case_a_results.csv").write_text("Diagnosis.accuracy\n78\n79\n")
    with pytest.raises(repeat.RepeatError, match="one judge row"):
        repeat.write_selection_report(report, {"case_a": run}, [])
    (run / "case_a_results.csv").write_text("Diagnosis.accuracy\n78\n")
    with pytest.raises(repeat.RepeatError, match="raw scorecard"):
        repeat.write_selection_report(report, {"case_a": run}, [])


def test_postrun_proof_rejects_wrong_connection_or_attempt(tmp_path: Path) -> None:
    run = _run(tmp_path / "results/batch", "case_a")
    with pytest.raises(repeat.RepeatError, match="attempt Logs connection"):
        repeat._needs_postrun_query(run, "another")
    assert repeat._needs_postrun_query(run, "connection123") is True
    proof = {"logs_connection_id": "another", "case_id": "case_a", "run_id": "run123", "checks": _checks("present")}
    (run / "splunk_lite_delivery.json").write_text(json.dumps(proof))
    with pytest.raises(repeat.RepeatError, match="proof targets another"):
        repeat._needs_postrun_query(run, "connection123")
    proof["logs_connection_id"] = "connection123"
    proof["run_id"] = "older-run"
    (run / "splunk_lite_delivery.json").write_text(json.dumps(proof))
    with pytest.raises(repeat.RepeatError, match="another attempt"):
        repeat._needs_postrun_query(run, "connection123")
    proof["run_id"] = "run123"
    proof["checks"].pop("events")
    (run / "splunk_lite_delivery.json").write_text(json.dumps(proof))
    assert repeat._needs_postrun_query(run, "connection123") is True


def test_finalize_reuses_saved_signal_proof_and_checkpoints_before_dossier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = tmp_path / "results/batch1"
    run = _run(batch, "case_a")
    (run / "splunk_lite_delivery.json").write_text(
        json.dumps(
            {
                "logs_connection_id": "connection123",
                "case_id": "case_a",
                "run_id": "run123",
                "checks": _checks("present"),
            }
        )
    )
    (run / "case_a_results.csv").write_text("Diagnosis.accuracy\n78\n")
    scorecard = batch / "assistant_v3_campaign/scorecard.md"
    scorecard.parent.mkdir()
    scorecard.write_text("# scorecard\n")
    calls: list[str] = []
    monkeypatch.setattr(repeat, "checkpoint_attempt", lambda *_: calls.append("checkpoint"))
    monkeypatch.setattr(repeat, "build_dossiers", lambda *_: calls.append("dossier"))

    count, missing = repeat.finalize(
        tmp_path, tmp_path / "results/reproductions/pilot", [batch], ["case_a", "case_b"], _environment()
    )

    assert (count, missing) == (1, 1)
    assert calls == ["checkpoint", "dossier"]


def test_finalize_retries_saved_query_error_before_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sregym.observability import splunk
    from sregym.results import splunk_lite_evidence

    batch = tmp_path / "results/batch1"
    run = _run(batch, "case_a")
    (run / "splunk_lite_delivery.json").write_text(
        json.dumps(
            {
                "logs_connection_id": "connection123",
                "case_id": "case_a",
                "run_id": "run123",
                "checks": _checks("query_error"),
            }
        )
    )
    (run / "case_a_results.csv").write_text("Diagnosis.accuracy\n78\n")
    scorecard = batch / "assistant_v3_campaign/scorecard.md"
    scorecard.parent.mkdir()
    scorecard.write_text("# scorecard\n")

    class FakeBackend:
        def __init__(self, _configuration: object) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(splunk.SplunkConfig, "from_env", lambda *_: object())
    monkeypatch.setattr(splunk, "SplunkHttpBackend", FakeBackend)
    calls: list[str] = []

    def retry(run_dir: Path, **_kwargs: object) -> Path:
        calls.append("retried")
        path = run_dir / "splunk_lite_delivery.json"
        path.write_text(
            json.dumps(
                {
                    "logs_connection_id": "connection123",
                    "case_id": "case_a",
                    "run_id": "run123",
                    "checks": _checks("present"),
                }
            )
        )
        return path

    monkeypatch.setattr(splunk_lite_evidence, "verify_case_delivery", retry)
    monkeypatch.setattr(repeat, "checkpoint_attempt", lambda *_: calls.append("checkpoint"))
    monkeypatch.setattr(repeat, "build_dossiers", lambda *_: calls.append("dossier"))
    assert repeat.finalize(tmp_path, tmp_path / "results/reproductions/pilot", [batch], ["case_a"], _environment()) == (
        1,
        0,
    )
    assert calls == ["retried", "checkpoint", "dossier"]


def test_finalize_needs_at_least_one_valid_case(tmp_path: Path) -> None:
    with pytest.raises(repeat.RepeatError, match="raw batch"):
        repeat.finalize(tmp_path, tmp_path / "results/out", [], ["case_a"], _environment())
    with pytest.raises(repeat.RepeatError, match="no completed valid"):
        repeat.finalize(tmp_path, tmp_path / "results/out", [tmp_path / "batch"], ["case_a"], _environment())
    assert repeat._batches(tmp_path / "absent") == set()


def test_finalize_refuses_persistent_postrun_query_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sregym.observability import splunk
    from sregym.results import splunk_lite_evidence

    batch = tmp_path / "results/batch1"
    run = _run(batch, "case_a")
    proof = {
        "logs_connection_id": "connection123",
        "case_id": "case_a",
        "run_id": "run123",
        "checks": _checks("query_error"),
    }
    (run / "splunk_lite_delivery.json").write_text(json.dumps(proof))

    class FakeBackend:
        def __init__(self, _configuration: object) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(splunk.SplunkConfig, "from_env", lambda *_: object())
    monkeypatch.setattr(splunk, "SplunkHttpBackend", FakeBackend)
    monkeypatch.setattr(
        splunk_lite_evidence, "verify_case_delivery", lambda *_args, **_kwargs: run / "splunk_lite_delivery.json"
    )
    monkeypatch.setattr(repeat, "checkpoint_attempt", lambda *_: pytest.fail("checkpointed failed query"))
    with pytest.raises(repeat.RepeatError, match="post-run query failed"):
        repeat.finalize(tmp_path, tmp_path / "results/reproductions/pilot", [batch], ["case_a"], _environment())


@pytest.mark.filterwarnings("ignore:.*found in sys.modules.*:RuntimeWarning")
def test_module_entry_point_is_callable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["assistant_v3_lite_repeat", "--help"])
    with pytest.raises(SystemExit) as exit_status:
        runpy.run_module("sregym.results.assistant_v3_lite_repeat", run_name="__main__")
    assert exit_status.value.code == 0


def test_run_cli_launches_only_one_case_and_passes_mapped_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = "cronjob_sidecar_blocks_completion_hotel_reservation"
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(repeat.os, "environ", _environment())
    monkeypatch.setattr(repeat, "_host_preflight", lambda *_: None)
    launched: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        launched.append(command)
        assert kwargs["env"]["SF_TOKEN"] == "query-secret"
        assert kwargs["env"]["ORG_ID"] == "synthetic-org"
        (tmp_path / "results/0928_2359").mkdir()
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(repeat.subprocess, "run", fake_run)
    packaged: list[tuple[Path, ...]] = []

    def fake_finalize(
        repo: Path, out: Path, batches: list[Path], expected: list[str], env: dict[str, str]
    ) -> tuple[int, int]:
        packaged.append(tuple(batches))
        assert expected == [case]
        assert env["SF_TOKEN"] == "query-secret"
        return 1, 0

    monkeypatch.setattr(repeat, "finalize", fake_finalize)
    exit_code = repeat.main(
        [
            "run",
            "--credentials",
            "synthetic",
            "--repository",
            str(tmp_path),
            "--output",
            str(tmp_path / "results/reproductions/pilot"),
            "--problem",
            case,
        ]
    )
    assert exit_code == 0
    assert len(launched) == 1
    assert launched[0][launched[0].index("--problem") + 1] == case
    assert launched[0][launched[0].index("--n-attempts") + 1] == "1"
    assert packaged == [(tmp_path / "results/0928_2359",)]


def test_run_cli_rejects_low_headroom_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(repeat.os, "environ", _environment())
    monkeypatch.setattr(
        repeat,
        "_host_preflight",
        lambda *_: (_ for _ in ()).throw(repeat.RepeatError("less than 6 GiB available host memory")),
    )
    monkeypatch.setattr(repeat.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("launched"))
    exit_code = repeat.main(
        [
            "run",
            "--credentials",
            "synthetic",
            "--repository",
            str(tmp_path),
            "--output",
            str(tmp_path / "results/reproductions/pilot"),
        ]
    )
    assert exit_code == 2


def test_run_cli_rejects_reduced_memory_gate_for_full_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(repeat.os, "environ", _environment())
    monkeypatch.setattr(repeat, "_host_preflight", lambda *_: pytest.fail("started preflight"))
    assert repeat.main([
        "run", "--credentials", "synthetic", "--repository", str(tmp_path),
        "--output", str(tmp_path / "results/reproductions/pilot"),
        "--min-available-gib", "4",
    ]) == 2


def test_host_preflight_uses_docker_limit_and_rejects_missing_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Memory:
        available = 7 * 1024**3

    class Disk:
        free = 20 * 1024**3

    monkeypatch.setattr(repeat.psutil, "virtual_memory", lambda: Memory())
    monkeypatch.setattr(repeat.shutil, "disk_usage", lambda *_: Disk())
    calls: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command[1])
        return subprocess.CompletedProcess(command, 0, stdout=str(10 * 1024**3))

    monkeypatch.setattr(repeat.subprocess, "run", fake_run)
    repeat._host_preflight(tmp_path, "image:tag")
    assert calls == ["info", "image"]

    def missing_image(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1] == "image":
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, stdout=str(10 * 1024**3))

    monkeypatch.setattr(repeat.subprocess, "run", missing_image)
    with pytest.raises(repeat.RepeatError, match="prebuilt agent image"):
        repeat._host_preflight(tmp_path, "missing:tag")


def test_finalize_cli_does_not_start_docker_and_rejects_external_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = tmp_path / "results/0928_1234"
    batch.mkdir(parents=True)
    monkeypatch.setattr(repeat.os, "environ", _environment())
    monkeypatch.setattr(repeat, "_host_preflight", lambda *_: pytest.fail("Docker started"))
    monkeypatch.setattr(repeat, "finalize", lambda *_: (1, 0))
    args = [
        "finalize",
        "--credentials",
        "synthetic",
        "--repository",
        str(tmp_path),
        "--batch",
        str(batch),
        "--output",
        str(tmp_path / "results/reproductions/pilot"),
    ]
    assert repeat.main(args) == 0
    args[-1] = str(tmp_path / "external")
    assert repeat.main(args) == 2


@pytest.mark.parametrize(
    "mode,expected", [("no_batch", 2), ("runner_failed", 1), ("minute_collision", 2), ("locked", 2)]
)
def test_run_cli_failure_paths_preserve_result_and_do_not_claim_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected: int,
) -> None:
    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(repeat.os, "environ", _environment())
    monkeypatch.setattr(repeat, "_host_preflight", lambda *_: None)
    monkeypatch.setattr(repeat, "finalize", lambda *_: (1, 0))
    if mode == "minute_collision":
        (results / datetime.now().strftime("%m%d_%H%M")).mkdir()
    if mode == "locked":
        monkeypatch.setattr(repeat.fcntl, "flock", lambda *_: (_ for _ in ()).throw(BlockingIOError()))

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if mode not in {"no_batch", "minute_collision", "locked"}:
            (results / "0928_2359").mkdir()
        return subprocess.CompletedProcess(command, 1 if mode == "runner_failed" else 0)

    monkeypatch.setattr(repeat.subprocess, "run", fake_run)
    assert (
        repeat.main(
            [
                "run",
                "--credentials",
                "synthetic",
                "--repository",
                str(tmp_path),
                "--output",
                str(results / "reproductions/pilot"),
            ]
        )
        == expected
    )
