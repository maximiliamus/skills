---
name: execute-guided-runbook
description: Run or resume a Markdown repository runbook with native parallel workers, verified completion and a durable pause. Also manages runbook discovery and project configuration.
---

# Execute Guided Runbook

The helper is at `scripts/runbook_session.py` under the installed skill.
Invoke it by its absolute path from the target repository or pass
`--repo-root PATH` before the command. `reserve SELECTOR` creates a run and
`change RUN_ID` records operations on it. Both take one UTF-8 JSON object on
stdin. Read the [execution contract](references/journal-contract.md) for request
shapes. The procedure and operator authorize actions, outputs and recovery.

## Set up or select a run

- Run the helper's `init` to initialize the project's
  `execute-guided-runbook.json` or `update` to validate it, then return.
  Preserve project settings.
  Use `list`, `register ID PATH` and `unregister ID` for discovery.
- For execution, read the governing repository instructions. Call
  `status SELECTOR` with the registered ID or repository-relative Markdown
  path for an ordinary request. It selects the latest run by
  reservation order. If there is no saved run or the latest is closed, prepare
  a new independent run. If another unparked run blocks reservation, report
  its UUID and respect its owner. Select an older run only when the operator
  names its UUID.
- For an open run, show its status. Use harness evidence or operator
  confirmation to identify a pause or interruption. Treat uncertain work as
  live until reconciled. Elapsed time alone provides no stop evidence.
  For a confirmed pause or interruption, report the runbook, UUID, start time,
  confirmed pause time or last recorded activity, last affected step,
  completed outcomes and outstanding questions, attempts, effects or
  publications. State the first continuation action and identify any unknown
  interruption time or cause.
- Offer to continue the latest paused or interrupted run or start a new one.
  Wait for the operator's choice. For explicit continuation, read
  `status RUN_ID` and use that open journal. Offer execution continuation
  only when `sourceMatches` is true. Otherwise offer to restore the exact
  original Markdown bytes or obtain a decision to start with an available
  procedure. Resume using the frozen instructions and scheduling rules.

## Recover ownership and settle previous work

The current owner can reconcile work with its token. An incoming coordinator
confirms the previous owner cannot issue further work, including prepared
handoffs, and submits that evidence in `takeover` at the current revision.
Use `recoveryOnly: true` for settlement with changed or unavailable Markdown.
Continuing work requires matching source and execution ownership. A parked
run has yielded ownership. To reclaim it by UUID, use a fresh execution
takeover with matching source and evidence fencing the former owner. Any
other unparked open run of that procedure must be closed or settled and paused.

Account for live attempts, shared effects and publication intents before
switching runs. After verified settlement, recover an applicable verified
outcome on its original attempt when possible. For uncertain effects,
inspect the original target and preconditions and record the disposition
through `resolve`. Report the specific blocker if safe settlement cannot be
established. For an older abandoned run with pending effects, select its UUID,
use recovery-only takeover and resolve the original attempt with verified
evidence and an explicit effect disposition. The run stays abandoned.

To supersede the latest open run, include its UUID, post-settlement revision,
the operator's decision and evidence that the old coordinator can issue no
more work in the new reservation's `restart`. A settled predecessor can be
superseded directly. An incoming owner needs a takeover first when it must
settle the predecessor.

