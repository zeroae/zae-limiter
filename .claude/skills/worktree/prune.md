# Prune Mode

Clean up stale worktree references.

## Triggers

- `/worktree prune`

## Process

### Step 1: Run Prune Command

```bash
git worktree prune --verbose
```

This removes references to worktrees that no longer exist on disk.

### Step 2: Report Results

The `--verbose` flag will show what was pruned. Display the output to the user.

If nothing was pruned, inform the user:
```
No stale worktree references found.
```

If entries were pruned, summarize:
```
Pruned N stale worktree reference(s).
```
