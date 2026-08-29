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

Registering a path that already has an unregistered session preserves its
recorded progress under the new registered ID.

If a runbook changes during an unfinished session, the skill pauses for an
explicit choice between continuing with saved progress and archiving it to
start over. External mutations such as pushing, tagging, publishing, or merging
still require explicit authorization.

## Usage

Invoke the skill with a request such as:

```text
Use $execute-guided-runbook to run the release runbook.
```

See [SKILL.md](./SKILL.md) for the complete agent instructions and the
[runbook registry schema](./references/runbook-registry.schema.json) for the
optional registry format.
