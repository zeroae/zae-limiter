# Performance Tuning Guide

This guide provides detailed recommendations for optimizing zae-limiter performance, covering DynamoDB capacity planning, Lambda configuration, and cost optimization strategies.

## 1. DynamoDB Capacity Planning

### Understanding RCU/WCU Costs

Each zae-limiter operation has specific DynamoDB capacity costs. Use this table for capacity planning:

| Operation | RCUs | WCUs | Notes |
|-----------|------|------|-------|
| `acquire()` | 1 | 1 | O(1) regardless of limit count (composite bucket items) |
| `acquire()` with cascade | 2 | 4 | Entity + parent bucket reads and writes (TransactWriteItems, 2 WCU per item) |
| `acquire()` retry (contention) | 0 | 1 | ADD-based writes don't require re-read |
| `acquire()` with adjustments | 0 | +1 per entity | Independent writes via `write_each()` (1 WCU each) |
| `acquire()` rollback (on exception) | 0 | +1 per entity | Independent compensating writes (1 WCU each) |
| `acquire(limits=None)` with config cache miss | +3 | 0 | +3 GetItem operations for config hierarchy |
| `available()` | 1 | 0 | Read-only, single composite bucket item |
| `get_limits()` | 1 | 0 | Query operation |
| `set_limits()` | 1 | N+1 | Query + N PutItems |
| `delete_entity()` | 1 | batched | Query + BatchWrite in 25-item chunks |

!!! note "O(1) Cost Optimization (v0.7.0)"
    ADR-114 (Composite Bucket Items) and ADR-115 (ADD-Based Writes) reduced `acquire()` costs
    from O(N) to O(1) where N is the number of limits. All limits for an entity+resource are
    stored in a single DynamoDB item, and ADD operations eliminate the need for read-modify-write
    cycles on contention retries.

!!! info "Capacity Validation"
    These costs are validated by automated tests. Run `uv run pytest tests/benchmark/test_capacity.py -v` to verify.

### Capacity Estimation Formula

Use these formulas to estimate hourly capacity requirements:

```
Hourly RCUs = requests/hour × (1 + cascade_pct + config_cache_miss_pct × 3)
Hourly WCUs = requests/hour × (1 + cascade_pct × 3)
```

!!! note "O(1) Scaling (v0.7.0+)"
    Costs no longer scale with the number of limits per request. Composite bucket items
    store all limits in a single DynamoDB item, so 1 limit and 10 limits cost the same.

With the Lambda aggregator enabled (2 windows: hourly, daily):
```
Additional WCUs = requests/hour × 2
```

### Example Calculations

#### Scenario 1: Simple API Rate Limiting

- 10,000 requests/hour
- 2 limits per request (rpm, tpm)
- No cascade, config cached

**Calculation:**
```
RCUs = 10,000 × 1.0 = 10,000 RCUs/hour
WCUs = 10,000 × 1.0 = 10,000 WCUs/hour
```

!!! note "Limit count doesn't affect cost"
    With composite bucket items (v0.7.0+), whether you track 1 limit or 10 limits,
    the DynamoDB cost is identical—all limits are stored in a single item.

#### Scenario 2: Hierarchical LLM Limiting

- 10,000 requests/hour
- 2 limits per request
- 50% use cascade (API key → project)
- 2% config cache miss rate

**Calculation:**
```
RCUs = 10,000 × (1 + 0.5 + 0.02×3) = 10,000 × 1.56 = 15,600 RCUs/hour
WCUs = 10,000 × (1 + 0.5×3) = 10,000 × 2.5 = 25,000 WCUs/hour
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
    {"rpm": 1},  # Initial consumption (1 request)
    limits=[rpm_limit, tpm_limit],
) as lease:
    # 1 BatchGetItem + 1 UpdateItem (1 WCU, single composite bucket)
    response = await call_llm()
    await lease.adjust(tpm=response.usage.total_tokens)
    # Adjustment: +1 UpdateItem via write_each (1 WCU)

# Inefficient: Separate acquisitions
async with limiter.acquire("entity-id", "llm-api", {"rpm": 1}, limits=[rpm_limit]):
    async with limiter.acquire("entity-id", "llm-api", {"tpm": 100}, limits=[tpm_limit]):
        # 2 reads + 2 writes (doubles cost!)
        pass
```

#### Cascade Optimization

