# ADR-121: Module-scoped Test Fixtures for Integration Tests

**Status:** Proposed
**Date:** 2026-02-02
**Issue:** [#253](https://github.com/zeroae/zae-limiter/issues/253)

## Context

Integration tests against LocalStack create and destroy DynamoDB tables for each test function. With 19 tests, this results in 19 table creation/deletion cycles, each taking several seconds. The cumulative overhead causes the integration test suite to run for approximately 15 minutes, creating friction in the development workflow and slowing CI feedback loops.

The tests are isolated at the data level—each test creates distinct entities and buckets—but share no infrastructure. This isolation pattern is overly conservative since tests do not interfere with each other's data when using unique entity identifiers.

## Decision

Integration tests must use module-scoped pytest fixtures (`localstack_repo_module`, `localstack_limiter_module`) that create infrastructure once per test module, with per-test data isolation achieved through unique entity ID prefixes (`unique_entity_prefix` fixture).

## Consequences

**Positive:**
- Integration test runtime reduced from ~15 minutes to ~68 seconds (parallel) or ~2 minutes (sequential)
- Same isolation guarantees maintained through unique entity prefixes
- Pattern aligns with E2E tests which already use class-scoped fixtures

**Negative:**
- Tests must use `loop_scope="module"` on both fixtures and test markers for async compatibility
- Debugging failures requires understanding that data from other tests exists in the shared table
- Tests that require clean infrastructure state must use function-scoped fixtures explicitly

## Alternatives Considered

### Class-scoped fixtures
Rejected because: Module scope maximizes sharing across test classes, reducing infrastructure creation from once-per-class to once-per-module.

### Session-scoped fixtures
Rejected because: Session scope would share infrastructure across test modules, making test isolation harder to reason about and potentially causing cross-module interference in CI parallel execution.

### Per-test table creation (status quo)
Rejected because: The ~15 minute runtime creates unacceptable friction for development iteration.
