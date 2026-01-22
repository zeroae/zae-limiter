# View PR Mode

Display PR details including status, CI checks, labels, and milestone.

## Triggers

- `/pr view` - View PR for current branch
- `/pr view 218` - View specific PR number
- `/pr view #218` - View specific PR (with hash)

## Process

### 1. Get PR Details

```bash
# If no PR number provided, get PR for current branch
gh pr view --json number,title,state,body,labels,milestone,baseRefName,headRefName,url,isDraft,statusCheckRollup

# Or if PR number provided
gh pr view <number> --json number,title,state,body,labels,milestone,baseRefName,headRefName,url,isDraft,statusCheckRollup
```

### 2. Format Output

Display a summary table:

```
**PR #<number>: <title>**

| Field | Value |
|-------|-------|
| URL | <url> |
| State | <OPEN/CLOSED/MERGED> |
| Draft | Yes/No |
| Base | `<baseRefName>` |
| Head | `<headRefName>` |
| Labels | <comma-separated labels> |
| Milestone | <milestone title or "None"> |

**CI Status:**

| Check | Status |
|-------|--------|
| <check name> | <status icon + status> |
...
```

### 3. Status Icons

Use these icons for CI status:

| Conclusion | Icon |
|------------|------|
| SUCCESS | ✅ |
| FAILURE | ❌ |
| IN_PROGRESS | ⏳ |
| QUEUED | ⏸️ |
| SKIPPED | ⏭️ |
| CANCELLED | 🚫 |
| NEUTRAL | ➖ |

### 4. Optional: Show Body

If user requests body (`/pr view --body` or `/pr view 218 --body`), also display the PR body after the tables.

## Output

Return formatted tables showing PR metadata and CI status.
