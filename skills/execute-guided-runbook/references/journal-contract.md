# Guided runbook execution contract v5

The coordinator schedules work through its current harness, verifies facts and
submits them to the local helper. The helper serializes journal changes and
checks the recorded facts against the current run. Choose ordinary native
worker handles and observations that establish the relevant facts.

## Commands and storage

Use `runbook_session.py [--repo-root PATH] COMMAND`. `reserve SELECTOR` and
`change RUN_ID` read one UTF-8 JSON object from stdin. Commands return JSON on
stdout. `init`, `update`, `list`, `register ID PATH` and `unregister ID` retain
their discovery contract with `ok`, `changed` and `runbooks`.

A selector is a registered ID or repository-relative Markdown path. Both can
locate the same open run. `status RUN_ID` reads any run identified by UUID,
including a finished run. Active runs are also available with `status SELECTOR`.
Status exposes current steps, their assessments, blockers and attempt history,
available slots, ready steps, pause and question state, unsettled work and
publication intents. It includes the frozen procedure under
`contract.instructions`. Status omits ownership tokens.

Errors have `ok: false`, `code`, `message` and `details`. Exit status is 0 on
success, 1 for `INVALID_REQUEST`, `NOT_FOUND`, `UNSUPPORTED_SCHEMA` or
`IO_ERROR`, and 2 for `CONFLICT`, `BLOCKED` or `DECISION_REQUIRED`.

Keep the root registry schema and place journals under
`.runbooks/runs/<runId>.json`. Use a repository-local OS lock for readers and
writers and atomic journal replacement. New reservations use the
[v5 schema](runbook-journal-v5.schema.json). The helper reads and continues
[v4 journals](runbook-journal-v4.schema.json) in their original format and
checks both versions for open runs and unresolved publication intents.

## Reserve and change

Reserve with a hyphenated UUID `runId`. The helper normalizes UUIDs to
lowercase. Before creating an active run it freezes the executable instructions,
settings, step order, graph and output paths. Optional `stepContracts` maps
step IDs to checked procedure facts. Supported facts are `gapCauses`, `reuse`,
`reassessAfterInvalidation`, positive integer `points` and supporting
`procedure` citations. A citation has `path`, `sha256` and `section`.
Supply `modelFamily` with `overrideDecision` for an authorized initial operator
selection. Both reservation and takeover return an opaque `ownershipToken`.

Each change includes UUID `requestId`, positive integer `ifRevision`, `type`
and object `data`. Every change except `takeover` requires the current
`ownershipToken`. The helper checks ownership before replay and checks
`ifRevision` for fresh requests. Identical replays report `duplicate: true`,
the original `committedRevision` and the current projection. A mismatched
payload conflicts. A reservation or takeover replay succeeds only while
its original tenure is current.

In v5, a single-slot `sequential` procedure without explicit dependencies
follows the frozen Markdown order. Only the first step without a current
terminal assessment can be admitted or assessed. A parked question, a
settled attempt with no assessment and a stale assessment all hold this
position. A manually authorized retry still respects it. An explicit graph
uses its own dependencies. Multiple slots allow any ready step in the graph
within the available capacity.

A `done` dependency requires an applicable terminal assessment. A `passed`
dependency additionally requires verified success. In newly reserved
contracts, the input spelling `assessed` normalizes to `done`. Send the exact
current assessment event for each declared dependency. Steps ordered only
by Markdown need no dependency references.

An `evidence` object contains nonempty `statement` and `reference`.
Record the actual observation and a reference to the returned result, command
exit, operator confirmation or other relevant evidence. A `decision` contains
`instruction` and `reference` to the operator's authorization. Flexible
finalization decisions also include `choice` of `accept` or `reject`.

### Unmarked sequential procedures

Top-level step markers declare the inventory when present. For a procedure
with no step markers, one slot, sequential order and no dependency graph,
the coordinator can supply `stepInventory` in the reservation. Use the
SHA-256 of the exact source bytes and one entry per explicit step, in the
order written. Each entry contains a lowercase kebab-case `id` and the
1-based `startLine` in the whole file, including frontmatter and blank lines.

```json
{
  "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "steps": [
    {"id": "check-inputs", "startLine": 3},
    {"id": "record-findings", "startLine": 7}
  ]
}
```

Anchor each step at a top-level ATX heading below level one or a top-level
ordered list item. A heading step includes its subsections up to the next
heading at the same or a higher level. A list item includes its content up
to the next top-level list item or heading. The coordinator selects the
intended steps and reports a startup error if the full sequence is unclear.
The helper checks anchors, unique IDs, order and non-overlapping ranges.
It rejects a missing or invalid inventory and inventories that would replace
marked steps. A changed source fingerprint yields `CONFLICT`.

