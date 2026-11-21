"""Validate journal requests and repository references."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import PureWindowsPath
from uuid import UUID

from .model import RunbookError
from .storage import inside_repo


class RequestError(RunbookError):
    def __init__(self, message, code="INVALID_REQUEST", **details):
        super().__init__(message)
        self.code = code
        self.details = details


def require(condition, message, code="INVALID_REQUEST", **details):
    if not condition:
        raise RequestError(message, code, **details)


def fields(value, required, optional=(), label="data"):
    require(isinstance(value, dict), f"{label} must be an object")
    missing = set(required) - value.keys()
    unknown = value.keys() - set(required) - set(optional)
    require(not unknown, f"Unknown {label} field(s): {', '.join(sorted(unknown))}")
    require(not missing, f"Missing {label} field(s): {', '.join(sorted(missing))}")


def string(value, label):
    require(isinstance(value, str) and value.strip(), f"{label} must be a nonempty string")
    return value


def uuid_string(value, label):
    string(value, label)
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise RequestError(f"{label} must be a UUID") from exc
    require(value.lower() == str(parsed), f"{label} must be a hyphenated UUID")
    return str(parsed)


def integer(value, label, minimum=1):
    require(type(value) is int and value >= minimum, f"{label} must be an integer >= {minimum}")
    return value


def boolean(value, label):
    require(type(value) is bool, f"{label} must be a boolean")
    return value


def choice(value, allowed, label):
    string(value, label)
    require(value in allowed, f"Invalid {label}: {value}")
    return value


def array(value, label, *, empty=False):
    require(
        isinstance(value, list) and (empty or value),
        f"{label} must be an array" + ("" if empty else " with at least one entry"),
    )
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def decode_json(content):
    value = json.loads(content.decode("utf-8"), object_pairs_hook=unique_object)
    # Reject nonfinite numbers (including overflowing exponents) and surrogates.
    canonical_json(value)
    require(isinstance(value, dict), "Expected a JSON object")
    return value


def canonical_json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def sha256(value):
    require(
        isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value),
        "sha256 must be a lowercase SHA-256 digest",
    )
    return value


def portable_name(value):
    string(value, "directory or path component")
    # Windows also reserves device names with extensions and superscript digits.
    device = re.compile(r"(?i:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])")
    require(
        value == value.strip()
        and not value.endswith(".")
        and not re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
        and not device.fullmatch(value.split(".")[0].rstrip())
        and len(value.encode("utf-8")) <= 255,
        f"Invalid portable directory or path component: {value!r}",
    )
    return value


def relative_name(value, *, portable=False):
    string(value, "path")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    require(
        not PureWindowsPath(value).anchor
        and not normalized.startswith("/")
        and not any(part in {"", ".", ".."} for part in parts)
        and not any(":" in part or not part.isprintable() for part in parts)
        and value == value.strip(),
        f"Path must be relative to the repository without traversal: {value!r}",
    )
    if portable:
        for part in parts:
            portable_name(part)
    return normalized


def path_key(value):
    return tuple(unicodedata.normalize("NFC", part).casefold() for part in value.split("/"))


def paths_overlap(first, second):
    left, right = path_key(first), path_key(second)
    count = min(len(left), len(right))
    return left[:count] == right[:count]


def protected_path(root, value, *, deliverable=False):
    """Inspect each component without following links, including inside the repository."""
    parts = relative_name(value, portable=True).split("/")
    if deliverable:
        reserved = {".git", "execute-guided-runbook.json"}
        if parts[0].casefold() == ".runbooks":
            require(
                len(parts) >= 3 and parts[1].casefold() not in {"runs", ".locks", "archive", "staging"},
                f"Deliverable targets runbook state: {value}",
            )
        require(
            parts[0].casefold() not in reserved,
            f"Deliverable targets repository metadata: {value}",
        )
    path = root
    for part in parts:
        path = path / part
        require(
            not path.is_symlink() and not path.is_junction(),
            f"Redirected path: {value}",
        )
    if path.exists():
        require(path.is_file() and path.stat().st_nlink == 1, f"Not a regular unlinked file: {value}")
    return path


def artifact(root, value, *, verify=False, historical=False):
    fields(value, ("path", "sha256"), label="artifact")
    path = root / relative_name(value["path"])
    sha256(value["sha256"])
    if not historical:
        path = inside_repo(root, path)
    if verify:
        require(
            matches_digest(path, value["sha256"]),
            f"Artifact is missing or changed: {value['path']}",
            "BLOCKED",
        )
    return path


def matches_digest(path, expected):
    try:
        return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected
    except FileNotFoundError, IsADirectoryError:
        return False


def decision(value, *, closing=False):
    required = ("instruction", "reference", "choice") if closing else ("instruction", "reference")
    fields(value, required, label="decision")
    string(value["instruction"], "decision.instruction")
    string(value["reference"], "decision.reference")
    if closing:
        choice(value["choice"], ("accept", "reject"), "decision.choice")


def require_decision(data, *, closing=False):
    require("decision" in data, "An explicit operator decision is required", "DECISION_REQUIRED")
    decision(data["decision"], closing=closing)
