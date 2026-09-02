# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
