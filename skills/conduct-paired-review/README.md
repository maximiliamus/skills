# Conduct Paired Review

Review a source file or specified project functionality with two models acting
as author and critic. They exchange Markdown replies and agree on improvements.
The starting file can be a proposal, a description, a file from the project or
an initial review. Both models work from the project root and read relevant
context directly.

There are two goals: **refine the original intent** and **improve existing
functionality**. The skill derives the goal from your initial request and context.
If the intended goal or result is unclear, it asks you before starting.

```text
$conduct-paired-review "<absolute-path>/proposal.md"
```

For `proposal.md`, the discussion creates only:

```text
proposal-R01C.md
proposal-R02A.md
proposal-R03C.md
proposal-RP.md
```

Names preserve the original spelling: `AUDIT.md` produces `AUDIT-R01C.md` and
`AUDIT-RP.md`.

The critic starts a fresh review. Each model then answers its opponent. The
plan lists concrete changes, unresolved questions and acceptance checks.
Both models must approve it. Four replies without reducing unresolved questions
stop the discussion as `stalled`; continuing progress has no fixed round limit.

The author maintains the plan, and the critic critically checks the findings and
proposed improvements. The coordinator passes their turns and materials without
reviewing or editing their responses. Messages stay focused on the substance,
without filler or unnecessary repetition. Participants choose the detail needed
to resolve the question; there are no length targets or mandatory repetitions.

Both models can investigate the system to understand the source and verify facts.
They record significant findings, evidence and implications in their replies,
expanding the context available to subsequent turns and resumed discussions.
When an answer cannot be established, they say so and identify hypotheses or
questions deferred until clarification.

The source keeps its original name and remains separate from the numbered replies.
The replies and plan are the complete discussion record. Reuse the source or plan
path to continue.

The final chat response links to the original document, every dialogue reply in
turn order, and the review plan, using their actual filenames.

## Skill Configuration

Run these commands in the repository where you use the skill:

```text
$conduct-paired-review init
$conduct-paired-review update
```

Settings live in the repository-root `conduct-paired-review.json`, outside the
installed skill. `init` creates it from the bundled [template](config.json) if
it is missing. `update` validates the existing file and preserves your values.
Commit it and edit it directly or ask an agent to change it. When no changes are
needed, the skill replies `nothing to do`.

Defaults select Codex as author and Claude as critic. Antigravity is also supported,
using its own client and Gemini model selection. Override either role in the prompt
or change the defaults in the settings. Ongoing discussions keep the participants
recorded in their plan. See [configuration details](references/protocol.md#configuration).

Antigravity headless runs prepare scoped access for the current session where
supported. Empty or permission-blocked responses leave the turn pending.
See [runtime guidance](references/runtime-execution.md#antigravity).

## Apply Or Clean

```text
$conduct-paired-review "<absolute-path>/proposal.md" apply
$conduct-paired-review "<absolute-path>/proposal-RP.md" apply
$conduct-paired-review "<absolute-path>/proposal.md" clean
```

Pass either the proposal or its plan. For `proposal.md`, the skill finds
`proposal-RP.md` beside it and checks that the plan exists before applying it.
A bare filename such as `proposal.md` is resolved from the current working
directory. If the plan is missing, the skill reports this and stops.

`apply` accepts the agreed plan and asks the author to update only the original
document. For a proposal, it applies document revisions. For a functionality
discussion, it transfers the agreed steps into the initial review as an accepted
improvement plan. The operator implements those steps in their own workflow.
The plan uses `agreed` once both participants confirm it and `applied` after
the author verifies the document update. The discussion and plan remain afterward.

Only a separate `clean` command removes the replies and plan, either afterward
or when you abandon the plan. It preserves the original document, including an
initial functionality review and its accepted plan. If nothing remains to delete,
it replies `nothing to do`.

## Start With Project Functionality

Name the functionality to review and omit the starting file.

```text
$conduct-paired-review Review runbook history retention in this project
```

The critic first investigates the requested functionality and saves its report
as an original source file such as `runbook-history-review.md` at the project
root. You can specify a different report path. The author then writes
`runbook-history-review-R01A.md` and creates the plan beside the report. The
critic's next reply is `runbook-history-review-R02C.md`.
See [the functionality workflow](references/functionality.md#start-with-project-functionality).

## Start From An Existing Review

Identify the source as an existing review and state your intended goal. A marker
in its filename can identify the writer's role. The opponent replies first.
Without a marker, specify the role in the prompt. For example, "This initial
review is from the author."
The skill keeps `checkout.md` unchanged and asks the critic to create
`checkout-R01C.md`. The source review is not a numbered dialogue turn.

If the prompt conflicts with the marker, the skill asks for clarification before
changing files. When the goal is to improve existing functionality, the plan
develops and agrees on the necessary improvements or reconsiderations. `apply`
then incorporates them into the initial review for the operator's future work.
It does not change the system. See [the functionality workflow](references/functionality.md).

Read [SKILL.md](SKILL.md) for the agent workflow. The optional filename and
configuration helper requires Python 3.14. The [smoke claim](assets/smoke-claim.md)
contains a deliberately incorrect total for a small review exercise. Automated
tests cover file operations and settings; they do not establish model behavior.
