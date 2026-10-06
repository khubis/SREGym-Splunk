"""Fail-closed contracts for the lightweight Lite repeat wrapper."""

from __future__ import annotations

import csv
import json
import re
import runpy
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from sregym.results import assistant_v3_lite_repeat as repeat


def test_splunk_lite_env_example_matches_wrapper_requirements_without_secrets() -> None:
    repository = Path(__file__).resolve().parents[2]
    template = (repository / ".env.splunk-lite.example").read_text(encoding="utf-8")
    names = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", template, re.MULTILINE))
    required = set(repeat._ALIASES) | (set(repeat._REQUIRED) - set(repeat._ALIASES.values()))

    assert required <= names.keys()
    assert not set(repeat._ALIASES.values()) & names.keys()
    assert all(
        value.strip() == '""'
        for name, value in names.items()
        if name.endswith(("TOKEN", "KEY")) and name != "JUDGE_API_KEY"
    )
    assert names["JUDGE_API_KEY"].strip() == '"unused-placeholder"'
    assert names["SREGYM_LITE_JUDGE_MODEL"].strip() == '"openai/gpt-5.6-luna"'
    assert names["SREGYM_LLM_GATEWAY_SERVICE_NAME"].strip() == '"sregym"'
    assert names["JUDGE_API_BASE"].strip() == '"https://llm-gateway.lab0.signalfx.com/openai/v1"'
    assert "SREGYM_LLM_GATEWAY_ORG_ID" not in names
    assert "./.env.splunk-lite.example" in (repository / "README.md").read_text(encoding="utf-8")
    assert "../.env.splunk-lite.example" in (repository / "docs/assistant-v3-lite-repeat.md").read_text(
        encoding="utf-8"
    )
    assert "--env-file .env.splunk-lite" in (repository / "README.md").read_text(encoding="utf-8")
    assert "--env-file .env.splunk-lite" in (repository / "docs/assistant-v3-lite-repeat.md").read_text(
        encoding="utf-8"
    )
    ignored = subprocess.run(["git", "check-ignore", "-q", ".env.splunk-lite"], cwd=repository, check=False)
    assert ignored.returncode == 0


def test_assistant_env_example_separates_gateway_embeddings_and_eval_memory() -> None:
    repository = Path(__file__).resolve().parents[2]
    template = (repository / ".env.assistant-v3-splunk.example").read_text(encoding="utf-8")
    names = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", template, re.MULTILINE))

    for key in ("SF_TOKEN", "SFX_REALM", "ORG_ID", "USER_ID"):
        assert names[key] == '""'
    assert names["USE_LLM_GATEWAY_SERVICE"] == "true"
    assert names["ASSISTANT_V3_USE_LLM_GATEWAY_SERVICE"] == "true"
    assert "ASSISTANT_V3_MEMORY_USE_LLM_GATEWAY_SERVICE" not in names
    assert "ASSISTANT_V3_MEMORY_EMBEDDING_MODEL" not in names
    assert "ASSISTANT_V3_MEMORY_EMBEDDING_DIM" not in names
    assert names["AIMEMORY_DB_NAME"] == "sregym_gateway_small_memory"
    assert names["LLM_GATEWAY_SERVICE_URL"] == names["ASSISTANT_V3_LLM_GATEWAY_SERVICE_URL"]
    assert names["USE_OPENAI_PROXY"] == "false"
    assert names["DEEPEVAL_FILE_SYSTEM"] == "READ_ONLY"
    assert names["DEEPEVAL_CACHE_FOLDER"] == ".local/deepeval"
    assert "LLM_PROXY_SF_TOKEN" not in names
    assert "OPENAI_API_KEY" not in names
    assert names["MEMORY_PG_DATABASE"] == names["AIMEMORY_DB_NAME"]
    assert names["ASSISTANT_V3_O11Y_POSTGRES_DATABASE"] == names["TOOL_DB_NAME"]
    databases = {names[key] for key in ("POSTGRES_DATABASE", "AIMEMORY_DB_NAME", "TOOL_DB_NAME")}
    assert len(databases) == 3
    assert all(database.startswith("sregym_") for database in databases)


