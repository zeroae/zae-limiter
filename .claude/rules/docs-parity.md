# Documentation Parity Rule

After implementing code changes, ensure documentation stays synchronized by invoking the `docs-updater` agent.

## When to Invoke docs-updater

**Always invoke after:**
- Adding or modifying public API methods (RateLimiter, Limit, Entity, etc.)
- Adding or modifying CLI commands or flags
- Adding new exceptions or error types
- Changing infrastructure (CloudFormation, deployment process)
- Modifying DynamoDB schema or access patterns
- Adding new models or configuration options

**Skip for:**
- Internal refactoring that doesn't change public interfaces
- Test-only changes
- CI/CD workflow changes (unless they affect user workflows)

## Parity Checklist

For any user-facing feature, verify coverage across:

| Layer | Location | Example |
|-------|----------|---------|
| Python API | `src/zae_limiter/` | `limiter.get_audit_events()` |
| CLI | `src/zae_limiter/cli.py` | `zae-limiter audit list` |
| API Docs | `docs/api/` | Method signature and examples |
| CLI Docs | `docs/cli.md` | Command usage and flags |
| User Guide | `docs/guide/` | When/why to use the feature |
| CLAUDE.md | Project root | Developer reference |

## Workflow

1. Implement the feature (API and/or CLI)
2. Run the `docs-updater` agent to synchronize documentation
3. Review the agent's changes before committing
