"""Bounded, secret-free ingestion proof for every registered Lite case."""

import json
import runpy
import sys
import warnings
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from sregym.results import splunk_lite_evidence as evidence_module
from sregym.results.splunk_lite_evidence import EvidenceError, verify_case_delivery

RUN_ID = "anon_0123456789abcdef0123456789abcdef"
START = "2026-09-25T18:00:00Z"
END = "2026-09-25T18:20:00Z"
CASE = "cronjob_sidecar_blocks_completion_hotel_reservation"


class FakeBackend:
    def __init__(self, *, failures=()):
        self.calls = []
        self.failures = set(failures)

    def query_signal(self, signal, context, scope, connection_id, checked_at, timeout_seconds) -> int:
        self.calls.append((signal, context, scope, connection_id, checked_at, timeout_seconds))
        if signal in self.failures:
            raise RuntimeError("backend-token-should-not-be-saved")
        return 1 if signal != "traces" else 0

    def query_object(self, kind, context, scope, connection_id, checked_at, timeout_seconds) -> int:
        self.calls.append((kind, context, scope, connection_id, checked_at, timeout_seconds))
        if kind in self.failures:
            raise RuntimeError("backend-token-should-not-be-saved")
        return 1


def make_run(tmp_path: Path, *, connection="HPEC1vyAAAA", profile="sregym-stratus-diagnosis-v1"):
    run = tmp_path / "attempt"
    (run / "assistant_v3").mkdir(parents=True)
    (run / "run_metadata.json").write_text(json.dumps({
        "problem_id": CASE,
        "run_id": RUN_ID,
        "attempt": 1,
        "benchmark_profile": "svelte",
        "comparable": False,
        "logs_connection_id": connection,
        "observability_provider": "splunk",
    }))
    (run / "assistant_v3" / "request.json").write_text(json.dumps({
        "prompt_profile_id": profile,
        "prompt": "Investigate the application and diagnose the root cause.",
        "action_instructions": (
            f"Telemetry time window: {START} through {END}, inclusive. "
            "Investigate using only telemetry within this time window."
        ),
    }))
    return run


def test_verifier_uses_exact_window_connection_and_saves_sanitized_partial_proof(tmp_path):
    run = make_run(tmp_path)
    backend = FakeBackend(failures={"pods"})
    path = verify_case_delivery(run, backend=backend, expected_connection="HPEC1vyAAAA", query_attempts=1)
    proof = json.loads(path.read_text())

    assert path == run / "splunk_lite_delivery.json"
    assert proof["schema"] == "sregym.splunk_lite_delivery.v1"
    assert proof["case_id"] == CASE
    assert proof["window"] == {"start": START, "end": END}
    assert proof["logs_connection_id"] == "HPEC1vyAAAA"
    assert proof["checks"]["metrics"]["status"] == "present"
    assert proof["checks"]["traces"]["status"] == "missing"
    assert proof["checks"]["pods"]["status"] == "query_error"
    assert proof["source_comparison"] == "unverified"
    assert proof["oracle_evidence"] == "unverified"
    assert len(backend.calls) == 6
    assert all(call[2].namespaces == ("hotel-reservation",) for call in backend.calls)
    assert all(call[3] == "HPEC1vyAAAA" for call in backend.calls)
    window_end = datetime(2026, 9, 25, 18, 20, tzinfo=UTC)
    assert all(
        call[4] == window_end + timedelta(minutes=2) if call[0] == "metrics" else call[4] == window_end
        for call in backend.calls
    )
    assert all(call[1].attempt_started_at == datetime(2026, 9, 25, 18, tzinfo=UTC) for call in backend.calls)
    assert "backend-token-should-not-be-saved" not in path.read_text()


