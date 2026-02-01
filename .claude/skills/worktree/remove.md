# Remove Worktree Mode

Remove a worktree and optionally delete its branch.

## Triggers

- `/worktree remove <branch-name>`
- `/worktree rm <branch-name>`

## Process

### Step 1: Get Repository Root

```bash
git rev-parse --show-toplevel
```

Save as `REPO_ROOT`.

### Step 2: Parse Branch Name

Strip `remove ` or `rm ` prefix from arguments to get the branch name.

### Step 3: Attempt Normal Removal

```bash
git worktree remove "${REPO_ROOT}/.worktrees/$BRANCH_NAME"
```

### Step 4: Handle Failures

If the removal fails (e.g., uncommitted changes, submodules), the error message will explain why.

**Ask the user** using `AskUserQuestion`:
```
header: "Force remove"
question: "Worktree removal failed. Would you like to force remove it? This will discard any uncommitted changes."
options:
  - label: "Yes, force remove"
    description: "Use --force to remove despite issues"
  - label: "No, keep it"
    description: "Cancel the removal"
```

If user confirms:
```bash
git worktree remove --force "${REPO_ROOT}/.worktrees/$BRANCH_NAME"
```

### Step 5: Offer Branch Deletion

After successful worktree removal, **ask the user**:

```
header: "Delete branch"
question: "Worktree removed. Would you like to also delete the branch '$BRANCH_NAME'?"
options:
  - label: "Yes, delete branch"
    description: "Delete the local branch"
  - label: "No, keep branch"
    description: "Keep the branch for later use"
```

If user confirms:
```bash
git branch -d "$BRANCH_NAME"
```

If `-d` fails (unmerged changes), ask about `-D`:
```
header: "Force delete"
question: "Branch has unmerged changes. Force delete?"
options:
  - label: "Yes, force delete"
    description: "Delete branch even with unmerged changes"
  - label: "No, keep branch"
    description: "Keep the branch"
```

If confirmed:
```bash
git branch -D "$BRANCH_NAME"
```
