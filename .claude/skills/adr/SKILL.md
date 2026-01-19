---
description: Create and review Architecture Decision Records. Use `/adr create <title>` to create a new ADR, `/adr review` to check an existing one, `/adr enforce` to validate changes against ADRs in docs/adr/, `/adr list` to show all ADRs, `/adr accept <number>` to mark as accepted, or `/adr supersede <old> <new>` to mark an ADR as superseded.
argument-hint: create <title> | review [file] | enforce | list | accept <number> | supersede <old> <new>
allowed-tools: Glob, Grep, Read, Write, Edit, Bash, AskUserQuestion
---

# ADR Skill

Create, review, and enforce Architecture Decision Records.

**Format rules:** See `template.md` in this skill directory for size limits, required sections, and excluded content.

## Arguments

- `$ARGUMENTS`: One of:
  - `create <title>` - Create a new ADR
  - `review [file]` - Review an existing ADR
  - `enforce` - Validate current changes against all ADRs
  - `list` - List all ADRs with status and title
  - `accept <number>` - Transition ADR status to Accepted
  - `supersede <old> <new>` - Mark old ADR as superseded by new one

## Create Mode

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
5. Generate ADR following `.claude/rules/adr-format.md` with this structure:

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
2. Check the ADR against criteria from `.claude/rules/adr-format.md`:

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

1. Run `git diff HEAD` to identify changed files and understand the current changes
2. Read all ADRs from `docs/adr/` (glob `*.md`)
3. For each ADR, extract:
   - Title and number
   - The **Context** (what problem it addresses)
   - The **Decision** (the constraint to enforce)
4. Compare each ADR's context against the changes from `git diff`
5. For ADRs whose context is relevant, check if the proposed approach aligns with the Decision
6. Report findings:

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

7. If a change appears to violate an ADR, ask the user if they want to:
   - Adjust the implementation to comply
   - Create a new ADR to supersede the old decision

## List Mode

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

## Accept Mode

When arguments start with `accept`:

1. Parse the ADR number from arguments (e.g., `accept 5` or `accept 005`)
2. Read the ADR file `docs/adr/NNN-*.md`
3. Use Edit to change `**Status:** Proposed` to `**Status:** Accepted`
4. Confirm the change to the user

## Supersede Mode

When arguments are `supersede <old> <new>`:

1. Parse both ADR numbers from arguments
2. Read the old ADR file
3. Use Edit to change status to `**Status:** Superseded by ADR-<new>`
4. Read the new ADR file
5. If the new ADR doesn't mention superseding, use Edit to add `**Supersedes:** ADR-<old>` after the Status line
6. Confirm both changes to the user
