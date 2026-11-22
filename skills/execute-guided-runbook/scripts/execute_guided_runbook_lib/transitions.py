"""Validate one successor journal transition before making any writes."""

from secrets import compare_digest, token_urlsafe

from .contract import GAP_CAUSES
from .model import EFFORT_LEVELS, MODEL_TIERS, VERIFIED_OUTCOMES
from .model_binding import normalize_model_family, normalize_model_id
from .validation import (
    array,
    artifact,
    boolean,
    choice,
    digest,
    fields,
    integer,
    relative_name,
    require,
    require_decision,
    string,
    uuid_string,
)

TRANSITIONS = (
    "takeover", "admit", "observe", "resolve", "set-pause", "invalidate", "assess", "override",
    "close",
)


GAP_OUTCOMES = {
    "failed-prerequisite": "SKIPPED",
    "exhausted": "EXHAUSTED",
    "invalidated": "INVALIDATED",
}


def evidence(value):
    fields(value, ("statement", "reference"), label="evidence")
    string(value["statement"], "evidence.statement")
    string(value["reference"], "evidence.reference")
    return value


def blocked(condition, message, **details):
    require(condition, message, "BLOCKED", **details)


def consistent(condition, message, **details):
    require(condition, message, "CONFLICT", **details)


def mutation(value):
    fields(value, ("target", "preconditions", "verify"), label="mutation")
    for key in ("target", "preconditions", "verify"):
        string(value[key], "mutation." + key)


def same_tenure_replay(run, event, request_digest):
    consistent(event["requestDigest"] == request_digest, "Request ID was reused")
    response = run.projection() | {"duplicate": True, "committedRevision": event["seq"]}
    if event["type"] in {"reserve", "takeover"}:
        consistent(event["seq"] == run.owner["seq"], "Coordinator tenure was superseded")
        response["ownershipToken"] = run.owner["data"]["ownershipToken"]
    return response


def prepare_change(run, request):
    fields(
        request, ("requestId", "ifRevision", "type", "data"), ("ownershipToken",), "change",
    )
    rid = uuid_string(request["requestId"], "requestId")
    rev = integer(request["ifRevision"], "ifRevision")
    kind = choice(request["type"], TRANSITIONS, "transition type")
    require(isinstance(request["data"], dict), "Transition data must be an object")
    if kind == "takeover":
        require("ownershipToken" not in request, "Omit ownershipToken for takeover")
    else:
        token = request.get("ownershipToken")
        consistent(
            isinstance(token, str)
            and compare_digest(token.encode("utf-8"), run.owner["data"]["ownershipToken"].encode()),
            "The current coordinator token is required",
        )
    request_digest = digest(request | {"requestId": rid})
    if previous := run.requests.get(rid):
        return same_tenure_replay(run, previous, request_digest), None
    for intent in run.intents.values():
        if intent["requestId"] == rid:
            consistent(request_digest == intent["requestDigest"], "Request ID was reused")
            blocked(False, "The original request has an unresolved publication intent")
    consistent(rev == run.revision, "Journal revision has changed", revision=run.revision)
    if run.terminal:
        data = request["data"]
        can_recover = run.terminal == "abandoned" and run.pending_effects and (
            (kind == "takeover" and data.get("recoveryOnly") is True)
            or (kind == "resolve" and data.get("kind") == "attempt" and "effects" in data)
        )
        consistent(can_recover, "The run is already closed")
    return None, (rid, request_digest, kind)


