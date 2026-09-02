---
name: execute-guided-runbook
description: Register, unregister, list, execute, and explicitly prune archived sessions for repository runbooks as resumable, verified, one-step-at-a-time workflows. Resolve stable IDs through an optional repository-root runbooks.json file, or use repository-relative Markdown paths when that file is absent. Use when managing runbooks, starting or resuming a session, verifying steps, pruning local runbook evidence, or advancing through operational release and maintenance workflows collaboratively.
---

# Execute Guided Runbook

Guide the user through one unresolved runbook step at a time. Keep a local
session ledger under `<repo>/.runbooks/` so another turn or session can resume
from verified evidence instead of reconstructing progress from chat history.
Treat this directory as uncommitted local state and ensure `/.runbooks/` is in
the repository's `.gitignore`.

Store archived ledger snapshots under `<repo>/.runbooks/archive/`. Retain them
indefinitely by default: ordinary runbook execution must never remove archived
evidence automatically. Prune only when the user explicitly requests archive
cleanup, and preview the exact archive set first unless the same request already
authorizes the deletion policy and scope.

Keep workflow progress separate from the outcome being assessed. A completed
step means that its check was performed and its result was recorded; it does
not necessarily mean that the checked criterion passed. Follow any scoring or
outcome model defined by the runbook, and never turn missing or failed evidence
into a successful result merely because the workflow continued.

Read the execution policy from optional Markdown frontmatter:

```yaml
---
id: release-readiness
description: Verify that a release is ready for publication.
acceptancePolicy: flexible
stepOrder: sequential
acceptanceThreshold: 80%
---
```

The helper accepts only unindented flat `key: value` string properties. Values
may be unquoted, single-quoted, or JSON-style double-quoted. `description` alone
may use `description: |` followed by space-indented literal lines. Do not use
nested mappings, sequences, other multiline forms, anchors, or tags; the strict
minimal parser rejects those constructs instead of pretending to support full
YAML.

`id` is an optional stable lowercase kebab-case identifier. It must match the
registry ID when the runbook is registered; without a registry, it replaces the
generated path ID. The helper safely migrates an existing path-ID ledger to a
newly declared document ID and then applies the normal revision-decision gate.

`description` is optional document-owned discovery metadata. `list`, `resolve`,
and `run` read its current value directly from the Markdown file. An explicit
`register --description` value overrides the document. For compatibility, a
description already stored in `runbooks.json` is also an explicit override.

The helper returns the resolved execution properties from `resolve`, `run`, and
`status` and stores them in the session ledger. Defaults are
`acceptancePolicy: flexible`, `stepOrder: sequential`, and no automatic
threshold. Do not infer a different mode from prose when a property is present.

Resolve acceptance independently from ordering:

- `strict` uses binary `PASS` or `FAIL`. The final result is `PASSED` only when
  every latest expected attempt passes and is otherwise `REJECTED`. Under
  sequential order, a failed expected step prevents advancing to a different
  step until a passing retry. Under arbitrary order, other steps may be
  assessed before returning to the failed step.
- `flexible` records a score. If `acceptanceThreshold` is present and the final
  rounded percentage reaches it, acceptance is automatic. Otherwise the result
  remains `PARTIAL` and the session remains active until the operator explicitly
  accepts or rejects it.
- `always` automatically produces `PASSED` for a 100% score and `ACCEPTED` for
  any lower score while preserving all limitations in evidence. It still
  requires every expected step to have a terminal assessment.

`acceptanceThreshold` uses an integer percentage from `1%` to `100%` and is
valid only with `acceptancePolicy: flexible`. Compare the final earned/maximum
score ratio with this normalized threshold, regardless of how many scored
criteria the current runbook revision contains.

Record scores in both natural and normalized form, for example `9/10 (90%)`.
The helper derives the percentage from the earned/available values and rounds
an exact half upward (`12.5%` becomes `13%`). Do not ask the operator to supply
both forms independently.

Keep the two result axes independent:

- session `status` is `active` until the workflow is evaluated and then becomes
  `completed`;
- assessment `result` is `PASSED`, `ACCEPTED`, `PARTIAL`, or `REJECTED`.

`PARTIAL` requires an operator decision and therefore does not complete the
session. `ACCEPTED` means completed with accepted limitations, not fully
passing. `REJECTED` may still belong to a completed assessment workflow.

Resolve step ordering independently:

