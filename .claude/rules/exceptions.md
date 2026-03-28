---
paths:
  - "src/zae_limiter/exceptions.py"
---

# Exception Conventions

## Naming

- Leaf exceptions that represent **conditions** (not errors in the traditional sense) may omit the `Error` suffix: `RateLimitExceeded`, `RateLimiterUnavailable`. These use `# noqa: N818`.
- All other exceptions MUST end in `Error`.
- Category bases use the pattern `{Category}Error` (e.g., `RateLimitError`, `InfrastructureError`).

## Hierarchy Placement

- `RateLimiterUnavailable` belongs under `InfrastructureError`, NOT `RateLimitError`. It is an infra problem, not a throttling signal. `except RateLimitError` must only catch "you're going too fast" scenarios.
- `StackAlreadyExistsError` is a direct `InfrastructureError` subclass, NOT under `StackOperationError`. "Already exists" is an idempotency check, not an operation failure.
- Namespace state conflicts (`NamespaceStateError`) belong under `InfrastructureError`.
- Reserved namespace validation belongs under `ValidationError`.

## Serialization

- Only `RateLimitExceeded` has `as_dict()` and `retry_after_header`. Other exceptions are operational/developer-facing and do not need serialization.

## Backward Compatibility

- Do NOT add `table_name` aliases or similar backward-compat attributes to exceptions. These were removed before 1.0.0.
- Do NOT add deprecated aliases for renamed exceptions. Use the new name directly.

## Adding a New Exception

1. Choose the correct category parent from the hierarchy.
2. Add to `exceptions.py` in the correct section.
3. Export from `__init__.py` (both import and `__all__`).
4. Add tests in `tests/unit/test_exceptions.py` (attributes, message, `isinstance` hierarchy check).
5. Add autodoc entry in `docs/api/exceptions.md`.
6. Update the hierarchy diagram in `docs/api/exceptions.md`.
7. Update `CLAUDE.md` exception list in the project structure section.
