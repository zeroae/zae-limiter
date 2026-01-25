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
Indicates the affected component. Project-specific scopes are defined in CLAUDE.md.

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

## Best Practices

1. **Commit often**: Make small, focused commits
2. **One concern per commit**: Don't mix refactoring with features
3. **Test before committing**: Ensure tests pass
4. **Reference issues**: Link commits to issues/PRs when relevant
