# Release Planning

See `.claude/skills/issue/conventions.md` for issue types, labels, and milestone conventions.

## Milestone Assignment

Every issue MUST be assigned to a milestone. Before assigning, query milestone descriptions to find the best thematic fit:

```bash
gh api repos/zeroae/zae-limiter/milestones --jq '.[] | "\(.title): \(.description)"'
```

Choose the milestone whose description best matches the issue - don't just pick the next version number. If no existing milestone fits the issue's theme, suggest a new milestone topic and ask the user before creating it.

## Project Scopes

Used for commit scopes and `area/*` labels:

| Scope | Area Label | Description |
|-------|------------|-------------|
| `limiter` | `area/limiter` | Core rate limiting logic |
| `cli` | `area/cli` | Command line interface |
| `infra` | `area/infra` | CloudFormation, IAM, infrastructure |
| `aggregator` | `area/aggregator` | Lambda aggregator function |
| `ci` | `area/ci` | CI/CD workflows |
| `local` | `area/local` | LocalStack local development commands |

**Additional commit-only scopes:** `bucket`, `models`, `schema`, `repository`, `lease`, `exceptions`, `cache`, `test`, `benchmark`