def test_assistant_setup_uses_main_not_obsolete_embedding_branch() -> None:
    repository = Path(__file__).resolve().parents[2]
    for relative in ("README.md", "docs/assistant-v3-lite-repeat.md", ".env.assistant-v3-splunk.example"):
        text = (repository / relative).read_text(encoding="utf-8")
        assert "3836" not in text
        assert "codex/assistant-v3-gateway-embeddings" not in text
        assert "ASSISTANT_V3_MEMORY_USE_LLM_GATEWAY_SERVICE" not in text
    runbook = (repository / "docs/assistant-v3-lite-repeat.md").read_text(encoding="utf-8")
    assert "git switch main" in runbook
    assert "git pull --ff-only origin main" in runbook


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
        "JUDGE_API_KEY": "judge-secret",
        "JUDGE_API_BASE": "https://judge.example.test",
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


def test_helm_environment_is_local_to_checkout_and_preserves_explicit_overrides(tmp_path: Path) -> None:
    environment = _environment()
    repeat.prepare_helm_environment(environment, tmp_path)

    assert environment["HELM_REPOSITORY_CONFIG"] == str(tmp_path / ".local/helm/repositories.yaml")
    assert environment["HELM_REPOSITORY_CACHE"] == str(tmp_path / ".local/helm/repository")
    assert Path(environment["HELM_REPOSITORY_CACHE"]).is_dir()
    assert not Path(environment["HELM_REPOSITORY_CONFIG"]).exists()

    explicit = dict(environment, HELM_REPOSITORY_CONFIG=str(tmp_path / "custom/repos.yaml"))
    repeat.prepare_helm_environment(explicit, tmp_path)
    assert explicit["HELM_REPOSITORY_CONFIG"] == str(tmp_path / "custom/repos.yaml")
    assert (tmp_path / "custom").is_dir()


def test_canonical_profile_accepts_complete_names_without_aliases() -> None:
    source = _environment()
    for alias, canonical in repeat._ALIASES.items():
        source[canonical] = source.pop(alias)
    assert repeat.resolve_environment(source, profile="canonical")["ORG_ID"] == "synthetic-org"


def test_unknown_credential_profile_fails() -> None:
    with pytest.raises(repeat.RepeatError, match="credentials"):
        repeat.resolve_environment({}, profile="auto")


def test_judge_credentials_are_required_before_launch() -> None:
    source = _environment()
    del source["JUDGE_API_KEY"]
    with pytest.raises(repeat.RepeatError, match="JUDGE_API_KEY"):
        repeat.resolve_environment(source, profile="synthetic")


def test_gateway_judge_uses_target_org_without_repeating_org_id() -> None:
    source = _environment()
    source["SREGYM_LITE_JUDGE_MODEL"] = "openai/gpt-5.6-luna"
    source["SREGYM_LLM_GATEWAY_SERVICE_NAME"] = "sregym"
    mapped = repeat.resolve_environment(source, profile="synthetic")

    assert mapped["SREGYM_LLM_GATEWAY_ORG_ID"] == "synthetic-org"
    assert mapped["SREGYM_LITE_JUDGE_MODEL"] == "openai/gpt-5.6-luna"
    assert repeat._campaign_fingerprint(["network_policy_block"], mapped, "synthetic", "image:tag") != (
        repeat._campaign_fingerprint(
            ["network_policy_block"],
            repeat.resolve_environment(_environment(), profile="synthetic"),
            "synthetic",
            "image:tag",
        )
    )

    changed_endpoint = dict(mapped, JUDGE_API_BASE="https://other-gateway.example.test/openai/v1")
    assert repeat._campaign_fingerprint(["network_policy_block"], mapped, "synthetic", "image:tag") != (
        repeat._campaign_fingerprint(["network_policy_block"], changed_endpoint, "synthetic", "image:tag")
    )


