# API/CLI Parity Rule

When implementing user-facing features, ensure parity between the Python API and CLI interfaces.

## When to Check Parity

**After adding:**
- New public methods to `RateLimiter` or `SyncRateLimiter`
- New CLI commands or subcommands
- New query/reporting functionality
- New administrative operations

**Use the `api-cli-parity` agent to:**
- Audit for gaps between interfaces
- Implement missing counterparts
- Verify naming and parameter consistency

## Quick Reference

| Feature Type | Needs API? | Needs CLI? |
|--------------|------------|------------|
| Infrastructure ops | ✅ | ✅ |
| Data queries | ✅ | ✅ |
| Admin actions | ✅ | ✅ |
| Runtime limiting | ✅ | ❌ |
| Interactive utilities | ❌ | ✅ |
| Template exports | ❌ | ✅ |

## Naming Conventions

Keep names aligned between interfaces:

| API Method | CLI Command |
|------------|-------------|
| `get_status()` | `zae-limiter status` |
| `get_audit_events()` | `zae-limiter audit list` |
| `delete_entity()` | `zae-limiter entity delete` |

## Workflow

1. Implement the feature in the primary interface (usually API first)
2. Run `api-cli-parity` agent to check if counterpart is needed
3. Implement counterpart if recommended
4. Run `docs-updater` agent to update documentation for both
