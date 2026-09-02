---
name: release-skills
description: Prepare, validate, tag, and atomically push a maximiliamus/skills repository release. Use when the user explicitly asks to release or publish the skills repository, including its version bump, changelog, release commit, annotated tag, and branch-and-tag push. Do not use for a bump-only or verification-only request.
---

# Release Skills

Orchestrate the complete Git release boundary for the `maximiliamus/skills`
repository.

## Authorization Boundary

An explicit request such as `Use $release-skills to release the next skills
version` authorizes the version/changelog mutation, one release-preparation
commit, one annotated release tag, and one atomic push of `master` plus that
tag. A request to prepare, preview, or verify does not authorize commit, tag, or
push; stop before those operations.

This workflow does not create a GitHub Release unless the user separately asks
for it.

## Execute The Release

Before taking release action, load these sources completely:

1. Repository instructions.
2. [Skills Release Process](references/release-process.md).
3. `$bump-skills-version`.
4. `skills/commit-bulk-changes/SKILL.md`.

Follow the referenced release process in order. Treat every preflight,
validation, identity, commit, tag, push, and verification condition as a hard
gate. Preserve its stop conditions; a release request does not authorize
retries or cleanup after a failed external mutation.

## Report

Report the released version, release commit, tag target, branch and tag push
result, and all validation results. Distinguish a completed local release from
a successfully published remote release.
