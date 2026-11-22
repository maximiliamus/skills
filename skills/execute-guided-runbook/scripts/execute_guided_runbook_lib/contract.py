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
    relative_name,
    require,
    run_artifacts,
    sha256,
    string,
)

GAP_CAUSES = ("failed-prerequisite", "exhausted", "invalidated")
STEP_SETTINGS = ("modelFamily", "modelTier", "effortLevel", "delegateSteps", "maxAttempts")
MARKER_CANDIDATE = re.compile(r" {0,3}<!--\s*(step|deliverable)\s*:")
MARKER = re.compile(r" {0,3}<!--\s*(step|deliverable):\s+(.+?)\s*-->[ \t]*")
HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.+)")
SETEXT = re.compile(r"=+|-+")
ORDERED_ITEM = re.compile(r" {0,3}[0-9]{1,9}[.)][ \t]+(.+)")
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
LIST = re.compile(r" {0,3}(?:[-+*]|[0-9]{1,9}[.)])(?:[ \t]+|$)")
WRITE_OUTPUT = re.compile(
    r"Write[ \t]+([^`]+?)[ \t]+to[ \t]+`([^`]+)`[.!]?", re.IGNORECASE,
)
QUALIFIED_OUTPUT = re.compile(
    r"\b(?:optional(?:ly)?|if|unless|when|may|might|can|examples?|samples?)\b", re.IGNORECASE,
)
EXAMPLE_LEAD = re.compile(
    r"(?:(?:for|as an?|by way of|in this)[ \t]+)?(?:examples?|samples?)\b|for instance\b|e\.g\.",
    re.IGNORECASE,
)
OPTIONAL_LEAD = re.compile(r"optional\b|(?:if|when) (?:needed|desired|requested)\b", re.IGNORECASE)
CONDITIONAL_LEAD = re.compile(r"(?:if|unless|only if|provided that)\b", re.IGNORECASE)
OPTIONAL_WORD = re.compile(r"\boptional(?:ly)?\b", re.IGNORECASE)
INLINE_CODE = re.compile(r"`([^`]+)`")


def top_level_lines(lines, start, *, list_prose=False, fence_starts=None):
    fence = None
    list_indent = 0
    list_indents = []
    html_end = None
    for index in range(start, len(lines)):
        line = lines[index]
        offset = 0
        if list_prose and line.strip():
            expanded = line.expandtabs(4)
            indentation = len(expanded) - len(expanded.lstrip(" "))
            while list_indents and indentation < list_indents[-1]:
                list_indents.pop()
            if list_indents:
                offset = list_indents[-1]
                line = expanded[offset:]
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
            if not list_prose:
                list_indent = item.end()
                yield index + 1, line
                continue
            list_indents.append(offset + item.end())
            line = line[item.end():]
        if line.lstrip().startswith(">"):
            if list_prose:
                yield index + 1, line
            continue
        opening = FENCE.fullmatch(line)
        if opening and (opening[1][0] == "~" or "`" not in opening[2]):
            fence = opening[1]
            if fence_starts is not None:
                fence_starts.add(index + 1)
            continue
        if not MARKER_CANDIDATE.match(line):
            if line.lstrip().startswith("<!--") and "-->" not in line:
                html_end = re.compile(r"-->")
            elif block := re.match(
                r" {0,3}<(pre|script|style|textarea|blockquote)(?:[\s>]|$)", line, re.IGNORECASE
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


def is_example(text):
    return EXAMPLE_LEAD.match(text) is not None or text.casefold().endswith("example:")


def exclude_condition(text, *, include_conditionals):
    return CONDITIONAL_LEAD.match(text) is not None and (
        not include_conditionals or OPTIONAL_WORD.search(text) is not None
    )


def procedure_prose(lines, visible, span, *, include_conditionals=False, fence_starts=()):
    """Yield source lines that can carry an operative output requirement."""
    excluded_heading = None
    excluded_paragraph = None
    excluded_item = None
    underline_line = None
    quoted = False
    for number in range(span["startLine"], span["endLine"] + 1):
        if number == underline_line:
            continue
        if excluded_paragraph == "example-intro" and number in fence_starts:
            # The fence already excludes the example from the visible prose.
            excluded_paragraph = None
            continue
        raw = lines[number - 1]
        if not raw.strip():
            if excluded_paragraph == "body":
                excluded_paragraph = None
            quoted = False
            continue
        expanded = raw.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip(" "))
        if excluded_item is not None:
            if indent > excluded_item:
                continue
            excluded_item = None
        text = visible.get(number, "").strip()
        if text.startswith(">"):
            if excluded_paragraph == "example-intro":
                excluded_paragraph = "body"
            quoted = bool(text.lstrip(">").strip())
            continue
        level = 0
        if heading := HEADING.fullmatch(text):
            level, title = len(heading[1]), heading_title(heading)
        elif text and number < span["endLine"] and not MARKER_CANDIDATE.match(text):
            underline = visible.get(number + 1, "").strip()
            if SETEXT.fullmatch(underline):
                level, title = (1 if underline.startswith("=") else 2), text
                underline_line = number + 1
        if level:
            if excluded_heading is not None and level <= excluded_heading:
                excluded_heading = None
            if (
                is_example(title) or OPTIONAL_LEAD.match(title)
                or exclude_condition(title, include_conditionals=include_conditionals)
            ):
                excluded_heading = level if excluded_heading is None else excluded_heading
            elif title and excluded_heading is None:
                yield number, title
            excluded_paragraph = None
            quoted = False
            continue
        if not quoted and excluded_heading is None:
            if excluded_paragraph == "example":
                continue
            if excluded_paragraph == "example-intro":
                # Plain example prose continues across blank lines up to the next heading.
                excluded_paragraph = "example" if text and not text.startswith("<") else "body"
                continue
            label = text.replace("*", "").replace("_", "").strip()
            introduces_context = label.endswith((":", ","))
            example = is_example(label)
            conditional = introduces_context and exclude_condition(
                label, include_conditionals=include_conditionals,
            )
            if example or OPTIONAL_LEAD.match(label) or conditional:
                if LIST.match(expanded.lstrip()):
                    excluded_item = indent
                elif example and introduces_context:
                    excluded_paragraph = "example-intro"
                else:
                    excluded_paragraph = "awaiting" if introduces_context else "body"
            elif excluded_paragraph:
                excluded_paragraph = "body"
            elif text and not MARKER_CANDIDATE.match(text):
                yield number, text


