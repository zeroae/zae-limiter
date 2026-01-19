# ADR Format Rules

When creating or editing files in `docs/adr/`:

## Size Limits

- **Maximum 100 lines** per ADR
- If content exceeds this, suggest splitting into multiple ADRs or moving details elsewhere

## One Decision Per ADR

Each ADR documents exactly one architectural decision. If you find yourself writing about multiple decisions, split them:

- ❌ "Use flat schema and add caching and deprecate old API"
- ✅ ADR-001: "Use flat schema for config records"
- ✅ ADR-002: "Add client-side config caching"
- ✅ ADR-003: "Deprecate use_stored_limits parameter"

## Required Sections

Every ADR must have:

1. **Context** - What problem were we facing? (2-3 paragraphs max)
2. **Decision** - What did we decide? (1-2 sentences)
3. **Consequences** - What are the trade-offs? (bullet lists)
4. **Alternatives Considered** - What else was considered? (1 sentence rejection reason each)

## Excluded Content

Do NOT put these in an ADR—they belong in GitHub issues or design docs:

- Code examples or API signatures
- Implementation checklists
- Test cases to write
- Phased rollout plans
- Detailed cost calculations
- Benchmark specifications

## Linking

- Link to the GitHub issue that prompted the decision
- Link to implementation issues for details
- Keep the ADR itself focused on the "why"
