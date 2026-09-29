"""Every Lite case has a separate public prompt recipe and oracle-side map."""

import json
import re
from pathlib import Path

import pytest
import yaml

from clients.assistant_v3.prompt import render_prompt
from sregym.conductor.problem_sets import SREGYM_LITE_PROBLEMS
from sregym.results.splunk_lite_cases import CasePromptError, load_prompt_recipe

CASE_ROOT = Path("cases/splunk-lite")
PROFILE = "sregym-stratus-diagnosis-v1"
DRAFT_CASE = "cronjob_sidecar_blocks_completion_hotel_reservation"
DRAFT_FILE = "proposed_starter_prompt.md"


def test_exact_registered_lite_case_directories_and_split_contracts():
    folders = {path.name for path in CASE_ROOT.iterdir() if path.is_dir()}
    assert folders == set(SREGYM_LITE_PROBLEMS)
    for case_id in SREGYM_LITE_PROBLEMS:
        case_dir = CASE_ROOT / case_id
        expected_files = {"prompt.yaml", "ground_truth.yaml"}
        if case_id == DRAFT_CASE:
            expected_files.add(DRAFT_FILE)
        assert {path.name for path in case_dir.iterdir()} == expected_files
        prompt = yaml.safe_load((case_dir / "prompt.yaml").read_text())
        evidence = yaml.safe_load((case_dir / "ground_truth.yaml").read_text())
        assert set(prompt) == {
            "schema", "profile_id", "application", "window", "symptom", "source_observation",
        }
        assert prompt["schema"] == "sregym.splunk_lite_prompt.v2"
        assert prompt["profile_id"] == PROFILE
        assert prompt["window"] == "runtime_incident_utc"
        assert isinstance(prompt["symptom"], str)
        assert 15 <= len(prompt["symptom"]) <= 160
        assert prompt["symptom"].endswith(".")
        assert "\n" not in prompt["symptom"]
        assert prompt["source_observation"]
        assert prompt["application"] in {
            "hotel_reservation", "social_network", "astronomy_shop"
        }
        assert evidence["schema"] == "sregym.splunk_lite_ground_truth.v1"
        assert evidence["problem_id"] == case_id
        assert evidence["review_status"] == "pending_live_verification"
        assert evidence["oracle_source"].startswith("sregym/conductor/problems/")
        assert Path(evidence["oracle_source"]).is_file()
        assert evidence["candidate_evidence"]
        for item in evidence["candidate_evidence"]:
            assert set(item) == {"fact", "signal", "query_focus"}
            assert item["signal"] in {
                "metrics", "traces", "logs", "pods", "events", "other_kubernetes_api", "other_subsystem_api"
            }
            assert item["fact"] and item["query_focus"]


def test_prompt_recipes_have_no_oracle_side_fields():
    forbidden = {"problem_id", "fault", "oracle", "root_cause", "run_id", "namespace"}
    for path in CASE_ROOT.glob("*/prompt.yaml"):
        prompt = yaml.safe_load(path.read_text())
        assert forbidden.isdisjoint(prompt)
        assert prompt["symptom"]


def test_cronjob_proposal_retains_exact_frozen_body_and_is_not_active():
    draft = (CASE_ROOT / DRAFT_CASE / DRAFT_FILE).read_text()
    prompt_block = re.search(r"## `prompt`.*?```text\n(.*?)```", draft, re.S)
    action_block = re.search(r"## `action_instructions`.*?```text\n(.*?)```", draft, re.S)
    assert prompt_block is not None and action_block is not None
    app = json.loads(Path("sregym/service/metadata/hotel-reservation.json").read_text())
    expected = render_prompt({
        "app_name": app["Name"],
        "app_description": app["Desc"],
        "app_namespace": app["Namespace"],
    }).text
    assert prompt_block.group(1) == expected
    assert action_block.group(1) == (
        "Telemetry time window: <START_UTC> through <END_UTC>, inclusive. "
        "Investigate using only telemetry within this time window. "
        "If telemetry is unavailable in this window, report that instead of using data outside it.\n"
        "Observed symptom: A scheduled background task in Hotel Reservation is taking unusually long to finish.\n"
    )
    assert "**not active**" in draft


def test_shared_loader_returns_reviewed_symptom_without_oracle_fields():
    recipe = load_prompt_recipe(DRAFT_CASE)
    assert recipe.application == "hotel_reservation"
    assert recipe.symptom == (
        "A scheduled background task in Hotel Reservation is taking unusually long to finish."
    )
    assert recipe.source_observation.startswith("Pre-agent ")
    with pytest.raises(CasePromptError):
        load_prompt_recipe("not-a-lite-case")
