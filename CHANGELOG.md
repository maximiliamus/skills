# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Add agent-agnostic model-family selection to `execute-guided-runbook` with
  `current`, `frontier`, `previous`, `family:<version>`, and
  `exact:<model-id>` modes, defaulting to the current session model.
- Record `modelFamily`, concrete `modelId`, `modelTier`, and `effortLevel` in
  version 2 session ledgers, fail closed on an unapproved model mismatch, and
  support an explicit operator-directed rebind that preserves saved progress.
- Keep model resolution within the executing agent's runtime, without provider
  mappings or agent-specific integrations, and stop without creating a ledger
  when the current model does not satisfy an explicit selection.
- Define an operator-authorized, runtime-native handoff in which the initiating
  agent proxies the selected model context while only that context creates or
  resumes the runbook ledger.
- Rename the repository release helpers to `$bump-version` and `$release`.
- Keep breaking pre-1.0 releases on minor increments and reserve `1.0.0` for an
  explicit product-readiness decision.

## [0.2.0] - 2026-09-02

- Expand `execute-guided-runbook` with structured acceptance policies,
  arbitrary or sequential step ordering, scored outcomes, flat frontmatter,
  safe session identity migration, and explicit archive pruning.
- Keep the runbook helper dependency-free with a strict minimal parser for the
  supported frontmatter properties.
- Add repository-local skills for deterministic version bumps and guarded
  releases.
- Move release guidance into the internal release skill, align it with the
  repository's `master` branch, and require post-bump validation.

## [0.1.0] - 2026-08-30

- Add `commit-bulk-changes` for safe, well-scoped commits, including scoped or
  explicitly authorized complete initial commits in new repositories.
- Add `execute-guided-runbook` for registered, resumable, step-by-step runbook
  workflows.
- Improve runbook session reliability and portability.
- Add concise skill documentation, release guidance, tests, and linting.
