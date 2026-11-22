"""Run-folder retention, restart ownership and exact-source continuation."""

import io
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from test_runbook_session import (
    DECISION,
    EVIDENCE,
    REGISTRY,
    Client,
    call,
    load_runbook_module,
    marked,
)
from test_runbook_session import repo as repo  # noqa: PLC0414 -- shared pytest fixture


@pytest.fixture
def invoke_cli(repo, monkeypatch, capsys):
    module = load_runbook_module()

    def invoke(*args, payload):
        stdin = type("Input", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})
        with monkeypatch.context() as patch:
            patch.setattr(sys, "stdin", stdin)
            code = module.main(["--repo-root", str(repo), *args])
        return code, json.loads(capsys.readouterr().out)

    return invoke


def restart_payload(previous):
    return {
        "runId": previous.run,
        "ifRevision": previous.view["revision"],
        "decision": DECISION,
        "evidence": EVIDENCE,
    }


def abandon(client):
    client.change("close", {"mode": "abandon", "decision": DECISION})


def test_default_retention_rotates_only_oldest_run_folder(repo):
    instructions = marked(
        "report",
        ending=(
            "<!-- deliverable: report.md -->\n"
            "<!-- deliverable: evidence/coverage.json -->\n"
            "<!-- deliverable: repo:published/{runId}/report.md -->\n"
        ),
    )
    (repo / "report.md").write_bytes(b"Existing repository report")
    other = Client(repo, marked("step"), relative="other.md")
    other.reserve()
    runs = []
    for _ in range(3):
        client = Client(repo, instructions)
        view = client.reserve()
        folder = f".runbooks/history/{client.run}"
        assert view["artifactsDirectory"] == f"{folder}/artifacts"
        assert view["stagingDirectory"] == f"{folder}/staging"
        root = repo / view["artifactsDirectory"]
        (root / "support.txt").write_bytes(b"Run-specific supporting data")
        paths = [
            f"{folder}/artifacts/report.md",
            f"{folder}/artifacts/evidence/coverage.json",
            f"published/{client.run}/report.md",
        ]
        assert view["contract"]["steps"][0]["deliverables"] == paths
        attempt = client.admit("report")
        outputs = [client.stage(attempt, path, str(index)) for index, path in enumerate(paths)]
        # Distinct staging paths also apply to private and public reports with the same name.
        client.result(attempt, deliverables=outputs)
        client.finish()
        runs.append(client)

    oldest = runs[0]
    journal = oldest.journal()
    journal["events"][-1]["at"] = "2099-10-01T12:00:00+00:00"
    oldest.path_to_journal.write_text(json.dumps(journal), encoding="utf-8")
    before = {run.path_to_journal: run.path_to_journal.read_bytes() for run in runs}
    assert call(repo, "status", "run.md")["runId"] == runs[-1].run
    failed = Client(repo, instructions, facts={"absent-step": {}})
    failed.reserve(code="INVALID_REQUEST")
    assert all(path.read_bytes() == saved for path, saved in before.items())

    fourth = Client(repo, instructions)
    assert fourth.reserve()["phase"] == "active"
    assert all(path.read_bytes() == saved for path, saved in before.items())
    fourth.change("set-pause", {"requested": True, "decision": DECISION})
    fourth.finish(code="BLOCKED")
    assert all(path.read_bytes() == saved for path, saved in before.items())
    fourth.change("set-pause", {"requested": False, "decision": DECISION})
    attempt = fourth.admit("report")
    paths = fourth.view["contract"]["steps"][0]["deliverables"]
    fourth.result(attempt, deliverables=[fourth.stage(attempt, path, "new") for path in paths])
    assert all(path.read_bytes() == saved for path, saved in before.items())
    fourth.finish()
    assert not oldest.path_to_journal.parent.exists()
    assert all(run.path_to_journal.is_file() for run in [*runs[1:], fourth, other])
    assert (repo / "published" / oldest.run / "report.md").read_text() == "2"
    assert (repo / "report.md").read_bytes() == b"Existing repository report"
    assert not (repo / "evidence").exists()
    call(repo, "status", oldest.run, code="NOT_FOUND")
    oldest.reserve(code="CONFLICT")


