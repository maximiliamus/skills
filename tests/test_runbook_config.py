"""Project configuration lifecycle and path protection."""

from __future__ import annotations

import json
import ntpath
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_runbook_session import (
    REGISTRY,
    Client,
    call,
    create_directory_link_or_skip,
    load_runbook_module,
    marked,
)
from test_runbook_session import repo as repo  # noqa: PLC0414 -- shared pytest fixture


def config_module():
    load_runbook_module()
    from execute_guided_runbook_lib import configuration

    return configuration


def custom_config():
    return {
        "schemaVersion": 2,
        "stateDirectory": ".runbooks",
        "runbooks": [
            {
                "id": "release",
                "path": "release.md",
                "title": "Custom release",
                "description": "Project-specific instructions",
            },
        ],
    }


@pytest.mark.parametrize("marker", ["AGENTS.md", ".agents/skills/execute-guided-runbook/SKILL.md"])
def test_config_commands_discover_non_git_project_from_context(
    tmp_path,
    monkeypatch,
    capsys,
    marker,
    non_git_project,
):
    module = load_runbook_module()
    anchor = tmp_path / marker
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text("Project instructions.\n", encoding="utf-8")
    working = tmp_path / "docs"
    working.mkdir()
    monkeypatch.chdir(working)
    for operation in ("init", "update"):
        assert module.main([operation]) == 0
        assert json.loads(capsys.readouterr().out)["changed"] == (operation == "init")
    assert (tmp_path / REGISTRY).is_file()
    assert not (working / REGISTRY).exists()


@pytest.mark.parametrize("git", [False, True])
def test_nested_instructions_preserve_config_and_journal(
    repo,
    tmp_path,
    monkeypatch,
    git,
    request,
):
    if not git:
        request.getfixturevalue("non_git_project")
    module = load_runbook_module()
    path = tmp_path / REGISTRY
    config = custom_config()
    client = Client(repo, marked("prepare"), relative="release.md", configuration=config)
    client.reserve(selector="release")
    journal = client.path_to_journal.read_bytes()
    working = tmp_path / "docs"
    working.mkdir()
    (working / "AGENTS.md").write_text("Documentation instructions.\n", encoding="utf-8")
    monkeypatch.chdir(working)
    before, modified = path.read_bytes(), path.stat().st_mtime_ns
    for operation in ("update", "init"):
        assert module.main([operation]) == 0
    assert path.read_bytes() == before and path.stat().st_mtime_ns == modified
    assert not (working / REGISTRY).exists()
    assert client.status("release")["runId"] == client.run
    assert client.path_to_journal.read_bytes() == journal
    assert len(list((repo / ".runbooks" / ".locks").glob("*.lock"))) == 1


def test_init_uses_template_and_update_preserves_existing_bytes(repo, monkeypatch):
    module = config_module()
    path = repo / REGISTRY
    assert call(repo, "init")["changed"]
    assert path.read_bytes() == module.CONFIG_TEMPLATE.read_bytes()
    path.write_text(json.dumps(custom_config(), indent=4) + "\n", encoding="utf-8")
    before, modified = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(module, "CONFIG_TEMPLATE", repo / "absent-template.json")
    with module.config_locks(repo):
        assert module.initialize_config(repo)["config"] == custom_config()
        assert module.update_config(repo)["config"] == custom_config()
    assert path.read_bytes() == before and path.stat().st_mtime_ns == modified


@pytest.mark.parametrize(
    "value,code",
    [
        ("{broken", "INVALID_REQUEST"),
        ("[]", "INVALID_REQUEST"),
        ('{"schemaVersion":2,"runbooks":[],"runbooks":[]}', "INVALID_REQUEST"),
        ('{"schemaVersion":3,"runbooks":[]}', "UNSUPPORTED_SCHEMA"),
        ('{"schemaVersion":true,"runbooks":[]}', "UNSUPPORTED_SCHEMA"),
        ('{"schemaVersion":2.0,"runbooks":[]}', "UNSUPPORTED_SCHEMA"),
    ],
)
def test_invalid_config_is_never_repaired(repo, value, code):
    path = repo / REGISTRY
    path.write_text(value, encoding="utf-8")
    before = path.read_bytes()
    for command in ("init", "update", "list"):
        call(repo, command, code=code)
        assert path.read_bytes() == before


def test_earlier_registry_does_not_block_new_configuration(repo):
    canonical = repo / REGISTRY
    old = repo / "runbooks.json"
    config = custom_config()
    del config["stateDirectory"]
    old.write_text(json.dumps(config), encoding="utf-8")
    before = old.read_bytes()
    assert call(repo, "list")["runbooks"] == []
    call(repo, "update", code="NOT_FOUND")
    assert not canonical.exists()
    assert call(repo, "init")["changed"]
    assert call(repo, "update")["runbooks"] == []
    assert old.read_bytes() == before


