"""Filesystem, locking, and serialization helpers for runbook sessions."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .model import (
    ATOMIC_REPLACE_TIMEOUT_SECONDS,
    DEFAULT_REGISTRY_FILENAME,
    LOCK_DIRECTORY_NAME,
    LOCK_POLL_SECONDS,
    LOCK_TIMEOUT_SECONDS,
    RunbookError,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


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
    ancestors = [current, *current.parents]
    for parent in ancestors:
        if (parent / ".git").exists():
            return parent
    for parent in ancestors:
        if (
            (parent / DEFAULT_REGISTRY_FILENAME).is_file()
            or (
                parent != Path.home()
                and (parent / ".agents/skills/execute-guided-runbook/SKILL.md").is_file()
            )
        ):
            return parent
    for parent in ancestors:
        if (parent / "AGENTS.md").is_file():
            return parent
    return current


def preserve_replacement_permissions(target: Path, temporary: Path) -> None:
    """Apply the existing file's access rules to its replacement before renaming it."""
    if os.name == "nt":
        copy_windows_dacl(target, temporary)
    else:
        temporary.chmod(stat.S_IMODE(target.stat().st_mode))


def copy_windows_dacl(target: Path, temporary: Path) -> None:
    from ctypes import POINTER, WinDLL, WinError, byref, c_void_p, get_last_error
    from ctypes.wintypes import BOOL, DWORD, LPCWSTR, WORD

    security = WinDLL("advapi32", use_last_error=True)
    local_free = WinDLL("kernel32").LocalFree
    local_free.argtypes = [c_void_p]
    local_free.restype = c_void_p
    read = security.GetNamedSecurityInfoW
    read.argtypes = [LPCWSTR, DWORD, DWORD, c_void_p, c_void_p, POINTER(c_void_p), c_void_p,
                     POINTER(c_void_p)]
    read.restype = DWORD
    control = security.GetSecurityDescriptorControl
    control.argtypes = [c_void_p, POINTER(WORD), POINTER(DWORD)]
    control.restype = BOOL
    write = security.SetNamedSecurityInfoW
    write.argtypes = [LPCWSTR, DWORD, DWORD, c_void_p, c_void_p, c_void_p, c_void_p]
    write.restype = DWORD

    file_object, dacl_information = 1, 4
    dacl, descriptor = c_void_p(), c_void_p()
    error = read(str(target), file_object, dacl_information, None, None, byref(dacl), None,
                 byref(descriptor))
    if error:
        raise WinError(error)
    try:
        flags, revision = WORD(), DWORD()
        if not control(descriptor, byref(flags), byref(revision)):
            raise WinError(get_last_error())
        # Retain whether the DACL inherits access rules from the parent.
        inheritance = 0x80000000 if flags.value & 0x1000 else 0x20000000
        error = write(str(temporary), file_object, dacl_information | inheritance, None, None,
                      dacl, None)
        if error:
            raise WinError(error)
    finally:
        local_free(descriptor)


def write_json_atomic(path: Path, payload: Any, *, default_mode: int) -> None:
    serialized = serialize_json(payload, subject=f"JSON file {path}")
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        replacing = path.exists() and not path.is_symlink()
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
            handle.flush()
            os.fsync(handle.fileno())
        if replacing:
            preserve_replacement_permissions(path, temporary)
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
        # On Windows a parent created during child resolution can leave a
        # spurious extended-path prefix, which looks like redirection.
        state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_root = fixed_local_path(
            state_root,
            LOCK_DIRECTORY_NAME,
            subject="Interprocess lock directory",
        )
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
        open_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, open_flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
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
        # Windows can lock the first byte of an empty file. Writing it before
        # acquiring the lock races with another process that already owns it.
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
        if expected.is_symlink() or expected.is_junction():
            raise RunbookError(f"{subject} must not be a symbolic link or junction: {expected}")
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


def print_json(payload: Any) -> None:
    print(serialize_json(payload, subject="JSON output"))


def configure_standard_streams() -> None:
    for stream, errors in ((sys.stdout, "strict"), (sys.stderr, "backslashreplace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors=errors)
