from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "execute-guided-runbook"
    / "scripts"
    / "runbook_session.py"
)
RUNBOOK = "run.md"
DECISION = {"instruction": "Proceed within scope", "reference": "operator:test"}
STOPPED = {
    "statement": "Worker and its commands have ended",
    "reference": "test:worker-exit",
}
FENCED = {
    "statement": "The former coordinator stopped and cannot submit work",
    "reference": "test:coordinator-exit",
}
RETRYABLE = {"BLOCKED", "CONFLICT", "DECISION_REQUIRED"}


def run_cli(root, *command, payload=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(root), *command],
        input=json.dumps(payload) if payload is not None else "",
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=20,
    )


def call(root, *command, payload=None, error=None):
    process = run_cli(root, *command, payload=payload)
    assert "Traceback" not in process.stderr, process.stderr
    body = json.loads(process.stdout)
    if error is not None:
        expected = (error,) if isinstance(error, str) else error
        assert body["ok"] is False and body["code"] in expected, body
        assert process.returncode == (2 if body["code"] in RETRYABLE else 1), body
        assert isinstance(body["details"], dict), body
    else:
        assert process.returncode == 0 and body["ok"] is True, body
    return body


class Client:
    def __init__(self, root, *steps, config="", runbook=RUNBOOK):
        self.root = root
        self.runbook = runbook
        header = f"---\n{config.strip()}\n---\n\n" if config else ""
        instructions = "".join(
            f"\n<!-- step: {step} -->\n## {step.title()}\n\nRead the supplied inputs.\n"
            for step in steps
        )
        (root / self.runbook).write_text(f"{header}# Procedure\n{instructions}", encoding="utf-8")
        self.run_id = str(uuid4())
        self.initial = {"runId": self.run_id, "stepContracts": {}}
        self.view = {}
        self.token = None

    def reserve(self, *, selector=None):
        self.view = call(self.root, "reserve", selector or self.runbook, payload=self.initial)
        self.token = self.view["ownershipToken"]
        return self.view

    def status(self, selector=None):
        self.view = call(self.root, "status", selector or self.run_id)
        return self.view

    def request(self, kind, data):
        request = {
            "requestId": str(uuid4()),
            "ifRevision": self.view["revision"],
            "type": kind,
            "data": data,
        }
        if kind != "takeover":
            request["ownershipToken"] = self.token
        return request

    def change(self, kind, data, *, error=None):
        response = call(
            self.root, "change", self.run_id, payload=self.request(kind, data), error=error
        )
        if error is None:
            self.view = response
            if kind == "takeover":
                self.token = response["ownershipToken"]
        return response

    def step(self, step_id):
        return next(step for step in self.view["steps"] if step["id"] == step_id)

    def admit(self, step_id, *, execution="inline", prerequisites=None, error=None):
        response = self.change(
            "admit", admission(step_id, execution=execution, prerequisites=prerequisites), error=error
        )
        return response if error else self.step(step_id)["attempt"]

    def observe(self, attempt, kind, **data):
        return self.change("observe", {"attempt": attempt, "kind": kind, **data})

    def result(self, attempt, outcome="PASS"):
        return self.observe(attempt, "result", outcome=outcome, evidence=STOPPED)

    def pause(self, requested=True, *, error=None):
        return self.change("set-pause", {"requested": requested, "decision": DECISION}, error=error)


def admission(step, *, execution="inline", prerequisites=None):
    return {
        "stepId": step,
        "execution": execution,
        "sources": [],
        "prerequisites": prerequisites or {},
    }


