# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Start a paired review by naming the project functionality you want to
  improve. The first reviewer writes the report that anchors the discussion
  and preserves the initial findings.
- Name required outputs once in the runbook's instructions. Guided
  runbooks verify those files during step acceptance and again at completion.
- Keep each guided runbook's recent runs, private reports and supporting files
  together. Keep earlier reports available until a new run has been saved as
  complete. Preserve evidence for shared changes that still need
  reconciliation. Set the history size per project or runbook and review
  the latest run before choosing whether to continue or start over. Continue
  with the original procedure when its source still matches.
  See the [upgrade instructions](./skills/execute-guided-runbook/README.md#upgrade).

## [0.4.0] - 2026-11-21

- Use `conduct-paired-review` to review proposals, plans, and existing
  functionality with two models. Turn their findings and disagreements into
  agreed revisions or an improvement plan, and retain the review history.
- Store settings for paired review and guided runbooks per project. Updates
  preserve those settings and ongoing work.
- Write simple sequential runbooks as Markdown headings or numbered steps.
  Run independent steps in parallel, pause once active work has settled, and
  resume with verified results and the original step order.
- See which steps passed, failed, were skipped, ran out of attempts, or need
  reassessment. Finish an audit with a verified report of unchecked sections
  when preparation runs out of attempts and the procedure allows those gaps.
- Continue existing v4 runs under their saved rules. Use older journals as
  reference and start fresh when that work is still needed. See the
  [upgrade instructions](./skills/execute-guided-runbook/README.md#upgrade).

## [0.3.1] - 2026-09-25

- Include commit bodies for substantive changes by default in
  `commit-bulk-changes`, while allowing one-line messages for fully mechanical
  or editorial changes.

## [0.3.0] - 2026-09-10

- Add model selection to `execute-guided-runbook` with `current`, `frontier`,
  `predecessor`, `family:<version>`, and `exact:<model-id>` modes, defaulting to
  the current session model.
- Reject ambiguous model selection and unapproved model mismatches before
  starting or resuming a runbook, without silently changing model families.
- Preserve runbook progress when the operator explicitly rebinds execution to
  another model, and start over only when the operator explicitly requests it.

## [0.2.0] - 2026-09-02

- Expand `execute-guided-runbook` with structured acceptance policies,
  arbitrary or sequential step ordering, scored outcomes, flat frontmatter,
  safe session identity migration, and explicit archive pruning.
- Keep the runbook helper dependency-free with a strict minimal parser for the
  supported frontmatter properties.

## [0.1.0] - 2026-08-30

- Add `commit-bulk-changes` for safe, well-scoped commits, including scoped or
  explicitly authorized complete initial commits in new repositories.
- Add `execute-guided-runbook` for registered, resumable, step-by-step runbook
  workflows.
- Improve runbook session reliability and portability.
- Add concise skill documentation, release guidance, tests, and linting.
