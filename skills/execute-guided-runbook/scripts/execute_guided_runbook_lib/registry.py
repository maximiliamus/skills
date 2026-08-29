"""Runbook registry loading, resolution, and mutation commands."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .model import (
    DEFAULT_EFFORT_LEVEL,
    DEFAULT_MODEL_TIER,
    EFFORT_LEVELS,
    GENERATED_PATH_ID_PATTERN,
    ID_PATTERN,
    MODEL_TIERS,
    REGISTRY_FIELDS,
    RunbookError,
)
from .state import read_state, sync_state_metadata
from .storage import (
    inside_repo,
    path_runbook_id,
    print_json,
    read_json,
    read_runbook_text,
    require_nonempty,
    session_locks,
    utc_now,
    write_json_atomic,
    write_state,
)


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
        raise RunbookError(
            f"Runbook id uses the reserved path-session namespace: {runbook_id}"
        )


def load_registry(path: Path, repo_root: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise RunbookError(f"Runbook registry must be a regular file: {path}")
    data = read_json(path)
    unknown_top_level = set(data) - {"schemaVersion", "runbooks"}
    if unknown_top_level:
        fields = ", ".join(sorted(unknown_top_level))
        raise RunbookError(f"Unknown registry field(s) in {path}: {fields}")
    version = data.get("schemaVersion")
    if type(version) is not int or version != 1:
        raise RunbookError(f"Unsupported runbook registry schema in {path}")

    entries = data.get("runbooks")
    if not isinstance(entries, list):
        raise RunbookError(f"Registry runbooks must be a list in {path}")

    registry: dict[str, dict[str, str]] = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise RunbookError(f"Every registry entry must be an object in {path}")
        unknown_entry_fields = set(raw_entry) - REGISTRY_FIELDS
        if unknown_entry_fields:
            fields = ", ".join(sorted(unknown_entry_fields))
            raise RunbookError(f"Unknown runbook field(s) in {path}: {fields}")
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
            "effortLevel": raw_entry.get("effortLevel"),
            "modelTier": raw_entry.get("modelTier"),
        }
        for field, value in optional_values.items():
            if field in raw_entry and (not isinstance(value, str) or not value.strip()):
                raise RunbookError(
                    f"Registry field {field} must be a non-empty string: {raw_entry}"
                )
        resolved_title = (optional_values["title"] or derived_title).strip()
        effort_level = optional_values["effortLevel"] or DEFAULT_EFFORT_LEVEL
        model_tier = optional_values["modelTier"] or DEFAULT_MODEL_TIER
        entry = {
            "id": runbook_id,
            "title": resolved_title,
            "path": portable_path,
            "description": (
                optional_values["description"] or f"Repository runbook: {resolved_title}."
            ).strip(),
            "effortLevel": effort_level,
            "modelTier": model_tier,
        }
        runbook_id = entry["id"]
        validate_registered_id(runbook_id)
        if runbook_id in registry:
            raise RunbookError(f"Duplicate runbook id: {runbook_id}")
        if entry["effortLevel"] not in EFFORT_LEVELS:
            raise RunbookError(f"Invalid effort level for {runbook_id}: {entry['effortLevel']}")
        if entry["modelTier"] not in MODEL_TIERS:
            raise RunbookError(f"Invalid model tier for {runbook_id}: {entry['modelTier']}")
        registry[runbook_id] = entry  # type: ignore[assignment]
    return registry


def write_registry(path: Path, registry: dict[str, dict[str, str]]) -> None:
    payload = {
        "schemaVersion": 1,
        "runbooks": [registry[runbook_id] for runbook_id in sorted(registry)],
    }
    write_json_atomic(path, payload, default_mode=0o666)


def resolve_runbook(
    selector: str,
    repo_root: Path,
    registry: dict[str, dict[str, str]],
    registry_enabled: bool,
) -> dict[str, Any]:
    if selector in registry:
        entry = registry[selector]
        runbook_path = inside_repo(repo_root, repo_root / entry["path"])
        result: dict[str, Any] = {
            "id": entry["id"],
            "title": entry["title"],
            "path": str(runbook_path),
            "relativePath": runbook_path.relative_to(repo_root.resolve()).as_posix(),
            "description": entry["description"],
            "effortLevel": entry["effortLevel"],
            "modelTier": entry["modelTier"],
            "registered": True,
        }
    elif registry_enabled:
        raise RunbookError(f"Runbook id is not registered: {selector}")
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
            "effortLevel": DEFAULT_EFFORT_LEVEL,
            "modelTier": DEFAULT_MODEL_TIER,
            "registered": False,
        }

    if runbook_path.suffix.lower() != ".md":
        raise RunbookError(f"Runbook must be a Markdown file: {runbook_path}")
    if not runbook_path.is_file():
        raise RunbookError(f"Runbook does not exist: {runbook_path}")
    read_runbook_text(runbook_path)
    return result


def resolve_runbook_path(raw_path: str, repo_root: Path) -> tuple[Path, str]:
    candidate_path = Path(raw_path)
    candidate = candidate_path if candidate_path.is_absolute() else repo_root / candidate_path
    runbook_path = inside_repo(repo_root, candidate)
    if runbook_path.suffix.lower() != ".md":
        raise RunbookError(f"Runbook must be a Markdown file: {runbook_path}")
    if not runbook_path.is_file():
        raise RunbookError(f"Runbook does not exist: {runbook_path}")
    read_runbook_text(runbook_path)
    relative_path = runbook_path.relative_to(repo_root.resolve()).as_posix()
    portable_path, _, normalized_path = normalize_registry_path(relative_path, repo_root)
    if normalized_path != runbook_path:
        raise RunbookError(f"Runbook path cannot be stored portably: {relative_path!r}")
    return runbook_path, portable_path


def document_title(path: Path) -> str:
    for line in read_runbook_text(path).splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            if title:
                return title
    return path.stem.replace("-", " ").replace("_", " ").title()


def command_list(registry: dict[str, dict[str, str]]) -> None:
    print_json(
        {
            "schemaVersion": 1,
            "runbooks": [registry[runbook_id] for runbook_id in sorted(registry)],
        }
    )


def command_register(
    registry_path: Path,
    registry: dict[str, dict[str, str]],
    repo_root: Path,
    runbook_id: str,
    raw_path: str,
    title: str | None,
    description: str | None,
    effort_level: str | None,
    model_tier: str | None,
) -> None:
    validate_registered_id(runbook_id)
    runbook_path, relative_path = resolve_runbook_path(raw_path, repo_root)
    existing = registry.get(runbook_id)
    if title is not None:
        resolved_title = require_nonempty(title, "Runbook title")
    elif existing is not None:
        resolved_title = existing["title"]
    else:
        resolved_title = document_title(runbook_path)

    if description is not None:
        resolved_description = require_nonempty(description, "Runbook description")
    elif existing is not None:
        resolved_description = existing["description"]
    else:
        resolved_description = f"Repository runbook: {resolved_title}."

    resolved_effort_level = (
        effort_level
        if effort_level is not None
        else existing["effortLevel"]
        if existing is not None
        else DEFAULT_EFFORT_LEVEL
    )
    resolved_model_tier = (
        model_tier
        if model_tier is not None
        else existing["modelTier"]
        if existing is not None
        else DEFAULT_MODEL_TIER
    )
    entry = {
        "id": runbook_id,
        "title": resolved_title,
        "path": relative_path,
        "description": resolved_description,
        "effortLevel": resolved_effort_level,
        "modelTier": resolved_model_tier,
    }
    source_runbook_id = path_runbook_id(relative_path)
    migration: dict[str, Any] | None = None
    created_target_state = False

    with session_locks(repo_root, [source_runbook_id, runbook_id]) as state_paths:
        source_state_path = state_paths[source_runbook_id]
        target_state_path = state_paths[runbook_id]

        if source_state_path != target_state_path and source_state_path.exists():
            if target_state_path.exists():
                raise RunbookError(
                    "Cannot register runbook while both its path-based and registered "
                    f"sessions exist: {source_state_path} and {target_state_path}"
                )
            state = read_state(source_state_path, {"id": source_runbook_id})
            state["runbookId"] = runbook_id
            sync_state_metadata(
                state,
                {
                    "title": entry["title"],
                    "relativePath": entry["path"],
                    "registered": True,
                    "effortLevel": entry["effortLevel"],
                    "modelTier": entry["modelTier"],
                },
            )
            state["updatedAt"] = utc_now()
            write_state(target_state_path, state)
            created_target_state = True
            migration = {
                "fromRunbookId": source_runbook_id,
                "toRunbookId": runbook_id,
                "fromStatePath": str(source_state_path),
                "toStatePath": str(target_state_path),
            }

        registry[runbook_id] = entry
        try:
            write_registry(registry_path, registry)
        except RunbookError:
            if created_target_state:
                try:
                    target_state_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

        if migration is not None:
            try:
                source_state_path.unlink()
            except OSError as exc:
                migration["sourceStateRetained"] = True
                migration["cleanupWarning"] = str(exc)
            else:
                migration["sourceStateRetained"] = False

    print_json(
        {
            "registered": registry[runbook_id],
            "registryPath": str(registry_path),
            "sessionMigration": migration,
        }
    )


def command_unregister(
    registry_path: Path,
    registry: dict[str, dict[str, str]],
    runbook_id: str,
) -> None:
    if runbook_id not in registry:
        raise RunbookError(f"Runbook id is not registered: {runbook_id}")
    removed = registry.pop(runbook_id)
    write_registry(registry_path, registry)
    print_json(
        {
            "unregistered": removed,
            "registryPath": str(registry_path),
            "runbookDeleted": False,
            "sessionDeleted": False,
        }
    )
