from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / ".agents"
    / "skills"
    / "bump-skills-version"
    / "scripts"
    / "bump_skills_version.py"
)


def run_git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def commit_all(repo: Path, message: str) -> None:
    staged = run_git(repo, "add", "--all")
    assert staged.returncode == 0, staged.stderr
    committed = run_git(repo, "commit", "-m", message)
    assert committed.returncode == 0, committed.stderr


def create_repository(tmp_path: Path, unreleased_body: str = "- Add release automation.") -> Path:
    repo = tmp_path / "skills"
    repo.mkdir()
    initialized = run_git(repo, "init", "-b", "master")
    assert initialized.returncode == 0, initialized.stderr
    assert run_git(repo, "config", "user.name", "Test User").returncode == 0
    assert run_git(repo, "config", "user.email", "test@example.com").returncode == 0
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "maximiliamus-skills"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text(
        "# Changelog\n\n"
        "Release history.\n\n"
        "## [Unreleased]\n\n"
        f"{unreleased_body}\n\n"
        "## [0.1.0] - 2026-08-30\n\n"
        "- Initial release.\n",
        encoding="utf-8",
    )
    commit_all(repo, "chore(release): prepare v0.1.0")
    tagged = run_git(repo, "tag", "-a", "v0.1.0", "-m", "Release v0.1.0")
    assert tagged.returncode == 0, tagged.stderr
    return repo


def run_bump(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo-root", str(repo), *arguments],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_default_minor_bump_promotes_reviewed_unreleased_notes(tmp_path: Path):
    repo = create_repository(tmp_path)

    result = run_bump(repo)

    assert result.returncode == 0, result.stderr
    assert 'version = "0.2.0"' in (repo / "pyproject.toml").read_text(encoding="utf-8")
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    release_date = datetime.now(UTC).date().isoformat()
    assert changelog.count("## [Unreleased]") == 1
    assert changelog.count(f"## [0.2.0] - {release_date}") == 1
    assert changelog.index("## [Unreleased]") < changelog.index("## [0.2.0]")
    assert changelog.count("- Add release automation.") == 1
    assert "## [0.1.0] - 2026-08-30" in changelog
    assert set(run_git(repo, "diff", "--name-only").stdout.splitlines()) == {
        "CHANGELOG.md",
        "pyproject.toml",
    }


def test_patch_dry_run_reports_target_without_writing(tmp_path: Path):
    repo = create_repository(tmp_path)
    before = {
        path.name: path.read_bytes()
        for path in (repo / "pyproject.toml", repo / "CHANGELOG.md")
    }

    result = run_bump(repo, "--kind", "patch", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "0.1.0 -> 0.1.1" in result.stdout
    assert "Dry run: no files changed." in result.stdout
    for path in (repo / "pyproject.toml", repo / "CHANGELOG.md"):
        assert path.read_bytes() == before[path.name]
    assert run_git(repo, "status", "--porcelain").stdout == ""


def test_exact_version_strips_v_prefix(tmp_path: Path):
    repo = create_repository(tmp_path)

    result = run_bump(repo, "--version", "v0.3.0")

    assert result.returncode == 0, result.stderr
    assert 'version = "0.3.0"' in (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert "## [0.3.0] - " in (repo / "CHANGELOG.md").read_text(encoding="utf-8")


def test_empty_unreleased_generates_notes_from_conventional_commits(tmp_path: Path):
    repo = create_repository(tmp_path, unreleased_body="")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    commit_all(repo, "feat(release): add deterministic publishing")

    result = run_bump(repo)

    assert result.returncode == 0, result.stderr
    changelog = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "### Features" in changelog
    assert "- Add deterministic publishing." in changelog


@pytest.mark.parametrize("target", ["0.1.0", "0.0.9", "1.2", "v01.2.3"])
def test_invalid_or_non_increasing_exact_target_changes_nothing(
    tmp_path: Path,
    target: str,
):
    repo = create_repository(tmp_path)
    before_pyproject = (repo / "pyproject.toml").read_bytes()
    before_changelog = (repo / "CHANGELOG.md").read_bytes()

    result = run_bump(repo, "--version", target)

    assert result.returncode == 1
    assert (repo / "pyproject.toml").read_bytes() == before_pyproject
    assert (repo / "CHANGELOG.md").read_bytes() == before_changelog


def test_dirty_release_metadata_is_rejected_before_writing(tmp_path: Path):
    repo = create_repository(tmp_path)
    changelog = repo / "CHANGELOG.md"
    changelog.write_text(
        changelog.read_text(encoding="utf-8") + "\nPending edit.\n",
        encoding="utf-8",
    )
    before_pyproject = (repo / "pyproject.toml").read_bytes()
    before_changelog = changelog.read_bytes()

    result = run_bump(repo)

    assert result.returncode == 1
    assert "already has uncommitted changes" in result.stderr
    assert (repo / "pyproject.toml").read_bytes() == before_pyproject
    assert changelog.read_bytes() == before_changelog


def test_wrong_project_identity_is_rejected(tmp_path: Path):
    repo = create_repository(tmp_path)
    pyproject = repo / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace(
            'name = "maximiliamus-skills"',
            'name = "other-project"',
        ),
        encoding="utf-8",
    )
    commit_all(repo, "test: change project identity")
    before_changelog = (repo / "CHANGELOG.md").read_bytes()

    result = run_bump(repo)

    assert result.returncode == 1
    assert "Expected [project].name" in result.stderr
    assert (repo / "CHANGELOG.md").read_bytes() == before_changelog
