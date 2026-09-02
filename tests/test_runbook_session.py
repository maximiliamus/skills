from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import UTC, timedelta
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

    def assert_locked(registry: dict[str, dict[str, str]], repo_root: Path):
        assert lock_active
        assert registry == {}
        assert repo_root == repo.resolve()

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
    assert session_data["acceptancePolicy"] == "flexible"
    assert session_data["stepOrder"] == "sequential"
    assert session_data["acceptanceThreshold"] is None
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
    res = run_cli(
        repo,
        "complete",
        "sample-deploy",
        "--evidence",
        "Verified cluster ready",
        "--result",
        "pass",
        "--score",
        "1/1",
    )
    assert res.returncode == 0
    comp_data = json.loads(res.stdout)
    assert len(comp_data["history"]) == 1
    assert comp_data["history"][0]["status"] == "completed"

    # Finish runbook
    res = run_cli(
        repo,
        "finish",
        "sample-deploy",
        "--evidence",
        "All phases verified",
        "--expected-step",
        "phase-1.1",
    )
    assert res.returncode == 0
    fin_data = json.loads(res.stdout)
    assert fin_data["status"] == "completed"
    assert fin_data["result"] == "PASSED"
    assert fin_data["score"]["display"] == "1/1 (100%)"

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
    assert run_cli(repo, "step", "release", "step-1", "--title", "Step 1").returncode == 0
    assert (
        run_cli(
            repo,
            "complete",
            "release",
            "--evidence",
            "Original revision done",
            "--result",
            "pass",
            "--score",
            "1/1",
        ).returncode
        == 0
    )
    assert (
        run_cli(
            repo,
            "finish",
            "release",
            "--evidence",
            "Original revision done",
            "--expected-step",
            "step-1",
        ).returncode
        == 0
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
    res_skip = run_cli(
        repo,
        "skip",
        "pipeline",
        "--reason",
        "Manual bypass authorized",
        "--score",
        "0/1",
    )
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
    run_cli(
        repo,
        "complete",
        "evolving",
        "--evidence",
        "Done",
        "--result",
        "pass",
        "--score",
        "1/1",
    )

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
    run_cli(
        repo,
        "complete",
        "repeatable",
        "--evidence",
        "Done",
        "--result",
        "pass",
        "--score",
        "1/1",
    )
    run_cli(
        repo,
        "finish",
        "repeatable",
        "--evidence",
        "Finished",
        "--expected-step",
        "step-1",
    )

    # Restart
    res_restart = run_cli(repo, "run", "repeatable", "--restart")
    assert res_restart.returncode == 0
    restart_data = json.loads(res_restart.stdout)
    assert restart_data["status"] == "active"
    assert restart_data["currentStep"] is None
    assert restart_data["history"] == []


def test_prune_keep_last_previews_then_deletes_only_selected_runbook(repo: Path):
    docs_dir = repo / "docs"
    docs_dir.mkdir(parents=True)
    for runbook_id in ("alpha", "beta"):
        (docs_dir / f"{runbook_id}.md").write_text(
            f"# {runbook_id.title()}\n",
            encoding="utf-8",
        )
        assert (
            run_cli(
                repo,
                "register",
                runbook_id,
                f"docs/{runbook_id}.md",
            ).returncode
            == 0
        )
        assert run_cli(repo, "run", runbook_id).returncode == 0

    for _ in range(3):
        assert run_cli(repo, "run", "alpha", "--restart").returncode == 0
    assert run_cli(repo, "run", "beta", "--restart").returncode == 0

    archive_dir = repo / ".runbooks" / "archive"
    alpha_archives = list(archive_dir.glob("alpha.*.json"))
    beta_archives = list(archive_dir.glob("beta.*.json"))
    current_ledger = (repo / ".runbooks" / "alpha.json").read_bytes()
    assert len(alpha_archives) == 3
    assert len(beta_archives) == 1
    newest_alpha_archive = max(alpha_archives, key=lambda archive: archive.name)

    preview = run_cli(repo, "prune", "alpha", "--keep-last", "1", "--dry-run")

    assert preview.returncode == 0, preview.stderr
    preview_data = json.loads(preview.stdout)
    assert preview_data["status"] == "preview"
    assert preview_data["policy"] == {"keepLast": 1}
    assert preview_data["selectedCount"] == 2
    assert preview_data["deletedCount"] == 0
    assert preview_data["retainedCount"] == 3
    assert len(list(archive_dir.glob("alpha.*.json"))) == 3

    pruned = run_cli(repo, "prune", "alpha", "--keep-last", "1")

    assert pruned.returncode == 0, pruned.stderr
    pruned_data = json.loads(pruned.stdout)
    assert pruned_data["status"] == "pruned"
    assert pruned_data["selectedCount"] == 2
    assert pruned_data["deletedCount"] == 2
    assert pruned_data["retainedCount"] == 1
    assert list(archive_dir.glob("alpha.*.json")) == [newest_alpha_archive]
    assert list(archive_dir.glob("beta.*.json")) == beta_archives
    assert (repo / ".runbooks" / "alpha.json").read_bytes() == current_ledger


def test_prune_older_than_days_uses_archive_timestamp(repo: Path):
    runbook = repo / "aging.md"
    runbook.write_text("# Aging\n", encoding="utf-8")
    assert run_cli(repo, "register", "aging", "aging.md").returncode == 0
    archive_dir = repo / ".runbooks" / "archive"
    archive_dir.mkdir(parents=True)
    old_timestamp = (real_datetime.now(UTC) - timedelta(days=30)).strftime(
        "%Y%m%dT%H%M%S.%fZ"
    )
    recent_timestamp = real_datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    old_archive = archive_dir / f"aging.{old_timestamp}.{'a' * 32}.json"
    recent_archive = archive_dir / f"aging.{recent_timestamp}.{'b' * 32}.json"
    unrelated = archive_dir / f"other.{old_timestamp}.{'c' * 32}.json"
    for archive in (old_archive, recent_archive, unrelated):
        archive.write_text("{}\n", encoding="utf-8")

    result = run_cli(repo, "prune", "aging", "--older-than-days", "7")

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["policy"] == {"olderThanDays": 7}
    assert data["archiveCountBefore"] == 2
    assert data["selectedCount"] == 1
    assert data["deletedCount"] == 1
    assert not old_archive.exists()
    assert recent_archive.is_file()
    assert unrelated.is_file()


def test_prune_includes_archives_from_the_current_path_identity(repo: Path):
    runbook = repo / "migrating.md"
    runbook.write_text("# Migrating\n", encoding="utf-8")
    started = run_cli(repo, "run", "migrating.md")
    assert started.returncode == 0, started.stderr
    path_runbook_id = json.loads(started.stdout)["runbookId"]
    assert run_cli(repo, "run", "migrating.md", "--restart").returncode == 0
    path_archive = next(
        (repo / ".runbooks" / "archive").glob(f"{path_runbook_id}.*.json")
    )
    assert run_cli(repo, "register", "migrating", "migrating.md").returncode == 0

    result = run_cli(repo, "prune", "migrating", "--keep-last", "0")

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["selectedCount"] == 1
    assert data["deletedCount"] == 1
    assert not path_archive.exists()


@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        (("--keep-last", "-1"), "--keep-last must be zero or greater"),
        (("--older-than-days", "0"), "--older-than-days must be one or greater"),
    ],
)
def test_prune_rejects_invalid_policy_values(
    repo: Path,
    arguments: tuple[str, str],
    expected_error: str,
):
    runbook = repo / "cleanup.md"
    runbook.write_text("# Cleanup\n", encoding="utf-8")
    assert run_cli(repo, "register", "cleanup", "cleanup.md").returncode == 0

    result = run_cli(repo, "prune", "cleanup", *arguments)

    assert result.returncode == 2
    assert expected_error in result.stderr


