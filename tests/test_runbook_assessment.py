"""Applicability, scoring and mandatory delivery, including the audit profile."""

import pytest
from test_runbook_session import DECISION, EVIDENCE, Client, active, artifact, binding, marked
from test_runbook_session import repo as repo  # noqa: PLC0414 -- shared pytest fixture


def policy(repo):
    (repo / "policy.md").write_text(
        "Procedure defines permitted gaps, deterministic reuse and safe output replacement.",
        encoding="utf-8",
    )
    return [artifact(repo, "policy.md") | {"section": "Scope and recovery"}]


def test_markdown_order_waits_for_a_terminal_outcome(repo):
    facts = {"first": {"procedure": policy(repo), "gapCauses": ["exhausted"]}}
    client = active(repo, "first", "second", "third", config="acceptancePolicy: always", facts=facts)
    assert client.view["ready"] == ["first"]
    assert client.admit("second", decision=DECISION, code="BLOCKED")["blockers"] == ["step-order"]
    first = client.admit("first")
    question = client.observe(first, "question", text="Do we have the inputs?")["revision"]
    settlement = client.observe(first, "stopped")["revision"]
    assert client.view["availableSlots"] == 1 and client.view["ready"] == []
    assert "question" in client.step("first")["blockers"]
    assert client.admit("second", decision=DECISION, code="BLOCKED")["nextStep"] == "first"
    client.change("resolve", {"kind": "question", "hold": question, "decision": DECISION})
    assert client.step("first")["assessment"] is None and client.view["ready"] == []
    assert "budget" in client.step("first")["blockers"]
    client.change("assess", {
        "kind": "gap", "stepId": "first", "cause": "exhausted", "trigger": settlement,
    })
    exhausted = client.step("first")["assessment"]
    assert exhausted["outcome"] == "EXHAUSTED"
    assert exhausted["evidence"]["reference"] == f"journal:{client.run}#{settlement}"
    assert client.status()["ready"] == ["second"]

    second = client.admit("second")
    client.observe(second, "result", outcome="FAILED", reason="Missing input", deliverables=[])
    failed = client.step("second")["assessment"]
    assert failed["outcome"] == "FAILED" and failed["reason"] == "Missing input"
    assert failed["evidence"] == EVIDENCE and client.view["ready"] == ["third"]
    client.result(client.admit("third"))
    assert client.step("third")["assessment"]["outcome"] == "PASSED"
    assert client.finish()["result"] == "ACCEPTED"


def test_markdown_order_holds_when_preceding_evidence_changes(repo):
    client = active(repo, "first", "second")
    (repo / "input.md").write_text("Current input", encoding="utf-8")
    client.result(client.admit("first", sources=[artifact(repo, "input.md")]))
    assert client.view["ready"] == ["second"]

    (repo / "input.md").write_text("Changed input", encoding="utf-8")
    assert client.status()["ready"] == []
    assert "applicability" in client.step("first")["blockers"]
    assert "step-order" in client.step("second")["blockers"]
    client.admit("second", decision=DECISION, code="BLOCKED")


def test_previously_started_step_cannot_be_marked_skipped(repo):
    procedure = policy(repo)
    facts = {
        "prepare": {"procedure": procedure, "reassessAfterInvalidation": True},
        "check": {"procedure": procedure, "gapCauses": ["failed-prerequisite", "invalidated"]},
    }
    client = active(
        repo, "prepare", "check", "report", facts=facts,
        config="""
maxAttempts: 2
steps:
  check:
    dependsOn:
      prepare: passed
  report:
    dependsOn:
      check: done
""",
    )
    first = client.result(client.admit("prepare"))["revision"]
    client.result(client.admit("check", dependencies={"prepare": first}))
    revoked = client.change("invalidate", {"steps": ["prepare"], "cause": "Changed scope"})
    assert client.step("check")["assessment"] is None and "report" not in client.view["ready"]
    failed = client.result(client.admit("prepare"), passed=False)["revision"]
    client.change("assess", {
        "kind": "gap", "stepId": "check", "cause": "failed-prerequisite", "trigger": failed,
    }, code="BLOCKED")
    client.change("assess", {
        "kind": "gap", "stepId": "check", "cause": "invalidated", "trigger": revoked["revision"],
    })
    invalidated = client.step("check")["assessment"]
    assert invalidated["outcome"] == "INVALIDATED" and invalidated["reason"] == "Changed scope"
    assert client.step("check")["charged"] == 1
    assert client.view["ready"] == ["report"]
    client.admit("report", dependencies={"check": invalidated["event"]})


