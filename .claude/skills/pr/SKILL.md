---
name: pr
description: Use when user says "/pr", "open pr", "create pr", "update pr", asks to create or edit a pull request, or needs to prepare a release.
allowed-tools: Bash(gh:*), Bash(git:*), Read, Grep, Glob, AskUserQuestion, Task(Explore)
user-invocable: true
context: fork
argument-hint: [view|edit|release|issue-number] [pr-number|version]
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

When this skill is invoked, arguments follow the skill name (e.g., `/pr view 218` passes "view 218" as arguments).

**IMPORTANT:** Before doing anything else, identify the mode from the invocation arguments:

| Arguments | Mode | Instructions |
|-----------|------|--------------|
| `view [pr-number]` | View | Read `view.md` |
| `edit [pr-number]` | Edit | Read `edit.md` |
| `release <version>` | Release | Read `release.md` |
| `#<number>` or just a number | Create (from issue) | Read `create.md` |
| (none) on release branch | Release | Read `release.md` |
| (none) or other | Create | Read `create.md` |

Pattern for release branches: `^release/v?[0-9]+\.[0-9]+\.[0-9]+$`

**First action:** Read the appropriate `.md` file for your detected mode, then follow those instructions exactly.

## Release Branch Workflow

Non-release PRs target the release branch matching their milestone:

1. Get milestone from issue (e.g., `v0.5.0`)
2. Check if `release/0.5.0` branch exists
3. If exists → PR targets `release/0.5.0` instead of `main`
4. If not exists → PR targets `main` (do not ask)

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
