# ADR-104: Stored Limits as Default Behavior

**Status:** Accepted
**Date:** 2026-01-18
**Issue:** [#130](https://github.com/zeroae/zae-limiter/issues/130)

## Context

Currently, `acquire()` requires explicit limits or opt-in via `use_stored_limits=True`:

```{.python .lint-only}
async with limiter.acquire(
    entity_id="user-123",
    resource="gpt-4",
    limits=[Limit.per_minute("tpm", 10000)],  # Required
    use_stored_limits=False,  # Default
):
    pass
```

This creates friction: users must pass limits on every call or remember to opt-in. With centralized config (ADR-102), stored limits become the natural default.

## Decision

Change the default behavior: **always resolve limits from stored config** (System > Resource > Entity hierarchy).

**Resolution order:**
1. Entity config → if exists, use it
2. Resource config → if exists, use it
3. System config → fallback
4. Error if no config found anywhere

**Backward compatibility:**
- `limits` parameter accepted as override (useful for testing, migration)
- `use_stored_limits=False` deprecated with warning in v0.5.0, removed in v1.0

## Consequences

**Positive:**
- Simpler API: no limits parameter needed in common case
- Centralized control: ops can change limits without code deployment
- Consistent behavior: all clients use same stored config

**Negative:**
- Breaking change for users relying on explicit limits only
- Requires config to be set up before use (or `limits` override)

## Alternatives Considered

### Keep Opt-In (`use_stored_limits=True`)
Rejected: Adds friction; stored config is the better default now that hierarchy exists.

### Remove `limits` Parameter Entirely
Rejected: Useful for testing and gradual migration; keep as override option.
