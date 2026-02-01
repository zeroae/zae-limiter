# List Worktrees Mode

List all worktrees and let the user choose one to open in VS Code.

## Triggers

- `/worktree` (no arguments)
- `/worktree list`

## Process

### Step 1: Get Worktree List

```bash
git worktree list
```

Example output:
```
/Users/sodre/ghq/github.com/zeroae/zae-limiter                        abc1234 [main]
/Users/sodre/ghq/github.com/zeroae/zae-limiter.worktrees/feat/42-foo   def5678 [feat/42-foo]
/Users/sodre/ghq/github.com/zeroae/zae-limiter.worktrees/fix/99-bug    ghi9012 [fix/99-bug]
```

### Step 2: Parse and Present Choices

Parse each line to extract:
- Path (first field)
- Branch name (text inside `[...]`)

Present choices using `AskUserQuestion`:
- Use branch name as the **label**
- Use relative path as the **description**:
  - Main worktree: `(main repo)`
  - Others: `repo.worktrees/<branch-name>`

**Note:** AskUserQuestion has a 4-option limit. If more than 4 worktrees exist:
- Show the first 4 as options
- Mention in the question that additional worktrees exist

Example question:
```
header: "Worktree"
question: "Which worktree would you like to open?"
options:
  - label: "main"
    description: "(main repo)"
  - label: "feat/42-foo"
    description: "repo.worktrees/feat/42-foo"
  - label: "fix/99-bug"
    description: "repo.worktrees/fix/99-bug"
```

### Step 3: Check .worktrees Directory

After getting the worktree list, also check if the worktrees sibling directory exists and has content:

```bash
git rev-parse --show-toplevel
```

Then:
```bash
ls -la ${REPO_ROOT}.worktrees
```

### Step 4: Open Selected Worktree

Based on user selection:

```bash
code "<selected-worktree-path>"
```

### Step 5: Show Claude Code Command

```
VS Code opened at: <selected-worktree-path>

To continue with Claude Code in this worktree:
  cd <selected-worktree-path> && claude
```
