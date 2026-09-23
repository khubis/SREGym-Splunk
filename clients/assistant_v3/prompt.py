"""Immutable, mechanically derived SRE Gym prompt for Assistant v3."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from string import Formatter
from typing import Any

import yaml

_PROMPTS_DIR = Path(__file__).with_name("prompts")
DEFAULT_SNAPSHOT_PATH = _PROMPTS_DIR / "upstream-stratus-v1.yaml"
DEFAULT_PROFILE_PATH = _PROMPTS_DIR / "diagnosis-v1.yaml"
DEFAULT_PROFILE_ID = "sregym-stratus-diagnosis-v1"

_ALLOWED_CONTEXT_FIELDS = frozenset({"app_name", "app_description", "app_namespace"})
_SUBSTITUTION_FIELDS = frozenset({"id", "source_exact", "replacement", "reason"})
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_PLACEHOLDER_PATTERN = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_REGISTERED_PROFILE_HASHES = {DEFAULT_PROFILE_ID: "003c952ea8365b5d6840b561a8928d50394a7245abedd1af874a05fbbe4bfbb2"}


class PromptProfileError(ValueError):
    """An immutable prompt artifact failed validation."""


@dataclass(frozen=True)
class PromptProvenance:
    profile_id: str
    profile_sha256: str
    source_path: str
    source_commit: str
    source_file_sha256: str
    reference_sha256: str
    template_sha256: str
    rendered_sha256: str
    substitution_ids: tuple[str, ...]


@dataclass(frozen=True)
class RenderedPrompt:
    text: str
    provenance: PromptProvenance

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def render_prompt(
    context: Mapping[str, str],
    *,
    snapshot_path: Path = DEFAULT_SNAPSHOT_PATH,
    profile_path: Path = DEFAULT_PROFILE_PATH,
) -> RenderedPrompt:
    """Render one validated profile using only public application metadata."""
    supplied_fields = frozenset(context)
    if supplied_fields != _ALLOWED_CONTEXT_FIELDS:
        raise PromptProfileError("prompt context fields must exactly match the public application metadata")
    if any(not isinstance(context[field], str) for field in _ALLOWED_CONTEXT_FIELDS):
        raise PromptProfileError("prompt context values must be strings")

    snapshot = _load_mapping(snapshot_path, "upstream prompt snapshot")
    profile = _load_mapping(profile_path, "prompt profile")
    system = _required_string(snapshot, "system", "upstream prompt snapshot")
    user = _required_string(snapshot, "user", "upstream prompt snapshot")
    reference = f"{system}\n\n{user}"
    actual_reference_hash = _sha256(reference)
    snapshot_reference_hash = _required_hash(snapshot, "reference_sha256", "upstream prompt snapshot")
    if actual_reference_hash != snapshot_reference_hash:
        raise PromptProfileError("upstream prompt reference hash does not match its immutable content")

    profile_reference_hash = _required_hash(profile, "reference_sha256", "prompt profile")
    if profile_reference_hash != snapshot_reference_hash:
        raise PromptProfileError("prompt profile reference does not match the upstream snapshot")

    substitutions = _substitutions(profile)
    template = reference
    for substitution in substitutions:
        source_exact = substitution["source_exact"]
        if template.count(source_exact) != 1:
            raise PromptProfileError(f"substitution {substitution['id']!r} must occur exactly once")
        template = template.replace(source_exact, substitution["replacement"], 1)

    _validate_placeholders(template)
    profile_id = _required_string(profile, "profile_id", "prompt profile")
    profile_hash = _sha256(_canonical_json(profile))
    if _REGISTERED_PROFILE_HASHES.get(profile_id) != profile_hash:
        raise PromptProfileError("prompt content does not match a registered profile version")

    rendered = template.format_map(dict(context))
    if _PLACEHOLDER_PATTERN.search(rendered):
        raise PromptProfileError("rendered prompt contains an unresolved placeholder")

    provenance = PromptProvenance(
        profile_id=profile_id,
        profile_sha256=profile_hash,
        source_path=_required_string(snapshot, "source_path", "upstream prompt snapshot"),
        source_commit=_required_string(snapshot, "source_commit", "upstream prompt snapshot"),
        source_file_sha256=_required_hash(snapshot, "source_file_sha256", "upstream prompt snapshot"),
        reference_sha256=actual_reference_hash,
        template_sha256=_sha256(template),
        rendered_sha256=_sha256(rendered),
        substitution_ids=tuple(substitution["id"] for substitution in substitutions),
    )
    return RenderedPrompt(text=rendered, provenance=provenance)


def _load_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        raise PromptProfileError(f"{label} is unavailable or invalid") from None
    if not isinstance(value, dict):
        raise PromptProfileError(f"{label} must be a mapping")
    return value


def _required_string(value: Mapping[str, Any], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise PromptProfileError(f"{label} must contain non-empty {key}")
    return item


def _required_hash(value: Mapping[str, Any], key: str, label: str) -> str:
    item = _required_string(value, key, label)
    if _HASH_PATTERN.fullmatch(item) is None:
        raise PromptProfileError(f"{label} must contain a valid {key}")
    return item


def _substitutions(profile: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    raw = profile.get("substitutions")
    if not isinstance(raw, list) or not raw:
        raise PromptProfileError("prompt profile must contain substitutions")
    substitutions: list[dict[str, str]] = []
    observed_ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != _SUBSTITUTION_FIELDS:
            raise PromptProfileError("prompt substitutions must use the approved schema")
        substitution = {key: item[key] for key in _SUBSTITUTION_FIELDS}
        if any(not isinstance(value, str) for value in substitution.values()):
            raise PromptProfileError("prompt substitution values must be strings")
        if not substitution["id"] or not substitution["source_exact"] or not substitution["reason"]:
            raise PromptProfileError("prompt substitutions require an id, source span, and reason")
        if substitution["id"] in observed_ids:
            raise PromptProfileError("prompt substitution ids must be unique")
        observed_ids.add(substitution["id"])
        substitutions.append(substitution)
    return tuple(substitutions)


def _validate_placeholders(template: str) -> None:
    try:
        parsed = tuple(Formatter().parse(template))
    except ValueError:
        raise PromptProfileError("prompt template contains an invalid placeholder") from None
    fields = {field for _, field, _, _ in parsed if field is not None}
    if fields != _ALLOWED_CONTEXT_FIELDS:
        raise PromptProfileError("prompt template contains an unapproved or missing placeholder")
    if any(conversion or format_spec for _, field, format_spec, conversion in parsed if field is not None):
        raise PromptProfileError("prompt placeholders may not use conversions or format specifications")


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


__all__ = [
    "DEFAULT_PROFILE_ID",
    "DEFAULT_PROFILE_PATH",
    "DEFAULT_SNAPSHOT_PATH",
    "PromptProfileError",
    "PromptProvenance",
    "RenderedPrompt",
    "render_prompt",
]
