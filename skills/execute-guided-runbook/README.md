# Execute Guided Runbook

`execute-guided-runbook` carries out Markdown runbooks as resumable, verified
workflows. It exposes one unresolved step at a time and records progress in a
local `.runbooks/` ledger, allowing a later turn or agent session to continue
from saved evidence.

Execution records and enforces a concrete model until the operator explicitly
changes it. Model-family values are `current` (the default), `frontier`, `predecessor`,
`family:<version>`, and `exact:<model-id>`. They constrain model selection
independently of the semantic `modelTier` and `effortLevel`; there is no fixed
provider/model table.

Set execution defaults in the Markdown runbook's frontmatter:

```yaml
---
modelTier: heavy
effortLevel: high
modelFamily: frontier
---
```

Omitted properties default to `medium`, `medium`, and `current` respectively.
Registration records the runbook's location and discovery metadata. It does not
accept or store these three execution properties in `runbooks.json`.

## How It Works

Runbooks can be selected by an ID from a repository-level `runbooks.json`
registry. When no registry exists, a repository-relative Markdown path can be
used instead. The skill supports listing and managing registered runbooks,
starting or resuming a session, recording completed or blocked steps, skipping
steps when permitted, and finishing a fully verified runbook.

Step completion records that a check was performed, not necessarily that its
criterion passed. Workflow status (`active` or `completed`) is separate from
assessment result (`PASSED`, `ACCEPTED`, `PARTIAL`, or `REJECTED`). Explicit
operator acceptance can complete a deficient result without turning the
deficiency into a pass.

Runbooks can declare a stable `id`, a discovery `description`,
`acceptancePolicy` (`strict`, `flexible`, or `always`), and `stepOrder`
(`sequential` or `arbitrary`) in Markdown frontmatter. Flexible policy may also
declare an `acceptanceThreshold` such as `80%`; without a threshold, an
incomplete score requires an operator decision. Defaults are `flexible`,
`sequential`, and no automatic threshold.

The helper deliberately parses only flat `key: value` string properties. Values
may be unquoted, single-quoted, or JSON-style double-quoted. `description` alone
also accepts a literal `description: |` block with space-indented content.
Nested mappings, sequences, other multiline forms, anchors, and tags are
rejected; no external YAML library is required.

Ordering is independent from acceptance. Arbitrary-order execution retains one
current ledger step while allowing any actionable step to be selected. The
`step --retry` command revisits a previously assessed step without deleting its
earlier evidence, and attempt numbers preserve the full progression.

Every new session records structured per-step results and natural scores. The
final command names every expected step, ensuring that `always` acceptance and
operator decisions cannot hide an unevaluated step. Percentages are derived by
the helper and exact halves round upward.

Registering a path that already has an unregistered session preserves its
recorded progress under the new registered ID. Adding a frontmatter `id` to an
unregistered runbook performs the same safe migration and then invokes the
normal changed-revision decision gate.

Document frontmatter is the live source for `description` unless registration
explicitly supplies an override. Existing registry descriptions remain
overrides for compatibility.

If a runbook changes during an unfinished session, the skill pauses for an
explicit choice between continuing with saved progress and archiving it to
start over. External mutations such as pushing, tagging, publishing, or merging
still require explicit authorization.

Archived ledgers are stored under `.runbooks/archive/` and retained
indefinitely by default. The skill never removes evidence during ordinary
execution. Use the explicit `prune` command to preview or remove archives for
one runbook by age or by the number of newest snapshots to keep.

## Usage

The dependency-free helper requires Python 3.14 or newer.

Model selection is agent-agnostic. The executing agent maps the runbook's
semantic profile to its own available models and session capabilities. The
skill contains no provider, scheduler, API, or model catalog integration. The
helper records the selected `modelId` and requires that same actual current
model ID on every progress command.
Read the [agent-agnostic execution contract](./references/model-execution.md)
for dynamic resolution and failure behavior.

When the current model does not satisfy the runbook selection, execution stops
until the operator decides what to do. After the operator explicitly approves
running on the selected model, the initiating agent uses its runtime's native
model-session or delegation facility to start that model context. Where the
runtime supports it, the initiating agent stays in the current conversation
and proxies the target context's questions, operator answers, evidence, and
results. The target context proves its own model identity and is the only
context that creates or resumes the ledger. This handoff contract does not add
an agent-specific integration to the skill.

`run` records `modelFamily`, `modelId`, tier and effort in the local ledger.
Resume and every step mutation compare `--model-id` with the recorded ID;
mismatches stop without creating another session. After an explicit operator
instruction, `rebind --model-family <family> --model-id <actual-current-model>`
updates the same ledger and preserves its current step, history, decisions,
tier, effort, and runbook revision. Registry updates and path migrations
preserve the binding. `list` reads current document defaults, while `status`
displays recorded values from any session.

Registry and ledger schemas are version 2. Version 1 files are rejected without
automatic migration or invented model evidence. Execution properties in the
registry are rejected; put them in Markdown frontmatter. For a new session, an
explicit operator request can override `modelFamily` using `run
--model-family <family>`. The helper records `modelFamily`, `modelId`,
`modelTier`, and `effortLevel`.
Existing sessions retain their binding after restart, revision decisions, and
document edits unless the operator explicitly invokes `rebind`. Starting from
the first step is reserved for an explicit operator instruction: use
`--restart` for the same revision or `--ignore` after a changed-revision prompt.

Invoke the skill with a request such as:

```text
Use $execute-guided-runbook to run the release runbook.
```

See [SKILL.md](./SKILL.md) for the complete agent instructions and the
[runbook registry schema](./references/runbook-registry.schema.json) for the
optional registry format.
