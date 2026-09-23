import hashlib
import json
import tomllib
from pathlib import Path

import pytest
import yaml

from clients.assistant_v3.prompt import (
    DEFAULT_PROFILE_PATH,
    DEFAULT_SNAPSHOT_PATH,
    PromptProfileError,
    render_prompt,
)

SOURCE_COMMIT = "3f6bd041231715184f962cedb33cfbcefa9b5b3d"
SOURCE_PATH = "clients/stratus/configs/diagnosis_agent_prompts.yaml"
SOURCE_FILE_SHA256 = "4d9de6d6c4c62a2280ec2dbb4cc73dc35c9404df3cb132099ca454f64294b5a2"
REFERENCE_SHA256 = "6dac52b47f7b3da387f22aab311962f5106b9bb0537263e1daa185130a9a786c"
PROFILE_ID = "sregym-stratus-diagnosis-v1"
SUBSTITUTION_IDS = (
    "remove_direct_kubernetes_enumeration",
    "remove_stratus_tool_instruction_protocol",
    "diagnosis_only_evaluation",
    "remove_mitigation_stage",
    "remove_downstream_stage_dependency",
    "replace_orchestrator_submission",
    "remove_stratus_round_control",
)
CONTEXT = {
    "app_name": "Online Boutique",
    "app_description": "A microservice application for an e-commerce storefront.",
    "app_namespace": "online-boutique",
}


def load_yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text())
    assert isinstance(value, dict)
    return value


def reference_text(snapshot: dict) -> str:
    return f"{snapshot['system']}\n\n{snapshot['user']}"


def write_yaml(path: Path, value: dict) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False))


def copied_prompt_files(tmp_path: Path) -> tuple[Path, Path, dict, dict]:
    snapshot = load_yaml(DEFAULT_SNAPSHOT_PATH)
    profile = load_yaml(DEFAULT_PROFILE_PATH)
    snapshot_path = tmp_path / "snapshot.yaml"
    profile_path = tmp_path / "profile.yaml"
    write_yaml(snapshot_path, snapshot)
    write_yaml(profile_path, profile)
    return snapshot_path, profile_path, snapshot, profile


def test_snapshot_matches_pinned_upstream_source_and_hashes():
    snapshot = load_yaml(DEFAULT_SNAPSHOT_PATH)
    source = Path(SOURCE_PATH).read_text()
    upstream = yaml.safe_load(source)

    assert snapshot["source_commit"] == SOURCE_COMMIT
    assert snapshot["source_path"] == SOURCE_PATH
    assert snapshot["source_file_sha256"] == SOURCE_FILE_SHA256
    assert hashlib.sha256(source.encode()).hexdigest() == SOURCE_FILE_SHA256
    assert snapshot["system"] == upstream["system"]
    assert snapshot["user"] == upstream["user"]
    assert snapshot["diagnosis_summary_prompt"] == upstream["diagnosis_summary_prompt"]
    assert hashlib.sha256(reference_text(snapshot).encode()).hexdigest() == REFERENCE_SHA256
    assert snapshot["reference_sha256"] == REFERENCE_SHA256


def test_prompt_module_and_yaml_assets_are_in_the_distribution():
    project = tomllib.loads(Path("pyproject.toml").read_text())
    setuptools = project["tool"]["setuptools"]

    assert {"clients.assistant_v3", "clients.assistant_v3.prompts"} <= set(setuptools["packages"])
    assert setuptools["package-data"]["clients.assistant_v3.prompts"] == ["*.yaml"]


def test_profile_has_only_the_approved_capability_substitutions():
    profile = load_yaml(DEFAULT_PROFILE_PATH)

    assert profile["profile_id"] == PROFILE_ID
    assert profile["reference_sha256"] == REFERENCE_SHA256
    assert tuple(item["id"] for item in profile["substitutions"]) == SUBSTITUTION_IDS
    assert all(item["source_exact"] and item["reason"] for item in profile["substitutions"])
    assert all(set(item) == {"id", "source_exact", "replacement", "reason"} for item in profile["substitutions"])


def test_render_applies_every_substitution_once_and_preserves_every_other_character():
    snapshot = load_yaml(DEFAULT_SNAPSHOT_PATH)
    profile = load_yaml(DEFAULT_PROFILE_PATH)
    original = reference_text(snapshot)
    expected = original
    untouched_segments: list[str] = []
    original_cursor = 0

    for substitution in profile["substitutions"]:
        source_exact = substitution["source_exact"]
        assert expected.count(source_exact) == 1
        source_position = original.index(source_exact, original_cursor)
        untouched_segments.append(original[original_cursor:source_position])
        original_cursor = source_position + len(source_exact)
        expected = expected.replace(source_exact, substitution["replacement"], 1)
    untouched_segments.append(original[original_cursor:])
    expected = expected.format_map(CONTEXT)

    rendered = render_prompt(CONTEXT)

    assert rendered.text == expected
    cursor = 0
    for segment in untouched_segments:
        concrete = segment.format_map(CONTEXT)
        assert rendered.text.find(concrete, cursor) >= cursor
        cursor = rendered.text.find(concrete, cursor) + len(concrete)
    assert "Get all the pods and deployments" not in rendered.text
    assert "two-stage pipeline" not in rendered.text
    assert "Mitigation (next stage)" not in rendered.text
    assert "submit_tool" not in rendered.text
    assert "In each round" not in rendered.text
    assert "Monitor and diagnose an application consisting of **MANY** microservices." in rendered.text
    assert "## Workloads (Applications)" in rendered.text
    assert "Be as specific and accurate as possible." in rendered.text
    assert "You will begin by analyzing the service's state and telemetry with the tools." in rendered.text


