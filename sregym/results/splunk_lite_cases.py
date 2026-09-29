"""Validated, public symptom recipes for the registered SREGym-Lite cases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from sregym.conductor.problem_sets import SREGYM_LITE_PROBLEMS

CASE_ROOT = Path(__file__).resolve().parents[2] / "cases" / "splunk-lite"
_FIELDS = frozenset({
    "schema", "profile_id", "application", "window", "symptom", "source_observation",
})
_APPLICATIONS = frozenset({"hotel_reservation", "social_network", "astronomy_shop"})


class CasePromptError(ValueError):
    """A Lite prompt recipe is missing or cannot be safely rendered."""


@dataclass(frozen=True)
class CasePromptRecipe:
    case_id: str
    application: str
    symptom: str
    source_observation: str


def load_prompt_recipe(case_id: str) -> CasePromptRecipe:
    """Load only approved, pre-diagnosis context; never read oracle-side files."""
    if case_id not in SREGYM_LITE_PROBLEMS:
        raise CasePromptError("case is not registered in SREGym-Lite")
    try:
        value = yaml.safe_load((CASE_ROOT / case_id / "prompt.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise CasePromptError("case prompt recipe is unavailable or invalid") from None
    if not isinstance(value, dict) or frozenset(value) != _FIELDS:
        raise CasePromptError("case prompt recipe has an unapproved field")
    if (
        value["schema"] != "sregym.splunk_lite_prompt.v2"
        or value["profile_id"] != "sregym-stratus-diagnosis-v1"
        or value["window"] != "runtime_incident_utc"
        or value["application"] not in _APPLICATIONS
    ):
        raise CasePromptError("case prompt recipe has an unapproved profile or application")
    symptom, source = value["symptom"], value["source_observation"]
    if (
        not isinstance(symptom, str)
        or not 15 <= len(symptom) <= 160
        or not symptom.endswith(".")
        or "\n" in symptom
        or "anon_" in symptom
        or not isinstance(source, str)
        or not source.startswith("Pre-agent ")
        or len(source) > 200
        or "\n" in source
    ):
        raise CasePromptError("case symptom or source observation is invalid")
    return CasePromptRecipe(case_id, value["application"], symptom, source)
