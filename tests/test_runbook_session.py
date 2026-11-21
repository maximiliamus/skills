from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest

RUNBOOK_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "execute-guided-runbook"
    / "scripts"
    / "runbook_session.py"
)


def load_runbook_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runbook_session_under_test", RUNBOOK_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    script_directory = str(RUNBOOK_SCRIPT.parent)
    sys.path.insert(0, script_directory)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(script_directory)
    return module


def test_in_process_import_does_not_claim_generic_lib_name(
    monkeypatch: pytest.MonkeyPatch,
):
    unrelated_lib = ModuleType("lib")
    monkeypatch.setitem(sys.modules, "lib", unrelated_lib)

    module = load_runbook_module()

    assert module.main is module.cli.main
    assert sys.modules["lib"] is unrelated_lib


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test User"], check=True)
    return tmp_path


def create_directory_link_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as exc:
        if sys.platform != "win32":
            pytest.skip(f"Directory symlinks are not available: {exc}")
    junction = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if junction.returncode != 0:
        pytest.skip(f"Directory links are not available: {junction.stderr}")


def test_find_repo_root_uses_repository_marker_without_running_git(repo: Path):
    module = load_runbook_module()
    nested = repo / "nested" / "directory"
    nested.mkdir(parents=True)

    assert module.find_repo_root(nested) == repo.resolve()


def test_repository_registry_rejects_file_symlink(repo: Path):
    module = load_runbook_module()
    outside = repo.parent / f"{repo.name}-outside-registry.json"
    try:
        (repo / "execute-guided-runbook.json").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"File symlinks are not available: {exc}")

    with pytest.raises(module.RunbookError, match="must not be a symbolic link or junction"):
        module.repository_registry_path(repo)
    assert not outside.exists()


DECISION = {"instruction": "Proceed within the recorded scope", "reference": "conversation:42"}
REGISTRY = "execute-guided-runbook.json"
MODES = {"CONFLICT": 2, "BLOCKED": 2, "DECISION_REQUIRED": 2}


def cli_command(repo, *args):
    return [sys.executable, str(RUNBOOK_SCRIPT), "--repo-root", str(repo), *args]


def run_cli(repo, *args, payload=None):
    stdin = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload
    return subprocess.run(
        cli_command(repo, *args),
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=20,
    )


def response(result, code=None):
    assert result.returncode == (MODES.get(code, 1) if code else 0), result.stdout + result.stderr
    assert "Traceback" not in result.stderr
    body = json.loads(result.stdout)
    assert body["ok"] is (code is None)
    if code:
        assert body["code"] == code and isinstance(body["details"], dict), body
    return body


def call(repo, *args, payload=None, code=None):
    return response(run_cli(repo, *args, payload=payload), code)


def marked(*ids, config="", ending=""):
    header = f"---\n{config.strip()}\n---\n\n" if config else ""
    return (
        header
        + "# Procedure\n\n"
        + "".join(
            f"<!-- step: {sid} -->\n## {sid.title()}\nInstructions for {sid}.\n\n" for sid in ids
        )
        + ending
    )


def binding(model_id="model-A", family="current", tier="medium", effort="medium"):
    return {"modelFamily": family, "modelId": model_id, "modelTier": tier, "effortLevel": effort}


def artifact(repo, path):
    return {"path": path, "sha256": hashlib.sha256((repo / path).read_bytes()).hexdigest()}



EVIDENCE = {"statement": "Command exit and result were verified", "reference": "worker:exit"}


