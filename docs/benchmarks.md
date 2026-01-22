# Benchmark Results

This document tracks benchmark results for performance-sensitive operations in zae-limiter.

## Baseline Results

Baseline benchmarks capture performance before optimization work. Compare new results against baselines to detect performance regressions.

### Moto Benchmarks (Mocked DynamoDB)

Moto benchmarks run against mocked DynamoDB and measure operation latency without network overhead.

```
Run with:
pytest tests/benchmark/test_operations.py -v --benchmark-json=benchmark-moto.json
```

**Key Metrics:**
- `acquire_release_single_limit`: Single limit acquire/release (baseline)
- `acquire_release_multiple_limits`: Multi-limit overhead (rpm + tpm)
- `cascade_optimized`: BatchGetItem optimization impact
- `config_lookup_cached`: Config cache hit performance
- `config_lookup_cold`: Config cache miss performance

### LocalStack Benchmarks (Realistic DynamoDB)

LocalStack benchmarks run against an emulated DynamoDB including realistic network latency.

```
Run with:
docker compose up -d
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
pytest tests/benchmark/test_localstack.py -v --benchmark-json=benchmark-localstack.json
```

**Key Metrics:**
- `acquire_release_localstack`: Basic acquire with realistic latency
- `cascade_with_batchgetitem_optimization`: Cascade using optimized pattern
- `cascade_with_config_cache_optimization`: Combined optimizations

## Performance Targets

| Scenario | Metric | Target |
|----------|--------|--------|
| Single limit acquire | p50 latency | No regression |
| Single limit acquire | p95 latency | No regression |
| Single limit acquire | p99 latency | < 2x baseline |
| Cascade (BatchGetItem) | p50 latency | 10-20% reduction vs sequential |
| Config lookup (cached) | p50 latency | < 5ms overhead |
| Config lookup (cold) | p50 latency | < 20ms overhead |

## Historical Comparison

Benchmark JSON files are stored alongside this document for version-to-version comparison:

- `benchmark-v0.11.0.json` - Baseline before config caching and cascade optimization
- `benchmark-v0.12.0.json` - After centralized config implementation (v0.5.0 feature)
- `benchmark-v0.12.1.json` - After cascade BatchGetItem optimization (issue #133)

## Benchmark Organization

```
tests/benchmark/
├── conftest.py           # Shared fixtures (CapacityCounter, benchmark_entities)
├── test_operations.py    # Moto benchmarks (fast, no Docker)
├── test_localstack.py    # LocalStack benchmarks (realistic latency)
├── test_latency.py       # p50/p95/p99 latency breakdown
├── test_throughput.py    # Sequential/concurrent throughput
├── test_capacity.py      # DynamoDB RCU/WCU tracking
└── test_aws.py           # Real AWS benchmarks (production metrics)
```

## Running Benchmarks

### Unit/Moto Benchmarks (Fast)

```bash
# All moto benchmarks
uv run pytest tests/benchmark/test_operations.py -v --benchmark-json=bench.json

# Specific benchmark class
uv run pytest tests/benchmark/test_operations.py::TestConfigLookupBenchmarks -v

# Compare against baseline
uv run pytest tests/benchmark/test_operations.py --benchmark-compare=bench-baseline.json
```

### LocalStack Benchmarks (Requires Docker)

```bash
# Start LocalStack
docker compose up -d

# Run LocalStack benchmarks
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
uv run pytest tests/benchmark/test_localstack.py -v --benchmark-json=bench-ls.json

# Stop LocalStack
docker compose down
```

### Real AWS Benchmarks (Optional)

```bash
# Run against real AWS (costs money!)
AWS_PROFILE=zeroae-code/AWSPowerUserAccess \
  uv run pytest tests/benchmark/test_aws.py --run-aws -v --benchmark-json=bench-aws.json
```

## Interpreting Results

### Pytest-Benchmark Output

```
benchmark: 5 tests

test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit
  mean ± std dev: 1.23 ± 0.15 ms [min: 0.98 ms, max: 1.65 ms]
  "... 1000 rounds"
```

**Columns:**
- `mean`: Average latency
- `std dev`: Standard deviation (consistency)
- `min/max`: Range of observed values
- Rounds: Number of iterations

### Comparing Baselines

```bash
# Generate JSON from new run
pytest tests/benchmark/test_operations.py -v --benchmark-json=new.json

# Compare against saved baseline
pytest tests/benchmark/test_operations.py -v --benchmark-compare=baseline.json
```

**Output interpretation:**
- `PASS`: Performance stable (no regression)
- `FAIL`: Performance degraded (potential issue)
- `5.50%` or `+5.50%`: Performance improved by 5.5%
- `-5.50%`: Performance degraded by 5.5%

## Adding New Benchmarks

When adding new benchmarks:

1. Create test in appropriate file (`test_operations.py`, `test_localstack.py`, etc.)
2. Use `@pytest.mark.benchmark` marker
3. Include clear docstring explaining what's measured
4. Compare against related baseline test
5. Run benchmark locally: `pytest tests/benchmark/test_*.py -v --benchmark-json=bench.json`
6. Commit JSON baseline for future comparison
7. Update this document with new metrics and targets

## Markers and Filters

| Marker | Purpose | Filter |
|--------|---------|--------|
| `@pytest.mark.benchmark` | Benchmark test | `pytest -m benchmark` |
| `@pytest.mark.integration` | Requires LocalStack | `pytest -m integration` |
| `@pytest.mark.slow` | > 30s runtime | `pytest -m "not slow"` |

## CI Integration

Benchmarks can be integrated into CI/CD:

```yaml
# .github/workflows/benchmark.yml (optional)
- name: Run benchmarks
  run: |
    docker compose up -d
    pytest tests/benchmark/ -v --benchmark-json=results.json
    docker compose down
```

Note: Consider benchmark flakiness before enabling in CI (network latency varies).
