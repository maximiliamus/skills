"""Project-owned paired-review profiles and one-time initialization."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "conduct-paired-review"
SCRIPT = SKILL / "scripts" / "review_files.py"
spec = importlib.util.spec_from_file_location("paired_review_config_under_test", SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


@pytest.fixture
def project(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path



def template():
    return review.read_json(SKILL / "config.json")



def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    return path



def cli(capsys, *args):
    code = review.main([str(arg) for arg in args])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return json.loads(captured.out)



def test_init_config_creates_exact_template_without_starting_review(project, capsys):
    result = cli(capsys, "init", project)
    path = project / "conduct-paired-review.json"
    assert result["created"] is True and Path(result["configPath"]) == path
    assert path.read_bytes() == (SKILL / "config.json").read_bytes()
    assert not list(project.rglob("*-RP.md"))
    assert not list(project.rglob("session.json"))



@pytest.mark.parametrize("nested", [False, True])
def test_config_commands_use_active_project_without_a_path(project, monkeypatch, capsys, nested):
    working = project / "docs/subdirectory" if nested else project
    working.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(working)
    result = cli(capsys, "init")
    path = project / review.PROJECT_CONFIG_NAME
    assert result["created"] and Path(result["configPath"]) == path
    config = template()
    config["maxStagnantTurns"] = 8
    write_json(path, config)
    before, modified = path.read_bytes(), path.stat().st_mtime_ns
    assert cli(capsys, "update")["config"] == config
    assert cli(capsys, "init")["created"] is False
    assert path.read_bytes() == before and path.stat().st_mtime_ns == modified
    assert not nested or not (working / review.PROJECT_CONFIG_NAME).exists()



@pytest.mark.parametrize("marker", ["AGENTS.md", ".agents/skills/conduct-paired-review/SKILL.md"])
def test_config_commands_discover_non_git_project_from_context(
    tmp_path, monkeypatch, capsys, marker, non_git_project,
):
    marker_path = tmp_path / marker
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text("Project instructions.\n", encoding="utf-8")
    working = tmp_path / "docs"
    working.mkdir()
    monkeypatch.chdir(working)
    assert cli(capsys, "init")["projectRoot"] == str(tmp_path)
    assert cli(capsys, "update")["projectRoot"] == str(tmp_path)



def test_config_commands_honor_explicit_project_without_positional_path(
    project, monkeypatch, capsys,
):
    monkeypatch.chdir(project / "..")
    assert cli(capsys, "init", "--project", project)["projectRoot"] == str(project)



def test_nested_instructions_do_not_override_local_installation(
    tmp_path, monkeypatch, capsys, non_git_project,
):
    installed = tmp_path / ".agents/skills/conduct-paired-review/SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_text("Installed skill.\n", encoding="utf-8")
    working = tmp_path / "docs"
    working.mkdir()
    (working / "AGENTS.md").write_text("Documentation instructions.\n", encoding="utf-8")
    monkeypatch.chdir(working)
    assert cli(capsys, "init")["projectRoot"] == str(tmp_path)
    assert not (working / review.PROJECT_CONFIG_NAME).exists()



def test_nearest_config_keeps_nested_non_git_projects_distinct(
    tmp_path, monkeypatch, capsys, non_git_project,
):
    outer = write_json(tmp_path / review.PROJECT_CONFIG_NAME, template())
    inner = tmp_path / "nested"
    working = inner / "docs"
    working.mkdir(parents=True)
    config = template()
    config["maxStagnantTurns"] = 8
    write_json(inner / review.PROJECT_CONFIG_NAME, config)
    (working / "AGENTS.md").write_text("Documentation instructions.\n", encoding="utf-8")
    monkeypatch.chdir(working)
    result = cli(capsys, "update")
    assert result["projectRoot"] == str(inner) and result["config"] == config
    assert review.read_json(outer) == template()



def test_skill_update_never_merges_or_rewrites_project_config(
    project, tmp_path, monkeypatch, capsys,
):
    cli(capsys, "init", project)
    path = project / review.PROJECT_CONFIG_NAME
    config = template()
    config["maxStagnantTurns"] = 8
    config["participants"]["codex"]["effortLevel"] = "low"
    write_json(path, config)
    original, version = path.read_bytes(), path.stat().st_mtime_ns
    updated = template()
    updated["participants"]["new-client"] = dict(updated["participants"]["codex"])
    updated["maxStagnantTurns"] = 10
    installed_template = write_json(tmp_path / "updated-template.json", updated)
    monkeypatch.setattr(review, "CONFIG_TEMPLATE", installed_template)
    for _ in range(2):
        result = cli(capsys, "init", project)
        assert result["created"] is False and result["config"] == config
    assert path.read_bytes() == original and path.stat().st_mtime_ns == version
    installed_template.unlink()
    assert cli(capsys, "init", project)["config"] == config



def test_concurrent_config_initialization_publishes_only_once(project):
    command = [sys.executable, str(SCRIPT), "init", str(project)]
    processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(2)]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr.decode()
        results.append(json.loads(stdout))
    assert sorted(result["created"] for result in results) == [False, True]
    assert (project / review.PROJECT_CONFIG_NAME).read_bytes() == (SKILL / "config.json").read_bytes()



def test_hardlinked_config_is_rejected_without_modifying_its_target(project, capsys):
    target = write_json(project / "external-profile.json", template())
    os.link(target, project / review.PROJECT_CONFIG_NAME)
    before = target.read_bytes()
    assert review.main(["init", str(project)]) == 1
    assert "Hard-linked" in json.loads(capsys.readouterr().err)["error"]
    assert target.read_bytes() == before



def test_invalid_template_does_not_leave_a_partial_config(project, monkeypatch, capsys):
    invalid = project / "invalid-template.json"
    invalid.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(review, "CONFIG_TEMPLATE", invalid)
    assert review.main(["init", str(project)]) == 1
    assert "error" in json.loads(capsys.readouterr().err)
    assert not (project / review.PROJECT_CONFIG_NAME).exists()



def test_config_publication_never_replaces_a_last_instant_external_creation(
    project, monkeypatch, capsys,
):
    path = project / review.PROJECT_CONFIG_NAME
    custom = template()
    custom["maxStagnantTurns"] = 8
    publish = review.publish_new

    def competing(source, destination):
        with destination.open("x", encoding="utf-8") as handle:
            json.dump(custom, handle)
        return publish(source, destination)

    monkeypatch.setattr(review, "publish_new", competing)
    assert cli(capsys, "init", project)["created"] is False
    assert review.read_json(path) == custom
    assert not list(project.glob(".*.tmp"))



@pytest.mark.parametrize("value", [None, "{broken", "{}", '{"schemaVersion": 900}', "[]"])
def test_update_config_does_not_initialize_or_repair_invalid_profiles(project, capsys, value):
    path = project / review.PROJECT_CONFIG_NAME
    if value is not None:
        path.write_text(value, encoding="utf-8")
    before = path.read_bytes() if path.exists() else None
    assert review.main(["update", str(project)]) == 1
    assert "error" in json.loads(capsys.readouterr().err)
    assert (path.read_bytes() if path.exists() else None) == before



@pytest.mark.parametrize("version", [True, 1.0, "1", 0, 2])
def test_schema_version_must_be_a_supported_integer(project, capsys, version):
    config = template()
    config["schemaVersion"] = version
    path = write_json(project / review.PROJECT_CONFIG_NAME, config)
    before = path.read_bytes()
    assert review.main(["update", str(project)]) == 1
    assert "schema" in json.loads(capsys.readouterr().err)["error"]
    assert path.read_bytes() == before




def test_configuration_commands_create_no_service_state_and_preserve_review_files(
    project, monkeypatch, capsys,
):
    monkeypatch.chdir(project)
    first = project / "claim-R01C.md"
    first.write_text("Original review", encoding="utf-8")
    plan = project / "claim-RP.md"
    plan.write_text("Participants: codex author, claude critic. Pending correction.", encoding="utf-8")
    original = {p: p.read_bytes() for p in (first, plan)}
    assert cli(capsys, "init")["created"]
    config = project / review.PROJECT_CONFIG_NAME
    custom = template()
    custom["defaultRoles"] = {"author": "antigravity", "critic": "codex"}
    write_json(config, custom)
    saved, modified = config.read_bytes(), config.stat().st_mtime_ns
    for operation in ("init", "update"):
        assert cli(capsys, operation)["config"] == custom
    assert config.read_bytes() == saved and config.stat().st_mtime_ns == modified
    assert set(project.iterdir()) == {project / ".git", config, first, plan}
    assert {p: p.read_bytes() for p in original} == original
