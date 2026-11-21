"""Agent-independent model constraints and operator-controlled bindings."""

from __future__ import annotations

import re

from .model import RunbookError

DEFAULT_MODEL_FAMILY = "current"
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
        return "exact:" + normalize_model_id(raw[exact.end() :])
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
        "modelFamily must be current, frontier, predecessor, family:<version>, or exact:<model-id>"
    )
