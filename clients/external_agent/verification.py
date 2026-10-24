"""Read existing native results using Conductor's stage_name.capitalize() contract."""

import hashlib
import json
import math
from pathlib import Path

from .driver import Hold


def prevent_native_grade_replay(terminal, results_path):
    """Refuse another investigation when *any* native evaluation was saved.

    The orchestrator supplies the exact owned attempt's results path. Even an
    evaluation error needs separate disposition; it never authorizes a retry.
    """
    if terminal.get("grade") is not None:
        raise Hold("Existing native grade requires retention, never another diagnosis")
    path = Path(results_path)
    if path.exists() and "Diagnosis" in json.loads(path.read_text()):
        raise Hold("Saved native evaluation forbids another investigation")


def verify_freeze(manifest, expected_sha256, *, _ancestors=frozenset()):
    """Validate a pinned manifest, its files and explicitly pinned child manifests.

    Paths are relative to each manifest's directory and may not escape it. The
    caller's expected digest is external to the manifest being verified. This is
    an integrity check, not a replacement for trusted storage or code review.
    """
    manifest = Path(manifest).resolve()
    if manifest in _ancestors:
        raise Hold("Nested manifest cycle")
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != expected_sha256:
        raise Hold("Frozen manifest changed")
    root = manifest.parent
    value = json.loads(manifest.read_text())
    if not isinstance(value.get("files"), dict) or not value["files"]:
        raise Hold("Frozen manifest must bind its implementation files")
    for name, expected in value["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise Hold("Frozen file escapes its owned root or is missing")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise Hold("Frozen implementation file changed")
    for name, expected in value.get("manifests", {}).items():
        child = (root / name).resolve()
        if child == manifest or not child.is_relative_to(root):
            raise Hold("Nested manifest escapes its owned root or cycles")
        verify_freeze(child, expected, _ancestors=_ancestors | {manifest})
    return value


def verified_diagnosis(state, results, driver, ledger):
    if results.get("cleanup_failed") or results.get("infrastructure_invalid") or results.get("incomplete_stage"):
        raise Hold("Original native infrastructure/cleanup result is invalid")
    grade = results.get("Diagnosis")
    if (
        not isinstance(grade, dict)
        or grade.get("error")
        or grade.get("judgment") not in {"True", "False"}
        or type(grade.get("accuracy")) not in {int, float}
        or not math.isfinite(grade["accuracy"])
        or not 0 <= grade["accuracy"] <= 100
        or type(grade.get("success")) is not bool
        or grade["success"] != (grade["judgment"] == "True")
    ):
        raise Hold("No original valid native Diagnosis grade")
    if (
        driver.get("phase") != "submitted"
        or driver.get("create_requests") != 1
        or driver.get("submission_requests") != 1
        or not driver.get("final_answer")
        or grade.get("submission") != driver["final_answer"]
    ):
        raise Hold("Saved original grade does not bind to exactly one diagnosis submission")
    for phase in ["evaluate:diagnosis", "cleanup"]:
        rows = [row for row in ledger if row.get("phase") == phase]
        if len(rows) != 2 or [row.get("event") for row in rows] != ["start", "end"] or rows[-1].get("outcome") != "ok":
            raise Hold("Exactly one original successful evaluation and cleanup boundary required")
    if any(state.get(k) for k in ["cleanup_error_type", "case_transport_cleanup_error", "preservation_error_type"]):
        raise Hold("Original execution recorded a cleanup/preservation error")
    if not state.get("local_api_exited") or not state.get("protected_preservation_verified"):
        raise Hold("Original final preservation/API readback missing")
    correction = state.get("phase") == "held"
    if correction and not (
        state.get("error_type") == "Hold"
        and state.get("held_from") == "native_grading_and_cleanup"
        and state.get("error") == "No valid native grade; preserve original placeholder without scoring it"
    ):
        raise Hold("This is not the isolated lowercase result-reader reporting hold")
    if not correction and state.get("phase") != "completed_verified":
        raise Hold("Execution is not terminal")
    return grade, correction
