---
name: commit-bulk-changes
description: Prepare and apply one or more well-scoped commits using active user or repository conventions, with Conventional Commits as the default. Use before any Git commit, whether the change contains one file or a large set of staged, unstaged, untracked, deleted, or renamed files.
---

# Committing Changes

Use this skill before every Git commit. Despite its historical name, the skill
supports any change size: one file produces one commit when it represents one
concern; larger change sets may be split into multiple coherent commits.

## Scope and Authorization

- Commit only changes produced for the current task or explicitly placed in
  scope by the user.
- The absence of `HEAD` never expands the authorized scope. Create a complete
  initial snapshot only when the user explicitly places the complete
  non-ignored worktree in scope.
- Preserve unrelated staged, unstaged, and untracked changes.
- An explicit instruction to commit the current changes authorizes the commit
  when its scope is unambiguous. Report the commit plan, then proceed without
  requesting redundant confirmation.
- If the user asks only for a commit plan, do not stage or commit anything.
- If ownership or scope is ambiguous, stop and ask before changing the index.
- Never push unless the user explicitly requests it.

## Workflow

1. Inspect the complete worktree and index:

   ```bash
   git status --short
   git diff --name-status
   git diff --cached --name-status
   ```

   Inspect the relevant diffs before deciding how to group changes. Include
   untracked files in the inventory without assuming they belong to the task.

   Run `git rev-parse --verify HEAD`. If it fails because the repository has no
   first commit, never expand the requested scope:

   - For an explicitly authorized complete initial snapshot, review every
     staged, unstaged, and untracked path. Stop if the inventory contains
     credentials, secrets, or files that do not belong in the repository. Then
     report one commit with the exact message `Initial commit.`, stage the
     complete worktree with `git add --all`, inspect the complete staged diff,
     commit, perform the verification in step 6, and stop this workflow.
   - For a narrower request, continue through steps 2-4 with only the requested
     paths in scope, then use the scoped first-commit rule in step 5. Never use
     `git add --all` for a narrower request.

2. Establish the authorized file set.

   Include files changed for the current task and any additional paths the user
   explicitly names. Do not absorb unrelated existing changes merely because
   they are staged or located near an affected file.

3. Group the authorized changes by logical concern.

   Each commit must represent one coherent change. A single-file change
   normally forms one commit. Multiple files may remain together when they
   implement, test, or document the same concern.

   Keep both sides of a rename in the same group. Do not split one file between
   commits unless patch-level staging is necessary and its existing index state
   can be preserved safely.

4. Prepare a commit plan.

   For every group, report:
   - the commit message following active instructions, or a Conventional Commit
     message when no convention is specified;
   - the exact repository-relative file list;
   - the reason the files belong together when more than one group exists.

   If the user has already requested a commit and the scope is clear, proceed
   after reporting the plan. Otherwise stop after presenting it.

5. Apply each group safely.

   First record the pre-existing staged paths and inspect their staged diff.

   - If the repository has no `HEAD` and the request is narrower than a complete
     initial snapshot, stop if any change outside the current group is staged,
     unless an isolated-index method can preserve it exactly. Otherwise stage
     only the exact authorized paths, verify that the complete staged diff
     contains exactly those changes, and commit with the planned message.
   - If `HEAD` exists and no unrelated changes are staged, stage only exact
     paths from the current group. Use `git add -- <paths>` for added or
     modified paths and the appropriate deletion-aware form for removed paths.
     Never use `git add .`, broad globs, or another command that could capture
     unrelated changes.
     Verify that `git diff --cached --name-status` contains exactly the intended
     group, inspect `git diff --cached --check` and the staged diff, then run
     `git commit` with the planned message.
   - If `HEAD` exists and unrelated changes are already staged, do not add or
     unstage them. For a group containing only tracked files whose complete
     working-tree changes are authorized, inspect `git diff HEAD -- <paths>` and
     `git diff --check HEAD -- <paths>`, then commit only those paths with
     `git commit --only -m "<message>" -- <paths>`. Verify afterward that the
     unrelated staged diff is unchanged.
   - If the intended group contains an untracked file, a rename with an
     untracked destination, or a partially staged path while unrelated changes
     are staged, stop before changing the index unless an isolated-index method
     can preserve the existing staged diff exactly.
   - Keep both sides of a rename in the same group.

6. Verify the result after every commit:

   ```bash
   git status --short
   git log -1 --oneline
   ```

   Confirm that the commit contains the intended files and that unrelated
   changes remain intact. Continue with the next planned group in dependency
   order.

## Grouping Rules

- Keep implementation and its directly corresponding tests together unless
  they are independently useful changes.
- Keep generated metadata with the source change that requires it.
- Keep documentation with the behavior it describes when both are part of the
  same task.
- Separate unrelated refactors, release metadata, documentation corrections,
  and feature behavior.
- Order multiple commits from foundational changes to dependent changes.

## Commit Message Rules

- Use any commit conventions or formatting rules already present in your active
  context or requested by the user. If no specific convention is given, default
  to Conventional Commits format: `<type>(<scope>): <subject>`.
  - Common types: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `ci`,
    `build`, `perf`, `style`, and `revert`.
  - Include a scope when it makes the change clearer.
- Follow any commit-message language or character rules in the active context.
  When none are specified, use a concise English subject, normally no longer
  than 72 characters, and ASCII characters only.
- Add a body when the subject does not adequately explain motivation,
  constraints, or impact.
- Do not claim broader behavior than the staged diff implements.