If a reservation or takeover reply is uncertain, replay the identical request
while its tenure is current to recover the ownership token. `status` omits the
token. Keep it in the coordinator context and out of worker prompts. For other
uncertain changes, inspect the current projection or replay the identical
request. See
[Reservation and change](references/journal-contract.md#reserve-and-change).

## Prepare a new reservation

Use the procedure's top-level step markers and IDs when present. Without
markers, accept only a one-slot sequential procedure without an explicit
dependency graph. Supply the complete ordered `stepInventory` with the
exact Markdown SHA-256 and documented
[step anchors](references/journal-contract.md#unmarked-sequential-procedures).
Exclude preamble, notes and examples. Stop with a startup error for malformed
markers or a missing or ambiguous step.

Read the whole procedure, including operative shared prose. Identify each
step's mandatory named outputs. Frontmatter, input references, incidental
mentions, optional outputs, quotations and examples carry no requirement.
Include only applicable conditional branches. If a runtime condition leaves
the required path or producing step uncertain, resolve it with a clarified
procedure or operator decision before reservation.

Reserve a fresh UUID with the verified procedure facts. Submit the complete
[output inventory](references/journal-contract.md#named-outputs-in-prose)
for each step, including outputs assigned in shared prose. Use an empty list
when a step has no required prose outputs. Supply applicable gap causes,
reuse and reassessment permissions, and nondefault step weights with their
supporting procedure citations. For an initial model override, supply the
model family and the documented operator decision.
Compare the frozen outputs returned by the helper with the intended set,
including existing deliverable markers. On mismatch, stop before dispatch.
Obtain authority to abandon or restart and reserve the corrected contract.
Clarify the governing text under appropriate authority when its unconditional
instructions themselves declare the unwanted output.

## Admit work and publish deliverables

1. Use `status` to check readiness, inputs, dependencies, permissions, slots
   and retry safety. Run independent ready steps in parallel within capacity.
   In v5, a one-slot sequential run without a graph waits for the first
   Markdown step without a current terminal assessment, including a marked
   step. With a graph, normally choose the first ready step for single-slot
   sequential work. Follow the original scheduling and outcome rules for a
   continued v4 run.
2. Choose inline execution when permitted or a fresh delegated worker when
   required. Establish the required model facts for either execution mode.
   If the harness cannot meet a requirement, leave the step pending and state
   why. Serialize pause handling and dispatch. Just before starting each
   attempt, `admit` the material repository-file input paths and SHA-256
   digests, the exact current prerequisite assessment events, and the
   target, preconditions and verification for any shared or external mutation.
   The helper checks mutation interference within the run.
3. Start work only on a fresh admission. A duplicate admission never
   authorizes another send. Record uncertain starts as `lost-contact`.
   Verified settlement releases the slot. Only definitive `no-start`
   refunds the attempt. Before retrying, record the confirmed no-start or
   settlement and check effects and the remaining budget. A non-transient
   retry or budget increase requires operator authority. See
   [dispatch](references/journal-contract.md#native-dispatch-and-models).
4. Give the worker the frozen instructions, verified inputs and allowed
   actions. Tell it to write private files under `artifactsDirectory` when
   present and stage mandatory outputs only under
   `<stagingDirectory>/<attempt>/`, using the admission event number. For reuse,
   stage under `<stagingDirectory>/reuse/`. Flat v4 and v5 journals have a null
   `artifactsDirectory` and use their returned staging directory. Only the
   helper publishes mandatory outputs to the frozen targets. Ask workers to
   report once their commands have ended.
5. Verify that workers and their commands have ended. Verify the staged
   bytes, expected prior target state and any replacement authority. Record
   the verified result through `observe` with its deliverables. A v4 result uses
   `PASS` or `FAIL` and omits `reason`. For unresolved publication, inspect the
   intent and use `resolve` with the applicable publication disposition,
   evidence and any required decision. See
   [publication](references/journal-contract.md#publish-and-recover).

The project registry sets retention for run folders, with per-runbook
overrides. Eligible older folders rotate only after a finished run is saved.
An explicit external destination such as `repo:reports/report.md` follows
the lifecycle stated in the procedure.

## Pause and resume

A worker-local question holds its step and any dependent work. Record it as
an `observe` question, then answer the recorded hold through `resolve`,
including during drain. The attempt holds a slot until the worker and
commands settle. A parked question still holds its step and can survive
pause and resume.

For an operator pause or a run-wide question, stop dispatch and request
`set-pause` with `requested: true` and the operator's decision or the
question text, respectively. Record worker and command settlement or
definitive no-start, then read the derived `paused` phase. Answer a run-wide
question through `resolve`, then use `set-pause` with `requested: false` and
a separate operator decision to lift the settled pause.
See [pause and resolution requests](references/journal-contract.md#changes).

## Assess and finish

Preserve applicable assessments. If an input or prerequisite changes,
use `invalidate` with the cause and affected step IDs or recorded file
references. Use `assess` for a procedure-authorized gap with its recorded
trigger or for verified equivalent reuse. For reuse, verify the governing
instructions, inputs, prerequisite assessments and required outputs. A
verified negative outcome can be `done`. A gap cannot satisfy a mandatory
output. Use `override` for an authorized budget or model change.

Before either `close` mode, settle live work, shared effects and publication
intents. Abandonment requires the operator's decision. Every `finish`,
including a rejection under strict scoring, requires resolved questions,
current assessments and verified published bytes for all mandatory outputs.
For a deficient flexible score below the configured threshold, or with no
threshold, obtain the operator's accept or reject decision. Report the
returned result, score, deliverables and any limitations. If a completed
finish reports `cleanupPending`, replay the identical finish to retry physical
cleanup. Report any cleanup still pending without repeating step work.
See [final assessment](references/journal-contract.md#final-assessment).
