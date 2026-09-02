---
name: bump-skills-version
description: Bump the shared maximiliamus/skills repository version and release changelog. Use when preparing the next skills release, defaulting to the next minor version, applying an explicitly requested patch or major bump, or setting an exact X.Y.Z or vX.Y.Z version. Do not use for Spec Kit Canon product version changes.
---

# Bump Skills Version

Update the repository-level release version and changelog together. This skill
prepares release metadata; it does not commit, tag, push, or publish.

## Resolve The Target

- When asked to infer the release kind from the changes, use SemVer:
  - major for breaking changes in skill contracts, command interfaces, or
    directory schemas;
  - minor for new skills or backward-compatible feature enhancements;
  - patch for bug fixes, documentation updates, or internal performance
    improvements.
- A request to bump the skills version without another qualifier means the next
  minor version.
- Honor an explicit `minor`, `patch`, or `major` bump.
- Honor an exact stable SemVer supplied as `X.Y.Z` or `vX.Y.Z`; write it without
  the leading `v`.
- Require the target to be greater than the current `[project].version` in
  `pyproject.toml`.

## Apply The Bump

From the `maximiliamus/skills` repository root, inspect the worktree and ensure
`pyproject.toml` and `CHANGELOG.md` have no uncommitted changes. Preserve all
unrelated work. Require `[project].name = "maximiliamus-skills"`; stop instead
of modifying a different Python project.

Run the bundled helper. The default is a minor bump:

```bash
python .agents/skills/bump-skills-version/scripts/bump_skills_version.py
```

Select another relative bump:

```bash
python .agents/skills/bump-skills-version/scripts/bump_skills_version.py \
  --kind patch
python .agents/skills/bump-skills-version/scripts/bump_skills_version.py \
  --kind major
```

Set an exact version only when explicitly requested:

```bash
python .agents/skills/bump-skills-version/scripts/bump_skills_version.py \
  --version v0.2.0
```

Use `--dry-run` to resolve and preview the target without changing files.

The helper must:

- update only `[project].version` in `pyproject.toml`;
- keep a fresh empty `## [Unreleased]` section in `CHANGELOG.md`;
- promote reviewed Unreleased content to
  `## [X.Y.Z] - YYYY-MM-DD` when present;
- otherwise derive English release notes from local Conventional Commit
  subjects since the latest reachable lower SemVer tag;
- refuse invalid, non-increasing, already tagged, or already documented target
  versions before writing either file.

## Verify The Result

1. Read the updated version from `pyproject.toml`.
2. Confirm `CHANGELOG.md` contains exactly one matching release heading and a
   fresh `## [Unreleased]` section above it.
3. Confirm the working-tree changes introduced by the bump are limited to
   `pyproject.toml` and `CHANGELOG.md`.
4. Report the resolved version and changed files. Do not commit, tag, push, or
   create a GitHub Release.
