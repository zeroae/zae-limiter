---
name: pr
description: Create, view, and edit pull requests following project conventions. Invoke with "open pr", "create pr", "update pr", or use `/pr view [pr-number]` to view, `/pr <issue-number>` to create, `/pr edit [pr-number]` to update, `/pr release <version>` for release prep.
allowed-tools: Bash(gh:*), Bash(git:*), Read, Grep, Glob, AskUserQuestion, Task(Explore)
user-invocable: true
context: fork
---

# Pull Request Skill

Create, view, and edit PRs following project conventions. Supports four modes:

| Mode | Trigger | Purpose |
|------|---------|---------|
| **View** | `/pr view [pr-number]` | Display PR details and CI status |
| **Create** | `/pr [issue-number]`, "create pr" | Create PR for features, bugs, tasks |
| **Edit** | `/pr edit [pr-number]`, "update pr" | Regenerate PR body from current commits |
| **Release** | `/pr release <version>`, "release prep" | Verify release readiness |

## Mode Detection

1. **If `view` keyword** (`/pr view`, `/pr view 218`) → **View mode**
2. **If issue number provided** (`/pr 123`) → **Create mode** (always, regardless of branch)
3. **If `edit` keyword** (`/pr edit`) → **Edit mode**
4. **If `release` keyword** (`/pr release 0.5.0`) → **Release mode**
5. **If on release branch AND no arguments** (`/pr` on `release/0.5.0`) → **Release mode**
6. **Otherwise** → **Create mode**

Pattern for release branches: `^release/v?[0-9]+\.[0-9]+\.[0-9]+$`

## Release Branch Workflow

Non-release PRs target the release branch matching their milestone:

1. Get milestone from issue (e.g., `v0.5.0`)
2. Check if `release/0.5.0` branch exists
3. If exists → PR targets `release/0.5.0` instead of `main`
4. If not exists → ask user: create branch, target main, or cancel

This ensures feature work flows into release branches, which then merge to main via release prep PRs.

```
feature-branch → release/0.5.0 → main
                      ↑
              (release prep PR)
```

## Reference Files

- [view.md](view.md) - View PR mode
- [create.md](create.md) - Create PR mode
- [edit.md](edit.md) - Edit PR mode
- [release.md](release.md) - Release prep mode
- [release-template.md](release-template.md) - Full release PR template
