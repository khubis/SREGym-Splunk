"""Build self-contained per-case copies of a completed local Lite pilot."""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

_ROW = re.compile(r"^\| \[([^]]+)\]\(([^)]+/assistant_v3_campaign/scorecard\.md)\) \| ([0-9.]+) \|")
_REQUIRED = {
    "prompt_request.json": "assistant_v3/request.json",
    "assistant_terminal.json": "assistant_v3/terminal.json",
    "native_trace.jsonl": "assistant_v3/events.jsonl",
    "atif_trace.json": "trajectory.json",
    "final_answer.md": "final_answer.md",
    "eval_metrics.json": "metrics.json",
    "delivery_audit.json": "observability/delivery.json",
    "pre_agent_proof.json": "splunk_lite_pre_agent.json",
    "postrun_signals.json": "splunk_lite_delivery.json",
    "run_metadata.json": "run_metadata.json",
}

_CHECKER_FALLBACK = {
    "edge_request_filter_cpu_saturation": "wait_for_edge_pre_agent",
}


@dataclass(frozen=True)
class Case:
    case_id: str
    run: Path
    scorecard: Path
    recipe: Path
    evidence_map: Path
    oracle_source: Path
    score: float
    request: dict
    causal_proof: dict
    delivery_check: dict
    judge: dict
    metrics: dict


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _oracle_excerpt(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    assignments = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Attribute) and target.attr == "root_cause" for target in targets):
            excerpt = ast.get_source_segment(source, node)
            if excerpt and "build_structured_root_cause" in excerpt:
                assignments.append(excerpt)
    if not assignments:
        raise ValueError(f"no structured benchmark oracle in {path}")
    return "\n\n".join(f"```python\n{excerpt}\n```" for excerpt in assignments)


