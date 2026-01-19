---
description: Manage git worktrees in a dedicated directory
allowed-tools: Bash(git:*), Bash(ls:*), Bash(mkdir:*), Bash(rm:*), Bash(cd:*), Bash(pwd:*), Bash(realpath:*), Bash(gh:*), Bash(code:*), Bash(.claude/scripts/worktree-status.sh), Glob, Read, AskUserQuestion
argument-hint: <add|list|status|remove|prune|#issue> [branch-name]
---

# Git Worktree Manager

Manage git worktrees with all worktrees stored in a sibling `.worktrees` directory.

## Directory Convention

Given a repository at `/path/to/repo`, worktrees are stored at:
```
/path/to/repo.worktrees/<branch-name>/
```

For example:
- Main repo: `/Users/sodre/ghq/github.com/zeroae/zae-limiter`
- Worktrees: `/Users/sodre/ghq/github.com/zeroae/zae-limiter.worktrees/`
  - `feat/new-feature/`
  - `fix/bug-123/`

## Commands

Parse `$ARGUMENTS` to determine the action:

### `add <branch-name>` or just `<branch-name>`
Create a new worktree for the given branch:

```bash
# Get repo root and compute worktree base
REPO_ROOT=$(git rev-parse --show-toplevel)
WORKTREE_BASE="${REPO_ROOT}.worktrees"

# Create worktree directory if needed
mkdir -p "$WORKTREE_BASE"

# Branch name from arguments (strip "add " prefix if present)
BRANCH_NAME="<parsed-branch-name>"

# Check if branch exists remotely
if git ls-remote --heads origin "$BRANCH_NAME" | grep -q .; then
    # Branch exists - create worktree tracking remote
    git worktree add "$WORKTREE_BASE/$BRANCH_NAME" "$BRANCH_NAME"
else
    # New branch - create from current HEAD
    git worktree add -b "$BRANCH_NAME" "$WORKTREE_BASE/$BRANCH_NAME"
fi
```

After creating, show the user:
1. The full path to the new worktree
2. How to navigate to it: `cd "$WORKTREE_BASE/$BRANCH_NAME"`

### `#<issue-number>` or `<number>` (GitHub Issue)
Create a worktree from a GitHub issue. The branch name is automatically generated from the issue type and title.

```bash
# Fetch issue details
ISSUE_NUM="<parsed-issue-number>"  # Strip leading # if present
ISSUE_JSON=$(gh issue view "$ISSUE_NUM" --json title,labels)

# Parse issue title to determine type prefix
# Issue titles follow format: "<emoji> <Title>"
# Map emoji to branch prefix:
#   ✨ → feat
#   🐛 → fix
#   📋 → task
#   🎯 → epic
#   🔧 → chore
#   ⚡ → perf
#   📝 → docs
#   ♻️ → refactor
#   Default → issue

# Extract title and create short slug:
# 1. Remove emoji prefix
# 2. Convert to lowercase
# 3. Extract 2-3 key words (skip filler words like "add", "the", "a", "for")
# 4. Replace spaces/special chars with hyphens
# 5. Format: <type>/<number>-<short-slug>

# Example: Issue #42 "✨ Add health_check method"
# → Branch: feat/42-health-check

REPO_ROOT=$(git rev-parse --show-toplevel)
WORKTREE_BASE="${REPO_ROOT}.worktrees"
mkdir -p "$WORKTREE_BASE"

# Create the worktree with the generated branch name
git worktree add -b "$BRANCH_NAME" "$WORKTREE_BASE/$BRANCH_NAME"
```

After creating, show the user:
1. The issue title and generated branch name
2. The full path to the new worktree
3. Instructions to move Claude Code to the new worktree:
   - For VS Code: Suggest opening the worktree folder
   - For terminal: `cd "$WORKTREE_BASE/$BRANCH_NAME"`

### `list` or no arguments
List all worktrees and let the user choose one to open in VS Code:

1. **Get worktree list:**
```bash
git worktree list
```

2. **Present choices to user** using `AskUserQuestion`:
   - Parse the output to extract worktree paths and branch names
   - Use branch name as label
   - Use relative path as description (e.g., `.worktrees/feat/42-health-check` or `(main repo)` for the main worktree)
   - Include up to 4 worktrees as options (AskUserQuestion limit)
   - If more than 4 worktrees exist, show the first 4 and mention others exist

3. **Open selected worktree in VS Code:**
```bash
code "<selected-worktree-path>"
```

4. **Show Claude Code command** for continuing in that worktree:
```
VS Code opened at: <selected-worktree-path>

To continue with Claude Code in this worktree:
  cd <selected-worktree-path> && claude
```

