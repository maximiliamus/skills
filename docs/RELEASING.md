# Releasing Skills

This document describes the release and version tagging lifecycle for the
`maximiliamus/skills` repository.

## Versioning Policy

This project adheres to [Semantic Versioning (SemVer)](https://semver.org/):

- **Major (X.0.0)**: Breaking changes in skill contracts, command interfaces, or
  directory schemas.
- **Minor (0.X.0)**: New skills added, or backward-compatible feature enhancements.
- **Patch (0.0.X)**: Bug fixes, doc updates, or internal performance improvements.

## Release Process

1. **Verify working tree cleanliness and test suite:**

   Run code quality and test gates:

   ```bash
   git status --porcelain
   python -m pytest
   python -m ruff check
   npx --yes markdownlint-cli2 "**/*.md"
   ```

   *Verification:* Require `git status --porcelain` to produce no output before
   continuing. Ensure all unit tests pass, ruff reports 0 lint errors, and
   markdownlint reports 0 issues.

2. **Update version identifiers and release notes:**

   - Update `version` in `pyproject.toml`
   - Add release notes in `CHANGELOG.md` under `## [X.Y.Z] - YYYY-MM-DD`

   *Verification:* Ensure the version in `pyproject.toml`, the heading in
   `CHANGELOG.md`, and the intended Git tag agree.

3. **Stage and commit release changes:**

   ```bash
   git add pyproject.toml CHANGELOG.md
   git commit -m "chore(release): prepare vX.Y.Z"
   ```

   *Verification:* Run `git log -n 1 --oneline` to confirm the commit was created
   and require `git status --porcelain` to produce no output.

4. **Create an annotated Git tag:**

   ```bash
   git tag -a vX.Y.Z -m "Release vX.Y.Z: <short summary>"
   ```

   Verify the tag target:

   ```bash
   git rev-parse "vX.Y.Z^{}"
   git rev-parse HEAD
   ```

   *Verification:* Require both commands to print the same commit ID. This
   confirms that the annotated tag resolves to the release commit.

5. **Publish branch and tag to GitHub:**

   ```bash
   git push --atomic origin main vX.Y.Z
   ```

   *Verification:* Ensure the push exits with code 0 and verify that the remote
   branch and the specific `vX.Y.Z` tag were updated.

6. **Create a GitHub Release:**

   - Go to `https://github.com/maximiliamus/skills/releases/new`
   - Select the tag `vX.Y.Z`
   - Set the title to `Release vX.Y.Z`
   - Paste the release notes from `CHANGELOG.md`
   - Publish release.

   *Verification:* Check that the release URL is live and accessible.
