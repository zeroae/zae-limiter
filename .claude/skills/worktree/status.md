# Status Mode

Show all worktrees with their PR and CI/CD status.

## Triggers

- `/worktree status`

## Process

### Step 1: Execute Status Script

```bash
.claude/scripts/worktree-status.sh
```

This outputs JSON with the following structure for each worktree:
```json
{
  "path": "/path/to/worktree",
  "branch": "feat/42-foo",
  "pr": { "number": 145, "state": "OPEN" },
  "ci": { "success": 5, "failure": 0, "pending": 0, "skipped": 1 },
  "safe_to_remove": false,
  "safe_reason": "PR open or no PR"
}
```

### Step 2: Render as Table

Format the JSON output as a table with these columns:

| Column | Description |
|--------|-------------|
| **Branch** | Branch name |
| **PR** | PR number as `#N`, or `-` if no PR |
| **Status** | `MERGED`, `OPEN`, `CLOSED`, or `-` |
| **CI** | Format counts: `✓N` (success), `✗N` (failure), `⏳N` (pending), `⊘N` (skipped). Only show non-zero. |
| **Safe to remove?** | `Yes` if MERGED/CLOSED, `No` otherwise, `No (main)` for main branch |

### Example Output

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
│ chore/119-conda-forge │ #140 │ MERGED │ ✓16 ⊘1      │ Yes             │
└───────────────────────┴──────┴────────┴─────────────┴─────────────────┘
```

### CI Status Formatting

Build the CI string by concatenating non-zero counts:
- If `failure > 0`: add `✗{failure}`
- If `pending > 0`: add `⏳{pending}`
- If `success > 0`: add `✓{success}`
- If `skipped > 0`: add `⊘{skipped}`
- If all are 0: show `-`

Show failure first (most important), then pending, success, skipped.