```python
# Entity without cascade (default) — saves 1 GetEntity + parent bucket operations
await limiter.create_entity(entity_id="api-key", parent_id="project-1")

async with limiter.acquire("api-key", "llm-api", {"rpm": 1}, limits=limits):
    pass  # Only checks api-key's limits

# Entity with cascade — checks and updates parent limits too
await limiter.create_entity(entity_id="api-key", parent_id="project-1", cascade=True)

async with limiter.acquire("api-key", "llm-api", {"rpm": 1}, limits=limits):
    pass  # Checks both api-key AND project-1 limits
```

#### Write Sharding for High-Fanout Parents

When a parent entity has many children (1000+) with `cascade=True`, the parent partition may experience write throttling. DynamoDB limits throughput per partition to ~1,000 WCU (or ~3,000 RCU).

**Manual Write Sharding Solution:**

Instead of one parent, distribute ownership across multiple sharded parent entities:

```python
# OLD: Single parent becomes a hotspot
# ├── project-1 (parent)
# │   ├── api-key-1 (child, cascade=True)
# │   ├── api-key-2 (child, cascade=True)
# │   └── ... (1000+ children)

# NEW: Distribute across shards (e.g., 10 shards = 10x capacity)
num_shards = 10
api_key_id = "api-key-12345"
shard_id = hash(api_key_id) % num_shards
parent_id = f"project-1-shard-{shard_id}"

# Create shard parents once during setup
for shard in range(num_shards):
    parent_id = f"project-1-shard-{shard}"
    await limiter.create_entity(entity_id=parent_id, parent_id="project-1")
    # Set the same limits on all shards
    await limiter.set_limits(
        parent_id,
        [
            Limit.per_minute("rpm", capacity=10000),
            Limit.per_minute("tpm", capacity=100000),
        ],
        resource="llm-api"
    )

# For each child, assign to a random shard
shard_id = hash(api_key_id) % num_shards
sharded_parent = f"project-1-shard-{shard_id}"
await limiter.create_entity(
    entity_id=api_key_id,
    parent_id=sharded_parent,
    cascade=True
)

# On acquire, use the same sharding logic
shard_id = hash(api_key_id) % num_shards
sharded_parent = f"project-1-shard-{shard_id}"
async with limiter.acquire(api_key_id, "llm-api", {"rpm": 1}, limits=limits):
    pass  # Cascades to sharded parent instead of single hotspot
```

**Benefits:**
- Distributes parent write traffic across N partitions
- With 10 shards: ~10x capacity improvement
- Only requires application-level sharding logic

**Drawbacks:**
- More parent entities to manage
- Limits checked per shard (not globally across all shards)
- Requires hash consistency in sharding logic

**When to use:**
- Parent has >500 API keys with `cascade=True` and hitting throttling
- Cost-effective alternative to on-demand billing
- Temporary solution before implementing more sophisticated load distribution

#### Stored Limits Optimization

```python
# Config caching reduces RCUs (60s TTL by default)
limiter = RateLimiter(
    name="rate-limits",
    region="us-east-1",
    config_cache_ttl=60,  # seconds (0 to disable)
)

# Pass explicit limits to skip config resolution entirely
async with limiter.acquire(
    entity_id="user-123",
    resource="api",
    limits=[Limit.per_minute("rpm", 100)],  # No config lookup
    consume={"rpm": 1},
) as lease:
    ...
```

### Bulk Operations

```python
# Efficient bulk limit setup
await limiter.set_limits("entity-1", [rpm_limit, tpm_limit], resource="llm-api")
await limiter.set_limits("entity-2", [rpm_limit, tpm_limit], resource="llm-api")
# Runs 2 Queries + 2×2 PutItems

# Entity deletion (automatically batched in 25-item chunks)
await limiter.delete_entity("entity-2")
# Runs 1 Query + BatchWrite (up to 25 WCUs per chunk)
```

---

## 4. Expected Latencies

### Operation Latencies

Latencies vary by environment and depend on network conditions, DynamoDB utilization, and operation complexity.

| Operation | Moto p50 | LocalStack p50 | AWS (external) p50 | AWS (in-region) p50 |
|-----------|----------|----------------|--------------------|--------------------|
| `acquire()` - single limit | 9ms | 43ms | 38ms | 15-20ms |
| `acquire()` - two limits | 11ms | 43ms | 36ms | 15-20ms |
| `acquire()` with cascade | 22ms | 47ms | 48ms | 25-35ms |
| `available()` check | 1ms | 7ms | 10ms | 1-3ms |