def test_gateway_judge_rejects_mismatched_org_or_unsupported_model() -> None:
    source = _environment()
    source.update(
        SREGYM_LITE_JUDGE_MODEL="openai/gpt-5.6-luna",
        SREGYM_LLM_GATEWAY_SERVICE_NAME="sregym",
        SREGYM_LLM_GATEWAY_ORG_ID="different-org",
    )
    with pytest.raises(repeat.RepeatError, match="gateway org"):
        repeat.resolve_environment(source, profile="synthetic")

    source["SREGYM_LLM_GATEWAY_ORG_ID"] = "synthetic-org"
    source["SREGYM_LITE_JUDGE_MODEL"] = "openai/other-model"
    with pytest.raises(repeat.RepeatError, match="SREGYM_LITE_JUDGE_MODEL"):
        repeat.resolve_environment(source, profile="synthetic")


def test_low_host_memory_warns_without_blocking(capsys: pytest.CaptureFixture[str]) -> None:
    gib = 1024**3
    repeat.check_headroom(available_bytes=2 * gib, free_disk_bytes=20 * gib, docker_memory_bytes=10 * gib)
    warning = capsys.readouterr().err
    assert "Warning" in warning
    assert "2" in warning and "6" in warning
    assert "continuing" in warning


def test_disk_and_docker_checks_still_fail_closed() -> None:
    gib = 1024**3
    with pytest.raises(repeat.RepeatError, match="disk"):
        repeat.check_headroom(available_bytes=7 * gib, free_disk_bytes=2 * gib, docker_memory_bytes=10 * gib)
    with pytest.raises(repeat.RepeatError, match="Docker"):
        repeat.check_headroom(available_bytes=7 * gib, free_disk_bytes=20 * gib, docker_memory_bytes=4 * gib)


def test_resource_wait_retries_only_transient_memory_pressure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    attempts = 0
    sleeps: list[float] = []

    def preflight(*_args: object) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise repeat.RepeatError("less than 4 GiB available host memory")

    monkeypatch.setattr(repeat, "_host_preflight", preflight)
    monkeypatch.setattr(repeat.time, "sleep", sleeps.append)
    repeat.wait_for_host_preflight(tmp_path, "image:tag", 4.0, retries=3, interval_seconds=30)
    assert attempts == 3
    assert sleeps == [30, 30]
    monkeypatch.setattr(
        repeat, "_host_preflight", lambda *_: (_ for _ in ()).throw(repeat.RepeatError("less than 10 GiB free disk"))
    )
    with pytest.raises(repeat.RepeatError, match="disk"):
        repeat.wait_for_host_preflight(tmp_path, "image:tag", 4.0, retries=3, interval_seconds=30)
    assert sleeps == [30, 30]
    monkeypatch.setattr(
        repeat,
        "_host_preflight",
        lambda *_: (_ for _ in ()).throw(repeat.RepeatError("less than 4 GiB available host memory")),
    )
    with pytest.raises(repeat.RepeatError, match="memory"):
        repeat.wait_for_host_preflight(tmp_path, "image:tag", 4.0, retries=2, interval_seconds=30)
    assert sleeps == [30, 30, 30]
    with pytest.raises(repeat.RepeatError, match="settings"):
        repeat.wait_for_host_preflight(tmp_path, "image:tag", 4.0, retries=0)
    with pytest.raises(repeat.RepeatError, match="memory"):
        repeat.wait_for_host_preflight(tmp_path, "image:tag", 4.0, retries=1)


