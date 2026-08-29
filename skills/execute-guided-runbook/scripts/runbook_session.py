#!/usr/bin/env python3
"""Manage resumable local sessions for repository runbooks."""

from __future__ import annotations

from execute_guided_runbook_lib import cli, state
from execute_guided_runbook_lib.cli import main
from execute_guided_runbook_lib.model import RunbookError
from execute_guided_runbook_lib.registry import write_registry
from execute_guided_runbook_lib.state import archive_state, new_state, read_state
from execute_guided_runbook_lib.storage import (
    find_repo_root,
    lock_file_path,
    read_json,
    repository_registry_path,
    session_path,
    write_state,
)

__all__ = [
    "RunbookError",
    "archive_state",
    "cli",
    "find_repo_root",
    "lock_file_path",
    "main",
    "new_state",
    "read_json",
    "read_state",
    "repository_registry_path",
    "session_path",
    "state",
    "write_registry",
    "write_state",
]


if __name__ == "__main__":
    raise SystemExit(main())
