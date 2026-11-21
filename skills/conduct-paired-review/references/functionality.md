# Starting From An Existing Review

The initial prompt determines the goal: refine the original intent or improve
existing functionality described in the original document. A prepared review may
be the source for either goal.
If the intended goal or result is unclear, ask the operator and wait.

For improving existing functionality, examine the system and develop agreed steps
for improving or reconsidering its behavior. The initial review is the document
that will receive those conclusions and the accepted improvement plan. System
implementation belongs to the operator's separate workflow.

## Initial Role And Files

The source keeps its original filename and contents during discussion. Do not
rename it, copy it into a numbered reply, or count it as the first dialogue turn.
The writer's opponent starts the new dialogue at `R01`.

| Prompt role | Filename role marker, if present | First new reply |
| --- | --- | --- |
| `author` | Absent or `A` | Critic writes `R01C` |
| `critic` | Absent or `C` | Author writes `R01A` |
| Unspecified | `A` or `C` | The opposite role |
| Conflicts | `A` or `C` | Ask the operator and wait |
| Unspecified | Absent | Ask the operator and wait |

Use the prompt, explicit conversation context or a filename marker to identify
the writer's role. Begin at `R01` regardless of the number in the original
filename. On conflict, leave all files unchanged until the operator clarifies.

For an author's initial review named `checkout.md`, keep `checkout.md`, have the
critic write `checkout-R01C.md`, and then have the author write `checkout-R02A.md`.
The shared plan is `checkout-RP.md`. For a critic's initial review, the author
starts with `checkout-R01A.md`. Use the original filename's full stem for the
new artifact family, subject to the shared portable-name rules.

The optional `inspect-review` helper checks the writer's role and suggests paths.
See [the helper contract](protocol.md#filename-helper).
If the source review is a conversation response, save its exact text once under
a clear filename; that becomes the original document. An existing source file
already has its name and location. Do not relocate it to establish a discussion.

## Discussion And Plan

Read applicable repository instructions and relevant code, requirements and tests.
Record the goal, intended outcome and a link to the original file in the plan's
Goal section. Verify the first review's findings against evidence instead of
assuming they are correct. The discussion must address the actual intent or
functionality, not merely improve the review's wording.

Both opponents may investigate the system for context and missing facts. Each
records new significant discoveries and their evidence in its own reply, with
their effect on the findings or proposed steps. Later turns cite that evidence
without repeating it. Scale detail to the actual uncertainty and consequences;
a resolved issue needs no new full analysis. Resumed reviews use this accumulated
written context; tool output alone is not a durable finding.

The author maintains the plan, and the critic critically reviews it. For existing
functionality, the paired-review plan develops the accepted steps
for improving it: changes to propose, decisions to revisit, rejected findings
with reasons, dependencies and future acceptance checks. Both participants must
approve these steps and conclusions. The initial review itself is not approval.

For example, a duplicate-payment finding might produce this agreed task:

- Add an idempotency step to the accepted improvement plan: repeated requests
  with the same key should return the existing payment. Explain the failure and
  rationale in the review. Include a future repeated-request test as the acceptance
  check. During `apply`, verify that the review contains this agreed step and
  check; implementing the change and running that new test belong to later work.

Resume from the replies and plan, retaining the stated goal and participants.

## Apply And Clean

`checkout.md apply` and `checkout-RP.md apply` select the same paired-review plan.
Check that the plan exists and is agreed. The author then updates only `checkout.md`:
reconcile findings and transfer the agreed steps into its Accepted improvement
plan section, preserving their meaning, rationale, dependencies and checks.
Keep those implementation steps pending for the operator's own workflow.

Set the paired-review plan to `applied` once the transfer and document checks are
complete. This records a document update, not completed system improvements.
Neither the initial review nor later dialogue replies are renamed. Retain all
files after application. No code or tests are changed by this operation.

A separate `clean` removes the generated dialogue replies and paired-review plan.
It preserves `checkout.md`, including the accepted improvement plan, whether the
discussion was applied or abandoned.
