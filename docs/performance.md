# Performance Tuning Guide

This guide provides detailed recommendations for optimizing zae-limiter performance, covering DynamoDB capacity planning, Lambda configuration, and cost optimization strategies.

## 1. DynamoDB Capacity Planning

### Understanding RCU/WCU Costs

Each zae-limiter operation has specific DynamoDB capacity costs. Use this table for capacity planning:

| Operation | RCUs | WCUs | Notes |
|-----------|------|------|-------|
| `acquire()` - single limit | 1 | 1 | GetItem + TransactWrite |
| `acquire()` - N limits | N | N | N GetItems + TransactWrite(N items) |
| `acquire(cascade=True)` | 3 | 2 | +GetEntity + parent bucket ops |
| `acquire(use_stored_limits=True)` | +2 | 0 | +2 Query operations for limits |
| `available()` | 1 per limit | 0 | Read-only, no transaction |
| `get_limits()` | 1 | 0 | Query operation |
| `set_limits()` | 1 | N+1 | Query + N PutItems |
| `delete_entity()` | 1 | batched | Query + BatchWrite in 25-item chunks |

!!! info "Capacity Validation"
    These costs are validated by automated tests. Run `uv run pytest tests/benchmark/test_capacity.py -v` to verify.

### Capacity Estimation Formula

Use these formulas to estimate hourly capacity requirements:

```
Hourly RCUs = requests/hour × limits/request × (1 + cascade_pct × 2 + stored_limits_pct × 2)
Hourly WCUs = requests/hour × limits/request × (1 + cascade_pct)
```

With the Lambda aggregator enabled (2 windows: hourly, daily):
```
Additional WCUs = requests/hour × limits/request × 2
```

### Example Calculations

#### Scenario 1: Simple API Rate Limiting

- 10,000 requests/hour
- 2 limits per request (rpm, tpm)
- No cascade, no stored limits

**Calculation:**
```
RCUs = 10,000 × 2 × 1.0 = 20,000 RCUs/hour
WCUs = 10,000 × 2 × 1.0 = 20,000 WCUs/hour
```

#### Scenario 2: Hierarchical LLM Limiting

- 10,000 requests/hour
- 2 limits per request
- 50% use cascade (API key → project)
- 20% use stored limits

**Calculation:**
```
RCUs = 10,000 × 2 × (1 + 0.5×2 + 0.2×2) = 10,000 × 2 × 2.4 = 48,000 RCUs/hour
WCUs = 10,000 × 2 × (1 + 0.5) = 10,000 × 2 × 1.5 = 30,000 WCUs/hour
```

### Billing Mode Selection

| Mode | Best For | Trade-offs |
|------|----------|------------|
| PAY_PER_REQUEST (default) | Variable traffic, new deployments | Higher per-request cost, no planning needed |
| Provisioned | Steady traffic >100 TPS | Lower cost at scale, requires planning |
| Provisioned + Reserved | High-volume production | Lowest cost, 1-year commitment |

!!! tip "Migration Guidance"
    Start with PAY_PER_REQUEST. Once traffic patterns stabilize (typically 2-4 weeks), analyze CloudWatch metrics to determine optimal provisioned capacity. Switch when monthly on-demand costs exceed provisioned + 20% buffer.

---

## 2. Lambda Concurrency Settings

The aggregator Lambda processes DynamoDB Stream events to maintain usage snapshots.

### Default Configuration

| Setting | Default | Range | Impact |
|---------|---------|-------|--------|
| Memory | 256 MB | 128-3008 MB | Higher = faster, more expensive |
| Timeout | 60 seconds | 1-900 seconds | Should be 2× typical duration |
| Reserved Concurrency | None | 1-1000 | Limits parallel executions |

### Memory Tuning

Lambda CPU scales linearly with memory allocation:

| Memory | vCPUs | Best For |
|--------|-------|----------|
| 128 MB | ~0.08 | Minimal workloads (testing only) |
| 256 MB | ~0.15 | Most workloads (default) |
| 512 MB | ~0.30 | High-throughput streams |
| 1024 MB | ~0.60 | Rarely needed |

