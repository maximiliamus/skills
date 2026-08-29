from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import UTC
from datetime import datetime as real_datetime
from pathlib import Path
from types import ModuleType

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


def cli_command(repo: Path, *args: str) -> list[str]:
    cmd = [sys.executable, str(RUNBOOK_SCRIPT), "--repo-root", str(repo)]
    cmd.extend(args)
    return cmd


def run_cli(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cli_command(repo, *args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


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


def test_empty_registry_list(repo: Path):
    result = run_cli(repo, "list")
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["schemaVersion"] == 1
    assert data["runbooks"] == []
    assert not (repo / ".runbooks").exists()


def test_list_with_registry_uses_registry_lock(repo: Path, monkeypatch: pytest.MonkeyPatch):
    registry_path = repo / "runbooks.json"
    registry_path.write_text(
        json.dumps({"schemaVersion": 1, "runbooks": []}),
        encoding="utf-8",
    )
    module = load_runbook_module()
    lock_active = False
    locked_targets: list[Path] = []

    class RecordingLock:
        def __enter__(self):
            nonlocal lock_active
            lock_active = True

        def __exit__(self, exc_type, exc_value, traceback):
            nonlocal lock_active
            lock_active = False

    def recording_lock(repo_root: Path, target: Path):
        assert repo_root == repo.resolve()
        locked_targets.append(target)
        return RecordingLock()

    def assert_locked(registry: dict[str, dict[str, str]]):
        assert lock_active
        assert registry == {}

    monkeypatch.setattr(module.cli, "interprocess_lock", recording_lock)
    monkeypatch.setattr(module.cli, "command_list", assert_locked)
    monkeypatch.setattr(module.cli, "configure_standard_streams", lambda: None)
    monkeypatch.setattr(sys, "argv", [str(RUNBOOK_SCRIPT), "--repo-root", str(repo), "list"])

    assert module.main() == 0
    assert locked_targets == [registry_path]
    assert not lock_active


@pytest.mark.parametrize("root_kind", ["missing", "file"])
def test_list_rejects_invalid_repo_root(repo: Path, root_kind: str):
    invalid_root = repo / "invalid-root"
    if root_kind == "file":
        invalid_root.write_text("not a directory\n", encoding="utf-8")

    result = run_cli(invalid_root, "list")

    assert result.returncode == 2
    assert "Repository root is not a directory" in result.stderr
    assert "Traceback" not in result.stderr


def test_find_repo_root_uses_repository_marker_without_running_git(repo: Path):
    module = load_runbook_module()
    nested = repo / "nested" / "directory"
    nested.mkdir(parents=True)

    assert module.find_repo_root(nested) == repo.resolve()


def test_registry_override_option_is_not_supported(repo: Path):
    result = run_cli(repo, "--registry", str(repo / "other.json"), "list")

    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_set_step_compatibility_alias_is_not_supported(repo: Path):
    result = run_cli(
        repo,
        "set-step",
        "release",
        "step-1",
        "--title",
        "Step 1",
    )

    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_register_and_run_lifecycle(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    runbook_md = docs_dir / "sample-deploy.md"
    runbook_md.write_text("# Sample Deploy Runbook\n\n## Phase 1\nStep 1 text\n", encoding="utf-8")

    # Register
    res = run_cli(repo, "register", "sample-deploy", "docs/sample-deploy.md")
    assert res.returncode == 0
    reg_data = json.loads(res.stdout)
    assert "registered" in reg_data
    assert reg_data["registered"]["id"] == "sample-deploy"
    assert reg_data["registered"]["effortLevel"] == "medium"
    assert reg_data["registered"]["modelTier"] == "medium"
    assert "registryPath" in reg_data
    assert Path(reg_data["registryPath"]) == repo / "runbooks.json"

    # List
    res = run_cli(repo, "list")
    assert res.returncode == 0
    list_data = json.loads(res.stdout)
    assert len(list_data["runbooks"]) == 1
    assert list_data["runbooks"][0]["id"] == "sample-deploy"

    # Start session
    res = run_cli(repo, "run", "sample-deploy")
    assert res.returncode == 0
    session_data = json.loads(res.stdout)
    assert session_data["status"] == "active"
    assert session_data["runbookId"] == "sample-deploy"
    assert session_data["effortLevel"] == "medium"
    assert session_data["modelTier"] == "medium"
    assert "statePath" in session_data

    # Set step
    res = run_cli(
        repo,
        "step",
        "sample-deploy",
        "phase-1.1",
        "--title",
        "First step",
    )
    assert res.returncode == 0
    step_data = json.loads(res.stdout)
    assert step_data["currentStep"]["id"] == "phase-1.1"

    # Complete step
    res = run_cli(repo, "complete", "sample-deploy", "--evidence", "Verified cluster ready")
    assert res.returncode == 0
    comp_data = json.loads(res.stdout)
    assert len(comp_data["history"]) == 1
    assert comp_data["history"][0]["status"] == "completed"

    # Finish runbook
    res = run_cli(repo, "finish", "sample-deploy", "--evidence", "All phases verified")
    assert res.returncode == 0
    fin_data = json.loads(res.stdout)
    assert fin_data["status"] == "completed"

    # Status check
    res = run_cli(repo, "status", "sample-deploy")
    assert res.returncode == 0
    stat_data = json.loads(res.stdout)
    assert stat_data["status"] == "completed"

    # Verify session file was stored in the ignored repository-local state directory.
    session_file = repo / ".runbooks" / "sample-deploy.json"
    assert session_file.is_file()


def test_cli_emits_utf8_json_for_unicode_metadata(repo: Path):
    runbook = repo / "release.md"
    runbook.write_text("# Release\n", encoding="utf-8")
    title = "Релиз 🚀"
    description = "Проверка Unicode ✅"

    registered = run_cli(
        repo,
        "register",
        "release",
        "release.md",
        "--title",
        title,
        "--description",
        description,
    )
    listed = run_cli(repo, "list")

    assert registered.returncode == 0
    assert json.loads(registered.stdout)["registered"]["title"] == title
    assert listed.returncode == 0
    entry = json.loads(listed.stdout)["runbooks"][0]
    assert entry["title"] == title
    assert entry["description"] == description


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing is required")
def test_cli_emits_utf8_diagnostics_for_localized_os_errors(repo: Path):
    runbook = repo / "release.md"
    runbook.write_text("# Release\n", encoding="utf-8")
    registry_path = repo / "runbooks.json"
    registry_path.write_text(
        json.dumps({"schemaVersion": 1, "runbooks": []}),
        encoding="utf-8",
    )

    with registry_path.open("rb"):
        result = run_cli(repo, "register", "release", "release.md")

    assert result.returncode == 2
    assert "Could not write JSON file" in result.stderr
    assert "Traceback" not in result.stderr


def test_status_marks_completed_older_revision_as_outdated(repo: Path):
    runbook_md = repo / "release.md"
    runbook_md.write_text("# Release\n\nOriginal instructions\n", encoding="utf-8")
    assert run_cli(repo, "register", "release", "release.md").returncode == 0
    started = json.loads(run_cli(repo, "run", "release").stdout)
    assert (
        run_cli(repo, "finish", "release", "--evidence", "Original revision done").returncode == 0
    )

    runbook_md.write_text("# Release\n\nNew required step\n", encoding="utf-8")
    result = run_cli(repo, "status", "release")

    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["status"] == "completed"
    assert data["outdated"] is True
    assert data["outdatedReason"] == "completed_runbook_changed"
    assert data["runbookSha256"] == started["runbookSha256"]
    assert data["currentRunbookSha256"] != data["runbookSha256"]
    assert data["currentRunbookPath"] == "release.md"

    step = run_cli(repo, "step", "release", "step-1", "--title", "Step 1")
    assert step.returncode == 2
    assert "complete for an older revision" in step.stderr
    assert "operator_decision_required" not in step.stdout


def test_register_with_custom_profiles(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    runbook_md = docs_dir / "complex-migration.md"
    runbook_md.write_text("# Complex Migration\n\nInstructions\n", encoding="utf-8")

    res = run_cli(
        repo,
        "register",
        "production-migration",
        "docs/complex-migration.md",
        "--effort-level",
        "high",
        "--model-tier",
        "heavy",
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["registered"]["id"] == "production-migration"
    assert data["registered"]["effortLevel"] == "high"
    assert data["registered"]["modelTier"] == "heavy"


def test_register_updates_existing_entry(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    original = docs_dir / "original.md"
    replacement = docs_dir / "replacement.md"
    original.write_text("# Original\n", encoding="utf-8")
    replacement.write_text("# Replacement\n", encoding="utf-8")

    assert run_cli(repo, "register", "release", "docs/original.md").returncode == 0
    result = run_cli(
        repo,
        "register",
        "release",
        "docs/replacement.md",
        "--title",
        "Updated Release",
        "--effort-level",
        "high",
        "--model-tier",
        "heavy",
    )

    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["registered"]["path"] == "docs/replacement.md"
    assert data["registered"]["title"] == "Updated Release"
    assert data["registered"]["effortLevel"] == "high"
    assert data["registered"]["modelTier"] == "heavy"

    listed = json.loads(run_cli(repo, "list").stdout)
    assert listed["runbooks"] == [data["registered"]]


def test_register_partial_update_preserves_unspecified_metadata(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    original = docs_dir / "original.md"
    replacement = docs_dir / "replacement.md"
    original.write_text("# Original\n", encoding="utf-8")
    replacement.write_text("# Replacement\n", encoding="utf-8")

    assert (
        run_cli(
            repo,
            "register",
            "release",
            "docs/original.md",
            "--title",
            "Custom Release",
            "--description",
            "Custom description",
            "--effort-level",
            "high",
            "--model-tier",
            "heavy",
        ).returncode
        == 0
    )

    result = run_cli(
        repo,
        "register",
        "release",
        "docs/replacement.md",
        "--title",
        "Renamed Release",
    )

    assert result.returncode == 0
    entry = json.loads(result.stdout)["registered"]
    assert entry == {
        "id": "release",
        "title": "Renamed Release",
        "path": "docs/replacement.md",
        "description": "Custom description",
        "effortLevel": "high",
        "modelTier": "heavy",
    }


@pytest.mark.parametrize(
    ("option", "expected_error"),
    [
        ("--title", "Runbook title must not be empty"),
        ("--description", "Runbook description must not be empty"),
    ],
)
def test_register_rejects_explicit_empty_metadata(
    repo: Path,
    option: str,
    expected_error: str,
):
    runbook = repo / "release.md"
    runbook.write_text("# Release\n", encoding="utf-8")

    result = run_cli(repo, "register", "release", "release.md", option, "")

    assert result.returncode == 2
    assert expected_error in result.stderr
    assert not (repo / "runbooks.json").exists()


def test_register_rejects_path_that_registry_cannot_reload(repo: Path):
    runbook = repo / " release.md"
    runbook.write_text("# Release\n", encoding="utf-8")

    result = run_cli(repo, "register", "release", " release.md")

    assert result.returncode == 2
    assert "Registry path must not contain surrounding whitespace" in result.stderr
    assert not (repo / "runbooks.json").exists()


def test_register_with_extra_effort_level(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    runbook_md = docs_dir / "deep-investigation.md"
    runbook_md.write_text("# Deep Investigation\n\nInstructions\n", encoding="utf-8")

    res = run_cli(
        repo,
        "register",
        "deep-investigation",
        "docs/deep-investigation.md",
        "--effort-level",
        "extra",
        "--model-tier",
        "heavy",
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["registered"]["effortLevel"] == "extra"
    assert data["registered"]["modelTier"] == "heavy"


def test_long_runbook_id_uses_portable_hashed_session_filename(repo: Path):
    runbook_id = "a" * 300
    alias_id = f"id-{hashlib.sha256(runbook_id.encode()).hexdigest()}"
    runbook = repo / "release.md"
    alias_runbook = repo / "alias.md"
    runbook.write_text("# Release\n", encoding="utf-8")
    alias_runbook.write_text("# Alias\n", encoding="utf-8")

    registered = run_cli(repo, "register", runbook_id, "release.md")
    alias_registered = run_cli(repo, "register", alias_id, "alias.md")
    started = run_cli(repo, "run", runbook_id)
    alias_started = run_cli(repo, "run", alias_id)

    assert registered.returncode == 0
    assert alias_registered.returncode == 0
    assert started.returncode == 0
    assert alias_started.returncode == 0
    state = json.loads(started.stdout)
    alias_state = json.loads(alias_started.stdout)
    state_path = Path(state["statePath"])
    alias_state_path = Path(alias_state["statePath"])
    assert state["runbookId"] == runbook_id
    assert state_path.name.startswith("id-")
    assert len(state_path.name) < 128
    assert state_path.is_file()
    assert alias_state["runbookId"] == alias_id
    assert alias_state_path.name.startswith("_id-")
    assert alias_state_path != state_path
    assert alias_state_path.is_file()


def test_reserved_windows_runbook_id_uses_hashed_session_filename(repo: Path):
    runbook = repo / "release.md"
    runbook.write_text("# Release\n", encoding="utf-8")

    assert run_cli(repo, "register", "con", "release.md").returncode == 0
    started = run_cli(repo, "run", "con")

    assert started.returncode == 0
    state_path = Path(json.loads(started.stdout)["statePath"])
    assert state_path.name.startswith("_id-")
    assert state_path.is_file()


def test_legacy_unsafe_session_filename_is_migrated(repo: Path):
    module = load_runbook_module()
    runbook_id = f"id-{'a' * 64}"
    runbook = repo / "release.md"
    runbook.write_text("# Release\n", encoding="utf-8")
    assert run_cli(repo, "register", runbook_id, "release.md").returncode == 0
    resolved = json.loads(run_cli(repo, "resolve", runbook_id).stdout)
    state = module.new_state(resolved)
    legacy_path = repo / ".runbooks" / f"{runbook_id}.json"
    module.write_state(legacy_path, state)
    canonical_path = module.session_path(repo, runbook_id)
    assert canonical_path != legacy_path
    assert not canonical_path.exists()

    status = run_cli(repo, "status", runbook_id)

    assert status.returncode == 0
    assert not legacy_path.exists()
    assert canonical_path.is_file()
    assert json.loads(status.stdout)["startedAt"] == state["startedAt"]


def test_concurrent_registrations_preserve_every_entry(repo: Path):
    temporary_roots = [repo / "temp-a", repo / "temp-b"]
    for temporary_root in temporary_roots:
        temporary_root.mkdir()

    runbook_ids = [f"parallel-{index}" for index in range(16)]
    processes: list[tuple[str, subprocess.Popen[str]]] = []
    for index, runbook_id in enumerate(runbook_ids):
        relative_path = f"{runbook_id}.md"
        (repo / relative_path).write_text(f"# {runbook_id}\n", encoding="utf-8")
        environment = os.environ.copy()
        temporary_root = str(temporary_roots[index % len(temporary_roots)])
        environment.update({"TEMP": temporary_root, "TMP": temporary_root, "TMPDIR": temporary_root})
        process = subprocess.Popen(
            cli_command(repo, "register", runbook_id, relative_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        processes.append((runbook_id, process))

    failures: list[str] = []
    for runbook_id, process in processes:
        stdout, stderr = process.communicate(timeout=30)
        if process.returncode != 0:
            failures.append(f"{runbook_id}: stdout={stdout!r}, stderr={stderr!r}")

    assert not failures
    listed = json.loads(run_cli(repo, "list").stdout)
    assert [entry["id"] for entry in listed["runbooks"]] == sorted(runbook_ids)


def test_interprocess_locks_use_repository_local_state(repo: Path):
    module = load_runbook_module()

    lock_path = module.lock_file_path(repo, repo / "runbooks.json")

    assert lock_path.parent == repo / ".runbooks" / ".locks"
    assert lock_path.suffix == ".lock"


def test_unregister_lifecycle(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    runbook_md = docs_dir / "temp-runbook.md"
    runbook_md.write_text("# Temp Runbook\n", encoding="utf-8")

    run_cli(repo, "register", "temp-runbook", "docs/temp-runbook.md")
    res = run_cli(repo, "unregister", "temp-runbook")
    assert res.returncode == 0
    unreg_data = json.loads(res.stdout)
    assert unreg_data["unregistered"]["id"] == "temp-runbook"
    assert unreg_data["runbookDeleted"] is False

    # Check list is empty
    list_res = run_cli(repo, "list")
    assert json.loads(list_res.stdout)["runbooks"] == []
    # Check Markdown file still exists
    assert runbook_md.is_file()


def test_step_block_and_skip_history(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    runbook_md = docs_dir / "pipeline.md"
    runbook_md.write_text("# Pipeline\n", encoding="utf-8")

    run_cli(repo, "register", "pipeline", "docs/pipeline.md")
    run_cli(repo, "run", "pipeline")

    # Block step
    run_cli(repo, "step", "pipeline", "step-1", "--title", "Step 1")
    res_block = run_cli(repo, "block", "pipeline", "--reason", "Waiting for database credentials")
    assert res_block.returncode == 0
    block_data = json.loads(res_block.stdout)
    assert block_data["currentStep"]["status"] == "blocked"
    assert block_data["currentStep"]["blockReason"] == "Waiting for database credentials"

    # Skip step
    res_skip = run_cli(repo, "skip", "pipeline", "--reason", "Manual bypass authorized")
    assert res_skip.returncode == 0
    skip_data = json.loads(res_skip.stdout)
    assert skip_data["currentStep"] is None
    assert len(skip_data["history"]) == 1
    assert skip_data["history"][0]["status"] == "skipped"
    assert skip_data["history"][0]["skipReason"] == "Manual bypass authorized"


def test_concurrent_step_updates_leave_exactly_one_current_step(repo: Path):
    runbook = repo / "parallel.md"
    runbook.write_text("# Parallel\n", encoding="utf-8")
    assert run_cli(repo, "register", "parallel", "parallel.md").returncode == 0
    assert run_cli(repo, "run", "parallel").returncode == 0

    processes = [
        subprocess.Popen(
            cli_command(
                repo,
                "step",
                "parallel",
                f"step-{index}",
                "--title",
                f"Step {index}",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        for index in range(8)
    ]
    results = [process.communicate(timeout=30) for process in processes]

    assert sum(process.returncode == 0 for process in processes) == 1, results
    status = json.loads(run_cli(repo, "status", "parallel").stdout)
    assert status["currentStep"]["id"] in {f"step-{index}" for index in range(8)}
    assert status["history"] == []


def test_outdated_runbook_decision_continue(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    runbook_md = docs_dir / "evolving.md"
    runbook_md.write_text("# Initial Revision\n\nStep 1\n", encoding="utf-8")

    run_cli(repo, "register", "evolving", "docs/evolving.md")
    run_cli(repo, "run", "evolving")
    run_cli(repo, "step", "evolving", "step-1", "--title", "First step")
    run_cli(repo, "complete", "evolving", "--evidence", "Done")

    # Modify markdown file while session is active
    runbook_md.write_text("# Revised Version\n\nStep 1\nStep 2\n", encoding="utf-8")

    # Run without flag returns decision required (code 3)
    res = run_cli(repo, "run", "evolving")
    assert res.returncode == 3
    data = json.loads(res.stdout)
    assert data["status"] == "operator_decision_required"
    assert data["operatorChoiceRequired"] is True

    # Run with --continue updates hash and keeps history
    res_cont = run_cli(repo, "run", "evolving", "--continue")
    assert res_cont.returncode == 0
    cont_data = json.loads(res_cont.stdout)
    assert cont_data["status"] == "active"
    assert len(cont_data["history"]) == 1
    assert len(cont_data["revisionDecisions"]) == 1
    assert cont_data["revisionDecisions"][0]["decision"] == "continue"


def test_outdated_runbook_decision_ignore_and_archive(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    runbook_md = docs_dir / "evolving2.md"
    runbook_md.write_text("# Initial Revision\n\nStep 1\n", encoding="utf-8")

    run_cli(repo, "register", "evolving2", "docs/evolving2.md")
    run_cli(repo, "run", "evolving2")
    run_cli(repo, "step", "evolving2", "step-1", "--title", "First step")

    # Modify markdown file
    runbook_md.write_text("# Brand New Instructions\n", encoding="utf-8")

    # Run with --ignore archives previous session and starts new
    res_ignore = run_cli(repo, "run", "evolving2", "--ignore")
    assert res_ignore.returncode == 0
    ignore_data = json.loads(res_ignore.stdout)
    assert ignore_data["status"] == "active"
    assert ignore_data["currentStep"] is None
    assert ignore_data["history"] == []
    assert "previousSession" in ignore_data

    archive_dir = repo / ".runbooks" / "archive"
    assert archive_dir.is_dir()
    assert len(list(archive_dir.glob("evolving2.*.json"))) >= 1


def test_restart_same_revision(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    runbook_md = docs_dir / "repeatable.md"
    runbook_md.write_text("# Repeatable\n", encoding="utf-8")

    run_cli(repo, "register", "repeatable", "docs/repeatable.md")
    run_cli(repo, "run", "repeatable")
    run_cli(repo, "step", "repeatable", "step-1", "--title", "Step 1")
    run_cli(repo, "complete", "repeatable", "--evidence", "Done")
    run_cli(repo, "finish", "repeatable", "--evidence", "Finished")

    # Restart
    res_restart = run_cli(repo, "run", "repeatable", "--restart")
    assert res_restart.returncode == 0
    restart_data = json.loads(res_restart.stdout)
    assert restart_data["status"] == "active"
    assert restart_data["currentStep"] is None
    assert restart_data["history"] == []


def test_restart_rejects_changed_unfinished_revision(repo: Path):
    runbook_md = repo / "changing.md"
    runbook_md.write_text("# Initial\n", encoding="utf-8")
    assert run_cli(repo, "register", "changing", "changing.md").returncode == 0
    assert run_cli(repo, "run", "changing").returncode == 0
    assert (
        run_cli(
            repo,
            "step",
            "changing",
            "step-1",
            "--title",
            "First step",
        ).returncode
        == 0
    )
    runbook_md.write_text("# Revised\n\nNew instructions\n", encoding="utf-8")

    result = run_cli(repo, "run", "changing", "--restart")

    assert result.returncode == 2
    assert "requires the same runbook revision" in result.stderr
    saved = json.loads((repo / ".runbooks" / "changing.json").read_text(encoding="utf-8"))
    assert saved["currentStep"]["id"] == "step-1"
    assert not (repo / ".runbooks" / "archive").exists()


def test_restart_reports_archive_path_collision_without_traceback(repo: Path):
    runbook_md = repo / "repeatable.md"
    runbook_md.write_text("# Repeatable\n", encoding="utf-8")
    assert run_cli(repo, "register", "repeatable", "repeatable.md").returncode == 0
    assert run_cli(repo, "run", "repeatable").returncode == 0
    assert (
        run_cli(
            repo,
            "step",
            "repeatable",
            "step-1",
            "--title",
            "First step",
        ).returncode
        == 0
    )
    archive_path = repo / ".runbooks" / "archive"
    archive_path.write_text("collision\n", encoding="utf-8")

    result = run_cli(repo, "run", "repeatable", "--restart")

    assert result.returncode == 2
    assert "Could not create session archive directory" in result.stderr
    assert "Traceback" not in result.stderr
    saved = json.loads((repo / ".runbooks" / "repeatable.json").read_text(encoding="utf-8"))
    assert saved["currentStep"]["id"] == "step-1"
    assert archive_path.read_text(encoding="utf-8") == "collision\n"


def test_outside_repo_path_rejection(repo: Path, tmp_path: Path):
    outside_file = tmp_path.parent / "outside.md"
    outside_file.write_text("# Outside\n", encoding="utf-8")

    res = run_cli(repo, "register", "outside", str(outside_file))
    assert res.returncode == 2
    assert "outside the repository" in res.stderr


def test_non_markdown_file_rejection(repo: Path):
    script_file = repo / "script.py"
    script_file.write_text("print('hello')", encoding="utf-8")

    res = run_cli(repo, "register", "script", "script.py")
    assert res.returncode == 2
    assert "must be a Markdown file" in res.stderr


@pytest.mark.parametrize("extra_args", [(), ("--title", "Invalid Runbook")])
def test_register_reports_invalid_utf8_runbook_without_traceback(
    repo: Path,
    extra_args: tuple[str, ...],
):
    (repo / "invalid.md").write_bytes(b"\xff\xfe\xfd")

    result = run_cli(repo, "register", "invalid", "invalid.md", *extra_args)

    assert result.returncode == 2
    assert "Runbook is not valid UTF-8" in result.stderr
    assert "Traceback" not in result.stderr
    assert not (repo / "runbooks.json").exists()


def test_resolve_reports_invalid_utf8_registered_runbook_without_traceback(repo: Path):
    (repo / "invalid.md").write_bytes(b"\xff\xfe\xfd")
    (repo / "runbooks.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runbooks": [
                    {
                        "id": "invalid",
                        "path": "invalid.md",
                        "title": "Invalid Runbook",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_cli(repo, "resolve", "invalid")

    assert result.returncode == 2
    assert "Runbook is not valid UTF-8" in result.stderr
    assert "Traceback" not in result.stderr


def test_duplicate_step_rejection(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    runbook_md = docs_dir / "duplicate-check.md"
    runbook_md.write_text("# Dup Check\n", encoding="utf-8")

    run_cli(repo, "register", "duplicate-check", "docs/duplicate-check.md")
    run_cli(repo, "run", "duplicate-check")
    run_cli(repo, "step", "duplicate-check", "step-1", "--title", "Step 1")
    run_cli(repo, "complete", "duplicate-check", "--evidence", "Done")

    # Attempt to set the same step again
    res = run_cli(
        repo,
        "step",
        "duplicate-check",
        "step-1",
        "--title",
        "Step 1 Again",
    )
    assert res.returncode == 2
    assert "already exists in session history" in res.stderr


def test_finish_with_unresolved_step_rejection(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    runbook_md = docs_dir / "unresolved.md"
    runbook_md.write_text("# Unresolved\n", encoding="utf-8")

    run_cli(repo, "register", "unresolved", "docs/unresolved.md")
    run_cli(repo, "run", "unresolved")
    run_cli(repo, "step", "unresolved", "step-1", "--title", "Step 1")

    # Attempt to finish while step-1 is pending
    res = run_cli(repo, "finish", "unresolved", "--evidence", "Done")
    assert res.returncode == 2
    assert "unresolved" in res.stderr


def test_unregistered_runbook_execution_by_path(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    runbook_md = docs_dir / "quick-test.md"
    runbook_md.write_text("# Quick Test Runbook\n\n## Step 1\nDo test\n", encoding="utf-8")

    res = run_cli(repo, "run", "docs/quick-test.md")
    assert res.returncode == 0
    session_data = json.loads(res.stdout)
    assert session_data["status"] == "active"
    assert session_data["registered"] is False
    assert session_data["effortLevel"] == "medium"
    assert session_data["modelTier"] == "medium"
    assert Path(session_data["statePath"]).parent == repo / ".runbooks"


def test_register_rejects_generated_path_id_namespace(repo: Path):
    first = repo / "first.md"
    second = repo / "second.md"
    first.write_text("# Same content\n", encoding="utf-8")
    second.write_text("# Same content\n", encoding="utf-8")
    started = json.loads(run_cli(repo, "run", "first.md").stdout)
    generated_id = started["runbookId"]

    registered = run_cli(repo, "register", generated_id, "second.md")

    assert registered.returncode == 2
    assert "reserved path-session namespace" in registered.stderr
    assert not (repo / "runbooks.json").exists()
    resumed = json.loads(run_cli(repo, "status", "first.md").stdout)
    assert resumed["runbookPath"] == "first.md"


def test_register_migrates_existing_path_session(repo: Path):
    runbook = repo / "release.md"
    runbook.write_text("# Release\n", encoding="utf-8")

    started = json.loads(run_cli(repo, "run", "release.md").stdout)
    assert run_cli(repo, "step", "release.md", "prepare", "--title", "Prepare").returncode == 0
    assert (
        run_cli(repo, "complete", "release.md", "--evidence", "Preparation verified").returncode
        == 0
    )

    registered = run_cli(repo, "register", "release", "release.md")
    resumed = run_cli(repo, "run", "release")

    assert registered.returncode == 0
    migration = json.loads(registered.stdout)["sessionMigration"]
    assert migration["fromRunbookId"] == started["runbookId"]
    assert migration["toRunbookId"] == "release"
    assert migration["sourceStateRetained"] is False
    assert not Path(migration["fromStatePath"]).exists()
    assert Path(migration["toStatePath"]).is_file()

    assert resumed.returncode == 0
    state = json.loads(resumed.stdout)
    assert state["runbookId"] == "release"
    assert state["registered"] is True
    assert state["history"][0]["id"] == "prepare"
    assert state["history"][0]["evidence"] == "Preparation verified"


def test_register_rejects_conflicting_path_and_registered_sessions(repo: Path):
    runbook = repo / "release.md"
    runbook.write_text("# Release\n", encoding="utf-8")
    started = json.loads(run_cli(repo, "run", "release.md").stdout)
    source_path = Path(started["statePath"])
    target_path = repo / ".runbooks" / "release.json"
    target_state = json.loads(source_path.read_text(encoding="utf-8"))
    target_state["runbookId"] = "release"
    target_path.write_text(json.dumps(target_state), encoding="utf-8")
    source_before = source_path.read_bytes()
    target_before = target_path.read_bytes()

    result = run_cli(repo, "register", "release", "release.md")

    assert result.returncode == 2
    assert "both its path-based and registered sessions exist" in result.stderr
    assert not (repo / "runbooks.json").exists()
    assert source_path.read_bytes() == source_before
    assert target_path.read_bytes() == target_before


def test_minimal_root_registry_resolves_arbitrary_id(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    runbook_md = docs_dir / "release-procedure.md"
    runbook_md.write_text("# Release Procedure\n", encoding="utf-8")
    (repo / "runbooks.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runbooks": [
                    {
                        "id": "ship-production",
                        "path": "docs/release-procedure.md",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    res = run_cli(repo, "resolve", "ship-production")

    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["id"] == "ship-production"
    assert data["relativePath"] == "docs/release-procedure.md"
    assert data["title"] == "Release Procedure"
    assert data["registered"] is True
    assert data["effortLevel"] == "medium"
    assert data["modelTier"] == "medium"


def test_registry_normalizes_windows_separators_for_portability(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "release.md").write_text("# Release\n", encoding="utf-8")
    (repo / "runbooks.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runbooks": [
                    {
                        "id": "release",
                        "path": r"docs\release.md",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    listed = run_cli(repo, "list")
    resolved = run_cli(repo, "resolve", "release")

    assert listed.returncode == 0
    assert json.loads(listed.stdout)["runbooks"][0]["path"] == "docs/release.md"
    assert resolved.returncode == 0
    assert json.loads(resolved.stdout)["relativePath"] == "docs/release.md"


def test_same_content_registry_path_change_refreshes_session_metadata(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    original = docs_dir / "original.md"
    moved = docs_dir / "moved.md"
    original.write_text("# Same Content\n", encoding="utf-8")
    moved.write_bytes(original.read_bytes())

    assert run_cli(repo, "register", "release", "docs/original.md").returncode == 0
    assert run_cli(repo, "run", "release").returncode == 0

    registry_path = repo / "runbooks.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    entry = registry["runbooks"][0]
    entry["path"] = "docs/moved.md"
    entry["title"] = "Moved Runbook"
    entry["effortLevel"] = "high"
    entry["modelTier"] = "heavy"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    original.unlink()

    result = run_cli(repo, "run", "release")

    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert data["runbookPath"] == "docs/moved.md"
    assert data["runbookTitle"] == "Moved Runbook"
    assert data["effortLevel"] == "high"
    assert data["modelTier"] == "heavy"
    saved = json.loads((repo / ".runbooks" / "release.json").read_text(encoding="utf-8"))
    assert saved["runbookPath"] == "docs/moved.md"


def test_root_registry_disables_path_fallback(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "quick-test.md").write_text("# Quick Test\n", encoding="utf-8")
    (repo / "runbooks.json").write_text(
        json.dumps({"schemaVersion": 1, "runbooks": []}),
        encoding="utf-8",
    )

    res = run_cli(repo, "resolve", "docs/quick-test.md")

    assert res.returncode == 2
    assert "Runbook id is not registered" in res.stderr


def test_root_registry_directory_is_rejected_instead_of_enabling_path_fallback(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "quick-test.md").write_text("# Quick Test\n", encoding="utf-8")
    (repo / "runbooks.json").mkdir()

    result = run_cli(repo, "resolve", "docs/quick-test.md")

    assert result.returncode == 2
    assert "Runbook registry must be a regular file" in result.stderr
    assert "Traceback" not in result.stderr


def test_root_registry_requires_relative_paths(repo: Path):
    runbook_md = repo / "absolute.md"
    runbook_md.write_text("# Absolute\n", encoding="utf-8")
    (repo / "runbooks.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runbooks": [{"id": "absolute", "path": str(runbook_md)}],
            }
        ),
        encoding="utf-8",
    )

    res = run_cli(repo, "resolve", "absolute")

    assert res.returncode == 2
    assert "must be relative to the repository root" in res.stderr


def test_root_registry_rejects_reserved_generated_path_id(repo: Path):
    runbook = repo / "release.md"
    runbook.write_text("# Release\n", encoding="utf-8")
    (repo / "runbooks.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runbooks": [{"id": "path-0123456789ab", "path": "release.md"}],
            }
        ),
        encoding="utf-8",
    )

    result = run_cli(repo, "list")

    assert result.returncode == 2
    assert "reserved path-session namespace" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("entry_update", "expected_error"),
    [
        ({"effortLevel": ""}, "effortLevel"),
        ({"modelTier": 0}, "modelTier"),
        ({"title": None}, "title"),
        ({"description": None}, "description"),
        ({"effortLevel": None}, "effortLevel"),
        ({"modelTier": None}, "modelTier"),
        ({"modelTire": "heavy"}, "modelTire"),
        ({"id": " release "}, "surrounding whitespace"),
        ({"path": "docs/release.txt"}, "Markdown file"),
        ({"path": " docs/release.md"}, "surrounding whitespace"),
        ({"path": "../release.md"}, "parent traversal"),
    ],
)
def test_root_registry_rejects_invalid_fields(
    repo: Path,
    entry_update: dict[str, object],
    expected_error: str,
):
    entry: dict[str, object] = {
        "id": "release",
        "path": "docs/release.md",
    }
    entry.update(entry_update)
    (repo / "runbooks.json").write_text(
        json.dumps({"schemaVersion": 1, "runbooks": [entry]}),
        encoding="utf-8",
    )

    res = run_cli(repo, "list")

    assert res.returncode == 2
    assert expected_error in res.stderr


def test_root_registry_rejects_boolean_schema_version(repo: Path):
    (repo / "runbooks.json").write_text(
        json.dumps({"schemaVersion": True, "runbooks": []}),
        encoding="utf-8",
    )

    res = run_cli(repo, "list")

    assert res.returncode == 2
    assert "Unsupported runbook registry schema" in res.stderr


def test_root_registry_rejects_float_schema_version(repo: Path):
    (repo / "runbooks.json").write_text(
        json.dumps({"schemaVersion": 1.0, "runbooks": []}),
        encoding="utf-8",
    )

    res = run_cli(repo, "list")

    assert res.returncode == 2
    assert "Unsupported runbook registry schema" in res.stderr


def test_root_registry_reports_invalid_utf8_without_traceback(repo: Path):
    (repo / "runbooks.json").write_bytes(b"\xff\xfe\xfd")

    res = run_cli(repo, "list")

    assert res.returncode == 2
    assert "File is not valid UTF-8" in res.stderr
    assert "Traceback" not in res.stderr


def test_root_registry_reports_unpaired_surrogate_without_traceback(repo: Path):
    (repo / "runbooks.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runbooks": [
                    {
                        "id": "release",
                        "path": "release.md",
                        "title": "\ud800",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = run_cli(repo, "list")

    assert result.returncode == 2
    assert "contains invalid Unicode" in result.stderr
    assert "Traceback" not in result.stderr


def test_session_commands_reject_blank_required_values(repo: Path):
    runbook_md = repo / "review.md"
    runbook_md.write_text("# Review\n", encoding="utf-8")
    assert run_cli(repo, "register", "review", "review.md").returncode == 0
    assert run_cli(repo, "run", "review").returncode == 0

    blank_step = run_cli(
        repo,
        "step",
        "review",
        "   ",
        "--title",
        "Review step",
    )
    assert blank_step.returncode == 2
    assert "Step id must not be empty" in blank_step.stderr

    blank_title = run_cli(
        repo,
        "step",
        "review",
        "step-1",
        "--title",
        "   ",
    )
    assert blank_title.returncode == 2
    assert "Step title must not be empty" in blank_title.stderr

    assert (
        run_cli(
            repo,
            "step",
            "review",
            "step-1",
            "--title",
            "Review step",
        ).returncode
        == 0
    )
    for command, option, expected_error in [
        ("complete", "--evidence", "Completion evidence must not be empty"),
        ("block", "--reason", "Block reason must not be empty"),
        ("skip", "--reason", "Skip reason must not be empty"),
    ]:
        result = run_cli(repo, command, "review", option, "   ")
        assert result.returncode == 2
        assert expected_error in result.stderr

    assert run_cli(repo, "complete", "review", "--evidence", "Verified").returncode == 0
    blank_finish = run_cli(repo, "finish", "review", "--evidence", "   ")
    assert blank_finish.returncode == 2
    assert "Completion evidence must not be empty" in blank_finish.stderr

    status = json.loads(run_cli(repo, "status", "review").stdout)
    assert status["status"] == "active"
    assert status["history"][0]["evidence"] == "Verified"


def test_archive_paths_are_unique_within_same_timestamp(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    module = load_runbook_module()

    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = real_datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
            return fixed if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(module.state, "datetime", FrozenDateTime)
    state_path = module.session_path(repo, "review")

    first = module.archive_state(state_path, {"status": "active"}, "restarted")
    second = module.archive_state(state_path, {"status": "active"}, "restarted")

    assert first != second
    assert first.is_file()
    assert second.is_file()


def test_state_write_does_not_reuse_predictable_temporary_path(repo: Path):
    module = load_runbook_module()
    state_path = module.session_path(repo, "review")
    state_path.parent.mkdir()
    predictable_temporary = state_path.with_suffix(".tmp")
    predictable_temporary.write_text("sentinel\n", encoding="utf-8")

    module.write_state(state_path, {"status": "active"})

    assert predictable_temporary.read_text(encoding="utf-8") == "sentinel\n"
    assert module.read_json(state_path) == {"status": "active"}


def test_atomic_write_retries_transient_replace_denial(
    repo: Path, monkeypatch: pytest.MonkeyPatch
):
    module = load_runbook_module()
    state_path = module.session_path(repo, "review")
    original_replace = Path.replace
    attempts = 0

    def transient_replace(source: Path, target: Path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(errno.EACCES, "transient replace denial", str(target))
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", transient_replace)

    module.write_state(state_path, {"status": "active"})

    assert attempts == 2
    assert module.read_json(state_path) == {"status": "active"}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes are not available")
def test_registry_write_preserves_posix_file_mode(repo: Path):
    module = load_runbook_module()
    registry = module.repository_registry_path(repo)
    registry.write_text('{"schemaVersion": 1, "runbooks": []}\n', encoding="utf-8")
    registry.chmod(0o644)

    module.write_registry(registry, {})

    assert stat.S_IMODE(registry.stat().st_mode) == 0o644


@pytest.mark.parametrize(
    ("field", "invalid_value", "expected_error"),
    [
        ("schemaVersion", True, "Unsupported session schema"),
        ("runbookId", "different-runbook", "does not match"),
        ("status", [], "Unsupported runbook session status"),
        ("effortLevel", [], "effortLevel is invalid"),
        ("modelTier", {}, "modelTier is invalid"),
        ("history", {}, "history must be a list"),
        ("history", [None], "history item 0 must be an object"),
        ("currentStep", "broken", "currentStep must be an object or null"),
        (
            "currentStep",
            {
                "id": "step-1",
                "title": "Step 1",
                "startedAt": "2026-08-28T00:00:00+00:00",
                "status": [],
            },
            "Unsupported current step status",
        ),
    ],
)
def test_state_validation_rejects_malformed_ledgers(
    repo: Path,
    field: str,
    invalid_value: object,
    expected_error: str,
):
    module = load_runbook_module()
    markdown = repo / "review.md"
    markdown.write_text("# Review\n", encoding="utf-8")
    runbook = {
        "id": "review",
        "title": "Review",
        "path": str(markdown),
        "relativePath": "review.md",
        "description": "Review",
        "effortLevel": "medium",
        "modelTier": "medium",
        "registered": True,
    }
    state = module.new_state(runbook)
    state[field] = invalid_value
    path = module.session_path(repo, "review")
    module.write_state(path, state)

    with pytest.raises(module.RunbookError, match=expected_error):
        module.read_state(path, runbook)


def test_state_validation_requires_current_step_field(repo: Path):
    markdown = repo / "review.md"
    markdown.write_text("# Review\n", encoding="utf-8")
    assert run_cli(repo, "register", "review", "review.md").returncode == 0
    assert run_cli(repo, "run", "review").returncode == 0

    state_path = repo / ".runbooks" / "review.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("currentStep")
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = run_cli(repo, "status", "review")

    assert result.returncode == 2
    assert "Session field currentStep is required" in result.stderr
    assert "Traceback" not in result.stderr


def test_repository_registry_rejects_file_symlink(repo: Path):
    module = load_runbook_module()
    outside = repo.parent / f"{repo.name}-outside-registry.json"
    try:
        (repo / "runbooks.json").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"File symlinks are not available: {exc}")

    with pytest.raises(module.RunbookError, match="must not be a symbolic link or junction"):
        module.repository_registry_path(repo)
    assert not outside.exists()


def test_session_path_rejects_state_directory_outside_repo(repo: Path):
    module = load_runbook_module()
    outside = repo.parent / f"{repo.name}-outside-state"
    outside.mkdir()
    state_link = repo / ".runbooks"
    create_directory_link_or_skip(state_link, outside)

    with pytest.raises(module.RunbookError, match="must not be a symbolic link or junction"):
        module.session_path(repo, "review")


def test_session_path_rejects_state_directory_redirect_inside_repo(repo: Path):
    module = load_runbook_module()
    target = repo / "config"
    target.mkdir()
    create_directory_link_or_skip(repo / ".runbooks", target)

    with pytest.raises(module.RunbookError, match="must not be a symbolic link or junction"):
        module.session_path(repo, "review")
    assert not (target / "review.json").exists()
