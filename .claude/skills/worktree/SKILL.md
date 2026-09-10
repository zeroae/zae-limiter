---
name: worktree
description: Use when user says "/worktree", asks for worktree PR/CI status across branches, or asks to start work on a GitHub issue in an isolated worktree.
allowed-tools: Bash(git worktree list:*), Bash(git branch:*), Bash(gh:*), Bash(.claude/scripts/worktree-status.sh), EnterWorktree, Glob, Read
user-invocable: true
argument-hint: <status|#issue>
---

# Git Worktree Manager

Two modes that Claude Code's built-in worktree support does not cover. Everything else
has been sunset in favour of the built-in — see [Sunset modes](#sunset-modes).

## Modes

| Mode | Triggers | Description |
|------|----------|-------------|
| **Status** | `/worktree status`, `/worktree` (no args) | PR/CI status table across all worktrees |
| **Issue** | `/worktree #<issue>`, `/worktree <issue-number>` | Name a branch from a GitHub issue, assign it, and move the session into a worktree |

## Mode Detection

**IMPORTANT:** Before doing anything else, identify the mode from the invocation arguments:

| Arguments | Mode | Instructions |
|-----------|------|--------------|
| (none) or `status` | Status | Read `status.md` |
| `#<number>` or a bare number | Issue | Read `issue.md` |
| anything else | — | Read [Sunset modes](#sunset-modes) below and tell the user the built-in equivalent. Do not improvise. |

**First action:** Read the appropriate `.md` file for your detected mode, then follow those instructions exactly.

## The built-in owns worktree creation

Worktrees live where the built-in puts them, `<repo>/.claude/worktrees/<name>`, and are
created only by the built-in. **Never run `git worktree add` from this skill**, and do not
add `WorktreeCreate`/`WorktreeRemove` hooks to relocate them: a hook-relocated worktree
defeats the built-in's pre-removal safety check, so `ExitWorktree(action: "remove")` then
demands `discard_changes: true` on every removal — including a pristine worktree — which
trains the override into a reflex and destroys the guard.

The one thing the built-in gets wrong for this project is the **branch** name. It flattens
slashes and prefixes: worktree name `feat/42-foo` yields branch `worktree-feat+42-foo`.
(The name *validator* accepts slashes; only the derivation flattens them.) The fix is one
command inside the worktree, `git branch -m feat/42-foo` — see `issue.md` Step 4. The
directory keeps its flattened name; nothing reads it.

`.claude/worktrees/` is gitignored, so `pre-commit run --all-files` (which lists via
`git ls-files`) and ruff skip it, and `testpaths = ["tests"]` keeps pytest out.

## Sunset modes

These were removed. Point the user at the replacement rather than reimplementing it:

| Old mode | Use instead |
|----------|-------------|
| `/worktree add <branch>` | `claude -w <name>` (new session), or ask Claude to "start a worktree" mid-session (`EnterWorktree`). Add `--tmux` for a dedicated pane. Rename the branch afterwards if it will become a PR. |
| `/worktree list` | `git worktree list` |
| `/worktree remove <branch>` | "exit the worktree and remove it" (`ExitWorktree`). It refuses to discard uncommitted or unmerged work unless told to. A branch renamed per `issue.md` Step 4 is left behind by design; delete it with `git branch -d`. |
| `/worktree prune` | `git worktree prune --verbose` |

## Important Notes

1. Branch names contain slashes (e.g. `feat/42-foo`); worktree *directory* names cannot
2. **Run commands separately** — don't use compound commands with `&&` or variable assignment
3. This skill does **not** run in a forked context. `EnterWorktree` must move the user's
   real session; from a forked or cwd-pinned agent it would only move the fork
4. Worktrees created for agent isolation (`Agent(isolation: "worktree")`) keep their
   `worktree-*` branch names. That is fine — they are ephemeral and rarely become PRs
