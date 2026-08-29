"""Filesystem, locking, and serialization helpers for runbook sessions."""

from __future__ import annotations

import errno
import hashlib
import json
import ntpath
import os
import stat
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .model import (
    ATOMIC_REPLACE_TIMEOUT_SECONDS,
    DEFAULT_REGISTRY_FILENAME,
    ID_PATTERN,
    LEGACY_HASH_FILENAME_ID_PATTERN,
    LOCK_DIRECTORY_NAME,
    LOCK_POLL_SECONDS,
    LOCK_TIMEOUT_SECONDS,
    MAX_DIRECT_SESSION_ID_LENGTH,
    RunbookError,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunbookError(f"File does not exist: {path}") from exc
    except UnicodeDecodeError as exc:
        raise RunbookError(f"File is not valid UTF-8: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RunbookError(f"Invalid JSON in {path}: {exc}") from exc
    except OSError as exc:
        raise RunbookError(f"Could not read JSON file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RunbookError(f"Expected a JSON object in {path}")
    return data


def serialize_json(payload: Any, *, subject: str) -> str:
    try:
        serialized = json.dumps(payload, indent=2, ensure_ascii=False)
        serialized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise RunbookError(f"{subject} contains invalid Unicode") from exc
    except (TypeError, ValueError) as exc:
        raise RunbookError(f"{subject} cannot be serialized as JSON") from exc
    return serialized


def find_repo_root(start_dir: Path | None = None) -> Path:
    current = (start_dir or Path.cwd()).resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return current


def write_json_atomic(path: Path, payload: Any, *, default_mode: int) -> None:
    serialized = serialize_json(payload, subject=f"JSON file {path}")
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        existing_mode: int | None = None
        if path.exists() and not path.is_symlink():
            existing_mode = stat.S_IMODE(path.stat().st_mode)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            default_mode,
        )
        handle = os.fdopen(
            descriptor,
            mode="w",
            encoding="utf-8",
            newline="\n",
        )
        descriptor = None
        with handle:
            handle.write(serialized)
            handle.write("\n")
        if os.name != "nt" and existing_mode is not None:
            temporary.chmod(existing_mode)
        replace_with_retry(temporary, path)
    except OSError as exc:
        raise RunbookError(f"Could not write JSON file {path}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def replace_with_retry(source: Path, target: Path) -> None:
    deadline = time.monotonic() + ATOMIC_REPLACE_TIMEOUT_SECONDS
    while True:
        try:
            source.replace(target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(LOCK_POLL_SECONDS)


def lock_file_path(repo_root: Path, target: Path) -> Path:
    try:
        resolved_repo = repo_root.resolve()
        if not resolved_repo.is_dir():
            raise RunbookError(f"Repository root is not a directory: {resolved_repo}")
        resolved_target = target.resolve()
        relative_target = resolved_target.relative_to(resolved_repo)
        normalized_target = os.path.normcase(relative_target.as_posix())

        state_root = fixed_local_path(
            resolved_repo,
            ".runbooks",
            subject="Session state directory",
        )
        lock_root = fixed_local_path(
            state_root,
            LOCK_DIRECTORY_NAME,
            subject="Interprocess lock directory",
        )
        state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_root.mkdir(mode=0o700, exist_ok=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RunbookError(f"Could not prepare interprocess lock for {target}: {exc}") from exc
    digest = hashlib.sha256(normalized_target.encode("utf-8")).hexdigest()
    return fixed_local_path(
        lock_root,
        f"{digest}.lock",
        subject="Interprocess lock file",
    )


def try_lock_handle(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_handle(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def interprocess_lock(repo_root: Path, target: Path) -> Iterator[None]:
    lock_path = lock_file_path(repo_root, target)
    descriptor: int | None = None
    try:
        open_flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, open_flags, 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "lock path is not a regular file", str(lock_path))
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        descriptor = None
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise RunbookError(f"Could not open interprocess lock for {target}: {exc}") from exc

    locked = False
    body_failed = False
    try:
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
        except OSError as exc:
            raise RunbookError(f"Could not initialize interprocess lock for {target}: {exc}") from exc

        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                try_lock_handle(handle)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise RunbookError(
                        f"Could not acquire interprocess lock for {target}: {exc}"
                    ) from exc
                if time.monotonic() >= deadline:
                    raise RunbookError(
                        f"Timed out waiting for another runbook command to finish: {target}"
                    ) from exc
                time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        except BaseException:
            body_failed = True
            raise
    finally:
        release_error: OSError | None = None
        if locked:
            try:
                unlock_handle(handle)
            except OSError as exc:
                release_error = exc
        try:
            handle.close()
        except OSError as exc:
            release_error = release_error or exc
        if release_error is not None and not body_failed:
            raise RunbookError(
                f"Could not release interprocess lock for {target}: {release_error}"
            ) from release_error


def confined_path(
    root: Path,
    candidate: Path,
    *,
    subject: str,
    boundary: str,
) -> Path:
    try:
        resolved_root = root.resolve()
        resolved_candidate = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise RunbookError(f"Could not resolve {subject.lower()}: {candidate}: {exc}") from exc
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise RunbookError(f"{subject} is outside {boundary}: {resolved_candidate}") from exc
    return resolved_candidate


def fixed_local_path(root: Path, name: str, *, subject: str) -> Path:
    try:
        resolved_root = root.resolve()
        expected = resolved_root / name
        resolved = expected.resolve()
    except (OSError, RuntimeError) as exc:
        raise RunbookError(f"Could not resolve {subject.lower()}: {root / name}: {exc}") from exc
    if resolved != expected:
        raise RunbookError(f"{subject} must not be a symbolic link or junction: {expected}")
    return expected


def repository_registry_path(repo_root: Path) -> Path:
    return fixed_local_path(
        repo_root,
        DEFAULT_REGISTRY_FILENAME,
        subject="Runbook registry",
    )


def inside_repo(repo_root: Path, candidate: Path) -> Path:
    return confined_path(
        repo_root,
        candidate,
        subject="Runbook path",
        boundary="the repository",
    )


def read_runbook_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RunbookError(f"Runbook is not valid UTF-8: {path}: {exc}") from exc
    except OSError as exc:
        raise RunbookError(f"Could not read runbook file: {path}") from exc


def path_runbook_id(relative_path: str) -> str:
    digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:12]
    return f"path-{digest}"


def content_hash(path: Path) -> str:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise RunbookError(f"Could not read runbook file: {path}") from exc
    return hashlib.sha256(content).hexdigest()


def legacy_session_filename(runbook_id: str) -> str:
    if len(runbook_id) <= MAX_DIRECT_SESSION_ID_LENGTH:
        return f"{runbook_id}.json"
    digest = hashlib.sha256(runbook_id.encode("utf-8")).hexdigest()
    return f"id-{digest}.json"


def session_filename(runbook_id: str) -> str:
    direct_name = f"{runbook_id}.json"
    if (
        len(runbook_id) <= MAX_DIRECT_SESSION_ID_LENGTH
        and not LEGACY_HASH_FILENAME_ID_PATTERN.fullmatch(runbook_id)
        and not ntpath.isreserved(direct_name)
    ):
        return direct_name
    if len(runbook_id) > MAX_DIRECT_SESSION_ID_LENGTH:
        return legacy_session_filename(runbook_id)
    digest = hashlib.sha256(runbook_id.encode("utf-8")).hexdigest()
    return f"_id-{digest}.json"


def session_path(repo_root: Path, runbook_id: str) -> Path:
    if not ID_PATTERN.fullmatch(runbook_id):
        raise RunbookError(f"Invalid runbook id for session state: {runbook_id}")
    state_root = fixed_local_path(
        repo_root,
        ".runbooks",
        subject="Session state directory",
    )
    return fixed_local_path(
        state_root,
        session_filename(runbook_id),
        subject="Runbook session file",
    )


def legacy_session_path(repo_root: Path, runbook_id: str) -> Path:
    if not ID_PATTERN.fullmatch(runbook_id):
        raise RunbookError(f"Invalid runbook id for session state: {runbook_id}")
    state_root = fixed_local_path(
        repo_root,
        ".runbooks",
        subject="Session state directory",
    )
    return fixed_local_path(
        state_root,
        legacy_session_filename(runbook_id),
        subject="Legacy runbook session file",
    )


def migrate_legacy_session(canonical: Path, legacy: Path, runbook_id: str) -> None:
    if canonical == legacy or not legacy.exists():
        return
    legacy_state = read_json(legacy)
    if legacy_state.get("runbookId") != runbook_id:
        return
    if canonical.exists():
        raise RunbookError(
            f"Both legacy and current session files exist for {runbook_id}: "
            f"{legacy} and {canonical}"
        )
    try:
        replace_with_retry(legacy, canonical)
    except OSError as exc:
        raise RunbookError(
            f"Could not migrate legacy session file for {runbook_id}: {legacy}"
        ) from exc


@contextmanager
def session_locks(
    repo_root: Path,
    runbook_ids: list[str],
) -> Iterator[dict[str, Path]]:
    unique_ids = list(dict.fromkeys(runbook_ids))
    canonical_paths = {
        runbook_id: session_path(repo_root, runbook_id) for runbook_id in unique_ids
    }
    legacy_paths = {
        runbook_id: legacy_session_path(repo_root, runbook_id) for runbook_id in unique_ids
    }
    lock_targets = sorted(
        {*canonical_paths.values(), *legacy_paths.values()},
        key=lambda candidate: os.path.normcase(str(candidate)),
    )
    with ExitStack() as locks:
        for lock_target in lock_targets:
            locks.enter_context(interprocess_lock(repo_root, lock_target))
        for runbook_id in unique_ids:
            migrate_legacy_session(
                canonical_paths[runbook_id],
                legacy_paths[runbook_id],
                runbook_id,
            )
        yield canonical_paths


def write_state(path: Path, state: dict[str, Any]) -> None:
    write_json_atomic(path, state, default_mode=0o600)


def state_payload(path: Path, state: dict[str, Any]) -> dict[str, Any]:
    return {**state, "statePath": str(path)}


def print_json(payload: Any) -> None:
    print(serialize_json(payload, subject="JSON output"))


def configure_standard_streams() -> None:
    for stream, errors in ((sys.stdout, "strict"), (sys.stderr, "backslashreplace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors=errors)


def require_nonempty(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise RunbookError(f"{field} must not be empty")
    return normalized
