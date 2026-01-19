---
name: pr
description: Create and edit pull requests following project conventions. Use `/pr <issue-number>` to create, `/pr edit [pr-number]` to update, or `/pr release <version>` for release prep.
allowed-tools: Bash(gh:*), Bash(git:*), Read, Grep, Glob, AskUserQuestion, Task
user-invocable: true
---

# Pull Request Skill

Create and edit PRs following project conventions. Supports three modes:

| Mode | Trigger | Purpose |
|------|---------|---------|
| **Create** | `/pr [issue-number]`, "create pr" | Create PR for features, bugs, tasks |
| **Edit** | `/pr edit [pr-number]`, "update pr" | Regenerate PR body from current commits |
| **Release** | `/pr release <version>`, "release prep" | Verify release readiness |

## Branch-Based Mode Detection

Before processing any mode, check the current branch name:

```bash
git rev-parse --abbrev-ref HEAD
```

If branch matches `release/X.X.X` or `release/vX.X.X` (e.g., `release/0.5.0`, `release/v1.2.3`):
- **Auto-detect as Release mode** - extract version from branch name
- Skip to [release.md](release.md) workflow
- No issue number required

Pattern: `^release/v?[0-9]+\.[0-9]+\.[0-9]+$`

## Reference Files

- [create.md](create.md) - Create PR mode
- [edit.md](edit.md) - Edit PR mode
- [release.md](release.md) - Release prep mode
- [release-prep-template.md](release-prep-template.md) - Full release PR template
