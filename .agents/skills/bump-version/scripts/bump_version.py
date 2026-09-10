#!/usr/bin/env python3
"""Bump the shared skills repository version and release changelog."""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SEMVER_RE = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$"
)
PROJECT_VERSION_RE = re.compile(
    r"^(?P<prefix>\s*version\s*=\s*)(?P<quote>['\"])(?P<version>[^'\"]+)"
    r"(?P=quote)(?P<suffix>\s*(?:#.*)?)$"
)
RELEASE_HEADING_RE = re.compile(
    r"^## \[(?P<version>\d+\.\d+\.\d+)\] - (?P<date>\d{4}-\d{2}-\d{2})$"
)
TAG_RE = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
CONVENTIONAL_RE = re.compile(
    r"^(?P<type>[a-z]+)(?:\([^\)]*\))?(?P<breaking>!)?: (?P<description>.+)$"
)
BUMP_KINDS = ("major", "minor", "patch")
CONVENTIONAL_SECTIONS = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "docs": "Documentation",
    "perf": "Performance",
    "refactor": "Refactors",
    "test": "Testing",
    "ci": "CI/CD",
    "build": "Build",
    "chore": "Chores",
}
SECTION_ORDER = (
    "Features",
    "Bug Fixes",
    "Documentation",
    "Performance",
    "Refactors",
    "Testing",
    "CI/CD",
    "Build",
    "Chores",
    "Miscellaneous",
)
EXPECTED_PROJECT_NAME = "maximiliamus-skills"


@dataclass(frozen=True)
class ChangelogPlan:
    updated_text: str
    previous_tag: str | None
    used_unreleased: bool
    commit_count: int


def normalize_version(raw_value: str) -> str:
    match = SEMVER_RE.fullmatch(raw_value.strip())
    if match is None:
        raise ValueError(
            f"Invalid version {raw_value!r}. Use stable SemVer like 0.2.0 or v0.2.0."
        )
    return ".".join(match.group(name) for name in ("major", "minor", "patch"))


def semver_parts(version: str) -> tuple[int, int, int]:
    normalized = normalize_version(version)
    major, minor, patch = normalized.split(".")
    return int(major), int(minor), int(patch)


def bump_version(version: str, kind: str) -> str:
    major, minor, patch = semver_parts(version)
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"Unsupported bump kind {kind!r}.")


def read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"Missing required file: {path}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"Required file is not valid UTF-8: {path}") from exc


def write_text_atomic(path: Path, text: str) -> None:
    original_mode = stat.S_IMODE(path.stat().st_mode)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, original_mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def run_git(repo_root: Path, *arguments: str, allow_exit: set[int] | None = None) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    allowed = allow_exit or {0}
    if completed.returncode not in allowed:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise OSError(detail or f"git {' '.join(arguments)} failed")
    return completed.stdout


def require_repository_root(repo_root: Path) -> None:
    resolved = repo_root.resolve()
    discovered = Path(run_git(resolved, "rev-parse", "--show-toplevel").strip()).resolve()
    if discovered != resolved:
        raise ValueError(f"Expected repository root {resolved}, found {discovered}.")


def require_clean_targets(repo_root: Path, paths: tuple[Path, ...]) -> None:
    relative_paths = [path.relative_to(repo_root).as_posix() for path in paths]
    status = run_git(repo_root, "status", "--porcelain", "--", *relative_paths)
    if status.strip():
        raise ValueError(
            "Release metadata already has uncommitted changes; preserve or commit them "
            f"before bumping:\n{status.rstrip()}"
        )


def plan_pyproject_version(text: str, path: Path) -> tuple[str, str, int]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in {path}: {exc}") from exc
    project = document.get("project")
    project_name = project.get("name") if isinstance(project, dict) else None
    if project_name != EXPECTED_PROJECT_NAME:
        raise ValueError(
            f"Expected [project].name = {EXPECTED_PROJECT_NAME!r} in {path}, "
            f"found {project_name!r}."
        )
    current_value = project.get("version") if isinstance(project, dict) else None
    if not isinstance(current_value, str):
        raise TypeError(f"Missing string [project].version in {path}.")
    current_version = normalize_version(current_value)

    lines = text.splitlines()
    in_project = False
    version_index: int | None = None
    version_match: re.Match[str] | None = None
    for index, line in enumerate(lines):
        section = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if section is not None:
            in_project = section.group(1).strip() == "project"
            continue
        if not in_project:
            continue
        match = PROJECT_VERSION_RE.fullmatch(line)
        if match is not None:
            if version_index is not None:
                raise ValueError(f"Duplicate [project].version assignments in {path}.")
            version_index = index
            version_match = match

    if version_index is None or version_match is None:
        raise ValueError(f"Could not locate [project].version assignment in {path}.")
    if normalize_version(version_match.group("version")) != current_version:
        raise ValueError(f"Parsed and textual [project].version disagree in {path}.")
    return current_version, "\r\n" if "\r\n" in text else "\n", version_index


