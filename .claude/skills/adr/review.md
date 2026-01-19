# Review Mode

When arguments start with `review`:

1. If no file specified, list `docs/adr/` and ask which to review
2. Read `docs/adr/000-adr-format-standard.md` to get the format rules
3. Check the target ADR against ADR-000's Decision section:

| Check | Pass Criteria (from ADR-000) |
|-------|------------------------------|
| Length | Under 100 lines |
| Single decision | One architectural decision, not multiple |
| No implementation | No code, checklists, test cases, rollout plans, cost calculations |
| Clear decision | Decision section is 1-2 sentences |
| Required sections | Context, Decision, Consequences, Alternatives Considered |

4. Report as checklist with specific fix suggestions
