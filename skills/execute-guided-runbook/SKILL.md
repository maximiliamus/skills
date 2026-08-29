---
name: execute-guided-runbook
description: Register, unregister, list, and execute repository runbooks as resumable, verified, one-step-at-a-time workflows. Resolve stable IDs through an optional repository-root runbooks.json file, or use repository-relative Markdown paths when that file is absent. Use when managing runbooks, starting a runbook, resuming an interrupted session, verifying steps, or advancing through operational release and maintenance workflows collaboratively.
---

# Execute Guided Runbook

Guide the user through one unresolved runbook step at a time. Keep a local
session ledger under `<repo>/.runbooks/` so another turn or session can resume
from verified evidence instead of reconstructing progress from chat history.
Treat this directory as uncommitted local state and ensure `/.runbooks/` is in
the repository's `.gitignore`.

## Resolve The Request

Treat the first argument as an operation or runbook selector.

Resolve selectors in exactly one of these repository-local modes:

- If `<repo>/runbooks.json` exists, treat the selector as a registered ID. Do
  not fall back to interpreting an unknown ID as a path.
- If `<repo>/runbooks.json` does not exist, treat the selector as a
  repository-relative Markdown path.

The optional `runbooks.json` file is committed project configuration. Its
minimal format is:

```json
{
  "schemaVersion": 1,
  "runbooks": [
    {
      "id": "release",
      "path": "docs/runbooks/release.md"
    }
  ]
}
```

`title`, `description`, `effortLevel`, and `modelTier` are optional per-entry
metadata. IDs are lowercase kebab-case and do not need to match the Markdown
filename. IDs matching `path-<12 lowercase hex characters>` are reserved for
unregistered path sessions. See the bundled
[registry schema](references/runbook-registry.schema.json) for the complete
format. Registry paths are emitted with `/`; the helper also accepts and
normalizes `\` separators from Windows-authored files.

- For `$execute-guided-runbook list`, run:

  ```bash
  python <path-to-skill>/scripts/runbook_session.py list
  ```

  Return every registered ID with its resolved metadata. If `runbooks.json`
  does not exist, return an empty list. Do not create or update a session.
- For `$execute-guided-runbook register <id> <path>`, run:

  ```bash
  python <path-to-skill>/scripts/runbook_session.py register <id> <path>
  ```

  Pass `--title` or `--description` when document-derived defaults need
  override. Pass `--effort-level` (`low`, `medium`, `high`, or `extra`) and
  `--model-tier` (`light`, `medium`, or `heavy`) as needed. This command creates
  or updates `<repo>/runbooks.json`; it does not copy the Markdown file. When an
  existing ID is updated, omitted metadata keeps its previous value. When the
  same path already has an unregistered session, registration migrates that
  ledger and its history to the registered ID. If both identities already have
  ledgers, resolve the conflict explicitly before retrying registration.
- For `$execute-guided-runbook unregister <id>`, run `unregister <id>`. Remove
  only the registry entry. Never delete the Markdown runbook or its local
  session ledger.
- For `$execute-guided-runbook status <selector>`, run `status <selector>` and
  summarize the session status, current step, completed steps, blockers, and
  runbook path. If the result contains `"outdated": true`, describe the
  completion as belonging to an older revision; do not report the current
  runbook as complete until a new session finishes.
- For `$execute-guided-runbook run <selector>`, start or resume the selected
  runbook using the repository mode described above.
- If no selector is supplied and `runbooks.json` exists, list registered
  runbooks and ask which ID to run. Otherwise ask for a repository-relative
  Markdown path. Do not choose implicitly.

Never accept a runbook path outside the repository.

Treat `effort_level` (`low`, `medium`, `high`, `extra`) and `model_tier`
(`light`, `medium`, `heavy`) as advisory guidance for task planning and
execution depth.

## Start Or Resume

1. Resolve and initialize the session:

   ```bash
   python <path-to-skill>/scripts/runbook_session.py run <selector>
   ```

2. Read the resolved runbook completely before acting. The helper validates its
   content hash on every session operation.
3. If the runbook changed while its session is still unfinished, the helper
   returns `operator_decision_required`. Prompt the operator and wait for an
   explicit decision:
   - **Continue** (`--continue`): preserves saved progress and applies it to the
     new revision.
   - **Ignore** (`--ignore`): archives the unfinished ledger and starts from the
     first step.
4. Use `--restart` only when explicitly asked to restart a same-revision
   session.
5. If a current step exists, resume it. Otherwise find the first actionable
   instruction not present in completed or skipped history.

Record the selected step before presenting or executing it:

```bash
python <path-to-skill>/scripts/runbook_session.py step \
  <selector> <step-id> --title "<short title>"
```

## Execute One Step

For the current step:

1. Explain the intended outcome in the user's language.
2. Show the exact action or command and the verification check.
3. Execute safe read-only checks directly when in scope.
4. Execute repository mutations only when authorized. Require explicit
   confirmation for external mutations (push, tag, publish, merge).
5. **Manual / Infeasible Execution Handoff**:
   When a step requires human intervention (such as 2FA, web UI actions,
   sensitive credentials, or hardware tokens) or when the agent determines
   that it cannot execute the step automatically (due to missing tooling,
   permission boundaries, or environment constraints):
   - Clearly state why automated execution cannot proceed.
   - Provide the operator with concise, step-by-step instructions to perform
     the action manually.
   - Provide the exact verification command or check the operator (or agent)
     should run to confirm completion.
   - Wait for operator confirmation or output before advancing.
6. Stop with at most one unresolved step. Let the user perform or approve it
   before advancing.

## Verify And Record

Do not mark a step complete merely because a command exited successfully. Check
the expected outcome named by the runbook.

After verification, record concise evidence:

```bash
python <path-to-skill>/scripts/runbook_session.py complete \
  <selector> --evidence "<what was checked and where>"
```

If blocked, record the blocker:

```bash
python <path-to-skill>/scripts/runbook_session.py block \
  <selector> --reason "<concrete blocker>"
```

Skip a step only when permitted:

```bash
python <path-to-skill>/scripts/runbook_session.py skip \
  <selector> --reason "<why this step does not apply>"
```

## Finish

Declare the runbook complete only after every required step has evidence:

```bash
python <path-to-skill>/scripts/runbook_session.py finish \
  <selector> --evidence "<final completion criteria and evidence>"
```