def test_render_records_complete_stable_provenance():
    first = render_prompt(CONTEXT)
    second = render_prompt(CONTEXT)

    assert first == second
    assert first.provenance.profile_id == PROFILE_ID
    assert first.provenance.source_commit == SOURCE_COMMIT
    assert first.provenance.source_path == SOURCE_PATH
    assert first.provenance.source_file_sha256 == SOURCE_FILE_SHA256
    assert first.provenance.reference_sha256 == REFERENCE_SHA256
    assert first.provenance.substitution_ids == SUBSTITUTION_IDS
    assert first.provenance.rendered_sha256 == hashlib.sha256(first.text.encode()).hexdigest()
    assert len(first.provenance.profile_sha256) == 64
    assert all(value in first.text for value in CONTEXT.values())
    assert all(f"{{{field}}}" not in first.text for field in CONTEXT)


@pytest.mark.parametrize("mode", ("missing", "duplicate"))
def test_render_rejects_substitutions_that_do_not_occur_exactly_once(tmp_path, mode):
    snapshot_path, profile_path, snapshot, profile = copied_prompt_files(tmp_path)
    source_exact = profile["substitutions"][0]["source_exact"]
    if mode == "missing":
        snapshot["system"] = snapshot["system"].replace(source_exact, "")
    else:
        snapshot["system"] += source_exact
    new_reference = hashlib.sha256(reference_text(snapshot).encode()).hexdigest()
    snapshot["reference_sha256"] = new_reference
    profile["reference_sha256"] = new_reference
    write_yaml(snapshot_path, snapshot)
    write_yaml(profile_path, profile)

    with pytest.raises(PromptProfileError, match="exactly once"):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)


def test_render_rejects_reference_hash_drift(tmp_path):
    snapshot_path, profile_path, snapshot, _ = copied_prompt_files(tmp_path)
    snapshot["system"] += " drift"
    write_yaml(snapshot_path, snapshot)

    with pytest.raises(PromptProfileError, match="reference hash"):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)


def test_render_rejects_profile_reference_mismatch(tmp_path):
    snapshot_path, profile_path, _, profile = copied_prompt_files(tmp_path)
    profile["reference_sha256"] = "0" * 64
    write_yaml(profile_path, profile)

    with pytest.raises(PromptProfileError, match="reference"):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)


@pytest.mark.parametrize(
    "mutation",
    (
        "changed_replacement",
        "changed_reason",
        "changed_order",
        "new_profile_id",
    ),
)
def test_prompt_changes_require_a_new_registered_profile_version(tmp_path, mutation):
    snapshot_path, profile_path, _, profile = copied_prompt_files(tmp_path)
    if mutation == "changed_replacement":
        profile["substitutions"][0]["replacement"] = "Use telemetry."
    elif mutation == "changed_reason":
        profile["substitutions"][0]["reason"] = "changed"
    elif mutation == "changed_order":
        profile["substitutions"].reverse()
    else:
        profile["profile_id"] = "sregym-stratus-diagnosis-v2"
    write_yaml(profile_path, profile)

    with pytest.raises(PromptProfileError, match="registered profile"):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)


@pytest.mark.parametrize(
    "forbidden_addition",
    (
        " Case: edge_request_filter_cpu_saturation.",
        " The injected fault is CPU saturation.",
        " The oracle expects a throttled deployment.",
        " Start with the checkout service.",
        " Investigate the last 15 minutes.",
        " The symptom is elevated latency.",
        " Route telemetry with run_id anon_0123.",
    ),
)
def test_profile_cannot_add_case_fault_oracle_diagnostic_or_routing_hints(tmp_path, forbidden_addition):
    snapshot_path, profile_path, _, profile = copied_prompt_files(tmp_path)
    profile["substitutions"][-1]["replacement"] = forbidden_addition
    write_yaml(profile_path, profile)

    with pytest.raises(PromptProfileError, match="registered profile"):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)


@pytest.mark.parametrize("placeholder", ("faulty_service", "time_window", "symptom", "run_id"))
def test_render_rejects_unapproved_or_unresolved_placeholders(tmp_path, placeholder):
    snapshot_path, profile_path, _, profile = copied_prompt_files(tmp_path)
    profile["substitutions"][0]["replacement"] = f"{{{placeholder}}}"
    write_yaml(profile_path, profile)

    with pytest.raises(PromptProfileError, match="placeholder"):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)


