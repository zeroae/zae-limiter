# Create Mode

When arguments start with `create`:

1. List `docs/adr/` to find the next ADR number
2. Gather context using this priority order:
   a. **GitHub issue**: If an issue number is in arguments (e.g., `create #123 Flat Schema`), run `gh issue view <number> --json title,body` to fetch details
   b. **Conversation context**: Review the current conversation for architectural discussions, decisions made, alternatives rejected, or problems solved
   c. **Ask the user**: Only if (a) and (b) provide insufficient context, ask via AskUserQuestion:
      - What problem are you solving? (Context)
      - What did you decide? (Decision - one sentence)
      - What alternatives did you reject and why?
3. Draft the ADR by inferring:
   - **Context**: From problem statements, bug reports, or "we need to..." discussions
   - **Decision**: From conclusions like "let's use X", "we'll go with Y", or implementation choices
   - **Alternatives**: From rejected options discussed ("we considered X but...")
4. Present the draft ADR to the user for confirmation before writing
5. Generate ADR following `docs/adr/000-adr-format-standard.md` with this structure:

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

6. Do NOT include: code examples, checklists, API signatures, test cases, cost calculations
