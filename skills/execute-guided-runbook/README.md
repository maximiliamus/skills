# Execute Guided Runbook

`execute-guided-runbook` carries out Markdown runbooks as resumable, verified
workflows. It exposes one unresolved step at a time and records progress in a
local `.runbooks/` ledger, allowing a later turn or agent session to continue
from saved evidence.

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

Invoke the skill with a request such as:

```text
Use $execute-guided-runbook to run the release runbook.
```

See [SKILL.md](./SKILL.md) for the complete agent instructions and the
[runbook registry schema](./references/runbook-registry.schema.json) for the
optional registry format.