def test_parallel_dependencies_and_drain_pause(tmp_path):
    client = Client(
        tmp_path,
        "alpha",
        "beta",
        "report",
        config="""\
maxParallelSteps: 2
delegateSteps: true
steps:
  report:
    dependsOn:
      alpha: done
      beta: done
""",
    )
    assert client.reserve()["phase"] == "active"
    assert client.view["availableSlots"] == 2
    assert "report" not in client.view["ready"]
    second = client.admit("beta", execution="delegated")
    first = client.admit("alpha", execution="delegated")
    assert first != second and client.view["availableSlots"] == 0

    assert client.pause()["phase"] == "pausing"
    second_result = client.result(second)
    assert second_result["phase"] == "pausing"
    duplicate = client.result(second)
    assert duplicate["duplicate"] is True
    assert duplicate["committedRevision"] == second_result["committedRevision"]
    assert duplicate["score"]["earned"] == 1
    assert "report" not in client.view["ready"]
    client.result(first)
    assert client.view["phase"] == "paused"
    client.admit("report", execution="delegated", error=("BLOCKED", "CONFLICT"))

    assert client.pause(False)["phase"] == "active"
    assert "report" in client.view["ready"]
    prerequisites = {step: client.step(step)["assessment"]["event"] for step in ("alpha", "beta")}
    client.result(client.admit("report", execution="delegated", prerequisites=prerequisites))
    assert client.change("close", {"mode": "finish"})["result"] == "PASSED"
    assert client.status()["result"] == "PASSED"


def test_reservation_preserves_canonical_id_and_open_alias(tmp_path):
    client = Client(tmp_path, "step")
    client.initial["runId"] = client.run_id.upper()
    call(tmp_path, "init")
    call(tmp_path, "register", "procedure", RUNBOOK)
    assert client.reserve(selector="procedure")["runId"] == client.run_id
    assert client.status(RUNBOOK)["runId"] == client.run_id
    assert client.status("procedure")["runId"] == client.run_id
    call(tmp_path, "reserve", RUNBOOK, payload={"runId": str(uuid4())}, error="CONFLICT")
    journal = tmp_path / ".runbooks" / "history" / client.run_id / "journal.json"
    assert json.loads(journal.read_bytes())["schemaVersion"] == 5


def test_replay_uses_current_tenure_and_original_committed_revision(tmp_path):
    client = Client(tmp_path, "step")
    reservation = client.reserve()
    old_token = client.token
    assert call(tmp_path, "reserve", RUNBOOK, payload=client.initial)["ownershipToken"] == old_token
    changed_reservation = client.initial | {"stepContracts": {"step": {"points": 1}}}
    call(tmp_path, "reserve", RUNBOOK, payload=changed_reservation, error="CONFLICT")

    admission_request = client.request("admit", admission("step"))
    admitted = call(tmp_path, "change", client.run_id, payload=admission_request)
    client.view = admitted
    assert client.pause()["phase"] == "pausing"
    replayed = call(tmp_path, "change", client.run_id, payload=admission_request)
    assert replayed["duplicate"] is True
    assert replayed["committedRevision"] == admitted["committedRevision"]
    assert replayed["revision"] == client.view["revision"]
    assert replayed["phase"] == "pausing"
    assert replayed["steps"][0]["charged"] == 1

    changed = admission_request | {"data": admission("step", execution="delegated")}
    call(tmp_path, "change", client.run_id, payload=changed, error="CONFLICT")
    stale = client.request("set-pause", {"requested": False, "decision": DECISION})
    stale["ifRevision"] = reservation["revision"]
    call(tmp_path, "change", client.run_id, payload=stale, error="CONFLICT")

    client.change("takeover", {}, error=("INVALID_REQUEST", "BLOCKED", "DECISION_REQUIRED"))
    first_takeover = client.request("takeover", {"evidence": FENCED})
    client.view = call(tmp_path, "change", client.run_id, payload=first_takeover)
    first_token = client.view["ownershipToken"]
    assert first_token != old_token
    retry = call(tmp_path, "change", client.run_id, payload=first_takeover)
    assert retry["ownershipToken"] == first_token
    assert retry["duplicate"] is True

    client.change("takeover", {"evidence": FENCED})
    assert client.token not in {old_token, first_token}
    for command, payload in (
        (("change", client.run_id), admission_request),
        (("change", client.run_id), first_takeover),
        (("reserve", RUNBOOK), client.initial),
    ):
        failure = call(tmp_path, *command, payload=payload, error="CONFLICT")
        assert client.token not in json.dumps(failure)
    assert "ownershipToken" not in client.status()
    assert client.token not in json.dumps(client.view)


