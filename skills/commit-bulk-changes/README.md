# Commit Bulk Changes

`commit-bulk-changes` organizes repository changes into one or more clear Git
commits. It works with staged, unstaged, untracked, deleted, and renamed files
while keeping unrelated work out of the resulting commits.

## How It Works

The skill inventories the working tree, determines which changes are in scope,
and groups them by logical concern. It then presents a commit plan, stages or
isolates the exact files for each group, creates the authorized commits, and
verifies the repository state afterward.

Commit messages follow conventions supplied by the user, agent, or repository.
When no convention is available, the skill defaults to Conventional Commits.
It never pushes changes unless the user explicitly requests a push.

For a repository without a first commit, it preserves the requested scope. It
creates a complete snapshot with the message `Initial commit.` only when the
complete non-ignored worktree is explicitly authorized.

## Usage

Invoke the skill with a request such as:

```text
Use $commit-bulk-changes to commit the changes from this task.
```

See [SKILL.md](./SKILL.md) for the complete agent instructions.
