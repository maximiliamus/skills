# Files And Configuration

## Markdown Files

The source, numbered replies and plan are the full working context. For `claim.md`:

```text
claim-R01C.md
claim-R02A.md
claim-R03C.md
claim-RP.md
```

Reply example:

```markdown
---
model: concrete-model-id
status: reviewing
---

R-001: The operations are sequential, so three minutes plus three minutes is
six minutes. Change the stated total from five to six minutes.
```

Use `approved` only when explicitly accepting the whole current plan. Discuss
individual issues in the body when they need an answer. An approval must clearly
accept the current plan; repeating its reasoning or tasks is not required.
An existing review used as the source
keeps its original filename and contents during discussion; it need not have this
metadata and is not a dialogue turn or implicit approval.

Record new system findings in the response body with their sources and impact on
the plan. Use the current context or actual investigation for factual claims.
Say explicitly when an answer could not be established; distinguish a tentative
hypothesis from a question deferred until clarification. Identify the missing
evidence instead of inventing it. Carry decision-relevant unresolved uncertainty
into the plan. These are content requirements when applicable, not mandatory
headings. Refer to findings already recorded instead of copying them into each reply.

The author writes and updates the plan. Plan example:

```markdown
---
status: reviewing
---

# Revision plan

## Goal

Refine the original intent in [claim.md](claim.md): retain the sequential
operations and make their stated duration consistent.

## Participants

- author: codex, concrete-author-model
- critic: claude, concrete-critic-model

## Tasks

- T-001 (R-001): Replace five with six minutes in the total sentence: 3 + 3 = 6.
  Check: the total is correct and all other conditions remain unchanged.
```

Keep Goal and Participants for context and resumption. Other body sections are
optional: combine an issue and its task for a simple correction, and use separate
issue tracking when disagreements need it. Keep actual dependencies and reasons
for disputed decisions; omit empty sections and repeated approvals. The replies
record each participant's agreement, and the plan's status records its overall state.

The Goal section records either refining the original intent or improving existing
functionality, as established from the operator's prompt. State the expected
result and link the original document to be updated. If the goal is unclear,
ask the operator before starting; the file's type does not decide the goal.

For existing functionality, develop the agreed improvement steps in this plan.
`apply` transfers them to the initial review as its Accepted improvement plan,
alongside reconciled findings. Preserve the proposed changes, rationale,
dependencies and acceptance checks for the operator's later implementation.
Keep rejected issues with their reasons. Record evidence that the document was
updated under the corresponding tasks, without claiming the system was changed.

Plan statuses are `reviewing`, `agreed` (both models approve the current plan
and the operator's decision is pending), `approved` (the operator accepted),
`applied`, and `stalled`. Read `approving` in existing plans as the former
name for `agreed`. Write `agreed` when recording new consensus.
`applied` means the original document incorporates the agreed results.
Implementation described in that document requires its own authorization.
There are no other metadata fields or hidden state. Approval is established by
reading the actual responses and current plan; modifying the plan requires renewed
approval from both participants.

## Configuration

The bundled `config.json` is a template. The repository-root
`conduct-paired-review.json` belongs to the user and is committed with that
repository. Installing or updating a skill must never replace it.

From the repository where the skill is used:

```text
python <skill>/scripts/review_files.py init
python <skill>/scripts/review_files.py update
```

`init` creates settings only when absent; repeated initialization preserves the
existing file. `update` validates the current supported format and preserves
user values. `schemaVersion` identifies the format; there is no product-version
field. Schema 1 is the first format, so no conversion currently applies. Missing
settings require `init`; invalid or unsupported settings are reported without
rewriting them. When a later schema is introduced, implement an explicit update
that preserves user choices. Configuration commands do not edit review files.

The helper discovers the enclosing Git repository, otherwise the nearest ancestor
with the skill settings or a local `.agents/skills/conduct-paired-review/SKILL.md`
installation. If neither exists, use the nearest `AGENTS.md`, then the current
directory. Nested instruction files do not override an established repository.
An optional absolute directory or `--project` selects another repository. With a
global installation, keep the working directory in the repository where the skill
is used, rather than the installation folder.

The profile lists participants, runtime, model family, capability tier and
reasoning effort, the default author and critic, and `maxStagnantTurns` (4 replies
by default).
Select exactly two distinct models. Resolve `current`, `frontier`, `predecessor`,
`family:<name>` or
`exact:<model-id>` within that participant's runtime. Tiers are `light`, `medium`,
`heavy`; efforts are `low`, `medium`, `high`, `extra` (maximum supported).

A single role override selecting the other default participant swaps the pair;
a third participant replaces only that role. Two overrides must be distinct.
Write the selected pair in the plan. Resume from that plan rather than changing
participants because the settings changed. No configuration snapshot is needed.

## Filename Helper

Participants write Markdown replies; the author writes and updates the plan.
The coordinator can save their exact returned text on their behalf. The helper
handles filenames and role conflicts; it does not run the discussion or apply edits.
Its structured output goes to the caller, not to a persisted JSON dialogue file.
Here `<helper>` is the installed `scripts/review_files.py`.

```text
python <helper> files <absolute-source-path> --as-source
python <helper> files <absolute-plan-path>
python <helper> inspect-review <absolute-review-path> --author-role author
python <helper> clean <absolute-source-path> --as-source
```

`files` lists the exact numbered filename family and plan path without creating
anything. Ordinary safe Markdown stems retain their original spelling, including
uppercase and mixed case, in both generated names and plan-path lookup. Unsafe
or very long stems use a portable hashed name. Other file formats receive a suffix
that avoids collisions with a Markdown source of the same stem. Use returned names
consistently in replies and chat.
On Windows, another casing of an existing path uses the file's stored spelling
and the same review family. Distinct files on case-sensitive filesystems stay
separate.

If source-based lookup finds an earlier hashed family for a now-readable uppercase
stem, it returns `needs-clarification` before changing anything, even if a named
family also exists. An explicit existing plan path selects that family for
continuation. Do not merge, rename or overwrite history automatically; resolve
renaming with the operator. Once a family has been renamed consistently, lookup
from the original source finds its replies and plan under the original spelling.

Use `--as-source` for the original file, so an existing suffix such as `-RP.md`
is treated as part of its name. For a plan path, omit it. A numbered review file
used as the original source also keeps its full name; it is not stripped back to
another document. The plan's Goal identifies the original file when resuming.

`inspect-review` reads the original review writer's role from the prompt or a
filename marker and returns the opponent as `nextRole`, the new `firstReply`
path at turn 01, and the plan path. It creates, renames and modifies nothing.
A missing or contradictory role returns `needs-clarification` (exit code 2).
After the operator answers, pass `--confirmed-role`
and `--operator-instruction` with that answer. Existing dialogue files are not
replaced. Resolve missing or inconsistent history from the actual files and user
context, not an invented session.

`clean` removes only `<stem>-R<turn>C.md`, `<stem>-R<turn>A.md` and `<stem>-RP.md`
beside the original source. Resolve that original document from the plan's Goal
and use `--as-source` to preserve its full filename, including a review suffix.
The original review is never a generated reply or a cleanup target. Before
invoking it,
the coordinator must receive a separate operator `clean` command. Application
always retains the dialogue and plan, regardless of its outcome.
If a cleanup candidate has its own plan or replies, the helper returns
`needs-clarification` (exit code 2) before deleting anything. Resolve the overlap
with the operator; a matching filename alone does not authorize deleting another
discussion's original document.
No matching files is a no-op. A later explicit cleanup covers any matching files
currently present; no deleted-history record is kept.
