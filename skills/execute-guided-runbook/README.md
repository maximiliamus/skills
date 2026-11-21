# Execute Guided Runbook

Execute a Markdown repository procedure with independent step workers and a
durable local journal. The coordinator schedules through the current harness
and records results after verification. The helper persists completion, pause
and recovery decisions.

## Project configuration

Invoke the skill with `init` to create the project-owned
`execute-guided-runbook.json`, or with `update` to validate the current file.
Use `list`, `register ID PATH` and `unregister ID` to manage discovery. A
repository-relative Markdown path can also select a runbook. Registry IDs and
paths that identify the same runbook share an open run.

The helper keeps journals, staging and the repository lock under `.runbooks`.
Keep the discovery registry at the repository root. See the
[registry schema](references/runbook-registry.schema.json).

## Author a runbook

### Simple sequential steps

Use clear Markdown headings or an ordered list to describe the steps in
execution order. With no step markers, one slot, and no dependency graph,
the coordinator identifies the complete sequence once and supplies it at
reservation. Start fails when the document lacks an unambiguous sequence.

```markdown
# Verify delivery

## 1. Check inputs

Verify the selected inputs and record evidence.

## 2. Record findings

Write a summary from the verified inputs.
```

### Marked steps

Declare steps with standalone `<!-- step: <id> -->` markers at the top
level, immediately before ATX headings. Use stable lowercase kebab-case
IDs. A step extends to the next step marker or the end of the document.
Preamble and unmarked headings can contain shared or step-local
instructions. Markers inside code, quotes and list items are examples or
content. The helper rejects duplicate IDs and malformed markers.

```markdown
# Verify delivery

<!-- step: check -->
## Check inputs

Verify the selected inputs and record evidence.

<!-- step: report -->
## Record findings

Write the report from the current check assessment.

<!-- deliverable: reports/{runId}/report.md -->
```

### Execution settings

| Frontmatter property | Default | Behavior |
| --- | --- | --- |
| `maxParallelSteps` | `1` | Concurrent live attempt limit. |
| `maxAttempts` | `1` | Attempt budget per step, including the first. |
| `delegateSteps` | `false` | With `true`, each attempt needs a fresh worker. |
| `stepOrder` | `sequential` | Selection order in a single-slot runbook. |
| `steps` | Empty mapping | Dependencies and overrides by step ID. |
| `modelFamily` | `current` | Model selector. |
| `modelTier` | `medium` | `light`, `medium` or `heavy`. |
| `effortLevel` | `medium` | `low`, `medium`, `high` or `extra`. |
| `acceptancePolicy` | `flexible` | Also supports `strict` and `always`. |
| `acceptanceThreshold` | Unset | `1%` through `100%` for `flexible`. |

Both limits must be positive integers. The attempt budget carries across
invocations. With one slot, `sequential` and no explicit dependencies, the
helper follows Markdown order for both ways of declaring steps. It waits for
the first step without a current terminal outcome, including a step parked
on an answer or out of attempts.

Use `dependsOn` to express a graph, including steps that require another step
to succeed. Every listed prerequisite must hold. `passed` requires a current
`PASSED` outcome. `done` accepts any current terminal outcome. The helper also
accepts the older spelling `assessed` when reading Markdown. It rejects
unknown IDs, unsupported conditions, self-dependencies and cycles. In a
single-slot graph, `sequential` picks the first ready step in declaration
order. `arbitrary` or multiple slots allow any ready step. With multiple slots,
declare dependencies for preparation, reporting and other ordered work
explicitly.

Per-step settings can override `modelFamily`, `modelTier`, `effortLevel`,
`delegateSteps` and `maxAttempts`.

Use top-level `<!-- deliverable: <path> -->` markers after the owning step's
heading. Deliverable paths are repository-relative and can include `{runId}`.
See the [execution contract](references/journal-contract.md) for model
selection, output publication, recovery and assessment.

## Upgrade

New runs use journal v5. The current helper can read and continue v4 runs
with their existing graph, ordering rules, outcomes and history. The same
registered ID or path still identifies an open v4 run. Use the current helper
in every coordinator sharing the repository before reserving a v5 run. Older
helpers cannot discover or continue v5 journals.

For work from before v4 that is still needed, stop any earlier execution and
reconcile its effects. Reserve a fresh run with the current procedure and
zero progress. Keep earlier journals and archives at their existing paths
as reference files.

## Run, pause and resume

Choose ready independent steps and use native workers in parallel when the
procedure and harness allow it. After verifying the result and required files,
the coordinator records `done`. To pause, it stops handing off steps and waits
for the already handed-off workers and their commands to finish. If the harness
cannot confirm whether a worker or command ended, the run stays `pausing`.

Resume from the journal's frozen procedure. Preserve applicable completed work
and retry safe unfinished steps within the recorded budget. When a new
coordinator takes over, it records evidence that its predecessor can no
longer issue work. Questions, outstanding effects and unresolved publication
intents remain visible for reconciliation.
