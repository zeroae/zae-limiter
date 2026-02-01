---
name: worktree
description: Use when user says "/worktree", asks to create a worktree, switch worktrees, list worktrees, check worktree status, or work on a GitHub issue in isolation.
allowed-tools: Bash(git:*), Bash(ls:*), Bash(mkdir:*), Bash(rm:*), Bash(gh:*), Bash(code:*), Bash(.claude/scripts/worktree-status.sh), Glob, Read, AskUserQuestion
user-invocable: true
argument-hint: <add|list|status|remove|prune|#issue> [branch-name]
context: fork
---

# Git Worktree Manager

Manage git worktrees with all worktrees stored in a `.worktrees` subdirectory.

## Directory Convention

Given a repository at `/path/to/repo`, worktrees are stored at:
```
/path/to/repo/.worktrees/<branch-name>/
```

## Modes

| Mode | Triggers | Description |
|------|----------|-------------|
| **Add** | `/worktree add <branch>`, `/worktree <branch>`, `/worktree #<issue>` | Create worktree |
| **Status** | `/worktree status` | Show PR/CI status table |
| **List** | `/worktree`, `/worktree list` | List and select worktrees |
| **Remove** | `/worktree remove <branch>`, `/worktree rm <branch>` | Remove worktree |
| **Prune** | `/worktree prune` | Clean stale references |

## Mode Detection

When this skill is invoked, arguments follow the skill name (e.g., `/worktree status` passes "status" as arguments).

**IMPORTANT:** Before doing anything else, identify the mode from the invocation arguments:

| Arguments | Mode | Instructions |
|-----------|------|--------------|
| (none) or `list` | List | Read `list.md` |
| `status` | Status | Read `status.md` |
| `#<number>` or just a number | Add (from issue) | Read `add.md` |
| `add <branch>` or just `<branch>` | Add (branch) | Read `add.md` |
| `remove <branch>` or `rm <branch>` | Remove | Read `remove.md` |
| `prune` | Prune | Read `prune.md` |

**First action:** Read the appropriate `.md` file for your detected mode, then follow those instructions exactly.

## Important Notes

1. Always use the `.worktrees` subdirectory convention
2. Branch names can contain slashes (e.g., `feat/new-feature`)
3. **Run commands separately** - Don't use compound commands with `&&` or variable assignment
4. Suggest adding `.worktrees` to `.gitignore` if not already present