**Guidance based on batch size:**

- <50 records/batch: 128-256 MB sufficient
- 50-100 records/batch: 256-512 MB recommended
- Peak streams: Monitor Lambda duration; increase memory if >50% of timeout

### Concurrency Management

DynamoDB Streams creates one shard per 1000 WCU (or ~3000 writes/sec). Each shard invokes one Lambda instance.

**Recommendations:**

| Volume | Reserved Concurrency | Notes |
|--------|---------------------|-------|
| <1000 writes/sec | None | Default scaling sufficient |
| 1000-10000/sec | 10-50 | Prevents runaway scaling |
| >10000/sec | Expected shards + 20% | Based on table monitoring |

### Error Handling

Configure error handling for production reliability:

```bash
# Deploy with DLQ and alarms
zae-limiter deploy --table-name rate_limits \
  --alarm-sns-topic arn:aws:sns:us-east-1:123456789012:alerts
```

- **Retries**: Failed records retry 3 times within the same batch
- **DLQ**: Persistent failures go to Dead Letter Queue (if configured)
- **Duration Alarm**: Triggers at 80% of timeout (48s default)

---

## 3. Batch Operation Patterns

### Transaction Limits

DynamoDB enforces these limits:

| Constraint | Limit | Impact |
|------------|-------|--------|
| TransactWriteItems | 100 items max | Affects multi-limit updates |
| BatchWriteItem | 25 items per request | Entity deletion is chunked |
| Optimistic locking | Entire transaction fails | Causes retry on contention |

### Efficient Patterns

#### Multi-Limit Acquisition

```python
# Efficient: Single lease for multiple limits
async with limiter.acquire(
    "entity-id",
    "llm-api",
    [rpm_limit, tpm_limit],
    {"rpm": 1},  # Initial consumption (1 request)
) as lease:
    # 2 GetItems + 1 TransactWrite (2 items)
    response = await call_llm()
    lease.adjust({"tpm": response.usage.total_tokens})

# Inefficient: Separate acquisitions
async with limiter.acquire("entity-id", "llm-api", [rpm_limit], {"rpm": 1}):
    async with limiter.acquire("entity-id", "llm-api", [tpm_limit], {"tpm": 100}):
        # 2 GetItems + 2 TransactWrites (doubles write cost!)
        pass
```

#### Cascade Optimization

```python
# Only use cascade when hierarchical limits are actually needed
async with limiter.acquire(
    "api-key",
    "llm-api",
    limits,
    {"rpm": 1},
    cascade=False,  # Saves 1 GetEntity + parent bucket operations
):
    pass

# Use cascade for hierarchical enforcement
async with limiter.acquire(
    "api-key",
    "llm-api",
    limits,
    {"rpm": 1},
    cascade=True,  # Checks and updates parent limits too
):
    pass
```

#### Stored Limits Optimization

```python
# Default: No stored limits lookup (saves 2 RCUs per acquire)
limiter = RateLimiter(
    table_name="rate_limits",
    region="us-east-1",
)

# Enable only when limits vary per entity
limiter = RateLimiter(
    table_name="rate_limits",
    region="us-east-1",
    use_stored_limits=True,  # +2 Queries per acquire
)
```

### Bulk Operations

```python
# Efficient bulk limit setup
await limiter.set_limits("entity-1", [rpm_limit, tpm_limit], resource="llm-api")
await limiter.set_limits("entity-2", [rpm_limit, tpm_limit], resource="llm-api")
# Runs 2 Queries + 2×2 PutItems

# Entity deletion (automatically batched in 25-item chunks)
await limiter.delete_entity("entity-id")
# Runs 1 Query + BatchWrite (up to 25 WCUs per chunk)
```

---

## 4. Expected Latencies

### Operation Latencies

Latencies vary by environment and depend on network conditions, DynamoDB utilization, and operation complexity.

