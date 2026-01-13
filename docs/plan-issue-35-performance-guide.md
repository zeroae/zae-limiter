# Implementation Plan: Performance Tuning Guide (Issue #35)

## Overview

Create comprehensive performance tuning documentation for zae-limiter covering DynamoDB capacity planning, Lambda optimization, and cost strategies.

**Issue**: #35 - 📝 docs: create performance tuning guide
**Target File**: `docs/performance.md`
**Estimated Effort**: 4-6 hours
**Labels**: `documentation`, `v1.0.0`

---

## Document Structure

### File Location
- Primary: `docs/performance.md`
- Navigation: Add to `mkdocs.yml` as top-level "Performance Tuning" section (after Infrastructure)

### mkdocs.yml Changes
```yaml
nav:
  # ... existing entries ...
  - Infrastructure:
      - Deployment: infra/deployment.md
      - LocalStack: infra/localstack.md
      - CloudFormation: infra/cloudformation.md
      - Migrations: migrations.md
  - Performance Tuning: performance.md  # NEW
  - CLI Reference: cli.md
  # ... rest ...
```

---

## Content Outline

### 1. DynamoDB Capacity Planning

#### 1.1 Understanding RCU/WCU Costs

Document the exact DynamoDB costs for each operation based on `repository.py` analysis:

| Operation | RCUs | WCUs | Notes |
|-----------|------|------|-------|
| `acquire()` - single limit | 1 | 1 | GetItem + TransactWrite |
| `acquire()` - multiple limits (N) | N | N | N GetItems + TransactWrite(N) |
| `acquire()` with `cascade=True` | 3 | 2 | +GetEntity, +parent bucket |
| `acquire()` with `use_stored_limits` | 2+N | N | +2 Queries for limits |
| `available()` - check only | 1 per limit | 0 | GetItem only, no transaction |
| `get_limits()` | 1 | 0 | Query operation |
| `set_limits()` | 1 | N | Query + N PutItems |
| `delete_entity()` | 1 | up to 25 | Query + BatchWrite chunks |
| Stream processing (Lambda) | 0 | 1 per delta per window | UpdateItem with ADD |

#### 1.2 Capacity Estimation Formula

Provide formulas for estimating capacity:

```
Hourly RCUs = requests_per_hour × avg_limits_per_request × (1 + cascade_pct × 2 + stored_limits_pct × 2)
Hourly WCUs = requests_per_hour × avg_limits_per_request × (1 + cascade_pct)

With Lambda aggregator (2 windows: hourly, daily):
Additional WCUs = requests_per_hour × avg_limits_per_request × 2
```

#### 1.3 Example Calculations

Provide concrete examples:

**Scenario 1: Simple API rate limiting**
- 10,000 requests/hour
- 2 limits per request (rpm, tpm)
- No cascade, no stored limits
- Result: 20,000 RCUs + 20,000 WCUs per hour

**Scenario 2: Hierarchical LLM limiting**
- 10,000 requests/hour
- 2 limits, cascade enabled (50%)
- Stored limits for some entities (20%)
- Result: ~28,000 RCUs + 25,000 WCUs per hour

#### 1.4 Billing Mode Selection

Document when to use each mode:

| Mode | Best For | Trade-offs |
|------|----------|------------|
| PAY_PER_REQUEST (default) | Variable/unpredictable traffic, new deployments | Higher per-request cost, no capacity planning needed |
| Provisioned | Steady traffic >100 TPS | Lower cost at scale, requires planning, auto-scaling setup |
| Provisioned + Reserved | High-volume production | Lowest cost, 1-year commitment |

Include migration guidance for switching modes.

---

### 2. Lambda Concurrency Settings

#### 2.1 Default Configuration

Document defaults from `cfn_template.yaml`:

| Setting | Default | Range | Impact |
|---------|---------|-------|--------|
| Memory | 256 MB | 128-3008 MB | Higher memory = faster, more expensive |
| Timeout | 60 seconds | 1-900 seconds | Should be 2x typical duration |
| Reserved Concurrency | None | 1-1000 | Limits parallel executions |

