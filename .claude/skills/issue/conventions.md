# Issue Conventions

Reference documentation for GitHub issue management in ZeroAE projects.

## Issue Types

Types classify the fundamental nature of work and map to conventional commit types:

| Type | Commit Types | Purpose |
|------|--------------|---------|
| Epic | N/A | Release narratives, multi-issue initiatives |
| Feature | `feat`, `perf` | New functionality, enhancements |
| Bug | `fix`, `security` | Defects, unexpected behavior |
| Task | `docs`, `test` | Specific deliverables |
| Chore | `refactor`, `chore`, `ci`, `build`, `deps` | Maintenance, internal work |

## Issue Titles

Use gitmoji prefix with capitalized description. Any gitmoji from conventional commits is valid:

| Gitmoji | Commit Type | GitHub Type | Example |
|---------|-------------|-------------|---------|
| ✨ | feat | Feature | `✨ Add health_check method` |
| 🐛 | fix | Bug | `🐛 Fix asyncio deprecation warning` |
| 📝 | docs | Task | `📝 Update migration docs` |
| ♻️ | refactor | Chore | `♻️ Simplify bucket calculation` |
| ⚡ | perf | Feature | `⚡ Optimize DynamoDB queries` |
| ✅ | test | Task | `✅ Add integration tests for lease` |
| 🔧 | chore | Chore | `🔧 Update ruff configuration` |
| 👷 | ci | Chore | `👷 Add Python 3.13 to CI matrix` |
| 🔒 | security | Bug | `🔒 Fix IAM permission escalation` |
| ⬆️ | deps | Chore | `⬆️ Upgrade boto3 to 1.35` |
| 🔥 | remove | Chore | `🔥 Remove deprecated v1 API` |
| 💥 | breaking | Feature | `💥 Change default cascade behavior` |
| 🎯 | (epic) | Epic | `🎯 v0.9.0: API Polish` |
| 🎨 | style | Theme | `🎨 Consistent error handling` |

See [commits.md](../../rules/commits.md) for the complete gitmoji list.

## Labels

### Area Labels

Use `area/` prefix for component labels. Project-specific areas are defined in each project's `CLAUDE.md`.

**zae-limiter areas:** `area/limiter`, `area/cli`, `area/infra`, `area/aggregator`, `area/ci`

### Attribute Labels

Common attributes across projects:

| Label | Purpose |
|-------|---------|
| `performance` | Performance optimization |
| `api-design` | API surface changes |
| `documentation` | Docs improvements |
| `testing` | Test coverage |
| `security` | Security improvements |
| `breaking` | Breaking change |
| `good first issue` | Good for newcomers |
| `help wanted` | Extra attention needed |

## Milestone Assignment

Every issue MUST be assigned to a milestone. Query milestone descriptions to find the best thematic fit:

```bash
gh api repos/{owner}/{repo}/milestones --jq '.[] | "\(.title): \(.description)"'
```

Choose the milestone whose description best matches the issue—don't just pick the next version number.

## Creating Issues via CLI

```bash
# 1. Create issue with labels and milestone
gh issue create \
  --title "✨ Add new feature" \
  --body "Description here" \
  --label "area/foo" \
  --milestone "vX.Y.Z"

# 2. Set the issue type (gh issue create doesn't support --type)
gh api -X PATCH repos/{owner}/{repo}/issues/{number} -f type=Feature
```

Valid type values: `Epic`, `Feature`, `Bug`, `Task`, `Chore`
