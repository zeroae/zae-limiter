---
description: Create and review Architecture Decision Records. Use `/adr create <title>` to create a new ADR, `/adr review` to check an existing one, `/adr enforce` to validate changes against ADRs in docs/adr/, `/adr list` to show all ADRs, `/adr accept <number>` to mark as accepted, or `/adr supersede <old> <new>` to mark an ADR as superseded.
argument-hint: create <title> | review [file] | enforce [--base <branch>] [--all] | list | accept <number> | supersede <old> <new>
allowed-tools: Glob, Grep, Read, Write, Edit, Bash, AskUserQuestion, Task
---

# ADR Skill

Create, review, and enforce Architecture Decision Records.

**Format rules:** See `docs/adr/000-adr-format-standard.md` for size limits, required sections, and excluded content.

## Arguments

- `$ARGUMENTS`: One of:
  - `create <title>` - Create a new ADR
  - `review [file]` - Review an existing ADR
  - `enforce [--base <branch>] [--all]` - Validate changes or entire codebase against all ADRs
  - `list` - List all ADRs with status and title
  - `accept <number>` - Transition ADR status to Accepted
  - `supersede <old> <new>` - Mark old ADR as superseded by new one

## Subcommand Instructions

Each subcommand has detailed instructions in a separate file:

| Command | Instructions |
|---------|--------------|
| `create` | See `create.md` |
| `review` | See `review.md` |
| `enforce` | See `enforce.md` |
| `list` | See `list.md` |
| `accept` | See `accept.md` |
| `supersede` | See `supersede.md` |

Read the appropriate file based on the first argument, then follow its instructions.
