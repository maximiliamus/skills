"""Session ledger validation and lifecycle commands."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .model import (
    ACCEPTANCE_POLICIES,
    ASSESSMENT_RESULTS,
    ASSESSMENT_VERSION,
    DEFAULT_ACCEPTANCE_POLICY,
    DEFAULT_STEP_ORDER,
    EFFORT_LEVELS,
    MODEL_TIERS,
    OPERATOR_DECISIONS,
    STEP_ORDERS,
    STEP_RESULTS,
    OperatorDecisionRequired,
    RunbookError,
    valid_acceptance_threshold,
)
from .storage import (
    content_hash,
    fixed_local_path,
    legacy_session_path,
    path_runbook_id,
    print_json,
    read_json,
    replace_with_retry,
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
        "assessmentVersion": ASSESSMENT_VERSION,
        "runbookId": runbook["id"],
        "runbookTitle": runbook["title"],
        "runbookPath": runbook["relativePath"],
        "runbookSha256": content_hash(Path(runbook["path"])),
        "registered": runbook["registered"],
        "effortLevel": runbook["effortLevel"],
        "modelTier": runbook["modelTier"],
        "acceptancePolicy": runbook.get(
            "acceptancePolicy", DEFAULT_ACCEPTANCE_POLICY
        ),
        "stepOrder": runbook.get("stepOrder", DEFAULT_STEP_ORDER),
        "acceptanceThreshold": runbook.get("acceptanceThreshold"),
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
        "acceptancePolicy": runbook.get(
            "acceptancePolicy", DEFAULT_ACCEPTANCE_POLICY
        ),
        "stepOrder": runbook.get("stepOrder", DEFAULT_STEP_ORDER),
        "acceptanceThreshold": runbook.get("acceptanceThreshold"),
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


def validate_score(score: Any, subject: str, path: Path) -> None:
    if not isinstance(score, dict):
        raise RunbookError(f"{subject} score must be an object in {path}")
    earned = score.get("earned")
    available = score.get("available")
    percent = score.get("percent")
    display = score.get("display")
    if type(earned) is not int or type(available) is not int:
        raise RunbookError(f"{subject} score values must be integers in {path}")
    if available < 1 or earned < 0 or earned > available:
        raise RunbookError(f"{subject} score range is invalid in {path}")
    expected_percent = rounded_percent(earned, available)
    if type(percent) is not int or percent != expected_percent:
        raise RunbookError(f"{subject} score percent is invalid in {path}")
    if display != score_display(earned, available):
        raise RunbookError(f"{subject} score display is invalid in {path}")


def validate_history_record(
    record: Any,
    index: int,
    path: Path,
    *,
    structured: bool,
) -> None:
    if not isinstance(record, dict):
        raise RunbookError(f"Session history item {index} must be an object in {path}")
    require_state_string(record, "id", path)
    require_state_string(record, "title", path)
    require_state_string(record, "startedAt", path)
    attempt = record.get("attempt", 1)
    if type(attempt) is not int or attempt < 1:
        raise RunbookError(f"Session history attempt is invalid at item {index} in {path}")
    status = record.get("status")
    if status == "completed":
        require_state_string(record, "evidence", path)
        require_state_string(record, "completedAt", path)
    elif status == "skipped":
        require_state_string(record, "skipReason", path)
        require_state_string(record, "skippedAt", path)
    else:
        raise RunbookError(f"Unsupported session history status at item {index}: {status}")
    if structured:
        step_result = record.get("stepResult")
        if not isinstance(step_result, str) or step_result not in STEP_RESULTS:
            raise RunbookError(
                f"Session history stepResult is invalid at item {index} in {path}"
            )
        validate_score(record.get("score"), f"Session history item {index}", path)


def validate_current_step(current: Any, path: Path) -> None:
    if current is None:
        return
    if not isinstance(current, dict):
        raise RunbookError(f"Session currentStep must be an object or null in {path}")
    require_state_string(current, "id", path)
    require_state_string(current, "title", path)
    require_state_string(current, "startedAt", path)
    attempt = current.get("attempt", 1)
    if type(attempt) is not int or attempt < 1:
        raise RunbookError(f"Session currentStep attempt is invalid in {path}")
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
    assessment_version = state.get("assessmentVersion")
    if assessment_version is not None and assessment_version != ASSESSMENT_VERSION:
        raise RunbookError(f"Unsupported session assessment schema in {path}")
    structured = assessment_version == ASSESSMENT_VERSION

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
    acceptance_policy = state.get("acceptancePolicy", DEFAULT_ACCEPTANCE_POLICY)
    if (
        not isinstance(acceptance_policy, str)
        or acceptance_policy not in ACCEPTANCE_POLICIES
    ):
        raise RunbookError(f"Session acceptancePolicy is invalid in {path}")
    step_order = state.get("stepOrder", DEFAULT_STEP_ORDER)
    if not isinstance(step_order, str) or step_order not in STEP_ORDERS:
        raise RunbookError(f"Session stepOrder is invalid in {path}")
    acceptance_threshold = state.get("acceptanceThreshold")
    if acceptance_threshold is not None and not valid_acceptance_threshold(
        acceptance_threshold
    ):
        raise RunbookError(f"Session acceptanceThreshold is invalid in {path}")
    if acceptance_policy != "flexible" and acceptance_threshold is not None:
        raise RunbookError(
            f"Session acceptanceThreshold requires flexible acceptancePolicy in {path}"
        )

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
        validate_history_record(record, index, path, structured=structured)

    decisions = state.get("revisionDecisions")
    if not isinstance(decisions, list) or not all(isinstance(item, dict) for item in decisions):
        raise RunbookError(f"Session revisionDecisions must be a list of objects in {path}")
    if status == "completed":
        require_state_string(state, "completedAt", path)
        require_state_string(state, "completionEvidence", path)
        if structured:
            result = state.get("result")
            if (
                not isinstance(result, str)
                or result not in ASSESSMENT_RESULTS - {"PARTIAL"}
            ):
                raise RunbookError(f"Completed session result is invalid in {path}")
            expected_steps = state.get("expectedSteps")
            if (
                not isinstance(expected_steps, list)
                or not expected_steps
                or not all(isinstance(item, str) and item.strip() for item in expected_steps)
                or len(set(expected_steps)) != len(expected_steps)
            ):
                raise RunbookError(f"Completed session expectedSteps are invalid in {path}")
            validate_score(state.get("score"), "Completed session", path)


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


def archived_sessions(
    path: Path,
    identity_stems: set[str],
) -> tuple[Path, list[tuple[datetime, Path]]]:
    archive_dir = fixed_local_path(
        path.parent,
        "archive",
        subject="Session archive directory",
    )
    if not archive_dir.exists():
        return archive_dir, []
    if not archive_dir.is_dir():
        raise RunbookError(f"Session archive path is not a directory: {archive_dir}")
    stem_pattern = "|".join(re.escape(stem) for stem in sorted(identity_stems))
    filename_pattern = re.compile(
        rf"(?:{stem_pattern})\."
        r"(?P<timestamp>[0-9]{8}T[0-9]{6}\.[0-9]{6}Z)\."
        r"[0-9a-f]{32}\.json"
    )
    entries: list[tuple[datetime, Path]] = []
    try:
        candidates = list(archive_dir.iterdir())
    except OSError as exc:
        raise RunbookError(f"Could not list session archives: {archive_dir}: {exc}") from exc
    for candidate in candidates:
        match = filename_pattern.fullmatch(candidate.name)
        if match is None:
            continue
        if candidate.is_symlink() or not candidate.is_file():
            raise RunbookError(f"Session archive is not a regular file: {candidate}")
        try:
            archived_at = datetime.strptime(
                match.group("timestamp"),
                "%Y%m%dT%H%M%S.%fZ",
            ).replace(tzinfo=UTC)
        except ValueError as exc:
            raise RunbookError(f"Session archive timestamp is invalid: {candidate}") from exc
        entries.append((archived_at, candidate))
    entries.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    return archive_dir, entries


def command_prune(
    runbook: dict[str, Any],
    repo_root: Path,
    keep_last: int | None,
    older_than_days: int | None,
    dry_run: bool,
) -> None:
    if (keep_last is None) == (older_than_days is None):
        raise RunbookError(
            "Choose exactly one archive policy: --keep-last or --older-than-days"
        )
    if keep_last is not None and keep_last < 0:
        raise RunbookError("--keep-last must be zero or greater")
    if older_than_days is not None and older_than_days < 1:
        raise RunbookError("--older-than-days must be one or greater")

    path = session_path(repo_root, runbook["id"])
    identity_ids = {runbook["id"], path_runbook_id(runbook["relativePath"])}
    identity_stems = {
        identity_path.stem
        for runbook_id in identity_ids
        for identity_path in (
            session_path(repo_root, runbook_id),
            legacy_session_path(repo_root, runbook_id),
        )
    }
    archive_dir, entries = archived_sessions(path, identity_stems)
    if keep_last is not None:
        selected = entries[keep_last:]
        policy: dict[str, int] = {"keepLast": keep_last}
    else:
        assert older_than_days is not None
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        selected = [entry for entry in entries if entry[0] < cutoff]
        policy = {"olderThanDays": older_than_days}

    deleted: list[Path] = []
    if not dry_run:
        for _, candidate in reversed(selected):
            try:
                candidate.unlink()
            except OSError as exc:
                raise RunbookError(f"Could not delete session archive: {candidate}: {exc}") from exc
            deleted.append(candidate)

    print_json(
        {
            "status": "preview" if dry_run else ("pruned" if deleted else "unchanged"),
            "runbookId": runbook["id"],
            "archiveDirectory": str(archive_dir),
            "identityStems": sorted(identity_stems),
            "policy": policy,
            "dryRun": dry_run,
            "archiveCountBefore": len(entries),
            "selectedCount": len(selected),
            "deletedCount": len(deleted),
            "retainedCount": len(entries) - len(deleted),
            "selectedArchives": [str(candidate) for _, candidate in selected],
        }
    )


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


def migrate_path_identity_session(
    runbook: dict[str, Any],
    legacy_runbook_id: str,
    state_paths: dict[str, Path],
) -> None:
    if legacy_runbook_id == runbook["id"]:
        return
    source_path = state_paths[legacy_runbook_id]
    target_path = state_paths[runbook["id"]]
    source_exists = source_path.exists()
    target_exists = target_path.exists()
    if source_exists and target_exists:
        raise RunbookError(
            "Cannot adopt the document runbook id while both path-based and "
            f"document-id sessions exist: {source_path} and {target_path}"
        )
    legacy_runbook = {**runbook, "id": legacy_runbook_id}
    renamed = False
    if source_exists:
        state = read_state(source_path, legacy_runbook)
        try:
            replace_with_retry(source_path, target_path)
        except OSError as exc:
            raise RunbookError(
                f"Could not atomically adopt the document runbook id: {source_path}"
            ) from exc
        renamed = True
    elif target_exists:
        raw_target = read_json(target_path)
        if raw_target.get("runbookId") != legacy_runbook_id:
            return
        state = read_state(target_path, legacy_runbook)
    else:
        return
    state["runbookId"] = runbook["id"]
    sync_state_metadata(state, runbook)
    state["updatedAt"] = utc_now()
    try:
        write_state(target_path, state)
    except RunbookError:
        if renamed:
            try:
                replace_with_retry(target_path, source_path)
            except OSError as rollback_exc:
                raise RunbookError(
                    "Could not finalize or roll back the document-id session "
                    f"migration: {source_path}; {target_path}"
                ) from rollback_exc
        raise


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


def command_step(
    runbook: dict[str, Any],
    repo_root: Path,
    step_id: str,
    title: str,
    retry: bool,
) -> None:
    step_id = require_nonempty(step_id, "Step id")
    title = require_nonempty(title, "Step title")
    path = session_path(repo_root, runbook["id"])
    state = load_state(path, runbook)
    require_active_state(state, runbook["id"])
    current = state["currentStep"]
    if current:
        if current.get("id") == step_id and current.get("title") == title:
            current_attempt = current.get("attempt", 1)
            if retry and current_attempt == 1:
                raise RunbookError(f"Cannot retry a step with no completed attempt: {step_id}")
            if retry == (current_attempt > 1):
                print_json(state_payload(path, state))
                return
            raise RunbookError(
                f"Step {step_id} is already active as attempt {current_attempt}"
            )
        raise RunbookError(
            f"Current step {current.get('id')} is unresolved; complete, block, or skip it first"
        )
    previous_attempts = [
        item for item in state.get("history", []) if item.get("id") == step_id
    ]
    if retry and not previous_attempts:
        raise RunbookError(f"Cannot retry a step with no completed attempt: {step_id}")
    if previous_attempts and not retry:
        raise RunbookError(f"Step already exists in session history: {step_id}")
    if state.get("assessmentVersion") == ASSESSMENT_VERSION:
        latest = latest_terminal_attempts(state.get("history", []))
        failed_steps = [
            item_id
            for item_id, item in latest.items()
            if item.get("stepResult") != "PASS"
        ]
        if (
            state["acceptancePolicy"] == "strict"
            and state["stepOrder"] == "sequential"
            and failed_steps
            and not (retry and step_id in failed_steps)
        ):
            failed = ", ".join(sorted(failed_steps))
            raise RunbookError(
                "Strict sequential execution cannot advance past a deficient step; "
                f"retry first: {failed}"
            )
    attempt = (
        max(
            item.get("attempt", 1)
            for item in previous_attempts
            if isinstance(item, dict)
        )
        + 1
        if retry
        else 1
    )
    now = utc_now()
    state["currentStep"] = {
        "id": step_id,
        "title": title,
        "status": "pending",
        "startedAt": now,
        "attempt": attempt,
    }
    state["updatedAt"] = now
    write_state(path, state)
    print_json(state_payload(path, state))


def rounded_percent(earned: int, available: int) -> int:
    return (earned * 200 + available) // (available * 2)


def score_display(earned: int, available: int) -> str:
    return f"{earned}/{available} ({rounded_percent(earned, available)}%)"


def parse_score(raw_score: str | None, *, required: bool) -> dict[str, Any] | None:
    if raw_score is None:
        if required:
            raise RunbookError("A score in earned/available form is required")
        return None
    match = re.fullmatch(r"([0-9]+)/([1-9][0-9]*)", raw_score.strip())
    if match is None:
        raise RunbookError("Score must use earned/available integers, for example 9/10")
    earned, available = (int(value) for value in match.groups())
    if earned > available:
        raise RunbookError("Score earned points cannot exceed available points")
    return {
        "earned": earned,
        "available": available,
        "percent": rounded_percent(earned, available),
        "display": score_display(earned, available),
    }


def structured_step_assessment(
    state: dict[str, Any],
    raw_result: str | None,
    raw_score: str | None,
) -> tuple[str, dict[str, Any]]:
    if raw_result is None:
        raise RunbookError("A step result of pass or fail is required")
    step_result = raw_result.upper()
    if step_result not in {"PASS", "FAIL"}:
        raise RunbookError("Step result must be pass or fail")
    if state["acceptancePolicy"] == "strict":
        if raw_score is not None:
            raise RunbookError("Strict acceptance derives its score from pass/fail results")
        score = parse_score("1/1" if step_result == "PASS" else "0/1", required=True)
    else:
        score = parse_score(raw_score, required=True)
    assert score is not None
    if step_result == "PASS" and score["earned"] != score["available"]:
        raise RunbookError("A passing step must earn every available point")
    if step_result == "FAIL" and score["earned"] == score["available"]:
        raise RunbookError("A failed step cannot earn every available point")
    return step_result, score


def command_complete(
    runbook: dict[str, Any],
    repo_root: Path,
    evidence: str,
    raw_result: str | None,
    raw_score: str | None,
) -> None:
    evidence = require_nonempty(evidence, "Completion evidence")
    path = session_path(repo_root, runbook["id"])
    state = load_state(path, runbook)
    require_active_state(state, runbook["id"])
    current = state["currentStep"]
    if not current:
        raise RunbookError("No unresolved step is active; set a step first")
    assessment: dict[str, Any] = {}
    if state.get("assessmentVersion") == ASSESSMENT_VERSION:
        step_result, score = structured_step_assessment(state, raw_result, raw_score)
        assessment = {"stepResult": step_result, "score": score}
    now = utc_now()
    record = {
        "id": current["id"],
        "title": current["title"],
        "status": "completed",
        "evidence": evidence,
        "startedAt": current["startedAt"],
        "attempt": current.get("attempt", 1),
        "completedAt": now,
        **assessment,
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


def command_skip(
    runbook: dict[str, Any],
    repo_root: Path,
    reason: str,
    raw_score: str | None,
) -> None:
    reason = require_nonempty(reason, "Skip reason")
    path = session_path(repo_root, runbook["id"])
    state = load_state(path, runbook)
    require_active_state(state, runbook["id"])
    current = state["currentStep"]
    if not current:
        raise RunbookError("No unresolved step is active; set a step first")
    assessment: dict[str, Any] = {}
    if state.get("assessmentVersion") == ASSESSMENT_VERSION:
        if state["acceptancePolicy"] == "strict":
            if raw_score is not None:
                raise RunbookError("Strict acceptance derives a skipped step score automatically")
            score = parse_score("0/1", required=True)
        else:
            score = parse_score(raw_score, required=True)
            assert score is not None
            if score["earned"] != 0:
                raise RunbookError("A skipped step must earn zero points")
        assessment = {"stepResult": "SKIPPED", "score": score}
    now = utc_now()
    record = {
        "id": current["id"],
        "title": current["title"],
        "status": "skipped",
        "skipReason": reason,
        "startedAt": current["startedAt"],
        "attempt": current.get("attempt", 1),
        "skippedAt": now,
        **assessment,
    }
    state.setdefault("history", []).append(record)
    state["currentStep"] = None
    state["updatedAt"] = now
    write_state(path, state)
    print_json(state_payload(path, state))


def latest_terminal_attempts(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in history:
        previous = latest.get(record["id"])
        if previous is None or record.get("attempt", 1) > previous.get("attempt", 1):
            latest[record["id"]] = record
    return latest


def normalize_expected_steps(expected_steps: list[str] | None) -> list[str]:
    if not expected_steps:
        raise RunbookError("At least one --expected-step is required")
    normalized = [require_nonempty(item, "Expected step id") for item in expected_steps]
    if len(set(normalized)) != len(normalized):
        raise RunbookError("Expected step ids must be unique")
    return normalized


def aggregate_score(records: list[dict[str, Any]]) -> dict[str, Any]:
    earned = sum(record["score"]["earned"] for record in records)
    available = sum(record["score"]["available"] for record in records)
    return {
        "earned": earned,
        "available": available,
        "percent": rounded_percent(earned, available),
        "display": score_display(earned, available),
    }


def assess_result(
    state: dict[str, Any],
    records: list[dict[str, Any]],
    score: dict[str, Any],
    decision: str | None,
) -> str:
    policy = state["acceptancePolicy"]
    if decision is not None and decision not in OPERATOR_DECISIONS:
        raise RunbookError("Operator decision must be accept or reject")
    if policy == "strict":
        if decision is not None:
            raise RunbookError("Strict acceptance does not permit an operator override")
        return (
            "PASSED"
            if all(record["stepResult"] == "PASS" for record in records)
            else "REJECTED"
        )
    if policy == "always":
        if decision is not None:
            raise RunbookError("Always acceptance does not require an operator decision")
        return "PASSED" if score["percent"] == 100 else "ACCEPTED"
    if score["percent"] == 100:
        if decision is not None:
            raise RunbookError("A fully passing score does not require an operator decision")
        return "PASSED"
    threshold = state.get("acceptanceThreshold")
    if threshold is not None and score["percent"] >= int(threshold[:-1]):
        if decision is not None:
            raise RunbookError("The automatic acceptance threshold already accepts this result")
        return "ACCEPTED"
    if decision == "accept":
        return "ACCEPTED"
    if decision == "reject":
        return "REJECTED"
    return "PARTIAL"


def command_finish(
    runbook: dict[str, Any],
    repo_root: Path,
    evidence: str,
    expected_steps: list[str] | None,
    decision: str | None,
) -> None:
    evidence = require_nonempty(evidence, "Completion evidence")
    path = session_path(repo_root, runbook["id"])
    state = load_state(path, runbook)
    require_active_state(state, runbook["id"])
    current = state["currentStep"]
    if current:
        raise RunbookError(f"Cannot finish while step {current.get('id')} is unresolved")
    if state.get("assessmentVersion") != ASSESSMENT_VERSION:
        now = utc_now()
        state["status"] = "completed"
        state["completedAt"] = now
        state["completionEvidence"] = evidence
        state["updatedAt"] = now
        write_state(path, state)
        print_json(state_payload(path, state))
        return

    normalized_steps = normalize_expected_steps(expected_steps)
    latest = latest_terminal_attempts(state.get("history", []))
    missing = [step_id for step_id in normalized_steps if step_id not in latest]
    if missing:
        raise RunbookError(
            "Every expected step must have a terminal assessment; missing: "
            + ", ".join(missing)
        )
    unexpected = sorted(set(latest) - set(normalized_steps))
    if unexpected:
        raise RunbookError(
            "Every assessed step must be declared with --expected-step; unexpected: "
            + ", ".join(unexpected)
        )
    records = [latest[step_id] for step_id in normalized_steps]
    score = aggregate_score(records)
    result = assess_result(state, records, score, decision)
    if result == "PARTIAL":
        raise OperatorDecisionRequired(
            {
                **state_payload(path, state),
                "result": "PARTIAL",
                "score": score,
                "expectedSteps": normalized_steps,
                "operatorDecisionRequired": True,
                "operatorPrompt": (
                    "The completed assessment is below automatic acceptance. "
                    "Repeat finish with --decision accept or --decision reject."
                ),
            }
        )
    now = utc_now()
    state["status"] = "completed"
    state["result"] = result
    state["score"] = score
    state["expectedSteps"] = normalized_steps
    if decision is not None:
        state["operatorDecision"] = decision
    state["completedAt"] = now
    state["completionEvidence"] = evidence
    state["updatedAt"] = now
    write_state(path, state)
    print_json(state_payload(path, state))
