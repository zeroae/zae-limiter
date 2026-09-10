# Issue Mode

Create a conventionally-named worktree for a GitHub issue, assign the issue, and move
the current session into the worktree.

## Triggers

- `/worktree #<issue-number>`
- `/worktree <issue-number>` (purely numeric)

For a worktree that is *not* tied to an issue, don't use this skill — run
`claude -w <name>`, or ask Claude to "start a worktree".

## Process

Do not run `git worktree add`. The built-in creates the worktree; this mode only
supplies the name and fixes up the branch afterwards.

### Step 1: Determine Branch Name

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

### Step 2: Assign the Issue

```bash
gh issue edit <issue-number> --add-assignee @me
```

Do this *before* Step 3 — once the session moves, the shell is in the worktree.

### Step 3: Enter the Worktree

Call the `EnterWorktree` tool with the branch name from Step 1:

```
EnterWorktree(name="feat/42-health-check")
```

### Step 4: Rename the Branch

The built-in derives its own branch name from the worktree name, flattening slashes and
adding a prefix: `feat/42-health-check` arrives as branch `worktree-feat+42-health-check`
in `.claude/worktrees/feat+42-health-check`. Rename the branch to the intended name:

```bash
git branch -m feat/42-health-check
```

This runs inside the worktree and renames the checked-out branch. **Only the branch is
renamed** — the directory keeps the flattened name, which is fine: nothing reads it, and
`.claude/worktrees/` is gitignored so no tooling walks it.

This step is convention, not a build requirement. No workflow, hook, or lint reads the
head branch name (CI's `branches:` filters match the PR's *base*). Rename anyway: the
name is permanent and human-facing on the PR, in `git log`, and in the merge commit. If
the rename fails, carry on and mention it — nothing downstream breaks.

The new branch has no upstream. That is correct — `/pr` runs
`git push -u origin <branch>`, which sets it. Do not set an upstream to `origin/main`.

### Step 5: Show Instructions

Display the issue title, the branch name, the worktree path, and confirm the session is
now working in the worktree.

To finish up later:

- `ExitWorktree(action="keep")` returns to the repo root and leaves everything on disk
- `ExitWorktree(action="remove")` deletes the worktree directory. It refuses if there is
  uncommitted or unmerged work. Because the branch was renamed in Step 4, the built-in
  no longer recognises it and **leaves the branch behind** — intended, since the PR lives
  on it. Delete it yourself with `git branch -d <branch>` once merged.