#### 2.2 Memory Tuning

Explain memory/CPU relationship:
- Lambda CPU scales linearly with memory
- 256 MB: ~0.15 vCPUs (suitable for most workloads)
- 512 MB: ~0.30 vCPUs (for high-throughput streams)
- 1024 MB: ~0.60 vCPUs (rarely needed)

Provide guidance based on batch size:
- <50 records/batch: 128-256 MB sufficient
- 50-100 records/batch: 256-512 MB recommended
- Peak streams: Monitor Lambda duration, increase if >50% of timeout

#### 2.3 Concurrency Management

Document stream parallelization:
- DynamoDB Streams: 1 Lambda per shard (default 4 shards)
- Shard count scales with write throughput
- Reserved concurrency prevents runaway scaling

Recommendations:
```
Low volume (<1000 writes/sec): No reserved concurrency needed
Medium volume (1000-10000/sec): Consider 10-50 reserved
High volume (>10000/sec): Set to expected max shards + 20%
```

#### 2.4 Error Handling

Document error behavior:
- Failed records retry within same batch (up to 3 times)
- Persistent failures go to DLQ (if configured)
- Duration alarms trigger at 80% of timeout (48s default)

---

### 3. Batch Operation Patterns

#### 3.1 Transaction Limits

Document DynamoDB transaction constraints:
- Max 100 items per `TransactWriteItems`
- Max 100 items per `BatchWriteItem` (but processed in 25-item chunks)
- Optimistic locking: entire transaction fails if any condition check fails

#### 3.2 Efficient Patterns

**Multi-limit acquisition** (from `limiter.py`):
```python
# Efficient: Single lease for multiple limits
async with limiter.acquire("entity", "llm-api", [rpm_limit, tpm_limit]) as lease:
    # 2 GetItems + 1 TransactWrite (2 items)
    pass

# Inefficient: Separate acquisitions
async with limiter.acquire("entity", "llm-api", [rpm_limit]) as lease1:
    async with limiter.acquire("entity", "llm-api", [tpm_limit]) as lease2:
        # 2 GetItems + 2 TransactWrites
        pass
```

**Cascade optimization**:
```python
# Only use cascade when hierarchical limits are needed
async with limiter.acquire("api-key", "llm-api", limits, cascade=False) as lease:
    # Saves: 1 GetEntity + parent bucket operations
    pass
```

**Stored limits optimization**:
```python
# Only enable when limits vary per entity
limiter = RateLimiter(..., use_stored_limits=False)  # Default: saves 2 Queries

# Enable only for dynamic limit scenarios
limiter = RateLimiter(..., use_stored_limits=True)  # +2 Queries per acquire
```

#### 3.3 Bulk Operations

Document bulk entity management:
```python
# Efficient bulk limit setup (batched internally)
await limiter.set_limits("entity-1", "llm-api", [rpm_limit, tpm_limit])
await limiter.set_limits("entity-2", "llm-api", [rpm_limit, tpm_limit])
# Runs 2 Queries + 2×2 PutItems

# Entity deletion (batched in 25-item chunks)
await limiter.delete_entity("entity-id")
# Runs 1 Query + BatchWrite (up to 25 WCUs per chunk)
```

---

### 4. Expected Latencies

#### 4.1 Operation Latencies

Document expected p50/p95/p99 latencies based on benchmark tests:

| Operation | p50 | p95 | p99 | Notes |
|-----------|-----|-----|-----|-------|
| `acquire()` - single limit | ~5ms | ~10ms | ~20ms | Moto benchmarks |
| `acquire()` - 2 limits | ~7ms | ~15ms | ~25ms | +2ms per additional limit |
| `acquire()` with cascade | ~10ms | ~20ms | ~35ms | +GetEntity + parent |
| `available()` check | ~3ms | ~8ms | ~15ms | No transaction overhead |
| Lambda aggregator batch | ~20ms | ~50ms | ~100ms | 10-record batch |

