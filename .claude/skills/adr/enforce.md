# Enforce Mode

When arguments are `enforce`, `enforce --base <branch>`, or `enforce --all`:

**Options:**
- `--base <branch>`: Specify base branch for comparison (diff mode)
- `--all`: Scan entire codebase instead of just changed files (full scan mode)
- `NNN`: Optional ADR number to enforce only that specific ADR (e.g., `enforce 013`)

**Use Task with Explore agent in background mode** (`run_in_background: true`). This hides intermediate tool calls from the user's context.

After launching, wait for completion using `TaskOutput`, then present the result.

Launch the Explate

For each agent (sequential or parallel), use this prompt:

```
Enforce ADR-NNN compliance for this codebase.

**ADR:** docs/adr/NNN-title.md
**Mode:** [diff against <branch> | full scan]
**Files to check:** [list of files or "all src/**/*.py"]

Instructions:
1. Read the ADR and extract the Decision section
2. Check if the specified files comply with the Decision
3. Return findings in this format:

| File | Line | ADR | Violation |
|------|------|-----|-----------|
```

## Output Format

After all agents complete, merge results and output:

```
## ADR Enforcement: ✅ All clear | ❌ N violations
| File | Line | ADR | Violation |
|------|------|-----|-----------|
```

If violations are found, ask if the user wants to:
- Adjust the implementation to comply
- Create a new ADR to supersede the old decision