The helper freezes the same step and instruction records used for marked
procedures. Read those records on resume. Reservation replays use the
original request, including the inventory when supplied.

## Changes

- `takeover` carries `evidence` that the former coordinator cannot issue any
  more work, including previously prepared sends. It rotates local ownership
  at the given revision. Reconcile previously handed-off attempts separately.
- `admit` carries `stepId`, `execution` (`inline` or `delegated`), `sources`
  as an array of `{path, sha256}` fingerprints, and `prerequisites` mapping
  each required ID to its current assessment event. Provide empty containers
  when there are no such references. For a shared-state effect, supply
  `mutation` with `target`, `preconditions` and `verify`. A manual retry can
  carry `decision`. Provide model facts when required. Admission records a
  numbered attempt and charges it once. Send to the harness only from a
  fresh admission. Live shared-state work is serialized with other attempts.
- `observe` carries `attempt` as its admission event number and `kind`. Use
  `task` with `taskId`, `question` with `text`, or `lost-contact`, `no-start`
  or `stopped` with `evidence`. A `technical-failure` carries `reason`,
  `transient` and evidence that the worker and commands ended. A `result`
  carries verified `outcome` (`PASSED` or `FAILED`), stop and result `evidence`,
  optional `reason` and `score` (`earned` and `available`) and required
  `deliverables`. The helper uses the evidence statement when the reason is
  omitted. It accepts `PASS` and `FAIL` as input aliases and stores the
  canonical outcome in v5. It can include observed `model` facts. Stop evidence
  can settle superseded work. Superseded or inapplicable outcomes remain
  historical.
- `resolve` with `kind: question` takes a question event as `hold` and
  `decision` containing the answer. With `kind: attempt` it takes `attempt`
  and verified stop `evidence`. Reconcile a shared-state effect with
  `effects` of `no-effect` or `safe-repeat` and an operator `decision` when
  new authority is needed. To recover an applicable original outcome, verify
  it and submit the result on that attempt. Publication recovery is below.
- `set-pause` takes `requested: true` and a pause `decision` or run-wide
  question `text`. With `requested: false`, include an operator `decision`
  and resolve the run-wide question first. Parked local questions can
  survive pause and resume.
- `invalidate` takes a `cause` and affected `steps` or `references`, or both.
  References use the previously recorded `{path, sha256}`. The helper
  withdraws the affected assessments and supersedes their attempts and
  transitive dependents together.
- `assess` records an authorized `gap` or verified `reuse` for `stepId`.
  A gap takes `cause` and its applicable `trigger` event. Reuse takes
  `source` with a supported `runId` and `event`, receiving
  `sources` and `prerequisites`, equivalence `evidence` and any staged
  `deliverables`. Verify equivalence under the receiving procedure's policy.
- `override` with `kind: budget` takes `stepId`, a higher `limit` and
  `decision`. With `kind: model` it takes `modelFamily`, optional `stepId`
  and `decision`. Future attempts use the override within its scope. Changes
  to frozen executable instructions need a new run.
- `close` with `mode: finish` verifies all current assessments and artifacts.
  With `mode: abandon` it requires a `decision` and settled active work.
  Reconcile outstanding publication intents before closing either way.

## Native dispatch and models

Use `modelFamily` selectors `current`, `frontier`, `predecessor`,
`family:<version>` and `exact:<model-id>`. The exact suffix stays opaque.
Per-step settings take precedence over an operator run-wide choice, followed
by runbook settings and defaults. Older attempts retain recorded provenance.

A harness default can run with sparse metadata. The optional admission `model`
object takes supported facts from `modelId`, `modelFamily`, `modelTier` and
`effortLevel`. A non-default family needs a confirmed selected model, and
`exact` needs the identified model matching its suffix. Explicit tier and
effort levels need evidence of the required level or above. Supply
`execution: delegated` only when the harness provides the required fresh
worker. Report unmet requirements and leave the step pending.

On native handoff uncertainty, record lost contact and keep counting the
attempt until there is verified no-start or settlement. A definitive no-start
releases the charge and slot. A paused run is still `pausing` while any
handed-off worker or command may be running. Ordinary stop evidence or an
operator confirmation identifying the stopped attempt can settle unreachable
work. A parked question releases its slot and still holds its step.
The default single attempt can be exhausted by an ambiguous interruption.

A local question holds its step and dependents that need its result. Questions
can be answered while a run drains. When their workers and commands settle,
parked questions release capacity and can remain pending through a pause.

