"""Runbook registry loading, resolution, and mutation commands."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .model import (
    ACCEPTANCE_POLICIES,
    DEFAULT_ACCEPTANCE_POLICY,
    DEFAULT_EFFORT_LEVEL,
    DEFAULT_MODEL_TIER,
    DEFAULT_STEP_ORDER,
    EFFORT_LEVELS,
    GENERATED_PATH_ID_PATTERN,
    ID_PATTERN,
    MODEL_TIERS,
    REGISTRY_FIELDS,
    REGISTRY_SCHEMA_VERSION,
    STEP_ORDERS,
    RunbookError,
    valid_acceptance_threshold,
)
from .model_binding import DEFAULT_MODEL_FAMILY, normalize_model_family
from .storage import (
    inside_repo,
    path_runbook_id,
    read_runbook_text,
)
from .validation import RequestError


def normalize_registry_path(
    relative_path: str,
    repo_root: Path,
) -> tuple[str, Path, Path]:
    if relative_path != relative_path.strip():
        raise RunbookError(
            f"Registry path must not contain surrounding whitespace: {relative_path!r}"
        )
    if len(relative_path.splitlines()) != 1:
        raise RunbookError(f"Registry path must not contain line breaks: {relative_path!r}")
    portable_path = relative_path.replace("\\", "/")
    candidate_path = Path(portable_path)
    if candidate_path.is_absolute() or re.match(r"^[A-Za-z]:", portable_path):
        raise RunbookError(
            f"Registry path must be relative to the repository root: {relative_path}"
        )
    if ".." in portable_path.split("/"):
        raise RunbookError(f"Registry path must not contain parent traversal: {relative_path}")
    if candidate_path.suffix.lower() != ".md":
        raise RunbookError(f"Registry path must reference a Markdown file: {relative_path}")
    resolved_path = inside_repo(repo_root, repo_root / candidate_path)
    return portable_path, candidate_path, resolved_path


def validate_registered_id(runbook_id: str) -> None:
    if not ID_PATTERN.fullmatch(runbook_id):
        raise RunbookError(f"Invalid runbook id: {runbook_id}")
    if GENERATED_PATH_ID_PATTERN.fullmatch(runbook_id):
        raise RunbookError(f"Runbook id uses the reserved path-session namespace: {runbook_id}")


def validate_registry(
    data: dict[str, Any],
    path: Path,
    repo_root: Path,
) -> dict[str, dict[str, Any]]:
    """Validate a project config or a template without publishing it."""
    unknown_top_level = set(data) - {"schemaVersion", "runbooks", "stateDirectory"}
    if unknown_top_level:
        fields = ", ".join(sorted(unknown_top_level))
        raise RunbookError(f"Unknown registry field(s) in {path}: {fields}")
    version = data.get("schemaVersion")
    if type(version) is not int or version != REGISTRY_SCHEMA_VERSION:
        raise RequestError(f"Unsupported runbook registry schema in {path}", "UNSUPPORTED_SCHEMA")
    if data.get("stateDirectory", ".runbooks") != ".runbooks":
        raise RunbookError(f"Runbook state must live in .runbooks: {path}")

    entries = data.get("runbooks")
    if not isinstance(entries, list):
        raise RunbookError(f"Registry runbooks must be a list in {path}")

    registry: dict[str, dict[str, Any]] = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise RunbookError(f"Every registry entry must be an object in {path}")
        unknown_entry_fields = set(raw_entry) - REGISTRY_FIELDS
        if unknown_entry_fields:
            fields = ", ".join(sorted(unknown_entry_fields))
            hint = ""
            if unknown_entry_fields & {"modelTier", "effortLevel", "modelFamily"}:
                hint = " Set modelTier, effortLevel and modelFamily in Markdown frontmatter."
            raise RunbookError(f"Unknown runbook field(s) in {path}: {fields}.{hint}")
        runbook_id = raw_entry.get("id")
        relative_path = raw_entry.get("path")
        if not isinstance(runbook_id, str) or not runbook_id.strip():
            raise RunbookError(f"Every registry entry requires a non-empty id: {raw_entry}")
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise RunbookError(f"Every registry entry requires a non-empty path: {raw_entry}")
        if runbook_id != runbook_id.strip():
            raise RunbookError(
                f"Runbook id must not contain surrounding whitespace: {runbook_id!r}"
            )
        portable_path, candidate_path, _ = normalize_registry_path(relative_path, repo_root)

        derived_title = candidate_path.stem.replace("-", " ").replace("_", " ").title()
        optional_values = {
            "title": raw_entry.get("title"),
            "description": raw_entry.get("description"),
        }
        for field, value in optional_values.items():
            if field in raw_entry and (not isinstance(value, str) or not value.strip()):
                raise RunbookError(
                    f"Registry field {field} must be a non-empty string: {raw_entry}"
                )
        resolved_title = (optional_values["title"] or derived_title).strip()
        entry = {
            "id": runbook_id,
            "title": resolved_title,
            "path": portable_path,
            "description": (
                optional_values["description"] or f"Repository runbook: {resolved_title}."
            ).strip(),
            "_descriptionOverride": "description" in raw_entry,
        }
        runbook_id = entry["id"]
        validate_registered_id(runbook_id)
        if runbook_id in registry:
            raise RunbookError(f"Duplicate runbook id: {runbook_id}")
        registry[runbook_id] = entry  # type: ignore[assignment]
    return registry


def public_registry_entry(entry: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in entry.items() if not key.startswith("_")}


def resolve_runbook(
    selector: str,
    repo_root: Path,
    registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if selector not in registry and Path(selector).suffix.lower() == ".md":
        _, _, selected_path = normalize_registry_path(selector, repo_root)
        matches = sorted(
            key
            for key, entry in registry.items()
            if inside_repo(repo_root, repo_root / entry["path"]) == selected_path
        )
        if matches:
            selector = matches[0]
    if selector in registry:
        entry = registry[selector]
        runbook_path = inside_repo(repo_root, repo_root / entry["path"])
        result: dict[str, Any] = {
            "id": entry["id"],
            "title": entry["title"],
            "path": str(runbook_path),
            "relativePath": runbook_path.relative_to(repo_root.resolve()).as_posix(),
            "description": entry["description"],
            "_descriptionOverride": entry.get("_descriptionOverride", False),
            "registered": True,
        }
    elif Path(selector).suffix.lower() != ".md":
        raise RequestError(f"Runbook id is not registered: {selector}", "NOT_FOUND")
    else:
        raw_path = Path(selector)
        candidate = raw_path if raw_path.is_absolute() else repo_root / raw_path
        runbook_path = inside_repo(repo_root, candidate)
        relative_path = runbook_path.relative_to(repo_root.resolve()).as_posix()
        result = {
            "id": path_runbook_id(relative_path),
            "title": runbook_path.stem.replace("-", " ").title(),
            "path": str(runbook_path),
            "relativePath": relative_path,
            "description": "Unregistered repository runbook.",
            "registered": False,
        }

    if runbook_path.suffix.lower() != ".md":
        raise RunbookError(f"Runbook must be a Markdown file: {runbook_path}")
    if not runbook_path.is_file():
        raise RequestError(f"Runbook does not exist: {runbook_path}", "NOT_FOUND")
    runbook_text = read_runbook_text(runbook_path)
    document_properties = runbook_document_properties(runbook_text, runbook_path)
    document_id = document_properties.pop("id", None)
    document_description = document_properties.pop("description", None)
    if document_id is not None:
        if (
            document_id in registry
            and inside_repo(repo_root, repo_root / registry[document_id]["path"]) != runbook_path
        ):
            raise RunbookError(
                f"Runbook frontmatter id {document_id!r} is already registered to a different path: "
                f"{registry[document_id]['path']}"
            )
        if result["registered"] and document_id != result["id"]:
            raise RunbookError(
                f"Runbook frontmatter id {document_id!r} does not match registered id "
                f"{result['id']!r} in {runbook_path}"
            )
        result["id"] = document_id
    if document_description is not None and not result.get("_descriptionOverride", False):
        result["description"] = document_description
    result.update(document_properties)
    result.pop("_descriptionOverride", None)
    return result


def runbook_document_properties(text: str, path: Path) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "effortLevel": DEFAULT_EFFORT_LEVEL,
        "modelTier": DEFAULT_MODEL_TIER,
        "modelFamily": DEFAULT_MODEL_FAMILY,
        "acceptancePolicy": DEFAULT_ACCEPTANCE_POLICY,
        "stepOrder": DEFAULT_STEP_ORDER,
        "acceptanceThreshold": None,
        "delegateSteps": False,
        "maxParallelSteps": 1,
        "maxAttempts": 1,
        "steps": {},
        "_explicitModelSettings": [],
    }
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return properties
    try:
        closing_index = next(
            index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
        )
    except StopIteration as exc:
        raise RunbookError(f"Runbook frontmatter is not terminated in {path}") from exc

    document = parse_runbook_frontmatter(lines[1:closing_index], path)

    supported = {
        "effortLevel",
        "modelTier",
        "modelFamily",
        "id",
        "description",
        "acceptancePolicy",
        "stepOrder",
        "acceptanceThreshold",
    }
    model_choices = {"effortLevel": EFFORT_LEVELS, "modelTier": MODEL_TIERS}
    properties["_explicitModelSettings"] = [
        key for key in ("modelFamily", "modelTier", "effortLevel") if key in document
    ]
    if "stepOverrides" in document:
        raise RunbookError("Use steps for per-step settings")
    for key, value in document.items():
        if key in {"delegateSteps", "maxParallelSteps", "maxAttempts", "steps"}:
            properties[key] = value
            continue
        if key not in supported:
            continue
        if key == "description" and value is None:
            raise RunbookError(f"Runbook frontmatter description must not be empty in {path}")
        if not isinstance(value, str):
            raise RunbookError(f"Runbook frontmatter property {key} must be a string in {path}")
        if key == "modelFamily":
            try:
                value = normalize_model_family(value)
            except RunbookError as exc:
                raise RunbookError(f"Invalid runbook frontmatter in {path}: {exc}") from exc
        else:
            value = value.strip()
        if key == "id":
            validate_registered_id(value)
        if key == "description" and not value:
            raise RunbookError(f"Runbook frontmatter description must not be empty in {path}")
        if key in model_choices and value not in model_choices[key]:
            choices = ", ".join(sorted(model_choices[key]))
            raise RunbookError(
                f"Invalid runbook frontmatter property {key}={value!r} in {path}; "
                f"expected one of: {choices}"
            )
        if key == "acceptancePolicy" and value not in ACCEPTANCE_POLICIES:
            choices = ", ".join(sorted(ACCEPTANCE_POLICIES))
            raise RunbookError(
                f"Invalid runbook frontmatter property {key}={value!r} in {path}; "
                f"expected one of: {choices}"
            )
        if key == "stepOrder" and value not in STEP_ORDERS:
            choices = ", ".join(sorted(STEP_ORDERS))
            raise RunbookError(
                f"Invalid runbook frontmatter property {key}={value!r} in {path}; "
                f"expected one of: {choices}"
            )
        if key == "acceptanceThreshold" and not valid_acceptance_threshold(value):
            raise RunbookError(
                f"Invalid runbook frontmatter property {key}={value!r} in {path}; "
                "expected a percentage from 1% to 100%"
            )
        properties[key] = value

    if (
        properties["acceptancePolicy"] != "flexible"
        and properties["acceptanceThreshold"] is not None
    ):
        raise RunbookError(
            f"Runbook acceptanceThreshold requires acceptancePolicy: flexible in {path}"
        )
    return properties


FRONTMATTER_KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
UNSUPPORTED_FRONTMATTER_VALUE_PREFIXES = ("[", "{", "|", ">", "&", "*", "!")


def parse_runbook_frontmatter(lines: list[str], path: Path) -> dict[str, Any]:
    """Read scalars, multiline descriptions and the bounded steps mapping."""
    return parse_mapping(lines, path, 0, 0, "document")[0]


def parse_mapping(lines, path, index, indentation, level):
    properties = {}
    while index < len(lines):
        line = lines[index]
        line_number = index + 2
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        current_indent = len(line) - len(line.lstrip(" "))
        if current_indent < indentation:
            break
        if current_indent != indentation or line[current_indent:].startswith("\t"):
            raise RunbookError(f"Unexpected frontmatter indentation in {path}:{line_number}")
        line = line[indentation:]
        if ":" not in line:
            raise RunbookError(
                f"Runbook frontmatter must contain key: value properties in {path} "
                f"at line {line_number}"
            )
        key, raw_value = line.split(":", 1)
        key = key.strip()
        pattern = ID_PATTERN if level in {"steps", "dependsOn"} else FRONTMATTER_KEY_PATTERN
        if not pattern.fullmatch(key):
            raise RunbookError(
                f"Invalid runbook frontmatter property name {key!r} in {path} at line {line_number}"
            )
        if key in properties:
            raise RunbookError(f"Duplicate runbook frontmatter property {key} in {path}")
        raw_value = raw_value.strip()
        mapping = (
            (level == "document" and key == "steps")
            or level == "steps"
            or (level == "step" and key == "dependsOn")
        )
        if mapping:
            if raw_value == "{}":
                properties[key] = {}
                index += 1
                continue
            if raw_value:
                raise RunbookError(f"Expected a mapping for {key} in {path}:{line_number}")
            child = next(
                (
                    i
                    for i in range(index + 1, len(lines))
                    if lines[i].strip() and not lines[i].lstrip().startswith("#")
                ),
                None,
            )
            child_indent = (
                len(lines[child]) - len(lines[child].lstrip(" ")) if child is not None else 0
            )
            if child_indent <= indentation:
                raise RunbookError(
                    f"Expected an indented mapping for {key} in {path}:{line_number}"
                )
            child_level = {"document": "steps", "steps": "step", "step": "dependsOn"}[level]
            properties[key], index = parse_mapping(lines, path, child, child_indent, child_level)
            continue
        if raw_value == "|":
            if level != "document" or key != "description":
                raise RunbookError(
                    f"Only runbook frontmatter description supports a multiline value "
                    f"in {path} at line {line_number}"
                )
            properties[key], index = parse_multiline_description(lines, index + 1, path)
            continue
        if key == "delegateSteps" and level in {"document", "step"}:
            if raw_value not in {"true", "false"}:
                raise RunbookError(f"{key} must be a boolean in {path}:{line_number}")
            properties[key] = raw_value == "true"
        elif key in {"maxAttempts", "maxParallelSteps"} and level in {"document", "step"}:
            if not re.fullmatch(r"[1-9][0-9]*", raw_value):
                raise RunbookError(f"{key} must be a positive integer in {path}:{line_number}")
            properties[key] = int(raw_value)
        else:
            properties[key] = parse_frontmatter_scalar(raw_value, path, line_number)
        index += 1
    return properties, index


def parse_multiline_description(lines: list[str], start_index: int, path: Path) -> tuple[str, int]:
    content: list[str] = []
    indentation: int | None = None
    index = start_index
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            content.append("")
            index += 1
            continue
        if line == line.lstrip():
            break
        if line.startswith("\t"):
            raise RunbookError(
                f"Multiline runbook description must use spaces in {path} at line {index + 2}"
            )
        current_indentation = len(line) - len(line.lstrip(" "))
        if indentation is None:
            indentation = current_indentation
        if current_indentation < indentation:
            raise RunbookError(
                f"Inconsistent multiline runbook description indentation in {path} "
                f"at line {index + 2}"
            )
        content.append(line[indentation:])
        index += 1
    if indentation is None:
        return "", index
    return "\n".join(content).strip(), index


def parse_frontmatter_scalar(raw_value: str, path: Path, line_number: int) -> str:
    if not raw_value:
        return ""
    if raw_value.startswith('"'):
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise RunbookError(
                f"Invalid double-quoted runbook frontmatter value in {path} "
                f"at line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(value, str):
            raise RunbookError(
                f"Runbook frontmatter values must be strings in {path} at line {line_number}"
            )
        return value
    if raw_value.startswith("'"):
        if len(raw_value) < 2 or not raw_value.endswith("'"):
            raise RunbookError(
                f"Unterminated single-quoted runbook frontmatter value in {path} "
                f"at line {line_number}"
            )
        inner = raw_value[1:-1]
        value: list[str] = []
        index = 0
        while index < len(inner):
            if inner[index] != "'":
                value.append(inner[index])
                index += 1
                continue
            if index + 1 >= len(inner) or inner[index + 1] != "'":
                raise RunbookError(
                    f"Single quotes inside a runbook frontmatter value must be doubled "
                    f"in {path} at line {line_number}"
                )
            value.append("'")
            index += 2
        return "".join(value)
    if raw_value.startswith(UNSUPPORTED_FRONTMATTER_VALUE_PREFIXES):
        raise RunbookError(
            f"Runbook frontmatter supports only scalar string values in {path} "
            f"at line {line_number}"
        )
    return raw_value


def document_title(path: Path) -> str:
    for line in read_runbook_text(path).splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            if title:
                return title
    return path.stem.replace("-", " ").replace("_", " ").title()