def test_strict_sequential_reports_after_failed_assessed_prerequisite(repo):
    client = active(
        repo, "prepare", "report", "independent",
        config="""
acceptancePolicy: strict
steps:
  report:
    dependsOn:
      prepare: assessed
""",
    )
    assert client.view["contract"]["steps"][1]["dependsOn"] == {"prepare": "done"}
    assert client.view["ready"] == ["prepare", "independent"]
    client.admit("independent", code="BLOCKED")
    event = client.result(client.admit("prepare", execution="inline"), passed=False)["revision"]
    assert client.step("prepare")["status"] == "done"
    assert client.view["ready"] == ["report", "independent"]
    client.admit("report", code="BLOCKED")
    client.result(client.admit("report", dependencies={"prepare": event}))
    client.result(client.admit("independent"))
    assert client.finish()["result"] == "REJECTED"


def test_withdrawal_cascades_and_fences_shared_mutation(repo):
    facts = {
        "prepare": {"procedure": policy(repo), "reassessAfterInvalidation": True},
        "unstarted": {"procedure": policy(repo), "gapCauses": ["invalidated"]},
    }
    client = active(
        repo, "prepare", "dependent", "closed-dependent", "unstarted", "free",
        config="""
maxAttempts: 2
maxParallelSteps: 3
stepOrder: arbitrary
steps:
  dependent:
    dependsOn:
      prepare: passed
  closed-dependent:
    dependsOn:
      prepare: passed
""",
        facts=facts,
    )
    (repo / "source.md").write_text("original boundary", encoding="utf-8")
    source = artifact(repo, "source.md")
    mutation = {
        "target": "shared snapshot", "preconditions": "recorded snapshot",
        "verify": "verify the replacement boundary",
    }
    seed = client.admit("prepare", sources=[source], mutation=mutation)
    prior = client.result(seed)["revision"]
    deps = {"prepare": prior}
    client.result(client.admit("closed-dependent", dependencies=deps))
    dependent = client.admit("dependent", dependencies=deps)
    question = client.observe(dependent, "question", text="Check current inputs?")["revision"]
    withdrawal = client.change(
        "invalidate", {"references": [source], "cause": "Source changed", "steps": ["unstarted"]},
    )
    assert withdrawal["score"] == {"earned": 0, "available": 0}
    assert client.step("dependent")["attempts"][0]["live"]
    assert client.step("dependent")["question"] is None
    assert client.step("closed-dependent")["assessment"] is None
    assert "free" in withdrawal["ready"]
    client.change("resolve", {"kind": "question", "hold": question, "decision": DECISION},
                  code="NOT_FOUND")
    client.admit("prepare", mutation=mutation, code="BLOCKED")
    client.change("assess", {
        "stepId": "unstarted", "kind": "gap", "cause": "invalidated",
        "trigger": withdrawal["revision"],
    }, code="BLOCKED")
    client.result(dependent)
    assert client.step("dependent")["assessment"] is None
    client.change("assess", {
        "stepId": "unstarted", "kind": "gap", "cause": "invalidated",
        "trigger": withdrawal["revision"],
    })
    assert client.step("unstarted")["assessment"]["outcome"] == "INVALIDATED"
    assert client.step("unstarted")["charged"] == 0
    client.result(client.admit("prepare", mutation=mutation))
    assert client.step("prepare")["charged"] == 2


def audit(repo, *, report_repeat=True, max_attempts=2):
    checks = [f"check-{index}" for index in range(1, 10)]
    config = f"""
acceptancePolicy: always
delegateSteps: true
maxParallelSteps: 3
maxAttempts: {max_attempts}
modelTier: heavy
effortLevel: high
steps:
"""
    config += "".join(f"  {sid}:\n    dependsOn:\n      prepare: passed\n" for sid in checks)
    config += "  final-report:\n    dependsOn:\n      prepare: done\n"
    config += "".join(f"      {sid}: done\n" for sid in checks)
    procedure = policy(repo)
    facts = {
        sid: {
            "procedure": procedure, "gapCauses": ["failed-prerequisite", "invalidated"],
            "reuse": True,
        }
        for sid in checks
    }
    facts["prepare"] = {"procedure": procedure, "gapCauses": ["exhausted"]}
    facts["final-report"] = {"procedure": procedure, "reassessAfterInvalidation": report_repeat}
    text = marked(
        "prepare", *checks, "final-report", config=config,
        ending="<!-- deliverable: .runbooks/reports/audit/{runId}/report.md -->\n",
    )
    client = Client(repo, text, facts=facts, model=binding(tier="heavy", effort="high"))
    client.reserve()
    return client, checks


def audit_preparation_gaps(client, checks, prepare):
    records = {"prepare": prepare}
    for sid in checks:
        client.change("assess", {
            "kind": "gap", "stepId": sid, "cause": "failed-prerequisite", "trigger": prepare,
        })
        check = client.step(sid)
        assessment = check["assessment"]
        assert check["status"] == "done" and check["charged"] == 0
        assert assessment["outcome"] == "SKIPPED"
        assert assessment["reason"] and assessment["evidence"]["reference"].endswith(f"#{prepare}")
        assert assessment["score"] == {"earned": 0, "available": 1}
        assert assessment["prerequisites"] == {"prepare": prepare}
        records[sid] = assessment["event"]
    return records