def test_prune_rejects_matching_non_file_archive_entry(repo: Path):
    runbook = repo / "cleanup.md"
    runbook.write_text("# Cleanup\n", encoding="utf-8")
    assert run_cli(repo, "register", "cleanup", "cleanup.md").returncode == 0
    archive_entry = (
        repo
        / ".runbooks"
        / "archive"
        / f"cleanup.20260101T000000.000000Z.{'a' * 32}.json"
    )
    archive_entry.mkdir(parents=True)

    result = run_cli(repo, "prune", "cleanup", "--keep-last", "0")

    assert result.returncode == 2
    assert "Session archive is not a regular file" in result.stderr
    assert archive_entry.is_dir()


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
    run_cli(
        repo,
        "complete",
        "duplicate-check",
        "--evidence",
        "Done",
        "--result",
        "pass",
        "--score",
        "1/1",
    )

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


def test_arbitrary_order_step_can_retry_without_losing_previous_attempt(repo: Path):
    runbook_md = repo / "arbitrary-order.md"
    runbook_md.write_text(
        "---\nstepOrder: arbitrary\n---\n\n# Arbitrary Order\n",
        encoding="utf-8",
    )
    assert (
        run_cli(repo, "register", "arbitrary-order", "arbitrary-order.md").returncode == 0
    )
    assert run_cli(repo, "run", "arbitrary-order").returncode == 0

    assert (
        run_cli(
            repo,
            "step",
            "arbitrary-order",
            "gate-1",
            "--title",
            "Gate 1",
        ).returncode
        == 0
    )
    first = run_cli(
        repo,
        "complete",
        "arbitrary-order",
        "--evidence",
        "First assessment was deficient",
        "--result",
        "fail",
        "--score",
        "0/1",
    )
    assert first.returncode == 0
    assert json.loads(first.stdout)["history"][0]["attempt"] == 1

    retry = run_cli(
        repo,
        "step",
        "arbitrary-order",
        "gate-1",
        "--title",
        "Gate 1",
        "--retry",
    )
    assert retry.returncode == 0
    assert json.loads(retry.stdout)["currentStep"]["attempt"] == 2

    second = run_cli(
        repo,
        "complete",
        "arbitrary-order",
        "--evidence",
        "Second assessment passed",
        "--result",
        "pass",
        "--score",
        "1/1",
    )
    assert second.returncode == 0
    history = json.loads(second.stdout)["history"]
    assert [item["attempt"] for item in history] == [1, 2]
    assert history[0]["evidence"] == "First assessment was deficient"
    assert history[1]["evidence"] == "Second assessment passed"


