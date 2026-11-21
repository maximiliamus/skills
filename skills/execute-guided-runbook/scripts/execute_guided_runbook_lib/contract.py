"""Compile the Markdown procedure and supplied facts into a frozen contract."""

from __future__ import annotations

import hashlib
import re
from graphlib import CycleError, TopologicalSorter

from .model import EFFORT_LEVELS, ID_PATTERN, MODEL_TIERS
from .model_binding import normalize_model_family
from .registry import runbook_document_properties
from .validation import (
    RequestError,
    array,
    artifact,
    boolean,
    choice,
    fields,
    integer,
    paths_overlap,
    protected_path,
    require,
    sha256,
    string,
)

GAP_CAUSES = ("failed-prerequisite", "exhausted", "invalidated")
STEP_SETTINGS = ("modelFamily", "modelTier", "effortLevel", "delegateSteps", "maxAttempts")
MARKER_CANDIDATE = re.compile(r" {0,3}<!--\s*(step|deliverable)\s*:")
MARKER = re.compile(r" {0,3}<!--\s*(step|deliverable):\s+(.+?)\s*-->[ \t]*")
HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.+)")
ORDERED_ITEM = re.compile(r" {0,3}[0-9]{1,9}[.)][ \t]+(.+)")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
LIST = re.compile(r" {0,3}(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]+|$)")


def top_level_lines(lines, start):
    fence = None
    list_indent = 0
    html_end = None
    for index in range(start, len(lines)):
        line = lines[index]
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + f"{{{len(fence)},}}[ \t]*", line):
                fence = None
            continue
        if html_end:
            if html_end.search(line):
                html_end = None
            continue
        if not line.strip():
            continue
        indent = len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip(" "))
        if indent >= 4 or 0 < list_indent <= indent:
            continue
        list_indent = 0
        item = LIST.match(line)
        if item:
            list_indent = item.end()
            yield index + 1, line
            continue
        if line.lstrip().startswith(">"):
            continue
        opening = FENCE.fullmatch(line)
        if opening and (opening[1][0] == "~" or "`" not in opening[2]):
            fence = opening[1]
            continue
        if not MARKER_CANDIDATE.match(line):
            if line.lstrip().startswith("<!--") and "-->" not in line:
                html_end = re.compile(r"-->")
            elif block := re.match(
                r" {0,3}<(pre|script|style|textarea)(?:[\s>]|$)", line, re.IGNORECASE
            ):
                end = re.compile(rf"</{block[1]}>", re.IGNORECASE)
                if not end.search(line):
                    html_end = end
        yield index + 1, line


def heading_title(heading):
    title = re.sub(r"[ \t]+#+[ \t]*$", "", heading[2]).strip() if heading else ""
    return title if set(title) != {"#"} else ""


def step_record(sid, title, first, last):
    return {
        "id": sid,
        "title": title,
        "deliverables": [],
        "instruction": {"startLine": first, "endLine": last},
    }


def inferred_steps(visible, line_count, step_inventory, revision):
    fingerprint = sha256(step_inventory["sha256"])
    require(fingerprint == revision, "Runbook changed after recognizing its steps", "CONFLICT")
    steps = []
    seen = set()
    previous_end = 0
    for entry in array(step_inventory["steps"], "stepInventory.steps"):
        fields(entry, ("id", "startLine"), label="inferred step")
        sid = string(entry["id"], "inferred step ID")
        first = integer(entry["startLine"], "inferred step startLine")
        require(ID_PATTERN.fullmatch(sid) and sid not in seen, f"Invalid or duplicate step ID: {sid!r}")
        require(first > previous_end, f"Inferred steps overlap or are out of order at line {first}")
        line = visible.get(first, "")
        heading = HEADING.fullmatch(line)
        item = ORDERED_ITEM.fullmatch(line)
        title = heading_title(heading) if heading and len(heading[1]) > 1 else ""
        if item:
            title = item[1].strip()
        require(title, f"Inferred step {sid} needs a top-level heading or ordered item at line {first}")

        last = line_count
        for number in range(first + 1, line_count + 1):
            following = visible.get(number, "")
            boundary = HEADING.fullmatch(following)
            if heading:
                ends = boundary and len(boundary[1]) <= len(heading[1])
            else:
                ends = boundary or LIST.match(following)
            if ends:
                last = number - 1
                break
        steps.append(step_record(sid, title, first, last))
        seen.add(sid)
        previous_end = last
    return steps


def inventory(lines, start, root, run_id, *, step_inventory, revision):
    visible = dict(top_level_lines(lines, start))
    steps = []
    outputs = []
    for number, line in visible.items():
        if not MARKER_CANDIDATE.match(line):
            continue
        marker = MARKER.fullmatch(line)
        if marker is None:
            raise RequestError(f"Malformed runbook marker at line {number}")
        kind, value = marker.groups()
        if kind == "deliverable":
            outputs.append((number, value))
            continue
        require(
            ID_PATTERN.fullmatch(value) and all(step["id"] != value for step in steps),
            f"Invalid or duplicate step ID at line {number}: {value!r}",
        )
        heading = HEADING.fullmatch(lines[number]) if number < len(lines) else None
        title = heading_title(heading)
        require(title, f"Step {value} requires an ATX heading on the next line")
        if steps:
            steps[-1]["instruction"]["endLine"] = number - 1
        steps.append(step_record(value, title, number + 1, len(lines)))

    if step_inventory is not None:
        require(not steps, "stepInventory cannot replace marked steps")
        steps = inferred_steps(visible, len(lines), step_inventory, revision)
    elif not steps and not outputs:
        raise RequestError("The runbook has no declared steps")

    deliverables = set()
    for number, value in outputs:
        owner = next((
            step for step in steps
            if step["instruction"]["startLine"] < number <= step["instruction"]["endLine"]
        ), None)
        if owner is None:
            raise RequestError(f"Deliverable outside a step at line {number}")
        path = value.replace("{runId}", run_id)
        require("{" not in path and "}" not in path, f"Invalid deliverable template at line {number}")
        path = protected_path(root, path, deliverable=True).relative_to(root).as_posix()
        require(
            not any(paths_overlap(path, seen) for seen in deliverables),
            f"Colliding or overlapping deliverable path: {path}",
        )
        deliverables.add(path)
        owner["deliverables"].append(path)
    return steps