def test_memory_override_changes_warning_threshold(capsys: pytest.CaptureFixture[str]) -> None:
    gib = 1024**3
    repeat.check_headroom(
        available_bytes=4.2 * gib,
        free_disk_bytes=20 * gib,
        docker_memory_bytes=10 * gib,
        min_available_gib=4.0,
    )
    assert capsys.readouterr().err == ""
    repeat.check_headroom(
        available_bytes=3.9 * gib,
        free_disk_bytes=20 * gib,
        docker_memory_bytes=10 * gib,
        min_available_gib=4.0,
    )
    assert "Warning" in capsys.readouterr().err
    with pytest.raises(repeat.RepeatError, match="memory floor"):
        repeat.check_headroom(
            available_bytes=7 * gib,
            free_disk_bytes=20 * gib,
            docker_memory_bytes=10 * gib,
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
    assert command[command.index("--agent-timeout") + 1] == "1200"
    gateway_command = repeat.suite_command(
        repository=tmp_path,
        agent_image="sregym-agent-base:latest",
        problem="network_policy_block",
        resume_csv=None,
        judge_model="openai/gpt-5.6-luna",
    )
    assert gateway_command[gateway_command.index("--judge-model") + 1] == "openai/gpt-5.6-luna"

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
                "benchmark_profile": "svelte",
                "requested_model": "gpt-5.6-luna",
                "requested_reasoning": "medium",
                "judge_model": "azure/gpt-5.6-luna",
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


def test_campaign_record_rejects_target_drift_and_unowned_batches(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    output = results / "reproductions/pilot"
    expected = ["case_a", "case_b"]
    environment = repeat.resolve_environment(_environment(), profile="synthetic")
    state = repeat.load_campaign(output, tmp_path, expected, environment, "synthetic", "image:tag")
    assert state["batches"] == []
    repeat.save_campaign(output, state)
    assert repeat.load_campaign(output, tmp_path, expected, environment, "synthetic", "image:tag") == state
    changed = dict(environment, ORG_ID="other-org")
    with pytest.raises(repeat.RepeatError, match="different target"):
        repeat.load_campaign(output, tmp_path, expected, changed, "synthetic", "image:tag")
    with pytest.raises(repeat.RepeatError, match="different target"):
        repeat.load_campaign(output, tmp_path, expected, environment, "synthetic", "other:tag")
    state["batches"] = ["../outside"]
    repeat.save_campaign(output, state)
    with pytest.raises(repeat.RepeatError, match="batch path"):
        repeat.load_campaign(output, tmp_path, expected, environment, "synthetic", "image:tag")
    state["batches"] = []
    state["pending"] = {"case_id": "unknown", "before": []}
    repeat.save_campaign(output, state)
    with pytest.raises(repeat.RepeatError, match="pending work"):
        repeat.load_campaign(output, tmp_path, expected, environment, "synthetic", "image:tag")
    (output / "campaign.json").write_text("{broken")
    with pytest.raises(repeat.RepeatError, match="unreadable"):
        repeat.load_campaign(output, tmp_path, expected, environment, "synthetic", "image:tag")
    (output / "campaign.json").unlink()
    (output / "summary.md").write_text("prior output")
    with pytest.raises(repeat.RepeatError, match="without a campaign record"):
        repeat.load_campaign(output, tmp_path, expected, environment, "synthetic", "image:tag")


def test_recover_pending_batch_after_interruption(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    output = results / "reproductions/pilot"
    environment = repeat.resolve_environment(_environment(), profile="synthetic")
    state = repeat.load_campaign(output, tmp_path, ["case_a"], environment, "synthetic", "image:tag")
    state["pending"] = {"case_id": "case_a", "before": []}
    repeat.save_campaign(output, state)
    (results / "0930_1200").mkdir()
    recovered = repeat.recover_pending(output, tmp_path, state)
    assert recovered["batches"] == ["0930_1200"]
    assert recovered["pending"] is None
    assert repeat.recover_pending(output, tmp_path, recovered) == recovered
    recovered["pending"] = {"case_id": "case_a", "before": []}
    (results / "0930_1201").mkdir()
    with pytest.raises(repeat.RepeatError, match="multiple raw batches"):
        repeat.recover_pending(output, tmp_path, recovered)


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


def test_campaign_summary_shows_valid_invalid_and_missing_without_invented_scores(tmp_path: Path) -> None:
    output = tmp_path / "results/reproductions/pilot"
    valid_batch, invalid_batch = tmp_path / "results/batch1", tmp_path / "results/batch2"
    valid = _run(valid_batch, "case_a")
    _run(invalid_batch, "case_b", valid=False)
    dossier = output / "by-case/case_a"
    dossier.mkdir(parents=True)
    (dossier / "manifest.json").write_text(json.dumps({"case_id": "case_a", "benchmark_score": 78.0}))
    (dossier / "pre_agent_proof.json").write_text(
        json.dumps({"causal": {"status": "confirmed", "visibility": "fully_splunk_observable"}})
    )
    for name in repeat._SUMMARY_LINKS:
        (dossier / name).touch()

    repeat.write_campaign_summary(
        output / "summary.md",
        {"case_a": valid},
        ["case_a", "case_b", "case_c"],
        [valid_batch, invalid_batch],
    )

    summary = (output / "summary.md").read_text()
    assert "78/100 across 1 valid attempt" in summary
    assert "| case_a | valid | 78 | confirmed |" in summary
    assert "by-case/case_a/final_answer.md" in summary
    assert "by-case/case_a/benchmark_ground_truth.md" in summary
    assert "by-case/case_a/judge_raw.csv" in summary
    assert "by-case/case_a/atif_trace.json" in summary
    assert "| case_b | infrastructure_invalid | — | — |" in summary
    assert "| case_c | missing | — | — |" in summary
    assert "query-secret" not in summary


def test_campaign_summary_rejects_incomplete_valid_dossier(tmp_path: Path) -> None:
    run = _run(tmp_path / "results/batch1", "case_a")
    with pytest.raises(repeat.RepeatError, match="dossier"):
        repeat.write_campaign_summary(
            tmp_path / "results/out/summary.md", {"case_a": run}, ["case_a"], [tmp_path / "results/batch1"]
        )


def test_campaign_summary_rejects_wrong_case_and_bad_score(tmp_path: Path) -> None:
    batch = tmp_path / "results/batch1"
    run = _run(batch, "case_a")
    dossier = tmp_path / "results/out/by-case/case_a"
    dossier.mkdir(parents=True)
    for name in (*repeat._SUMMARY_LINKS, "pre_agent_proof.json"):
        (dossier / name).touch()
    (dossier / "pre_agent_proof.json").write_text(json.dumps({"causal": {"status": "confirmed"}}))
    manifest = dossier / "manifest.json"
    manifest.write_text(json.dumps({"case_id": "other", "benchmark_score": 50}))
    with pytest.raises(repeat.RepeatError, match="identity mismatch"):
        repeat.write_campaign_summary(tmp_path / "results/out/summary.md", {"case_a": run}, ["case_a"], [batch])
    manifest.write_text(json.dumps({"case_id": "case_a", "benchmark_score": 101}))
    with pytest.raises(repeat.RepeatError, match="invalid benchmark score"):
        repeat.write_campaign_summary(tmp_path / "results/out/summary.md", {"case_a": run}, ["case_a"], [batch])


def test_campaign_summary_classifies_failed_delivery_and_missing_metadata(tmp_path: Path) -> None:
    batch = tmp_path / "results/batch1"
    completed = _run(batch, "case_a")
    (completed / "observability/delivery.json").write_text(json.dumps({"valid": False}))
    missing_metadata = _run(batch, "case_b", valid=False)
    (missing_metadata / "run_metadata.json").unlink()
    report = tmp_path / "results/out/summary.md"
    repeat.write_campaign_summary(report, {}, ["case_a", "case_b"], [batch])
    summary = report.read_text()
    assert "| case_a | delivery_invalid | — | — |" in summary
    assert "| case_b | incomplete | — | — | — |" in summary


def test_campaign_summary_preserves_corrupt_invalid_attempt_as_incomplete(tmp_path: Path) -> None:
    batch = tmp_path / "results/batch1"
    run = _run(batch, "case_a", valid=False)
    (run / "run_metadata.json").write_text("{truncated")
    report = tmp_path / "results/out/summary.md"
    repeat.write_campaign_summary(report, {}, ["case_a"], [batch])
    assert "| case_a | incomplete | — | — |" in report.read_text()


def test_finalize_without_valid_attempt_still_reports_failure(tmp_path: Path) -> None:
    batch = tmp_path / "results/batch1"
    _run(batch, "case_a", valid=False)
    output = tmp_path / "results/reproductions/pilot"
    assert repeat.finalize(tmp_path, output, [batch], ["case_a", "case_b"], _environment()) == (0, 2)
    report = (output / "summary.md").read_text()
    assert "0 valid attempts" in report
    assert "infrastructure_invalid" in report
    assert "missing" in report


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
    monkeypatch.setattr(repeat, "write_campaign_summary", lambda *_: calls.append("summary"))

    count, missing = repeat.finalize(
        tmp_path, tmp_path / "results/reproductions/pilot", [batch], ["case_a", "case_b"], _environment()
    )

    assert (count, missing) == (1, 1)
    assert calls == ["checkpoint", "dossier", "summary"]


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
    monkeypatch.setattr(repeat, "write_campaign_summary", lambda *_: calls.append("summary"))
    assert repeat.finalize(tmp_path, tmp_path / "results/reproductions/pilot", [batch], ["case_a"], _environment()) == (
        1,
        0,
    )
    assert calls == ["retried", "checkpoint", "dossier", "summary"]


def test_finalize_needs_at_least_one_valid_case(tmp_path: Path) -> None:
    with pytest.raises(repeat.RepeatError, match="raw batch"):
        repeat.finalize(tmp_path, tmp_path / "results/out", [], ["case_a"], _environment())
    assert repeat.finalize(tmp_path, tmp_path / "results/out", [tmp_path / "batch"], ["case_a"], _environment()) == (
        0,
        1,
    )
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
    monkeypatch.setattr(repeat, "_prepared_runtime_preflight", lambda *_: None)
    launched: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        launched.append(command)
        assert kwargs["env"]["SF_TOKEN"] == "query-secret"
        assert kwargs["env"]["ORG_ID"] == "synthetic-org"
        (tmp_path / "results/0928_2359").mkdir()
        _run(tmp_path / "results/0928_2359", case)
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


def test_full_campaign_resumes_after_saved_case_without_relaunching_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = list(repeat.SREGYM_LITE_PROBLEMS)[:3]
    (tmp_path / "results").mkdir()
    output = tmp_path / "results/reproductions/pilot"
    monkeypatch.setattr(repeat.os, "environ", _environment())
    monkeypatch.setattr(repeat, "SREGYM_LITE_PROBLEMS", cases)
    monkeypatch.setattr(repeat, "_host_preflight", lambda *_: None)
    monkeypatch.setattr(repeat, "_prepared_runtime_preflight", lambda *_: None)
    monkeypatch.setattr(repeat, "finalize", lambda *_: (0, 0))
    launches: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        case = command[command.index("--problem") + 1]
        launches.append(case)
        batch = tmp_path / "results" / f"0930_12{len(launches):02d}"
        batch.mkdir()
        _run(batch, case, valid=len(launches) != 2)
        return subprocess.CompletedProcess(command, 0 if len(launches) != 2 else 1)

    monkeypatch.setattr(repeat.subprocess, "run", fake_run)
    args = ["run", "--credentials", "synthetic", "--repository", str(tmp_path), "--output", str(output)]
    assert repeat.main(args) == 1
    assert launches == cases[:2]
    assert (output / "campaign.json").is_file()
    assert repeat.main(args) == 0
    assert launches == [cases[0], cases[1], cases[1], cases[2]]
    assert len(json.loads((output / "campaign.json").read_text())["batches"]) == 4


def test_abrupt_wrapper_interruption_recovers_one_pending_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = list(repeat.SREGYM_LITE_PROBLEMS)[:2]
    (tmp_path / "results").mkdir()
    output = tmp_path / "results/reproductions/pilot"
    monkeypatch.setattr(repeat.os, "environ", _environment())
    monkeypatch.setattr(repeat, "SREGYM_LITE_PROBLEMS", cases)
    monkeypatch.setattr(repeat, "_host_preflight", lambda *_: None)
    monkeypatch.setattr(repeat, "_prepared_runtime_preflight", lambda *_: None)
    monkeypatch.setattr(repeat, "finalize", lambda *_: (0, 0))
    launched: list[str] = []

    def interrupted(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        case = command[command.index("--problem") + 1]
        launched.append(case)
        batch = tmp_path / "results" / f"0930_12{len(launched):02d}"
        batch.mkdir()
        _run(batch, case)
        if len(launched) == 2:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(repeat.subprocess, "run", interrupted)
    args = ["run", "--credentials", "synthetic", "--repository", str(tmp_path), "--output", str(output)]
    with pytest.raises(KeyboardInterrupt):
        repeat.main(args)
    assert json.loads((output / "campaign.json").read_text())["pending"]["case_id"] == cases[1]
    assert repeat.main(args) == 0
    assert launched == cases
    assert json.loads((output / "campaign.json").read_text())["pending"] is None


def test_selected_attempt_identity_rejects_wrong_model_or_connection(tmp_path: Path) -> None:
    run = _run(tmp_path / "batch", "case_a")
    metadata_path = run / "run_metadata.json"
    metadata = json.loads(metadata_path.read_text())
    metadata.update(
        benchmark_profile="svelte",
        requested_model="gpt-5.6-luna",
        requested_reasoning="medium",
        judge_model="azure/gpt-5.6-luna",
    )
    metadata_path.write_text(json.dumps(metadata))
    repeat.validate_selected_identity({"case_a": run}, repeat.resolve_environment(_environment(), profile="synthetic"))
    metadata["requested_model"] = "other"
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(repeat.RepeatError, match="model"):
        repeat.validate_selected_identity(
            {"case_a": run}, repeat.resolve_environment(_environment(), profile="synthetic")
        )
    metadata_path.write_text("{broken")
    with pytest.raises(repeat.RepeatError, match="unreadable"):
        repeat.validate_selected_identity(
            {"case_a": run}, repeat.resolve_environment(_environment(), profile="synthetic")
        )


def test_run_rejects_manual_resume_controls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(repeat.os, "environ", _environment())
    monkeypatch.setattr(repeat, "_host_preflight", lambda *_: pytest.fail("launched"))
    assert (
        repeat.main(
            [
                "run",
                "--credentials",
                "synthetic",
                "--repository",
                str(tmp_path),
                "--output",
                str(tmp_path / "results/reproductions/pilot"),
                "--resume-csv",
                str(tmp_path / "missing.csv"),
            ]
        )
        == 2
    )


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
    monkeypatch.setattr(repeat.time, "sleep", lambda *_: None)
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


def test_run_cli_continues_sequential_suite_with_low_host_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(repeat.os, "environ", _environment())
    floors: list[float] = []
    monkeypatch.setattr(repeat.time, "sleep", lambda *_: None)

    def low_memory(_repo: Path, _image: str, threshold: float) -> None:
        floors.append(threshold)
        repeat.check_headroom(
            available_bytes=2 * 1024**3,
            free_disk_bytes=20 * 1024**3,
            docker_memory_bytes=10 * 1024**3,
            min_available_gib=threshold,
        )

    monkeypatch.setattr(repeat, "_host_preflight", low_memory)
    monkeypatch.setattr(repeat, "_prepared_runtime_preflight", lambda: None)
    monkeypatch.setattr(repeat, "finalize", lambda *_: (0, 0))
    monkeypatch.setattr(repeat, "SREGYM_LITE_PROBLEMS", list(repeat.SREGYM_LITE_PROBLEMS)[:2])

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        case = command[command.index("--problem") + 1]
        batch = tmp_path / "results" / f"0930_12{len(floors):02d}"
        batch.mkdir()
        _run(batch, case)
        return subprocess.CompletedProcess(command, 0)

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
                str(tmp_path / "results/reproductions/pilot"),
            ]
        )
        == 0
    )
    assert floors == [6.0, 6.0]
    assert capsys.readouterr().err.count("Warning") == 2


def test_low_memory_check_warns_in_a_real_subprocess() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from sregym.results.assistant_v3_lite_repeat import check_headroom; "
            "check_headroom(available_bytes=2*1024**3, free_disk_bytes=20*1024**3, docker_memory_bytes=10*1024**3)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "Warning" in result.stderr and "continuing" in result.stderr


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


@pytest.mark.parametrize("memory_gib", [4, 10])
def test_host_preflight_uses_podman_memory_and_enforces_runtime_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_gib: int,
) -> None:
    class Memory:
        available = 7 * 1024**3

    class Disk:
        free = 20 * 1024**3

    monkeypatch.setattr(repeat.psutil, "virtual_memory", lambda: Memory())
    monkeypatch.setattr(repeat.shutil, "disk_usage", lambda *_: Disk())
    calls: list[list[str]] = []

    def podman(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command == ["docker", "info", "--format", "{{.MemTotal}}"]:
            raise subprocess.CalledProcessError(1, command, stderr="can't evaluate field MemTotal")
        if command == ["docker", "info", "--format", "{{.Host.MemTotal}}"]:
            return subprocess.CompletedProcess(command, 0, stdout=str(memory_gib * 1024**3))
        assert command == ["docker", "image", "inspect", "image:tag"]
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(repeat.subprocess, "run", podman)
    if memory_gib < 8:
        with pytest.raises(repeat.RepeatError, match="allocated less than 8 GiB"):
            repeat._host_preflight(tmp_path, "image:tag")
    else:
        repeat._host_preflight(tmp_path, "image:tag")
    assert calls == [
        ["docker", "info", "--format", "{{.MemTotal}}"],
        ["docker", "info", "--format", "{{.Host.MemTotal}}"],
        ["docker", "image", "inspect", "image:tag"],
    ]


@pytest.mark.parametrize("runtime", ["docker", "podman"])
def test_host_preflight_cli_accepts_docker_and_podman(tmp_path: Path, runtime: str) -> None:
    """Exercise CLI subprocess boundaries without requiring either daemon in CI."""
    executable = tmp_path / "docker"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['info', '--format'] and len(args) == 3:\n"
        "    field = '{{.MemTotal}}' if os.environ['TEST_RUNTIME'] == 'docker' else '{{.Host.MemTotal}}'\n"
        "    if args[2] != field:\n"
        "        print('unsupported memory field', file=sys.stderr)\n"
        "        sys.exit(1)\n"
        "    print(10 * 1024**3)\n"
        "elif args == ['image', 'inspect', 'image:tag']:\n"
        "    pass\n"
        "else:\n"
        "    sys.exit(2)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    environment = dict(repeat.os.environ)
    environment.update(PATH=f"{tmp_path}:{environment.get('PATH', '')}", TEST_RUNTIME=runtime)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; from sregym.results.assistant_v3_lite_repeat import _host_preflight; "
            "_host_preflight(Path.cwd(), 'image:tag'); print('preflight passed')",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "preflight passed" in result.stdout


def test_prepared_runtime_preflight_requires_ready_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def kubectl(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}),
        )

    monkeypatch.setattr(repeat.subprocess, "run", kubectl)
    repeat._prepared_runtime_preflight()
    assert calls == [["kubectl", "get", "nodes", "-o", "json"]]


