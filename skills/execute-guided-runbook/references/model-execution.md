# Agent-Agnostic Model Execution

Read this before starting or resuming a runbook. The runbook declares semantic
requirements. The executing agent maps those requirements to models and
session capabilities available in its own environment. This skill contains no
agent, provider, scheduler, API, or model catalog integration.

## Resolve The Binding

Read `modelFamily`, `modelTier`, and `effortLevel` from Markdown frontmatter.
Omitted properties resolve to `current`, `medium`, and `medium`. An explicit
operator request may override `modelFamily` for a new session. Registration
does not store or override any of these properties.

Supported `modelFamily` values are:

- `current`: the concrete model already executing the current session;
- `frontier`: the newest model family available to the executing agent;
- `predecessor`: the family immediately before frontier;
- `family:<version>`: the requested family;
- `exact:<model-id>`: the exact concrete identity while this binding is active.

`current` requires no generation lookup: use the actual concrete ID of the
current session. For every other value, resolve family chronology from models
available in the current environment. Do not use a table committed with the
skill. Stop before creating or changing the ledger if frontier, immediate
the predecessor, the requested family, or a suitable concrete identity cannot be
identified unambiguously. Never silently select a different generation or
combine models from different generations.

Choose a concrete identity that satisfies both `modelTier` and `effortLevel`
within the allowed family. These are independent semantic dimensions:

- `modelTier` (`light`, `medium`, `heavy`) describes the capability required by
  the runbook;
- `effortLevel` (`low`, `medium`, `high`, `extra`) describes reasoning depth;
- `modelFamily` limits which generation or concrete model may be selected.

`extra` means the maximum reasoning effort supported by the selected model.
For `exact`, keep the declared tier and effort, use only the requested concrete
identity.

Family keywords and `family:<version>` values are normalized to lowercase; a
leading `v` before a numeric version is removed, so `family:V5.6` becomes
`family:5.6`. Concrete model IDs are opaque printable strings: preserve their case,
Unicode, punctuation, and boundary whitespace exactly, including the suffix of
`exact:`.

## Execute On The Selected Model

Obtain the current concrete model identity from the agent's own runtime
capabilities. A requested model, a session display name, or the value intended
for the ledger is not proof of the executing identity.

For `current`, the proven current identity is the selected identity. For an
explicit `frontier`, `predecessor`, `family:<version>`, or `exact:<model-id>`
selection, compare the selected identity with the proven current identity. If
they differ, state both identities and that execution cannot start, then stop
without creating a ledger. Do not switch or create a session, ask the operator
to launch one, or select a fallback. Wait for the operator's next instruction.

If the operator explicitly answers that execution should proceed on the model
selected by the runbook, the initiating agent may then use the native
model-session or delegation mechanism supplied by its own runtime. Start one
target context on the resolved concrete identity and pass it:

- the repository root and runbook selector;
- resolved `modelFamily` and concrete `modelId`;
- recorded or resolved `modelTier` and `effortLevel`;
- the current ledger status, when one exists;
- the instruction to execute the entire runbook and preserve its ledger.

The target context must obtain its actual model identity from its runtime and
prove that it matches the resolved or recorded `modelId`. Only then may it call
the helper. For a new runbook, this means the ledger is created in the target
context. For a resume, the target context continues the existing ledger without
creating a parallel ledger or calling `rebind` when its identity already
matches.

When native delegation supports bidirectional communication, the initiating
agent remains the operator-facing coordinator. Forward the target context's
questions to the operator, return the operator's answers without changing
their meaning, and relay evidence and results back to the operator. Keep all
runbook execution in the target context until completion or another explicit
operator decision. If the runtime cannot create the required model context or
proxy its interaction, report the limitation and stop. The skill does not
define a provider-specific session API or require manual conversation transfer
when native proxying is available.

The target session starts or resumes the ledger with:

```bash
python <path-to-skill>/scripts/runbook_session.py run <selector> \
  --model-id <actual-current-model>
```

Add `--model-family <family>` only for an explicit operator override. The helper
records the supplied ID as `modelId`. The agent must derive that argument from
its actual runtime identity, not from the operator's requested value or a
session label.

Every command that advances execution (`run`, `step`, `complete`, `block`,
`skip`, and `finish`) requires `--model-id`. The helper compares it with the
recorded ID before writing. `status`, `list`, `resolve`,
registry management, and archive pruning remain read-only with respect to
runbook execution and do not require an executing identity.

## Resume And Failure Semantics

The version 2 ledger records:

- `modelFamily`;
- `modelId`;
- `modelTier`;
- `effortLevel`.

On resume, derive the current identity again and pass it to the helper. Even
when the ledger records `current`, it refers to the currently recorded model ID;
a
mismatch fails closed before progress, revision decisions, archive creation, or
path-ID migration. It does not create a replacement ledger or prompt for a
particular recovery action.

An explicitly repeated `modelFamily` must also match the ledger. Document
edits, registry changes, restart, revision continuation, and completed-revision
renewal preserve the binding. Family chronology is resolved only for a new
binding and is not recalculated on resume.

After a mismatch, wait for the operator's next instruction. If the operator
explicitly directs execution to continue on the current model, update the same
ledger with:

```bash
python <path-to-skill>/scripts/runbook_session.py rebind <selector> \
  --model-family current --model-id <actual-current-model>
```

If the operator specifies another family or exact model, resolve it and prove
the current identity first, then pass that canonical family and actual ID to
`rebind`. This command atomically changes the ledger's `modelFamily`, `modelId`,
and `updatedAt`. It preserves tier, effort, runbook revision, current step,
history, and revision decisions, so execution continues with all saved context.
Do not call `rebind` merely because a mismatch occurred.

Only an explicit instruction to start over permits a clean run. Use `--restart`
for the same runbook revision, or `--ignore` after a changed-revision decision.
If the clean run uses another model, perform the operator-authorized `rebind`
first; the reset then archives the previous ledger before creating the new one.

The helper verifies equality and persistence; the executing agent is
responsible for obtaining truthful runtime identity. If its capabilities cannot
prove model identity, stop and report that limitation. Do not replace proof
with an advisory field.

Registry and session schemas are version 2. Version 1 files are rejected. New
model fields are required; no missing-field or obsolete-name compatibility path
is provided.