def test_concurrent_takeovers_cannot_both_commit(tmp_path):
    client = Client(tmp_path, "step")
    client.reserve()
    requests = [client.request("takeover", {"evidence": FENCED}) for _ in range(2)]

    def attempt_takeover(request):
        process = run_cli(tmp_path, "change", client.run_id, payload=request)
        return process.returncode, json.loads(process.stdout)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(attempt_takeover, requests))
    assert sorted(code for code, _ in responses) == [0, 2], responses
    winner = next(body for code, body in responses if code == 0)
    loser = next(body for code, body in responses if code == 2)
    assert loser["code"] == "CONFLICT"
    assert winner["ownershipToken"] not in json.dumps(loser)
    assert client.status()["revision"] == winner["revision"]


def test_live_question_holds_slot_and_parked_question_survives_pause(tmp_path):
    client = Client(
        tmp_path,
        "waiting",
        "free",
        "dependent",
        config="""\
maxAttempts: 2
steps:
  dependent:
    dependsOn:
      waiting: assessed
""",
    )
    client.reserve()
    active = client.admit("waiting")
    question = client.observe(active, "question", text="May this step continue?")
    assert question["availableSlots"] == 0
    assert "free" not in question["ready"]
    assert client.pause()["phase"] == "pausing"

    parked = client.observe(active, "stopped", evidence=STOPPED)
    assert parked["phase"] == "paused"
    assert parked["availableSlots"] == 1
    assert client.pause(False)["phase"] == "active"
    assert "free" in client.view["ready"]
    assert "waiting" not in client.view["ready"] and "dependent" not in client.view["ready"]
    client.result(client.admit("free"))

    client.change(
        "resolve",
        {"kind": "question", "hold": question["committedRevision"], "decision": DECISION},
    )
    assert "waiting" in client.view["ready"]
    assert "dependent" not in client.view["ready"]


def test_late_invalidated_result_settles_without_restoring_credit(tmp_path):
    client = Client(
        tmp_path,
        "input",
        "middle",
        "late",
        config="""\
maxParallelSteps: 2
steps:
  middle:
    dependsOn:
      input: passed
  late:
    dependsOn:
      middle: passed
""",
    )
    client.reserve()
    client.result(client.admit("input"))
    first = client.step("input")["assessment"]["event"]
    client.result(client.admit("middle", prerequisites={"input": first}))
    second = client.step("middle")["assessment"]["event"]
    late = client.admit("late", prerequisites={"middle": second})

    withdrawal = client.change("invalidate", {"steps": ["input"], "cause": "Source changed"})
    assert all(step["status"] != "done" for step in withdrawal["steps"])
    assert withdrawal["availableSlots"] == 1
    assert client.pause()["phase"] == "pausing"

    result = client.result(late)
    assert result["phase"] == "paused"
    assert result["availableSlots"] == 2
    assert result["score"]["earned"] == 0
    assert all(step["status"] != "done" for step in result["steps"])
    repeated = client.result(late)
    assert repeated["duplicate"] is True
    assert repeated["committedRevision"] == result["committedRevision"]


def test_no_start_releases_charge_but_lost_contact_keeps_it(tmp_path):
    client = Client(tmp_path, "step")
    client.reserve()
    began = client.admit("step")
    client.observe(
        began,
        "no-start",
        evidence={"statement": "Harness rejected the send", "reference": "test:no-start"},
    )
    assert client.step("step")["charged"] == 0
    assert client.view["availableSlots"] == 1

    admitted = client.admit("step")
    client.observe(
        admitted,
        "lost-contact",
        evidence={"statement": "The send result was lost", "reference": "test:unknown-send"},
    )
    assert client.step("step")["charged"] == 1
    assert client.view["availableSlots"] == 0
    assert client.pause()["phase"] == "pausing"
    client.pause(False, error=("BLOCKED", "CONFLICT"))


