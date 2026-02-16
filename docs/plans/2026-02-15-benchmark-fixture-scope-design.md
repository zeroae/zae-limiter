# Benchmark Fixture Scope Design

**Issue:** #171
**Date:** 2026-02-15
**Status:** Approved

## Problem

Benchmark tests pay ~0.12-0.42s per test for `sync_limiter` setup (moto mock + DynamoDB table creation). With 25+ moto-based benchmark tests, this adds ~4.2s of setup overhead (~44% of total runtime). Additionally, tests measure a mix of cold-path (first acquire creates entity/bucket) and warm-path (subsequent acquires hit existing state), contaminating steady-state measurements.

## Solution

Module-scope the moto mock and limiter for benchmark tests. Create a `BenchmarkEntities` dataclass with pre-warmed flat and hierarchical entities, created once per test file.

### New Fixtures (in `tests/benchmark/conftest.py`)

```python
@dataclass
class BenchmarkEntities:
    flat: list[str]                    # 100 standalone entity IDs
    parents: list[str]                 # parent entity IDs
    children: dict[str, list[str]]     # parent_id → [child_ids] (cascade=True)
    limiter: SyncRateLimiter           # the module-scoped limiter

@pytest.fixture(scope="module")
def mock_dynamodb_module()              # module-scoped moto mock

@pytest.fixture(scope="module")
def benchmark_limiter(mock_dynamodb_module)  # module-scoped SyncRateLimiter

@pytest.fixture(scope="module")
def benchmark_entities(benchmark_limiter)    # 100 flat + hierarchy, all pre-warmed
```

### Test Categories

| Category | Fixture | Rationale |
|----------|---------|-----------|
| Warm-path measurement | `benchmark_entities` | Pre-warmed entities, no cold-start contamination |
| Custom config on shared table | `benchmark_limiter` | Shared table, test adds own `set_limits()` / `create_entity()` |
| Optimization comparison | `sync_limiter` (function-scoped) | Needs clean state, cache manipulation, or `sync_limiter_no_cache` |

### Test File Mapping

**test_throughput.py** — all 7 tests switch to `benchmark_entities`:
- `TestThroughputBenchmarks` (5 tests): use `benchmark_entities.flat`
- `TestThroughputWithHierarchy` (2 tests): use `benchmark_entities.children`, remove `hierarchy_limiter` fixture

**test_latency.py** — most tests switch:
- `TestLatencyBenchmarks`: simple acquire tests use `benchmark_entities.flat`, cascade test uses `benchmark_entities.children`, stored limits test uses `benchmark_limiter` with own `set_limits()`
- `TestLatencyComparison`: cascade comparison uses `benchmark_entities`, non-cascade baseline creates own entity on `benchmark_limiter`, limits comparison uses `benchmark_entities.flat`

**test_operations.py** — mixed:
- `TestAcquireReleaseBenchmarks` (2 tests): use `benchmark_entities.flat`
- `TestTransactionOverheadBenchmarks` (2 tests): use `benchmark_entities`
- `TestCascadeOverheadBenchmarks` (3 tests): use `benchmark_entities.children` + create non-cascade entity on `benchmark_limiter`
- `TestConcurrentThroughputBenchmarks` (2 tests): use `benchmark_entities`
- `TestConfigLookupBenchmarks` (4 tests): KEEP on function-scoped `sync_limiter` (config cache warmup tests)
- `TestOptimizationComparison` (8 tests): KEEP on function-scoped `sync_limiter` / `sync_limiter_no_cache` (cache disabled, entity cache cleared per iteration, batch ops disabled)

**test_capacity.py** — all tests KEEP on function-scoped `sync_limiter` (exact RCU/WCU assertions need clean state)

### BenchmarkEntities Contents

- **100 flat entities**: `bench-entity-000` through `bench-entity-099`, each with one pre-warmed `rpm` bucket on resource `benchmark`
- **1 parent**: `bench-parent-0`
- **10 children**: `bench-child-0-00` through `bench-child-0-09`, all with `cascade=True` and parent `bench-parent-0`, pre-warmed

### Baseline

```
25 passed, 30 skipped in 9.50s (--benchmark-skip)
Setup cost: ~0.12-0.42s per test (~4.2s total, 44% of runtime)
```

Expected improvement: setup cost drops to ~0.4s per file (once) instead of ~0.12-0.42s per test.

### Documentation Update

Add fixture scope guidance to `.claude/rules/testing.md`:

| Scope | Use When | Example |
|-------|----------|---------|
| `function` | Test mutates state, needs isolation | `sync_limiter` (each test gets clean state) |
| `class` | Expensive setup shared by class | `e2e_limiter` (CloudFormation stack) |
| `module` | Expensive setup shared by file | `benchmark_entities` (100 pre-warmed entities) |
| `session` | Immutable configuration | `localstack_endpoint` (env var read) |

**Rule**: If fixture setup takes >100ms and is used by multiple tests in the same file, consider `scope="module"`.