**Note**: Include disclaimer that actual latencies depend on:
- AWS region and proximity
- DynamoDB table utilization
- Network conditions
- Instance type (for self-hosted)

#### 4.2 Latency Breakdown

Document where time is spent:
```
acquire() latency breakdown (single limit):
├── DynamoDB GetItem (bucket)     ~2-5ms
├── Bucket calculation            <1ms (in-memory)
├── TransactWriteItems            ~3-8ms
└── Network overhead              variable
```

#### 4.3 LocalStack vs AWS Comparison

Document that LocalStack has higher latency (~2-5x):
- Useful for functional testing
- Not representative of production performance
- Use `tests/benchmark/test_localstack.py` for realistic testing

---

### 5. Throughput Benchmarks

#### 5.1 Maximum Throughput

Document theoretical and practical limits:

| Scenario | Theoretical Max | Practical Max | Bottleneck |
|----------|-----------------|---------------|------------|
| Single entity, single limit | ~3000 TPS | ~1000 TPS | Optimistic locking contention |
| Single entity, cascade | ~1500 TPS | ~500 TPS | Parent contention |
| Multiple entities | 40,000+ TPS | 25,000+ TPS | DynamoDB partition limits |

#### 5.2 Contention Analysis

Explain optimistic locking behavior:
- Same bucket updated concurrently → transaction retry
- Retry adds latency (~5-10ms per retry)
- High contention → exponential backoff recommended

Mitigation strategies:
```python
# Strategy 1: Larger bucket windows
rpm_limit = Limit.rpm(capacity=1000, window_seconds=60)  # Less contention

# Strategy 2: Pre-warm buckets for hot entities
await limiter._repo.get_or_create_bucket(...)

# Strategy 3: Client-side rate limiting before acquire
# Reduce concurrent requests to same entity
```

#### 5.3 Running Benchmarks

Provide instructions for running project benchmarks:
```bash
# Moto benchmarks (fast, mocked)
uv run pytest tests/benchmark/test_operations.py -v --benchmark-json=results.json

# LocalStack benchmarks (realistic latency)
docker compose up -d
export AWS_ENDPOINT_URL=http://localhost:4566
uv run pytest tests/benchmark/test_localstack.py -v --benchmark-json=results.json

# Analyze results
python -c "import json; print(json.load(open('results.json'))['benchmarks'])"
```

---

### 6. Cost Optimization Strategies

#### 6.1 DynamoDB Cost Breakdown

Document cost components (us-east-1 pricing as reference):

| Component | Cost | Notes |
|-----------|------|-------|
| On-Demand Write | $1.25 per million WCUs | PAY_PER_REQUEST |
| On-Demand Read | $0.25 per million RCUs | PAY_PER_REQUEST |
| Storage | $0.25 per GB/month | Usually minimal |
| Streams | $0.02 per 100K reads | Lambda polling |
| Lambda | $0.20 per million requests + duration | Aggregator |

#### 6.2 Cost Estimation Examples

Provide monthly cost examples:

**Example 1: Low volume (10K requests/day)**
```
DynamoDB:
  Writes: 10K × 2 limits × 30 days = 600K WCUs = $0.75
  Reads:  10K × 2 limits × 30 days = 600K RCUs = $0.15
  Streams: 600K events = $0.12
Lambda: 600K invocations ≈ $0.12 + duration
Storage: ~10 MB = negligible
Total: ~$1.15/month
```

**Example 2: Medium volume (1M requests/day)**
```
DynamoDB:
  Writes: 1M × 2 × 30 = 60M WCUs = $75.00
  Reads:  1M × 2 × 30 = 60M RCUs = $15.00
  Streams: 60M events = $12.00
Lambda: 60M invocations ≈ $12.00 + duration
Total: ~$115/month (on-demand)
       ~$70/month (provisioned with auto-scaling)
```

