---
name: worktree
description: Manage git worktrees in a `.worktrees` subdirectory. Triggers on "/worktree", "create worktree", "switch worktree", "list worktrees", "worktree status", or working on a GitHub issue in isolation.
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
| **List** | `/worktree`, `/worktree list` | List and select worktrees |
| **Status** | `/worktree status` | Show PR/CI status table |
| **Remove** | `/worktree remove <branch>`, `/worktree rm <branch>` | Remove worktree |
| **Prune** | `/worktree prune` | Clean stale references |

## Mode Detection

Parse `$ARGUMENTS` to determine the mode:

1. **No arguments** or `list` → **List mode** (see `list.md`)
2. **`status`** → **Status mode** (see `status.md`)
3. **`#<number>`** or purely numeric → **Add mode** from GitHub issue (see `add.md`)
4. **`add <branch>`** or just `<branch>`** → **Add mode** for branch (see `add.md`)
5. **`remove <branch>`** or `rm <branch>` → **Remove mode** (see `remove.md`)
6. **`prune`** → **Prune mode** (see `prune.md`)

## Reference Files

After determining the mode, read the corresponding file for detailed instructions:

- `add.md` - Creating worktrees (branch or issue)
- `list.md` - Listing and selecting worktrees
- `status.md` - PR/CI status display
- `remove.md` - Removing worktrees
- `prune.md` - Pruning stale references

## Important Notes

1. Always use the `.worktrees` subdirectory convention
2. Branch names can contain slashes (e.g., `feat/new-feature`)
3. **Run commands separately** - Don't use compound commands with `&&` or variable assignment
4. Suggest adding `.worktrees` to `.gitignore` if not already present
