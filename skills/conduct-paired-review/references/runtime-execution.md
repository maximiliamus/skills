# Runtime Execution

The initiating agent passes turns and materials between two independent participants.
It does not assess or rewrite their responses, request shorter answers or maintain
the plan. The author maintains the plan; the critic critically reviews it.
Resolve concrete
model identities from runtime metadata or a documented dispatch contract, not a
model's self-description. Labels such as Codex or Claude are not model IDs.
Never silently substitute a model or have one model impersonate both roles.

Read relevant project instructions and evidence. Give the first critic fresh
context; subsequent turns receive the current plan, latest opponent reply and
relevant earlier responses. Send the participant its role and dialogue rules;
include the plan format when the author needs to create or update the plan.
Use the client's dedicated agent or system-instruction channel for this role
when available, while preserving its tool permissions and approval settings.
Do not add output-length targets or demand a fixed report structure. Each
participant writes its own response without a coordinator editing cycle.
On resumption, reconstruct context from
the Markdown files and retain the participants listed in the plan. Include
discoveries recorded in earlier replies; do not depend on a surviving private
model context. Both models
may inspect the system for evidence. Before passing the turn, each must record
new significant findings, source locations, observed check results and implications
in its reply; later turns can cite them. If the context and investigation do not
establish an answer, report that limitation, label any hypothesis, or defer the
question until clarification.
Do not fill missing evidence with invented facts or claim unperformed checks.
No separate transport or session record is required.

## Dispatch

Use native subagents when they support both selected concrete models and repeated
turns. Spawn two independent contexts and keep one outstanding turn. Save each
reply and any changes the author made to the plan before passing control to the
opponent.
Do not create
visible user-owned chats to implement internal participants.

When another installed client is required, inspect its help and supported
non-interactive interface. Preserve the user's sandbox and approval settings.
Do not install clients, supply credentials or enable permission-bypass options
merely to complete a discussion. A missing client or unavailable model is a
specific limitation to report.

Where supported, the general call shapes are:

```text
codex exec --model <concrete-id> --sandbox read-only --json -
claude --print --model <concrete-id> --output-format stream-json --verbose
agy --print <prompt> --model <id> --mode plan --sandbox --output-format json
```

Use stdin or structured arguments for prompts. Check actual model identity,
terminal completion, errors, permission denials and a nonempty reply body.
Exit code zero or a success label alone does not establish delivery. Save the
participant's response as its numbered Markdown file; transport JSON is not a
review artifact. An empty response or a permission-blocked attempt creates no
reply, approval or turn advance, and does not count toward stagnation. Preserve
published files and report the actual blocker. Retry the same pending turn only
after addressing its cause within the authorized scope; stop if it recurs.
These are transport checks, not a review or rewrite of the participant's content.

During review, both participants read evidence and author responses. After the
operator authorizes `apply`, the author updates only the original document at its
existing filename. For existing functionality, transfer the agreed steps into
the initial review as its accepted improvement plan; leave implementation to the
operator's own workflow. Verify the document update and preserve the replies and
plan afterward. The coordinator performs
cleanup only in response to a separate operator `clean` command.

## Antigravity

Antigravity uses its own `agy` client, not Gemini CLI. Select a Gemini model inside
that client with `family:gemini`, inspect available model IDs and pin the selected
model in the plan. Inspect `agy --version`, `agy --help` and `agy models` before
dispatch; a backend runtime version may differ from the CLI version. Verify
supported flags locally rather than assuming current web examples fit the installed
client. Never substitute `gemini` for `agy` silently.

Use `--print-timeout 15m` for each Antigravity turn by default. Honor a
different limit requested by the operator. Allow the CLI to report its
terminal result before the coordinator's outer deadline. Treat a timeout
or partial output as a failed attempt and keep the same turn pending.

Prepare access before a headless turn:

- Inspect the effective permissions and the evidence the participant needs.
  Prefer native file-reading and search tools within the authorized scope.
  Adding a workspace with `--add-dir` does not grant command permission.
- When local help supports `--settings`, use its session-only override for
  necessary grants, preserving existing allow, deny and ask rules. Do not assume
  permission arrays merge. Add only concrete `read_file(<path>)` grants needed
  for authorized evidence; keep explicit denials and approval requirements.
  Deny takes precedence over Ask, which takes precedence over Allow. A matching
  Ask rule still needs operator resolution; adding Allow does not override it.
- Keep planning mode and the sandbox. Do not combine `--mode plan` with
  `--disable-slash-commands` when the client warns that this disables planning.
  Planning mode is not itself a read-only permission boundary.
- A shell command used for reading still needs command permission. Do not grant
  `command(*)`, a whole shell, or an unrestricted command prefix as a read-only
  substitute. Use only an already authorized, narrowly matched operation whose
  matching behavior is verified. Never enable permission bypass to unblock review.
- If direct access is unavailable, supply the necessary authorized texts through
  stdin using a supported input format. For `--input-format stream-json`, send
  user events and use `--output-format stream-json`; consume the terminal result.
  This does not authorize access to denied files or disclosure outside the agreed
  scope. If required evidence remains unavailable, report the limitation and stop.

Do not persist review-specific grants in the user's global settings or the skill
configuration. A persistent permission change needs separate operator direction.
If session overrides are unsupported, use already permitted access or report the
specific missing permission instead of changing global defaults.

Prefer streaming output to inspect tool errors as well as the terminal result.
Check `tool_info.error`, the final envelope's `response`, `error` and
`denied_actions`, and stderr. An explicit policy denial may appear only in a tool
error, without `denied_actions`. Antigravity can exit zero with `status: SUCCESS`
despite a denied tool.
An empty or whitespace-only response is not a reply. Nonempty `denied_actions`
must be reported as a blocked attempt, even if response text is present; never
claim that the denied inspection ran. Keep the pending turn until a permitted
retry delivers the participant's reply, or stop and report the blocker.

When an existing participant context can be addressed safely, use it; otherwise
supply the Markdown history to a new context on the same model. Do not use a generic
continue option that might attach to an unrelated conversation. A client requiring
manual turn forwarding cannot provide the requested automatic discussion.

Consult the official [headless reference](https://antigravity.google/docs/cli/headless/),
[permissions](https://antigravity.google/docs/permissions/),
[CLI settings](https://antigravity.google/docs/settings?tab=cli) and
[model selection](https://antigravity.google/docs/models) alongside local help.

## Small Smoke Exercise

Use `assets/smoke-claim.md` in a disposable directory. The critic should identify
the incorrect total, the author should agree on a precise correction, and both
should approve the plan. Verify that only numbered Markdown replies and a plan
were created and that the source is unchanged before operator-authorized application.
Verify that findings address the review goal, the author writes the plan, and
the coordinator forwards responses without assessing or shortening them.
Verify that application retains all replies and the plan, and only a separate
`clean` command removes them. Mocked file-operation tests do not prove independent
model behavior.

For Antigravity, use synthetic evidence to check scoped native reading and a
denied command without changing persistent permission settings. A denied action
or empty response must not create a numbered reply or advance the turn, even
when the process exits zero and the envelope reports success.

For an existing-functionality exercise, start from a review of a trivial defect
with an explicit role for its writer. Check that the goal comes from the request,
the source keeps its name, and the opponent writes the new `R01` reply. After
`apply`, the original review should contain the accepted improvement steps, while
the reviewed code and tests remain unchanged. A separate `clean` must preserve
that review.
An unclear goal must prompt an operator question before the dialogue starts.
