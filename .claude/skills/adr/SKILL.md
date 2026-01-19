---
description: Create and review Architecture Decision Records. Use `/adr create <title>` to create a new ADR, `/adr review` to check an existing one, `/adr enforce` to validate changes against ADRs in docs/adr/, `/adr list` to show all ADRs, `/adr accept <number>` to mark as accepted, or `/adr supersede <old> <new>` to mark an ADR as superseded.
argument-hint: create <title> | review [file] | enforce [--brief] [--base <branch>] [--all] | list | accept <number> | supersede <old> <new>
allowed-tools: Glob, Grep, Read, Write, Edit, Bash, AskUserQuestion
---

# ADR Skill

Create, review, and enforce Architecture Decision Records.

**Format rules:** See `template.md` in this skill directory for size limits, required sections, and excluded content.

## Arguments

- `$ARGUMENTS`: One of:
  - `create <title>` - Create a new ADR
  - `review [file]` - Review an existing ADR
  - `enforce [--brief] [--base <branch>] [--all]` - Validate changes or entire codebase against all ADRs
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
5. Generate ADR following `template.md` with this structure:

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
2. Check the ADR against criteria from `template.md`:

| Check | Pass Criteria |
|-------|---------------|
| Length | Under 100 lines |
| Single decision | One architectural decision, not multiple |
| No implementation | No code, checklists, test cases |
| Clear decision | Decision section is 1-2 sentences |
| Required sections | Context, Decision, Consequences, Alternatives |

3. Report as checklist with specific fix suggestions

## Enforce Mode

When arguments are `enforce`, `enforce --brief`, `enforce --base <branch>`, or `enforce --all`:

**Options:**
- `--brief`: Concise output showing only files with violations (default: verbose)
- `--base <branch>`: Specify base branch for comparison (diff mode)
- `--all`: Scan entire codebase instead of just changed files (full scan mode)

**Mode selection:**
- `--all` and `--base` are mutually exclusive
- If `--all` is specified, skip branch detection and scan all source files
- If `--base` is specified (or detected), compare against that branch

### Step 1: Determine scan mode

**If `--all` is specified:**
- Skip to Step 2b (full scan)
- Report: "Scan mode: full codebase"

**Otherwise (diff mode):**
- Continue with branch detection below
- Use this priority order to find the correct base branch:

1. **Explicit argument**: If `--base <branch>` is provided, use it
2. **PR context**: Run `gh pr view --json baseRefName -q .baseRefName` - if successful, use the PR's base branch
3. **GitHub Actions env**: If `GITHUB_BASE_REF` environment variable is set, use it
4. **Repository default**: Run `gh repo view --json defaultBranchRef -q .defaultBranchRef.name` to get the default branch

Report which detection method was used: "Base branch: `<branch>` (detected from: <method>)"

### Step 2a: Get the diff (diff mode)

Run `git diff origin/<base>...HEAD` to identify changed files and understand the current changes.

If the base branch doesn't exist locally, fetch it first: `git fetch origin <base>`

### Step 2b: Get all source files (full scan mode)

When `--all` is specified:

1. Glob `src/**/*.py` to find all Python source files
2. Also include other relevant files: `tests/**/*.py`, configuration files
3. Read file contents for analysis against ADR constraints

This mode is useful for:
- **Auditing**: Check entire codebase against all ADRs
- **Onboarding**: Understand how ADRs apply to existing code
- **Refactoring**: Verify compliance before major changes

### Step 3: Read and analyze ADRs

1. Read all ADRs from `docs/adr/` (glob `*.md`)
2. For each ADR, extract:
   - Title and number
   - The **Context** (what problem it addresses)
   - The **Decision** (the constraint to enforce)
3. Compare each ADR's context against the files being checked (diff or full scan)
4. For ADRs whose context is relevant, check if the code aligns with the Decision

### Step 4: Report findings

**Brief output format** (when `--brief` is specified):

Show only violations grouped by file. If no violations, output a single success line.

```
## ADR Enforcement: ✅ All clear | ❌ N violations

| File | Violation | ADR |
|------|-----------|-----|
| src/foo.py | Uses ABC instead of Protocol | ADR-108 |
| src/bar.py | Missing capability declaration | ADR-109 |
```

If all compliant:
```
## ADR Enforcement: ✅ All clear (checked N ADRs against M files)
```

For full scan mode, append scan type:
```
## ADR Enforcement: ✅ All clear (checked N ADRs against M files, full scan)
```

**Verbose output format** (default, when `--brief` is NOT specified):

```
## ADR Enforcement Check

**Scan mode:** `diff` (base: <branch>) | `full codebase`

### Relevant ADRs for this change:
- ADR-NNN: <title> - ✅ Compliant | ⚠️ Review needed | ❌ Violation

### Details:
<For any non-compliant items, explain the concern and suggest fixes>

### Not applicable:
<List ADRs checked but not relevant to this change>
```

### Step 5: Handle violations

If a change appears to violate an ADR, ask the user if they want to:
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