@pytest.mark.parametrize(
    "project_limit,override,expected_registered,expected_unregistered",
    [(1, "all", 3, 1), ("all", 2, 2, 3)],
)
def test_retention_override_works_for_id_and_path(
    repo, project_limit, override, expected_registered, expected_unregistered,
):
    config = {
        "schemaVersion": 2,
        "journalRetention": project_limit,
        "runbooks": [{"id": "registered", "path": "run.md", "journalRetention": override}],
    }
    (repo / REGISTRY).write_text(json.dumps(config), encoding="utf-8")
    registered = []
    unregistered = []
    for selector in ("registered", "run.md", "registered"):
        client = Client(repo, marked("step"))
        client.reserve(selector=selector)
        assert call(repo, "status", "registered")["runId"] == client.run
        assert call(repo, "status", "run.md")["runId"] == client.run
        client.result(client.admit("step"))
        client.finish()
        registered.append(client)

        other = Client(repo, marked("step"), relative="other.md")
        other.reserve()
        other.result(other.admit("step"))
        other.finish()
        unregistered.append(other)

    assert [run.path_to_journal.is_file() for run in registered] == (
        [False] * (3 - expected_registered) + [True] * expected_registered
    )
    assert [run.path_to_journal.is_file() for run in unregistered] == (
        [False] * (3 - expected_unregistered) + [True] * expected_unregistered
    )


def test_registry_move_keeps_history_separate_from_new_book_at_old_path(repo):
    first = Client(repo, marked("step"))
    call(repo, "register", "original", "run.md")
    first.reserve(selector="original")
    abandon(first)
    moved = Client(repo, marked("step"), relative="moved.md")
    call(repo, "register", "original", "moved.md")
    call(repo, "register", "replacement", "run.md")
    assert call(repo, "status", "moved.md")["runId"] == first.run
    call(repo, "status", "replacement", code="NOT_FOUND")
    moved.reserve()
    unrelated = Client(repo, marked("step"))
    unrelated.reserve(selector="replacement")
    assert call(repo, "status", "original")["runId"] == moved.run
    assert call(repo, "status", "run.md")["runId"] == unrelated.run


@pytest.mark.parametrize("hardlink", [False, True])
def test_register_rejects_two_ids_for_one_file_without_writing_config(repo, hardlink):
    client = Client(repo, marked("step"))
    call(repo, "register", "original", client.relative)
    registry = repo / REGISTRY
    before = registry.read_bytes()
    alias = client.relative
    if hardlink:
        alias = "alias.md"
        os.link(client.path, repo / alias)
    call(repo, "register", "replacement", alias, code="CONFLICT")
    assert registry.read_bytes() == before
    assert client.reserve(selector="original")["runId"] == client.run
    assert client.status(alias)["runId"] == client.run


def test_ambiguous_registry_blocks_close_without_retiring_another_books_run(repo):
    config = {"schemaVersion": 2, "runbooks": [
        {"id": "first", "path": "first.md", "journalRetention": 1},
        {"id": "second", "path": "second.md", "journalRetention": "all"},
    ]}
    registry = repo / REGISTRY
    registry.write_text(json.dumps(config), encoding="utf-8")
    second = Client(repo, marked("step"), relative="second.md")
    second.reserve(selector="second")
    second.result(second.admit("step"))
    second.finish()
    report = repo / second.view["artifactsDirectory"] / "report.md"
    report.write_bytes(b"Verified report from the other runbook")

    first = Client(repo, marked("step"), relative="first.md")
    first.reserve(selector="first")
    first.result(first.admit("step"))
    request = first.request("close", {"mode": "finish"})
    before = first.path_to_journal.read_bytes()
    config["runbooks"][1]["path"] = "first.md"
    registry.write_text(json.dumps(config), encoding="utf-8")
    call(repo, "update", code="CONFLICT")
    call(repo, "change", first.run, payload=request, code="CONFLICT")
    assert first.path_to_journal.read_bytes() == before
    assert report.read_bytes() == b"Verified report from the other runbook"

    config["runbooks"][1]["path"] = "second.md"
    registry.write_text(json.dumps(config), encoding="utf-8")
    assert call(repo, "change", first.run, payload=request)["phase"] == "finalized"
    assert second.status()["phase"] == "finalized"
    assert report.is_file()