def names_output(text, path):
    if path in INLINE_CODE.findall(text):
        return True
    unquoted = INLINE_CODE.sub("", text)
    return re.search(
        rf"(?<![\w./\\:-]){re.escape(path)}(?![\w/\\:-]|\.[\w.])", unquoted,
    ) is not None


def inventory(lines, start, root, run_id, *, step_inventory, supplied, revision):
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

    fields(supplied, (), {step["id"] for step in steps}, "stepContracts")
    deliverables = {}

    def attach(number, value, owner, *, marker=False):
        path = value.replace("{runId}", run_id)
        require("{" not in path and "}" not in path, f"Invalid deliverable template at line {number}")
        if path.startswith("repo:"):
            path = path.removeprefix("repo:")
        else:
            private = relative_name(path, portable=True)
            path = f"{run_artifacts(run_id)}/{private}"
        path = protected_path(root, path, deliverable=True, run_id=run_id)
        path = path.relative_to(root).as_posix()
        if not marker and deliverables.get(path) == owner["id"]:
            return
        require(
            not any(paths_overlap(path, seen) for seen in deliverables),
            f"Colliding or overlapping deliverable path: {path}",
        )
        deliverables[path] = owner["id"]
        owner["deliverables"].append(path)

    for number, value in outputs:
        owner = next((
            step for step in steps
            if step["instruction"]["startLine"] < number <= step["instruction"]["endLine"]
        ), None)
        if owner is None:
            raise RequestError(f"Deliverable outside a step at line {number}")
        attach(number, value, owner, marker=True)

    fence_starts = set()
    prose_visible = dict(top_level_lines(lines, start, list_prose=True, fence_starts=fence_starts))
    runbook_prose = dict(procedure_prose(
        lines, prose_visible, {"startLine": start + 1, "endLine": len(lines)},
        include_conditionals=True, fence_starts=fence_starts,
    ))
    for step in steps:
        span = step["instruction"]
        for number, text in procedure_prose(lines, prose_visible, span, fence_starts=fence_starts):
            directive = WRITE_OUTPUT.fullmatch(text)
            if directive and not QUALIFIED_OUTPUT.search(directive[1]):
                attach(number, directive[2], step)

        facts = supplied.get(step["id"], {})
        require(isinstance(facts, dict), "Expected a step contract object")
        citations = array(facts.get("outputs", []), "step contract outputs", empty=True)
        if not citations:
            continue
        cited_prose = dict(procedure_prose(
            lines, prose_visible, span, include_conditionals=True, fence_starts=fence_starts,
        ))
        for output in citations:
            fields(output, ("line", "path", "sha256"), ("scope",), "output citation")
            require(
                sha256(output["sha256"]) == revision,
                "Runbook changed after recognizing its outputs", "CONFLICT",
            )
            number = integer(output["line"], "output citation line")
            value = string(output["path"], "output citation path")
            scope = choice(output.get("scope", "step"), ("step", "runbook"), "output citation scope")
            source = runbook_prose if scope == "runbook" else cited_prose
            require(
                number in source and names_output(source[number], value),
                f"Output citation for step {step['id']} must name {value!r} "
                f"in {scope} instructions at line {number}",
            )
            attach(number, value, step)
    return steps


def step_facts(root, raw, strict):
    fields(
        raw,
        (),
        (
            "procedure",
            "outputs",
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
    steps = inventory(
        lines, body, root, run_id, step_inventory=step_inventory, supplied=supplied,
        revision=revision,
    )
    ids = {step["id"] for step in steps}
    explicit = props["_explicitModelSettings"]
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
