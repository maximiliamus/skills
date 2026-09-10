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
modelTier: medium
effortLevel: medium
modelFamily: frontier
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

`modelTier`, `effortLevel`, and `modelFamily` belong to the runbook's
frontmatter. Omitted properties use `medium`, `medium`, and `current`
respectively. Set them in the document to change its execution defaults;
registration does not accept or store these properties. `list` and `resolve`
read current document values; existing sessions retain their recorded model binding.

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
  <selector> <step-id> --title "<short title>" --retry \
  --model-id <actual-current-model>
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
  "schemaVersion": 2,
  "runbooks": [
    {
      "id": "release",
      "path": "docs/runbooks/release.md"
    }
  ]
}
```

`title` and `description` are optional per-entry metadata.
A stored `description` is an override; omit it to use live document
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
  removed. This command creates or updates `<repo>/runbooks.json`; it does not
  copy the Markdown file. When an
  existing ID is updated, omitted metadata keeps its previous value. When the
  same path already has an unregistered session, registration migrates that
  ledger and its history to the registered ID. If both identities already have
  ledgers, resolve the conflict explicitly before retrying registration.
  Its JSON response also shows the document's resolved model properties for
  inspection; these are not persisted in the registry. Updating registry
  metadata never retargets an existing session.
- For `$execute-guided-runbook unregister <id>`, run `unregister <id>`. Remove
  only the registry entry. Never delete the Markdown runbook or its local
  session ledger.
- For `$execute-guided-runbook status <selector>`, run `status <selector>` and
  summarize the session status, current step, completed steps, blockers, and
  runbook path, recorded model family, concrete model, tier, and effort.
  `list` shows document defaults;
  `status` shows the pinned ledger, without requiring a model match for inspection.
  If the result contains `"outdated": true`, describe the
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

## Model Selection And Execution

Keep `modelTier` (`light`, `medium`, `heavy`) independent of generation:
light covers straightforward bounded work, medium covers ordinary multi-step
reasoning, and heavy covers complex analysis and consequential judgment.
`effortLevel` (`low`, `medium`, `high`, `extra`) controls reasoning depth;
extra means the maximum supported depth. Map these semantics to the executing
agent's available capabilities; never use a tracked provider mapping.

Normalize operator requests independently of tier:

| Operator request | `modelFamily` |
| --- | --- |
| “используй текущую” | `current` |
| “используй frontier” | `frontier` |
| “используй предыдущие” | `predecessor` |
| “используй family 5.6” | `family:5.6` |
| “используй exact model-id” | `exact:<model-id>` |

An explicit request supplies `--model-family` for a new session only.
Otherwise use `modelFamily` from the document, or `current` when omitted. For
`current`, use the concrete identity of the current session without resolving
a generation. For every other value, resolve frontier, its **immediate**
predecessor generation, or the requested family from models available to the
current agent. Select one suitable concrete identity. For `exact`, use only
the requested identity while that binding is active. Stop before
execution if the generation, family, or a suitable model is ambiguous or
unavailable. Never infer generation from a session name or label, fall back to a
different generation, or mix generations.

Before starting, read the [agent-agnostic execution contract](references/model-execution.md).
Use the current agent's own capabilities to inspect available models and prove
its concrete identity. A session name or a requested model is not proof. If an
explicit selection does not match the current identity, report the mismatch and
stop before creating a ledger. Do not switch or create a session, ask the
operator to launch one, or choose a fallback. Wait for the operator to decide
what to do next. If the agent cannot prove its identity, stop with that concrete
limitation.

If the operator then explicitly directs execution on the model selected by the
runbook, use the current agent runtime's native model-session or delegation
capability to start a target context on that concrete model. The initiating
agent remains the operator-facing coordinator when the runtime supports it: it
forwards the target context's questions to the operator, returns the operator's
answers unchanged, and reports the target context's evidence and results. Do
not require the operator to move the conversation manually when native
proxying is available.

Pass the repository root, runbook selector, resolved `modelFamily`, concrete
`modelId`, `modelTier`, `effortLevel`, existing ledger status, and the
instruction to execute the whole runbook to the target context. The target
context must independently prove its actual `modelId` before it creates or
resumes the ledger. A new runbook therefore has no ledger until the target
context invokes `run`; an existing runbook resumes its single ledger without
`rebind` when the target identity already matches the recorded binding. Keep
the runbook in that target context until completion or another explicit
operator decision changes the binding or starts over. If the runtime cannot
create the required context or proxy its interaction, report that limitation
and stop without a fallback.

The helper records `modelFamily`, `modelId`, `modelTier`, and `effortLevel`.
Every execution command requires the actual current identity as `--model-id`
and compares it with the ledger. On resume, use the recorded values; do not
resolve the generation again against a changed model catalog. A mismatch fails
closed and never creates a parallel ledger. Registry edits, path migrations,
revision decisions, and restarts preserve the binding unless the operator
explicitly changes it.

After a mismatch, do nothing until the operator gives a new instruction. If
the operator says to continue on the current model, update the existing ledger
in place and preserve all progress:

```bash
python <path-to-skill>/scripts/runbook_session.py rebind <selector> \
  --model-family current --model-id <actual-current-model>