def step_facts(root, raw, strict):
    fields(
        raw,
        (),
        (
            "procedure",
            "gapCauses",
            "reuse",
            "reassessAfterInvalidation",
            "points",
        ),
        "step contract",
    )
    facts = {
        "procedure": array(raw.get("procedure", []), "procedure", empty=True),
        "gapCauses": array(raw.get("gapCauses", []), "gapCauses", empty=True),
        "reuse": boolean(raw.get("reuse", False), "reuse"),
        "reassessAfterInvalidation": boolean(
            raw.get("reassessAfterInvalidation", False), "reassessAfterInvalidation"
        ),
        "points": integer(raw.get("points", 1), "points"),
    }
    for cause in facts["gapCauses"]:
        choice(cause, GAP_CAUSES, "gap cause")
    require(len(set(facts["gapCauses"])) == len(facts["gapCauses"]), "Duplicate gap cause")
    require(not strict or facts["points"] == 1, "Strict acceptance requires one point per step")
    if any(
        (
            facts["gapCauses"],
            facts["reuse"],
            facts["reassessAfterInvalidation"],
            facts["points"] != 1,
        )
    ):
        require(facts["procedure"], "Supplied step policy requires procedure citations")
    for citation in facts["procedure"]:
        fields(citation, ("path", "sha256", "section"), label="procedure citation")
        string(citation["section"], "procedure.section")
        artifact(root, {key: citation[key] for key in ("path", "sha256")}, verify=True)
    return facts


def compile_contract(root, path, run_id, supplied, *, operator_family=None, step_inventory=None):
    raw = path.read_bytes()
    revision = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")
    props = runbook_document_properties(text, path)
    lines = text.splitlines()
    body = (
        next(index + 1 for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        if lines and lines[0].strip() == "---"
        else 0
    )
    overrides = props.pop("steps")
    if step_inventory is not None:
        require(
            props["maxParallelSteps"] == 1 and props["stepOrder"] == "sequential"
            and not any(settings.get("dependsOn") for settings in overrides.values()),
            "stepInventory requires a sequential runbook with one slot and no dependency graph",
        )
    steps = inventory(lines, body, root, run_id, step_inventory=step_inventory, revision=revision)
    ids = {step["id"] for step in steps}
    explicit = props["_explicitModelSettings"]
    fields(supplied, (), ids, "stepContracts")
    require(overrides.keys() <= ids, f"Unknown step overrides: {sorted(overrides.keys() - ids)}")
    contract = {
        key: props[key]
        for key in (
            "maxParallelSteps",
            "delegateSteps",
            "stepOrder",
            "acceptancePolicy",
            "acceptanceThreshold",
        )
    }
    contract["steps"] = steps
    contract["instructions"] = text
    graph = {}
    for step in steps:
        sid = step["id"]
        settings = overrides.get(sid, {})
        fields(settings, (), (*STEP_SETTINGS, "dependsOn"), f"settings for {sid}")
        base_family = operator_family or props["modelFamily"]
        origin = (
            "operator" if operator_family else "runbook" if "modelFamily" in explicit else "default"
        )
        family = (
            normalize_model_family(settings["modelFamily"])
            if "modelFamily" in settings
            else base_family
        )
        depends = settings.get("dependsOn", {})
        fields(depends, (), ids - {sid}, f"dependsOn for {sid}")
        for condition in depends.values():
            choice(condition, ("passed", "done", "assessed"), "dependency condition")
        depends = {key: "done" if condition == "assessed" else condition
                   for key, condition in depends.items()}
        graph[sid] = depends
        step.update(
            dependsOn=depends,
            modelFamily=family,
            modelFamilyOrigin="step" if "modelFamily" in settings else origin,
            explicitTier="modelTier" in settings or "modelTier" in explicit,
            explicitEffort="effortLevel" in settings or "effortLevel" in explicit,
            modelTier=choice(
                settings.get("modelTier", props["modelTier"]), MODEL_TIERS, "modelTier"
            ),
            effortLevel=choice(
                settings.get("effortLevel", props["effortLevel"]), EFFORT_LEVELS, "effortLevel"
            ),
            delegateSteps=boolean(
                settings.get("delegateSteps", props["delegateSteps"]), "delegateSteps"
            ),
            maxAttempts=integer(settings.get("maxAttempts", props["maxAttempts"]), "maxAttempts"),
        )
        step["instruction"].update(path=path.relative_to(root).as_posix(), sha256=revision)
        step.update(step_facts(root, supplied.get(sid, {}), props["acceptancePolicy"] == "strict"))
    try:
        tuple(TopologicalSorter(graph).static_order())
    except CycleError as exc:
        raise RequestError(f"Runbook dependency cycle: {exc.args[1]}") from exc
    return revision, contract