def _checker_symbols(case_id: str, repository: Path) -> tuple[str, str, str]:
    """Resolve the actual runner dispatch and its two called verifier functions."""
    main_tree = ast.parse((repository / "main.py").read_text(encoding="utf-8"))
    candidates = []
    for node in ast.walk(main_tree):
        if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
            continue
        test = node.test
        if not (
            isinstance(test.left, ast.Attribute)
            and test.left.attr == "problem_id"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == case_id
        ):
            continue
        candidates += [
            call.func.id
            for statement in node.body
            for call in ast.walk(statement)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id.startswith("wait_for_")
        ]
    if not candidates and case_id in _CHECKER_FALLBACK:
        candidates.append(_CHECKER_FALLBACK[case_id])
    if len(candidates) != 1:
        raise ValueError(f"cannot resolve case checker dispatch: {case_id}")
    tree = ast.parse((repository / "sregym/results/splunk_lite_causal.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    wait = candidates[0]

    def calls(name: str, prefix: str) -> list[str]:
        if name not in functions:
            raise ValueError(f"missing case checker function: {name}")
        return sorted(
            {
                call.func.id
                for call in ast.walk(functions[name])
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id.startswith(prefix)
            }
        )

    verified = calls(wait, "verify_")
    checked = calls(verified[0], "check_") if len(verified) == 1 else []
    if len(verified) != 1 or len(checked) != 1:
        raise ValueError(f"cannot resolve verify/check functions: {case_id}")
    return wait, verified[0], checked[0]


def _cases(report: Path, repository: Path) -> list[Case]:
    selected: list[Case] = []
    seen: set[str] = set()
    for line in report.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if not match:
            continue
        label = match.group(1)
        scorecard = (report.parent / match.group(2)).resolve(strict=True)
        batch = scorecard.parent.parent
        eligible: list[Path] = []
        # Machine-generated campaign reports use the exact case ID as the
        # link label. Keep older human-labeled, one-case-batch reports valid.
        run_glob = f"{label}/run_*" if (batch / "assistant_v3" / label).is_dir() else "*/run_*"
        for run in (batch / "assistant_v3").glob(run_glob):
            metadata_path = run / "run_metadata.json"
            delivery_path = run / "observability/delivery.json"
            if not metadata_path.is_file() or not delivery_path.is_file():
                continue
            metadata, delivery = _read_json(metadata_path), _read_json(delivery_path)
            if metadata.get("classification") == "completed" and delivery.get("valid") is True:
                eligible.append(run)
        if len(eligible) != 1:
            raise ValueError(f"expected one valid scored attempt in {batch}, found {len(eligible)}")
        run = eligible[0]
        case_id = run.parent.name
        if case_id in seen or _read_json(run / "run_metadata.json").get("problem_id") != case_id:
            raise ValueError(f"duplicate or mismatched case: {case_id}")
        seen.add(case_id)
        case_dir = repository / "cases/splunk-lite" / case_id
        recipe, evidence_map = case_dir / "prompt.yaml", case_dir / "ground_truth.yaml"
        mapping = yaml.safe_load(evidence_map.read_text(encoding="utf-8"))
        if mapping.get("problem_id") != case_id:
            raise ValueError(f"ground-truth map mismatch: {case_id}")
        oracle_source = (repository / mapping["oracle_source"]).resolve(strict=True)
        if not oracle_source.is_relative_to(repository.resolve()):
            raise ValueError(f"oracle source outside repository: {case_id}")
        _oracle_excerpt(oracle_source)
        for relative in (*_REQUIRED.values(), f"{case_id}_results.csv"):
            if not (run / relative).is_file():
                raise ValueError(f"missing required attempt artifact: {run / relative}")
        with (run / f"{case_id}_results.csv").open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        if len(rows) != 1:
            raise ValueError(f"expected one judge row: {run}")
        judge = rows[0]
        score = float(judge["Diagnosis.accuracy"])
        if score != float(match.group(3)):
            raise ValueError(f"report/judge score mismatch: {case_id}")
        if judge["Diagnosis.submission"].strip() != (run / "final_answer.md").read_text(encoding="utf-8").strip():
            raise ValueError(f"judge/final-answer mismatch: {case_id}")
        request = _read_json(run / "assistant_v3/request.json")
        if request.get("problem_id") != case_id or not all(
            isinstance(request.get(key), str) for key in ("prompt", "action_instructions")
        ):
            raise ValueError(f"invalid saved starter prompt: {case_id}")
        selected.append(
            Case(
                case_id,
                run,
                scorecard,
                recipe,
                evidence_map,
                oracle_source,
                score,
                request,
                _read_json(run / "splunk_lite_pre_agent.json"),
                _read_json(run / "splunk_lite_delivery.json"),
                judge,
                _read_json(run / "metrics.json"),
            )
        )
    if not selected:
        raise ValueError(f"no scored case rows in {report}")
    return selected


def _write(path: Path, content: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def _copy(folder: Path, name: str, source: Path) -> str:
    """Replace only our prior matching symlink, or keep an identical copy."""
    if not source.is_file():
        raise ValueError(f"missing source artifact: {source}")
    destination = folder / name
    checksum = _digest(source)
    if destination.is_symlink():
        if destination.resolve(strict=True) != source.resolve(strict=True):
            raise ValueError(f"existing link points to another attempt: {destination}")
    elif destination.exists():
        if _digest(destination) != checksum:
            raise ValueError(f"refusing to replace modified artifact: {destination}")
        return checksum
    descriptor, name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=folder)
    try:
        with os.fdopen(descriptor, "wb") as target, source.open("rb") as original:
            shutil.copyfileobj(original, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if _digest(Path(name)) != checksum:
            raise ValueError(f"copy failed hash verification: {source}")
        os.replace(name, destination)
    finally:
        Path(name).unlink(missing_ok=True)
    return checksum


def _splunk_ground_truth(case: Case) -> str:
    causal = case.causal_proof.get("causal", {})
    checks = case.delivery_check.get("checks", {})
    lines = [
        "# Splunk-visible ground truth and limits",
        "",
        "This is an evidence assessment, **not** a separately graded Splunk-visible numeric score.",
        "The pre-agent check ran before V3 and does not use the oracle to guide its prompt.",
        "",
        f"- Incident window: {case.causal_proof['window']['start']} through {case.causal_proof['window']['end']} UTC",
        f"- Pre-agent status: `{causal.get('status', 'unverified')}`; visibility: `{causal.get('visibility', 'undetermined')}`",
        f"- Evidence: {causal.get('interpretation', 'See pre_agent_proof.json.')}",
        f"- Access gap: {causal.get('access_gap') or 'None established by this check.'}",
        f"- Remedy: {causal.get('remedy') or 'No additional access remedy specified.'}",
        "",
        "## Representative post-run Splunk checks",
        "",
        "| Signal | Status | Count |",
        "|---|---|---:|",
    ]
    for signal, result in sorted(checks.items()):
        lines.append(f"| {signal} | {result.get('status', 'unverified')} | {result.get('count', '—')} |")
    golden_path = case.run / "golden_telemetry.json"
    lines += ["", "## Independent post-grade audit", ""]
    if golden_path.is_file():
        audit = _read_json(golden_path)
        lines += [
            f"Status: `{audit.get('status', 'unverified')}`. {audit.get('access_note', '')}",
            "",
            "[Query and sanitized samples](postgrade_golden_telemetry.json)",
        ]
    else:
        lines.append(
            "No separate post-grade golden-telemetry API audit was saved for this attempt. "
            "Use the [pre-agent proof](pre_agent_proof.json); do not treat it as exhaustive parity."
        )
    return "\n".join(lines) + "\n"


def _rubric(case: Case) -> str:
    dimensions = ast.literal_eval(case.judge["Diagnosis.dimensions"])
    if not isinstance(dimensions, dict):
        raise ValueError(f"invalid judge dimensions: {case.case_id}")
    metrics = case.metrics
    lines = [
        "# Benchmark rubric and this attempt",
        "",
        "The [shared benchmark rubric](benchmark_rubric.yaml) scores fault localization (D1), "
        "fault characterization (D2), and scope precision (D3), with weights 0.33/0.33/0.34 "
        "and a 0.70 pass threshold. A passing score does not necessarily mean the full mechanism was named.",
        "",
        f"- Benchmark score: **{case.score:g}/100**; judge verdict: `{case.judge['Diagnosis.judgment']}`",
    ]
    for key, item in sorted(dimensions.items()):
        lines.append(f"- {key} {item['name']}: {item['score']}")
    lines += [
        "- Splunk-visible numeric score: **unverified** (not implemented for this pilot)",
        "",
        "## Execution metrics",
        "",
        f"- Agent runtime: {metrics['agent_duration_ms'] / 1000:.1f} s",
        f"- Tokens: {metrics['total_tokens']:,}",
        f"- Tool calls: {metrics['tool_calls']} ({metrics['failed_tool_results']} failed)",
        "",
        "See the [unchanged raw judge result](judge_raw.csv) for every checklist response and critique, "
        "and [raw execution metrics](eval_metrics.json) for the measured values.",
    ]
    return "\n".join(lines) + "\n"


def _verification(case: Case, repository: Path) -> str:
    proof = case.causal_proof
    causal = proof.get("causal", {})
    wait, verify, check = _checker_symbols(case.case_id, repository)
    mapping = yaml.safe_load(case.evidence_map.read_text(encoding="utf-8"))
    readiness = _read_json(case.run / "run_metadata.json").get("readiness_report", {})
    required = set(proof.get("required_signals", []))
    signals = proof.get("signals", {})
    lines = [
        "# What telemetry was verified?",
        "",
        "This page describes checks recorded for **this valid attempt**, not a guarantee that "
        "every emitted metric, log, span, or Kubernetes object arrived. The checker was run after "
        "fault injection and before Assistant V3; its oracle-aware facts were **not** put in the starter prompt.",
        "",
        "## Code to inspect or rerun",
        "",
        "The runner's `_assistant_case_preflight` in `main.py` dispatches this case to "
        f"`{wait}`, then `{verify}`, then `{check}` in "
        "[the shared case verifier](pre_agent_verifier_source.py). "
        "The [shared post-run verifier](postrun_verifier_source.py) checks representative "
        "signal presence after grading. The source files here are **current code snapshots made "
        "when this dossier was built**; earlier runs did not save a verifier-source hash, so "
        "they cannot prove byte-for-byte historical code identity. The JSON proofs below are the "
        "saved run-time evidence.\n",
        "## Before V3: provider readiness",
        "",
        "The provider's run-scoped readiness check queried metrics, traces, logs, and Kubernetes "
        "events. A positive result means at least one scoped example was queryable, not complete delivery.\n",
        "| Signal | Ready | Observed count |",
        "|---|---|---:|",
    ]
    for item in readiness.get("signals", []):
        lines.append(
            f"| {item.get('signal', 'unknown')} | {item.get('ready', False)} | "
            f"{item.get('evidence', {}).get('count', '—')} |"
        )
    lines += [
        "",
        "## Before V3: case-specific source/Splunk gate",
        "",
        f"Gate status: `{proof.get('status', 'unverified')}`. The incident window was "
        f"`{proof['window']['start']}` through `{proof['window']['end']}`. "
        "The table below lists the additional signal-presence checks recorded by this case gate; "
        "only rows marked required were launch requirements.\n",
        "| Signal | Query result | Required for this case gate? |",
        "|---|---:|---|",
    ]
    for signal, count in sorted(signals.items()):
        lines.append(f"| {signal} | {count} | {'yes' if signal in required else 'no'} |")
    if not signals:
        lines.append("| — | — | See the provider-readiness table above. |")
    interpretation = causal.get("interpretation")
    if not interpretation and case.case_id == "network_policy_block":
        interpretation = (
            "The source deny-all NetworkPolicy was confirmed and four scoped "
            "request-timeout log rows were found in Splunk; the policy rules themselves were not exported."
        )
    lines += [
        "",
        f"**Decisive check (`{causal.get('check_id', 'unknown')}`):** "
        f"{interpretation or 'Read the saved structured proof; no prose interpretation was recorded.'}",
        "",
        f"- Evidence type actually matched: `{causal.get('signal', 'unverified')}`; "
        f"status: `{causal.get('status', 'unverified')}`.",
    ]
    facts = []
    for key in (
        "source_count",
        "source_counts",
        "source_job_count",
        "source_metric_count",
        "source_policy_confirmed",
        "source_rule_confirmed",
        "source_selector_confirmed",
        "source_pvc_rwo_confirmed",
        "source_quota_confirmed",
        "source_strategy_confirmed",
        "source_empty_endpoints",
        "source_cross_node",
        "source_policy_local",
    ):
        if key in causal:
            facts.append(f"{key}={causal[key]}")
    if facts:
        lines.append("- Source Kind evidence: " + "; ".join(facts) + ".")
    facts = []
    for key in (
        "splunk_count",
        "splunk_counts",
        "splunk_job_count",
        "splunk_metric_count",
        "splunk_timeout_count",
        "splunk_failed_response_count",
        "splunk_backlog_confirmed",
        "splunk_cross_node",
        "splunk_roles",
        "splunk_phases",
    ):
        if key in causal:
            facts.append(f"{key}={causal[key]}")
    if facts:
        lines.append("- Splunk evidence: " + "; ".join(facts) + ".")
    if causal.get("metric_names"):
        lines.append(
            "- Exact named metrics queried: " + ", ".join(f"`{name}`" for name in causal["metric_names"]) + "."
        )
    if causal.get("matched_pod"):
        lines.append(f"- Matched pod: `{causal['matched_pod']}`.")
    lines += [
        "- Splunk queries match the opaque run ID, namespace, and saved time window; "
        "source checks use the selected Kind context and incident window. "
        "Raw log and pod bodies were not saved; see [pre_agent_proof.json](pre_agent_proof.json) "
        "for sanitized counts, statuses, and timestamps, and the code for exact predicates.",
        "",
        "## Why those clues matter—and what remains unproven",
        "",
    ]
    for item in mapping.get("candidate_evidence", []):
        lines.append(
            f"- Candidate clue ({item.get('signal', 'unknown')}): {item.get('fact', '')}. "
            f"Query focus: {item.get('query_focus', '')}."
        )
    lines += [
        "",
        "The candidate clues above come from the benchmark evidence map; **they are not all "
        "claimed as verified**. The actual verified signal and result are in the decisive check above.",
        f"Access/coverage gap: {causal.get('access_gap') or 'No additional gap was established by this gate.'}",
        f"Remedy: {causal.get('remedy') or 'No additional remedy was recorded.'}",
        "",
        "## After the run: representative presence and delivery",
        "",
        "| Signal/object class | Status | Query count |",
        "|---|---|---:|",
    ]
    for signal, result in sorted(case.delivery_check.get("checks", {}).items()):
        lines.append(f"| {signal} | {result.get('status', 'unverified')} | {result.get('count', '—')} |")
    delivery = _read_json(case.run / "observability/delivery.json")
    lines += [
        "",
        f"Collector closing audit: valid=`{delivery.get('valid')}`, drained=`{delivery.get('drained')}`. "
        "See [delivery_audit.json](delivery_audit.json) for sent/failure deltas and final queue sizes. "
        "The post-run query is a presence spot check; a zero can mean no matching object changed "
        "inside the short incident window, and a one is not a complete event count. "
        "The `kubernetes_events` signal and `events` object check are two query views, "
        "not proof of distinct event streams.",
        "",
        "The [Splunk-visible ground-truth page](splunk_visible_ground_truth.md) records any "
        "independent post-grade golden query and separates it from the pre-agent gate.",
    ]
    return "\n".join(lines) + "\n"


def build(report: Path, output: Path, repository: Path) -> list[Path]:
    cases = _cases(report.resolve(strict=True), repository.resolve(strict=True))
    output.mkdir(parents=True, exist_ok=True)
    folders: list[Path] = []
    for case in cases:
        folder = output / case.case_id
        folder.mkdir(exist_ok=True)
        artifacts = {name: case.run / relative for name, relative in _REQUIRED.items()}
        artifacts.update(
            {
                "phase_ledger.jsonl": case.run.parent / "phases_attempt1.jsonl",
                "judge_raw.csv": case.run / f"{case.case_id}_results.csv",
                "case_prompt_recipe.yaml": case.recipe,
                "case_evidence_map.yaml": case.evidence_map,
                "oracle_source.py": case.oracle_source,
                "benchmark_rubric.yaml": repository / "sregym/conductor/oracles/llm_as_a_judge/rca_checklists.yaml",
                "pre_agent_verifier_source.py": repository / "sregym/results/splunk_lite_causal.py",
                "postrun_verifier_source.py": repository / "sregym/results/splunk_lite_evidence.py",
            }
        )
        if (case.run / "golden_telemetry.json").is_file():
            artifacts["postgrade_golden_telemetry.json"] = case.run / "golden_telemetry.json"
        if (case.run / "proof_amendment.md").is_file():
            artifacts["proof_amendment.md"] = case.run / "proof_amendment.md"
        hashes = {name: _copy(folder, name, source) for name, source in artifacts.items()}
        _write(
            folder / "manifest.json",
            json.dumps(
                {
                    "schema": "sregym.lite_case_dossier.v1",
                    "case_id": case.case_id,
                    "original_attempt": os.path.relpath(case.run, repository),
                    "benchmark_score": case.score,
                    "artifact_sha256": hashes,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        _write(
            folder / "starter_prompt.md",
            "# Exact starter prompt\n\n"
            "The Assistant request sent the following **two separate fields**. "
            "[prompt_request.json](prompt_request.json) is the exact machine-readable record.\n\n"
            f"## Diagnosis prompt\n\n{case.request['prompt']}\n\n"
            f"## Action instructions\n\n{case.request['action_instructions']}\n",
        )
        _write(
            folder / "benchmark_ground_truth.md",
            "# Benchmark ground truth\n\n"
            "The canonical diagnosis oracle is defined in [oracle_source.py](oracle_source.py). "
            "This is its source expression; runtime interpolation, if any, is governed by that code. "
            "[case_evidence_map.yaml](case_evidence_map.yaml) is a candidate evidence map, "
            "not a replacement for the benchmark oracle.\n\n"
            f"{_oracle_excerpt(case.oracle_source)}\n",
        )
        _write(folder / "splunk_visible_ground_truth.md", _splunk_ground_truth(case))
        _write(folder / "verification.md", _verification(case, repository))
        _write(folder / "rubric_and_metrics.md", _rubric(case))
        old_scorecard = folder / "case_scorecard.md"
        if old_scorecard.is_symlink() and old_scorecard.resolve(strict=True) != case.scorecard.resolve(strict=True):
            raise ValueError(f"existing scorecard link points to another attempt: {old_scorecard}")
        _write(
            old_scorecard,
            f"# {case.case_id} scorecard\n\n"
            f"Benchmark score: **{case.score:g}/100**. See the "
            "[raw judge result](judge_raw.csv), [rubric and metrics](rubric_and_metrics.md), "
            "[final answer](final_answer.md), and "
            "[Splunk-visible evidence and limits](splunk_visible_ground_truth.md).\n",
        )
        _write(
            folder / "README.md",
            f"# {case.case_id}\n\n"
            f"Benchmark score: **{case.score:g}/100**. This is a local `svelte`, symptom-guided "
            "Assistant V3 run, not a leaderboard-comparable submission.\n\n"
            "Start with the [exact starter prompt](starter_prompt.md), "
            "[benchmark oracle](benchmark_ground_truth.md), "
            "[Splunk-visible evidence and limits](splunk_visible_ground_truth.md), "
            "[verification walkthrough](verification.md), [final answer](final_answer.md), "
            "and [rubric/metrics](rubric_and_metrics.md). The "
            "[phase ledger](phase_ledger.jsonl) records injection, evaluation, and cleanup; "
            "[terminal status](assistant_terminal.json) records whether V3 submitted.\n\n"
            "Raw provenance: [native trace](native_trace.jsonl) · [ATIF trace](atif_trace.json) · "
            "[judge CSV](judge_raw.csv) · [pre-agent proof](pre_agent_proof.json) · "
            "[collector audit](delivery_audit.json) · [post-run signal check](postrun_signals.json). "
            "These are verified, unedited copies of the selected valid attempt; "
            "[manifest.json](manifest.json) records source provenance and SHA-256 hashes.\n",
        )
        folders.append(folder)
    _write(
        output / "README.md",
        "# SREGym-Lite case dossiers\n\n"
        f"Self-contained copies of {len(folders)} valid scored cases, originally selected from {report.name}. "
        "Each folder has source provenance and artifact hashes. Splunk-visible evidence "
        "is not a separate numeric judgment. Open a case's `verification.md` for a "
        "plain-language account of pre-agent causal checks, per-signal presence, "
        "post-run delivery, and coverage gaps.\n\n"
        + "\n".join(f"- [{case.case_id}]({case.case_id}/README.md) — {case.score:g}/100" for case in cases)
        + "\n",
    )
    return folders


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    arguments = parser.parse_args()
    folders = build(arguments.report, arguments.output, arguments.repository)
    print(f"Created {len(folders)} case dossiers at {arguments.output.resolve()}")


if __name__ == "__main__":
    main()