class Transitions:
    def __init__(self, run, repository):
        self.run = run
        self.repository = repository

    def take(self, kind, data):
        return getattr(self, kind.replace("-", "_"))(data)

    def takeover(self, data):
        fields(data, ("evidence",), ("recoveryOnly",), "takeover")
        evidence(data["evidence"])
        if "recoveryOnly" in data:
            boolean(data["recoveryOnly"], "recoveryOnly")
        return data | {"ownershipToken": token_urlsafe(32)}

    def ready_inputs(self, step, data):
        sources = array(data["sources"], "sources", empty=True)
        for item in sources:
            artifact(self.run.root, item, verify=True)
        prerequisites = data["prerequisites"]
        fields(prerequisites, (), step.contract["dependsOn"], "prerequisites")
        for ref in prerequisites.values():
            integer(ref, "prerequisite assessment")
        expected = self.run.dependencies(step, {})
        blocked(expected is not None and prerequisites == expected, "Prerequisites changed")

    def require_markdown_turn(self, sid):
        if self.run.markdown_order:
            next_step = self.run.next_markdown_step({})
            blocked(
                next_step == sid, "Complete earlier Markdown steps first",
                blockers=["step-order"], nextStep=next_step,
            )

    def admit(self, data):
        fields(
            data, ("stepId", "execution", "sources", "prerequisites"),
            ("model", "mutation", "decision"), "admission",
        )
        sid = string(data["stepId"], "stepId")
        step = self.run.step(sid)
        self.require_markdown_turn(sid)
        execution = choice(data["execution"], ("inline", "delegated"), "execution")
        if "mutation" in data:
            mutation(data["mutation"])
        blockers = self.run.blockers(sid, {}, effects="mutation" in data)
        blocked(not blockers, f"Step is not ready: {sid}", blockers=blockers)
        if step.latest and step.latest.failure and not step.latest.failure["transient"] or "decision" in data:
            require_decision(data)
        if (
            (self.run.legacy or self.run.contract["maxParallelSteps"] == 1)
            and self.run.contract["stepOrder"] == "sequential"
            and "decision" not in data
        ):
            ready = self.run.projection()["ready"]
            blocked(ready and ready[0] == sid, "Select the first ready step", ready=ready)
        self.ready_inputs(step, data)
        settings = self.run.model_settings(sid)
        blocked(
            execution == "delegated" or not settings["delegateSteps"],
            "This step requires a fresh delegated worker",
        )
        self.check_model(settings, data.get("model", {}))
        return data | {"number": len(step.attempts) + 1, "requirements": settings}

    @staticmethod
    def check_model(settings, actual):
        fields(actual, (), ("modelId", "modelFamily", "modelTier", "effortLevel"), "model")
        if "modelId" in actual:
            normalize_model_id(actual["modelId"])
        actual_family = (
            normalize_model_family(actual["modelFamily"]) if "modelFamily" in actual else None
        )
        family = settings["modelFamily"]
        if family.startswith("exact:"):
            blocked(actual.get("modelId") == family[6:], f"Required exact model: {family[6:]!r}")
        elif family != "current":
            blocked(
                actual.get("modelId") and actual_family == family,
                f"The harness must establish the selected model for {family}",
            )
        for field, flag, choices in (
            ("modelTier", "explicitTier", MODEL_TIERS),
            ("effortLevel", "explicitEffort", EFFORT_LEVELS),
        ):
            if field in actual:
                choice(actual[field], choices, field)
            if settings[flag]:
                blocked(
                    field in actual and choices.index(actual[field]) >= choices.index(settings[field]),
                    f"The harness must establish the required {field}: {settings[field]}",
                )

    def attempt(self, number):
        number = integer(number, "attempt")
        require(number in self.run.attempts, f"Unknown attempt: {number}", "NOT_FOUND")
        return self.run.attempts[number]

    def observe(self, data):
        require("attempt" in data and "kind" in data, "An observation needs attempt and kind")
        attempt = self.attempt(data["attempt"])
        kind = choice(
            data["kind"],
            ("task", "question", "lost-contact", "no-start", "stopped", "technical-failure", "result"),
            "observation kind",
        )
        if kind == "result":
            return self.result(data, attempt)
        required = {
            "task": ("taskId",),
            "question": ("text",),
            "technical-failure": ("evidence", "reason", "transient"),
        }.get(kind, ("evidence",))
        fields(data, ("attempt", "kind", *required), label="observation")
        blocked(attempt.live, "The attempt has already settled")
        if kind == "task":
            string(data["taskId"], "taskId")
            consistent(attempt.task in {None, data["taskId"]}, "The attempt has a different task ID")
        elif kind == "question":
            string(data["text"], "question")
            blocked(attempt.question is None and not attempt.superseded, "No new question can be opened")
        else:
            evidence(data["evidence"])
        if kind == "technical-failure":
            boolean(data["transient"], "transient")
            string(data["reason"], "reason")
        if kind == "no-start":
            blocked(attempt.question is None, "A worker already asked a question")
        return data

    @staticmethod
    def score(outcome, step, supplied):
        available = step.contract["points"]
        passed = VERIFIED_OUTCOMES[outcome] == "PASSED"
        value = supplied if supplied is not None else {
            "earned": available if passed else 0, "available": available,
        }
        fields(value, ("earned", "available"), label="score")
        earned = integer(value["earned"], "score.earned", minimum=0)
        require(
            integer(value["available"], "score.available") == available
            and (earned == available if passed else earned < available),
            "Score must match the outcome and frozen point contract",
        )
        return value

    def result(self, data, attempt):
        allowed = ("PASS", "FAIL") if self.run.legacy else VERIFIED_OUTCOMES
        optional = ("score", "deliverables", "model") if self.run.legacy else (
            "score", "deliverables", "model", "reason",
        )
        fields(data, ("attempt", "kind", "outcome", "evidence"), optional, "result")
        outcome = self.run.verified_outcome(choice(data["outcome"], allowed, "outcome"))
        observed = evidence(data["evidence"])
        if "model" in data:
            fields(data["model"], (), ("modelId", "modelFamily", "modelTier", "effortLevel"), "model")
            self.check_model(attempt.data["requirements"], attempt.data.get("model", {}) | data["model"])
        sid = attempt.data["stepId"]
        step = self.run.steps[sid]
        score = self.score(outcome, step, data.get("score"))
        result_digest = digest(data)
        if attempt.result:
            consistent(
                attempt.result["digest"] == result_digest,
                "A different result was already recorded for the attempt",
            )
            return {"duplicateResult": attempt.result["event"]}
        blocked(attempt.charged, "A never-started attempt cannot produce a result")
        blocked(not self.run.related_intent(sid), "Reconcile this step's publication intent first")
        blocked(attempt.question is None, "Answer the step question before accepting its result")
        inputs = {key: attempt.data[key] for key in ("sources", "prerequisites")}
        event = data | {"stepId": sid, "resultDigest": result_digest, "outcome": outcome}
        if not self.run.legacy:
            event["reason"] = string(data.get("reason", observed["statement"]), "reason")
        if (attempt.superseded or step.latest is not attempt or step.assessment
                or not self.run.applicable(inputs | {"deliverables": []}, {})):
            return event | {"historical": True}
        outputs = self.repository.prepare_publication(sid, data.get("deliverables", []), self.run,
                                                      str(attempt.event))
        event["assessment"] = inputs | {
            "outcome": outcome,
            "score": score,
            "deliverables": [{"path": item["path"], "sha256": item["sha256"]} for item in outputs],
            "attempt": attempt.event,
        }
        if not self.run.legacy:
            event["assessment"].update(reason=event["reason"], evidence=observed)
        return event | {"_outputs": outputs}

    def set_pause(self, data):
        fields(data, ("requested",), ("decision", "text"), "pause")
        if boolean(data["requested"], "requested"):
            if "text" in data:
                require("decision" not in data, "Specify either a question or a pause decision")
                string(data["text"], "run question")
                blocked(self.run.pause_question is None, "Answer the current run question first")
            else:
                require_decision(data)
        else:
            require("text" not in data, "Resume does not open a question")
            require_decision(data)
            blocked(self.run.phase == "paused", "Current work must settle before resuming")
            blocked(self.run.pause_question is None, "The run-wide question is still open")
        return data

    def resolve(self, data):
        kind = choice(data.get("kind"), ("question", "attempt", "publication"), "resolution kind")
        if kind == "question":
            fields(data, ("kind", "hold", "decision"), label="question resolution")
            require_decision(data)
            hold = integer(data["hold"], "hold")
            require(hold in self.run.questions, "Question is not open", "NOT_FOUND")
        elif kind == "attempt":
            fields(data, ("kind", "attempt", "evidence"), ("effects", "decision"), "attempt resolution")
            attempt = self.attempt(data["attempt"])
            evidence(data["evidence"])
            if "effects" in data:
                blocked(attempt.effects_pending, "The attempt has no unresolved effects")
                choice(data["effects"], ("no-effect", "safe-repeat"), "effects")
            if "decision" in data:
                require_decision(data)
        else:
            return self.resolve_publication(data)
        return data

    def resolve_publication(self, data):
        fields(
            data, ("kind", "intent", "evidence", "disposition"), ("effects", "decision"),
            "publication resolution",
        )
        intent_id = integer(data["intent"], "intent")
        require(intent_id in self.run.intents, "Publication intent is not open", "NOT_FOUND")
        intent = self.run.intents[intent_id]
        evidence(data["evidence"])
        disposition = choice(data["disposition"], ("complete", "accounted"), "disposition")
        if disposition == "complete":
            assessment = intent["eventData"]["assessment"]
            blocked(
                not intent["stale"]
                and self.run.applicable(assessment | {"deliverables": []}, {}),
                "The publication no longer applies to the current inputs and assessments",
            )
            require("effects" not in data and "decision" not in data,
                    "Completion reuses the recorded publication authority")
        else:
            require_decision(data)
            if intent["operation"] == "observe":
                choice(data.get("effects"), ("no-effect", "safe-repeat"), "effects")
            else:
                require("effects" not in data, "A reused assessment has no execution effects")
        return data

    def invalidate(self, data):
        fields(data, ("cause",), ("steps", "references"), "invalidation")
        string(data["cause"], "cause")
        affected = set()
        for sid in array(data.get("steps", []), "steps", empty=True):
            self.run.step(string(sid, "stepId"))
            affected.add(sid)
        targets = set()
        for item in array(data.get("references", []), "references", empty=True):
            artifact(self.run.root, item, historical=True)
            targets.add((relative_name(item["path"]), item["sha256"]))
        if targets:
            for sid, step in self.run.steps.items():
                records = [a.data for a in step.attempts]
                if step.assessment:
                    records.append(step.assessment)
                records.extend(intent["eventData"]["assessment"]
                               for intent in self.run.intents.values()
                               if intent["eventData"]["stepId"] == sid)
                if any(
                    (relative_name(ref["path"]), ref["sha256"]) in targets
                    for record in records for key in ("sources", "deliverables")
                    for ref in record.get(key, [])
                ):
                    affected.add(sid)
        require(affected, "No recorded work matches this invalidation", "NOT_FOUND")
        while True:
            dependents = {
                sid for sid, step in self.run.steps.items()
                if affected.intersection(step.contract["dependsOn"])
            }
            if dependents <= affected:
                break
            affected.update(dependents)
        return data | {"affected": sorted(affected)}

    def assess(self, data):
        kind = choice(data.get("kind"), ("reuse", "gap"), "assessment kind")
        sid = string(data.get("stepId"), "stepId")
        step = self.run.step(sid)
        self.require_markdown_turn(sid)
        blocked(
            not step.assessment and step.question is None and not self.run.related_intent(sid)
            and not any(a.live or a.effects_pending for a in step.attempts),
            "Settle the step's existing work, effects and holds first",
        )
        if kind == "gap":
            return self.gap(sid, step, data)
        fields(
            data, ("kind", "stepId", "source", "sources", "prerequisites", "evidence"),
            ("deliverables",), "reuse",
        )
        blocked(step.contract["reuse"], "The frozen procedure does not authorize reuse")
        evidence(data["evidence"])
        self.ready_inputs(step, data)
        previous = self.repository.reuse_record(data["source"], sid, self.run)
        outcome = self.run.verified_outcome(previous["outcome"])
        score = self.score(outcome, step, previous["score"])
        outputs = self.repository.prepare_publication(
            sid, data.get("deliverables", []), self.run, "reuse",
        )
        previous_outputs = {item["path"]: item["sha256"] for item in previous["deliverables"]}
        source_run_id = previous.get("runId", self.run.run_id)
        for item in outputs:
            original = item["path"].replace(self.run.run_id, source_run_id)
            recorded = previous_outputs.get(item["path"], previous_outputs.get(original))
            blocked(recorded in {None, item["sha256"]},
                    f"Reusable bytes do not match the prior artifact for {item['path']}")
        blocked(
            sorted(item["sha256"] for item in outputs) == sorted(previous_outputs.values()),
            "Reusable output digests must match the receiving run's staged bytes",
        )
        assessment = {
            **{key: data[key] for key in ("sources", "prerequisites")},
            "outcome": outcome,
            "score": score,
            "deliverables": [{"path": item["path"], "sha256": item["sha256"]} for item in outputs],
            "reuse": data["source"],
        }
        if not self.run.legacy:
            assessment.update(reason=data["evidence"]["statement"], evidence=data["evidence"])
        return data | {"_outputs": outputs, "assessment": assessment}

    def gap(self, sid, step, data):
        fields(data, ("kind", "stepId", "cause", "trigger"), label="gap")
        cause = choice(data["cause"], GAP_CAUSES, "gap cause")
        blocked(cause in step.contract["gapCauses"], "The frozen procedure does not authorize this gap")
        blocked(not step.contract["deliverables"], "A gap cannot fulfill unresolved mandatory outputs")
        trigger = integer(data["trigger"], "trigger")
        require(trigger <= self.run.revision, "Unknown gap trigger", "NOT_FOUND")
        event = self.run.events[trigger - 1]
        prerequisites = {}
        if cause == "failed-prerequisite":
            if not self.run.legacy:
                blocked(not step.charged, "A skipped step must never have started")
            failing = (
                {"FAIL", "NOT_ASSESSED"} if self.run.legacy
                else {"FAILED", *GAP_OUTCOMES.values()}
            )
            for key, condition in step.contract["dependsOn"].items():
                if condition != "passed":
                    continue
                record = self.run.current_assessment(key, {})
                if (
                    record is not None
                    and record["event"] == trigger
                    and record["outcome"] in failing
                ):
                    prerequisites[key] = trigger
            blocked(prerequisites, "A gap needs a current failing required prerequisite")
            reason = f"Required prerequisite {next(iter(prerequisites))} did not pass"
        elif cause == "exhausted":
            latest = step.latest
            blocked(
                latest and step.charged >= step.limit and not latest.result
                and event["type"] in {"observe", "resolve"}
                and event["data"].get("attempt") == latest.event
                and event["data"].get("kind") in {"technical-failure", "stopped", "attempt"},
                "A gap needs the current exhausted attempt's settlement",
            )
            prerequisites = self.run.dependencies(step, {})
            blocked(prerequisites is not None, "Prerequisites of the exhausted step changed")
            reason = event["data"].get("reason", "Attempt budget exhausted after verified settlement")
        else:
            blocked(
                trigger == step.invalidated and not step.contract["reassessAfterInvalidation"],
                "The gap must reference the invalidation requiring a separate run",
            )
            reason = event["data"]["cause"]
            if not self.run.legacy:
                affected = event["data"]["affected"]
                blocked(
                    not any(
                        attempt.superseded and (attempt.live or attempt.effects_pending)
                        for sid in affected for attempt in self.run.steps[sid].attempts
                    ) and not any(
                        intent["stale"] and intent["eventData"]["stepId"] in affected
                        for intent in self.run.intents.values()
                    ),
                    "Settle affected work and reconcile stale publications first",
                )
        assessment = {
            "outcome": "NOT_ASSESSED" if self.run.legacy else GAP_OUTCOMES[cause],
            "score": {"earned": 0, "available": step.contract["points"]},
            "sources": [], "prerequisites": prerequisites, "deliverables": [],
            "cause": cause, "trigger": trigger, "procedure": step.contract["procedure"],
        }
        if not self.run.legacy:
            assessment.update(reason=reason, evidence={
                "statement": reason, "reference": f"journal:{self.run.run_id}#{trigger}",
            })
        return data | {"assessment": assessment}

    def override(self, data):
        kind = choice(data.get("kind"), ("budget", "model"), "override kind")
        if kind == "budget":
            fields(data, ("kind", "stepId", "limit", "decision"), label="budget override")
            step = self.run.step(string(data["stepId"], "stepId"))
            require(integer(data["limit"], "limit") > step.limit, "Increase the current limit")
        else:
            fields(data, ("kind", "modelFamily", "decision"), ("stepId",), "model override")
            if "stepId" in data:
                self.run.step(string(data["stepId"], "stepId"))
            data = data | {"modelFamily": normalize_model_family(data["modelFamily"])}
        require_decision(data)
        return data

    def close(self, data):
        fields(data, ("mode",), ("decision",), "close")
        mode = choice(data["mode"], ("finish", "abandon"), "close mode")
        blocked(not any(a.live for a in self.run.attempts.values()), "Current work has not settled")
        blocked(not self.run.intents, "Reconcile publication intents before closing")
        if mode == "abandon":
            blocked(not self.run.pending_effects, "Reconcile outstanding effects before abandoning")
            require_decision(data)
            return data | {"result": "ABANDONED"}
        blocked(
            not self.run.questions and not self.run.pending_effects,
            "Resolve outstanding questions and effects before finalizing",
        )
        cache = {}
        incomplete = [sid for sid in self.run.steps if not self.run.assessment_current(sid, cache)]
        blocked(not incomplete, "Every step and mandatory output needs current verification",
                steps=incomplete)
        score = self.run.score(cache)
        earned, available = score["earned"], score["available"]
        policy = self.run.contract["acceptancePolicy"]
        percentage = self.run.contract["acceptanceThreshold"]
        threshold = int(percentage.removesuffix("%")) if percentage is not None else None
        if earned == available:
            result = "PASSED"
        elif policy == "strict":
            result = "REJECTED"
        elif policy == "always" or (threshold is not None and earned * 100 >= available * threshold):
            result = "ACCEPTED"
        else:
            require_decision(data, closing=True)
            result = "ACCEPTED" if data["decision"]["choice"] == "accept" else "REJECTED"
        if "decision" in data:
            require_decision(data, closing=True)
        return data | {"result": result, "score": score}
