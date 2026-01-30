# List Mode

Run this command now:

```bash
gh api repos/zeroae/zae-limiter/milestones --jq '.[] | "- \(.title): \(.description)"'
```

Output the results to the user.