On resume, retain each current `done` assessment. Restart safe unfinished work
within the remaining budget once an earlier attempt cannot interfere. Isolate
attempt outputs when an older worker might return late. For a shared-state
or external effect, inspect the original target and its preconditions before
repeating the action. Recover a verified applicable result on its original
attempt. Keep the step pending while an effect or interference is uncertain.
Automatic retries within the same invocation apply to transient technical
failures. An operator can authorize a manual retry or a higher budget.

### Evidence applicability

An assessment records input fingerprints, exact prerequisite assessment
events and output digests. If an input or a prerequisite assessment changes,
invalidate all affected results and scores, including transitive dependents.
This applies when a replacement prerequisite has the same verdict. A late
observation can prove an old attempt stopped. Its superseded result stays in
history and cannot satisfy dependencies or change the score.

For reuse, verify the governing instructions, inputs, prerequisite assessments
and required outputs under the procedure's source policy. Record the receiving
run's current prerequisite events and the reused source assessment. Verified
reuse can complete a step without using another attempt. Check required output
digests at the receiving run's declared paths, including paths expanded with
its run ID.

## Publish and recover

Declare each output with a top-level `<!-- deliverable: path -->` inside its
step. The helper expands `{runId}` and rejects redirected, colliding and
overlapping targets. For a current result or reuse, submit one entry per
declared path with the following fields.

- `path` is the declared target.
- `stagedPath` points beneath `.runbooks/staging/<runId>/<attempt>`. Use
  `.runbooks/staging/<runId>/reuse` for reuse.
- `sha256` is the verified staged digest.
- `priorSha256` is the expected target digest, or `null` for a new target.
  Include `replacement` evidence of authority when replacing existing content.

The helper verifies staged bytes and prior state, records a publication intent,
copies and verifies temporary files in their target directories, replaces the
targets, and commits the checked outputs with `done`. Staged bytes stay
available for recovery. An unresolved intent blocks closure and overlapping
publication across runs.

Use `resolve` with `kind: publication` and the `intent` event. With
`disposition: complete`, supply reconciliation `evidence`. The helper checks
applicability, digests and target states under the same lock before completing.
Invalidation makes an affected intent stale. When already written bytes are
accounted for, use `disposition: accounted`, `evidence` and an explicit
`decision`. For an observation intent also record `effects` of `no-effect`
or `safe-repeat`. Stale work cannot establish `done`.

## Final assessment

By default every step is worth one point. Under `strict` one point per step
is required. Weighted procedures record positive integer step weights at
reservation. A verified negative outcome or authorized gap can be `done`.
A gap cannot fulfill an unresolved mandatory output. Close requires current
verified assessments and all mandatory artifacts.

A current v5 assessment records its terminal `outcome`, `reason` and
`evidence`. The step's progress remains `pending`, `active` or `done`. Use
these terminal outcomes:

- `PASSED` records a current verified success, including authorized reuse.
- `FAILED` records a current verified negative result, including authorized
  reuse.
- `SKIPPED` records an authorized `failed-prerequisite` gap for a step that
  never started. The trigger is a current non-passing assessment on a
  `passed` dependency. The gap retains that event as a prerequisite.
- `EXHAUSTED` records an authorized `exhausted` gap after the latest charged
  attempt settled without a result and used the remaining budget. The
  trigger is the settlement event.
- `INVALIDATED` records an authorized `invalidated` gap after affected work
  and effects are settled and stale publications reconciled. The trigger is
  the invalidation requiring assessment in a separate run. Invalidation
  alone withdraws the old assessment.

Gaps record zero points, spend no attempt and cite the recorded trigger
event as evidence. Required outputs still need verification. An open
question or an unrecorded result leaves the step unfinished. A superseded
result stays historical. A continued v4 run retains its original
`PASS`, `FAIL` and `NOT_ASSESSED` outcomes and scheduling rules.

Full credit yields `PASSED`. Deficient `strict` yields `REJECTED` and
`always` yields `ACCEPTED`. For `flexible`, use integer percentages from
`1%` through `100%` and compare `earned * 100 >= available * percentage`.
Below the threshold or with no threshold, record the operator's accept or
reject decision.

### Example consumer profile

An audit can use one preparation step, nine independently scheduled checks
that require preparation to pass, and one report step with `done` dependencies
on all ten earlier steps. With `always` and one point per step, preparation recorded
as failed or exhausted can leave the nine checks as procedure-authorized gaps.
A verified report yields `ACCEPTED` at `1/11`. A missing or stale report leaves
the run incomplete. The consumer owns its checks, report contents, evidence
policy and downstream decisions.