class Client:
    def __init__(
        self, repo, text, *, relative="run.md", configuration=None, model=None,
        model_family=None, decision=None, facts=None,
    ):
        self.repo, self.relative = repo, relative
        self.path = repo / relative
        self.path.write_text(text, encoding="utf-8")
        if configuration:
            (repo / REGISTRY).write_text(json.dumps(configuration), encoding="utf-8")
        self.run = str(uuid4())
        self.initial = {"runId": self.run, "stepContracts": facts or {}}
        if model_family:
            self.initial["modelFamily"] = model_family
        if decision:
            self.initial["overrideDecision"] = decision
        self.model = model
        self.view = self.token = None

    @property
    def path_to_journal(self):
        return self.repo / ".runbooks" / "runs" / f"{self.run}.json"

    def journal(self):
        return json.loads(self.path_to_journal.read_bytes())

    def reserve(self, *, selector=None, code=None):
        body = call(self.repo, "reserve", selector or self.relative, payload=self.initial, code=code)
        if not code:
            self.view, self.token = body, body["ownershipToken"]
        return body

    def status(self, selector=None):
        self.view = call(self.repo, "status", selector or self.run)
        return self.view

    def request(self, kind, data, *, revision=None):
        request = {
            "requestId": str(uuid4()), "ifRevision": revision or self.view["revision"],
            "type": kind, "data": data,
        }
        if kind != "takeover":
            request["ownershipToken"] = self.token
        return request

    def change(self, kind, data, *, code=None):
        body = call(self.repo, "change", self.run, payload=self.request(kind, data), code=code)
        if not code:
            self.view = body
            self.token = body.get("ownershipToken", self.token)
        return body

    def admission(self, sid, *, execution="delegated", dependencies=None, sources=None, **extra):
        data = {
            "stepId": sid, "execution": execution, "prerequisites": dependencies or {},
            "sources": sources or [],
        }
        if self.model:
            data["model"] = self.model
        return data | extra

    def admit(self, sid, *, code=None, **kwargs):
        response = self.change("admit", self.admission(sid, **kwargs), code=code)
        return response["details"] if code else self.step(sid)["attempt"]

    def observe(self, attempt, kind, *, code=None, **extra):
        record = {"attempt": attempt, "kind": kind}
        if kind not in {"task", "question"}:
            record["evidence"] = EVIDENCE
        return self.change("observe", record | extra, code=code)

    def result(self, attempt, *, passed=True, code=None, **extra):
        return self.observe(attempt, "result", outcome="PASS" if passed else "FAIL", code=code, **extra)

    def stage(self, attempt, target, text, *, prior=None):
        staged = f".runbooks/staging/{self.run}/{attempt}/{Path(target).name}"
        path = self.repo / staged
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        result = {
            "path": target, "stagedPath": staged, "sha256": artifact(self.repo, staged)["sha256"],
            "priorSha256": prior,
        }
        if prior is not None:
            result["replacement"] = EVIDENCE
        return result

    def step(self, sid):
        return next(step for step in self.view["steps"] if step["id"] == sid)

    def finish(self, *, code=None, **extra):
        return self.change("close", {"mode": "finish", **extra}, code=code)


def active(repo, *ids, config="", facts=None):
    client = Client(repo, marked(*ids, config=config), facts=facts)
    client.reserve()
    return client


def test_v4_parked_run_resumes_with_original_order_and_outcomes(repo):
    # Generated by the released v0.4.0 CLI, with a stopped worker and an open question.
    fixture = Path(__file__).with_name("fixtures") / "runbook-v4-parked.json"
    original = fixture.read_bytes()
    journal = json.loads(original)
    client = Client(repo, journal["contract"]["instructions"], relative=journal["runbook"]["path"])
    call(repo, "register", "review-docs", "run.md")
    client.run = journal["runId"]
    client.token = journal["events"][0]["data"]["ownershipToken"]
    path = client.path_to_journal
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)

    view = client.status("review-docs")
    assert view["schemaVersion"] == 4 and view["contract"] == journal["contract"]
    assert path.read_bytes() == original
    assert view["ready"] == ["second"] and client.step("first")["question"] is not None
    conflict = call(repo, "reserve", "review-docs", payload={"runId": str(uuid4())}, code="CONFLICT")
    assert conflict["details"]["runId"] == client.run

    event = client.result(client.admit("second"))["revision"]
    result = client.step("second")["assessment"]
    assert result["outcome"] == "PASS" and "reason" not in result and "evidence" not in result
    client.change("resolve", {
        "kind": "question", "hold": client.step("first")["question"], "decision": DECISION,
    })
    client.result(client.admit("first"))
    assert client.finish()["result"] == "PASSED"
    assert client.journal()["schemaVersion"] == 4
    assert client.journal()["events"][:len(journal["events"])] == journal["events"]
    assert client.status()["phase"] == "finalized"

    new = Client(repo, journal["contract"]["instructions"], relative="run.md", facts={
        "second": {
            "reuse": True,
            "procedure": [artifact(repo, "run.md") | {"section": "Second check"}],
        },
    })
    view = new.reserve(selector="review-docs")
    assert view["schemaVersion"] == 5 and view["ready"] == ["first"]
    new.result(new.admit("first"))
    new.change("assess", {
        "kind": "reuse", "stepId": "second", "source": {"runId": client.run, "event": event},
        "sources": [], "prerequisites": {}, "evidence": EVIDENCE,
    })
    reused = new.step("second")["assessment"]
    assert reused["outcome"] == "PASSED" and reused["evidence"] == EVIDENCE
    assert new.finish()["result"] == "PASSED"