5. **Check for .worktrees directory** (use separate commands):
```bash
# First, get repo root
git rev-parse --show-toplevel
# Then use the absolute path to list worktrees directory
ls -la /absolute/path/to/repo.worktrees
```

### `remove <branch-name>`
Remove a worktree:

```bash
REPO_ROOT=$(git rev-parse --show-toplevel)
WORKTREE_BASE="${REPO_ROOT}.worktrees"
BRANCH_NAME="<parsed-branch-name>"

# Try to remove the worktree (without --force first)
git worktree remove "$WORKTREE_BASE/$BRANCH_NAME"

# If it fails (e.g., submodules, uncommitted changes):
# - Ask user for confirmation before using --force
# - Explain why --force is needed
git worktree remove --force "$WORKTREE_BASE/$BRANCH_NAME"

# After removal, ask user if they want to delete the branch too
```

### `prune`
Clean up stale worktree references:

```bash
git worktree prune --verbose
```

### `status`
Show all worktrees with their merge status and CI/CD status.

**Step 1:** Execute the status script:

```bash
.claude/scripts/worktree-status.sh
```

**Step 2:** Render the JSON as a table with these columns:
- Branch name
- PR number (if exists, show as `#N`)
- Status: `MERGED`, `OPEN`, `CLOSED`, or `-`
- CI: Format as `✓N` (success), `✗N` (failure), `⏳N` (pending), `⊘N` (skipped) - only show non-zero counts
- Safe to remove? (`Yes` if MERGED/CLOSED, `No` otherwise, `No (main)` for main branch)

Example output:
```
┌───────────────────────┬──────┬────────┬─────────────┬─────────────────┐
│ Branch                │  PR  │ Status │     CI      │ Safe to remove? │
├───────────────────────┼──────┼────────┼─────────────┼─────────────────┤
│ main                  │  -   │   -    │      -      │ No (main)       │
├───────────────────────┼──────┼────────┼─────────────┼─────────────────┤
│ feat/42-health-check  │ #145 │  OPEN  │ ✓5          │ No              │
├───────────────────────┼──────┼────────┼─────────────┼─────────────────┤
│ fix/99-bug            │ #150 │  OPEN  │ ✗1 ✓4       │ No              │
├───────────────────────┼──────┼────────┼─────────────┼─────────────────┤
│ chore/119-conda-forge │ #140 │ MERGED │ ✓16 ⊘1     │ Yes             │
└───────────────────────┴──────┴────────┴─────────────┴─────────────────┘
```

## Argument Parsing

- No arguments or `list`: List worktrees, let user choose one, open in VS Code
- `#<number>` or just `<number>`: Create worktree from GitHub issue (auto-generates branch name)
- `add <branch>` or just `<branch>`: Add worktree for branch
- `remove <branch>` or `rm <branch>`: Remove worktree
- `prune`: Clean up stale references
- `status`: Show merge status of all worktrees

**Detecting issue numbers vs branch names:**
- If argument is purely numeric or starts with `#` followed by digits → GitHub issue
- Otherwise → branch name

## Examples

```
/worktree                     # List worktrees, choose one, open in VS Code
/worktree list                # Same as above
/worktree status              # Show merge status of all worktrees
/worktree #42                 # Create worktree from issue #42 (e.g., feat/42-health-check)
/worktree 123                 # Create worktree from issue #123
/worktree feat/new-api        # Create worktree for feat/new-api branch
/worktree add fix/bug-123     # Create worktree for fix/bug-123 branch
/worktree remove feat/old     # Remove the feat/old worktree
/worktree prune               # Clean up stale worktree references
```

## Important Notes

1. Always use the `.worktrees` sibling directory convention
2. Branch names can contain slashes (e.g., `feat/new-feature`)
3. When creating worktrees for new branches, base them on the current HEAD
4. When creating worktrees for existing branches, track the remote
5. Suggest adding `.worktrees` to global gitignore if not already present
6. **Run commands separately** - Don't use compound commands with `&&` or variable assignment. Run `git rev-parse --show-toplevel` first, then use the absolute path in subsequent `ls`, `mkdir`, etc. commands. This ensures commands match the `allowed-tools` patterns.

## VS Code Integration

After creating a worktree, automatically open it in VS Code:

```bash
code "$WORKTREE_BASE/$BRANCH_NAME"
```

This opens a new VS Code window in the worktree directory, allowing the user to start working immediately.

## Moving Claude Code to the New Worktree

After creating a worktree, Claude Code cannot automatically change its working directory. Provide the command to start a new session:

```
Worktree created at: /path/to/repo.worktrees/feat/issue-42-add-feature
VS Code opened in worktree.

To continue with Claude Code in this worktree:
  cd /path/to/repo.worktrees/feat/issue-42-add-feature && claude
```
