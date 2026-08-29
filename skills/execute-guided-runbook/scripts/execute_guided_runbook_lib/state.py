"""Session ledger validation and lifecycle commands."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .model import (
    EFFORT_LEVELS,
    MODEL_TIERS,
    OperatorDecisionRequired,
    RunbookError,
)
from .storage import (
    content_hash,
    fixed_local_path,
    print_json,
    read_json,
    require_nonempty,
    session_path,
    state_payload,
    utc_now,
    write_state,
)


def session_status(state: dict[str, Any]) -> str:
    status = state.get("status")
    if not isinstance(status, str) or status not in {
        "active",
        "completed",
        "ignored",
    }:
        raise RunbookError(f"Unsupported runbook session status: {status}")
    return status


def new_state(runbook: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    return {
        "schemaVersion": 1,
        "runbookId": runbook["id"],
        "runbookTitle": runbook["title"],
        "runbookPath": runbook["relativePath"],
        "runbookSha256": content_hash(Path(runbook["path"])),
        "registered": runbook["registered"],
        "effortLevel": runbook["effortLevel"],
        "modelTier": runbook["modelTier"],
        "startedAt": now,
        "updatedAt": now,
        "status": "active",
        "currentStep": None,
        "history": [],
        "revisionDecisions": [],
    }


def sync_state_metadata(state: dict[str, Any], runbook: dict[str, Any]) -> bool:
    expected = {
        "runbookTitle": runbook["title"],
        "runbookPath": runbook["relativePath"],
        "registered": runbook["registered"],
        "effortLevel": runbook["effortLevel"],
        "modelTier": runbook["modelTier"],
    }
    changed = any(state.get(key) != value for key, value in expected.items())
    if changed:
        state.update(expected)
    return changed


def require_state_string(state: dict[str, Any], key: str, path: Path) -> str:
    value = state.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RunbookError(f"Session field {key} must be a non-empty string in {path}")
    return value


def validate_history_record(record: Any, index: int, path: Path) -> None:
    if not isinstance(record, dict):
        raise RunbookError(f"Session history item {index} must be an object in {path}")
    require_state_string(record, "id", path)
    require_state_string(record, "title", path)
    require_state_string(record, "startedAt", path)
    status = record.get("status")
    if status == "completed":
        require_state_string(record, "evidence", path)
        require_state_string(record, "completedAt", path)
    elif status == "skipped":
        require_state_string(record, "skipReason", path)
        require_state_string(record, "skippedAt", path)
    else:
        raise RunbookError(f"Unsupported session history status at item {index}: {status}")


def validate_current_step(current: Any, path: Path) -> None:
    if current is None:
        return
    if not isinstance(current, dict):
        raise RunbookError(f"Session currentStep must be an object or null in {path}")
    require_state_string(current, "id", path)
    require_state_string(current, "title", path)
    require_state_string(current, "startedAt", path)
    status = current.get("status")
    if not isinstance(status, str) or status not in {"pending", "blocked"}:
        raise RunbookError(f"Unsupported current step status in {path}: {status}")
    if status == "blocked":
        require_state_string(current, "blockReason", path)
        require_state_string(current, "blockedAt", path)


def validate_state(state: dict[str, Any], runbook: dict[str, Any], path: Path) -> None:
    version = state.get("schemaVersion")
    if type(version) is not int or version != 1:
        raise RunbookError(f"Unsupported session schema in {path}")

    runbook_id = require_state_string(state, "runbookId", path)
    if runbook_id != runbook["id"]:
        raise RunbookError(
            f"Session runbookId {runbook_id!r} does not match {runbook['id']!r} in {path}"
        )
    for key in ("runbookTitle", "runbookPath", "startedAt", "updatedAt"):
        require_state_string(state, key, path)

    digest = require_state_string(state, "runbookSha256", path)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RunbookError(f"Session runbookSha256 is invalid in {path}")
    if type(state.get("registered")) is not bool:
        raise RunbookError(f"Session field registered must be a boolean in {path}")
    effort_level = state.get("effortLevel")
    if not isinstance(effort_level, str) or effort_level not in EFFORT_LEVELS:
        raise RunbookError(f"Session effortLevel is invalid in {path}")
    model_tier = state.get("modelTier")
    if not isinstance(model_tier, str) or model_tier not in MODEL_TIERS:
        raise RunbookError(f"Session modelTier is invalid in {path}")

    status = session_status(state)
    if "currentStep" not in state:
        raise RunbookError(f"Session field currentStep is required in {path}")
    current = state["currentStep"]
    validate_current_step(current, path)
    if status == "completed" and current is not None:
        raise RunbookError(f"Completed session cannot have a currentStep in {path}")

    history = state.get("history")
    if not isinstance(history, list):
        raise RunbookError(f"Session history must be a list in {path}")
    for index, record in enumerate(history):
        validate_history_record(record, index, path)

    decisions = state.get("revisionDecisions")
    if not isinstance(decisions, list) or not all(isinstance(item, dict) for item in decisions):
        raise RunbookError(f"Session revisionDecisions must be a list of objects in {path}")
    if status == "completed":
        require_state_string(state, "completedAt", path)
        require_state_string(state, "completionEvidence", path)


def read_state(path: Path, runbook: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        raise RunbookError(
            "No session exists for "
            f"{runbook['id']}; invoke runbook_session.py run {runbook['id']} first"
        )
    state = read_json(path)
    validate_state(state, runbook, path)
    return state


def outdated_session_payload(
    path: Path,
    state: dict[str, Any],
    runbook: dict[str, Any],
) -> dict[str, Any]:
    current_hash = content_hash(Path(runbook["path"]))
    prev_hash = state["runbookSha256"]
    curr_step = state["currentStep"]
    return {
        "status": "operator_decision_required",
        "reason": "unfinished_runbook_changed",
        "operatorChoiceRequired": True,
        "operatorPrompt": (
            "This runbook changed while its previous session is unfinished. "
            "Choose continue to keep the saved progress and apply it to the "
            "new revision, or choose ignore to archive the unfinished session "
            "and start over from the first step."
        ),
        "runbookId": runbook["id"],
        "runbookPath": runbook["relativePath"],
        "previousSha256": prev_hash,
        "currentSha256": current_hash,
        "currentStep": curr_step,
        "completedSteps": [
            item["id"] for item in state.get("history", []) if item.get("status") == "completed"
        ],
        "statePath": str(path),
    }


def archive_state(
    path: Path,
    state: dict[str, Any],
    decision: str,
) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    archive_dir = fixed_local_path(
        path.parent,
        "archive",
        subject="Session archive directory",
    )
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RunbookError(f"Could not create session archive directory: {archive_dir}") from exc
    archive_path = archive_dir / f"{path.stem}.{timestamp}.{uuid.uuid4().hex}.json"
    state["archivedAt"] = utc_now()
    state["archiveReason"] = decision
    if decision == "ignored":
        state["status"] = "ignored"
    write_state(archive_path, state)
    return archive_path


def load_state(
    path: Path,
    runbook: dict[str, Any],
    allow_completed_outdated: bool = False,
) -> dict[str, Any]:
    state = read_state(path, runbook)
    expected_hash = content_hash(Path(runbook["path"]))
    current_stored_hash = state["runbookSha256"]
    if current_stored_hash != expected_hash:
        if session_status(state) == "completed":
            if allow_completed_outdated:
                return {
                    **state,
                    "outdated": True,
                    "outdatedReason": "completed_runbook_changed",
                    "currentRunbookSha256": expected_hash,
                    "currentRunbookTitle": runbook["title"],
                    "currentRunbookPath": runbook["relativePath"],
                }
            raise RunbookError(
                f"Runbook session is complete for an older revision: {runbook['id']}; "
                f"invoke runbook_session.py run {runbook['id']} to start the current revision"
            )
        raise OperatorDecisionRequired(outdated_session_payload(path, state, runbook))
    if sync_state_metadata(state, runbook):
        state["updatedAt"] = utc_now()
        write_state(path, state)
    return state


def require_active_state(state: dict[str, Any], runbook_id: str) -> None:
    status = session_status(state)
    if status == "completed":
        raise RunbookError(
            f"Runbook session is already complete: {runbook_id}; use --restart to run again"
        )
    if status == "ignored":
        raise RunbookError(
            f"Runbook session was ignored: {runbook_id}; use run {runbook_id} to start over"
        )


def command_start(
    runbook: dict[str, Any],
    repo_root: Path,
    restart: bool,
    continue_outdated: bool,
    ignore_outdated: bool,
) -> None:
    path = session_path(repo_root, runbook["id"])
    if not path.exists():
        if restart or continue_outdated or ignore_outdated:
            raise RunbookError(f"No existing session can use the requested option: {runbook['id']}")
        state = new_state(runbook)
        write_state(path, state)
        print_json(state_payload(path, state))
        return

    previous = read_state(path, runbook)
    expected_hash = content_hash(Path(runbook["path"]))
    curr_stored_hash = previous["runbookSha256"]
    outdated = curr_stored_hash != expected_hash

    if continue_outdated:
        if not outdated or session_status(previous) != "active":
            raise RunbookError(
                "--continue requires an unfinished session from an older runbook revision"
            )
        now = utc_now()
        previous.setdefault("revisionDecisions", []).append(
            {
                "decision": "continue",
                "previousSha256": curr_stored_hash,
                "newSha256": expected_hash,
                "decidedAt": now,
            }
        )
        previous["runbookSha256"] = expected_hash
        sync_state_metadata(previous, runbook)
        previous["updatedAt"] = now
        write_state(path, previous)
        print_json(state_payload(path, previous))
        return

    if ignore_outdated:
        if not outdated or session_status(previous) != "active":
            raise RunbookError(
                "--ignore requires an unfinished session from an older runbook revision"
            )
        archive_path = archive_state(path, previous, decision="ignored")
        state = new_state(runbook)
        state["previousSession"] = {
            "decision": "ignored",
            "archivePath": str(archive_path),
        }
        write_state(path, state)
        print_json(state_payload(path, state))
        return

    if restart:
        if outdated:
            raise RunbookError(
                "--restart requires the same runbook revision; rerun without --restart "
                "to handle a changed runbook"
            )
        archive_path = archive_state(path, previous, decision="restarted")
        state = new_state(runbook)
        state["previousSession"] = {
            "decision": "restarted",
            "archivePath": str(archive_path),
        }
        write_state(path, state)
        print_json(state_payload(path, state))
        return

    if outdated and session_status(previous) == "active":
        raise OperatorDecisionRequired(outdated_session_payload(path, previous, runbook))
    if outdated:
        archive_path = archive_state(
            path,
            previous,
            decision="completed-revision",
        )
        state = new_state(runbook)
        state["previousSession"] = {
            "decision": "completed-revision",
            "archivePath": str(archive_path),
        }
        write_state(path, state)
    else:
        state = previous
        if sync_state_metadata(state, runbook):
            state["updatedAt"] = utc_now()
            write_state(path, state)
    print_json(state_payload(path, state))


def command_step(runbook: dict[str, Any], repo_root: Path, step_id: str, title: str) -> None:
    step_id = require_nonempty(step_id, "Step id")
    title = require_nonempty(title, "Step title")
    path = session_path(repo_root, runbook["id"])
    state = load_state(path, runbook)
    require_active_state(state, runbook["id"])
    current = state["currentStep"]
    if current:
        if current.get("id") == step_id and current.get("title") == title:
            print_json(state_payload(path, state))
            return
        raise RunbookError(
            f"Current step {current.get('id')} is unresolved; complete, block, or skip it first"
        )
    if any(item.get("id") == step_id for item in state.get("history", [])):
        raise RunbookError(f"Step already exists in session history: {step_id}")
    now = utc_now()
    state["currentStep"] = {
        "id": step_id,
        "title": title,
        "status": "pending",
        "startedAt": now,
    }
    state["updatedAt"] = now
    write_state(path, state)
    print_json(state_payload(path, state))


def command_complete(runbook: dict[str, Any], repo_root: Path, evidence: str) -> None:
    evidence = require_nonempty(evidence, "Completion evidence")
    path = session_path(repo_root, runbook["id"])
    state = load_state(path, runbook)
    require_active_state(state, runbook["id"])
    current = state["currentStep"]
    if not current:
        raise RunbookError("No unresolved step is active; set a step first")
    now = utc_now()
    record = {
        "id": current["id"],
        "title": current["title"],
        "status": "completed",
        "evidence": evidence,
        "startedAt": current["startedAt"],
        "completedAt": now,
    }
    state.setdefault("history", []).append(record)
    state["currentStep"] = None
    state["updatedAt"] = now
    write_state(path, state)
    print_json(state_payload(path, state))


def command_block(runbook: dict[str, Any], repo_root: Path, reason: str) -> None:
    reason = require_nonempty(reason, "Block reason")
    path = session_path(repo_root, runbook["id"])
    state = load_state(path, runbook)
    require_active_state(state, runbook["id"])
    current = state["currentStep"]
    if not current:
        raise RunbookError("No unresolved step is active; set a step first")
    now = utc_now()
    current["status"] = "blocked"
    current["blockReason"] = reason
    current["blockedAt"] = now
    state["updatedAt"] = now
    write_state(path, state)
    print_json(state_payload(path, state))


def command_skip(runbook: dict[str, Any], repo_root: Path, reason: str) -> None:
    reason = require_nonempty(reason, "Skip reason")
    path = session_path(repo_root, runbook["id"])
    state = load_state(path, runbook)
    require_active_state(state, runbook["id"])
    current = state["currentStep"]
    if not current:
        raise RunbookError("No unresolved step is active; set a step first")
    now = utc_now()
    record = {
        "id": current["id"],
        "title": current["title"],
        "status": "skipped",
        "skipReason": reason,
        "startedAt": current["startedAt"],
        "skippedAt": now,
    }
    state.setdefault("history", []).append(record)
    state["currentStep"] = None
    state["updatedAt"] = now
    write_state(path, state)
    print_json(state_payload(path, state))


def command_finish(runbook: dict[str, Any], repo_root: Path, evidence: str) -> None:
    evidence = require_nonempty(evidence, "Completion evidence")
    path = session_path(repo_root, runbook["id"])
    state = load_state(path, runbook)
    require_active_state(state, runbook["id"])
    current = state["currentStep"]
    if current:
        raise RunbookError(f"Cannot finish while step {current.get('id')} is unresolved")
    now = utc_now()
    state["status"] = "completed"
    state["completedAt"] = now
    state["completionEvidence"] = evidence
    state["updatedAt"] = now
    write_state(path, state)
    print_json(state_payload(path, state))
