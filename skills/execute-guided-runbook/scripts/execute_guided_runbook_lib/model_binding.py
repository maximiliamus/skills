"""Agent-independent model constraints and operator-controlled bindings."""

from __future__ import annotations

import re
from typing import Any

from .model import RunbookError

DEFAULT_MODEL_FAMILY = "current"
MODEL_BINDING_FIELDS = ("modelFamily", "modelId", "modelTier", "effortLevel")
FAMILY_PATTERN = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"


def normalize_model_id(value: object) -> str:
    normalized = value if isinstance(value, str) else ""
    if not normalized.strip() or not normalized.isprintable():
        raise RunbookError("modelId must be an explicit canonical runtime model ID")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RunbookError("modelId must be valid UTF-8") from exc
    return normalized


def normalize_model_family(value: object) -> str:
    raw = value if isinstance(value, str) else ""
    exact = re.match(r"^\s*exact:", raw, flags=re.IGNORECASE)
    if exact:
        return "exact:" + normalize_model_id(raw[exact.end():])
    normalized = raw.strip()
    keyword, separator, suffix = normalized.partition(":")
    keyword = keyword.lower()
    if not separator and keyword in {"current", "frontier", "predecessor"}:
        return keyword
    family = suffix.strip().lower()
    if family.startswith("v") and family[1:2].isdigit():
        family = family[1:]
    if separator and keyword == "family" and re.fullmatch(FAMILY_PATTERN, family):
        return "family:" + family
    raise RunbookError(
        "modelFamily must be current, frontier, predecessor, family:<version>, "
        "or exact:<model-id>"
    )


def model_binding(data: dict[str, Any]) -> dict[str, Any]:
    return {key: data[key] for key in MODEL_BINDING_FIELDS}


def validate_binding(data: dict[str, Any]) -> None:
    family = normalize_model_family(data.get("modelFamily"))
    model_id = normalize_model_id(data.get("modelId"))
    if family != data["modelFamily"] or model_id != data["modelId"]:
        raise RunbookError("Session model binding must use canonical values")
    if family.startswith("exact:") and model_id != family.removeprefix("exact:"):
        raise RunbookError("Exact model family disagrees with modelId")


def resolve_binding(runbook: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    binding = {
        "modelFamily": normalize_model_family(
            request["modelFamily"] if request.get("modelFamily") is not None
            else runbook["modelFamily"]
        ),
        "modelId": normalize_model_id(request.get("modelId")),
        "modelTier": runbook["modelTier"],
        "effortLevel": runbook["effortLevel"],
    }
    validate_binding(binding)
    return binding


def check_resume_request(state: dict[str, Any], request: dict[str, Any]) -> None:
    if (
        request.get("modelFamily") is not None
        and normalize_model_family(request["modelFamily"]) != state.get("modelFamily")
    ):
        raise RunbookError(
            "Resume modelFamily disagrees with the recorded session binding; "
            "no replacement session was created"
        )


def verify_model_id(data: dict[str, Any], value: object) -> str:
    model_id = normalize_model_id(value)
    if model_id != data["modelId"]:
        raise RunbookError(
            f"Executing model {model_id!r} does not match recorded modelId "
            f"{data['modelId']!r}; runbook execution cannot continue"
        )
    return model_id