@pytest.mark.parametrize(
    "forbidden_key",
    (
        "case_id",
        "problem_id",
        "fault",
        "oracle",
        "service_name",
        "time_window",
        "symptom",
        "run_id",
        "connection_id",
    ),
)
def test_render_rejects_case_oracle_hint_and_routing_metadata(forbidden_key):
    context = {**CONTEXT, forbidden_key: "must-not-enter-the-prompt"}

    with pytest.raises(PromptProfileError, match="context fields"):
        render_prompt(context)


@pytest.mark.parametrize("missing_key", tuple(CONTEXT))
def test_render_requires_every_public_application_field(missing_key):
    context = CONTEXT.copy()
    context.pop(missing_key)

    with pytest.raises(PromptProfileError, match="context fields"):
        render_prompt(context)


def test_render_rejects_placeholder_shaped_application_values():
    with pytest.raises(PromptProfileError, match="unresolved placeholder"):
        render_prompt({**CONTEXT, "app_description": "Uses {unknown_runtime_value}."})


def test_rendered_prompt_and_provenance_are_json_serializable():
    rendered = render_prompt(CONTEXT)

    encoded = json.dumps(rendered.as_dict(), sort_keys=True)

    assert CONTEXT["app_name"] in encoded
    assert rendered.provenance.rendered_sha256 in encoded


def test_render_rejects_non_string_context_values():
    with pytest.raises(PromptProfileError, match="values must be strings"):
        render_prompt({**CONTEXT, "app_namespace": 42})  # type: ignore[dict-item]


@pytest.mark.parametrize("content", ("[not, a, mapping]", "broken: [yaml"))
def test_render_rejects_unavailable_invalid_or_non_mapping_yaml(tmp_path, content):
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text(content)

    with pytest.raises(PromptProfileError, match="unavailable or invalid|must be a mapping"):
        render_prompt(CONTEXT, profile_path=invalid)
    with pytest.raises(PromptProfileError, match="unavailable"):
        render_prompt(CONTEXT, snapshot_path=tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    "field,value,message",
    (
        ("source_path", "", "non-empty source_path"),
        ("source_commit", None, "non-empty source_commit"),
        ("source_file_sha256", "not-a-hash", "valid source_file_sha256"),
    ),
)
def test_render_rejects_invalid_snapshot_provenance(tmp_path, field, value, message):
    snapshot_path, profile_path, snapshot, _ = copied_prompt_files(tmp_path)
    snapshot[field] = value
    write_yaml(snapshot_path, snapshot)

    with pytest.raises(PromptProfileError, match=message):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)


@pytest.mark.parametrize("value", (None, [], "invalid"))
def test_render_requires_a_non_empty_substitution_list(tmp_path, value):
    snapshot_path, profile_path, _, profile = copied_prompt_files(tmp_path)
    profile["substitutions"] = value
    write_yaml(profile_path, profile)

    with pytest.raises(PromptProfileError, match="must contain substitutions"):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)


@pytest.mark.parametrize("value", ("not-a-mapping", {"id": "incomplete"}))
def test_render_rejects_substitution_schema_drift(tmp_path, value):
    snapshot_path, profile_path, _, profile = copied_prompt_files(tmp_path)
    profile["substitutions"][0] = value
    write_yaml(profile_path, profile)

    with pytest.raises(PromptProfileError, match="approved schema"):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)


def test_render_rejects_non_string_substitution_values(tmp_path):
    snapshot_path, profile_path, _, profile = copied_prompt_files(tmp_path)
    profile["substitutions"][0]["replacement"] = 42
    write_yaml(profile_path, profile)

    with pytest.raises(PromptProfileError, match="values must be strings"):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)


@pytest.mark.parametrize("field", ("id", "source_exact", "reason"))
def test_render_rejects_empty_required_substitution_values(tmp_path, field):
    snapshot_path, profile_path, _, profile = copied_prompt_files(tmp_path)
    profile["substitutions"][0][field] = ""
    write_yaml(profile_path, profile)

    with pytest.raises(PromptProfileError, match="require an id, source span, and reason"):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)


def test_render_rejects_duplicate_substitution_ids(tmp_path):
    snapshot_path, profile_path, _, profile = copied_prompt_files(tmp_path)
    profile["substitutions"][1]["id"] = profile["substitutions"][0]["id"]
    write_yaml(profile_path, profile)

    with pytest.raises(PromptProfileError, match="ids must be unique"):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)


@pytest.mark.parametrize("replacement,message", (("{", "invalid placeholder"), ("{app_name!r}", "conversions")))
def test_render_rejects_invalid_or_formatted_placeholders(tmp_path, replacement, message):
    snapshot_path, profile_path, _, profile = copied_prompt_files(tmp_path)
    profile["substitutions"][0]["replacement"] = replacement
    write_yaml(profile_path, profile)

    with pytest.raises(PromptProfileError, match=message):
        render_prompt(CONTEXT, snapshot_path=snapshot_path, profile_path=profile_path)