#### 6.3 Cost Reduction Strategies

Document actionable strategies:

1. **Disable unused features**
   ```python
   # Skip cascade if not needed
   limiter.acquire(..., cascade=False)  # Saves 1-2 WCUs

   # Skip stored limits if static
   limiter = RateLimiter(..., use_stored_limits=False)  # Saves 2 RCUs
   ```

2. **Optimize TTL settings**
   ```python
   # Shorter TTL = faster cleanup = less storage
   RateLimiter(..., bucket_ttl_seconds=3600)  # 1 hour vs 24 hour default
   ```

3. **Reduce snapshot granularity**
   ```bash
   # Only hourly snapshots (skip daily)
   zae-limiter deploy --snapshot-windows hourly
   ```

4. **Switch to provisioned at scale**
   - Break-even: ~5M operations/month
   - Use auto-scaling with 70% target utilization
   - Consider reserved capacity for >20M ops/month

5. **Batch similar operations**
   - Combine multiple limits into single acquire()
   - Use bulk set_limits() for entity setup

#### 6.4 Cost Monitoring

Document CloudWatch metrics for cost tracking:
```
DynamoDB metrics:
- ConsumedReadCapacityUnits
- ConsumedWriteCapacityUnits
- AccountProvisionedReadCapacityUtilization
- AccountProvisionedWriteCapacityUtilization

Lambda metrics:
- Invocations
- Duration
- ConcurrentExecutions

Set up billing alerts:
- AWS Budgets for DynamoDB + Lambda
- CloudWatch alarms for unexpected spikes
```

---

## Implementation Tasks

### Phase 1: Core Documentation (2-3 hours)
- [ ] Create `docs/performance.md` with all 6 main sections
- [ ] Add practical code examples for each section
- [ ] Include tables with concrete numbers
- [ ] Add cost estimation formulas

### Phase 2: Integration (30 minutes)
- [ ] Update `mkdocs.yml` navigation
- [ ] Cross-link from related docs (deployment.md, cloudformation.md)
- [ ] Add "See Performance Tuning" callouts in relevant sections

### Phase 3: Validation (1 hour)
- [ ] Run benchmarks to verify latency claims
- [ ] Test all code examples
- [ ] Verify cost calculations against AWS pricing
- [ ] Review with mkdocs serve locally

### Phase 4: Review (30 minutes)
- [ ] Self-review for accuracy
- [ ] Check formatting and readability
- [ ] Ensure examples match current API
- [ ] Create PR with descriptive summary

---

## Dependencies

### Code References
- `src/zae_limiter/repository.py` - DynamoDB operations and costs
- `src/zae_limiter/bucket.py` - Token bucket math
- `src/zae_limiter/limiter.py` - Acquire algorithm
- `src/zae_limiter/lease.py` - Transaction patterns
- `src/zae_limiter/aggregator/processor.py` - Stream processing costs
- `src/zae_limiter/infra/cfn_template.yaml` - Default configurations
- `tests/benchmark/` - Performance metrics

### External References
- AWS DynamoDB Pricing: https://aws.amazon.com/dynamodb/pricing/
- AWS Lambda Pricing: https://aws.amazon.com/lambda/pricing/
- DynamoDB Best Practices: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/best-practices.html

---

## Success Criteria

1. **Completeness**: All 6 sections from issue description are covered
2. **Accuracy**: Numbers match actual code behavior and AWS pricing
3. **Actionable**: Users can apply recommendations immediately
4. **Examples**: Each section has working code snippets
5. **Testable**: Benchmark instructions allow users to verify claims

---

## Notes

- Issue specifies `docs/performance.md` as the file location
- Billing mode is PAY_PER_REQUEST (on-demand) by default
- Lambda aggregator is optional (can deploy with `--no-aggregator`)
- All examples should use current API (async primary, sync wrapper available)
