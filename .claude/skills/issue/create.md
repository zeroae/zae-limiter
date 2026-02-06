# Create Mode

Create a new GitHub issue following ZeroAE conventions.

## Process

1. **Mine Conversation**: Systematically extract details (see [Conversation Mining](#conversation-mining))
2. **Infer or Ask**: Use inference first, batch questions if multiple fields ambiguous
3. **Build Issue Body**: Use template from [templates.md](templates.md), enriched with mined details
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

## Acceptance Criteria Guidelines

When writing acceptance criteria, ensure each criterion is **objectively verifiable**. Avoid subjective language that requires judgment calls.

### Red Flags (Subjective)

| Subjective Phrase | Problem |
|-------------------|---------|
| "where beneficial" | Who decides what's beneficial? |
| "as appropriate" | Undefined threshold |
| "improved performance" | No measurable target |
| "clean code" | Style is subjective |
| "well-documented" | No specific requirement |
| "reasonable" / "acceptable" | Undefined standards |

### Rewrite Patterns

| ❌ Subjective | ✅ Objective |
|---------------|--------------|
| "Projection expressions used where beneficial" | "Projection expressions fetch only attributes necessary and sufficient for execution without changing unit tests" |
| "Performance is improved" | "p99 latency ≤ baseline" or "No regression in p99 latency" |
| "Code is well-tested" | "Unit tests cover new methods" or "Coverage ≥ 80%" |
| "Documentation updated as needed" | "CLAUDE.md updated with new access patterns" |
| "Error handling is appropriate" | "RateLimitExceeded raised when tokens < 0" |

### Verification Test

For each criterion, ask: **Can this be verified with grep, pytest, or a measurable metric?**

If not, rewrite it to specify:
- **What** exactly must exist/change
- **Where** it must be (file, function, config)
- **How** to verify (test passes, grep finds it, metric threshold)

### Ask User for Subjective Criteria

If any acceptance criterion contains subjective language, **do not create the issue**. Instead, use `AskUserQuestion` to propose objective alternatives:

```
Criterion: "Projection expressions used where beneficial"

This criterion is subjective ("where beneficial" requires judgment).

How would you like to make it objective?
- "Projection expressions fetch only attributes necessary and sufficient for execution without changing unit tests"
- "Projection expressions used in all GetItem calls"
- "Projection expressions reduce item size by ≥50%"
- Other (specify)
```

**Rules:**
- Scan all criteria before creating the issue
- Flag ALL subjective criteria in a single question (batch them)
- Provide 2-3 concrete alternatives based on context
- Only proceed to create after user confirms objective rewrites

## Conversation Mining

Before building the issue body, scan the full conversation and extract every item in the checklist below. Each category maps to a section or detail in the issue body. **If a category was discussed but is missing from your draft, the draft is incomplete.**

### Extraction Checklist

| Category | What to Look For | Where It Goes |
|----------|------------------|---------------|
| **Correctness / Safety** | "why this is safe", "never over-admits", "invariant", "guarantee" | Dedicated subsection in Proposed Solution |
| **Concrete API / Schema** | Code snippets, expressions, attribute names, wire format | Code blocks in Proposed Solution |
| **Decision Tree / Flow** | "if X then Y", branching logic, happy path vs fallback | Diagram (code block or mermaid) in Proposed Solution |
| **Cost / Performance** | RCU, WCU, round trips, latency — both happy AND worst case | Tables in Problem or Proposed Solution |
| **Edge Cases** | "what if", first-time, missing data, race conditions | Listed in Proposed Solution or Acceptance Criteria |
| **Invariants / Assumptions** | "X is immutable", "Y never changes", "Z defaults to false" | Called out where they justify design decisions |
| **User Decisions** | User explicitly said "yes", "not a big deal", "let's do X" | Woven into design decisions, not lost |
| **Self-Correcting Behaviors** | "falls back to", "eventually", "self-correcting" | Noted alongside fallback costs |
| **Alternatives Rejected** | "we considered", "rejected because", user chose A over B | Alternatives Considered section |
| **Dependencies / Prerequisites** | "depends on", "after #X", "requires" | Dependencies section |

### Process

1. **First pass**: Read the full conversation and tag each substantive message with the categories above
2. **Second pass**: For each category that has content, draft the corresponding section
3. **Completeness check**: Verify every tagged item appears in the draft. If a discussed topic is missing, add it
4. **User decisions are authoritative**: When the user made an explicit decision ("yes, we should do that", "not a big deal"), that decision MUST appear in the issue body — these are the most important details to preserve

### Common Gaps to Watch For

These are the details most often lost when summarizing a design conversation:

- **Why something is safe** (not just what it does) — correctness arguments are critical for reviewers
- **Worst-case costs** — drafts tend to only include happy-path numbers
- **Concrete syntax** — the actual UpdateExpression, ConditionExpression, SQL, etc. discussed
- **Edge cases and their handling** — "what happens on first call", "what if the item doesn't exist"
- **Assumptions that make the design work** — "cascade is immutable" is load-bearing context

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