def test_recovery_retains_done_and_restarts_only_with_stop_and_budget(tmp_path):
    client = Client(tmp_path, "done", "retry")
    client.reserve()
    client.result(client.admit("done"))
    completed = client.step("done")["assessment"]["event"]
    interrupted = client.admit("retry")
    client.observe(
        interrupted,
        "lost-contact",
        evidence={"statement": "Worker disconnected", "reference": "test:lost-worker"},
    )
    client.status()
    client.change("takeover", {"evidence": FENCED})
    assert client.pause()["phase"] == "pausing"
    assert client.view["availableSlots"] == 0

    stopped = client.change(
        "resolve", {"kind": "attempt", "attempt": interrupted, "evidence": STOPPED}
    )
    assert stopped["phase"] == "paused"
    assert stopped["availableSlots"] == 1
    client.pause(False)
    assert "retry" not in client.view["ready"]
    assert client.step("retry")["charged"] == 1
    client.change(
        "override", {"kind": "budget", "stepId": "retry", "limit": 2, "decision": DECISION}
    )
    assert "retry" in client.view["ready"] and client.step("retry")["limit"] == 2
    replacement = client.admit("retry")
    assert replacement != interrupted
    client.result(replacement)
    assert client.step("done")["assessment"]["event"] == completed
    assert client.step("done")["charged"] == 1
    assert client.step("retry")["charged"] == 2
    assert client.change("close", {"mode": "finish"})["result"] == "PASSED"


@pytest.mark.parametrize(("threshold", "needs_decision"), [("67%", True), ("66%", False)])
def test_flexible_threshold_uses_exact_integer_ratio(tmp_path, threshold, needs_decision):
    client = Client(
        tmp_path, "first", "second", "third", config=f"acceptanceThreshold: {threshold}"
    )
    client.reserve()
    client.result(client.admit("first"))
    client.result(client.admit("second"))
    client.result(client.admit("third"), outcome="FAIL")
    assert client.step("third")["status"] == "done"
    assert client.view["score"] == {"earned": 2, "available": 3}

    close = {"mode": "finish"}
    if needs_decision:
        client.change("close", close, error="DECISION_REQUIRED")
        close["decision"] = DECISION | {"choice": "accept"}
    assert client.change("close", close)["result"] == "ACCEPTED"


def declare_deliverables(client, *paths):
    with (client.root / client.runbook).open("a", encoding="utf-8") as instructions:
        for path in paths:
            instructions.write(f"\n<!-- deliverable: repo:{path} -->\n")


def staged_file(client, attempt, target, content):
    staged_path = Path(client.view["stagingDirectory"]) / str(attempt) / target
    staged_file = client.root / staged_path
    staged_file.parent.mkdir(parents=True, exist_ok=True)
    staged_file.write_bytes(content)
    return {
        "path": target,
        "stagedPath": staged_path.as_posix(),
        "sha256": hashlib.sha256(content).hexdigest(),
        "priorSha256": None,
    }


def exit_during_second_replacement(client, attempt, outputs):
    # Exit a separate CLI process after intent and first replacement are durable.
    code = """\
import os
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from execute_guided_runbook_lib import cli, storage

root = Path(sys.argv[2])
interruption_target = root / sys.argv[4]
replace = storage.replace_with_retry

def interrupt(source, target):
    if target == interruption_target:
        os._exit(93)
    return replace(source, target)

storage.replace_with_retry = interrupt
raise SystemExit(cli.main(["--repo-root", str(root), "change", sys.argv[3]]))
"""
    request = client.request(
        "observe",
        {
            "attempt": attempt,
            "kind": "result",
            "outcome": "PASS",
            "evidence": STOPPED,
            "deliverables": outputs,
        },
    )
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(SCRIPT.parent),
            str(client.root),
            client.run_id,
            outputs[1]["path"],
        ],
        input=json.dumps(request),
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=20,
    )
    assert process.returncode == 93, (process.stdout, process.stderr)