def test_cli_wire_contract_and_registry_lifecycle(repo):
    assert call(repo, "list") == {"ok": True, "changed": False, "runbooks": []}
    call(repo, "run", "run.md", code="INVALID_REQUEST")
    call(repo, "reserve", "run.md", payload='{"runId":1,"runId":2}', code="INVALID_REQUEST")
    call(repo, "reserve", "run.md", payload='{"x":1e999}', code="INVALID_REQUEST")
    call(repo, "reserve", "run.md", payload=r'{"x":"\ud800"}', code="INVALID_REQUEST")
    call(repo, "status", str(uuid4()), code="NOT_FOUND")
    client = Client(repo, marked("prepare", config="description: 'Подготовка: входные данные'"))
    assert call(repo, "register", "publish", "run.md")["changed"]
    assert call(repo, "list")["runbooks"][0]["description"] == "Подготовка: входные данные"
    client.reserve(selector="publish")
    original = client.path_to_journal.read_bytes()
    assert client.reserve(selector="publish")["revision"] == 1
    assert client.path_to_journal.read_bytes() == original
    assert call(repo, "status", "run.md")["runId"] == client.run
    call(
        repo, "reserve", "run.md", payload=client.initial | {"runId": str(uuid4())}, code="CONFLICT"
    )
    call(
        repo, "reserve", "publish", payload=client.initial | {"stepContracts": {"prepare": {}}}, code="CONFLICT"
    )
    call(repo, "unregister", "publish", code="CONFLICT")
    call(repo, "register", "copy", "run.md", code="CONFLICT")
    client.result(client.admit("prepare"))
    final = client.finish()
    assert final["result"] == "PASSED" and final["phase"] == "finalized"
    assert client.status()["score"] == {"earned": 1, "available": 1}
    call(repo, "status", "publish", code="NOT_FOUND")
    assert call(repo, "unregister", "publish")["runbooks"] == []
    assert client.path.exists()
    assert client.path_to_journal.exists()
    call(
        repo,
        "change",
        client.run,
        payload=client.request("close", {"mode": "abandon"}),
        code="CONFLICT",
    )


def inferred_inventory(client, *anchors):
    lines = client.path.read_text(encoding="utf-8").splitlines()
    return {
        "sha256": artifact(client.repo, client.relative)["sha256"],
        "steps": [{"id": sid, "startLine": lines.index(anchor) + 1} for sid, anchor in anchors],
    }