def test_prepared_runtime_preflight_rejects_unready_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        repeat.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({"items": [{"status": {"conditions": [{"type": "Ready", "status": "False"}]}}]}),
        ),
    )
    with pytest.raises(repeat.RepeatError, match="Kubernetes nodes are not ready"):
        repeat._prepared_runtime_preflight()
    monkeypatch.setattr(
        repeat.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout=json.dumps({"items": [{"status": {"conditions": [None]}}]})
        ),
    )
    with pytest.raises(repeat.RepeatError, match="Kubernetes nodes are not ready"):
        repeat._prepared_runtime_preflight()


def test_prepared_runtime_preflight_rejects_kubectl_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        repeat.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("private context"))
    )
    with pytest.raises(repeat.RepeatError, match="selected context is unavailable") as error:
        repeat._prepared_runtime_preflight()
    assert "private context" not in str(error.value)


def test_run_does_not_launch_when_cluster_preflight_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(repeat.os, "environ", _environment())
    monkeypatch.setattr(repeat, "_host_preflight", lambda *_: None)
    monkeypatch.setattr(
        repeat, "_prepared_runtime_preflight", lambda *_: (_ for _ in ()).throw(repeat.RepeatError("not ready"))
    )
    monkeypatch.setattr(repeat.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("launched runner"))
    assert (
        repeat.main(
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
        == 2
    )


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
    monkeypatch.setattr(repeat, "_prepared_runtime_preflight", lambda *_: None)
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