def test_verifier_polls_asynchronous_trace_search_before_marking_missing(tmp_path):
    class EventuallyVisibleBackend(FakeBackend):
        def query_signal(self, signal, context, scope, connection_id, checked_at, timeout_seconds):
            result = super().query_signal(signal, context, scope, connection_id, checked_at, timeout_seconds)
            if signal == "traces" and sum(call[0] == "traces" for call in self.calls) >= 2:
                return 1
            return result

    run = make_run(tmp_path)
    backend = EventuallyVisibleBackend()
    path = verify_case_delivery(
        run, backend=backend, expected_connection="HPEC1vyAAAA", sleep=lambda _: None,
    )

    proof = json.loads(path.read_text())
    assert proof["checks"]["traces"] == {"status": "present", "count": 1}
    assert sum(call[0] == "traces" for call in backend.calls) == 2


def test_verifier_requires_exact_reviewed_symptom_in_guided_request(tmp_path):
    run = make_run(tmp_path)
    request_path = run / "assistant_v3" / "request.json"
    request = json.loads(request_path.read_text())
    request["action_profile_id"] = "sregym-symptom-window-v1"
    request["action_instructions"] += (
        "\nObserved symptom: A scheduled background task in Hotel Reservation "
        "is taking unusually long to finish."
    )
    request_path.write_text(json.dumps(request))
    assert verify_case_delivery(
        run, backend=FakeBackend(), expected_connection="HPEC1vyAAAA", query_attempts=1,
    ).exists()

    request["action_instructions"] = request["action_instructions"].replace(
        "taking unusually long to finish", "blocked by a sidecar",
    )
    request_path.write_text(json.dumps(request))
    with pytest.raises(EvidenceError, match="reviewed symptom"):
        verify_case_delivery(run, backend=FakeBackend(), expected_connection="HPEC1vyAAAA")


@pytest.mark.parametrize("connection,profile", [
    ("wrong-connection", "sregym-stratus-diagnosis-v1"),
    ("HPEC1vyAAAA", "unreviewed-prompt-profile"),
])
def test_verifier_refuses_wrong_target_or_prompt_profile(tmp_path, connection, profile):
    run = make_run(tmp_path, connection=connection, profile=profile)
    backend = FakeBackend()
    with pytest.raises(EvidenceError):
        verify_case_delivery(run, backend=backend, expected_connection="HPEC1vyAAAA")
    assert backend.calls == []
    assert not (run / "splunk_lite_delivery.json").exists()


@pytest.mark.parametrize("filename,contents", [
    ("run_metadata.json", None),
    ("run_metadata.json", "{"),
    ("run_metadata.json", "[]"),
    ("assistant_v3/request.json", "[]"),
])
def test_verifier_rejects_missing_or_invalid_attempt_json(tmp_path, filename, contents):
    run = make_run(tmp_path)
    path = run / filename
    if contents is None:
        path.unlink()
    else:
        path.write_text(contents)
    with pytest.raises(EvidenceError, match="attempt artifact"):
        verify_case_delivery(run, backend=FakeBackend(), expected_connection="HPEC1vyAAAA")


@pytest.mark.parametrize("field,value,message", [
    ("problem_id", "not-a-lite-case", "registered Lite"),
    ("observability_provider", "none", "Splunk provider"),
    ("run_id", None, "opaque run identity"),
])
def test_verifier_rejects_wrong_attempt_metadata(tmp_path, field, value, message):
    run = make_run(tmp_path)
    path = run / "run_metadata.json"
    metadata = json.loads(path.read_text())
    metadata[field] = value
    path.write_text(json.dumps(metadata))
    backend = FakeBackend()
    with pytest.raises(EvidenceError, match=message):
        verify_case_delivery(run, backend=backend, expected_connection="HPEC1vyAAAA")
    assert backend.calls == []