def test_creation_and_concurrent_registrations_keep_all_entries(repo):
    with ThreadPoolExecutor(max_workers=4) as pool:
        created = list(pool.map(lambda _: call(repo, "init")["changed"], range(2)))
        assert sorted(created) == [False, True]
        names = [f"procedure-{index}" for index in range(5)]
        for name in names:
            (repo / f"{name}.md").write_text("# Procedure", encoding="utf-8")
        assert all(
            pool.map(lambda name: call(repo, "register", name, f"{name}.md")["changed"], names)
        )
    assert [entry["id"] for entry in call(repo, "list")["runbooks"]] == names


@pytest.mark.skipif(os.name != "nt", reason="Windows path resolution race")
def test_init_prepares_lock_parent_before_resolving_lock_directory(repo, monkeypatch, capsys):
    module = load_runbook_module()
    state_dir = repo.resolve() / ".runbooks"
    lock_dir = state_dir / ".locks"
    get_final_path = ntpath._getfinalpathname

    def create_parent_during_lookup(path):
        try:
            return get_final_path(path)
        except FileNotFoundError as exc:
            if exc.winerror == 3 and Path(path) == lock_dir:
                state_dir.mkdir(exist_ok=True)
            raise

    monkeypatch.setattr(ntpath, "_getfinalpathname", create_parent_during_lookup)
    assert module.main(["--repo-root", str(repo), "init"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"]
    assert len(list(lock_dir.glob("*.lock"))) == 1


def test_last_instant_external_creation_is_not_overwritten(repo, monkeypatch):
    module = config_module()
    path = repo / REGISTRY
    publish = module.publish_new

    def competing(source, target):
        with target.open("x", encoding="utf-8") as handle:
            json.dump(custom_config(), handle)
        return publish(source, target)

    monkeypatch.setattr(module, "publish_new", competing)
    with pytest.raises(FileExistsError), module.config_locks(repo):
        module.initialize_config(repo)
    assert json.loads(path.read_bytes()) == custom_config()
    assert not list(repo.glob(".*.tmp"))


@pytest.mark.parametrize("name", ["runner-state", "..", True])
def test_noncanonical_state_directory_is_rejected_before_locking(repo, name):
    config = {"schemaVersion": 2, "stateDirectory": name, "runbooks": []}
    (repo / REGISTRY).write_text(json.dumps(config), encoding="utf-8")
    call(repo, "list", code="INVALID_REQUEST")
    assert not (repo / ".runbooks").exists()
    assert not (repo / "runner-state").exists()


@pytest.mark.parametrize("part", ["directory", ".locks", "runs"])
def test_store_rejects_redirected_directory(repo, part):
    target = repo / "redirect-target"
    target.mkdir()
    store = repo / ".runbooks"
    if part == "directory":
        link = store
    else:
        store.mkdir()
        link = store / part
    create_directory_link_or_skip(link, target)
    result = call(repo, "list", code="INVALID_REQUEST")
    assert "symbolic link or junction" in result["message"]
    assert not list(target.iterdir())


def test_copied_runbook_cannot_borrow_registered_identity(repo):
    config = custom_config()
    client = Client(
        repo, marked("prepare", config="id: release"), relative="release.md", configuration=config
    )
    client.reserve()
    (repo / "copied.md").write_bytes(client.path.read_bytes())
    call(
        repo,
        "reserve",
        "copied.md",
        payload=client.initial
        | {
            "runId": "00000000-0000-4000-9000-000000000001",
        },
        code="INVALID_REQUEST",
    )
    call(repo, "status", "copied.md", code="NOT_FOUND")
    assert client.status("release")["runId"] == client.run


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_registry_and_journal_preserve_posix_modes(repo):
    config = repo / REGISTRY
    call(repo, "init")
    config.chmod(0o640)
    client = Client(repo, marked("prepare"))
    call(repo, "register", "book", "run.md")
    client.reserve()
    assert config.stat().st_mode & 0o777 == 0o640
    assert client.path_to_journal.stat().st_mode & 0o777 == 0o600
    client.path_to_journal.chmod(0o640)
    client.admit("prepare")
    assert client.path_to_journal.stat().st_mode & 0o777 == 0o640


@pytest.mark.skipif(os.name != "nt", reason="Windows discretionary access rules")
def test_registry_replacement_preserves_protected_windows_dacl(repo):
    call(repo, "init")
    path = repo / REGISTRY
    subprocess.run(["icacls", str(path), "/inheritance:d"], check=True, capture_output=True)

    def access_rules():
        return subprocess.check_output(["icacls", str(path)])

    before = access_rules()
    Client(repo, marked("prepare"))
    assert call(repo, "register", "book", "run.md")["changed"]
    assert access_rules() == before
