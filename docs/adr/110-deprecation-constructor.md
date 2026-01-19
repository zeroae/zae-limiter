# ADR-110: Deprecation Strategy for RateLimiter Constructor

**Status:** Proposed
**Date:** 2026-01-19
**Issue:** [#150](https://github.com/zeroae/zae-limiter/issues/150)
**Milestone:** v0.5.0

## Context

The current `RateLimiter` constructor accepts infrastructure-specific parameters directly:

```python
limiter = RateLimiter(
    name="my-app",
    region="us-east-1",
    endpoint_url=None,
    stack_options=StackOptions(),
    on_unavailable=OnUnavailable.BLOCK,
)
```

This design conflates business logic configuration (`on_unavailable`) with data access configuration (`name`, `region`, `endpoint_url`, `stack_options`). Issue #150 proposes extracting a `Repository` class and `RepositoryProtocol` to:

1. **Separate concerns**: Repository owns data access + infrastructure; RateLimiter owns business logic
2. **Enable testability**: Easy to inject mock repositories
3. **Support future backends**: Protocol enables Redis (#149) without breaking changes
4. **Relocate StackOptions**: It belongs on Repository (DynamoDB-specific)

## Decision

Deprecate the `name`, `region`, `endpoint_url`, and `stack_options` parameters on `RateLimiter` and `SyncRateLimiter` constructors. Introduce a new `repository` parameter that accepts a `RepositoryProtocol` instance.

### Current API (Deprecated in v0.5.0)

```python
limiter = RateLimiter(
    name="my-app",
    region="us-east-1",
    endpoint_url=None,
    stack_options=StackOptions(),
    on_unavailable=OnUnavailable.BLOCK,
)
```

### New API (Preferred in v0.5.0+)

```python
from zae_limiter import RateLimiter, Repository, StackOptions

repo = Repository(
    name="my-app",
    region="us-east-1",
    endpoint_url=None,
    stack_options=StackOptions(),
)
limiter = RateLimiter(
    repository=repo,
    on_unavailable=OnUnavailable.BLOCK,
)
```

### Deprecation Warning

When the old constructor signature is used, emit:

```python
warnings.warn(
    "Passing name/region/endpoint_url/stack_options directly to "
    "RateLimiter is deprecated. Use Repository(...) instead. "
    "This will be removed in v2.0.0.",
    DeprecationWarning,
    stacklevel=2,
)
```

**stacklevel=2** ensures the warning points to the caller's code, not the `RateLimiter.__init__` method.

### Parameter Conflict Handling

If both `repository` and `name` are provided, raise `ValueError`:

```python
if repository is not None and name is not None:
    raise ValueError(
        "Cannot specify both 'repository' and 'name'. "
        "Use Repository(name=...) instead."
    )
```

### Default Behavior (Backward Compatibility)

When neither `repository` nor `name` is provided, create a default repository for backward compatibility:

```python
if repository is None and name is None:
    # Maintain backward compatibility: default to "limiter" name
    self._repo = Repository(name="limiter")
```

This matches the current default behavior where `name` defaults to `"limiter"`.

### Timeline

| Version | Behavior |
|---------|----------|
| v0.4.x and earlier | Only old constructor signature available |
| v0.5.0 | Both signatures work; old emits `DeprecationWarning` |
| v0.6.0 - v1.x | Deprecation warning remains |
| v2.0.0 | Old parameters removed; only `repository` accepted |

### SyncRateLimiter

`SyncRateLimiter` follows the same deprecation pattern:

```python
# Old (deprecated)
sync_limiter = SyncRateLimiter(name="my-app", region="us-east-1")

# New (preferred)
repo = Repository(name="my-app", region="us-east-1")
sync_limiter = SyncRateLimiter(repository=repo)
```

## Consequences

**Positive:**

- Clear separation between data access and business logic
- Enables mock repositories for testing without AWS
- Protocol-based design supports future backends (Redis, in-memory)
- Type-safe API with better IDE support
- Gradual migration path with 1+ major version deprecation cycle

**Negative:**

- More verbose for simple cases (two objects instead of one)
- Users must update code before v2.0.0 to avoid breakage
- Documentation must cover both patterns during transition

## Migration Guide

### Basic Usage

```python
# Before (v0.4.x)
limiter = RateLimiter(
    name="my-app",
    region="us-east-1",
)

# After (v0.5.0+)
from zae_limiter import RateLimiter, Repository

repo = Repository(name="my-app", region="us-east-1")
limiter = RateLimiter(repository=repo)
```

### With Infrastructure Management

```python
# Before (v0.4.x)
limiter = RateLimiter(
    name="my-app",
    region="us-east-1",
    stack_options=StackOptions(
        lambda_memory=512,
        enable_alarms=True,
    ),
)

# After (v0.5.0+)
from zae_limiter import RateLimiter, Repository, StackOptions

repo = Repository(
    name="my-app",
    region="us-east-1",
    stack_options=StackOptions(
        lambda_memory=512,
        enable_alarms=True,
    ),
)
limiter = RateLimiter(repository=repo)
```

### LocalStack Development

```python
# Before (v0.4.x)
limiter = RateLimiter(
    name="my-app",
    endpoint_url="http://localhost:4566",
    region="us-east-1",
    stack_options=StackOptions(),
)

# After (v0.5.0+)
repo = Repository(
    name="my-app",
    endpoint_url="http://localhost:4566",
    region="us-east-1",
    stack_options=StackOptions(),
)
limiter = RateLimiter(repository=repo)
```

### Sync Client

```python
# Before (v0.4.x)
sync_limiter = SyncRateLimiter(name="my-app", region="us-east-1")

# After (v0.5.0+)
repo = Repository(name="my-app", region="us-east-1")
sync_limiter = SyncRateLimiter(repository=repo)
```

## Documentation Updates Required

| Document | Update Needed |
|----------|---------------|
| `docs/getting-started.md` | Update quickstart examples to use Repository |
| `docs/guide/basic-usage.md` | Update all RateLimiter instantiation examples |
| `docs/infra/deployment.md` | Update declarative infrastructure examples |
| `docs/api/` | Document Repository class and RepositoryProtocol |
| `docs/contributing/localstack.md` | Update development examples |
| `CLAUDE.md` | Update Infrastructure Deployment section examples |

## Alternatives Considered

### Keep Parameters on RateLimiter, Add Repository as Optional

Rejected: Creates ambiguity about which is preferred; doesn't achieve separation of concerns goal.

### Remove Old Parameters Immediately (Breaking Change)

Rejected: Violates semantic versioning principles; existing users would face immediate breakage without migration path.

### Use Factory Function Instead of Constructor Change

```python
# Alternative considered
limiter = RateLimiter.from_repository(repo)
```

Rejected: Adding a factory while keeping constructor creates two "right" ways to do things; cleaner to have single constructor with deprecation.
