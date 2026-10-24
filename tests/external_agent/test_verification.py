import hashlib
import json
from copy import deepcopy

import pytest

from clients.external_agent.driver import Hold
from clients.external_agent.verification import prevent_native_grade_replay, verified_diagnosis, verify_freeze


def completed():
    state = {"phase": "completed_verified", "local_api_exited": True, "protected_preservation_verified": True}
    driver = {
        "phase": "submitted",
        "create_requests": 1,
        "submission_requests": 1,
        "final_answer": "Synthetic diagnosis",
    }
    results = {
        "Diagnosis": {"accuracy": 67, "success": False, "judgment": "False", "submission": driver["final_answer"]}
    }
    ledger = [
        {"phase": phase, "event": event, **({"outcome": "ok"} if event == "end" else {})}
        for phase in ["evaluate:diagnosis", "cleanup"]
        for event in ["start", "end"]
    ]
    return state, results, driver, ledger


def test_valid_failed_diagnosis_is_retained_without_retry_or_zero_fill():
    state, results, driver, ledger = completed()
    grade, corrected = verified_diagnosis(state, results, driver, ledger)
    assert grade == results["Diagnosis"] and not grade["success"] and not corrected


@pytest.mark.parametrize("accuracy", [None, True, float("nan"), float("inf"), -1, 101])
def test_invalid_grade_is_never_counted(accuracy):
    state, results, driver, ledger = completed()
    results["Diagnosis"]["accuracy"] = accuracy
    with pytest.raises(Hold):
        verified_diagnosis(state, results, driver, ledger)


@pytest.mark.parametrize("change", ["cleanup_failed", "different_answer", "duplicate_grade", "no_exit"])
def test_grade_requires_matching_original_answer_cleanup_and_exit(change):
    state, results, driver, ledger = completed()
    if change == "cleanup_failed":
        results["cleanup_failed"] = True
    elif change == "different_answer":
        driver["final_answer"] = "Different answer"
    elif change == "duplicate_grade":
        ledger.extend(deepcopy(ledger[:2]))
    else:
        state["local_api_exited"] = False
    with pytest.raises(Hold):
        verified_diagnosis(state, results, driver, ledger)


@pytest.mark.parametrize("grade", [{"accuracy": 100}, {"error": "judge failure"}, None])
def test_saved_native_evaluation_blocks_another_investigation_even_with_null_external_grade(tmp_path, grade):
    path = tmp_path / "native-results.json"
    path.write_text(json.dumps({"Diagnosis": grade}))
    with pytest.raises(Hold, match="forbids another investigation"):
        prevent_native_grade_replay({"grade": None}, path)


def test_existing_external_grade_also_blocks_replay(tmp_path):
    with pytest.raises(Hold):
        prevent_native_grade_replay({"grade": {"accuracy": 67}}, tmp_path / "missing.json")


def test_no_evaluation_can_proceed_to_separate_harness_admission(tmp_path):
    prevent_native_grade_replay({"grade": None}, tmp_path / "missing.json")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_nested_qualification_is_verified_before_startup(tmp_path):
    code = tmp_path / "adapter.py"
    code.write_text("unchanged = True\n")
    child = tmp_path / "adapter-freeze.json"
    child.write_text(json.dumps({"files": {"adapter.py": digest(code)}}))
    outer = tmp_path / "implementation.json"
    outer.write_text(
        json.dumps({"files": {"adapter.py": digest(code)}, "manifests": {"adapter-freeze.json": digest(child)}})
    )
    pin = digest(outer)
    verify_freeze(outer, pin)
    child.write_text(json.dumps({"files": {"adapter.py": "stale"}}))
    with pytest.raises(Hold, match="manifest changed"):
        verify_freeze(outer, pin)


def test_unchanged_outer_manifest_does_not_hide_changed_code(tmp_path):
    code = tmp_path / "adapter.py"
    code.write_text("original\n")
    manifest = tmp_path / "implementation.json"
    manifest.write_text(json.dumps({"files": {"adapter.py": digest(code)}}))
    pin = digest(manifest)
    code.write_text("changed\n")
    with pytest.raises(Hold, match="file changed"):
        verify_freeze(manifest, pin)


@pytest.mark.parametrize("filename", ["../outside.py", "/etc/passwd"])
def test_manifest_cannot_escape_owned_directory(tmp_path, filename):
    manifest = tmp_path / "implementation.json"
    manifest.write_text(json.dumps({"files": {filename: "unused"}}))
    with pytest.raises(Hold, match="escapes"):
        verify_freeze(manifest, digest(manifest))