def replace_pyproject_version(text: str, path: Path, target_version: str) -> str:
    current_version, line_ending, index = plan_pyproject_version(text, path)
    lines = text.splitlines()
    match = PROJECT_VERSION_RE.fullmatch(lines[index])
    assert match is not None
    lines[index] = (
        f"{match.group('prefix')}{match.group('quote')}{target_version}"
        f"{match.group('quote')}{match.group('suffix')}"
    )
    updated = line_ending.join(lines)
    if text.endswith(("\r", "\n")):
        updated += line_ending
    if current_version == target_version:
        return text
    return updated


def release_tag_parts(tag: str) -> tuple[int, int, int] | None:
    match = TAG_RE.fullmatch(tag.strip())
    if match is None:
        return None
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def resolve_previous_tag(repo_root: Path, target_version: str) -> str | None:
    target = semver_parts(target_version)
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for raw_tag in run_git(repo_root, "tag", "--merged", "HEAD", "--list", "v*").splitlines():
        tag = raw_tag.strip()
        parts = release_tag_parts(tag)
        if parts is not None and parts < target:
            candidates.append((parts, tag))
    return max(candidates)[1] if candidates else None


def require_unused_target(repo_root: Path, target_version: str, changelog: str) -> None:
    target_tag = f"v{target_version}"
    existing_tag = run_git(
        repo_root,
        "tag",
        "--list",
        target_tag,
    ).strip()
    if existing_tag:
        raise ValueError(f"Release tag already exists: {target_tag}")
    matching_headings = [
        line for line in changelog.splitlines() if line.startswith(f"## [{target_version}]")
    ]
    if matching_headings:
        raise ValueError(f"CHANGELOG.md already documents version {target_version}.")


def polished_entry(description: str, breaking: bool) -> str:
    entry = description.strip()
    if not entry:
        return "Record an unspecified repository change."
    entry = entry[0].upper() + entry[1:]
    if breaking:
        entry = f"{entry} (breaking change)"
    if entry[-1] not in ".!?":
        entry += "."
    return entry


def collect_release_notes(
    repo_root: Path,
    previous_tag: str | None,
) -> tuple[dict[str, list[str]], int]:
    revision = f"{previous_tag}..HEAD" if previous_tag else "HEAD"
    subjects = [
        line.strip()
        for line in run_git(repo_root, "log", "--no-merges", "--pretty=%s", revision).splitlines()
        if line.strip()
    ]
    sections: dict[str, list[str]] = {}
    for subject in subjects:
        match = CONVENTIONAL_RE.fullmatch(subject)
        if match is None:
            section = "Miscellaneous"
            entry = polished_entry(subject, False)
        else:
            section = CONVENTIONAL_SECTIONS.get(match.group("type"), "Miscellaneous")
            entry = polished_entry(match.group("description"), bool(match.group("breaking")))
        sections.setdefault(section, []).append(entry)
    return sections, len(subjects)


def generated_release_body(
    sections: dict[str, list[str]],
    previous_tag: str | None,
) -> str:
    lines: list[str] = []
    for section in SECTION_ORDER:
        entries = sections.get(section)
        if not entries:
            continue
        if lines:
            lines.append("")
        lines.extend((f"### {section}", ""))
        lines.extend(f"- {entry}" for entry in entries)
    if not lines:
        reference = previous_tag or "repository history"
        lines.append(f"- No recorded changes since {reference}.")
    return "\n".join(lines)


