#!/usr/bin/env python3
"""Run the repository-local guided-runbook state machine."""

from __future__ import annotations

from execute_guided_runbook_lib import cli
from execute_guided_runbook_lib.cli import main
from execute_guided_runbook_lib.model import RunbookError
from execute_guided_runbook_lib.storage import (
    find_repo_root,
    lock_file_path,
    repository_registry_path,
)

__all__ = [
    "RunbookError",
    "cli",
    "find_repo_root",
    "lock_file_path",
    "main",
    "repository_registry_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