def test_retiring_legacy_flat_journal_preserves_earlier_external_files(repo):
    fixture = Path(__file__).with_name("fixtures") / "runbook-v4-parked.json"
    journal = json.loads(fixture.read_bytes())
    source = journal["contract"]["instructions"].encode()
    (repo / REGISTRY).write_text(
        '{"schemaVersion":2,"journalRetention":1,"runbooks":[]}', encoding="utf-8",
    )
    old = Client(repo, source.decode())
    old.path.write_bytes(source)
    old.run = journal["runId"]
    old.token = journal["events"][0]["data"]["ownershipToken"]
    flat = repo / ".runbooks" / "runs" / f"{old.run}.json"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_bytes(fixture.read_bytes())
    outside = repo / ".runbooks" / "history" / old.run / "journal.json"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(b"An older procedure's explicit publication")
    old.status()
    abandon(old)

    new = Client(repo, marked("step"))
    new.reserve()
    assert flat.exists()
    new.result(new.admit("step"))
    new.finish()
    assert not flat.exists()
    assert outside.read_bytes() == b"An older procedure's explicit publication"
    assert new.status("run.md")["runId"] == new.run


@pytest.mark.parametrize("value", [0, -1, 1.5, 3.0, True, None, "3", "ALL"])
@pytest.mark.parametrize("entry_override", [False, True])
def test_rejects_invalid_retention(repo, value, entry_override):
    config = {"schemaVersion": 2, "runbooks": []}
    if entry_override:
        config["runbooks"] = [{"id": "example", "path": "run.md", "journalRetention": value}]
    else:
        config["journalRetention"] = value
    (repo / REGISTRY).write_text(json.dumps(config), encoding="utf-8")
    assert "journalRetention" in call(repo, "list", code="INVALID_REQUEST")["message"]


def test_ordinary_selection_is_latest_and_explicit_older_resume_remains_possible(repo):
    text = marked("first", "second")
    older = Client(repo, text)
    older.reserve()
    older.result(older.admit("first"))
    journal = older.journal()
    for index, event in enumerate(journal["events"]):
        event["at"] = f"2000-01-01T00:00:0{index}+00:00"
    older.path_to_journal.write_text(json.dumps(journal), encoding="utf-8")
    status = older.status("run.md")
    assert status["runId"] == older.run and status["sourceMatches"]
    assert status["phase"] == "active" and status["pausedAt"] is None
    assert status["startedAt"] == journal["events"][0]["at"]
    assert status["lastActivityAt"] == journal["events"][-1]["at"]
    assert status["lastStepId"] == "first"
    assert status["steps"][0]["status"] == "done" and status["ready"] == ["second"]

    latest = Client(repo, text)
    latest.initial["restart"] = restart_payload(older)
    latest.reserve()
    older.admit("second", code="CONFLICT")
    latest.result(latest.admit("first"))
    latest.result(latest.admit("second"))
    latest.finish()
    assert latest.status("run.md")["phase"] == "finalized"
    assert older.status()["parked"] and older.view["ready"] == []

    # The newer closed journal remains the status selection even if the older
    # journal has more recent events.
    older.change("takeover", {"evidence": EVIDENCE})
    assert older.view["ready"] == ["second"]
    assert latest.status("run.md")["runId"] == latest.run
    older.change("set-pause", {"requested": True, "decision": DECISION})
    fresh = Client(repo, text)
    fresh.reserve()
    assert fresh.status("run.md")["runId"] == fresh.run and older.status()["parked"]


def test_reservation_order_survives_clock_rollback_and_controls_retention(
    repo, invoke_cli, monkeypatch,
):
    from execute_guided_runbook_lib import cli

    (repo / REGISTRY).write_text(
        '{"schemaVersion":2,"journalRetention":1,"runbooks":[]}', encoding="utf-8",
    )
    older = Client(repo, marked("step"))
    older.reserve()
    older.result(older.admit("step"))
    older.finish()
    newer = Client(repo, marked("step"))
    with monkeypatch.context() as patch:
        patch.setattr(cli, "utc_now", lambda: "2000-01-01T00:00:00+00:00")
        code, newer.view = invoke_cli("reserve", newer.relative, payload=newer.initial)
    assert code == 0
    newer.token = newer.view["ownershipToken"]
    selected = newer.status("run.md")
    assert selected["runId"] == newer.run and selected["phase"] == "active"
    assert selected["startedAt"] < older.view["startedAt"]
    assert newer.reserve()["duplicate"]
    newer.result(newer.admit("step"))
    newer.finish()
    call(repo, "status", older.run, code="NOT_FOUND")
    assert not older.path_to_journal.parent.exists()


