# List Mode

When arguments are `list`:

1. Glob `docs/adr/*.md` to find all ADRs
2. For each ADR, extract:
   - Number (from filename)
   - Title (from `# ADR-NNN: <title>` heading)
   - Status (from `**Status:**` line)
3. Output as a table:

```
| ADR | Title | Status |
|-----|-------|--------|
| 001 | <title> | Accepted |
| 002 | <title> | Proposed |
| 003 | <title> | Superseded by ADR-005 |
```
