# ADR-108: Repository Protocol Design

**Status:** Accepted
**Date:** 2026-01-19
**Issue:** [#150](https://github.com/zeroae/zae-limiter/issues/150)
**Milestone:** v0.5.0

## Context

`RateLimiter` currently constructs its own `Repository` instance internally, tightly coupling business logic to DynamoDB. This creates several problems:

1. **Testing friction**: Unit tests require mocking DynamoDB internals or using moto
2. **Backend lock-in**: Users wanting Redis (#149), SQLite (#156), In-Memory (#157), Cosmos DB (#158), or Firestore (#159) would need invasive changes
3. **Misplaced concerns**: `StackOptions` (DynamoDB infrastructure) is passed to `RateLimiter` (business logic)
4. **Third-party integration**: External packages cannot implement backends without depending on zae-limiter

The current API conflates data access configuration with rate limiting behavior:

```python
limiter = RateLimiter(
    name="my-app",              # Data access
    region="us-east-1",         # Data access
    endpoint_url=None,          # Data access
    stack_options=StackOptions(),  # Infrastructure (DynamoDB-specific)
    on_unavailable=OnUnavailable.BLOCK,  # Business logic
)
```

## Decision

### 1. Define RepositoryProtocol with `@runtime_checkable`

Use Python's `typing.Protocol` with `@runtime_checkable` decorator:

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class RepositoryProtocol(Protocol):
    """Protocol for rate limiter data backends."""

    # Entity operations
    async def get_entity(self, entity_id: str) -> Entity | None: ...
    async def create_entity(self, entity_id: str, name: str | None = None,
                           parent_id: str | None = None) -> Entity: ...
    async def delete_entity(self, entity_id: str) -> None: ...

    # Bucket operations
    async def get_buckets(self, entity_id: str, resource: str) -> list[BucketState]: ...
    async def get_or_create_bucket(self, entity_id: str, resource: str,
                                   limit: Limit) -> BucketState: ...

    # Transaction operations
    async def transact_consume(self, entries: list[ConsumeEntry]) -> None: ...
    async def transact_release(self, entries: list[ReleaseEntry]) -> None: ...

    # Limit config operations
    async def get_limits(self, entity_id: str, resource: str) -> list[Limit]: ...
    async def set_limits(self, entity_id: str, limits: list[Limit],
                        resource: str) -> None: ...

    # Lifecycle
    async def close(self) -> None: ...

    # Properties
    @property
    def name(self) -> str: ...
```

### 2. Why Protocol over ABC

| Aspect | Protocol | ABC |
|--------|----------|-----|
| Inheritance required | No | Yes |
| Duck typing | Natural fit | Must inherit |
| Third-party backends | No zae-limiter dependency needed | Must import base class |
| Existing Repository | Works without changes | Needs to inherit |
| Runtime checking | `isinstance(repo, RepositoryProtocol)` | `isinstance(repo, ABCRepository)` |

Protocol is more Pythonic: any object with the right methods satisfies the protocol, enabling:
- Third-party packages to implement backends without importing zae-limiter
- Mock objects in tests without inheriting from a base class
- Gradual adoption in existing codebases

### 3. Required vs Optional Methods

**Required (core rate limiting):**
- `get_entity`, `create_entity`, `delete_entity` - Entity lifecycle
- `get_buckets`, `get_or_create_bucket` - Token bucket access
- `transact_consume`, `transact_release` - Atomic limit operations
- `get_limits`, `set_limits` - Limit configuration
- `close` - Resource cleanup
- `name` property - Backend identifier

**Optional (backend-specific, not in protocol):**
- `ensure_infrastructure()` - DynamoDB CloudFormation stack
- `get_audit_events()` - Audit logging (DynamoDB-specific schema)
- `get_usage_snapshots()` - Usage aggregation (requires Lambda)

Optional methods are accessed via backend-specific types, not the protocol.

### 4. Infrastructure Management Belongs in Repository

`StackOptions` moves from `RateLimiter` to `Repository`:

```python
# Repository owns infrastructure
repo = Repository(
    name="my-app",
    region="us-east-1",
    stack_options=StackOptions(
        lambda_memory=512,
        enable_alarms=True,
    ),
)

# RateLimiter owns business logic only
limiter = RateLimiter(
    repository=repo,
    on_unavailable=OnUnavailable.BLOCK,
)
```

This separation enables:
- Redis backends that don't need CloudFormation
- In-memory backends for testing with zero infrastructure
- Clear ownership: Repository manages its own resources

### 5. Deprecation Path for Old Constructor

```python
class RateLimiter:
    def __init__(
        self,
        # New way (preferred)
        repository: RepositoryProtocol | None = None,
        # Old way (deprecated)
        name: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
        stack_options: StackOptions | None = None,
        # Business logic config (not deprecated)
        on_unavailable: OnUnavailable = OnUnavailable.BLOCK,
    ):
        if repository is not None:
            if name is not None:
                raise ValueError("Cannot specify both repository and name")
            self._repo = repository
        elif name is not None:
            warnings.warn(
                "Passing name/region/endpoint_url/stack_options directly to "
                "RateLimiter is deprecated. Use Repository(...) instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            self._repo = Repository(
                name=name,
                region=region,
                endpoint_url=endpoint_url,
                stack_options=stack_options,
            )
        else:
            self._repo = Repository(name="limiter")
```

Deprecated constructor will be removed in v2.0.0.

## Consequences

**Positive:**
- Clean separation: Repository owns data + infrastructure, RateLimiter owns business logic
- Backend flexibility: Redis (#149), SQLite (#156), In-Memory (#157) without breaking changes
- Third-party extensibility: External packages can implement backends without zae-limiter dependency
- Testability: Easy mock injection via protocol
- Type safety: `@runtime_checkable` enables `isinstance()` checks

**Negative:**
- More verbose construction for simple cases (two objects instead of one)
- Deprecation period requires maintaining both constructor signatures
- Optional methods (audit, usage) require type narrowing to access

## Alternatives Considered

### Abstract Base Class (ABC)

```python
from abc import ABC, abstractmethod

class ABCRepository(ABC):
    @abstractmethod
    async def get_entity(self, entity_id: str) -> Entity | None: ...
```

Rejected because:
- Requires inheritance, preventing duck typing
- Third-party backends must import and inherit from zae-limiter
- Less Pythonic than Protocol for interface definitions

### Keep StackOptions on RateLimiter

Rejected because:
- Conflates business logic with infrastructure management
- Makes non-DynamoDB backends awkward (they'd ignore StackOptions)
- Violates single responsibility principle

### Separate Sync and Async Protocols

```python
class SyncRepositoryProtocol(Protocol): ...
class AsyncRepositoryProtocol(Protocol): ...
```

Rejected because:
- Increases protocol surface area
- `SyncRateLimiter` can wrap async repository with `asyncio.run()`
- Single protocol with async methods is simpler
