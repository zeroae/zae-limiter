# Issue Mode

Create a conventionally-named worktree for a GitHub issue, assign the issue, and move
the current session into the worktree. If the issue already has a branch, resume it
instead of starting a new one.

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

### Step 2: Look for an Existing Branch

A second `/worktree #<n>` on the same issue must resume its branch, not start a new one
beside it. Match on the issue number, not the full name: the slug is derived loosely and
may come out differently this time. Run each command from the repo root:

```bash
git worktree list --porcelain
```

```bash
git branch --list '*/<issue-number>-*' --format='%(refname:short)'
```

```bash
git ls-remote --heads origin '*/<issue-number>-*'
```

In the `--porcelain` output, a `branch refs/heads/<prefix>/<issue-number>-<slug>` line
means the branch is checked out; its path is the `worktree <path>` line above it.

Take the first case that applies:

| Found | Case | Branch name used from here on |
|-------|------|-------------------------------|
| Checked out in a worktree | **Open** | The existing branch |
| A local branch | **Resume** | The existing branch |
| On origin only | **Resume from origin** | The existing branch |
| Nothing | **New** | The name from Step 1 |

If more than one branch matches, list them and ask the user which one to use. Don't
guess. If the match is checked out in the **main** working tree (the first `worktree`
entry), tell the user and stop: no worktree is needed.

### Step 3: Assign the Issue

```bash
gh issue edit <issue-number> --add-assignee @me
```

Do this *before* Step 4 — once the session moves, the shell is in the worktree. It is a
no-op if the issue is already assigned to you.

### Step 4: Enter the Worktree

**Open:** switch into the worktree that already has the branch, then skip to Step 6:

```
EnterWorktree(path="<path from Step 2>")
```

**Every other case:** call `EnterWorktree` with the branch name:

```
EnterWorktree(name="feat/42-health-check")
```

### Step 5: Put the Worktree on the Right Branch

The built-in derives its own branch name from the worktree name, flattening slashes and
adding a prefix: `feat/42-health-check` arrives as branch `worktree-feat+42-health-check`
in `.claude/worktrees/feat+42-health-check`, based on `origin/main` (or on your local
HEAD, if the `worktree.baseRef` setting is `head`). **Only the branch
changes** — the directory keeps the flattened name, which is fine: nothing reads it, and
`.claude/worktrees/` is gitignored so no tooling walks it.

**New** — rename the branch to the intended name:

```bash
git branch -m feat/42-health-check
```

The new branch has no upstream. That is correct — `/pr` runs
`git push -u origin <branch>`, which sets it. Do not set an upstream to `origin/main`.

**Resume** — switch to the existing branch:

```bash
git switch feat/42-health-check
```

**Resume from origin** — fetch the branch, then switch to it with tracking set:

```bash
git fetch origin feat/42-health-check
```

```bash
git switch --track origin/feat/42-health-check
```

Never rename onto an existing branch. `git branch -m` fails outright if the name exists
locally. If the name exists only on origin, the rename succeeds but creates an unrelated
branch with the same name, and `/pr`'s push is rejected as non-fast-forward.

After a switch, the throwaway `worktree-…` branch stays behind with no commits of its
own. Leave it: `ExitWorktree(action="remove")` deletes it along with the worktree. After
`action="keep"`, delete it with `git branch -d`.

The rename is convention, not a build requirement. No workflow, hook, or lint reads the
head branch name (CI's `branches:` filters match the PR's *base*). Rename anyway: the
name is permanent and human-facing on the PR, in `git log`, and in the merge commit. If
the rename or switch fails, carry on and mention it — nothing downstream breaks.

### Step 6: Show Instructions

Display the issue title, the branch name, whether it was new or resumed, the worktree
path, and confirm the session is now working in the worktree.

To finish up later:

- `ExitWorktree(action="keep")` returns to the repo root and leaves everything on disk
- `ExitWorktree(action="remove")` deletes the worktree directory. It refuses if there is
  uncommitted or unmerged work. Because the branch was renamed or switched in Step 5, the
  built-in no longer recognises it and **leaves the branch behind** — intended, since the
  PR lives on it. Delete it yourself with `git branch -d <branch>` once merged
- After a **Resume**, `remove` refuses whenever the resumed branch has commits not on
  `main`, which is nearly always. It reports them as commits "on `worktree-<name>`" that
  will be "discarded permanently". That is misleading: the commits are on the resumed
  branch, and `remove` only deletes the throwaway branch. Before passing
  `discard_changes: true`, run this inside the worktree:

  ```bash
  git status -sb
  ```

  Override **only** if there are no uncommitted or untracked files and the first line
  names an upstream (`## <branch>...origin/<branch>`) with no `[ahead N]`. A branch with
  no upstream has never been pushed. Otherwise push first, or use `action="keep"`.
  `discard_changes` really does delete uncommitted files, so a reflexive override loses
  work
- A worktree opened by `path` in the **Open** case is never removed by `ExitWorktree`;
  use `action="keep"`