def test_pause_context_uses_settlement_event_and_restart_preserves_live_work(repo):
    client = Client(repo, marked("first", "second"))
    client.reserve()
    attempt = client.admit("first")
    client.observe(attempt, "question", text="Which input should be used?")
    client.change("set-pause", {"requested": True, "decision": DECISION})
    assert client.view["phase"] == "pausing" and client.view["pausedAt"] is None
    next_run = Client(repo, marked("first", "second"))
    next_run.initial["restart"] = restart_payload(client)
    next_run.reserve(code="BLOCKED")
    assert not next_run.path_to_journal.exists()

    client.observe(attempt, "stopped")
    stopped_at = client.journal()["events"][-1]["at"]
    status = client.status("run.md")
    assert status["phase"] == "paused" and status["pausedAt"] == stopped_at
    assert status["lastStepId"] == "first"
    assert status["steps"][0]["questionText"] == "Which input should be used?"
    next_run.reserve(code="CONFLICT")  # The earlier decision named a stale revision.
    assert client.status()["revision"] == status["revision"]


def test_finishing_an_older_run_preserves_it_until_the_newer_run_finishes(repo):
    (repo / REGISTRY).write_text(
        '{"schemaVersion":2,"journalRetention":1,"runbooks":[]}', encoding="utf-8",
    )
    older = Client(repo, marked("step"))
    older.reserve()
    newer = Client(repo, marked("step"))
    newer.initial["restart"] = restart_payload(older)
    newer.reserve()
    newer.change("set-pause", {"requested": True, "decision": DECISION})

    older.status()
    older.change("takeover", {"evidence": EVIDENCE})
    older.result(older.admit("step"))
    older.finish()
    assert older.path_to_journal.is_file()
    assert newer.status("run.md")["phase"] == "paused"

    newer.change("takeover", {"evidence": EVIDENCE})
    newer.change("set-pause", {"requested": False, "decision": DECISION})
    newer.result(newer.admit("step"))
    newer.finish()
    assert not older.path_to_journal.parent.exists()
    assert newer.path_to_journal.is_file()


def test_source_change_between_offer_and_resume_blocks_until_exact_bytes_return(repo):
    client = Client(repo, marked("first", "second"))
    client.reserve()
    client.result(client.admit("first"))
    client.change("set-pause", {"requested": True, "decision": DECISION})
    original = client.path.read_bytes()
    proposal = client.status("run.md")
    assert proposal["sourceMatches"]
    revision = proposal["revision"]
    client.path.write_bytes(original + b"\n")
    assert not client.status()["sourceMatches"]
    client.change("takeover", {"evidence": EVIDENCE}, code="BLOCKED")
    client.change("set-pause", {"requested": False, "decision": DECISION}, code="BLOCKED")
    assert client.status()["revision"] == revision
    client.path.unlink()
    assert not client.status("run.md")["sourceMatches"]

    client.change("takeover", {"evidence": EVIDENCE, "recoveryOnly": True})
    client.path.write_bytes(original)
    assert client.status()["sourceMatches"]
    client.change("set-pause", {"requested": False, "decision": DECISION}, code="BLOCKED")
    client.change("takeover", {"evidence": EVIDENCE})
    client.change("set-pause", {"requested": False, "decision": DECISION})
    assert client.view["ready"] == ["second"]
    assert client.view["contract"] == proposal["contract"]