!!! note "Environment Differences"
    - **Moto**: In-memory mock, measures code overhead only
    - **LocalStack**: Docker-based, includes local network latency (varies by host)
    - **AWS (external)**: From outside AWS, includes internet latency (~8-14ms per round-trip)
    - **AWS (in-region)**: From EC2/Lambda in same region (~0.5-1ms per round-trip)

!!! tip "In-Region Performance"
    When running inside AWS (same region as DynamoDB), latency drops significantly because
    network round-trips take <1ms instead of 8-14ms. For a typical LLM API call, rate limit
    overhead is ~4% (20ms / 500ms) vs ~7% when calling from external networks.

### Latency Breakdown

Typical `acquire()` latency breakdown for a single limit (non-cascade):

```
acquire() latency breakdown (external client):
├── Network to AWS               ~8-10ms   (internet latency)
├── DynamoDB GetItem             ~3-5ms    (server-side processing)
├── Network back                 ~8-10ms
├── UpdateItem                   ~3-5ms    (single-item API, 1 WCU)
└── Network back                 ~8-10ms
                                 ─────────
                         Total:  ~30-40ms

acquire() latency breakdown (in-region):
├── Network to DynamoDB          ~0.5-1ms  (VPC internal)
├── DynamoDB GetItem             ~3-5ms
├── Network back                 ~0.5-1ms
├── UpdateItem                   ~3-5ms    (single-item API, 1 WCU)
└── Network back                 ~0.5-1ms
                                 ─────────
                         Total:  ~10-15ms
```

!!! note "Single-item vs Transaction writes"
    Non-cascade `acquire()` writes a single composite bucket item, so `transact_write()`
    dispatches it as a plain UpdateItem (1 WCU). Cascade mode with 2 items uses
    TransactWriteItems (2 WCU per item). Adjustments and rollbacks always use
    independent single-item writes via `write_each()` (1 WCU each).

### Environment Selection

| Environment | Use Case | Latency Factor |
|-------------|----------|----------------|
| Moto | Unit tests, CI/CD | 1× (baseline) |
| LocalStack | Integration tests, local dev | 4-5× |
| AWS (external) | Development, testing | 4× |
| AWS (in-region) | Production | 2× |

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

| Scenario | Moto TPS | AWS TPS | Bottleneck |
|----------|----------|---------|------------|
| Sequential, single entity | 95 | 28 | Network round-trip |
| Sequential, multiple entities | 76 | 26 | Network round-trip |
| Concurrent, separate entities | 85 | 176 | Scales with parallelism |
| Concurrent, single entity | 88 | — | Optimistic locking contention |
| Cascade sequential | 27 | 19 | Parent bucket operations |
| Cascade concurrent | 28 | 91 | Parent bucket contention |

!!! note "AWS Concurrent Performance"
    AWS concurrent throughput (176 TPS) exceeds sequential (28 TPS) because parallel
    requests to separate entities eliminate serialization. In-region performance would
    be ~2× higher due to reduced network latency.

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
# Strategy 1: Higher capacity (reduces contention per request)
rpm_limit = Limit.per_minute("rpm", capacity=1000)

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

!!! note "O(1) Costs (v0.7.0+)"
    With composite bucket items, costs no longer multiply by number of limits.
    Whether you track 2 limits or 10 limits per request, DynamoDB costs are the same.

#### Low Volume: 10K requests/day

```
DynamoDB:
  Writes: 10K × 30 days = 300K WCUs            = $0.19
  Reads:  10K × 30 days = 300K RCUs            = $0.04
  Streams: 300K events                         = $0.06
Lambda: 300K invocations                       ≈ $0.06 + duration
Storage: ~10 MB                                = negligible
─────────────────────────────────────────────────────────
Total: ~$0.35/month
```

#### Medium Volume: 1M requests/day

```
DynamoDB:
  Writes: 1M × 30 = 30M WCUs                   = $18.75
  Reads:  1M × 30 = 30M RCUs                   = $3.75
  Streams: 30M events                          = $6.00
Lambda: 30M invocations                        ≈ $6.00 + duration
─────────────────────────────────────────────────────────
Total (on-demand): ~$35/month
Total (provisioned with auto-scaling): ~$22/month
```

