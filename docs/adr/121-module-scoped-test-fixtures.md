# ADR-121: Module-scoped Test Fixtures for Integration Tests

**Status:** Proposed
**Date:** 2026-02-02
**Issue:** [#253](https://github.com/zeroae/zae-limiter/issues/253)

## Context

Integration tests against LocalStack create and destroy DynamoDB tables for each test function. With 19 tests distributed across 4 pytest-xdist workers, this results in multiple table creation/deletion cycles per worker. The cumulative overhead causes the integration test suite to run for approximately 13 minutes.

The tests are isolated at the data level—each test creates distinct entities and buckets—but share no infrastructure within each worker. This isolation pattern is overly conservative since tests do not interfere with each other's data when using unique entity identifiers.

## Decision

Integration tests must use module-scoped pytest fixtures (`localstack_repo_module`, `localstack_limiter_module`) that create infrastructure once per test module, with per-test data isolation achieved through unique entity ID prefixes (`unique_entity_prefix` fixture).

## Consequences

**Positive:**
- Integration test runtime reduced by ~20% (from ~13 minutes to ~10 minutes with xdist)
- Same isolation guarantees maintained through unique entity prefixes
- Pattern aligns with E2E tests which already use class-scoped fixtures
- Cleaner fixture naming convention with scope in name

**Negative:**
- Tests must use `loop_scope="module"` on both fixtures and test markers for async compatibility
- Debugging failures requires understanding that data from other tests exists in the shared table
- Tests that require clean infrastructure state must use function-scoped fixtures explicitly

**Observed Limitation:**
The improvement is modest (~20%) because CI runs with pytest-xdist (`-n auto --dist loadscope`), which distributes test modules across 4 workers. Each worker creates its own module-scoped fixtures, so the total stack count equals the worker count rather than 1.

Disabling xdist to maximize fixture sharing was tested and rejected—sequential execution took ~26 minutes vs ~10 minutes with xdist. Parallelization benefits outweigh stack creation overhead.

## Alternatives Considered

### Disable xdist for integration tests
Rejected because: Sequential execution (~26 min) is significantly slower than parallel execution with per-worker stacks (~10 min). The parallelization benefit exceeds the stack creation overhead.

### Class-scoped fixtures
Rejected because: Module scope maximizes sharing across test classes within each xdist worker, reducing infrastructure creation from once-per-class to once-per-module.

### Session-scoped fixtures
Rejected because: Session scope would share infrastructure across test modules, making test isolation harder to reason about and potentially causing cross-module interference in CI parallel execution.

### Per-test table creation (status quo)
Rejected because: The ~13 minute runtime creates friction for development iteration. Even a 20% improvement is worthwhile.