def test_unmarked_headings_freeze_coordinator_inventory_for_resume(repo):
    text = """\
# Verify delivery

Read the repository rules.

~~~markdown
<!-- step: illustration -->
## Example step
~~~

## 1. Verify delivery inputs

Check the inventory.

### Source details

Inspect the files listed in the inventory.

## 2. Record findings

Record a summary.

## Notes

These notes are outside the steps.
"""
    client = Client(repo, text)
    call(repo, "register", "delivery", client.relative)
    client.reserve(selector="delivery", code="INVALID_REQUEST")
    client.initial["stepInventory"] = inferred_inventory(
        client, ("verify-inputs", "## 1. Verify delivery inputs"), ("report", "## 2. Record findings"),
    )
    run = client.reserve(selector="delivery")
    assert run["ready"] == ["verify-inputs"]
    frozen = run["contract"]
    assert [step["title"] for step in frozen["steps"]] == [
        "1. Verify delivery inputs", "2. Record findings",
    ]
    instructions = frozen["instructions"].splitlines()

    def step_body(step):
        span = step["instruction"]
        return instructions[span["startLine"] - 1:span["endLine"]]

    assert "### Source details" in step_body(frozen["steps"][0])
    assert "## 2. Record findings" not in step_body(frozen["steps"][0])
    assert "## Notes" not in step_body(frozen["steps"][1])
    client.admit("report", decision=DECISION, code="BLOCKED")
    client.result(client.admit("verify-inputs"))
    client.path.write_text("# Revised worktree\n", encoding="utf-8")
    assert client.status("delivery")["contract"] == frozen
    assert client.view["ready"] == ["report"]
    assert client.reserve(selector="delivery")["duplicate"]
    client.admit("report")
    assert client.journal()["contract"] == frozen


def test_unmarked_ordered_steps_capture_local_content_and_deliverable(repo):
    client = Client(
        repo,
        "# Checklist\n\n"
        "1. Collect inputs\n"
        "   - Include the changed files.\n"
        "2. Publish the report\n"
        "   Use the verified inputs.\n\n"
        "<!-- deliverable: reports/{runId}/report.md -->\n\n"
        "## Notes\n\nThe repository rules govern access.\n",
    )
    client.initial["stepInventory"] = inferred_inventory(
        client, ("collect", "1. Collect inputs"), ("publish", "2. Publish the report"),
    )
    view = client.reserve()
    first, second = view["contract"]["steps"]
    assert (first["title"], second["title"]) == ("Collect inputs", "Publish the report")
    assert first["instruction"]["endLine"] == second["instruction"]["startLine"] - 1
    target = f"reports/{client.run}/report.md"
    assert second["deliverables"] == [target] and not first["deliverables"]
    client.result(client.admit("collect"))
    attempt = client.admit("publish")
    staged = client.stage(attempt, target, "Verified report.")
    client.result(attempt, deliverables=[staged])
    assert client.finish()["result"] == "PASSED"


@pytest.mark.parametrize(
    ("anchors", "message"),
    [
        ([], "stepInventory.steps"),
        ([("code", "## Fenced example")], "top-level heading or ordered item"),
        ([("quote", "> ## Quoted example")], "top-level heading or ordered item"),
        ([("title", "# Procedure")], "top-level heading or ordered item"),
        ([("one", "## First"), ("one", "## Second")], "duplicate step ID"),
        ([("two", "## Second"), ("one", "## First")], "overlap or are out of order"),
        ([("one", "## First"), ("part", "### First details")], "overlap or are out of order"),
    ],
)
def test_unmarked_inventory_rejects_invalid_anchors_and_order(repo, anchors, message):
    client = Client(
        repo,
        "# Procedure\n\n```md\n## Fenced example\n```\n\n"
        "> ## Quoted example\n\n## First\n\n### First details\n\n## Second\n",
    )
    client.initial["stepInventory"] = inferred_inventory(client, *anchors)
    result = client.reserve(code="INVALID_REQUEST")
    assert message in result["message"]
    assert not client.path_to_journal.exists()


def test_unmarked_inventory_rejects_stale_source_and_marked_replacement(repo):
    client = Client(repo, "# Procedure\n\n## First\n")
    client.initial["stepInventory"] = inferred_inventory(client, ("first", "## First"))
    client.path.write_text("# Procedure\n\n## New worktree title\n", encoding="utf-8")
    assert "changed" in client.reserve(code="CONFLICT")["message"]
    assert not client.path_to_journal.exists()

    client.path.write_text(marked("first"), encoding="utf-8")
    client.initial["stepInventory"] = inferred_inventory(client, ("replacement", "## First"))
    assert "cannot replace marked steps" in client.reserve(code="INVALID_REQUEST")["message"]
    assert not client.path_to_journal.exists()
    client.initial.pop("stepInventory")
    assert client.reserve()["ready"] == ["first"]