### Cost Reduction Strategies

#### 1. Disable Unused Features

```{.python .lint-only}
# Create entity without cascade if not needed (saves 1-2 WCUs per request)
await limiter.create_entity(entity_id="entity", parent_id="project-1")  # cascade=False by default
async with limiter.acquire("entity", "api", limits, {"rpm": 1}):
    pass

# Disable stored limits if static (saves 2 RCUs per request)
limiter = RateLimiter(name="rate-limits", region="us-east-1")
```

#### 2. Optimize TTL Settings

```python
# Shorter TTL = faster cleanup = less storage
# bucket_ttl_seconds is configured via StackOptions or CloudFormation
limiter = RateLimiter(name="rate-limits", region="us-east-1")
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
    entity_id="entity",
    resource="api",
    consume={"rpm": 1},  # 1 transaction vs 3
    limits=[rpm_limit, tpm_limit, daily_limit],
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

## 7. Config Cache Tuning

The config cache reduces DynamoDB reads by caching system defaults, resource defaults, and entity limits. This section covers tuning the cache for your workload.

### Cache Configuration

```python
from zae_limiter import RateLimiter, Repository

# Default: 60-second TTL (recommended for most workloads)
limiter = RateLimiter(
    repository=Repository(name="my-app", region="us-east-1"),
    config_cache_ttl=60,  # Default
)

# High-frequency updates: Shorter TTL for faster propagation
limiter = RateLimiter(
    repository=Repository(name="my-app", region="us-east-1"),
    config_cache_ttl=10,  # 10 seconds - faster updates, more cache misses
)

# Disable caching: For testing or when config changes must be immediate
limiter = RateLimiter(
    repository=Repository(name="my-app", region="us-east-1"),
    config_cache_ttl=0,  # Disabled - every acquire reads from DynamoDB
)
```

### Cost Impact

Without caching, each `acquire()` call performs 3 DynamoDB reads to resolve limits:

1. Entity-level config lookup (1 RCU)
2. Resource-level config lookup (1 RCU)
3. System-level config lookup (1 RCU)

With caching (default):

| Traffic Rate | Cache Hit Rate | Amortized RCU/request |
|--------------|----------------|----------------------|
| 1 req/sec | 98.3% | 0.05 RCU |
| 10 req/sec | 99.8% | 0.005 RCU |
| 100 req/sec | 99.98% | 0.0005 RCU |

**Negative caching** also helps: When an entity has no custom config (95%+ of entities typically), the cache remembers this to avoid repeated lookups.

### Manual Invalidation

After modifying config, you can force immediate refresh:

```python
# Update config
await limiter.set_system_defaults([Limit.per_minute("rpm", 1000)])

# Force immediate cache refresh (optional)
await limiter.invalidate_config_cache()
```

Without manual invalidation, changes propagate within the TTL period (max 60 seconds by default).

### Monitoring Cache Performance

```python
# Get cache statistics
stats = limiter.get_cache_stats()
total = stats.hits + stats.misses
print(f"Cache hit rate: {stats.hits / total:.1%}" if total else "No requests yet")
print(f"Cache entries: {stats.size}")
print(f"TTL: {stats.ttl_seconds}s")
```

### TTL Selection Guidelines

| Scenario | Recommended TTL | Rationale |
|----------|-----------------|-----------|
| Production (stable config) | 60s (default) | Best cost/latency trade-off |
| Development/testing | 10-30s | Faster config iteration |
| Compliance-critical | 10-30s | Minimizes staleness |
| Testing with frequent changes | 0 (disabled) | Immediate visibility |
| High-traffic APIs (>100 req/s) | 60-120s | Maximize cache hits |

---

## Summary

| Optimization Area | Key Recommendations |
|-------------------|---------------------|
| Capacity | Start with on-demand, switch to provisioned at 5M+ ops/month |
| Latency | Expect 15-20ms p50 in-region, 35-45ms external; network is the dominant factor |
| Throughput | Distribute load across entities to avoid contention |
| Cost | Disable cascade/stored_limits when not needed |
| Config Cache | Use default 60s TTL; invalidate manually for immediate changes |
| Monitoring | Set up CloudWatch alerts for capacity and cost anomalies |

For detailed benchmark data, run:
```bash
python scripts/run_benchmarks.py --run-aws
```