@pytest.mark.parametrize("recovery", ["complete", "invalidate", "changed-target"])
def test_publication_intent_survives_partial_replacement_and_blocks_overlap(tmp_path, recovery):
    first = "reports/first.md"
    second = "reports/second.md"
    client = Client(tmp_path, "report")
    declare_deliverables(client, first, second)
    client.reserve()
    attempt = client.admit("report")
    outputs = [
        staged_file(client, attempt, first, b"first verified result"),
        staged_file(client, attempt, second, b"second verified result"),
    ]
    before = client.view["revision"]
    exit_during_second_replacement(client, attempt, outputs)

    assert (tmp_path / first).read_bytes() == b"first verified result"
    assert not (tmp_path / second).exists()
    recovered = client.status()
    assert recovered["revision"] > before
    assert client.step("report")["status"] != "done"
    assert recovered["score"]["earned"] == 0
    [intent] = recovered["publications"]
    assert not intent["stale"] and set(intent["targets"]) == {first, second}
    for output in outputs:
        assert (tmp_path / output["stagedPath"]).is_file()
    restart = {
        "runId": client.run_id,
        "ifRevision": recovered["revision"],
        "decision": DECISION,
        "evidence": STOPPED,
    }
    call(tmp_path, "reserve", RUNBOOK, payload={"runId": str(uuid4()), "restart": restart},
         error="BLOCKED")

    overlap = Client(tmp_path, "report", runbook="other.md")
    declare_deliverables(overlap, second)
    overlap.reserve()
    overlap_attempt = overlap.admit("report")
    overlapping_result = {
        "attempt": overlap_attempt,
        "kind": "result",
        "outcome": "PASS",
        "evidence": STOPPED,
        "deliverables": [staged_file(overlap, overlap_attempt, second, b"other run result")],
    }

    if recovery == "invalidate":
        withdrawal = client.change("invalidate", {"steps": ["report"], "cause": "Source changed"})
        assert withdrawal["publications"][0]["stale"]
    overlap.change("observe", overlapping_result, error="BLOCKED")
    client.change("close", {"mode": "abandon", "decision": DECISION}, error="BLOCKED")

    resolution = {
        "kind": "publication",
        "intent": intent["event"],
        "disposition": "complete",
        "evidence": STOPPED,
    }
    if recovery == "complete":
        source = tmp_path / RUNBOOK
        source.write_bytes(source.read_bytes() + b"\n")
        assert not client.status()["sourceMatches"]
        client.change("resolve", resolution)
        assert client.step("report")["status"] == "done"
        assert (tmp_path / second).read_bytes() == b"second verified result"
        assert client.change("close", {"mode": "finish"})["result"] == "PASSED"
        return

    if recovery == "changed-target":
        (tmp_path / second).write_bytes(b"independently changed")
    client.change("resolve", resolution, error="BLOCKED")
    assert client.status()["publications"][0]["event"] == intent["event"]
    assert client.step("report")["status"] != "done"
    if recovery == "changed-target":
        assert (tmp_path / second).read_bytes() == b"independently changed"
        (tmp_path / second).unlink()
    (tmp_path / first).unlink()
    client.change(
        "resolve",
        resolution | {"disposition": "accounted", "effects": "safe-repeat", "decision": DECISION},
    )
    assert not client.status()["publications"]
    assert client.step("report")["status"] != "done"
    overlap.change("observe", overlapping_result)
    assert (tmp_path / second).read_bytes() == b"other run result"