@pytest.mark.parametrize(
    "config", [
        "maxParallelSteps: 2",
        "stepOrder: arbitrary",
        "steps:\n  second:\n    dependsOn:\n      first: done",
    ],
)
def test_unmarked_inventory_requires_simple_sequential_runbook(repo, config):
    client = Client(repo, f"---\n{config}\n---\n\n# Procedure\n\n## First\n\n## Second\n")
    client.initial["stepInventory"] = inferred_inventory(
        client, ("first", "## First"), ("second", "## Second"),
    )
    result = client.reserve(code="INVALID_REQUEST")
    assert "requires a sequential runbook" in result["message"]
    assert not client.path_to_journal.exists()


def test_marker_inventory_ignores_markdown_examples_and_keeps_unmarked_headings(repo):
    examples = """
# Preamble
~~~markdown
<!-- step: fenced -->
## Fenced example
~~~

    <!-- step: indented -->
    ## Indented example

> <!-- step: quoted -->
> ## Quoted example

- List item

  <!-- step: list-item -->
  ## List example

<!--
<!-- step: comment -->
-->
"""
    client = Client(
        repo,
        examples
        + marked("first", "second").replace(
            "Instructions for first.", "### Unmarked step subsection\nInstructions for first."
        ),
    )
    client.reserve()
    assert [step["id"] for step in client.view["steps"]] == ["first", "second"]
    info = client.journal()["contract"]["steps"][0]["instruction"]
    lines = client.path.read_text(encoding="utf-8").splitlines()
    assert "### Unmarked step subsection" in lines[info["startLine"] - 1 : info["endLine"]]


@pytest.mark.parametrize(
    "text",
    [
        "# Empty\n",
        "<!-- step: -->\n## Empty\n",
        "<!-- step: bad_ID -->\n## Invalid\n",
        "<!-- step: a -->\n\n## Orphan marker\n",
        "<!-- step: a -->\n## One\n<!-- step: a -->\n## Two\n",
        "<!-- deliverable: output -->\n" + marked("a"),
        marked("a", ending="<!-- deliverable: ../escape -->\n"),
        marked("a", ending="<!-- deliverable: out -->\n<!-- deliverable: out -->\n"),
        marked("a", ending="<!-- deliverable: out -->\n<!-- deliverable: OUT -->\n"),
        marked("a", ending="<!-- deliverable: out -->\n<!-- deliverable: out/child.md -->\n"),
        marked("a", ending="<!-- deliverable: out --> trailing\n"),
    ],
)
def test_bad_markers_are_rejected_before_a_journal_exists(repo, text):
    client = Client(repo, text)
    client.reserve(code="INVALID_REQUEST")
    assert not client.path_to_journal.exists()


