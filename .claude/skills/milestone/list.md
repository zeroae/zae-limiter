# List Mode

When arguments are `list` or empty:

List all open milestones with their descriptions:

```bash
gh api repos/zeroae/zae-limiter/milestones --jq '.[] | "- \(.title): \(.description)"'
```

Output the list directly to the user.
