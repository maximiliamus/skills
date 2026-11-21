"""Explicit lifecycle of the project-owned runbook configuration."""

from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path

from .model import RunbookError
from .registry import validate_registry
from .storage import (
    fixed_local_path,
    interprocess_lock,
    repository_registry_path,
)
from .validation import RequestError, decode_json, require

CONFIG_TEMPLATE = Path(__file__).resolve().parents[2] / "config.json"


def config_path(root: Path, name: str, *, allow_link: bool = False) -> Path:
    path = fixed_local_path(root, name, subject="Project configuration")
    if path.exists():
        if not path.is_file():
            raise RunbookError(f"Project configuration must be a regular file: {path}")
        if not allow_link and path.stat().st_nlink != 1:
            raise RunbookError(f"Hard-linked project configuration: {path}; run update")
    return path


@contextmanager
def config_locks(root: Path):
    registry = repository_registry_path(root)
    path = config_path(root, registry.name, allow_link=True)
    if path.exists():
        read_config(path, root)
    try:
        with interprocess_lock(root, registry):
            yield
    except RunbookError as exc:
        if isinstance(exc, RequestError):
            raise
        # Storage wraps OS errors with the operation and path.
        if isinstance(exc.__cause__, OSError):
            raise RequestError(str(exc), "IO_ERROR") from exc
        raise


def publish_new(source: Path, target: Path) -> None:
    """Move complete bytes into place without overwriting another writer."""
    if os.name == "nt":
        os.rename(source, target)
    else:
        os.link(source, target)
        source.unlink()


def read_config(path: Path, root: Path) -> dict:
    config = decode_json(path.read_bytes())
    validate_registry(config, path, root)
    return config


def recover_initialization(root: Path) -> Path:
    """Finish only our staged initialization; caller holds config_locks(root)."""
    path = config_path(root, repository_registry_path(root).name, allow_link=True)
    if path.is_file() and path.stat().st_nlink == 2:
        prefix = f".{path.name}.init-"
        for staged in root.glob(f"{prefix}*.tmp"):
            if not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}\.tmp", staged.name):
                continue
            staged = fixed_local_path(root, staged.name, subject="Staged configuration")
            if staged.is_file() and staged.samefile(path):
                read_config(path, root)
                if path.stat().st_nlink != 2 or staged.stat().st_nlink != 2:
                    raise RunbookError("Configuration links changed during recovery")
                staged.unlink()
                break
    return config_path(root, path.name)


def initialize_config(root: Path) -> dict:
    """Caller holds config_locks; existing bytes and permissions remain unchanged."""
    path = recover_initialization(root)
    created = not path.exists()
    if created:
        data = CONFIG_TEMPLATE.read_bytes()
        config = decode_json(data)
        if not isinstance(config, dict):
            raise RunbookError("Expected a configuration object in the template")
        validate_registry(config, CONFIG_TEMPLATE, root)
        temporary = path.with_name(f".{path.name}.init-{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            config_path(root, path.name)
            publish_new(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        config = read_config(path, root)
    return {"projectRoot": str(root), "configPath": str(path), "created": created, "config": config}


def update_config(root: Path) -> dict:
    """Validate the canonical registry without rewriting it."""
    path = recover_initialization(root)
    require(path.is_file(), "No project config; run init first", "NOT_FOUND")
    return {"created": False, "config": read_config(path, root)}