def test_completion_and_cleanup_are_retryable_without_deleting_at_start(
    repo, invoke_cli, monkeypatch,
):
    (repo / REGISTRY).write_text(
        '{"schemaVersion":2,"journalRetention":1,"runbooks":[]}', encoding="utf-8",
    )
    client = Client(repo, marked("first"))
    client.reserve()
    client.result(client.admit("first"))
    client.finish()
    previous = client.path_to_journal.read_bytes()
    abandoned = Client(repo, marked("first"))
    abandoned.reserve()
    abandon(abandoned)
    assert client.path_to_journal.read_bytes() == previous

    fresh = Client(repo, marked("first"))
    fresh.reserve()
    fresh.result(fresh.admit("first"))
    request = fresh.request("close", {"mode": "finish"})

    from execute_guided_runbook_lib.cli import Repository
    from execute_guided_runbook_lib.history import RunStore

    def fail_commit(self, run):
        raise OSError("Injected completion save failure")

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "save", fail_commit)
        code, body = invoke_cli("change", fresh.run, payload=request)
    assert code == 1 and body["code"] == "IO_ERROR"
    assert client.path_to_journal.read_bytes() == previous
    assert client.status()["phase"] == "finalized"
    assert fresh.status()["phase"] == "active"

    def fail_cleanup(self):
        raise OSError("Injected failure after commit")

    with monkeypatch.context() as patch:
        patch.setattr(RunStore, "cleanup", fail_cleanup)
        code, body = invoke_cli("change", fresh.run, payload=request)
    assert code == 0 and body["cleanupPending"]
    assert client.path_to_journal.read_bytes() == previous
    call(repo, "status", client.run, code="NOT_FOUND")
    assert fresh.status("run.md")["phase"] == "finalized"
    later = Client(repo, marked("first"))
    assert later.reserve()["cleanupPending"]
    assert client.path_to_journal.exists()  # Starting another run leaves cleanup pending.
    assert call(repo, "change", fresh.run, payload=request)["duplicate"]
    assert not client.path_to_journal.parent.exists()


def test_retry_failed_completion_obeys_new_retention_without_reviving_old_plan(
    repo, invoke_cli, monkeypatch,
):
    from execute_guided_runbook_lib.cli import Repository

    config = {"schemaVersion": 2, "journalRetention": 1, "runbooks": []}
    registry = repo / REGISTRY
    registry.write_text(json.dumps(config), encoding="utf-8")
    older = Client(repo, marked("step"))
    older.reserve()
    older.result(older.admit("step"))
    older.finish()
    report = repo / older.view["artifactsDirectory"] / "report.md"
    report.write_bytes(b"An earlier verified report")
    saved = older.path_to_journal.read_bytes()

    newer = Client(repo, marked("step"))
    newer.reserve()
    newer.result(newer.admit("step"))
    request = newer.request("close", {"mode": "finish"})

    def fail_save(self, run):
        raise OSError("Interrupted before the close event was saved")

    with monkeypatch.context() as patch:
        patch.setattr(Repository, "save", fail_save)
        code, response = invoke_cli("change", newer.run, payload=request)
    assert code == 1 and response["code"] == "IO_ERROR"
    assert older.status()["phase"] == "finalized"

    registry.write_text(json.dumps(config | {"journalRetention": "all"}), encoding="utf-8")
    assert call(repo, "change", newer.run, payload=request)["phase"] == "finalized"
    assert call(repo, "change", newer.run, payload=request)["duplicate"]
    assert older.status()["phase"] == "finalized"
    assert older.path_to_journal.read_bytes() == saved
    assert report.read_bytes() == b"An earlier verified report"


