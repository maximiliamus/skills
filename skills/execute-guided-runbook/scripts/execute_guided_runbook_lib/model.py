"""Shared constants and errors for runbook sessions."""

from __future__ import annotations

import re
from typing import Any

DEFAULT_REGISTRY_FILENAME = "runbooks.json"
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
GENERATED_PATH_ID_PATTERN = re.compile(r"^path-[0-9a-f]{12}$")
LEGACY_HASH_FILENAME_ID_PATTERN = re.compile(r"^id-[0-9a-f]{64}$")
EFFORT_LEVELS = {"low", "medium", "high", "extra"}
DEFAULT_EFFORT_LEVEL = "medium"
MODEL_TIERS = {"light", "medium", "heavy"}
DEFAULT_MODEL_TIER = "medium"
ACCEPTANCE_POLICIES = {"strict", "flexible", "always"}
DEFAULT_ACCEPTANCE_POLICY = "flexible"
STEP_ORDERS = {"sequential", "arbitrary"}
DEFAULT_STEP_ORDER = "sequential"
STEP_RESULTS = {"PASS", "FAIL", "SKIPPED"}
ASSESSMENT_RESULTS = {"PASSED", "ACCEPTED", "PARTIAL", "REJECTED"}
OPERATOR_DECISIONS = {"accept", "reject"}
ASSESSMENT_VERSION = 1
LOCK_DIRECTORY_NAME = ".locks"
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.05
ATOMIC_REPLACE_TIMEOUT_SECONDS = 1.0
# Leave room for the atomic writer's UUID suffix under common 255-byte limits.
MAX_DIRECT_SESSION_ID_LENGTH = 128
REGISTRY_FIELDS = {
    "id",
    "title",
    "path",
    "description",
    "effortLevel",
    "modelTier",
}


def valid_acceptance_threshold(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return re.fullmatch(r"(?:100|[1-9][0-9]?)%", value) is not None


class RunbookError(RuntimeError):
    """Raised when a runbook session cannot safely continue."""


class OperatorDecisionRequired(RunbookError):
    """Raised when an unfinished outdated session needs an operator choice."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload["operatorPrompt"])
        self.payload = payload
