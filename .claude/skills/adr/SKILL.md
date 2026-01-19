---
description: Create and review Architecture Decision Records. Use `/adr create <title>` to create a new ADR, `/adr review` to check an existing one, or `/adr enforce` to validate changes against architectural decisions. Invoke `/adr enforce` before implementing significant changes to core modules or architecture.
argument-hint: create <title> | review [file] | enforce
allowed-tools: Glob, Read, Write, AskUserQuestion
---

# ADR Skill

Create, review, and enforce Architecture Decision Records.

## Arguments

- `$ARGUMENTS`: One of:
  - `create <title>` - Create a new ADR
  - `review [file]` - Review an existing ADR
  - `enforce` - Validate current changes against all ADRs

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

## Enforce Mode

When arguments are `enforce` (or empty and invoked proactively):

1. Read all ADRs from `docs/adr/` (glob `*.md`)
2. For each ADR, extract:
   - Title and number
   - The **Context** (what problem it addresses)
   - The **Decision** (the constraint to enforce)
3. Compare each ADR's context against the current task/changes
4. For ADRs whose context is relevant, check if the proposed approach aligns with the Decision
5. Report findings:

**Output format:**

```
## ADR Enforcement Check

### Relevant ADRs for this change:
- ADR-NNN: <title> - ✅ Compliant | ⚠️ Review needed | ❌ Violation

### Details:
<For any non-compliant items, explain the concern and suggest fixes>

### Not applicable:
<List ADRs checked but not relevant to this change>
```

6. If a change appears to violate an ADR, ask the user if they want to:
   - Adjust the implementation to comply
   - Create a new ADR to supersede the old decision
