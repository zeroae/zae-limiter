# Commit Conventions

All commits follow the [Conventional Commits](https://www.conventionalcommits.org/) format with [gitmoji](https://gitmoji.dev/) emojis.

## Format

```
<emoji> <type>(<scope>): <description>

[optional body]

[optional footer(s)]
```

## Gitmoji Mappings

| Emoji | Type | Description |
|-------|------|-------------|
| ✨ | `feat` | Introduce new features |
| 🐛 | `fix` | Fix a bug |
| 📝 | `docs` | Add or update documentation |
| 🎨 | `style` | Improve structure/format of code |
| ♻️ | `refactor` | Refactor code |
| ⚡ | `perf` | Improve performance |
| ✅ | `test` | Add or update tests |
| 🔧 | `chore` | Add or update configuration files |
| 🔨 | `build` | Add or update development scripts |
| 👷 | `ci` | Add or update CI build system |
| 🔒 | `security` | Fix security issues |
| ⬆️ | `deps` | Upgrade dependencies |
| 🔥 | `remove` | Remove code or files |
| ⏪️ | `revert` | Revert changes |
| 💥 | `breaking` | Introduce breaking changes |

**Additional useful gitmojis:** 🚑️ (hotfix), 🚧 (WIP), 💚 (fix CI), 🩹 (simple fix), 🏗️ (architecture), ✏️ (typos)

See [gitmoji.dev](https://gitmoji.dev/) for the complete list.

## Guidelines

### Type (Required)
Always specify a type that communicates the intent of the change.

### Scope (Optional)
Indicates the affected component. Project-specific scopes are defined in `release-planning.md`.

### Description (Required)
- Use **imperative mood**: "add feature" not "added feature"
- Keep first line **≤72 characters**
- Start with lowercase (after the type)

### Body (Optional)
Explain **why** the change was made and provide context.

### Footer (Optional)
- **Breaking changes**: `BREAKING CHANGE: description`
- **Issue references**: `Fixes #123`, `Closes #456`
- **Co-authors**: `Co-Authored-By: Name <email>`

### Breaking Changes
Indicate with `!` after the type/scope:
```
✨ feat(api)!: remove deprecated v1 endpoints

BREAKING CHANGE: All v1 endpoints removed. Use v2 API instead.
```

## Fixes to Pre-existing Behaviour Get Their Own Commit

When work on a feature uncovers a bug that **already existed** on `main`, the fix must be its
own `fix(scope):` commit with a `Fixes #NNN` footer. Never fold it into the `feat:` commit that
happened to expose it.

**Why this is a rule and not a preference.** `git-cliff` builds the changelog by parsing commit
messages, and nothing else. Verified against git-cliff 2.13.1: the template context exposes
`release.github.contributors` and `commit.github.{pr_number, pr_title, pr_labels, username}` —
and **no issues collection at any level**. A closed issue with no commit of its own does not
appear in the changelog, however carefully it is written, labelled, or milestoned. The commit
message is the only channel.

**What happens when the rule is broken.** During #222, the aggregator was found to be dividing
the reserved `wcu` limit by `shard_count`, collapsing the per-partition write ceiling on exactly
the hot buckets sharding exists to protect — a bug predating #222 entirely. The fix was folded
into `fa879d2e ✨ feat(aggregator): refill at the scheduled rate and re-stamp vu`. It is
therefore filed under **Features** in the v0.14.0 changelog, and a user running sharded buckets
has no way to learn from the release notes that the bug existed or was fixed. Issue #519 was
filed retroactively, but that changes nothing — git-cliff never reads it. The commit is merged
and immutable, so this one cannot be recovered.

Compare #518, the same situation handled correctly: its fix landed as
`🐛 fix(models): report the scheduled, per-shard capacity in rejections`, so it appears under
**Bug Fixes** where someone auditing their own limits will find it.

**In practice:** when a task brief says "if you find a defect, fix it and say so explicitly",
that fix is a separate commit. Splitting costs one `git add -p`; not splitting is unrecoverable
after merge.

## Best Practices

1. **Commit often**: Make small, focused commits
2. **One concern per commit**: Don't mix refactoring with features
3. **Test before committing**: Ensure tests pass
4. **Reference issues**: Link commits to issues/PRs when relevant
