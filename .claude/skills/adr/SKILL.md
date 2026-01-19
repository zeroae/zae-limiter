---
description: Create and review Architecture Decision Records. Use `/adr create <title>` to create a new ADR or `/adr review` to check an existing one.
argument-hint: create <title> | review [file]
allowed-tools: Glob, Read, Write, AskUserQuestion
---

# ADR Skill

Create and review Architecture Decision Records.

## Arguments

- `$ARGUMENTS`: Either `create <title>` or `review [file]`

## Create Mode

When arguments start with `create`:

1. List `docs/adr/` to find the next ADR number
2. Ask the user (via AskUserQuestion):
   - What problem are you solving? (Context)
   - What did you decide? (Decision - one sentence)
   - What alternatives did you reject and why?
3. Generate ADR under 100 lines with this structure:

```markdown
# ADR-NNN: <Title>

**Status:** Proposed
**Date:** <today>
**Issue:** [#NNN](link) (if applicable)

## Context

<2-3 paragraphs max>

## Decision

<1-2 sentences>

## Consequences

**Positive:**
- <benefit>

**Negative:**
- <trade-off>

## Alternatives Considered

### <Alternative 1>
Rejected because: <one sentence>
```

4. Do NOT include: code examples, checklists, API signatures, test cases, cost calculations

## Review Mode

When arguments start with `review`:

1. If no file specified, list `docs/adr/` and ask which to review
2. Check the ADR against these criteria:

| Check | Pass Criteria |
|-------|---------------|
| Length | Under 100 lines |
| Single decision | One architectural decision, not multiple |
| No implementation | No code, checklists, test cases |
| Clear decision | Decision section is 1-2 sentences |
| Required sections | Context, Decision, Consequences, Alternatives |

3. Report as checklist with specific fix suggestions
