---
name: issue
description: Create and update GitHub issues. Triggers on "create issue", "file issue", "open issue", "new issue", "report bug", "request feature", "track this", "create epic", "create theme", "update issue", "check off", "mark complete", "close issue", "verify issue", "verify #". Infers type, labels, and milestone from conversation context.
allowed-tools: Bash(gh:*), AskUserQuestion, Grep, Read
user-invocable: true
argument-hint: verify <number> [--all] [--dry-run]
---

# Issue Skill

Create, update, and manage GitHub issues following ZeroAE conventions. Infer as much as possible from context; only ask when ambiguous.

## Modes

| Mode | Trigger | Purpose |
|------|---------|---------|
| **Create** | "create issue", "file issue", "new issue", "report bug", "request feature" | Create new issue |
| **Update** | "update issue", "add to issue", "update #123" | Modify existing issue |
| **Progress** | "check off", "mark complete", "done with", "finished" | Check checkboxes based on work done |
| **Verify** | `/issue verify <number> [--all] [--dry-run]` | Validate unchecked criteria, prompt to check off |

## Mode Detection

1. **If `verify` keyword** (`/issue verify 150`) → **Verify mode**
2. **If update phrases** ("update issue", "add to #123") → **Update mode**
3. **If progress phrases** ("check off", "mark complete") → **Progress mode**
4. **Otherwise** → **Create mode**

## Supported Issue Types

GitHub supports 5 issue types. The title emoji can be any gitmoji:

| GitHub Type | Canonical Emoji | Alternative Emojis | Use For |
|-------------|-----------------|-------------------|---------|
| Bug | 🐛 | 🔒 (security) | Defects, unexpected behavior |
| Feature | ✨ | ⚡ (perf), 💥 (breaking) | New functionality, enhancements |
| Task | 📋 | 📝 (docs), ✅ (test) | Documentation, testing, specific work items |
| Chore | 🔧 | ♻️ (refactor), ⬆️ (deps), 👷 (ci), 🔥 (remove) | Maintenance: refactor, deps, ci, cleanup |
| Epic | 🎯 | - | Major feature spanning multiple issues |
| Theme | 🎨 | - | Strategic initiative spanning epics |

See [conventions.md](conventions.md) for full gitmoji-to-type mapping.

> **Note:** For release preparation, use `/pr release <version>` to create a Release Prep PR.

## Reference Files

- [create.md](create.md) - Create issue mode
- [update.md](update.md) - Update issue mode
- [progress.md](progress.md) - Progress tracking mode
- [verify.md](verify.md) - Acceptance criteria verification mode
- [templates.md](templates.md) - Issue body templates for each type
- [conventions.md](conventions.md) - Label taxonomy, title formatting, milestone rules