def test_cleanup_continues_after_one_runs_folder_cannot_be_deleted(
    repo, invoke_cli, monkeypatch,
):
    from execute_guided_runbook_lib import history

    (repo / REGISTRY).write_text(
        '{"schemaVersion":2,"journalRetention":1,"runbooks":[]}', encoding="utf-8",
    )

    def ready(relative):
        client = Client(repo, marked("step"), relative=relative)
        client.reserve()
        client.result(client.admit("step"))
        return client

    old_a, old_b = ready("first.md"), ready("second.md")
    old_a.finish()
    old_b.finish()
    folder_a = old_a.path_to_journal.parent
    folder_b = old_b.path_to_journal.parent
    quarantined_a = repo / ".runbooks" / "runs" / f".retired-{old_a.run}"
    new_a, new_b = ready("first.md"), ready("second.md")
    request_a = new_a.request("close", {"mode": "finish"})
    request_b = new_b.request("close", {"mode": "finish"})
    rmtree = history.shutil.rmtree

    def deny_first(path, *args, **kwargs):
        if path == quarantined_a:
            raise PermissionError("Run folder is in use")
        return rmtree(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(history.shutil, "rmtree", deny_first)
        for client, request in [(new_a, request_a), (new_b, request_b)]:
            code, response = invoke_cli("change", client.run, payload=request)
            assert code == 0 and response["cleanupPending"]
        assert not folder_a.exists() and quarantined_a.is_dir()
        assert not folder_b.exists()
    replay = call(repo, "change", new_a.run, payload=request_a)
    assert replay["duplicate"] and not replay.get("cleanupPending")
    assert not quarantined_a.exists()


def test_stopped_writer_must_be_reconciled_before_abandonment(repo):
    (repo / REGISTRY).write_text(
        '{"schemaVersion":2,"journalRetention":1,"runbooks":[]}', encoding="utf-8",
    )
    client = Client(repo, marked("check"))
    client.reserve()
    snapshot = repo / client.view["artifactsDirectory"] / "before.bin"
    snapshot.write_bytes(b"the original state")
    attempt = client.admit("check", mutation={
        "target": "shared.txt", "preconditions": "Compare original bytes", "verify": "Inspect target",
    })
    stopped = client.observe(attempt, "stopped")
    assert stopped["recovery"][0]["effectsPending"]
    assert not stopped["recovery"][0]["live"]
    before = client.path_to_journal.read_bytes()
    blocked = client.change("close", {"mode": "abandon", "decision": DECISION}, code="BLOCKED")
    assert "effects" in blocked["message"]
    replacement = Client(repo, marked("check"))
    replacement.initial["restart"] = restart_payload(client)
    replacement.reserve(code="BLOCKED")
    assert client.path_to_journal.read_bytes() == before
    assert snapshot.read_bytes() == b"the original state"

    client.change("resolve", {
        "kind": "attempt", "attempt": attempt, "effects": "no-effect", "evidence": EVIDENCE,
    })
    abandon(client)
    del replacement.initial["restart"]
    replacement.reserve()
    replacement.result(replacement.admit("check"))
    replacement.finish()
    assert not client.path_to_journal.exists()
    assert not snapshot.exists()


@pytest.mark.parametrize(
    ("version", "layout", "queued"),
    [(4, "flat", None), (4, "flat", "pending"), (5, "flat", "cleanup"), (5, "folder", "cleanup")],
)
def test_legacy_abandonment_with_effects_survives_rotation_and_can_be_reconciled(
    repo, version, layout, queued,
):
    # Both fixtures were saved by the v0.4.0 helper. The v4 fixture extends
    # its existing parked journal, and the v5 fixture was reserved from scratch.
    fixture = Path(__file__).with_name("fixtures") / f"runbook-v{version}-abandoned-effects.json"
    original = fixture.read_bytes()
    journal = json.loads(original)
    old = Client(repo, journal["contract"]["instructions"])
    old.run = journal["runId"]
    old.token = journal["events"][0]["data"]["ownershipToken"]
    state = repo / ".runbooks"
    if layout == "flat":
        journal_path = state / "runs" / f"{old.run}.json"
        evidence_file = state / "staging" / old.run / "before.bin"
    else:
        folder = state / "history" / old.run
        folder.mkdir(parents=True)
        (folder / ".runbook-session").write_bytes((old.run + "\n").encode())
        journal_path = folder / "journal.json"
        evidence_file = folder / "artifacts" / "before.bin"
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_bytes(original)
    evidence_file.parent.mkdir(parents=True)
    evidence_file.write_bytes(b"Original information about the shared effect")
    (repo / REGISTRY).write_text(
        '{"schemaVersion":2,"journalRetention":1,"runbooks":[]}', encoding="utf-8",
    )

    def finish_next():
        client = Client(repo, marked("check"))
        client.reserve()
        client.result(client.admit("check"))
        request = client.request("close", {"mode": "finish"})
        assert call(repo, "change", client.run, payload=request)["phase"] == "finalized"
        return client, request

    new, close_request = finish_next()
    assert journal_path.read_bytes() == original and evidence_file.is_file()
    index = state / "runs" / ".retention.json"
    if queued:
        older_index = {"retired": [], "parked": {}}
        if queued == "pending":
            close = new.journal()["events"][-1]
            older_index["pending"] = {
                "runId": new.run, "seq": close["seq"], "digest": close["requestDigest"],
                "retire": {old.run: layout}, "park": {},
            }
        else:
            older_index.update(retired=[old.run], cleanup={old.run: layout})
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps(older_index), encoding="utf-8")
    status = old.status()
    assert status["phase"] == "abandoned"
    [unsettled] = status["recovery"]
    assert unsettled["effectsPending"] and not unsettled["live"]
    if queued:
        assert json.loads(index.read_bytes()) == older_index  # status has no side effects
    assert journal_path.read_bytes() == original and evidence_file.is_file()

    if queued == "cleanup":
        assert call(repo, "change", new.run, payload=close_request)["duplicate"]
        assert evidence_file.is_file()
    if queued != "pending":
        old.change("takeover", {"evidence": EVIDENCE}, code="CONFLICT")
        old.change("takeover", {"recoveryOnly": True, "evidence": EVIDENCE})
    old.change("resolve", {
        "kind": "attempt", "attempt": unsettled["attempt"], "evidence": EVIDENCE,
    }, code="CONFLICT")
    request = old.request("resolve", {
        "kind": "attempt", "attempt": unsettled["attempt"], "effects": "no-effect",
        "evidence": EVIDENCE,
    })
    resolved = call(repo, "change", old.run, payload=request)
    assert resolved["phase"] == "abandoned" and not resolved["recovery"]
    assert call(repo, "change", old.run, payload=request)["duplicate"]
    assert call(repo, "change", new.run, payload=close_request)["duplicate"]
    assert journal_path.is_file() and evidence_file.is_file()
    assert not old.status()["recovery"]

    finish_next()
    call(repo, "status", old.run, code="NOT_FOUND")
    assert not journal_path.exists()
    assert evidence_file.exists() == (layout == "flat")