def test_v2_journal_and_archives_stay_in_place_with_zero_progress_on_restart(repo):
    client = Client(repo, marked("first", "second"))
    store = repo / ".runbooks"
    old = store / "release.json"
    archived = store / "archive" / f"release.20260912T120000.000000Z.{uuid4().hex}.json"
    archived.parent.mkdir(parents=True)
    call(repo, "register", "release", "run.md")
    # Schema-2 ledger fields from the previous executor's new_state.
    v2 = {
        "schemaVersion": 2,
        "assessmentVersion": 1,
        "runbookId": "release",
        "runbookTitle": "Release",
        "runbookPath": "run.md",
        "registered": True,
        "runbookSha256": artifact(repo, "run.md")["sha256"],
        **binding(),
        "acceptancePolicy": "flexible",
        "stepOrder": "sequential",
        "acceptanceThreshold": None,
        "startedAt": "2026-09-12T12:00:00+00:00",
        "updatedAt": "2026-09-12T12:00:00+00:00",
        "status": "active",
        "currentStep": None,
        "revisionDecisions": [],
        "history": [
            {
                "id": "first",
                "title": "First",
                "attempt": 1,
                "status": "completed",
                "stepResult": "PASS",
                "score": {"earned": 1, "available": 1, "percent": 100, "display": "1/1 (100%)"},
                "evidence": "Output verified",
                "startedAt": "2026-09-12T12:00:00+00:00",
                "completedAt": "2026-09-12T12:00:00+00:00",
            }
        ],
    }
    old.write_text(json.dumps(v2), encoding="utf-8")
    archived.write_text(
        json.dumps(
            v2
            | {
                "status": "ignored",
                "archiveReason": "ignored",
                "archivedAt": "2026-09-12T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    before = {path: path.read_bytes() for path in (old, archived)}
    call(repo, "status", "release", code="NOT_FOUND")
    call(repo, "status", archived.relative_to(repo).as_posix(), code="NOT_FOUND")
    reserved = client.reserve(selector="release")
    assert reserved["revision"] == 1 and reserved["score"] == {"earned": 0, "available": 0}
    assert all(step["charged"] == 0 and step["assessment"] is None for step in reserved["steps"])
    assert all(path.read_bytes() == saved for path, saved in before.items())
    assert client.journal()["schemaVersion"] == 5
    assert client.path_to_journal.stem == client.run


def test_failed_atomic_replace_keeps_previous_revision_and_request_retryable(
    repo, monkeypatch, capsys
):
    client = active(repo, "first")
    module = load_runbook_module()
    from execute_guided_runbook_lib import storage

    request = client.request("admit", client.admission("first"))
    previous = client.path_to_journal.read_bytes()

    def replace_failure(source, target):
        assert target == client.path_to_journal and source.parent == target.parent
        raise PermissionError("Injected denial before replacement")

    monkeypatch.setattr(storage, "replace_with_retry", replace_failure)
    monkeypatch.setattr(
        sys,
        "stdin",
        type(
            "Input",
            (),
            {
                "buffer": __import__("io").BytesIO(json.dumps(request).encode("utf-8")),
            },
        )(),
    )
    assert module.main(["--repo-root", str(repo), "change", client.run]) == 1
    failure = json.loads(capsys.readouterr().out)
    assert (
        failure["code"] == "IO_ERROR" and failure["details"]["revision"] == client.view["revision"]
    )
    assert client.path_to_journal.read_bytes() == previous
    assert not list(client.path_to_journal.parent.glob("*.tmp"))
    committed = call(repo, "change", client.run, payload=request)
    assert committed["revision"] == client.view["revision"] + 1


@pytest.mark.parametrize("identifier", ["aux", "r" * 300])
def test_registry_identifier_never_becomes_a_journal_filename(repo, identifier):
    client = Client(repo, marked("prepare"))
    call(repo, "register", identifier, client.relative)
    client.reserve(selector=identifier)
    assert client.path_to_journal.is_file()
    assert client.journal()["runbook"]["id"] == identifier
    assert list(client.path_to_journal.parent.iterdir()) == [client.path_to_journal]


def test_physical_runbook_alias_cannot_open_a_second_run(repo):
    import os

    client = Client(repo, marked("prepare"))
    os.link(client.path, repo / "alias.md")
    client.reserve()
    assert client.status("alias.md")["runId"] == client.run
    call(
        repo,
        "reserve",
        "alias.md",
        payload=client.initial | {"runId": str(uuid4())},
        code="CONFLICT",
    )


def test_outstanding_local_question_blocks_assessment_after_stop(repo):
    client = active(repo, "check")
    attempt = client.admit("check")
    question = client.observe(attempt, "question", text="Authorize the selected input?")["revision"]
    client.result(attempt, code="BLOCKED")
    client.observe(attempt, "stopped")
    assert client.step("check")["attempt"] is None
    assert client.step("check")["question"] == question
    client.result(attempt, code="BLOCKED")
    client.change("resolve", {"kind": "question", "hold": question, "decision": DECISION})
    client.result(attempt)
    assert client.finish()["result"] == "PASSED"


@pytest.mark.parametrize("disposition", ["no-effect", "safe-repeat"])
def test_retry_after_verified_effect_disposition(repo, disposition):
    client = active(repo, "write", config="maxAttempts: 2")
    mutation = {"target": "output", "preconditions": "before digest", "verify": "compare digest"}
    attempt = client.admit("write", mutation=mutation)
    client.observe(attempt, "technical-failure", reason="Interrupted", transient=True)
    client.admit("write", mutation=mutation, code="BLOCKED")
    client.change("resolve", {
        "kind": "attempt", "attempt": attempt, "effects": disposition, "evidence": EVIDENCE,
    })
    retry = client.admit("write", mutation=mutation)
    client.result(retry)
    assert client.step("write")["charged"] == 2
    assert client.finish()["result"] == "PASSED"


def test_abandonment_requires_settlement_and_operator_authority(repo):
    client = active(repo, "check")
    attempt = client.admit("check")
    client.change("close", {"mode": "abandon", "decision": DECISION}, code="BLOCKED")
    client.observe(attempt, "stopped")
    client.change("close", {"mode": "abandon"}, code="DECISION_REQUIRED")
    assert client.change("close", {"mode": "abandon", "decision": DECISION})["phase"] == "abandoned"


@pytest.mark.parametrize("reported_task", [False, True])
def test_optional_harness_handle_does_not_change_charge_or_slot(repo, reported_task):
    client = active(repo, "check")
    attempt = client.admit("check")
    if reported_task:
        client.observe(attempt, "task", taskId="native-worker-reference")
    assert client.step("check")["charged"] == 1 and client.view["availableSlots"] == 0
    assert client.step("check")["attempts"][0]["taskId"] == (
        "native-worker-reference" if reported_task else None
    )
    client.result(attempt)
    assert client.finish()["result"] == "PASSED"


def test_v3_journal_stays_in_place_and_does_not_block_a_fresh_run(repo):
    # Schema and reservation event follow the preceding v3 helper.
    client = Client(repo, marked("check"))
    old_id, launch = str(uuid4()), str(uuid4())
    path = repo / ".runbooks" / "runs" / f"{old_id}.json"
    path.parent.mkdir(parents=True)
    runbook_sha = artifact(repo, "run.md")["sha256"]
    journal = {
        "schemaVersion": 3, "runId": old_id,
        "runbook": {"path": "run.md", "sha256": runbook_sha},
        "contract": {
            "maxParallelSteps": 1, "delegateSteps": False, "stepOrder": "sequential",
            "acceptancePolicy": "flexible", "acceptanceThreshold": None,
            "steps": [{
                "id": "check", "title": "Check", "deliverables": [], "dependsOn": {},
                "instruction": {"path": "run.md", "sha256": runbook_sha, "startLine": 4, "endLine": 6},
                "modelFamily": "current", "modelFamilyOrigin": "default", "modelTier": "medium",
                "effortLevel": "medium", "delegateSteps": False, "maxAttempts": 1,
                "procedure": [], "gapCauses": [], "reuse": False,
                "reassessAfterInvalidation": False, "points": 1,
            }],
        },
        "events": [{
            "seq": 1, "at": "2026-11-16T12:00:00+00:00", "requestId": old_id,
            "requestDigest": "a" * 64, "type": "reserve",
            "data": {"route": "native", "launchId": launch, "binding": binding()},
        }],
    }
    path.write_text(json.dumps(journal), encoding="utf-8")
    saved = path.read_bytes()
    call(repo, "status", old_id, code="UNSUPPORTED_SCHEMA")
    call(repo, "status", "run.md", code="NOT_FOUND")
    call(repo, "change", old_id, payload={}, code="UNSUPPORTED_SCHEMA")
    call(repo, "reserve", "run.md", payload={"runId": old_id}, code="UNSUPPORTED_SCHEMA")
    reserved = client.reserve()
    assert reserved["phase"] == "active" and reserved["revision"] == 1
    assert reserved["steps"][0]["assessment"] is None
    assert client.status("run.md")["runId"] == client.run
    assert path.read_bytes() == saved