@pytest.mark.parametrize("instructions,message", [
    (None, "exact incident window"),
    ("Investigate now", "exact incident window"),
    (f"Telemetry time window: invalid through {END}, inclusive.", "invalid incident UTC"),
    (f"Telemetry time window: 2026-09-25T18:00:00+01:00 through {END}, inclusive.", "UTC Z"),
    (f"Telemetry time window: {END} through {START}, inclusive.", "empty or reversed"),
    (f"Telemetry time window: {START} through {END}, inclusive. {RUN_ID}", "leaked"),
])
def test_verifier_rejects_bad_window_or_run_id_leak(tmp_path, instructions, message):
    run = make_run(tmp_path)
    path = run / "assistant_v3/request.json"
    request = json.loads(path.read_text())
    request["action_instructions"] = instructions
    path.write_text(json.dumps(request))
    with pytest.raises(EvidenceError, match=message):
        verify_case_delivery(run, backend=FakeBackend(), expected_connection="HPEC1vyAAAA")


def test_verifier_rejects_run_id_in_benchmark_prompt(tmp_path):
    run = make_run(tmp_path)
    path = run / "assistant_v3/request.json"
    request = json.loads(path.read_text())
    request["prompt"] += f" {RUN_ID}"
    path.write_text(json.dumps(request))
    with pytest.raises(EvidenceError, match="leaked"):
        verify_case_delivery(run, backend=FakeBackend(), expected_connection="HPEC1vyAAAA")


def test_verifier_marks_invalid_backend_count_as_query_error(tmp_path):
    run = make_run(tmp_path)
    class BadBackend(FakeBackend):
        def query_object(self, kind, context, scope, connection_id, checked_at, timeout_seconds) -> int:
            return -1

    backend = BadBackend()
    proof = json.loads(verify_case_delivery(run, backend=backend, expected_connection="HPEC1vyAAAA").read_text())
    assert proof["checks"]["pods"] == {"status": "query_error", "count": None}
    assert proof["checks"]["events"] == {"status": "query_error", "count": None}


def test_verifier_rejects_missing_application_namespace(tmp_path, monkeypatch):
    run = make_run(tmp_path)
    metadata_dir = tmp_path / "metadata"
    metadata_dir.mkdir()
    (metadata_dir / "hotel-reservation.json").write_text('{"Name":"Hotel Reservation"}')
    monkeypatch.setattr(evidence_module, "METADATA_ROOT", metadata_dir)
    with pytest.raises(EvidenceError, match="application namespace"):
        verify_case_delivery(run, backend=FakeBackend(), expected_connection="HPEC1vyAAAA")


def test_cli_uses_configured_connection_and_closes_backend(tmp_path, monkeypatch, capsys):
    run = make_run(tmp_path)
    backend = SimpleNamespace(close=lambda: None)
    calls = []
    monkeypatch.setattr(sys, "argv", ["splunk_lite_evidence", "--run-dir", str(run)])
    monkeypatch.setattr(evidence_module.SplunkConfig, "from_env", lambda: SimpleNamespace(logs_connection_id="HPEC1vyAAAA"))
    monkeypatch.setattr(evidence_module, "SplunkHttpBackend", lambda config: backend)
    monkeypatch.setattr(evidence_module, "verify_case_delivery", lambda *args, **kwargs: calls.append(kwargs) or run / "proof.json")
    assert evidence_module.main() == 0
    assert calls[0]["expected_connection"] == "HPEC1vyAAAA"
    assert capsys.readouterr().out.strip() == str(run / "proof.json")


def test_cli_requires_configured_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["splunk_lite_evidence", "--run-dir", str(tmp_path)])
    monkeypatch.setattr(evidence_module.SplunkConfig, "from_env", lambda: SimpleNamespace(logs_connection_id=None))
    with pytest.raises(SystemExit) as raised:
        evidence_module.main()
    assert raised.value.code == 2


def test_module_cli_help_needs_no_credentials(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["splunk_lite_evidence", "--help"])
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*found in sys.modules.*", category=RuntimeWarning)
        with pytest.raises(SystemExit) as raised:
            runpy.run_module("sregym.results.splunk_lite_evidence", run_name="__main__")
    assert raised.value.code == 0
    assert "--run-dir" in capsys.readouterr().out
