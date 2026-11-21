"""Replay a local run journal without depending on an execution harness."""

from dataclasses import dataclass, field

from .model import LEGACY_JOURNAL_SCHEMA_VERSION, VERIFIED_OUTCOMES, RunbookError
from .validation import artifact, matches_digest, protected_path, require


@dataclass
class Attempt:
    event: int
    data: dict
    live: bool = True
    charged: bool = True
    superseded: bool = False
    question: int | None = None
    task: str | None = None
    lost_contact: bool = False
    failure: dict | None = None
    result: dict | None = None
    effects: str | None = None

    @property
    def mutation(self):
        return self.data.get("mutation")

    @property
    def effects_pending(self):
        return self.charged and self.mutation is not None and self.effects is None


@dataclass
class Step:
    contract: dict
    limit: int
    attempts: list = field(default_factory=list)
    assessment: dict | None = None
    invalidated: int | None = None

    @property
    def charged(self):
        return sum(attempt.charged for attempt in self.attempts)

    @property
    def latest(self):
        return next((a for a in reversed(self.attempts) if a.charged), None)

    @property
    def question(self):
        return next((a.question for a in self.attempts if a.question is not None), None)


class Run:
    def __init__(self, root, journal):
        self.root = root
        self.journal = journal
        self.run_id = journal["runId"]
        self.runbook = journal["runbook"]
        self.contract = journal["contract"]
        self.legacy = journal["schemaVersion"] == LEGACY_JOURNAL_SCHEMA_VERSION
        self.markdown_order = (
            not self.legacy and self.contract["stepOrder"] == "sequential"
            and self.contract["maxParallelSteps"] == 1
            and not any(step["dependsOn"] for step in self.contract["steps"])
        )
        self.events = journal["events"]
        self.steps = {
            step["id"]: Step(step, step["maxAttempts"]) for step in self.contract["steps"]
        }
        self.attempts = {}
        self.requests = {}
        self.questions = {}
        self.intents = {}
        self.model_overrides = {}
        self.pause = None
        self.pause_question = None
        self.terminal = None
        self.result = None
        self.owner = None
        for index, event in enumerate(self.events, start=1):
            require(
                type(event["seq"]) is int and event["seq"] == index
                and (index == 1) == (event["type"] == "reserve"),
                "Invalid journal event sequence",
                "IO_ERROR",
            )
            self.apply(event)

    @property
    def revision(self):
        return len(self.events)

    @property
    def available_slots(self):
        return self.contract["maxParallelSteps"] - sum(a.live for a in self.attempts.values())

    @property
    def phase(self):
        if self.terminal:
            return self.terminal
        if self.pause is not None:
            return "pausing" if any(a.live for a in self.attempts.values()) else "paused"
        return "active"

    def append(self, event):
        self.events.append(event)
        self.apply(event)

    def apply(self, event):
        seq, kind, data = event["seq"], event["type"], event["data"]
        if kind != "publication-intent":
            self.requests[event["requestId"]] = event
        getattr(self, "apply_" + kind.replace("-", "_"))(seq, data)

    def apply_reserve(self, seq, data):
        self.owner = self.events[seq - 1]

    apply_takeover = apply_reserve

    def apply_admit(self, seq, data):
        attempt = Attempt(seq, data)
        self.steps[data["stepId"]].attempts.append(attempt)
        self.attempts[seq] = attempt

    def apply_observe(self, seq, data):
        attempt = self.attempts[data["attempt"]]
        kind = data["kind"]
        if kind == "task":
            attempt.task = data["taskId"]
        elif kind == "lost-contact":
            attempt.lost_contact = True
        elif kind == "question":
            attempt.question = seq
            self.questions[seq] = {"text": data["text"], "attempt": attempt.event}
        else:
            attempt.live = False
            attempt.lost_contact = False
            if kind == "no-start":
                attempt.charged = False
                attempt.effects = "no-effect"
            elif kind == "technical-failure":
                attempt.failure = data
            elif kind == "result":
                attempt.result = {"event": seq, "digest": data["resultDigest"]}
                if "assessment" in data:
                    attempt.effects = "complete"
                    self.apply_assess(seq, data)

    def apply_set_pause(self, seq, data):
        if data["requested"]:
            self.pause = seq
            if "text" in data:
                self.questions[seq] = {"text": data["text"]}
                self.pause_question = seq
        else:
            self.pause = None

    def apply_resolve(self, seq, data):
        kind = data["kind"]
        if kind == "question":
            question = self.questions.pop(data["hold"])
            if "attempt" in question:
                self.attempts[question["attempt"]].question = None
            else:
                self.pause_question = None
        elif kind == "attempt":
            attempt = self.attempts[data["attempt"]]
            attempt.live = False
            attempt.lost_contact = False
            if "effects" in data:
                attempt.effects = data["effects"]
        else:
            intent = self.intents.pop(data["intent"])
            if data["disposition"] == "complete":
                original = intent["eventData"]
                if intent["operation"] == "observe":
                    self.apply_observe(seq, original)
                else:
                    self.apply_assess(seq, original)
                self.requests[intent["requestId"]] = {
                    "seq": seq,
                    "requestDigest": intent["requestDigest"],
                    "type": intent["operation"],
                }
            elif intent["operation"] == "observe":
                self.attempts[intent["eventData"]["attempt"]].effects = data["effects"]

    def apply_invalidate(self, seq, data):
        for sid in data["affected"]:
            step = self.steps[sid]
            step.assessment = None
            step.invalidated = seq
            for attempt in step.attempts:
                attempt.superseded = True
                if attempt.question is not None:
                    self.questions.pop(attempt.question)
                    attempt.question = None
        for intent in self.intents.values():
            if intent["eventData"]["stepId"] in data["affected"]:
                intent["stale"] = True

    def apply_assess(self, seq, data):
        step = self.steps[data["stepId"]]
        step.assessment = {"event": seq, **data["assessment"]}
        step.invalidated = None
        if "publication" in data:
            self.intents.pop(data["publication"])

    def apply_publication_intent(self, seq, data):
        self.intents[seq] = {**data, "stale": False}
        if data["operation"] == "observe":
            attempt = self.attempts[data["eventData"]["attempt"]]
            attempt.live = False
            attempt.lost_contact = False

    def apply_override(self, seq, data):
        if data["kind"] == "budget":
            self.steps[data["stepId"]].limit = data["limit"]
        else:
            self.model_overrides[data.get("stepId")] = data["modelFamily"]

    def apply_close(self, seq, data):
        self.terminal = "finalized" if data["mode"] == "finish" else "abandoned"
        self.result = data["result"]

    def step(self, sid):
        require(sid in self.steps, f"Unknown step: {sid}", "NOT_FOUND")
        return self.steps[sid]

    def model_settings(self, sid):
        contract = self.steps[sid].contract
        family = self.model_overrides.get(sid)
        if family is None and contract["modelFamilyOrigin"] != "step":
            family = self.model_overrides.get(None)
        settings = {
            key: contract[key]
            for key in ("modelFamily", "modelFamilyOrigin", "modelTier", "effortLevel",
                        "explicitTier", "explicitEffort", "delegateSteps")
        }
        if family is not None:
            settings.update(modelFamily=family, modelFamilyOrigin="operator")
        return settings

    def record_current(self, reference, *, deliverable=False):
        try:
            path = (protected_path(self.root, reference["path"], deliverable=True)
                    if deliverable else artifact(self.root, reference))
            return matches_digest(path, reference["sha256"])
        except (RunbookError, OSError, ValueError):
            return False

    def assessment_current(self, sid, cache):
        if sid not in cache:
            current = self.steps[sid].assessment
            cache[sid] = bool(current) and self.applicable(current, cache)
        return cache[sid]

    def current_assessment(self, sid, cache):
        return self.steps[sid].assessment if self.assessment_current(sid, cache) else None

    def next_markdown_step(self, cache):
        if self.markdown_order:
            return next((sid for sid in self.steps if not self.assessment_current(sid, cache)), None)
        return None

    def verified_outcome(self, outcome):
        status = VERIFIED_OUTCOMES[outcome]
        if not self.legacy:
            return status
        return "PASS" if status == "PASSED" else "FAIL"

    def applicable(self, assessment, cache):
        return all(
            self.record_current(reference)
            for reference in assessment["sources"]
        ) and all(
            self.record_current(reference, deliverable=True)
            for reference in assessment["deliverables"]
        ) and all(
            (prerequisite := self.current_assessment(sid, cache)) is not None
            and prerequisite["event"] == event
            for sid, event in assessment["prerequisites"].items()
        )

    def dependencies(self, step, cache):
        expected = {}
        for sid, condition in step.contract["dependsOn"].items():
            assessment = self.current_assessment(sid, cache)
            if assessment is None:
                return None
            if condition == "passed" and assessment["outcome"] != self.verified_outcome("PASS"):
                return None
            expected[sid] = assessment["event"]
        return expected

    def related_intent(self, sid):
        return any(intent["eventData"]["stepId"] == sid for intent in self.intents.values())

    def blockers(self, sid, cache, *, effects=False):
        step = self.steps[sid]
        blockers = []
        if self.phase != "active":
            blockers.append(self.phase)
        if step.assessment:
            blockers.append("done" if self.assessment_current(sid, cache) else "applicability")
        if step.question is not None:
            blockers.append("question")
        if any(attempt.live and not attempt.superseded for attempt in step.attempts):
            blockers.append("attempt")
        if self.available_slots <= 0:
            blockers.append("capacity")
        if step.charged >= step.limit:
            blockers.append("budget")
        if self.dependencies(step, cache) is None:
            blockers.append("prerequisites")
        if step.invalidated and step.charged and not step.contract["reassessAfterInvalidation"]:
            blockers.append("reassessment")
        if self.related_intent(sid):
            blockers.append("publication")
        if any(attempt.effects_pending for attempt in step.attempts):
            blockers.append("effects")
        if any((effects or attempt.mutation) and (attempt.live or attempt.effects_pending)
               for attempt in self.attempts.values() if attempt.data["stepId"] != sid
               or attempt.superseded):
            blockers.append("interference")
        return blockers

    def score(self, cache):
        assessments = [
            step.assessment["score"] for sid, step in self.steps.items()
            if step.assessment and (self.terminal or self.assessment_current(sid, cache))
        ]
        return {key: sum(item[key] for item in assessments) for key in ("earned", "available")}

    def attempt_projection(self, attempt):
        return {
            "event": attempt.event,
            **attempt.data,
            "live": attempt.live,
            "charged": attempt.charged,
            "superseded": attempt.superseded,
            "taskId": attempt.task,
            "lostContact": attempt.lost_contact,
            "effects": attempt.effects,
            "failure": attempt.failure,
            "result": attempt.result,
        }

    def step_projection(self, sid, step, cache, next_markdown_step):
        current = step.assessment and (self.terminal or self.assessment_current(sid, cache))
        active = next((a for a in reversed(step.attempts) if a.live and not a.superseded), None)
        blockers = [] if current or self.terminal else self.blockers(sid, cache)
        if self.markdown_order and not current and not self.terminal and sid != next_markdown_step:
            blockers.append("step-order")
        latest = step.latest
        if (not blockers and not current and latest and latest.failure
                and not latest.failure["transient"]):
            blockers.append("operator-retry")
        question = step.question
        return {
            "id": sid,
            "status": "done" if current else "active" if active else "pending",
            "assessment": step.assessment if current else None,
            "attempt": active.event if active else None,
            "attempts": [self.attempt_projection(attempt) for attempt in step.attempts],
            "charged": step.charged,
            "limit": step.limit,
            "blockers": blockers,
            "question": question,
            "questionText": self.questions[question]["text"] if question is not None else None,
            **self.model_settings(sid),
        }

    def projection(self):
        cache = {}
        next_markdown_step = None if self.terminal else self.next_markdown_step(cache)
        steps = [
            self.step_projection(sid, step, cache, next_markdown_step)
            for sid, step in self.steps.items()
        ]
        recovery = [
            {"attempt": a.event, "stepId": a.data["stepId"], "live": a.live,
             "lostContact": a.lost_contact, "effectsPending": a.effects_pending}
            for a in self.attempts.values() if a.live or a.effects_pending
        ]
        return {
            "ok": True,
            "schemaVersion": self.journal["schemaVersion"],
            "runId": self.run_id,
            "revision": self.revision,
            "phase": self.phase,
            "pause": self.pause,
            "pauseQuestion": {"event": self.pause_question, **self.questions[self.pause_question]}
            if self.pause_question else None,
            "result": self.result,
            "score": self.score(cache),
            "steps": steps,
            "ready": [step["id"] for step in steps
                      if self.phase == "active" and step["status"] == "pending"
                      and not step["blockers"]],
            "availableSlots": self.available_slots,
            "recovery": recovery,
            "publications": [
                {"event": key, "stepId": intent["eventData"]["stepId"], "stale": intent["stale"],
                 "targets": [item["path"] for item in intent["artifacts"]]}
                for key, intent in self.intents.items()
            ],
            "runbook": self.runbook,
            "contract": self.contract,
        }
