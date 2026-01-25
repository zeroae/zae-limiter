# ADR-108: Repository Protocol Design

**Status:** Accepted
**Date:** 2026-01-19
**Issue:** [#150](https://github.com/zeroae/zae-limiter/issues/150)
**Milestone:** v0.5.0

## Context

`RateLimiter` tightly couples business logic to DynamoDB by constructing its own `Repository` internally. This creates testing friction, backend lock-in, and misplaced infrastructure concerns. Users wanting alternative backends (Redis #149, SQLite #156, In-Memory #157) would need invasive changes, and third-party packages cannot implement backends without depending on zae-limiter.

## Decision

Use Python's `typing.Protocol` with `@runtime_checkable` decorator to define `RepositoryProtocol`. This enables duck typing, third-party backends without zae-limiter dependency, and easy mock injection for testing.

**Key design choices:**

1. **Protocol over ABC**: Any object with matching methods satisfies the protocol—no inheritance required
2. **Infrastructure ownership**: Repository owns data access and infrastructure (`StackOptions`); RateLimiter owns business logic only
3. **Method categorization**:
   - Required: entity CRUD, bucket operations, transactions, limit config, lifecycle
   - Optional: audit events, usage snapshots (backend-specific, not in protocol)
   - Capability-gated: batch operations (detected via `capabilities` property)
4. **Infrastructure API**: `ensure_infrastructure()` replaces `create_stack()`; `stack_options` passed to constructor, not method

See [#150](https://github.com/zeroae/zae-limiter/issues/150) for implementation details and method signatures.

## Consequences

**Positive:**
- Clean separation of concerns (data vs business logic)
- Backend flexibility without breaking changes
- Third-party extensibility without zae-limiter dependency
- Testability via mock injection
- Type safety with `@runtime_checkable`

**Negative:**
- More verbose construction (two objects instead of one)
- Deprecation period requires maintaining both constructor signatures
- Optional methods require type narrowing to access

## Alternatives Considered

### Abstract Base Class (ABC)
Rejected: Requires inheritance, preventing duck typing; less Pythonic for interfaces.

### Keep StackOptions on RateLimiter
Rejected: Conflates business logic with infrastructure; violates single responsibility.

### Separate Sync and Async Protocols
Rejected: Increases surface area; `SyncRateLimiter` can wrap async with `asyncio.run()`.
