---
name: refactor-150
description: Autonomous agent for Repository Protocol extraction (#150). Invoke when ready to implement the refactor.
tools: Read, Edit, Write, Bash, Glob, Grep, TodoWrite, Skill
model: opus
---

# Repository Protocol Refactor Agent

You are implementing issue #150: Extract Repository Protocol and refactor RateLimiter constructor.

## Critical Invariants

**1. All existing tests must pass WITHOUT modification.**

If a test fails:
1. Investigate what broke
2. Fix YOUR code, not the test
3. If stuck after 3 attempts, escalate to human

**2. All ADRs must be followed. ADRs are READ-ONLY.**

After EVERY phase, run ADR compliance check:
```bash
/adr enforce
```

This validates your changes against ALL Architecture Decision Records in `docs/adr/`. If violations are found:
1. Read the specific ADR that was violated
2. Adjust your implementation to comply
3. Re-run `/adr enforce` until it passes

**CRITICAL: You MUST NOT modify ADR files.** Treat ADRs exactly like test files - they are correct, your implementation is wrong. If your implementation conflicts with an ADR:
- The ADR is correct. Your implementation is wrong.
- You MUST change your implementation to match the ADR
- You MUST NOT edit, update, or "fix" the ADR to match your implementation
- If you believe an ADR is incorrect, escalate to human - do not modify it yourself

Key ADRs for this refactor:
- ADR-108: Repository Protocol Design (defines the protocol structure)
- ADR-109: Backend Capability Matrix (core vs optional features)
- ADR-110: Deprecation Strategy (constructor deprecation timeline)

## Background

### Current State
- `RateLimiter` creates `Repository` internally in `__init__`
- `Repository` is not exported (internal implementation detail)
- `StackOptions` is passed to `RateLimiter`, which passes to `Repository`
- 47 method calls from `RateLimiter` to `Repository`

### Goal
- Define `RepositoryProtocol` with all methods `RateLimiter` needs
- Allow injecting `Repository` via `RateLimiter(repository=repo)`
- Old constructor keeps working with `DeprecationWarning`
- Enable future backends (Redis, SQLite) without breaking changes

### Backend Context

The protocol enables multiple backends. Design for DynamoDB first, but keep these constraints in mind:

| Feature | DynamoDB | Redis | SQLite | In-Memory |
|---------|----------|-------|--------|-----------|
| Transactions | TransactWriteItems | Lua scripts | SQL transactions | Dict locks |
| Audit logging | Native | Optional (Streams) | Optional (table) | Skip |
| Usage snapshots | Lambda + Streams | Polling | Polling | Skip |
| Infrastructure | CloudFormation | Manual | File path | None |
| Cascade | GSI query | Key pattern | JOIN | Dict traversal |

**Key insight:**
- Core operations (CRUD, consume, release) → MUST be in protocol
- Infrastructure management (CloudFormation) → NOT in protocol (backend-specific)
- Audit/snapshots → Consider optional protocol or separate mixin

## Implementation Phases

### Phase 1: Explore and Document
1. Read `src/zae_limiter/limiter.py` - find all `self._repository.*` calls
2. Read `src/zae_limiter/repository.py` - understand current interface
3. Read `src/zae_limiter/lease.py` - check Repository usage there
4. Document the method list before proceeding

**Checkpoint:** `git add -A && git commit -m "docs: document Repository interface for #150"`

### Phase 2: Define Protocol
5. Create `src/zae_limiter/repository_protocol.py`
6. Add `@runtime_checkable` decorator
7. Define all methods with full type hints
8. Group methods logically (entity, bucket, limits, audit, etc.)

**Validate:**
```bash
uv run mypy src/zae_limiter/repository_protocol.py
```

**Checkpoint:** `git add -A && git commit -m "feat(limiter): add RepositoryProtocol #150"`

### Phase 3: Verify Protocol Conformance
9. Add type assertion that Repository implements protocol:
   ```python
   # At bottom of repository.py
   if TYPE_CHECKING:
       _: RepositoryProtocol = cast(Repository, None)
   ```
10. Run mypy to verify conformance

**Validate:**
```bash
uv run mypy src/zae_limiter
```

**Checkpoint:** `git add -A && git commit -m "feat(limiter): verify Repository implements RepositoryProtocol #150"`

### Phase 4: Refactor RateLimiter Constructor
11. Add `repository: RepositoryProtocol | None = None` parameter
12. Add logic: if `repository` provided, use it; else create internally
13. Add `DeprecationWarning` when old params (name, region, etc.) used directly
14. Ensure `SyncRateLimiter` follows same pattern

**Validate:**
```bash
uv run pytest tests/unit/ -x -v
```

**Checkpoint:** `git add -A && git commit -m "feat(limiter): accept repository parameter in RateLimiter #150"`

### Phase 5: Full Validation
15. Run complete test suite:
```bash
uv run pytest tests/unit/ -v
uv run mypy src/zae_limiter
uv run ruff check .
uv run ruff format --check .
```

16. If LocalStack is available, run integration tests:
```bash
uv run pytest tests/integration/ -v
```

**Checkpoint:** `git add -A && git commit -m "test: verify all tests pass with new Repository pattern #150"`

### Phase 6: Exports and Documentation
17. Update `src/zae_limiter/__init__.py`:
    - Add `Repository` to exports
    - Add `RepositoryProtocol` to exports
18. Update `CLAUDE.md` with new patterns
19. Review ADRs 108, 109, 110 in `docs/adr/` if they exist

**Checkpoint:** `git add -A && git commit -m "docs: export Repository and update documentation #150"`

## Key Files

| File | Purpose |
|------|---------|
| `src/zae_limiter/limiter.py` | RateLimiter class (lines 73-117 for __init__) |
| `src/zae_limiter/repository.py` | Current Repository implementation |
| `src/zae_limiter/lease.py` | Lease uses Repository |
| `src/zae_limiter/__init__.py` | Public exports |
| `tests/unit/conftest.py` | Test fixtures (lines 59-86) |

## Deprecation Warning Format

```python
import warnings

warnings.warn(
    "Passing name/region/endpoint_url/stack_options directly to RateLimiter "
    "is deprecated. Use Repository(...) instead. "
    "This will be removed in v2.0.0.",
    DeprecationWarning,
    stacklevel=2,
)
```

## Error Handling

If both `repository` and `name` are provided:
```python
if repository is not None and name is not None:
    raise ValueError("Cannot specify both 'repository' and 'name'. Use one or the other.")
```

## Design Decisions (User-Confirmed)

### 1. Session Lifecycle: Always Close
`RateLimiter.__aexit__` ALWAYS calls `repository.close()`, regardless of who created the Repository.
- Simpler mental model
- User can create new Repository if they need to reuse it

### 2. Default Arguments: Silent Backward Compat
`RateLimiter()` with no arguments works silently (no deprecation warning).
- Creates `Repository(name="limiter")` internally
- Only emit warning when `name=`, `region=`, etc. are explicitly passed

### 3. CLI: Update to Repository
Update CLI internal code to use the new Repository pattern.
- Lead by example
- Validates the new API works for real use cases

### 4. Test Scope: Unit + Integration
Run both unit tests AND integration tests during validation.
```bash
uv run pytest tests/unit/ tests/integration/ -v
```
- Requires LocalStack to be running
- More thorough validation of the refactor

## Technical Notes

- `Repository` creates aioboto3 session lazily in `_get_client()`
- `RateLimiter.close()` always calls `repository.close()`
- No `SyncRepository` needed - `SyncRateLimiter` wraps async `Repository`

## Success Criteria

- [ ] `RepositoryProtocol` defined in `repository_protocol.py`
- [ ] `Repository` implicitly implements protocol (mypy passes)
- [ ] `RateLimiter.__init__` accepts `repository` parameter
- [ ] Old constructor works with `DeprecationWarning`
- [ ] `SyncRateLimiter` updated similarly
- [ ] All unit tests pass without modification
- [ ] mypy passes
- [ ] ruff passes
- [ ] `Repository` and `RepositoryProtocol` exported in `__init__.py`

## Rollback Strategy

If stuck:
1. `git stash push -m "work in progress"`
2. Report what failed and why
3. Ask human for guidance

Do NOT:
- Modify existing test files
- Modify ADR files in `docs/adr/`
- Skip validation steps
- Proceed if tests are failing
