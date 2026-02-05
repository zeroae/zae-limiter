# ADR-121: Native Sync Implementation via Unasync Pattern

## Status

Accepted

## Context

The `SyncRateLimiter` class wrapped the async `RateLimiter` using
`asyncio.run_until_complete()`. This approach failed in environments
with their own async runtime (Gevent, Eventlet) because:

- Gevent/Eventlet use greenlet-based cooperative concurrency
- asyncio's event loop conflicts with monkey-patched I/O
- The wrapper blocked entire greenlet pools or failed outright

## Decision

Adopt the **unasync pattern** used by httpx, httpcore, and urllib3:

1. Async code (aioboto3) is the **source of truth**
2. AST transformation generates sync code (boto3) at build time
3. Generated code is **committed to git** for documentation and debugging
4. Pre-commit and CI verify generated code stays in sync

### Transformation Rules

| Async | Sync |
|-------|------|
| `async def` | `def` |
| `await expr` | `expr` |
| `async with` | `with` |
| `async for` | `for` |
| `aioboto3` | `boto3` |
| `asyncio.Lock` | `threading.Lock` |
| `asyncio.sleep` | `time.sleep` |
| `asynccontextmanager` | `contextmanager` |
| `RateLimiter` | `SyncRateLimiter` |
| `Repository` | `SyncRepository` |
| `RepositoryProtocol` | `SyncRepositoryProtocol` |

### File Mapping

| Source | Generated |
|--------|-----------|
| repository_protocol.py | sync_repository_protocol.py |
| repository.py | sync_repository.py |
| limiter.py | sync_limiter.py |
| lease.py | sync_lease.py |
| config_cache.py | sync_config_cache.py |
| infra/stack_manager.py | infra/sync_stack_manager.py |
| infra/discovery.py | infra/sync_discovery.py |

### Build Integration

- **Hatch build hook**: Generates sync code before wheel/sdist
- **Pre-commit hook**: Verifies generated code is up-to-date
- **CI verification**: Blocks PRs with stale generated code

## Consequences

### Positive

- **Single source of truth**: Maintain async code only
- **Native performance**: No event loop overhead in sync contexts
- **Gevent/Eventlet compatible**: boto3 blocking I/O becomes cooperative via monkey-patching
- **Debuggable**: Generated code is committed, has line numbers
- **Correct types**: Both versions have proper type hints

### Negative

- **Build complexity**: Requires AST transformer and verification hooks
- **PR noise**: Generated code changes appear in diffs
- **Two test suites**: Both async and sync tests must pass
- **Transformer maintenance**: New async patterns may need transformer updates

### Neutral

- Generated files have header indicating they're auto-generated
- Developers must run `python scripts/generate_sync.py` after modifying async code

## References

- Design doc: `docs/plans/2026-02-02-native-sync-unasync-design.md`
- Implementation plan: `docs/plans/2026-02-02-native-sync-implementation.md`
- httpx unasync: https://github.com/encode/httpx/tree/master/scripts
- httpcore unasync: https://github.com/encode/httpcore