| Operation | Moto p50 | LocalStack p50 | AWS p50 |
|-----------|----------|----------------|---------|
| `acquire()` - single limit | 14ms | 36ms | 36ms |
| `acquire()` - two limits | 30ms | 52ms | 43ms |
| `acquire(cascade=True)` | 28ms | 57ms | 51ms |
| `available()` check | 1ms | 9ms | 8ms |

!!! note "Environment Differences"
    - **Moto**: In-memory mock, measures code overhead only
    - **LocalStack**: Docker-based, includes local network latency
    - **AWS**: Production DynamoDB with real network round-trips

### Latency Breakdown

Typical `acquire()` latency breakdown for a single limit:

```
acquire() latency breakdown:
├── DynamoDB GetItem (bucket)     ~5-15ms   (network + read)
├── Token bucket calculation      <1ms      (in-memory math)
├── TransactWriteItems            ~10-25ms  (network + write + condition check)
└── Network overhead              variable  (region, instance type)
```

### Environment Selection

| Environment | Use Case | Latency Factor |
|-------------|----------|----------------|
| Moto | Unit tests, CI/CD | 1× (baseline) |
| LocalStack | Integration tests, local dev | 2-3× |
| AWS | Production, load testing | 2-4× |

Run benchmarks to measure your specific environment:

```bash
# Moto benchmarks (fast)
uv run pytest tests/benchmark/test_latency.py -v --benchmark-json=latency.json

# LocalStack benchmarks (requires Docker)
docker compose up -d
export AWS_ENDPOINT_URL=http://localhost:4566
uv run pytest tests/benchmark/test_localstack.py -v --benchmark-json=latency.json

# AWS benchmarks (requires credentials)
uv run pytest tests/benchmark/test_aws.py --run-aws -v
```

---

## 5. Throughput Benchmarks

### Maximum Throughput

Theoretical and practical throughput limits depend on contention patterns:

| Scenario | Expected TPS | Bottleneck |
|----------|--------------|------------|
| Sequential, single entity | 50-200 | Serialized operations |
| Sequential, multiple entities | 50-200 | Network round-trip |
| Concurrent, separate entities | 100-500 | Scales with parallelism |
| Concurrent, single entity | 20-100 | Optimistic locking contention |
| Cascade operations | 30-100 | Parent bucket contention |

### Contention Analysis

When multiple requests update the same bucket concurrently, DynamoDB's optimistic locking causes transaction retries:

```
Concurrent updates to same bucket:
├── Request A: Read bucket version=1
├── Request B: Read bucket version=1
├── Request A: Write with condition version=1 → SUCCESS, version=2
├── Request B: Write with condition version=1 → FAIL (ConditionalCheckFailed)
└── Request B: Retry with version=2 → SUCCESS
```

Each retry adds ~10-30ms latency.

### Mitigation Strategies

```python
# Strategy 1: Larger bucket windows (reduces update frequency)
rpm_limit = Limit.per_minute("rpm", capacity=1000, window_seconds=60)

# Strategy 2: Distribute load across entities
# Instead of one shared entity, use sharded entities:
shard = hash(request_id) % 10
entity_id = f"api-key-shard-{shard}"

# Strategy 3: Client-side rate limiting before acquire
# Reduce concurrent requests to the same entity
```

### Running Benchmarks

Use the automated benchmark runner:

```bash
# Run all benchmarks (moto + LocalStack)
python scripts/run_benchmarks.py

# Include AWS benchmarks
python scripts/run_benchmarks.py --run-aws

# Skip LocalStack (moto only)
python scripts/run_benchmarks.py --skip-localstack

# Custom output directory
python scripts/run_benchmarks.py --output-dir ./results
```

Or run individual test suites:

```bash
# Throughput tests
uv run pytest tests/benchmark/test_throughput.py -v

# Analyze results
python -c "import json; print(json.load(open('benchmark.json'))['benchmarks'])"
```

---

## 6. Cost Optimization Strategies

### DynamoDB Cost Breakdown

Costs vary by region. Using us-east-1 as reference:

| Component | On-Demand Cost | Notes |
|-----------|----------------|-------|
| Write Request Units | $0.625 per million | Each WCU = one write |
| Read Request Units | $0.125 per million | Each RCU = one read |
| Storage | $0.25 per GB/month | Usually minimal |
| Streams | $0.02 per 100K reads | Lambda polling |
| Lambda | $0.20 per million + duration | Aggregator function |

### Cost Estimation Examples

#### Low Volume: 10K requests/day

```
DynamoDB:
  Writes: 10K × 2 limits × 30 days = 600K WCUs = $0.38
  Reads:  10K × 2 limits × 30 days = 600K RCUs = $0.08
  Streams: 600K events                         = $0.12
Lambda: 600K invocations                       ≈ $0.12 + duration
Storage: ~10 MB                                = negligible
─────────────────────────────────────────────────────────
Total: ~$0.70/month
```

#### Medium Volume: 1M requests/day

```
DynamoDB:
  Writes: 1M × 2 × 30 = 60M WCUs               = $37.50
  Reads:  1M × 2 × 30 = 60M RCUs               = $7.50
  Streams: 60M events                          = $12.00
Lambda: 60M invocations                        ≈ $12.00 + duration
─────────────────────────────────────────────────────────
Total (on-demand): ~$70/month
Total (provisioned with auto-scaling): ~$45/month
```

### Cost Reduction Strategies

#### 1. Disable Unused Features

```python
# Skip cascade if not needed (saves 1-2 WCUs per request)
async with limiter.acquire("entity", "api", limits, {"rpm": 1}, cascade=False):
    pass

# Disable stored limits if static (saves 2 RCUs per request)
limiter = RateLimiter(table_name="rate_limits", region="us-east-1")
```

#### 2. Optimize TTL Settings

```python
# Shorter TTL = faster cleanup = less storage
limiter = RateLimiter(
    table_name="rate_limits",
    region="us-east-1",
    bucket_ttl_seconds=3600,  # 1 hour vs 24 hour default
)
```

#### 3. Reduce Snapshot Granularity

```bash
# Deploy without aggregator if usage tracking not needed
zae-limiter deploy --table-name rate_limits --no-aggregator
```

#### 4. Switch to Provisioned at Scale

- **Break-even**: ~5M operations/month
- Use auto-scaling with 70% target utilization
- Consider reserved capacity for >20M ops/month

#### 5. Batch Similar Operations

```python
# Combine multiple limits into single acquire
async with limiter.acquire(
    "entity",
    "api",
    [rpm_limit, tpm_limit, daily_limit],
    {"rpm": 1},  # 1 transaction vs 3
):
    pass
```

### Cost Monitoring

Set up CloudWatch metrics for cost tracking:

**DynamoDB Metrics:**

- `ConsumedReadCapacityUnits`
- `ConsumedWriteCapacityUnits`
- `AccountProvisionedReadCapacityUtilization`
- `AccountProvisionedWriteCapacityUtilization`

**Lambda Metrics:**

- `Invocations`
- `Duration`
- `ConcurrentExecutions`

**Recommended Alerts:**

```bash
# Deploy with alarms for cost anomalies
zae-limiter deploy --table-name rate_limits \
  --alarm-sns-topic arn:aws:sns:us-east-1:123456789012:billing-alerts

# Set AWS Budgets alert at 80% of expected monthly cost
aws budgets create-budget \
  --account-id 123456789012 \
  --budget file://budget.json \
  --notifications-with-subscribers file://notifications.json
```

---

## Summary

| Optimization Area | Key Recommendations |
|-------------------|---------------------|
| Capacity | Start with on-demand, switch to provisioned at 5M+ ops/month |
| Latency | Expect 30-50ms p50 on AWS; use LocalStack for realistic testing |
| Throughput | Distribute load across entities to avoid contention |
| Cost | Disable cascade/stored_limits when not needed |
| Monitoring | Set up CloudWatch alerts for capacity and cost anomalies |

For detailed benchmark data, run:
```bash
python scripts/run_benchmarks.py --run-aws
```
