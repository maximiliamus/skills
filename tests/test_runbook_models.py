"""Agent-agnostic model-family and ledger-binding checks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_runbook_session import RUNBOOK_SCRIPT
from test_runbook_session import repo as repo  # noqa: PLC0414 -- shared pytest fixture


def invoke(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNBOOK_SCRIPT), "--repo-root", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )


def setup(repo: Path, frontmatter: str = "") -> None:
    text = f"---\n{frontmatter}---\n# Release\n\n1. Verify.\n" if frontmatter else (
        "# Release\n\n1. Verify.\n"
    )
    (repo / "release.md").write_text(text, encoding="utf-8")


def start(
    repo: Path,
    family: str | None = None,
    model_id: str = "Model-X",
    *options: str,
) -> subprocess.CompletedProcess[str]:
    args = ["run", "release.md", "--model-id", model_id]
    if family is not None:
        args.extend(["--model-family", family])
    return invoke(repo, *args, *options)


def ledger(repo: Path) -> Path:
    return next((repo / ".runbooks").glob("path-*.json"))


@pytest.mark.parametrize(("family", "model_id"), [
    ("current", "Model-Current"),
    ("frontier", "Model-X"),
    ("predecessor", "Model-W"),
    ("family:5.4", "Vendor/Model-V"),
    ("exact:Vendor/Model-X+Preview", "Vendor/Model-X+Preview"),
])
def test_supported_families_record_agent_resolved_values(repo, family, model_id):
    setup(repo, "modelTier: heavy\neffortLevel: extra\n")
    result = start(repo, family, model_id)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["schemaVersion"] == 2
    assert state["modelFamily"] == family
    assert state["modelId"] == model_id
    assert (state["modelTier"], state["effortLevel"]) == ("heavy", "extra")
    assert "execution" not in state


def test_current_is_document_default_and_profile_is_not_registered(repo):
    setup(repo)
    resolved = json.loads(invoke(repo, "resolve", "release.md").stdout)
    assert (resolved["modelFamily"], resolved["modelTier"], resolved["effortLevel"]) == (
        "current", "medium", "medium",
    )
    registered = invoke(repo, "register", "release", "release.md")
    assert registered.returncode == 0, registered.stderr
    entry = json.loads((repo / "runbooks.json").read_text(encoding="utf-8"))["runbooks"][0]
    assert not {"modelFamily", "modelTier", "effortLevel"} & entry.keys()
    listed = json.loads(invoke(repo, "list").stdout)["runbooks"][0]
    assert listed["modelFamily"] == "current"


@pytest.mark.parametrize(("declared", "normalized"), [
    ("CURRENT", "current"),
    ("FRONTIER", "frontier"),
    ("Predecessor", "predecessor"),
    ("family:V5.6", "family:5.6"),
    ("EXACT:Vendor/Model-X", "exact:Vendor/Model-X"),
])
def test_document_model_family_is_normalized_without_changing_exact_id(
    repo, declared, normalized,
):
    setup(repo, f"modelFamily: {declared}\n")

    result = invoke(repo, "resolve", "release.md")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["modelFamily"] == normalized


@pytest.mark.parametrize("operation", [
    ("run",),
    ("step", "verify", "--title", "Verify"),
    ("complete", "--evidence", "Checked", "--result", "pass", "--score", "1/1"),
    ("block", "--reason", "Blocked"),
    ("skip", "--reason", "Skipped", "--score", "0/1"),
    ("finish", "--evidence", "Finished", "--expected-step", "verify"),
])
def test_resume_and_progress_fail_closed_on_executor_mismatch(repo, operation):
    setup(repo)
    assert start(repo).returncode == 0
    before = ledger(repo).read_bytes()
    args = [operation[0], "release.md", *operation[1:],
            "--model-id", "Other-Model"]
    result = invoke(repo, *args)
    assert result.returncode == 2
    assert "does not match recorded modelId" in result.stderr
    assert ledger(repo).read_bytes() == before
    assert len(list((repo / ".runbooks").glob("path-*.json"))) == 1


@pytest.mark.parametrize(("flag", "value", "error"), [
    ("--model-family", "predecessor", "disagrees with the recorded session binding"),
    ("--model-id", "Other-Model", "does not match recorded modelId"),
])
def test_resume_rejects_changed_explicit_binding(repo, flag, value, error):
    setup(repo)
    assert start(repo).returncode == 0
    before = ledger(repo).read_bytes()
    result = invoke(
        repo, "run", "release.md", "--model-id", "Model-X", flag, value,
    )
    assert result.returncode == 2
    assert error in result.stderr
    assert ledger(repo).read_bytes() == before


def test_operator_rebind_preserves_progress_and_changes_required_identity(repo):
    setup(repo)
    assert start(repo, model_id="Model-A").returncode == 0
    assert invoke(
        repo,
        "step", "release.md", "prepare", "--title", "Prepare",
        "--model-id", "Model-A",
    ).returncode == 0
    assert invoke(
        repo,
        "complete", "release.md", "--evidence", "Prepared",
        "--result", "pass", "--score", "1/1", "--model-id", "Model-A",
    ).returncode == 0
    assert invoke(
        repo,
        "step", "release.md", "verify", "--title", "Verify",
        "--model-id", "Model-A",
    ).returncode == 0
    path = ledger(repo)
    before = json.loads(path.read_text(encoding="utf-8"))

    mismatch = invoke(
        repo, "block", "release.md", "--reason", "Waiting",
        "--model-id", "Model-B",
    )
    assert mismatch.returncode == 2
    assert json.loads(path.read_text(encoding="utf-8")) == before

    rebound = invoke(
        repo,
        "rebind", "release.md",
        "--model-family", "current",
        "--model-id", "Model-B",
    )
    assert rebound.returncode == 0, rebound.stderr
    state = json.loads(rebound.stdout)
    assert Path(state["statePath"]) == path
    assert (state["modelFamily"], state["modelId"]) == ("current", "Model-B")
    assert (state["modelTier"], state["effortLevel"]) == ("medium", "medium")
    assert state["history"] == before["history"]
    assert state["currentStep"] == before["currentStep"]
    assert state["runbookSha256"] == before["runbookSha256"]
    assert state["revisionDecisions"] == before["revisionDecisions"]
    assert len(list((repo / ".runbooks").glob("path-*.json"))) == 1

    old_model = invoke(
        repo, "block", "release.md", "--reason", "Waiting",
        "--model-id", "Model-A",
    )
    assert old_model.returncode == 2
    continued = invoke(
        repo, "block", "release.md", "--reason", "Waiting",
        "--model-id", "Model-B",
    )
    assert continued.returncode == 0, continued.stderr


def test_operator_rebind_normalizes_family_and_validates_exact_before_write(repo):
    setup(repo)
    assert start(repo, model_id="Model-A").returncode == 0
    path = ledger(repo)

    rebound = invoke(
        repo,
        "rebind", "release.md",
        "--model-family", "family:V5.6",
        "--model-id", "Model-5.6",
    )
    assert rebound.returncode == 0, rebound.stderr
    state = json.loads(rebound.stdout)
    assert (state["modelFamily"], state["modelId"]) == (
        "family:5.6", "Model-5.6",
    )

    before = path.read_bytes()
    invalid = invoke(
        repo,
        "rebind", "release.md",
        "--model-family", "exact:Other-Model",
        "--model-id", "Model-5.6",
    )
    assert invalid.returncode == 2
    assert "disagrees" in invalid.stderr
    assert path.read_bytes() == before


def test_rebind_requires_an_active_existing_session(repo):
    setup(repo)
    missing = invoke(
        repo,
        "rebind", "release.md",
        "--model-family", "current",
        "--model-id", "Model-B",
    )
    assert missing.returncode == 2
    assert "No session exists" in missing.stderr

    assert start(repo, model_id="Model-A").returncode == 0
    assert invoke(
        repo, "step", "release.md", "verify", "--title", "Verify",
        "--model-id", "Model-A",
    ).returncode == 0
    assert invoke(
        repo,
        "complete", "release.md", "--evidence", "Checked",
        "--result", "pass", "--score", "1/1", "--model-id", "Model-A",
    ).returncode == 0
    assert invoke(
        repo,
        "finish", "release.md", "--evidence", "Done",
        "--expected-step", "verify", "--model-id", "Model-A",
    ).returncode == 0
    before = ledger(repo).read_bytes()

    completed = invoke(
        repo,
        "rebind", "release.md",
        "--model-family", "current",
        "--model-id", "Model-B",
    )
    assert completed.returncode == 2
    assert "already complete" in completed.stderr
    assert ledger(repo).read_bytes() == before


def test_invalid_rebind_precedes_document_id_ledger_migration(repo):
    setup(repo)
    assert start(repo, model_id="Model-A").returncode == 0
    before = {path.name: path.read_bytes() for path in (repo / ".runbooks").glob("*.json")}
    (repo / "release.md").write_text(
        "---\nid: release\n---\n# Release\n\n1. Verify.\n", encoding="utf-8",
    )

    result = invoke(
        repo,
        "rebind", "release.md",
        "--model-family", "exact:Other-Model",
        "--model-id", "Model-B",
    )

    assert result.returncode == 2
    assert "disagrees" in result.stderr
    after = {path.name: path.read_bytes() for path in (repo / ".runbooks").glob("*.json")}
    assert after == before


def test_explicit_restart_after_rebind_archives_progress_and_starts_clean(repo):
    setup(repo)
    assert start(repo, model_id="Model-A").returncode == 0
    assert invoke(
        repo,
        "step", "release.md", "verify", "--title", "Verify",
        "--model-id", "Model-A",
    ).returncode == 0
    assert invoke(
        repo,
        "rebind", "release.md",
        "--model-family", "current",
        "--model-id", "Model-B",
    ).returncode == 0

    restarted = start(repo, None, "Model-B", "--restart")

    assert restarted.returncode == 0, restarted.stderr
    state = json.loads(restarted.stdout)
    assert (state["modelFamily"], state["modelId"]) == ("current", "Model-B")
    assert state["currentStep"] is None
    assert state["history"] == []
    assert state["previousSession"]["decision"] == "restarted"
    archive = Path(state["previousSession"]["archivePath"])
    archived = json.loads(archive.read_text(encoding="utf-8"))
    assert archived["currentStep"]["id"] == "verify"
    assert (archived["modelFamily"], archived["modelId"]) == (
        "current", "Model-B",
    )


def test_document_profile_changes_do_not_retarget_existing_session(repo):
    setup(repo, "modelTier: heavy\neffortLevel: high\nmodelFamily: predecessor\n")
    assert start(repo, "predecessor", "Model-W").returncode == 0
    (repo / "release.md").write_text(
        "---\nmodelTier: light\neffortLevel: low\nmodelFamily: frontier\n---\n# Release changed\n",
        encoding="utf-8",
    )
    result = invoke(
        repo, "run", "release.md", "--continue", "--model-id", "Model-W",
    )
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert (state["modelFamily"], state["modelId"]) == ("predecessor", "Model-W")
    assert (state["modelTier"], state["effortLevel"]) == ("heavy", "high")


@pytest.mark.parametrize(("family", "model_id", "error"), [
    ("auto", "Model-X", "modelFamily"),
    ("family:", "Model-X", "modelFamily"),
    ("exact:Model-Y", "Model-X", "disagrees"),
])
def test_invalid_binding_fails_before_ledger(repo, family, model_id, error):
    setup(repo)
    result = start(repo, family, model_id)
    assert result.returncode == 2
    assert error in result.stderr
    assert not list((repo / ".runbooks").glob("path-*.json"))


@pytest.mark.parametrize("property_line", [
    "modelTier: giant", "effortLevel: maximum", "modelFamily: auto",
])
def test_invalid_document_profile_is_rejected(repo, property_line):
    setup(repo, f"{property_line}\n")
    result = invoke(repo, "resolve", "release.md")
    assert result.returncode == 2
    assert "frontmatter" in result.stderr


@pytest.mark.parametrize("field", [
    "modelFamily", "modelId", "modelTier", "effortLevel",
])
def test_missing_required_model_fields_fail_closed(repo, field):
    setup(repo)
    assert start(repo).returncode == 0
    path = ledger(repo)
    state = json.loads(path.read_text(encoding="utf-8"))
    del state[field]
    path.write_text(json.dumps(state), encoding="utf-8")
    before = path.read_bytes()
    result = invoke(repo, "run", "release.md", "--model-id", "Model-X")
    assert result.returncode == 2
    assert path.read_bytes() == before


def test_schema_one_registry_and_ledger_are_rejected(repo):
    setup(repo)
    (repo / "runbooks.json").write_text(
        '{"schemaVersion":1,"runbooks":[]}', encoding="utf-8",
    )
    assert invoke(repo, "list").returncode == 2
    (repo / "runbooks.json").unlink()
    assert start(repo).returncode == 0
    path = ledger(repo)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["schemaVersion"] = 1
    path.write_text(json.dumps(state), encoding="utf-8")
    result = invoke(repo, "run", "release.md", "--model-id", "Model-X")
    assert result.returncode == 2
    assert "Unsupported session schema" in result.stderr


@pytest.mark.parametrize("field", ["modelFamily", "modelTier", "effortLevel"])
def test_registry_rejects_execution_profile_fields(repo, field):
    setup(repo)
    (repo / "runbooks.json").write_text(json.dumps({
        "schemaVersion": 2,
        "runbooks": [{"id": "release", "path": "release.md", field: "frontier"}],
    }), encoding="utf-8")
    result = invoke(repo, "list")
    assert result.returncode == 2
    assert "Markdown frontmatter" in result.stderr


def test_model_identity_is_opaque_and_case_sensitive(repo):
    setup(repo)
    identity = "Vendor/Модель+X@Preview:2026"
    result = start(repo, f"EXACT:{identity}", identity)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["modelFamily"] == f"exact:{identity}"
    assert state["modelId"] == identity
    result = invoke(
        repo, "run", "release.md", "--model-id", identity.lower(),
    )
    assert result.returncode == 2
    assert "does not match recorded modelId" in result.stderr


def test_model_identity_preserves_boundary_whitespace_exactly(repo):
    setup(repo)
    identity = " Model-X "
    result = start(repo, f"exact:{identity}", identity)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["modelFamily"] == f"exact:{identity}"
    assert state["modelId"] == identity
    before = ledger(repo).read_bytes()
    mismatch = invoke(
        repo, "run", "release.md", "--model-id", identity.strip(),
    )
    assert mismatch.returncode == 2
    assert ledger(repo).read_bytes() == before


def test_frontmatter_exact_identity_preserves_quoted_boundary_whitespace(repo):
    identity = " Model-X "
    setup(repo, f'modelFamily: "exact:{identity}"\n')
    resolved = invoke(repo, "resolve", "release.md")
    assert resolved.returncode == 0, resolved.stderr
    assert json.loads(resolved.stdout)["modelFamily"] == f"exact:{identity}"
    result = invoke(
        repo, "run", "release.md", "--model-id", identity,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["modelId"] == identity


def test_status_needs_no_executor_identity_but_progress_does(repo):
    setup(repo)
    assert start(repo).returncode == 0
    status = invoke(repo, "status", "release.md")
    assert status.returncode == 0
    assert json.loads(status.stdout)["modelId"] == "Model-X"
    progress = invoke(repo, "step", "release.md", "verify", "--title", "Verify")
    assert progress.returncode != 0
    assert "--model-id" in progress.stderr


def test_executor_mismatch_precedes_document_id_ledger_migration(repo):
    setup(repo)
    assert start(repo).returncode == 0
    before = {path.name: path.read_bytes() for path in (repo / ".runbooks").glob("*.json")}
    (repo / "release.md").write_text(
        "---\nid: release\n---\n# Release\n\n1. Verify.\n", encoding="utf-8",
    )
    result = invoke(
        repo, "run", "release.md", "--model-id", "Other-Model",
    )
    assert result.returncode == 2
    assert "does not match recorded modelId" in result.stderr
    after = {path.name: path.read_bytes() for path in (repo / ".runbooks").glob("*.json")}
    assert after == before


def test_all_session_aliases_are_preflighted_before_any_filename_migration(repo):
    setup(repo)
    assert start(repo, model_id="Other-Model").returncode == 0
    path_ledger = ledger(repo)
    document_id = f"id-{'a' * 64}"
    document_state = json.loads(path_ledger.read_text(encoding="utf-8"))
    document_state.update(
        runbookId=document_id,
        modelId="Model-X",
    )
    legacy_document_ledger = repo / ".runbooks" / f"{document_id}.json"
    legacy_document_ledger.write_text(json.dumps(document_state), encoding="utf-8")
    (repo / "release.md").write_text(
        f"---\nid: {document_id}\n---\n# Release\n\n1. Verify.\n", encoding="utf-8",
    )
    before = {path.name: path.read_bytes() for path in (repo / ".runbooks").glob("*.json")}

    result = invoke(
        repo, "run", "release.md", "--model-id", "Model-X",
    )

    assert result.returncode == 2
    assert "does not match recorded modelId" in result.stderr
    after = {path.name: path.read_bytes() for path in (repo / ".runbooks").glob("*.json")}
    assert after == before


def test_registration_validates_unrelated_legacy_candidate_before_migration(repo):
    setup(repo)
    assert start(repo).returncode == 0
    source = ledger(repo)
    document_id = f"id-{'c' * 64}"
    unrelated_legacy = repo / ".runbooks" / f"{document_id}.json"
    unrelated_legacy.write_text("{}", encoding="utf-8")
    (repo / "release.md").write_text(
        f"---\nid: {document_id}\n---\n# Release\n\n1. Verify.\n", encoding="utf-8",
    )
    before = {path.name: path.read_bytes() for path in (repo / ".runbooks").glob("*.json")}

    result = invoke(repo, "register", document_id, "release.md")

    assert result.returncode == 2
    assert "Unsupported session schema" in result.stderr
    assert {path.name: path.read_bytes() for path in (repo / ".runbooks").glob("*.json")} == before
    assert source.is_file()
    assert not (repo / ".runbooks" / f"_id-{'c' * 64}.json").exists()


def test_prune_rejects_malformed_active_ledger(repo):
    setup(repo)
    assert start(repo).returncode == 0
    path = ledger(repo)
    state = json.loads(path.read_text(encoding="utf-8"))
    del state["modelFamily"]
    path.write_text(json.dumps(state), encoding="utf-8")
    before = path.read_bytes()

    result = invoke(repo, "prune", "release.md", "--keep-last", "0")

    assert result.returncode == 2
    assert "modelFamily" in result.stderr
    assert path.read_bytes() == before


def test_status_validates_unsafe_legacy_ledger_before_filename_migration(repo):
    setup(repo)
    runbook_id = f"id-{'b' * 64}"
    assert invoke(repo, "register", runbook_id, "release.md").returncode == 0
    started = invoke(
        repo, "run", runbook_id,
        "--model-family", "frontier",
        "--model-id", "Model-X",
    )
    assert started.returncode == 0, started.stderr
    canonical = Path(json.loads(started.stdout)["statePath"])
    state = json.loads(canonical.read_text(encoding="utf-8"))
    del state["modelId"]
    legacy = repo / ".runbooks" / f"{runbook_id}.json"
    legacy.write_text(json.dumps(state), encoding="utf-8")
    canonical.unlink()
    before = legacy.read_bytes()

    result = invoke(repo, "status", runbook_id)

    assert result.returncode == 2
    assert "modelId" in result.stderr
    assert legacy.read_bytes() == before
    assert not canonical.exists()


def test_register_validates_unsafe_legacy_ledger_before_filename_migration(repo):
    setup(repo)
    assert start(repo).returncode == 0
    path = ledger(repo)
    state = json.loads(path.read_text(encoding="utf-8"))
    path.unlink()
    runbook_id = f"id-{'c' * 64}"
    state["runbookId"] = runbook_id
    del state["modelFamily"]
    legacy = repo / ".runbooks" / f"{runbook_id}.json"
    legacy.write_text(json.dumps(state), encoding="utf-8")
    before = legacy.read_bytes()

    result = invoke(repo, "register", runbook_id, "release.md")

    assert result.returncode == 2
    assert "modelFamily" in result.stderr
    assert legacy.read_bytes() == before
    assert not list((repo / ".runbooks").glob("_id-*.json"))
    assert not (repo / "runbooks.json").exists()