def test_retry_cleanup_does_not_delete_files_recreated_at_the_original_location(
    repo, invoke_cli, monkeypatch,
):
    from execute_guided_runbook_lib.history import RunStore

    (repo / REGISTRY).write_text(
        '{"schemaVersion":2,"journalRetention":1,"runbooks":[]}', encoding="utf-8",
    )
    older = Client(repo, marked("check"))
    older.reserve()
    older.result(older.admit("check"))
    older.finish()
    folder = older.path_to_journal.parent
    newer = Client(repo, marked("check"))
    newer.reserve()
    newer.result(newer.admit("check"))
    request = newer.request("close", {"mode": "finish"})
    write_index = RunStore.write_index

    def fail_after_deletion(self, data):
        if data.get("cleanup") == {} and self.index.get("cleanup"):
            raise OSError("Stopped before recording the completed cleanup")
        return write_index(self, data)

    with monkeypatch.context() as patch:
        patch.setattr(RunStore, "write_index", fail_after_deletion)
        code, response = invoke_cli("change", newer.run, payload=request)
    assert code == 0 and response["cleanupPending"] and not folder.exists()
    folder.mkdir()
    unrelated = folder / "external.txt"
    unrelated.write_bytes(b"New data at the old location")
    assert call(repo, "change", newer.run, payload=request)["duplicate"]
    assert unrelated.read_bytes() == b"New data at the old location"
    assert older.reserve(code="CONFLICT")["message"] == "Run ID was previously used and retired"


@pytest.mark.parametrize(
    "target",
    [
        "repo:.runbooks/history/{runId}/journal.json",
        "repo:.runbooks/history/{runId}/staging/report.md",
        f"repo:.runbooks/history/{uuid4()}/artifacts/report.md",
    ],
)
def test_deliverables_cannot_write_to_reserved_run_paths(repo, target):
    client = Client(repo, marked("report", ending=f"<!-- deliverable: {target} -->\n"))
    client.reserve(code="INVALID_REQUEST")
    assert not client.path_to_journal.exists()
