# Shared Reusable Agent Skills

A collection of portable, verified Agent Skills for coding agents that support
the open `SKILL.md` format.

## Included Skills

| Skill | Description | Overview |
| --- | --- | --- |
| **`commit-bulk-changes`** | Group and commit changes using active conventions, or Conventional Commits by default | [Read overview](./skills/commit-bulk-changes/README.md) |
| **`conduct-paired-review`** | Discuss a file (functionality description, proposal prepared, review results) with two models | [Read overview](./skills/conduct-paired-review/README.md) |
| **`execute-guided-runbook`** | Execute marked runbooks with dependency-aware scheduling and resumable progress | [Read overview](./skills/execute-guided-runbook/README.md) |

## Installation

### Skills CLI (recommended)

Install skills in the repository where you want to use them:

```bash
npx skills add maximiliamus/skills
```

Add `--global` to make the selected skills available across repositories, or
use `--skill <name>` to install only one skill. Settings and progress belong
to the repository where a skill is used, even with a global installation.

See the [Vercel Labs Skills CLI](https://github.com/vercel-labs/skills) for all
installation options.

### Manual Skill Installation

Copy a skill from this collection into the repository where you will use it:

```bash
mkdir -p <your-repository>/.agents/skills
cp -R skills/<skill-name> <your-repository>/.agents/skills/
```

## Skill Configuration

After installing a skill, initialize it in the repository where you will use it:

```text
$<skill-name> init
```

After installing a newer version, run its update command:

```text
$<skill-name> update
```

Every skill handles both commands before its main workflow. For skills with
project settings, `init` creates the settings file when needed, and `update`
checks that the installed skill can use it. The file lives outside the
installation folder. Commit it with your project. Skill updates preserve your
settings and ongoing work. If no actions are needed, the skill replies only
`nothing to do`.

## Documentation & Maintenance

- [Changelog](./CHANGELOG.md)
- [Runbook execution contract](./skills/execute-guided-runbook/references/journal-contract.md)

## License

[MIT License](./LICENSE)
