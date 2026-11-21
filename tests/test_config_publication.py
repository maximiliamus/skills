"""Configuration retries recover only the helper's own interrupted publication."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, local

import pytest
from test_paired_review_config import SCRIPT as REVIEW_SCRIPT
from test_paired_review_config import SKILL as REVIEW_SKILL
from test_paired_review_config import review
from test_runbook_session import RUNBOOK_SCRIPT

SKILLS = [("review", REVIEW_SCRIPT, "conduct-paired-review.json", REVIEW_SKILL / "config.json"),
          ("runbook", RUNBOOK_SCRIPT, "execute-guided-runbook.json",
           RUNBOOK_SCRIPT.parents[1] / "config.json")]

CRASH_PROCESS = r'''
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext

kind, script, directory, platform, phase = sys.argv[1:]
root = Path(directory)
if kind == "review":
    spec = importlib.util.spec_from_file_location("crash_review", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lock, initialize = lambda root: nullcontext(), module.initialize_project_config
else:
    sys.path.insert(0, str(Path(script).parent))
    from execute_guided_runbook_lib import configuration as module
    lock, initialize = module.config_locks, module.initialize_config

rename, link = os.rename, os.link

def boundary(action):
    if phase == "before":
        os._exit(17)
    action()
    os._exit(17)

os.rename = lambda *args: boundary(lambda: rename(*args))
os.link = lambda *args: boundary(lambda: link(*args))
publish = module.publish_new

def interrupted(source, target):
    if platform == "posix":
        # Only publication sees POSIX; locking and staging still use the host OS.
        module.os = SimpleNamespace(**vars(os))
        module.os.name = "posix"
    return publish(source, target)

module.publish_new = interrupted
with lock(root):
    initialize(root)
raise AssertionError("Publication did not reach the injected process-exit boundary")
'''


@pytest.mark.parametrize("kind,script,filename,template", SKILLS)
@pytest.mark.parametrize("platform", ["native", "posix"])
@pytest.mark.parametrize("phase,first_command", [
    ("before", "init"), ("after", "init"), ("after", "update"),
])
def test_config_commands_work_after_publication_process_exit(
    tmp_path, kind, script, filename, template, platform, phase, first_command,
):
    crashed = subprocess.run(
        [sys.executable, "-B", "-c", CRASH_PROCESS, kind, str(script), str(tmp_path), platform, phase],
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=20,
    )
    assert crashed.returncode == 17, crashed.stderr
    path = tmp_path / filename
    assert path.exists() == (phase == "after")
    saved = path.read_bytes() if path.exists() else None
    previous = path.stat() if path.exists() else None
    for operation in (first_command, "init", "update"):
        result = subprocess.run(
            ([sys.executable, "-B", str(script), "--repo-root", str(tmp_path), operation]
             if kind == "runbook"
             else [sys.executable, "-B", str(script), operation, str(tmp_path)]),
            capture_output=True, text=True, encoding="utf-8", check=False, timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["changed" if kind == "runbook" else "created"] == (saved is None)
        assert path.stat().st_nlink == 1
        saved = saved if saved is not None else template.read_bytes()
        assert path.read_bytes() == saved
    if previous is not None:
        assert path.stat().st_mtime_ns == previous.st_mtime_ns
        assert path.stat().st_mode == previous.st_mode
        assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("kind,script,filename,template", SKILLS)
@pytest.mark.parametrize("operation", ["init", "update"])
@pytest.mark.parametrize("case", ["unrecognized-name", "extra-link", "different-file", "invalid"])
def test_config_recovery_never_removes_unrelated_links_or_repairs_invalid_settings(
    tmp_path, kind, script, filename, template, operation, case,
):
    path = tmp_path / filename
    path.write_bytes(b"{broken" if case == "invalid" else template.read_bytes())
    staged = tmp_path / f".{filename}.init-{'a' * 32}.tmp"
    if case == "unrecognized-name":
        staged = tmp_path / f".{filename}.init-not-a-token.tmp"
    if case == "different-file":
        staged.write_bytes(b"unrelated")
        os.link(path, tmp_path / "outside.json")
    else:
        os.link(path, staged)
    if case == "extra-link":
        os.link(path, tmp_path / "outside.json")
    before = {entry: entry.read_bytes() for entry in tmp_path.iterdir() if entry.is_file()}
    result = subprocess.run(
        ([sys.executable, "-B", str(script), "--repo-root", str(tmp_path), operation]
             if kind == "runbook"
             else [sys.executable, "-B", str(script), operation, str(tmp_path)]),
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=20,
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    for entry, data in before.items():
        assert entry.read_bytes() == data
    assert path.stat().st_nlink == (3 if case == "extra-link" else 2)


@pytest.mark.parametrize("operation", ["init", "update"])
@pytest.mark.parametrize("pause_at", ["validate", "samefile", "unlink"])
def test_concurrent_review_config_recovery_is_idempotent(tmp_path, monkeypatch, operation, pause_at):
    path = tmp_path / review.PROJECT_CONFIG_NAME
    config = json.loads((REVIEW_SKILL / "config.json").read_bytes())
    config["maxStagnantTurns"] = 8
    path.write_text(json.dumps(config, indent=4) + "\n", encoding="utf-8")
    # This is the actual POSIX publication boundary, also reproducible on NTFS.
    staged = tmp_path / f".{path.name}.init-{'b' * 32}.tmp"
    os.link(path, staged)
    before, previous = path.read_bytes(), path.stat()
    paused, resume, state = Event(), Event(), local()

    def wait_for_other_recovery():
        if getattr(state, "pause", False):
            state.pause = False
            paused.set()
            assert resume.wait(10), "Other recovery did not finish"

    if pause_at == "validate":
        validate = review.validate_config

        def interrupted_validate(config):
            result = validate(config)
            wait_for_other_recovery()
            return result

        monkeypatch.setattr(review, "validate_config", interrupted_validate)
    else:
        original = getattr(Path, pause_at)

        def interrupted_path_call(candidate, *args, **kwargs):
            if candidate == staged:
                wait_for_other_recovery()
            return original(candidate, *args, **kwargs)

        monkeypatch.setattr(Path, pause_at, interrupted_path_call)

    command = (review.initialize_project_config if operation == "init"
               else review.update_project_config)

    def delayed_recovery():
        state.pause = True
        return command(tmp_path)

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(delayed_recovery)
        try:
            assert paused.wait(10), "Recovery did not reach the selected boundary"
            first = command(tmp_path)
        finally:
            resume.set()
        second = pending.result(timeout=10)

    assert first["config"] == second["config"] == config
    assert first["created"] is second["created"] is False
    assert path.read_bytes() == before and path.stat().st_nlink == 1
    assert path.stat().st_mtime_ns == previous.st_mtime_ns
    assert path.stat().st_mode == previous.st_mode
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("change", ["invalid-config", "unrelated-link"])
def test_completed_recovery_still_checks_final_config(tmp_path, monkeypatch, change):
    path = tmp_path / review.PROJECT_CONFIG_NAME
    path.write_bytes((REVIEW_SKILL / "config.json").read_bytes())
    staged = tmp_path / f".{path.name}.init-{'c' * 32}.tmp"
    os.link(path, staged)
    validate = review.validate_config

    def competing_recovery(config):
        validate(config)
        if staged.exists():
            staged.unlink()
            if change == "invalid-config":
                path.write_text("{}", encoding="utf-8")
            else:
                os.link(path, tmp_path / "unrelated.json")

    monkeypatch.setattr(review, "validate_config", competing_recovery)
    with pytest.raises(review.ReviewError, match="schema|Hard-linked"):
        review.update_project_config(tmp_path)
    assert not staged.exists()
    if change == "invalid-config":
        assert path.read_bytes() == b"{}"
    else:
        assert path.stat().st_nlink == 2
        assert (tmp_path / "unrelated.json").samefile(path)
