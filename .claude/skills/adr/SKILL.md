---
description: Create and review Architecture Decision Records. Use `/adr create <title>` to create a new ADR, `/adr review` to check an existing one, `/adr audit` to validate code compliance, `/adr consistency` to check ADRs for conflicts, `/adr list` to show all ADRs, `/adr accept <number>` to mark as accepted, or `/adr supersede <old> <new>` to mark an ADR as superseded.
argument-hint: create <title> | review [file] | audit [--base <branch>] | consistency | list | accept <number> | supersede <old> <new>
allowed-tools: Glob, Grep, Read, Write, Edit, Bash, AskUserQuestion, Task
---

# ADR Skill

Create, review, and audit Architecture Decision Records.

**Format rules:** See `docs/adr/000-adr-format-standard.md` for size limits, required sections, and excluded content.

## Arguments

- `$ARGUMENTS`: One of:
  - `create <title>` - Create a new ADR
  - `review [file]` - Review an existing ADR
  - `audit [--base <branch>]` - Validate code against ADR decisions
  - `consistency` - Check ADRs for conflicts and ambiguities
  - `list` - List all ADRs with status and title
  - `accept <number>` - Transition ADR status to Accepted
  - `supersede <old> <new>` - Mark old ADR as superseded by new one

## Subcommand Instructions

Each subcommand has detailed instructions in a separate file:

| Command | Instructions |
|---------|--------------|
| `create` | See `create.md` |
| `review` | See `review.md` |
| `audit` | See `audit.md` |
| `consistency` | See `consistency.md` |
| `list` | See `list.md` |
| `accept` | See `accept.md` |
| `supersede` | See `supersede.md` |

Read the appropriate file based on the first argument, then follow its instructions.
