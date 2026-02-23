# Performance Tuning Guide

This guide provides detailed recommendations for optimizing zae-limiter performance, covering DynamoDB capacity planning, Lambda configuration, and cost optimization strategies.

## 1. DynamoDB Capacity Planning

### Understanding RCU/WCU Costs

Each zae-limiter operation has specific DynamoDB capacity costs. Use this table for capacity planning:

| Operation | RCUs | WCUs | Notes |
|-----------|------|------|-------|
| `acquire()` | 1 | 1 | O(1) regardless of limit count (composite bucket items) |
| `acquire()` with cascade | 2 | 4 | Entity + parent bucket reads and writes (TransactWriteItems, 2 WCU per item) |
| `acquire()` speculative success | 0 | 1 | Skips read; conditional UpdateItem (issue #315) |
| `acquire()` speculative success + cascade (sequential) | 0 | 2 | Child then parent speculative UpdateItem |
| `acquire()` speculative success + cascade (parallel) | 0 | 2 | Concurrent child + parent via entity cache (issue #318) |
| `acquire()` speculative fast rejection | 0 | 0 | Exhausted bucket; rejected from ALL_OLD without write |
| `acquire()` speculative fallback (non-cascade) | 1 | 2 | Failed speculative (1 WCU) + normal path (1 RCU + 1 WCU) |
| `acquire()` speculative cascade fallback (parent refill helps) | 0.5 | 3 | Child stays consumed; parent-only read (0.5 RCU) + single-item write (1 WCU) |
| `acquire()` retry (contention) | 0 | 1 | ADD-based writes don't require re-read |
| `acquire()` with adjustments | 0 | +1 per entity | Independent writes via `write_each()` (1 WCU each) |
| `acquire()` rollback (on exception) | 0 | +1 per entity | Independent compensating writes (1 WCU each) |
| Aggregator bucket refill (per active bucket) | 0 | 1 | Proactive refill via Lambda; 0 WCU if lock lost |
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

!!! info "Namespace Overhead"
    Namespace-prefixed keys (e.g., `{ns}/ENTITY#id`) add a few bytes per item but have no measurable impact on RCU/WCU costs. All operations in the table above apply identically regardless of namespace.

!!! info "Capacity Validation"
    These costs are validated by automated tests. Run `uv run pytest tests/benchmark/test_capacity.py -v` to verify.

### Capacity Estimation Formula

Use these formulas to estimate hourly capacity requirements:

```
Hourly RCUs = requests/hour × (1 + cascade_pct + config_cache_miss_pct × 3)
Hourly WCUs = requests/hour × (1 + cascade_pct × 3)
```

With speculative writes enabled (`speculative_writes=True`), the steady-state formula changes:
```
Hourly RCUs = requests/hour × (fallback_pct + cascade_pct × 0.5 + config_cache_miss_pct × 3)
Hourly WCUs = requests/hour × (1 + cascade_pct)
```

Where `fallback_pct` is the fraction of requests that fall back to the slow path (typically <5% for pre-warmed buckets).

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
zae-limiter deploy --name my-app \
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

#### Write Sharding (Automatic Pre-Shard Buckets)

Starting with v0.9.0 (GHSA-76rv), zae-limiter automatically handles DynamoDB hot partition
mitigation via **pre-shard buckets**. Each bucket item lives on its own DynamoDB partition
key (`PK={ns}/BUCKET#{id}#{resource}#{shard}`), and an auto-injected `wcu:1000`
infrastructure limit tracks per-partition write pressure.

**How it works:**

1. Every bucket starts with `shard_count=1` (shard 0)
2. An internal `wcu` limit (capacity: 1000 millitokens) is auto-injected on every bucket
3. When `wcu` is exhausted on a speculative write, the client doubles `shard_count` via a
   conditional write on shard 0 (source of truth)
4. The Lambda aggregator proactively doubles shards at >=80% wcu capacity before clients
   experience throttling
5. Shard count changes on shard 0 are propagated to all other shards by the aggregator
6. Clients pick a random shard from the entity cache: `random.randrange(shard_count)`
7. If application limits are exhausted on one shard but the entity has multiple shards,
   the client retries on up to 2 other randomly chosen shards

**Shard-aware capacity:** The aggregator divides effective capacity and refill amount
by `shard_count` when computing refills, so each shard receives its proportional share
of tokens.

**No application code changes required.** Pre-shard buckets are transparent to users.
The `wcu` limit is filtered from all user-facing output (bucket states, exceptions,
usage snapshots).

**When automatic sharding is insufficient:**

For extreme high-fanout cascade scenarios (1000+ children with `cascade=True` writing to the
same parent), automatic bucket sharding handles the per-bucket partition pressure. However,
if you need to distribute traffic across multiple *logical* parents for application-level
load balancing, you can still use manual entity sharding:

```python
# Manual entity sharding for application-level distribution
# (Only needed for extreme cascade fan-out beyond what pre-shard handles)
num_shards = 10
api_key_id = "api-key-12345"
shard_id = hash(api_key_id) % num_shards
parent_id = f"project-1-shard-{shard_id}"

await limiter.create_entity(
    entity_id=api_key_id,
    parent_id=parent_id,
    cascade=True,
)
```

#### Stored Limits Optimization

```python
# Config caching reduces RCUs (60s TTL by default)
repo = await Repository.open(config_cache_ttl=60)  # seconds (0 to disable)
limiter = RateLimiter(repository=repo)

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

With speculative writes enabled (`speculative_writes=True`), the read round trip is eliminated on success:

```
speculative acquire() latency breakdown (in-region, success):
├── Network to DynamoDB          ~0.5-1ms  (VPC internal)
├── Conditional UpdateItem       ~3-5ms    (1 WCU, skips read)
└── Network back                 ~0.5-1ms
                                 ─────────
                         Total:  ~5-8ms
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
rpm_limit = Limit.per_minute("rpm", 1000)

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
repo = await Repository.open()
limiter = RateLimiter(repository=repo)
```

#### 2. Optimize TTL Settings

```python
# Shorter TTL = faster cleanup = less storage
# bucket_ttl is configured via builder or CloudFormation
repo = await Repository.open()
limiter = RateLimiter(repository=repo)
```

#### 3. Reduce Snapshot Granularity

```bash
# Deploy without aggregator if usage tracking not needed
zae-limiter deploy --name my-app --no-aggregator
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
zae-limiter deploy --name my-app \
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
repo = await Repository.open(config_cache_ttl=60)
limiter = RateLimiter(repository=repo)

# High-frequency updates: Shorter TTL for faster propagation
repo = await Repository.open(config_cache_ttl=10)
limiter = RateLimiter(repository=repo)

# Disable caching: For testing or when config changes must be immediate
repo = await Repository.open(config_cache_ttl=0)
limiter = RateLimiter(repository=repo)
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

### Automatic Cache Eviction

Config-modifying methods (`set_limits()`, `delete_limits()`) automatically evict relevant cache entries. Manual invalidation is only needed after external changes (e.g., direct DynamoDB writes or changes from another process).

### Manual Invalidation

After external config changes, force immediate refresh:

```python
await repo.invalidate_config_cache()
```

Without manual invalidation, changes propagate within the TTL period (max 60 seconds by default).

### Monitoring Cache Performance

```python
# Get cache statistics
stats = repo.get_cache_stats()
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

## 8. Speculative Writes

Speculative writes (issue #315) enable a fast path for `acquire()` that skips the read round trip by issuing a conditional `UpdateItem` directly. This is most effective for pre-warmed buckets with sufficient capacity.

### Configuring Speculative Writes

```python
from zae_limiter import RateLimiter, Repository

repo = await Repository.open()
limiter = RateLimiter(repository=repo, speculative_writes=True)  # Default
```

### How It Works

Instead of the normal read-then-write flow (BatchGetItem + UpdateItem), the speculative path attempts a conditional UpdateItem first.

**First acquire (sequential, populates entity cache):**

```
acquire(entity_id, resource, consume)
|
+- Speculative UpdateItem (condition: bucket exists AND has enough tokens)
   |
   +- SUCCEEDS -> read cascade/parent_id from ALL_NEW, populate entity cache
   |  +- cascade=False -> DONE (1 RT, 0 RCU, 1 WCU)
   |  +- cascade=True -> Parent speculative UpdateItem (sequential)
   |     +- SUCCEEDS -> DONE (2 RT, 0 RCU, 2 WCU)
   |     +- FAILS -> [parent failure handling, see below]
   |
   +- FAILS (ConditionalCheckFailedException)
      +- No ALL_OLD (bucket missing) -> SLOW PATH (creates bucket)
      +- Missing limit in ALL_OLD -> SLOW PATH
      +- Refill would help -> SLOW PATH
      +- Refill won't help -> RateLimitExceeded (0 RCU, 0 WCU)
```

**Subsequent acquires (parallel, issue #318):**

When the entity cache contains `(cascade=True, parent_id)` from a prior acquire, child and parent speculative writes are issued concurrently:

```
acquire(entity_id, resource, consume)   [cache hit: cascade=True, parent_id known]
|
+- asyncio.gather(child_speculative, parent_speculative)
   |
   +- BOTH SUCCEED -> DONE (1 RT, 0 RCU, 2 WCU)
   +- CHILD FAILS, PARENT SUCCEEDS -> Compensate parent, check child ALL_OLD
   |  +- [same child failure handling as sequential path]
   +- CHILD SUCCEEDS, PARENT FAILS -> Check parent ALL_OLD (child stays consumed)
   |  +- No ALL_OLD (missing) -> Compensate child, SLOW PATH
   |  +- Missing limit -> Compensate child, SLOW PATH
   |  +- Refill won't help -> Compensate child, RateLimitExceeded
   |  +- Refill would help -> Parent-only slow path (keep child)
   |     +- Parent acquire succeeds -> DONE (2 RT, 0.5 RCU, 3 WCU)
   |     +- Parent acquire fails -> Compensate child, SLOW PATH
   +- BOTH FAIL -> Check child ALL_OLD, fall back or fast-reject
```

The `ReturnValuesOnConditionCheckFailure=ALL_OLD` response provides the current bucket state on failure, allowing the limiter to determine whether refill would help without an additional read.

**Deferred cascade compensation:** When the child speculative write succeeds but the parent fails with "refill would help", the child's consumption is kept in place while a parent-only slow path is attempted. This avoids compensating the child (1 WCU), re-reading it (0.5 RCU), and using TransactWriteItems for the full cascade write (4 WCU). Instead, only the parent is read (0.5 RCU) and written via a single-item UpdateItem (1 WCU). Compensation only happens when the parent-only path also fails.

**Entity metadata cache (issue #318):** `Repository._entity_cache` stores `{entity_id: (cascade, parent_id)}` as immutable metadata with no TTL. After the first acquire populates the cache (from `ALL_NEW` on speculative success or from the entity META record on slow path), subsequent cascade acquires fire child and parent speculative writes concurrently via `asyncio.gather` inside `speculative_consume()`. This reduces cascade latency from 2 sequential round trips to 1 parallel round trip while maintaining the same WCU cost. In sync mode, `asyncio.gather` is transformed to `self._run_in_executor(lambda: a, lambda: b)` using a lazy `ThreadPoolExecutor(max_workers=2)` for true parallel execution.

### Cost Comparison

| Scenario | Round Trips | RCU | WCU | Cost per 1M |
|----------|-------------|-----|-----|-------------|
| **Normal path** (non-cascade) | 2 | 1 | 1 | $0.75 |
| **Speculative success** (non-cascade) | 1 | 0 | 1 | $0.625 |
| **Speculative fast rejection** (exhausted) | 1 | 0 | 0 | $0.00 |
| **Speculative fallback** (refill helps) | 3 | 1 | 2 | $1.375 |
| **Normal path** (cascade) | 3 | 2 | 4 | $1.75 |
| **Speculative success** (cascade, sequential) | 2 | 0 | 2 | $1.25 |
| **Speculative success** (cascade, parallel) | 1 | 0 | 2 | $1.25 |
| **Speculative cascade fallback** (parent refill helps) | 2+ | 0.5 | 3 | $2.00 |
| **Speculative cascade fast rejection** (parent exhausted) | 1 | 0 | 2 | $1.25 |

!!! note "When speculative writes save money"
    The speculative path is cheaper than the normal path when most requests succeed without needing refill. If a high percentage of requests fall back to the slow path (new entities, near-capacity buckets, frequent config changes), the extra WCU from the failed speculative write makes it more expensive.

### Latency Comparison

| Scenario | Round Trips | Expected Latency (in-region) |
|----------|-------------|------------------------------|
| Normal path (non-cascade) | 2 | 10-15ms |
| Speculative success (non-cascade) | 1 | 5-8ms |
| Speculative fast rejection (exhausted) | 1 | 5-8ms |
| Speculative fallback (refill helps) | 3 | 15-22ms |
| Normal path (cascade) | 3 | 15-22ms |
| Speculative success (cascade, sequential) | 2 | 8-12ms |
| Speculative success (cascade, parallel) | 1 | 5-8ms |
| Speculative cascade fallback (parent refill helps) | 2+ | 12-20ms |
| Speculative cascade fast rejection (parent exhausted) | 1 | 5-8ms |

### Aggregator-Assisted Refill (Issue #317)

When the Lambda aggregator is enabled, it proactively refills token buckets for active entities between client requests. This keeps speculative writes on the fast path (1 RT, 0 RCU, 1 WCU) by ensuring buckets have sufficient tokens, reducing fallback to the slow path (3 RT, 1 RCU, 2 WCU).

**How it works:**

1. The aggregator processes DynamoDB Stream events for bucket modifications
2. For each active (entity, resource) bucket, it aggregates consumption deltas from the batch
3. If projected tokens after natural refill are insufficient to cover the observed consumption rate, it writes a proactive refill
4. The refill uses `ADD` (commutative with concurrent speculative writes) and an optimistic lock on `rf` to prevent double-refill

**Cost:** 1 WCU per refill written (0 WCU if another writer updated `rf` first). The cost is amortized across all stream records in a batch, so high-throughput workloads see fewer refills per request.

!!! tip "Aggregator refill + speculative writes"
    The combination of aggregator-assisted refill and speculative writes provides the best latency and cost profile: the aggregator keeps buckets warm so speculative writes rarely fall back, achieving ~5-8ms p50 latency at $0.625/M requests (non-cascade).

### When to Use Speculative Writes

**Good fit:**

- High-throughput workloads with pre-warmed buckets
- Buckets that rarely exhaust capacity (high capacity relative to request rate)
- Latency-sensitive applications where saving one round trip matters
- Cascade entities with repeated acquires (entity cache enables parallel writes after first acquire)
- Deployments with the Lambda aggregator enabled (aggregator keeps buckets warm for speculative success)

**Poor fit:**

- New entities that have never been seen before (first acquire always falls back)
- Near-capacity buckets that frequently exhaust (high fallback rate)
- Workloads with frequent config changes (missing limits trigger fallback)
- One-shot entities that are only acquired once (entity cache provides no benefit)

### Monitoring Speculative Effectiveness

Track the ratio of speculative successes to fallbacks to determine if speculative writes are beneficial for your workload:

```python
# Speculative writes work transparently with acquire()
# Monitor DynamoDB ConsumedWriteCapacityUnits to observe:
# - Lower WCU = more speculative successes
# - Higher WCU = more fallbacks (consider disabling)
```

!!! tip "Disabling speculative writes"
    If most requests are from new entities or near-capacity buckets, disable with `speculative_writes=False` to avoid the extra WCU from failed speculative attempts.

---

## 9. Load Testing with Locust

For realistic, multi-user load testing against a live DynamoDB stack, zae-limiter provides a Locust integration module (`zae_limiter.locust`). It exposes `RateLimiterUser` and `RateLimiterSession`, analogous to Locust's built-in `HttpUser` and `HttpSession`, so that every `acquire()`, `available()`, and management call fires Locust request events with timing.

### Installation

Install the `[bench]` extra to pull in Locust and its dependencies:

```{.python .requires-external}
# pip install zae-limiter[bench]
```

### Quick Start

```{.python .requires-external}
from locust import task
from zae_limiter.locust import RateLimiterUser

class MyUser(RateLimiterUser):
    stack_name = "my-limiter"

    @task
    def do_acquire(self):
        with self.client.acquire(
            entity_id="user-123",
            resource="gpt-4",
            consume={"rpm": 1, "tpm": 500},
            name="gpt-4/baseline",
        ):
            pass  # simulate work
```

Run with:

```bash
locust -f locustfile.py --host <stack-name>
```

### Key Design Points

- **Shared limiter:** A single `SyncRateLimiter` instance is shared across all Locust user greenlets (thread-safe via boto3).
- **Connection pool:** `_configure_boto3_pool()` automatically enlarges the boto3 connection pool (default 1000, override with `BOTO3_MAX_POOL` env var) to prevent pool exhaustion under high concurrency.
- **Event types:** `ACQUIRE`, `COMMIT`, `RATE_LIMITED`, `AVAILABLE`, and management operations (`SET_SYSTEM_DEFAULTS`, `CREATE_ENTITY`, etc.) appear as distinct request types in the Locust UI.
- **Rate limit handling:** `RateLimitExceeded` is tracked as `RATE_LIMITED` (not counted as a failure), so Locust statistics cleanly separate infrastructure errors from expected rate limiting.

### Example Scenarios

Pre-built locustfiles are available in `examples/locust/locustfiles/`:

| Scenario | File | Description |
|----------|------|-------------|
| Simple | `simple.py` | Single resource, single limit, basic `acquire` |
| Max RPS | `max_rps.py` | Zero-wait back-to-back `acquire` for throughput ceiling |
| LLM Gateway | `llm_gateway.py` | 8 LLM models with RPM + TPM and lease adjustments |
| LLM Production | `llm_production.py` | Weighted tasks with custom daily/spike load shapes |
| Stress | `stress.py` | 16K entities with whale/spike/power-law traffic patterns |

See `examples/locust/README.md` for full usage instructions including distributed execution on AWS.

---

## Summary

| Optimization Area | Key Recommendations |
|-------------------|---------------------|
| Capacity | Start with on-demand, switch to provisioned at 5M+ ops/month |
| Latency | Expect 15-20ms p50 in-region, 35-45ms external; network is the dominant factor |
| Throughput | Distribute load across entities to avoid contention |
| Cost | Disable cascade/stored_limits when not needed |
| Config Cache | Use default 60s TTL; invalidate manually for immediate changes |
| Speculative Writes | Enable for pre-warmed high-throughput workloads; saves 1 round trip on success; cascade entities get parallel writes after first acquire |
| Load Testing | Use `zae_limiter.locust` with `RateLimiterUser` for realistic multi-user load tests; see `examples/locust/` |
| Monitoring | Set up CloudWatch alerts for capacity and cost anomalies |

For detailed benchmark data, run:
```bash
python scripts/run_benchmarks.py --run-aws
```
