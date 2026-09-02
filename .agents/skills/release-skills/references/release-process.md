# Skills Release Process

This reference is the source of truth for releasing the
`maximiliamus/skills` repository. Follow the phases in order and stop at the
first failed gate.

## Preflight

1. Require `[project].name = "maximiliamus-skills"` and require the `origin`
   fetch and push URLs to identify `maximiliamus/skills`. Stop before any
   external action when repository identity is ambiguous.
2. Require the repository root, current branch `master`, an existing `HEAD`,
   and a completely clean staged and unstaged worktree.
3. Fetch `origin/master` without tags:

   ```bash
   git fetch --no-tags origin master
   ```

4. Require `origin/master` to be an ancestor of local `HEAD`. Stop on
   divergence or when the remote contains commits not present locally.
5. Resolve the requested target with the bump helper's `--dry-run`. Default to
   minor only when the operator supplied neither a kind nor an exact version.
6. Require that neither the local repository nor `origin` already has the
   target `vX.Y.Z` tag.

Do not bump files until every preflight condition passes.

## Bump And Validate

1. Invoke `$bump-skills-version` with the resolved kind or exact version.
2. Require the resulting diff to contain exactly `pyproject.toml` and
   `CHANGELOG.md`, with nothing staged.
3. Verify that `[project].version`, the release heading
   `## [X.Y.Z] - YYYY-MM-DD`, and the intended `vX.Y.Z` tag agree.
4. Run all repository gates on the bumped worktree:

   ```bash
   python -m pytest
   python -m ruff check
   npx --yes markdownlint-cli2 "**/*.md"
   ```

If any check fails, stop with the bump changes left uncommitted for inspection.
Do not commit, tag, or push a failing release candidate.

## Commit

1. Follow `skills/commit-bulk-changes/SKILL.md` to commit exactly
   `pyproject.toml` and `CHANGELOG.md` with:

   ```text
   chore(release): prepare vX.Y.Z
   ```

2. Require a clean worktree and confirm the release commit is `HEAD`.

If commit creation fails, stop without creating a tag or pushing.

## Tag

1. Create one annotated tag at `HEAD`:

   ```bash
   git tag -a vX.Y.Z -m "Release vX.Y.Z: shared agent skills"
   ```

2. Require `git rev-parse "vX.Y.Z^{}"` to equal `git rev-parse HEAD`.

If tag creation or verification fails, stop without pushing.

## Push And Verify

1. Atomically publish the branch and tag:

   ```bash
   git push --atomic origin master vX.Y.Z
   ```

2. Verify through `git ls-remote` that `origin/master`, the annotated tag, and
   its peeled target resolve to the expected local commit.

If the atomic push fails, leave the local release commit and tag intact. Report
their exact state; do not delete, recreate, or retry them without a new explicit
instruction.

## GitHub Release Boundary

The workflow does not create a GitHub Release. Create one only when the
operator separately requests it after the branch and tag have been published.
