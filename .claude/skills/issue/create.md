# Create Mode

Create a new GitHub issue following ZeroAE conventions.

## Process

1. **Gather Context**: Analyze conversation for problem, solution, files discussed
2. **Infer or Ask**: Use inference first, batch questions if multiple fields ambiguous
3. **Build Issue Body**: Use template from [templates.md](templates.md)
4. **Confirm and Create**: Show preview, then create

```bash
gh issue create \
  --title "<emoji> <Title>" \
  --body "$(cat <<'EOF'
<body from template>
EOF
)" \
  --label "<area/label>" \
  --milestone "<milestone>"
```

5. **Set Issue Type**:
```bash
gh api -X PATCH repos/{owner}/{repo}/issues/<number> -f type=<Type>
```

## Context Inference

### Type Inference

Infer both the GitHub type AND the most specific gitmoji:

| Context Clues | Emoji | GitHub Type |
|---------------|-------|-------------|
| "bug", "broken", "error", "fix", "crash", "fails", "doesn't work" | 🐛 | Bug |
| "security", "vulnerability", "CVE", "exploit", "auth bypass" | 🔒 | Bug |
| "add", "new", "feature", "implement", "support", "enable" | ✨ | Feature |
| "performance", "optimize", "faster", "slow", "latency" | ⚡ | Feature |
| "breaking change", "deprecate", "remove API" | 💥 | Feature |
| "docs", "documentation", "readme", "write docs" | 📝 | Task |
| "test", "coverage", "add tests", "unit test", "e2e" | ✅ | Task |
| "refactor", "cleanup", "simplify", "restructure" | ♻️ | Chore |
| "upgrade", "deps", "dependencies", "bump", "update package" | ⬆️ | Chore |
| "ci", "workflow", "actions", "pipeline", "build system" | 👷 | Chore |
| "config", "settings", "configuration" | 🔧 | Chore |
| "remove", "delete", "drop", "prune" | 🔥 | Chore |
| "epic", "major feature", "multi-issue", "spanning" | 🎯 | Epic |
| "theme", "strategic", "initiative", "long-term" | 🎨 | Theme |

> **Tip:** If user mentions "release", "cut release", or "release prep", redirect to `/pr release <version>`.

### Label Inference

Infer `area/` labels from context:
- Files mentioned → extract component (e.g., `src/cli.py` → `area/cli`)
- Topics discussed → map to area (e.g., "deployment" → `area/infra`)
- Available areas: `area/limiter`, `area/cli`, `area/infra`, `area/aggregator`, `area/ci`

### Milestone Inference

Query milestones and match by theme:
```bash
gh api repos/{owner}/{repo}/milestones --jq '.[] | "\(.title): \(.description)"'
```

**IMPORTANT:** Type and milestone are MANDATORY. Never create an issue without both.

## Output

Return the new issue URL.