- `sequential` selects the first unresolved actionable step in document order.
- `arbitrary` selects any unresolved actionable step while honoring declared
  dependencies and prerequisites.

Arbitrary order changes selection, not ledger concurrency. Keep at most one
current unresolved step, and retain completion history so skipped-over document
positions remain visible for later assessment.

A step whose assessment completed with a deficient result may be revisited
later in an arbitrary-order runbook. Start a new attempt with:

```bash
python <path-to-skill>/scripts/runbook_session.py step \
  <selector> <step-id> --title "<short title>" --retry
```

Use `--retry` only after the same step ID has a completed or skipped history
record. Preserve every prior attempt; record the new attempt's evidence
normally. Unless the runbook defines another aggregation rule, use the latest
attempt when calculating the final outcome and score.

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
metadata. A stored `description` is an override; omit it to use live document
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

  Pass `--title` or `--description` when document-derived defaults need an
  override. `--description` remains authoritative until the registry entry is
  removed. Pass `--effort-level` (`low`, `medium`, `high`, or `extra`) and
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
- For `$execute-guided-runbook prune <selector>`, require one explicit policy:
  `--keep-last <count>` retains the newest count, while
  `--older-than-days <days>` selects archives older than that age. Use
  `--dry-run` to report the exact selected files without deleting them. Pruning
  applies only to the selected runbook, never removes its current ledger, and
  does not run automatically during `run`, `finish`, `--ignore`, or `--restart`.
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
5. If a current step exists, resume it. Otherwise select the next actionable
   instruction according to the runbook's resolved ordering policy and exclude
   instructions already present in completed or skipped history.

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

When a check identifies missing or failed evidence, report the result before
advancing. Under strict policy, use `block` until the requirement is satisfied.
Under flexible policy, use `block` only while an action or operator decision is
still pending. If the operator explicitly accepts the available evidence and
directs the workflow to continue, record the step as completed with the
deficiency, the operator decision, and any lost score in the evidence. Continue
to the next step without calling the deficient criterion passed. Operator
acceptance does not authorize a retry, external mutation, or other separately
controlled action.

## Verify And Record

Do not mark a step complete merely because a command exited successfully. Check
the expected outcome named by the runbook.

After verification, record concise evidence and the step assessment. Under
`flexible` and `always`, supply the natural score; under `strict`, omit the score
because the helper derives `1/1` for pass and `0/1` for fail:

```bash
# flexible or always
python <path-to-skill>/scripts/runbook_session.py complete \
  <selector> --evidence "<what was checked and where>" \
  --result <pass|fail> --score <earned/available>

# strict
python <path-to-skill>/scripts/runbook_session.py complete \
  <selector> --evidence "<what was checked and where>" \
  --result <pass|fail>
```

If blocked, record the blocker:

```bash
python <path-to-skill>/scripts/runbook_session.py block \
  <selector> --reason "<concrete blocker>"
```

Skip a step only when permitted. Under `flexible` and `always`, record zero
earned points with the step's available points. Under `strict`, omit `--score`;
the helper records a failed `0/1` assessment:

```bash
# flexible or always
python <path-to-skill>/scripts/runbook_session.py skip \
  <selector> --reason "<why this step does not apply>" \
  --score <0/available>

# strict
python <path-to-skill>/scripts/runbook_session.py skip \
  <selector> --reason "<why this step does not apply>"
```

## Finish

Declare the runbook workflow complete only after every expected step has a
terminal assessment. Derive the complete set of actionable step IDs from the
current runbook revision and pass every ID, including skipped steps:

```bash
python <path-to-skill>/scripts/runbook_session.py finish \
  <selector> --evidence "<final completion criteria and evidence>" \
  --expected-step <step-id> [--expected-step <step-id> ...]
```

For a flexible result below its automatic threshold, `finish` returns
`PARTIAL` without completing the session. After the operator decides, repeat
the command with `--decision accept` or `--decision reject`. Do not supply an
operator decision when the policy or score already determines the result.

Completion of the workflow and success of its assessed outcome are distinct.
Report both `status` and `result`, plus the natural and percentage score. Do not
describe `ACCEPTED` or `REJECTED` as fully passing, and preserve every
limitation attached to an accepted result.

Ledgers created by an earlier helper version remain readable and use their
legacy recording protocol until restarted. A restart creates a structured
assessment ledger governed by the rules above.