def audit_inputs(client, checks):
    prepare = client.result(client.admit("prepare"))["revision"]
    records = {"prepare": prepare}
    for first in range(0, len(checks), 3):
        batch = checks[first:first + 3]
        attempts = [client.admit(sid, dependencies={"prepare": prepare}) for sid in batch]
        assert client.view["availableSlots"] == 0
        for sid, attempt in zip(batch, attempts, strict=True):
            records[sid] = client.result(attempt)["revision"]
    return records


def publish_audit_report(client, records, text, *, prior=None):
    target = f".runbooks/reports/audit/{client.run}/report.md"
    attempt = client.admit("final-report", dependencies=records)
    output = client.stage(attempt, target, text, prior=prior)
    client.result(attempt, deliverables=[output])
    assert (client.repo / target).read_text(encoding="utf-8") == text
    return target


def test_audit_failed_preparation_gaps_and_mandatory_report_score_one_of_eleven(repo):
    client, checks = audit(repo)
    client.admit("prepare", execution="inline", code="BLOCKED")
    prepare = client.result(client.admit("prepare"), passed=False)["revision"]
    records = audit_preparation_gaps(client, checks, prepare)
    report = client.admit("final-report", dependencies=records)
    client.result(report, code="BLOCKED")
    client.finish(code="BLOCKED")
    path = f".runbooks/reports/audit/{client.run}/report.md"
    output = client.stage(report, path, "Preparation failed, nine checks not assessed.")
    client.result(report, deliverables=[output])
    assert client.finish()["score"] == {"earned": 1, "available": 11}
    assert client.view["result"] == "ACCEPTED"


def test_audit_exhausted_preparation_gaps_allow_mandatory_report(repo):
    client, checks = audit(repo)
    dependent = {"kind": "gap", "stepId": checks[0], "cause": "failed-prerequisite"}
    exhausted = {"kind": "gap", "stepId": "prepare", "cause": "exhausted"}
    first = client.admit("prepare")
    question = client.observe(first, "question", text="Are preparation inputs available?")
    client.change("assess", dependent | {"trigger": question["revision"]}, code="BLOCKED")
    client.change("resolve", {
        "kind": "question", "hold": question["revision"], "decision": DECISION,
    })
    stopped = client.observe(first, "technical-failure", reason="Input unavailable", transient=True)
    client.change("assess", exhausted | {"trigger": stopped["revision"]}, code="BLOCKED")

    second = client.admit("prepare")
    stopped = client.observe(second, "technical-failure", reason="Input unavailable", transient=True)
    client.change("assess", dependent | {"trigger": stopped["revision"]}, code="BLOCKED")
    client.change("assess", exhausted | {"trigger": stopped["revision"]})
    prepare = client.step("prepare")
    assert prepare["charged"] == 2
    assert prepare["assessment"]["outcome"] == "EXHAUSTED"
    assert prepare["assessment"]["cause"] == "exhausted"
    prepare_event = prepare["assessment"]["event"]
    client.admit(checks[0], dependencies={"prepare": prepare_event}, code="BLOCKED")

    records = audit_preparation_gaps(client, checks, prepare_event)
    assert client.view["ready"] == ["final-report"]
    publish_audit_report(client, records, "Preparation exhausted; nine checks not assessed.")
    assert client.finish()["result"] == "ACCEPTED"
    assert client.status()["score"] == {"earned": 1, "available": 11}


def test_audit_invalidation_requires_new_report_and_authorized_replacement(repo):
    client, checks = audit(repo)
    records = audit_inputs(client, checks)
    path = publish_audit_report(client, records, "All ten assessments passed.")
    prior = artifact(repo, path)["sha256"]
    affected = checks[0]
    trigger = client.change("invalidate", {"steps": [affected], "cause": "New source scope"})
    assert client.step("final-report")["status"] == "pending"
    client.finish(code="BLOCKED")
    client.change("assess", {
        "kind": "gap", "stepId": affected, "cause": "invalidated", "trigger": trigger["revision"],
    })
    records[affected] = client.step(affected)["assessment"]["event"]
    report = client.admit("final-report", dependencies=records)
    output = client.stage(report, path, "One check needs another audit.", prior=prior)
    client.result(report, deliverables=[{key: val for key, val in output.items()
                                         if key != "replacement"}], code="INVALID_REQUEST")
    client.result(report, deliverables=[output])
    assert client.finish()["score"] == {"earned": 10, "available": 11}


