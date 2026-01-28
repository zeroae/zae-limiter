# Stored Config as Default

**Date:** 2026-01-28
**Status:** Approved

## Problem

Documentation currently emphasizes passing `limits=[...]` to every `acquire()` call, but stored config (System > Resource > Entity hierarchy) should be the default approach. Additionally, requiring `limits=None` explicitly is cumbersome.

## Decision

### API Change

Make `limits=None` the default in `acquire()`:

```python lint-only
async def acquire(
    self,
    entity_id: str,
    resource: str,
    consume: dict[str, int],
    limits: list[Limit] | None = None,  # defaults to None
    ...
):
```

**Behavior when `limits=None`:**
- Resolve limits from stored config (Entity > Resource > System)
- If no config exists at any level, raise `ValidationError` with helpful message

**Files to change:**
- `src/zae_limiter/limiter.py` — `RateLimiter.acquire()`
- `src/zae_limiter/sync_limiter.py` — `SyncRateLimiter.acquire()`

### Documentation Restructure

**Scope:** Key entry points only (`getting-started.md`, `guide/basic-usage.md`)

**Structure for `getting-started.md` Quick Start:**

1. **Minimalist** — Code-only example with inline `limits=[...]` for scripts/demos
2. **Stored Config (Recommended)** — Tabbed CLI/Python setup, then simple app code

**Structure for `guide/basic-usage.md`:**

1. Lead with stored config (simple `acquire()` without limits)
2. Brief hierarchy explanation (Entity > Resource > System)
3. Inline limits as override pattern

**Files NOT changed:** `guide/hierarchical.md`, `guide/llm-integration.md` — inline examples remain for teaching self-contained concepts.

## Implementation Plan

1. Update `limiter.py` — Change `acquire()` signature, add validation
2. Update `sync_limiter.py` — Same change for `SyncRateLimiter`
3. Update `docs/getting-started.md` — Restructure Quick Start
4. Update `docs/guide/basic-usage.md` — Lead with stored config
5. Run doctests — `uv run pytest --doctest-modules`

## Testing

**New tests:**
- `acquire()` without `limits` resolves from stored config
- `acquire()` without `limits` and no stored config raises `ValidationError`
- Error message includes guidance (set_system_defaults, set_resource_defaults, or pass explicitly)

**Existing tests:**
- Tests passing `limits=None` explicitly should still work
- Tests omitting `limits` should now use stored config behavior
