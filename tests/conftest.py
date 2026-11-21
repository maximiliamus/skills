"""Isolate project discovery fixtures from the containing development checkout."""

from pathlib import Path

import pytest


@pytest.fixture
def non_git_project(tmp_path, monkeypatch):
    exists, is_file = Path.exists, Path.is_file
    filenames = {"AGENTS.md", "conduct-paired-review.json", "execute-guided-runbook.json"}

    def project_file(path):
        marker = path.name in filenames or (path.name == "SKILL.md" and ".agents" in path.parts)
        return False if marker and not path.is_relative_to(tmp_path) else is_file(path)

    monkeypatch.setattr(Path, "exists", lambda path: path.name != ".git" and exists(path))
    monkeypatch.setattr(Path, "is_file", project_file)
    return tmp_path
