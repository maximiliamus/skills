"""Shared constants and errors for runbook sessions."""

from __future__ import annotations

import re

DEFAULT_REGISTRY_FILENAME = "execute-guided-runbook.json"
REGISTRY_SCHEMA_VERSION = 2
DEFAULT_JOURNAL_RETENTION = 3
RUN_FOLDER_MARKER = ".runbook-session"
LEGACY_JOURNAL_SCHEMA_VERSION = 4
JOURNAL_SCHEMA_VERSION = 5
VERIFIED_OUTCOMES = {"PASS": "PASSED", "FAIL": "FAILED", "PASSED": "PASSED", "FAILED": "FAILED"}
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
GENERATED_PATH_ID_PATTERN = re.compile(r"^path-[0-9a-f]{12}$")
EFFORT_LEVELS = ("low", "medium", "high", "extra")
DEFAULT_EFFORT_LEVEL = "medium"
MODEL_TIERS = ("light", "medium", "heavy")
DEFAULT_MODEL_TIER = "medium"
ACCEPTANCE_POLICIES = {"strict", "flexible", "always"}
DEFAULT_ACCEPTANCE_POLICY = "flexible"
STEP_ORDERS = {"sequential", "arbitrary"}
DEFAULT_STEP_ORDER = "sequential"
LOCK_DIRECTORY_NAME = ".locks"
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.05
ATOMIC_REPLACE_TIMEOUT_SECONDS = 1.0
REGISTRY_FIELDS = {
    "id",
    "title",
    "path",
    "description",
    "journalRetention",
}


def valid_acceptance_threshold(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return re.fullmatch(r"(?:100|[1-9][0-9]?)%", value) is not None


class RunbookError(RuntimeError):
    """Raised when a runbook session cannot safely continue."""
