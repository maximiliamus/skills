"""Install verified staged deliverables while the repository lock is held."""

import os
import shutil
import tempfile
from pathlib import Path

from . import storage
from .transitions import blocked, evidence
from .validation import (
    array,
    fields,
    matches_digest,
    paths_overlap,
    protected_path,
    require,
    run_staging,
    sha256,
)


def prepare(root, run, step_id, supplied, stage_scope, other_intents):
    declared = run.steps[step_id].contract["deliverables"]
    staging = run_staging(run.run_id, flat=run.flat)
    prefix = f"{staging}/{stage_scope}"
    outputs = []
    for item in array(supplied, "deliverables", empty=True):
        fields(item, ("path", "stagedPath", "sha256", "priorSha256"), ("replacement",), "deliverable")
        target = protected_path(root, item["path"], deliverable=True, run_id=run.run_id, flat=run.flat)
        staged = protected_path(root, item["stagedPath"])
        blocked(
            staged.is_relative_to(root / prefix) and staged != root / prefix,
            f"Stage deliverables under {prefix}/",
        )
        relative = target.relative_to(root).as_posix()
        require(relative in declared, f"Undeclared deliverable: {relative}")
        blocked(matches_digest(staged, sha256(item["sha256"])), f"Staged bytes changed: {relative}")
        expected = item["priorSha256"]
        if expected is not None:
            sha256(expected)
            evidence(item.get("replacement"))
        else:
            require("replacement" not in item, "Replacement authority needs the expected target digest")
        for intent in other_intents:
            blocked(
                not any(paths_overlap(relative, old["path"]) for old in intent["artifacts"]),
                f"Another unresolved publication overlaps {relative}",
            )
        blocked(prior_matches(target, expected), f"Target changed before publication: {relative}")
        outputs.append(item | {"path": relative, "stagedPath": staged.relative_to(root).as_posix()})
    blocked(
        sorted(item["path"] for item in outputs) == sorted(declared),
        "Supply verified staged bytes for every declared deliverable",
        paths=declared,
    )
    return outputs


def prior_matches(target, expected):
    return not target.exists() if expected is None else matches_digest(target, expected)


def publish(root, outputs, *, run_id, flat=False, recover=False):
    for item in outputs:
        target = protected_path(root, item["path"], deliverable=True, run_id=run_id, flat=flat)
        staged = protected_path(root, item["stagedPath"])
        wanted = item["sha256"]
        blocked(matches_digest(staged, wanted), f"Staged bytes are missing or changed: {staged}")
        if recover and matches_digest(target, wanted):
            continue
        blocked(
            prior_matches(target, item["priorSha256"]),
            f"Target changed before replacement: {item['path']}",
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        protected_path(root, item["path"], deliverable=True, run_id=run_id, flat=flat)
        fd, temporary = tempfile.mkstemp(prefix=".runbook-", suffix=".tmp", dir=target.parent)
        temporary = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as output, staged.open("rb") as source:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            blocked(matches_digest(temporary, wanted), f"Staged bytes changed while copying: {staged}")
            if item["priorSha256"] is not None:
                storage.preserve_replacement_permissions(target, temporary)
            protected_path(root, item["path"], deliverable=True, run_id=run_id, flat=flat)
            blocked(
                prior_matches(target, item["priorSha256"]),
                f"Target changed before replacement: {item['path']}",
            )
            storage.replace_with_retry(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    for item in outputs:
        target = protected_path(root, item["path"], deliverable=True, run_id=run_id, flat=flat)
        blocked(matches_digest(target, item["sha256"]), f"Published bytes changed: {item['path']}")