def plan_changelog(
    repo_root: Path,
    text: str,
    target_version: str,
) -> ChangelogPlan:
    if not text.lstrip().startswith("# Changelog"):
        raise ValueError("CHANGELOG.md must start with '# Changelog'.")
    lines = text.splitlines()
    unreleased_positions = [
        index for index, line in enumerate(lines) if line.strip() == "## [Unreleased]"
    ]
    if len(unreleased_positions) != 1:
        raise ValueError("CHANGELOG.md must contain exactly one ## [Unreleased] section.")
    start = unreleased_positions[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    unreleased_body = "\n".join(lines[start + 1 : end]).strip()
    remaining_lines = [*lines[:start], *lines[end:]]
    first_section = next(
        (index for index, line in enumerate(remaining_lines) if line.startswith("## ")),
        len(remaining_lines),
    )
    preamble = "\n".join(remaining_lines[:first_section]).strip()
    existing_sections = "\n".join(remaining_lines[first_section:]).strip()

    previous_tag = resolve_previous_tag(repo_root, target_version)
    sections, commit_count = collect_release_notes(repo_root, previous_tag)
    release_body = unreleased_body or generated_release_body(sections, previous_tag)
    release_entry = (
        f"## [{target_version}] - {datetime.now(UTC).date().isoformat()}\n\n"
        f"{release_body}"
    )
    chunks = [preamble, "## [Unreleased]", release_entry]
    if existing_sections:
        chunks.append(existing_sections)
    normalized = "\n\n".join(chunks) + "\n"
    line_ending = "\r\n" if "\r\n" in text else "\n"
    return ChangelogPlan(
        updated_text=normalized.replace("\n", line_ending),
        previous_tag=previous_tag,
        used_unreleased=bool(unreleased_body),
        commit_count=commit_count,
    )


def validate_planned_release(
    pyproject_text: str,
    changelog_text: str,
    target_version: str,
) -> None:
    parsed = tomllib.loads(pyproject_text)
    project = parsed.get("project")
    written_version = project.get("version") if isinstance(project, dict) else None
    if written_version != target_version:
        raise ValueError(
            f"Planned [project].version is {written_version!r}, expected {target_version}."
        )
    if changelog_text.splitlines().count("## [Unreleased]") != 1:
        raise ValueError("Planned CHANGELOG.md must contain one ## [Unreleased] section.")
    target_headings = [
        line
        for line in changelog_text.splitlines()
        if (match := RELEASE_HEADING_RE.fullmatch(line)) is not None
        and match.group("version") == target_version
    ]
    if len(target_headings) != 1:
        raise ValueError(
            f"Planned CHANGELOG.md must contain one dated [{target_version}] heading."
        )
    if changelog_text.index("## [Unreleased]") > changelog_text.index(target_headings[0]):
        raise ValueError("Planned CHANGELOG.md must keep Unreleased above the new release.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bump the skills repository version and changelog."
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--version", help="Exact stable SemVer, with optional v prefix.")
    target.add_argument("--kind", choices=BUMP_KINDS, help="Relative bump kind.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repo-root", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = (
        args.repo_root.resolve()
        if args.repo_root is not None
        else Path(__file__).resolve().parents[4]
    )
    pyproject_path = repo_root / "pyproject.toml"
    changelog_path = repo_root / "CHANGELOG.md"
    try:
        require_repository_root(repo_root)
        require_clean_targets(repo_root, (pyproject_path, changelog_path))
        pyproject_text = read_text(pyproject_path)
        changelog_text = read_text(changelog_path)
        current_version, _, _ = plan_pyproject_version(pyproject_text, pyproject_path)
        if args.version is not None:
            target_version = normalize_version(args.version)
            resolution = f"exact version {target_version}"
        else:
            kind = args.kind or "minor"
            target_version = bump_version(current_version, kind)
            resolution = f"{kind} bump from {current_version}"
        if semver_parts(target_version) <= semver_parts(current_version):
            raise ValueError(
                f"Target version {target_version} must be greater than {current_version}."
            )
        require_unused_target(repo_root, target_version, changelog_text)
        updated_pyproject = replace_pyproject_version(
            pyproject_text,
            pyproject_path,
            target_version,
        )
        changelog_plan = plan_changelog(repo_root, changelog_text, target_version)
        validate_planned_release(
            updated_pyproject,
            changelog_plan.updated_text,
            target_version,
        )
        if not args.dry_run:
            write_text_atomic(pyproject_path, updated_pyproject)
            write_text_atomic(changelog_path, changelog_plan.updated_text)
    except (OSError, TypeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    verb = "Would bump" if args.dry_run else "Bumped"
    print(f"Resolved target: {resolution}")
    print(f"{verb} version: {current_version} -> {target_version}")
    if changelog_plan.used_unreleased:
        print(f"Promoted reviewed Unreleased notes to [{target_version}].")
    else:
        print(
            f"Generated [{target_version}] notes from "
            f"{changelog_plan.previous_tag or 'repository history'} "
            f"using {changelog_plan.commit_count} commit(s)."
        )
    if args.dry_run:
        print("Dry run: no files changed.")
    else:
        print("Updated: pyproject.toml, CHANGELOG.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
