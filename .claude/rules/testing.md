# Testing

## Test Directory Structure

```
tests/
├── conftest.py                  # Shared config (--run-aws flag)
├── unit/                        # Fast tests with mocked AWS (moto)
│   ├── test_limiter.py
│   ├── test_repository.py
│   └── test_sync_limiter.py
├── integration/                 # LocalStack tests (repository-level)
│   └── test_repository.py
├── e2e/                         # Full workflow tests (LocalStack + AWS)
│   ├── test_localstack.py
│   └── test_aws.py
└── benchmark/                   # Performance benchmarks (pytest-benchmark)
    ├── test_operations.py       # Mocked benchmarks
    └── test_localstack.py       # LocalStack benchmarks
```

## Test Categories

| Category | Directory | Backend | What to Test | Speed |
|----------|-----------|---------|--------------|-------|
| **Unit** | `tests/unit/` | moto (mocked) | Business logic, bucket math, schema, exceptions | Fast (~seconds) |
| **Integration** | `tests/integration/` | LocalStack | Repository operations, transactions, GSI queries, optimistic locking | Medium |
| **E2E** | `tests/e2e/` | LocalStack or AWS | Full workflows: CLI, rate limiting, hierarchical limits, aggregator | Slow |
| **Benchmark** | `tests/benchmark/` | moto or LocalStack | Latency (p50/p95/p99), throughput, cascade overhead | Variable |

## When to Add Tests

- **New business logic** (bucket calculations, limit validation) → `unit/`
- **New DynamoDB operations** (queries, transactions, GSI) → `integration/`
- **New user-facing features** (CLI commands, rate limiting workflows) → `e2e/`
- **AWS-specific behavior** (alarms, DLQ, CloudWatch metrics) → `e2e/test_aws.py`
- **Performance-sensitive code** (new operations, optimizations) → `benchmark/`

## Pytest Markers

| Marker | Description | How to Run |
|--------|-------------|------------|
| (none) | Unit tests | `pytest tests/unit/` |
| `@pytest.mark.gevent` | Gevent monkey-patching (auto-skipped under xdist) | `pytest tests/unit/ -m gevent -n 0` |
| `@pytest.mark.integration` | Requires LocalStack | `pytest -m integration` (with LocalStack env vars) |
| `@pytest.mark.e2e` | End-to-end workflows | `pytest -m e2e` (with LocalStack env vars) |
| `@pytest.mark.aws` | Real AWS (requires `--run-aws`) | `pytest -m aws --run-aws` |
| `@pytest.mark.benchmark` | Performance benchmarks | `pytest -m benchmark` |
| `@pytest.mark.slow` | Tests with >30s waits | Skip with `-m "not slow"` |
| `@pytest.mark.monitoring` | CloudWatch/DLQ verification | Skip with `-m "not monitoring"` |
| `@pytest.mark.snapshots` | Usage snapshot verification | Skip with `-m "not snapshots"` |

## Running Tests

```bash
# Unit tests only (fast, no Docker)
uv run pytest tests/unit/ -v
# Gevent tests require xdist disabled (monkey-patching incompatible with xdist workers)
uv run pytest tests/unit/ -m gevent -n 0 -v

# Start LocalStack
zae-limiter local up

# Set environment variables for LocalStack
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1

# Integration / E2E / Benchmarks
uv run pytest tests/integration/ -v
uv run pytest tests/e2e/test_localstack.py -v
uv run pytest tests/e2e/test_aws.py --run-aws -v     # Real AWS (costs money!)
uv run pytest tests/benchmark/test_operations.py -v   # Mocked (fast)
uv run pytest tests/benchmark/test_localstack.py -v   # LocalStack (realistic)

# Coverage
pytest --cov=zae_limiter --cov-report=html

# Stop LocalStack
zae-limiter local down
```

## Benchmark Workflow

Benchmarks detect performance regressions. Files in `tests/benchmark/` track latency, throughput, and DynamoDB capacity.

**Important:** `-o "addopts="` disables xdist by overriding `pyproject.toml`. Only use it for benchmarks and gevent tests — all other test runs (unit, integration, E2E) must keep xdist enabled for parallel execution:

```bash
# Run benchmarks (disable xdist with -o "addopts=")
uv run pytest tests/benchmark/ -o "addopts=" -v --benchmark-only

# 1. Baseline before optimization
uv run pytest tests/benchmark/test_operations.py -o "addopts=" -v --benchmark-json=baseline.json

# 2. Make changes, then compare
uv run pytest tests/benchmark/test_operations.py -o "addopts=" -v --benchmark-compare=baseline.json

# 3. Export results for JSON
uv run pytest tests/benchmark/ -o "addopts=" -v --benchmark-json=benchmark.json
```

| Type | File | Backend | Use Case |
|------|------|---------|----------|
| **Operations** | `test_operations.py` | moto | Fast local iteration |
| **LocalStack** | `test_localstack.py` | DynamoDB emulation | Realistic network latency |
| **Latency** | `test_latency.py` | moto | p50/p95/p99 breakdown |
| **Throughput** | `test_throughput.py` | moto | Sequential/concurrent ops |
| **Capacity** | `test_capacity.py` | moto | RCU/WCU tracking |
| **AWS** | `test_aws.py` | Real AWS | Production metrics |
