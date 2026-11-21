---
name: execute-guided-runbook
description: Run or resume a Markdown repository runbook with native parallel workers, verified completion and a durable pause. Also manages runbook discovery and project configuration.
---

# Execute Guided Runbook

Use `scripts/runbook_session.py` in the target repository. Read the
[execution contract](references/journal-contract.md) before operating the helper.
The procedure and operator set the authority for actions, outputs and recovery.

## Resolve the request

- For `init`, initialize the project's `execute-guided-runbook.json` and return.
  For `update`, validate that file and return. Preserve project settings.
- Use `list`, `register ID PATH` and `unregister ID` for discovery. For execution,
  select a registered ID or a repository-relative Markdown path.
- Read the procedure and governing sources. Check `status` before reserving.
  Continue an open v4 run under its saved rules. For older work that is
  still needed, reconcile effects and start a fresh run. See
  [Upgrade](README.md#upgrade).
- For a new run, read the procedure and use its top-level step markers when
  present. If there are none, read the explicit steps of the simple sequential
  procedure in document order. Identify all steps from their heading or
  ordered list item, giving each an ID and its source line. Exclude preamble,
  notes and examples. Stop with a startup error identifying the missing or
  ambiguous step before handing off any work. Report malformed markers.
- Call `reserve` with a fresh run UUID and verified procedure facts. For the
  unmarked sequence, supply `stepInventory` with the SHA-256 of the exact
  Markdown bytes read and the ordered step anchors, as described in the
  execution contract. Retain the returned local ownership token in the
  coordinator context. Use the journal's frozen steps and instructions on
  resume. If ownership changes, record evidence the former coordinator cannot
  issue further work before `takeover`.

## Coordinate execution

1. Read ready steps from `status` and check their inputs, dependencies,
   permissions, available slots and retry safety. Use the current harness to
   start independent steps in parallel within the configured limit. For a
   simple sequential procedure, wait for the first unfinished Markdown step's
   current terminal outcome. Give each worker the exact instructions,
   inputs, allowed actions and expected outputs. Required fresh delegation or
   explicit model requirements that the harness cannot meet leave the step
   pending with a concrete explanation.
2. Process a pause and initiate native handoffs on one serial path. Call `admit`
   immediately before each handoff. Send only on a fresh admission. For a
   definitive no-start, record it, then admit afresh when eligible. An
   ambiguous start retains its slot and attempt charge until reconciled.
3. Ask workers to wait for their commands to finish before returning. Verify the
   result and required artifacts, then record it with `observe`. A current
   verified negative result can be `done`. Stage mandatory outputs at the
   documented run and attempt paths so the helper can publish them and commit
   `done` under its lock.
4. Relay local questions and record answers through `resolve`, including
   during drain. A live worker waiting for an answer holds its slot. A parked
   question remains a hold on its step after the worker and commands settle.
   Stop sending work on `set-pause`. Confirm `paused` when all handed-off work
   and commands ended or definitively never started.
5. Preserve applicable `done` results on resume. Recheck outstanding attempts,
   effects and publication intents before repeating work. Use evidence of
   actual settlement for unreachable work. Continue safe unfinished steps
   within budget, and report the specific blocker when an effect or worker
   cannot be accounted for. Invalidate old inputs and assessment references
   before reusing or replacing affected evidence.
6. Check every current assessment and mandatory deliverable before `close`.
   Record an explicit decision when the applicable policy requires one. Report
   the final assessment, score, deliverables and unresolved limitations.

After uncertain JSON delivery, consult `status` or replay the identical request.
The current owner can reconcile ambiguous handoffs from ordinary harness
observations. Keep the ownership token in the coordinator and out of worker
prompts.