def test_retry_requires_a_previous_attempt(repo: Path):
    runbook_md = repo / "retry.md"
    runbook_md.write_text("# Retry\n", encoding="utf-8")
    assert run_cli(repo, "register", "retry", "retry.md").returncode == 0
    assert run_cli(repo, "run", "retry").returncode == 0

    result = run_cli(
        repo,
        "step",
        "retry",
        "gate-1",
        "--title",
        "Gate 1",
        "--retry",
    )

    assert result.returncode == 2
    assert "no completed attempt" in result.stderr


def test_retry_flag_does_not_turn_an_active_first_attempt_into_a_retry(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text("# Review\n", encoding="utf-8")
    assert run_cli(repo, "run", "review.md").returncode == 0
    assert run_cli(repo, "step", "review.md", "gate-1", "--title", "Gate 1").returncode == 0

    retry = run_cli(
        repo,
        "step",
        "review.md",
        "gate-1",
        "--title",
        "Gate 1",
        "--retry",
    )

    assert retry.returncode == 2
    assert "no completed attempt" in retry.stderr
    status = json.loads(run_cli(repo, "status", "review.md").stdout)
    assert status["currentStep"]["attempt"] == 1


def test_flexible_threshold_completes_as_accepted(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\nacceptancePolicy: flexible\nacceptanceThreshold: 60%\n---\n\n# Review\n",
        encoding="utf-8",
    )
    assert run_cli(repo, "run", "review.md").returncode == 0
    for step_id, result, score in [("gate-1", "fail", "1/2"), ("gate-2", "fail", "2/3")]:
        assert (
            run_cli(repo, "step", "review.md", step_id, "--title", step_id.title()).returncode
            == 0
        )
        assert (
            run_cli(
                repo,
                "complete",
                "review.md",
                "--evidence",
                f"Assessed {step_id}",
                "--result",
                result,
                "--score",
                score,
            ).returncode
            == 0
        )

    finished = run_cli(
        repo,
        "finish",
        "review.md",
        "--evidence",
        "Assessment complete",
        "--expected-step",
        "gate-1",
        "--expected-step",
        "gate-2",
    )

    assert finished.returncode == 0
    data = json.loads(finished.stdout)
    assert data["status"] == "completed"
    assert data["result"] == "ACCEPTED"
    assert data["score"]["display"] == "3/5 (60%)"


@pytest.mark.parametrize(
    ("result", "score", "expected_error"),
    [
        ("pass", "1/2", "passing step must earn every available point"),
        ("fail", "2/2", "failed step cannot earn every available point"),
    ],
)
def test_step_result_and_score_must_be_semantically_consistent(
    repo: Path,
    result: str,
    score: str,
    expected_error: str,
):
    runbook = repo / "review.md"
    runbook.write_text("# Review\n", encoding="utf-8")
    assert run_cli(repo, "run", "review.md").returncode == 0
    assert run_cli(repo, "step", "review.md", "gate-1", "--title", "Gate 1").returncode == 0

    completed = run_cli(
        repo,
        "complete",
        "review.md",
        "--evidence",
        "Inconsistent assessment",
        "--result",
        result,
        "--score",
        score,
    )

    assert completed.returncode == 2
    assert expected_error in completed.stderr


def test_flexible_below_threshold_stays_partial_until_operator_decides(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\nacceptancePolicy: flexible\nacceptanceThreshold: 80%\n---\n\n# Review\n",
        encoding="utf-8",
    )
    assert run_cli(repo, "run", "review.md").returncode == 0
    assert run_cli(repo, "step", "review.md", "gate-1", "--title", "Gate 1").returncode == 0
    assert (
        run_cli(
            repo,
            "complete",
            "review.md",
            "--evidence",
            "One criterion failed",
            "--result",
            "fail",
            "--score",
            "1/2",
        ).returncode
        == 0
    )

    partial = run_cli(
        repo,
        "finish",
        "review.md",
        "--evidence",
        "Assessment complete",
        "--expected-step",
        "gate-1",
    )

    assert partial.returncode == 3
    partial_data = json.loads(partial.stdout)
    assert partial_data["status"] == "active"
    assert partial_data["result"] == "PARTIAL"
    assert partial_data["score"]["display"] == "1/2 (50%)"
    assert partial_data["operatorDecisionRequired"] is True

    accepted = run_cli(
        repo,
        "finish",
        "review.md",
        "--evidence",
        "Operator accepted the limitations",
        "--expected-step",
        "gate-1",
        "--decision",
        "accept",
    )
    assert accepted.returncode == 0
    accepted_data = json.loads(accepted.stdout)
    assert accepted_data["status"] == "completed"
    assert accepted_data["result"] == "ACCEPTED"


def test_half_percentage_is_rounded_up_before_threshold_comparison(repo: Path):
    runbook = repo / "rounding.md"
    runbook.write_text(
        "---\nacceptancePolicy: flexible\nacceptanceThreshold: 13%\n---\n\n# Rounding\n",
        encoding="utf-8",
    )
    assert run_cli(repo, "run", "rounding.md").returncode == 0
    assert run_cli(repo, "step", "rounding.md", "gate-1", "--title", "Gate 1").returncode == 0
    completed = run_cli(
        repo,
        "complete",
        "rounding.md",
        "--evidence",
        "One of eight points",
        "--result",
        "fail",
        "--score",
        "1/8",
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["history"][0]["score"]["percent"] == 13

    finished = run_cli(
        repo,
        "finish",
        "rounding.md",
        "--evidence",
        "Rounded half up",
        "--expected-step",
        "gate-1",
    )
    assert finished.returncode == 0
    assert json.loads(finished.stdout)["result"] == "ACCEPTED"


def test_finish_requires_every_expected_and_assessed_step(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text("# Review\n", encoding="utf-8")
    assert run_cli(repo, "run", "review.md").returncode == 0
    assert run_cli(repo, "step", "review.md", "gate-1", "--title", "Gate 1").returncode == 0
    assert (
        run_cli(
            repo,
            "complete",
            "review.md",
            "--evidence",
            "Assessed",
            "--result",
            "pass",
            "--score",
            "1/1",
        ).returncode
        == 0
    )

    missing = run_cli(
        repo,
        "finish",
        "review.md",
        "--evidence",
        "Incomplete",
        "--expected-step",
        "gate-1",
        "--expected-step",
        "gate-2",
    )
    omitted = run_cli(
        repo,
        "finish",
        "review.md",
        "--evidence",
        "Incomplete",
        "--expected-step",
        "other-gate",
    )

    assert missing.returncode == 2
    assert "missing: gate-2" in missing.stderr
    assert omitted.returncode == 2
    assert "missing: other-gate" in omitted.stderr


def test_always_policy_accepts_only_after_every_expected_step_is_evaluated(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\nacceptancePolicy: always\nstepOrder: arbitrary\n---\n\n# Review\n",
        encoding="utf-8",
    )
    assert run_cli(repo, "run", "review.md").returncode == 0
    assert run_cli(repo, "step", "review.md", "gate-1", "--title", "Gate 1").returncode == 0
    assert (
        run_cli(
            repo,
            "skip",
            "review.md",
            "--reason",
            "Unavailable evidence",
            "--score",
            "0/2",
        ).returncode
        == 0
    )

    incomplete = run_cli(
        repo,
        "finish",
        "review.md",
        "--evidence",
        "Not all gates evaluated",
        "--expected-step",
        "gate-1",
        "--expected-step",
        "gate-2",
    )
    assert incomplete.returncode == 2
    assert "missing: gate-2" in incomplete.stderr

    assert run_cli(repo, "step", "review.md", "gate-2", "--title", "Gate 2").returncode == 0
    assert (
        run_cli(
            repo,
            "complete",
            "review.md",
            "--evidence",
            "Evaluated",
            "--result",
            "fail",
            "--score",
            "0/1",
        ).returncode
        == 0
    )
    accepted = run_cli(
        repo,
        "finish",
        "review.md",
        "--evidence",
        "All gates evaluated",
        "--expected-step",
        "gate-1",
        "--expected-step",
        "gate-2",
    )
    assert accepted.returncode == 0
    data = json.loads(accepted.stdout)
    assert data["status"] == "completed"
    assert data["result"] == "ACCEPTED"
    assert data["score"]["display"] == "0/3 (0%)"


def test_strict_arbitrary_can_continue_but_finishes_rejected(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\nacceptancePolicy: strict\nstepOrder: arbitrary\n---\n\n# Review\n",
        encoding="utf-8",
    )
    assert run_cli(repo, "run", "review.md").returncode == 0
    for step_id, result in [("gate-1", "fail"), ("gate-2", "pass")]:
        assert (
            run_cli(repo, "step", "review.md", step_id, "--title", step_id.title()).returncode
            == 0
        )
        assert (
            run_cli(
                repo,
                "complete",
                "review.md",
                "--evidence",
                f"Assessed {step_id}",
                "--result",
                result,
            ).returncode
            == 0
        )

    finished = run_cli(
        repo,
        "finish",
        "review.md",
        "--evidence",
        "All gates assessed",
        "--expected-step",
        "gate-1",
        "--expected-step",
        "gate-2",
    )
    assert finished.returncode == 0
    data = json.loads(finished.stdout)
    assert data["status"] == "completed"
    assert data["result"] == "REJECTED"


def test_strict_sequential_requires_retry_before_advancing(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\nacceptancePolicy: strict\nstepOrder: sequential\n---\n\n# Review\n",
        encoding="utf-8",
    )
    assert run_cli(repo, "run", "review.md").returncode == 0
    assert run_cli(repo, "step", "review.md", "gate-1", "--title", "Gate 1").returncode == 0
    assert (
        run_cli(
            repo,
            "complete",
            "review.md",
            "--evidence",
            "Failed",
            "--result",
            "fail",
        ).returncode
        == 0
    )
    advance = run_cli(repo, "step", "review.md", "gate-2", "--title", "Gate 2")
    assert advance.returncode == 2
    assert "retry first: gate-1" in advance.stderr

    assert (
        run_cli(
            repo,
            "step",
            "review.md",
            "gate-1",
            "--title",
            "Gate 1",
            "--retry",
        ).returncode
        == 0
    )
    assert (
        run_cli(
            repo,
            "complete",
            "review.md",
            "--evidence",
            "Passed on retry",
            "--result",
            "pass",
        ).returncode
        == 0
    )
    assert run_cli(repo, "step", "review.md", "gate-2", "--title", "Gate 2").returncode == 0


def test_legacy_active_session_remains_compatible(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text("# Review\n", encoding="utf-8")
    started = json.loads(run_cli(repo, "run", "review.md").stdout)
    assert run_cli(repo, "step", "review.md", "gate-1", "--title", "Gate 1").returncode == 0
    state_path = Path(started["statePath"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.pop("assessmentVersion")
    state_path.write_text(json.dumps(state), encoding="utf-8")

    completed = run_cli(repo, "complete", "review.md", "--evidence", "Legacy evidence")
    finished = run_cli(repo, "finish", "review.md", "--evidence", "Legacy finish")

    assert completed.returncode == 0
    assert finished.returncode == 0
    data = json.loads(finished.stdout)
    assert data["status"] == "completed"
    assert "result" not in data


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
        run_cli(
            repo,
            "complete",
            "release.md",
            "--evidence",
            "Preparation verified",
            "--result",
            "pass",
            "--score",
            "1/1",
        ).returncode
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
    assert data["acceptancePolicy"] == "flexible"
    assert data["stepOrder"] == "sequential"
    assert data["acceptanceThreshold"] is None


def test_runbook_frontmatter_controls_identity_description_and_execution(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\n"
        "id: release-review\n"
        "description: Review the release evidence.\n"
        "acceptancePolicy: flexible\n"
        "stepOrder: arbitrary\n"
        "acceptanceThreshold: 80%\n"
        "---\n\n"
        "# Review\n",
        encoding="utf-8",
    )

    resolved = run_cli(repo, "resolve", "review.md")

    assert resolved.returncode == 0
    data = json.loads(resolved.stdout)
    assert data["id"] == "release-review"
    assert data["description"] == "Review the release evidence."
    assert data["acceptancePolicy"] == "flexible"
    assert data["stepOrder"] == "arbitrary"
    assert data["acceptanceThreshold"] == "80%"


@pytest.mark.parametrize(
    ("property_lines", "expected_error"),
    [
        ("acceptancePolicy: optional", "acceptancePolicy"),
        ("stepOrder: random", "stepOrder"),
        ("acceptanceThreshold: 101%", "acceptanceThreshold"),
        (
            "acceptancePolicy: strict\nacceptanceThreshold: 100%",
            "requires acceptancePolicy: flexible",
        ),
        (
            "acceptancePolicy: always\nacceptanceThreshold: 100%",
            "requires acceptancePolicy: flexible",
        ),
        ("acceptancePolicy: strict\nacceptancePolicy: flexible", "Duplicate"),
        ("id: Invalid ID", "Invalid runbook id"),
        ("description:", "description must not be empty"),
    ],
)
def test_invalid_runbook_frontmatter_is_rejected(
    repo: Path,
    property_lines: str,
    expected_error: str,
):
    runbook = repo / "review.md"
    runbook.write_text(
        f"---\n{property_lines}\n---\n\n# Review\n",
        encoding="utf-8",
    )

    result = run_cli(repo, "resolve", "review.md")

    assert result.returncode == 2
    assert expected_error in result.stderr


def test_registration_rejects_frontmatter_id_mismatch(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\nid: documented-review\n---\n\n# Review\n",
        encoding="utf-8",
    )

    result = run_cli(repo, "register", "other-review", "review.md")

    assert result.returncode == 2
    assert "does not match registration id" in result.stderr


def test_registration_uses_frontmatter_description(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\n"
        "id: documented-review\n"
        "description: Review directly from the document.\n"
        "---\n\n"
        "# Review\n",
        encoding="utf-8",
    )

    result = run_cli(repo, "register", "documented-review", "review.md")

    assert result.returncode == 0
    assert (
        json.loads(result.stdout)["registered"]["description"]
        == "Review directly from the document."
    )
    registry = json.loads((repo / "runbooks.json").read_text(encoding="utf-8"))
    assert "description" not in registry["runbooks"][0]

    runbook.write_text(
        "---\n"
        "id: documented-review\n"
        "description: Updated directly in the document.\n"
        "---\n\n"
        "# Review\n",
        encoding="utf-8",
    )

    assert (
        json.loads(run_cli(repo, "list").stdout)["runbooks"][0]["description"]
        == "Updated directly in the document."
    )
    assert (
        json.loads(run_cli(repo, "resolve", "documented-review").stdout)["description"]
        == "Updated directly in the document."
    )


def test_explicit_registry_description_overrides_document_description(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\ndescription: Document description.\n---\n\n# Review\n",
        encoding="utf-8",
    )

    registered = run_cli(
        repo,
        "register",
        "review",
        "review.md",
        "--description",
        "Registry override.",
    )
    runbook.write_text(
        "---\ndescription: Changed document description.\n---\n\n# Review\n",
        encoding="utf-8",
    )

    assert registered.returncode == 0
    assert (
        json.loads(run_cli(repo, "list").stdout)["runbooks"][0]["description"]
        == "Registry override."
    )
    assert (
        json.loads(run_cli(repo, "resolve", "review").stdout)["description"]
        == "Registry override."
    )


def test_existing_registry_description_is_treated_as_an_override(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\ndescription: Document description.\n---\n\n# Review\n",
        encoding="utf-8",
    )
    (repo / "runbooks.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runbooks": [
                    {
                        "id": "review",
                        "path": "review.md",
                        "description": "Existing registry description.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    resolved = run_cli(repo, "resolve", "review")

    assert resolved.returncode == 0
    assert json.loads(resolved.stdout)["description"] == "Existing registry description."


def test_minimal_frontmatter_supports_quoted_colons_and_rejects_non_flat_data(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\ndescription: 'Review: release evidence'\n---\n\n# Review\n",
        encoding="utf-8",
    )

    resolved = run_cli(repo, "resolve", "review.md")

    assert resolved.returncode == 0
    assert json.loads(resolved.stdout)["description"] == "Review: release evidence"

    runbook.write_text("---\n- not\n- a mapping\n---\n\n# Review\n", encoding="utf-8")
    rejected = run_cli(repo, "resolve", "review.md")
    assert rejected.returncode == 2
    assert "flat key: value properties" in rejected.stderr


def test_minimal_frontmatter_supports_literal_multiline_description(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\n"
        "id: review\n"
        "description: |\n"
        "  Review the release evidence.\n"
        "  Preserve every stated limitation.\n"
        "acceptancePolicy: flexible\n"
        "---\n\n"
        "# Review\n",
        encoding="utf-8",
    )

    resolved = run_cli(repo, "resolve", "review.md")

    assert resolved.returncode == 0
    assert (
        json.loads(resolved.stdout)["description"]
        == "Review the release evidence.\nPreserve every stated limitation."
    )


def test_minimal_frontmatter_rejects_multiline_values_for_other_properties(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text(
        "---\nacceptancePolicy: |\n  flexible\n---\n\n# Review\n",
        encoding="utf-8",
    )

    rejected = run_cli(repo, "resolve", "review.md")

    assert rejected.returncode == 2
    assert "Only runbook frontmatter description" in rejected.stderr


def test_document_id_adopts_existing_path_session_without_losing_progress(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text("# Review\n", encoding="utf-8")
    started = json.loads(run_cli(repo, "run", "review.md").stdout)
    legacy_path = Path(started["statePath"])
    assert run_cli(repo, "step", "review.md", "gate-1", "--title", "Gate 1").returncode == 0
    assert (
        run_cli(
            repo,
            "complete",
            "review.md",
            "--evidence",
            "Assessed",
            "--result",
            "pass",
            "--score",
            "1/1",
        ).returncode
        == 0
    )
    runbook.write_text("---\nid: review\n---\n\n# Review\n", encoding="utf-8")

    decision = run_cli(repo, "run", "review.md")

    assert decision.returncode == 3
    assert json.loads(decision.stdout)["reason"] == "unfinished_runbook_changed"
    adopted_path = repo / ".runbooks" / "review.json"
    assert adopted_path.is_file()
    assert not legacy_path.exists()
    adopted = json.loads(adopted_path.read_text(encoding="utf-8"))
    assert adopted["runbookId"] == "review"
    assert adopted["history"][0]["id"] == "gate-1"

    continued = run_cli(repo, "run", "review.md", "--continue")
    assert continued.returncode == 0
    assert json.loads(continued.stdout)["history"][0]["evidence"] == "Assessed"


def test_document_id_migration_recovers_an_interrupted_atomic_rename(repo: Path):
    runbook = repo / "review.md"
    runbook.write_text("# Review\n", encoding="utf-8")
    started = json.loads(run_cli(repo, "run", "review.md").stdout)
    legacy_path = Path(started["statePath"])
    target_path = repo / ".runbooks" / "review.json"
    runbook.write_text("---\nid: review\n---\n\n# Review\n", encoding="utf-8")
    legacy_path.replace(target_path)

    recovered = run_cli(repo, "run", "review.md")

    assert recovered.returncode == 3
    assert json.loads(recovered.stdout)["reason"] == "unfinished_runbook_changed"
    recovered_state = json.loads(target_path.read_text(encoding="utf-8"))
    assert recovered_state["runbookId"] == "review"
    assert not legacy_path.exists()


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
    (repo / "release.md").write_text("# Release\n", encoding="utf-8")
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

    assert (
        run_cli(
            repo,
            "complete",
            "review",
            "--evidence",
            "Verified",
            "--result",
            "pass",
            "--score",
            "1/1",
        ).returncode
        == 0
    )
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
