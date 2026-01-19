# ADR-005: Categorized Exception Hierarchy

**Status:** Accepted
**Date:** 2026-01-11
**PR:** [#53](https://github.com/zeroae/zae-limiter/pull/53)
**Issue:** [#29](https://github.com/zeroae/zae-limiter/issues/29)
**Milestone:** v0.2.0

## Context

The initial exception design had flat inheritance from `Exception`, making it difficult to catch categories of errors. `EntityNotFoundError` inherited from `RateLimitError` despite not being a rate limit issue. Users couldn't distinguish infrastructure failures from entity errors.

## Decision

Implement categorized exception hierarchy with semantic grouping.

**Hierarchy:**
```
ZAELimiterError (base)
├── RateLimitError (rate limiting)
│   ├── RateLimitExceeded
│   └── RateLimiterUnavailable
├── InfrastructureError (AWS/CloudFormation)
│   ├── StackCreationError
│   └── InfrastructureNotFoundError
├── EntityError (CRUD operations)
│   ├── EntityNotFoundError
│   └── EntityExistsError
└── VersionError (compatibility)
    ├── VersionMismatchError
    └── IncompatibleSchemaError
```

## Consequences

**Positive:**
- Catch broad categories (`except RateLimitError`) or specific exceptions
- Semantic clarity: exception type indicates problem domain
- Follows Python conventions (`requests.RequestException`, etc.)
- Backward compatible: existing catches still work

**Negative:**
- More exception classes to maintain
- Migration required for code catching moved exceptions (e.g., `EntityNotFoundError`)

## Alternatives Considered

- **Flat hierarchy**: Rejected; no category-level catching, poor semantics
- **Error codes instead of types**: Rejected; not Pythonic, loses type checking benefits
- **Fewer categories**: Rejected; infrastructure vs entity distinction valuable for error handling
