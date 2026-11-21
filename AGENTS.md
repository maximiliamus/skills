# Repository Guidance

## Scope and validation

- The skill collection lives in `skills/`.
- Use Python 3.14 for routine development and validation.
- After code changes, run `python -m pytest` and `python -m ruff check`. After Markdown
  changes, also run `npx --yes markdownlint-cli2 "**/*.md"`.
- Preserve unrelated staged, unstaged, and untracked user changes.

## Changelog

- Write entries in English and describe the added or changed value for users:
  what they can now accomplish, what problem is solved, or what work is preserved.
- Describe final capabilities and group related changes by user outcome.
  Do not turn the changelog into a development log or list intermediate fixes
  made while building an unreleased feature.
- Keep usage instructions and technical details in skill documentation.
  Do not enumerate CLI commands, arguments, paths, configuration fields or
  dialogue mechanics. Omit internal design choices, refactors, tests, release
  mechanics and versioning policy. Explain any necessary upgrade impact in
  user terms and link to the upgrade instructions.
- Review every added or edited entry against these rules before committing,
  including entries promoted from `Unreleased` or derived from commit messages.

## Skill lifecycle commands

- Apply the `init` and `update` command convention only in `skills/`.
  Handle these commands before the skill's main workflow. Run the applicable
  setup or update actions, including external configuration management where
  supported, then finish the request.
- If the skill has no actions for the requested command, reply only
  `nothing to do`. Do not create files or execute its main workflow.

## Releases

- Before every release, invoke and execute [`$release`](.agents/skills/release/SKILL.md).
  Complete every required step and verification gate. Respect its authorization
  boundaries and stop conditions. A failed gate stops the release.

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
- Runbook registry and session-state mutations must remain atomic under repository-local
  locks stored inside `.runbooks`. Keep session state and lock files inside that
  directory; keep the committed registry at repository-root
  `execute-guided-runbook.json`.
  Preserve required permissions and reject symlink or junction redirection.
- Preserve the `runbook_session.py` CLI, its JSON output, the registry schema,
  and journals in the current format. Cover active and completed runs, changes
  to a procedure, and registered or moved paths when changing these surfaces.
  Procedures from earlier versions start a fresh run if needed.
- Keep generic mechanical checks in tests or tooling. Add review guidance here
  only for consequential, non-obvious invariants that automation does not
  already enforce.