```

For another explicit family, resolve and verify that selection first, then use
the same command with its canonical `<family>` and actual model ID. `rebind`
changes only `modelFamily`, `modelId`, and `updatedAt`; it preserves
`modelTier`, `effortLevel`, the current step, history, decisions, and runbook
revision. Invoke it only after an explicit operator instruction to change the
model and continue the existing runbook.

## Start Or Resume

1. Use `resolve <selector>` and inspect an existing `status <selector>` before
   execution. Resolve the model only for a new session. After the current
   session proves its identity and satisfies the selection, start with:

   ```bash
   python <path-to-skill>/scripts/runbook_session.py run <selector> \
     --model-id <actual-current-model>
   ```

   Add `--model-family <family>` only for an explicit operator override.
   Resume an existing ledger with `run <selector>` from its verified model
   session. Pass `--model-id <actual-current-model>` to `step`, `complete`,
   `block`, `skip`, and `finish` as well. Read the resolved runbook completely
   before acting. The helper validates its content hash on every session operation.
2. Keep all saved progress and later operator decisions in that same ledger.
   A model change continues from that ledger after an explicit `rebind`. Use
   only the session controls provided by the current agent environment.
3. If the runbook changed while its session is still unfinished, the helper
   returns `operator_decision_required`. Prompt the operator and wait for an
   explicit decision:
   - **Continue** (`--continue`): preserves saved progress and applies it to the
     new revision.
   - **Ignore** (`--ignore`): archives the unfinished ledger and starts from the
     first step.
4. Start from the first step only when the operator explicitly says to start
   over. Use `--restart` for a same-revision session. If the runbook changed,
   use `--ignore` after the helper reports `operator_decision_required`. When
   the new run must use another model, perform the explicit `rebind` first;
   the restart or ignore operation then archives the old ledger and creates a
   clean one with the new binding.
5. If a current step exists, resume it. Otherwise select the next actionable
   instruction according to the runbook's resolved ordering policy and exclude
   instructions already present in completed or skipped history.

Record the selected step before presenting or executing it:

```bash
python <path-to-skill>/scripts/runbook_session.py step \
  <selector> <step-id> --title "<short title>" \
  --model-id <actual-current-model>
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
  --result <pass|fail> --score <earned/available> \
  --model-id <actual-current-model>

# strict
python <path-to-skill>/scripts/runbook_session.py complete \
  <selector> --evidence "<what was checked and where>" \
  --result <pass|fail> \
  --model-id <actual-current-model>
```

If blocked, record the blocker:

```bash
python <path-to-skill>/scripts/runbook_session.py block \
  <selector> --reason "<concrete blocker>" \
  --model-id <actual-current-model>
```

Skip a step only when permitted. Under `flexible` and `always`, record zero
earned points with the step's available points. Under `strict`, omit `--score`;
the helper records a failed `0/1` assessment:

```bash
# flexible or always
python <path-to-skill>/scripts/runbook_session.py skip \
  <selector> --reason "<why this step does not apply>" \
  --score <0/available> \
  --model-id <actual-current-model>

# strict
python <path-to-skill>/scripts/runbook_session.py skip \
  <selector> --reason "<why this step does not apply>" \
  --model-id <actual-current-model>
```

## Finish

Declare the runbook workflow complete only after every expected step has a
terminal assessment. Derive the complete set of actionable step IDs from the
current runbook revision and pass every ID, including skipped steps:

```bash
python <path-to-skill>/scripts/runbook_session.py finish \
  <selector> --evidence "<final completion criteria and evidence>" \
  --expected-step <step-id> [--expected-step <step-id> ...] \
  --model-id <actual-current-model>
```

For a flexible result below its automatic threshold, `finish` returns
`PARTIAL` without completing the session. After the operator decides, repeat
the command with `--decision accept` or `--decision reject`. Do not supply an
operator decision when the policy or score already determines the result.

Completion of the workflow and success of its assessed outcome are distinct.
Report both `status` and `result`, plus the natural and percentage score. Do not
describe `ACCEPTED` or `REJECTED` as fully passing, and preserve every
limitation attached to an accepted result.

Registry and session schemas are version 2. Version 1 ledgers and registries
are rejected; missing execution fields are never inferred. Preserve old ledger
files as evidence and resolve their disposition explicitly before starting a
new version 2 session. Do not claim their steps were executed on a recorded model.
