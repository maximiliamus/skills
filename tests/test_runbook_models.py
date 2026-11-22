"""Model selection, frozen instructions and provenance of delegated work."""

import pytest
from test_runbook_session import DECISION, EVIDENCE, Client, active, binding, call, marked
from test_runbook_session import repo as repo  # noqa: PLC0414 -- shared pytest fixture


@pytest.mark.parametrize(
    "selector,canonical,model_id",
    [
        ("CuRrEnT", "current", "stable"),
        (" FRONTIER ", "frontier", "model-6"),
        ("Predecessor", "predecessor", "model-5"),
        ("Family:V6.2", "family:6.2", "vendor/6.2"),
        ("EXACT: Vendor/Model-v2  ", "exact: Vendor/Model-v2  ", " Vendor/Model-v2  "),
    ],
)
def test_model_family_normalization_and_opaque_exact_id(repo, selector, canonical, model_id):
    client = Client(
        repo, marked("prepare", config=f'modelFamily: "{selector}"'),
    )
    client.reserve()
    assert client.journal()["contract"]["steps"][0]["modelFamily"] == canonical
    if canonical != "current":
        client.admit("prepare", code="BLOCKED")
        client.admit("prepare", model=binding("different", "current"), code="BLOCKED")
    attempt = client.admit("prepare", model=binding(model_id, selector))
    assert client.step("prepare")["attempts"][0]["model"]["modelId"] == model_id
    client.result(attempt)


def test_override_and_exact_requirements_preserve_attempt_provenance(repo):
    config = """
maxParallelSteps: 2
modelTier: heavy
effortLevel: high
steps:
  exact:
    modelFamily: 'exact:Provider/Locked-ID'
    delegateSteps: true
  following:
    modelFamily: current
"""
    client = Client(
        repo, marked("inherited", "exact", "following", config=config),
        model_family="frontier", decision=DECISION,
    )
    client.reserve()
    contract = client.journal()["contract"]["steps"]
    assert [step["modelFamilyOrigin"] for step in contract] == ["operator", "step", "step"]
    selected = binding("selected-frontier", "frontier", "heavy", "high")
    old = client.admit("inherited", model=selected)
    client.change("takeover", {"evidence": EVIDENCE})
    client.change("override", {"kind": "model", "modelFamily": "predecessor", "decision": DECISION})
    assert client.step("inherited")["modelFamily"] == "predecessor"
    assert client.step("inherited")["attempts"][0]["requirements"]["modelFamily"] == "frontier"
    exact = binding("Provider/Locked-ID", "exact:Provider/Locked-ID", "heavy", "high")
    client.admit("exact", execution="inline", model=exact, code="BLOCKED")
    client.admit("exact", model=exact | {"effortLevel": "low"}, code="BLOCKED")
    exact_attempt = client.admit("exact", model=exact)
    client.result(old)
    client.result(exact_attempt)
    client.admit("following", code="BLOCKED")
    future = client.admit("following", execution="inline",
                          model={"modelTier": "heavy", "effortLevel": "high"})
    client.result(future)
    assert client.finish()["result"] == "PASSED"


def test_unapproved_override_and_unmet_model_requirements(repo):
    client = Client(repo, marked("prepare"), model_family="frontier")
    client.reserve(code="DECISION_REQUIRED")
    assert not client.path_to_journal.exists()
    client.initial["overrideDecision"] = DECISION
    client.reserve()
    client.admit("prepare", code="BLOCKED")
    assert client.step("prepare")["charged"] == 0


def test_observed_model_mismatch_needs_reconciliation(repo):
    client = active(repo, "prepare", config="maxAttempts: 2\nmodelFamily: 'exact:model-A'")
    selected = {"modelId": "model-A"}
    first = client.admit("prepare", model=selected)
    client.result(first, model={"modelId": "unexpected"}, code="BLOCKED")
    client.observe(first, "technical-failure", reason="Unexpected model", transient=False)
    assert client.step("prepare")["charged"] == 1
    assert client.step("prepare")["assessment"] is None
    client.admit("prepare", model=selected, code="DECISION_REQUIRED")
    client.result(client.admit("prepare", model=selected, decision=DECISION))
    assert client.finish()["result"] == "PASSED"


def test_edited_procedure_keeps_frozen_instructions_and_open_holds(repo):
    client = active(repo, "prepare", "review", config="maxParallelSteps: 2")
    first = client.admit("prepare")
    hold = client.observe(first, "question", text="Operator scope?")["revision"]
    original = client.view["contract"]["instructions"]
    client.path.write_text(
        original.replace("maxParallelSteps: 2", "maxParallelSteps: 1") + "\nRevised instructions.\n",
        encoding="utf-8", newline="",
    )
    view = client.status()
    assert view["contract"]["instructions"] == original
    assert not view["sourceMatches"] and view["ready"] == []
    assert client.step("prepare")["question"] == hold
    assert client.step("prepare")["attempt"] == first
    client.change("accept-revision", {}, code="INVALID_REQUEST")
    client.change("resolve", {"kind": "question", "hold": hold, "decision": DECISION})
    client.result(first)
    client.admit("review", code="BLOCKED")
    revised = Client(repo, client.path.read_text(encoding="utf-8"))
    revised.initial["restart"] = {
        "runId": client.run, "ifRevision": client.view["revision"],
        "decision": DECISION, "evidence": EVIDENCE,
    }
    assert revised.reserve()["contract"]["maxParallelSteps"] == 1


@pytest.mark.parametrize(
    "config",
    [
        "maxParallelSteps: 0",
        "maxAttempts: 1.2",
        "delegateSteps: 'true'",
        "steps:\n  typo: {}",
        "steps:\n  first:\n    accept: true",
        "steps:\n  first:\n    dependsOn:\n      first: passed",
        "steps:\n  first:\n    dependsOn:\n      typo: passed",
        "steps:\n  first:\n    dependsOn:\n      second: pass",
        "steps:\n  first:\n    dependsOn:\n      second: passed\n  second:\n    dependsOn:\n      first: assessed",
        "modelFamily: mystery",
        "effortLevel: best",
        "modelTier: sun",
        "stepOverrides: obsolete",
        "acceptancePolicy: always\nacceptanceThreshold: 80%",
    ],
)
def test_invalid_settings_are_rejected_before_reservation(repo, config):
    client = Client(repo, marked("first", "second", config=config))
    client.reserve(code="INVALID_REQUEST")
    assert not client.path_to_journal.exists()


@pytest.mark.parametrize("fact", ["modelFamily", "modelTier", "effortLevel"])
def test_registry_rejects_execution_profile_fields(repo, fact):
    import json

    from test_runbook_session import REGISTRY

    (repo / REGISTRY).write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "runbooks": [{"id": "release", "path": "release.md", fact: "frontier"}],
            }
        ),
        encoding="utf-8",
    )
    call(repo, "list", code="INVALID_REQUEST")
