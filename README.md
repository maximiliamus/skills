# Shared Reusable Agent Skills

A collection of portable, verified Agent Skills for coding agents that support
the open `SKILL.md` format.

## Included Skills

| Skill | Description | Overview |
| --- | --- | --- |
| **`commit-bulk-changes`** | Group and commit changes using active conventions, or Conventional Commits by default | [Read overview](./skills/commit-bulk-changes/README.md) |
| **`execute-guided-runbook`** | Register, manage, and execute repository runbooks step by step | [Read overview](./skills/execute-guided-runbook/README.md) |

## Installation

### Skills CLI (recommended)

Install interactively into the current project:

```bash
npx skills add maximiliamus/skills
```

Project scope is the default. Add `--global` to make the selected skills
available across projects, or use `--skill <name>` to install only one skill.
The skills keep runbook configuration and progress in the active repository,
so both installation scopes behave the same at runtime.

See the [Vercel Labs Skills CLI](https://github.com/vercel-labs/skills) for all
installation options.

### Manual Project Installation

```bash
cp -R skills/<skill-name> .agents/skills/<skill-name>
```

## Documentation & Maintenance

- [Changelog](./CHANGELOG.md)

## License

[MIT License](./LICENSE)
