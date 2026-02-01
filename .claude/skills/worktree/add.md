# Add Worktree Mode

Create a new worktree for a branch or GitHub issue.

## Triggers

- `/worktree add <branch-name>`
- `/worktree <branch-name>` (branch name, not a number)
- `/worktree #<issue-number>`
- `/worktree <issue-number>` (purely numeric)

## Process

### Step 1: Get Repository Root

```bash
git rev-parse --show-toplevel
```

Save this as `REPO_ROOT`. The worktree base is `${REPO_ROOT}.worktrees`.

### Step 2: Create Worktree Directory

```bash
mkdir -p "${REPO_ROOT}.worktrees"
```

### Step 3: Determine Branch Name

**If argument is a branch name:**
- Strip `add ` prefix if present
- Use the branch name directly

**If argument is an issue number:**
1. Fetch issue details:
   ```bash
   gh issue view <issue-number> --json title,labels
   ```

2. Parse emoji from title to determine branch prefix:
   | Emoji | Prefix |
   |-------|--------|
   | ✨ | `feat` |
   | 🐛 | `fix` |
   | 📋 | `task` |
   | 🎯 | `epic` |
   | 🔧 | `chore` |
   | ⚡ | `perf` |
   | 📝 | `docs` |
   | ♻️ | `refactor` |
   | (default) | `issue` |

3. Create short slug from title:
   - Remove emoji prefix
   - Convert to lowercase
   - Extract 2-3 key words (skip filler words: "add", "the", "a", "for", "to", "in")
   - Replace spaces/special chars with hyphens
   - Format: `<prefix>/<issue-number>-<short-slug>`

   Example: Issue #42 "✨ Add health_check method" → `feat/42-health-check`

### Step 4: Create the Worktree

```bash
# Check if branch exists remotely
git ls-remote --heads origin "$BRANCH_NAME"
```

**If branch exists remotely:**
```bash
git worktree add "${REPO_ROOT}.worktrees/$BRANCH_NAME" "$BRANCH_NAME"
```

**If branch is new:**
```bash
git worktree add -b "$BRANCH_NAME" "${REPO_ROOT}.worktrees/$BRANCH_NAME"
```

### Step 5: Assign Issue (if created from issue)

If the worktree was created from an issue number, assign the issue to yourself:

```bash
gh issue edit <issue-number> --add-assignee @me
```

### Step 6: Open in VS Code

```bash
code "${REPO_ROOT}.worktrees/$BRANCH_NAME"
```

### Step 7: Show Instructions

Display to the user:

```
Worktree created at: ${REPO_ROOT}.worktrees/$BRANCH_NAME
VS Code opened in worktree.

To continue with Claude Code in this worktree:
  cd ${REPO_ROOT}.worktrees/$BRANCH_NAME && claude
```

If created from an issue, also show:
- The issue title
- The generated branch name