@pytest.mark.parametrize("permission,limit", [(False, 2), (True, 1)])
def test_report_replacement_respects_frozen_permission_and_budget(repo, permission, limit):
    client, checks = audit(repo, report_repeat=permission, max_attempts=limit)
    records = audit_inputs(client, checks)
    publish_audit_report(client, records, "Original verified report.")
    client.change("invalidate", {"steps": ["final-report"], "cause": "Report is stale"})
    error = client.admit("final-report", dependencies=records, code="BLOCKED")
    assert ("reassessment" if not permission else "budget") in error["blockers"]


def test_cross_run_reuse_publishes_verified_artifact_at_receiving_path(repo):
    procedure = policy(repo)
    facts = {"report": {"procedure": procedure, "reuse": True}}
    text = marked("report", ending="<!-- deliverable: reports/{runId}/report.md -->\n")
    previous = Client(repo, text, facts=facts)
    previous.reserve()
    source_path = f"reports/{previous.run}/report.md"
    attempt = previous.admit("report")
    output = previous.stage(attempt, source_path, "Verified report")
    previous.result(attempt, deliverables=[output])
    source_event = previous.step("report")["assessment"]["event"]
    previous.finish()

    receiving = Client(repo, text, facts=facts)
    receiving.reserve()
    target = f"reports/{receiving.run}/report.md"
    reuse = {
        "stepId": "report", "kind": "reuse",
        "source": {"runId": previous.run, "event": source_event},
        "sources": [], "prerequisites": {}, "evidence": EVIDENCE,
    }
    receiving.change("assess", reuse, code="BLOCKED")
    different = receiving.stage("reuse", target, "Wrong bytes")
    receiving.change("assess", reuse | {"deliverables": [different]}, code="BLOCKED")
    staged = receiving.stage("reuse", target, "Verified report")
    receiving.change("assess", reuse | {"deliverables": [staged]})
    assert receiving.step("report")["status"] == "done"
    assert receiving.step("report")["charged"] == 0
    assert (repo / target).read_text(encoding="utf-8") == "Verified report"
    assert receiving.finish()["result"] == "PASSED"


def test_reuse_rejects_same_verdict_prerequisite_from_old_revision(repo):
    facts = {
        "input": {"procedure": policy(repo), "reassessAfterInvalidation": True},
        "check": {"procedure": policy(repo), "reuse": True},
    }
    client = active(
        repo, "input", "check", config="maxAttempts: 2\nsteps:\n  check:\n    dependsOn:\n      input: passed",
        facts=facts,
    )
    old = client.result(client.admit("input"))["revision"]
    assessed = client.result(client.admit("check", dependencies={"input": old}))["revision"]
    client.change("invalidate", {"steps": ["input"], "cause": "New input revision"})
    new = client.result(client.admit("input"))["revision"]
    data = {
        "stepId": "check", "kind": "reuse", "source": {"runId": client.run, "event": assessed},
        "sources": [], "prerequisites": {"input": old}, "evidence": EVIDENCE,
    }
    assert old != new
    client.change("assess", data, code="BLOCKED")
    client.change("assess", data | {"prerequisites": {"input": new}})
    assert client.step("check")["charged"] == 1


def test_exhaustion_gap_preserves_attempt_count_and_frozen_weight(repo):
    facts = {"check": {"procedure": policy(repo), "gapCauses": ["exhausted"], "points": 3}}
    client = active(repo, "check", "report", config="acceptancePolicy: always", facts=facts)
    attempt = client.admit("check")
    failure = client.observe(attempt, "technical-failure", reason="I/O error", transient=True)
    client.change("assess", {
        "stepId": "check", "kind": "gap", "cause": "exhausted", "trigger": failure["revision"],
    })
    assert client.step("check")["charged"] == 1
    assert client.step("check")["assessment"]["score"] == {"earned": 0, "available": 3}
    client.result(client.admit("report"))
    assert client.finish()["score"] == {"earned": 1, "available": 4}


def test_failed_prerequisite_gap_tracks_the_withdrawn_assessment(repo):
    facts = {
        "prepare": {"procedure": policy(repo), "reassessAfterInvalidation": True},
        "dependent": {"procedure": policy(repo), "gapCauses": ["failed-prerequisite"]},
    }
    client = active(
        repo, "prepare", "dependent",
        config="maxAttempts: 2\nsteps:\n  dependent:\n    dependsOn:\n      prepare: passed",
        facts=facts,
    )
    failure = client.result(client.admit("prepare"), passed=False)["revision"]
    gap = {"stepId": "dependent", "kind": "gap", "cause": "failed-prerequisite", "trigger": failure}
    client.change("assess", gap)
    client.change("invalidate", {"steps": ["prepare"], "cause": "Inputs changed"})
    client.result(client.admit("prepare"))
    client.change("assess", gap, code="BLOCKED")
    assert client.step("dependent")["status"] == "pending"
    assert client.view["ready"] == ["dependent"]
