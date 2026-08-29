# Repository Guidance

## Scope and validation

- Treat `skills/` as the distributable skill collection. Repository-local
  agent configuration such as this file is development infrastructure, not a
  published skill.
- Use Python 3.14 for routine development and validation.
- After code changes, run `python -m pytest` and `python -m ruff check`. After Markdown
  changes, also run `npx --yes markdownlint-cli2 "**/*.md"`.
- Preserve unrelated staged, unstaged, and untracked user changes.

## Code review rules

Use the built-in review workflow. A review is read-only unless the user
separately asks for fixes.

- Respect the selected review target. For a working-tree review, include
  staged, unstaged, deleted, renamed, and untracked files. If the repository
  has no `HEAD`, treat the current non-ignored working tree as the snapshot and
  distinguish it from the index.
- Do not stop after the first valid findings. Make one pass over contracts,
  state changes, persistence, and callers, then a fresh adversarial pass over
  boundary inputs, concurrency, portability, imports, and compatibility.
- Report only reproducible defects with a concrete trigger and observable
  impact. Give an exact file and line, explain why current checks miss the
  defect, and suggest the smallest safe correction direction.
- Do not infer historical persisted formats from compatibility requirements
  alone. Require evidence from a prior commit, released schema, pre-existing
  migration code, fixture, or documented deployed artifact. If the repository
  has no `HEAD` or release, treat the current schema as new unless the user
  confirms pre-existing data. Compatibility tests must model evidenced formats
  rather than manufacture them by deleting fields from the current schema.
- Validate plausible findings with focused tests or temporary-directory
  probes. Do not mutate repository files while reviewing.

### Repository invariants

- Any user-controlled identifier used in a filename must be portable across
  Windows, Linux, and macOS. Account for Windows reserved device names,
  trailing dots or spaces, Unicode, and filename-length limits; hash unsafe
  identifiers rather than using them literally.
- Helper code must not expose generic top-level import names such as `lib` or `utils`.
  Preserve both direct CLI execution and in-process imports when changing the runbook
  helper package.
- Registry and session-state mutations must remain atomic under repository-local
  locks stored inside `.runbooks`. Keep session state and lock files inside that
  directory; keep the committed registry at repository-root `runbooks.json`.
  Preserve required permissions and reject symlink or junction redirection.
- Treat the `runbook_session.py` CLI, its JSON output, the registry schema, and
  persisted session files as compatibility surfaces. Changes require coverage
  for existing ledgers, completed and outdated sessions, and registered or
  migrated runbook paths.
- Keep generic mechanical checks in tests or tooling. Add review guidance here
  only for consequential, non-obvious invariants that automation does not
  already enforce.
