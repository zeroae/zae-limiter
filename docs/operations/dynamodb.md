# DynamoDB Operations

This guide covers troubleshooting and operational procedures for DynamoDB capacity management and throttling issues.

## Decision Tree

```mermaid
flowchart TD
    START([DynamoDB Issue]) --> Q1{What's the symptom?}

    Q1 -->|ReadThrottleEvents| A1[Check read patterns]
    Q1 -->|WriteThrottleEvents| A2[Check write patterns]
    Q1 -->|High latency| A3[Check capacity utilization]
    Q1 -->|Planning scale| A4[Capacity planning]

    A1 --> FIX1{Emergency?}
    A2 --> FIX1
    FIX1 -->|Yes| EMERGENCY[Switch to on-demand]
    FIX1 -->|No| PLAN[Increase provisioned capacity]

    A3 --> CHECK[Check CloudWatch metrics]
    CHECK --> FIX1

    A4 --> LINK([→ Performance Guide])

    click A1 "#throttling" "Diagnose throttling"
    click A2 "#throttling" "Diagnose throttling"
    click A3 "#capacity-planning" "Check capacity"
    click EMERGENCY "#emergency-on-demand" "Switch to on-demand"
    click PLAN "#scaling-procedures" "Scale capacity"
    click CHECK "#diagnostic-queries" "View metrics"
    click LINK "../performance/" "Capacity planning guide"
```

## Troubleshooting

### Symptoms

- `ProvisionedThroughputExceededException` errors
- Increased latency on rate limit checks
- CloudWatch throttle alarms triggered
- `RateLimiterUnavailable` with `OnUnavailable.BLOCK` mode

### Diagnostic Steps

**Check CloudWatch metrics:**

```bash
# View throttle events (last hour)
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ReadThrottleEvents \
  --dimensions Name=TableName,Value=<name> \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 \
  --statistics Sum
```

**Check capacity utilization:**

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ConsumedReadCapacityUnits \
  --dimensions Name=TableName,Value=<name> \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 \
  --statistics Sum
```

**Identify hot partitions with Contributor Insights:**

See [Per-Partition Monitoring](#per-partition-monitoring) below for detailed setup and troubleshooting.

### Throttling

#### Common Causes and Solutions

| Cause | Solution |
|-------|----------|
| **Provisioned capacity too low** | Increase RCU/WCU or switch to on-demand |
| **Hot partition** | Distribute entity IDs more evenly |
| **Burst traffic** | Enable auto-scaling or use on-demand |
| **GSI throttling** | Check GSI capacity separately |

#### Read Throttling

**Check which operations are throttling:**

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ThrottledRequests \
  --dimensions Name=TableName,Value=<name> Name=Operation,Value=GetItem \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 \
  --statistics Sum
```

#### Write Throttling

Write throttling typically occurs during high-volume rate limiting or when the aggregator Lambda is processing many stream events.

**Check write patterns:**

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ConsumedWriteCapacityUnits \
  --dimensions Name=TableName,Value=<name> \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 \
  --statistics Sum
```

### Capacity Planning

For detailed capacity calculations, see the [Performance Tuning Guide](../performance.md#1-dynamodb-capacity-planning).

**Quick estimates:**

| Operation | RCU | WCU |
|-----------|-----|-----|
| `acquire()` | ~2 | ~4 |
| `acquire()` with cascade | ~4 | ~8 |
| `available()` | ~2 | ~0 |
| Aggregator (per record) | ~1 | ~2 |

## Procedures

### Emergency Capacity Increase

**Switch to on-demand capacity (immediate relief):**

!!! note "Billing Impact"
    On-demand pricing is typically 5-7x more expensive than provisioned capacity at steady state, but has no throttling.

```bash
aws dynamodb update-table \
  --table-name <name> \
  --billing-mode PAY_PER_REQUEST
```

**Increase provisioned capacity:**

```bash
aws dynamodb update-table \
  --table-name <name> \
  --provisioned-throughput ReadCapacityUnits=1000,WriteCapacityUnits=500
```

### Scaling Procedures

#### Planned Capacity Scaling

**Step 1: Analyze current usage**

```bash
# Get average consumption over last 24 hours
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ConsumedReadCapacityUnits \
  --dimensions Name=TableName,Value=<name> \
  --start-time $(date -u -d '24 hours ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 3600 \
  --statistics Average
```

**Step 2: Calculate required capacity**

Use the formulas from [Performance Tuning](../performance.md):

- `RCU = (requests_per_second × 2) + (cascade_requests × 2)`
- `WCU = (requests_per_second × 4) + (cascade_requests × 4)`

Add 20% headroom for bursts.

**Step 3: Apply changes**

```bash
aws dynamodb update-table \
  --table-name <name> \
  --provisioned-throughput ReadCapacityUnits=<new_rcu>,WriteCapacityUnits=<new_wcu>
```

**Step 4: Verify**

```bash
aws dynamodb describe-table --table-name <name> \
  --query 'Table.ProvisionedThroughput'
```

#### Enable Auto-Scaling

**Create scaling targets:**

```bash
# Register read capacity target
aws application-autoscaling register-scalable-target \
  --service-namespace dynamodb \
  --resource-id "table/<name>" \
  --scalable-dimension "dynamodb:table:ReadCapacityUnits" \
  --min-capacity 5 \
  --max-capacity 1000

# Register write capacity target
aws application-autoscaling register-scalable-target \
  --service-namespace dynamodb \
  --resource-id "table/<name>" \
  --scalable-dimension "dynamodb:table:WriteCapacityUnits" \
  --min-capacity 5 \
  --max-capacity 500
```

**Create scaling policies:**

```bash
# Read capacity policy (target 70% utilization)
aws application-autoscaling put-scaling-policy \
  --service-namespace dynamodb \
  --resource-id "table/<name>" \
  --scalable-dimension "dynamodb:table:ReadCapacityUnits" \
  --policy-name "<name>-read-scaling" \
  --policy-type "TargetTrackingScaling" \
  --target-tracking-scaling-policy-configuration '{
    "TargetValue": 70.0,
    "PredefinedMetricSpecification": {
      "PredefinedMetricType": "DynamoDBReadCapacityUtilization"
    }
  }'
```

#### Switch to On-Demand

**When to use on-demand:**

- Unpredictable traffic patterns
- New deployments without baseline data
- Cost is less important than avoiding throttling

**Switch from provisioned to on-demand:**

```bash
aws dynamodb update-table \
  --table-name <name> \
  --billing-mode PAY_PER_REQUEST
```

**Switch back to provisioned:**

!!! warning "Cooldown Period"
    You can only switch billing modes once per 24 hours.

```bash
aws dynamodb update-table \
  --table-name <name> \
  --billing-mode PROVISIONED \
  --provisioned-throughput ReadCapacityUnits=100,WriteCapacityUnits=50
```

### Verification

After capacity changes, monitor for 15-30 minutes:

```bash
# Watch throttle events
watch -n 30 "aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ReadThrottleEvents \
  --dimensions Name=TableName,Value=<name> \
  --start-time \$(date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time \$(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 \
  --statistics Sum"
```

## Per-Partition Monitoring

### Understanding Hot Partitions

DynamoDB distributes data across multiple internal partitions. Each partition has its own throughput quota:

- **Per-partition read limit**: ~3,000 RCU
- **Per-partition write limit**: ~1,000 WCU

If a single partition receives most traffic (e.g., a high-fanout parent entity with 1,000+ children and `cascade=True`), that partition throttles while others are idle. This is called a **hot partition**.

Typical symptoms:
- High latency on specific operations
- CloudWatch shows `ReadThrottleEvents` or `WriteThrottleEvents`
- Some entity IDs throttle but others don't
- Table-level capacity looks fine but requests still throttle

### Contributor Insights Setup

**DynamoDB Contributor Insights** identifies which partition keys consume the most throughput, helping you spot hot partitions.

#### Enable Contributor Insights

```bash
# Enable for main table
aws dynamodb update-contributor-insights \
  --table-name <name> \
  --contributor-insights-action ENABLE

# Enable for GSI (if needed)
aws dynamodb update-contributor-insights \
  --table-name <name> \
  --contributor-insights-action ENABLE \
  --index-name GSI1
```

**Cost:** ~$0.10 per table per day (minimal). Monitoring is available after ~30 minutes of data collection.

#### View Contributor Insights Data

```bash
# Check if enabled
aws dynamodb describe-contributor-insights \
  --table-name <name>

# Get top partition keys by throughput
aws cloudwatch get-metric-statistics \
  --namespace AWS/DynamoDB \
  --metric-name ConributorValue \
  --dimensions Name=TableName,Value=<name> Name=Contributor,Value=PK \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 300 \
  --statistics Sum \
  --max-records 10
```

Or use the AWS Console:

1. Go to DynamoDB → Tables → `<name>` → Contributor Insights
2. Review "Top Keys" to identify which partition keys consume most throughput
3. Cross-reference with your entity IDs to find the hot entity

#### Interpreting Results

**If you see:**

| Pattern | Meaning | Action |
|---------|---------|--------|
| One entity ID consuming 70%+ of throughput | Hot parent with cascade | Implement write sharding |
| Multiple entity IDs evenly distributed | Normal load distribution | No action needed |
| Spike on specific entity at specific time | Burst traffic on one user | Monitor or rate limit |
| GSI1 showing skewed distribution | Parent lookup imbalance | Review parent structure |

### Identifying Cascade Hot Partitions

When using `cascade=True` with high-fanout parents, check for these patterns:

```bash
# Query: Which parent entities have the most children?
# This is NOT a built-in query, but you can estimate from operation patterns:

# 1. Enable Contributor Insights (as above)
# 2. In CloudWatch, look at top PK values during peak traffic
# 3. If top 3-5 PK values account for >50% of writes, those are likely high-fanout parents
```

**Example diagnosis:**

```
Top 5 Partition Keys (by write throughput):
1. ENTITY#project-123  → 45% of writes (HIGH FANOUT - HOTSPOT!)
2. ENTITY#project-456  → 12% of writes
3. ENTITY#project-789  → 10% of writes
4. ENTITY#api-key-999  → 8% of writes
5. ENTITY#...          → remainder

Analysis:
- project-123 likely has 1000+ API keys with cascade=True
- This parent partition can handle ~1,000 WCU max
- If concurrent writes exceed this, throttling occurs
```

### Mitigations

Once you've identified a hot partition:

#### 1. Write Sharding (Recommended for Cascade)

Split high-fanout parent into sharded parents. See [Write Sharding Guide](../performance.md#write-sharding-for-high-fanout-parents) for detailed example.

```python
# Instead of:
#   API Key → project-123 (1000+ children, hotspot)
#
# Create:
#   API Key → project-123-shard-0 through project-123-shard-N
#   (Distribute API keys across N shards)

# This spreads writes across N partitions, multiplying capacity
```

**Effectiveness:** 10x shards = ~10x capacity improvement (linear)

#### 2. Reduce Cascade Usage

If hot partition is detected and write sharding isn't feasible:

```python
# Create entities without cascade
await limiter.create_entity(entity_id="api-key", parent_id="project-1", cascade=False)

# Then manually check parent limits if needed
available = await limiter.available("project-1", "llm-api", [Limit.per_minute("rpm", 1000)])
```

**Trade-off:** Parent limits no longer automatically enforced; you must check them explicitly.

#### 3. Switch to On-Demand Billing

```bash
aws dynamodb update-table \
  --table-name <name> \
  --billing-mode PAY_PER_REQUEST
```

**Cost:** 5-7x more expensive, but no throttling and no partition limits.

#### 4. Distributed Load Strategy

For enterprise deployments with many customers:

- Create one parent per customer tenant instead of one global parent
- Distribute high-fanout parents across multiple tables (region sharding)
- Use AWS Global Tables for multi-region replication

### Monitoring Checklist

Add these CloudWatch alarms to catch hot partitions early:

```bash
# Alert on write throttling
aws cloudwatch put-metric-alarm \
  --alarm-name "<name>-write-throttle" \
  --alarm-description "Alert when write throttling occurs" \
  --metric-name WriteThrottleEvents \
  --namespace AWS/DynamoDB \
  --dimensions Name=TableName,Value=<name> \
  --statistic Sum \
  --period 300 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:alerts

# Alert on read throttling
aws cloudwatch put-metric-alarm \
  --alarm-name "<name>-read-throttle" \
  --alarm-description "Alert when read throttling occurs" \
  --metric-name ReadThrottleEvents \
  --namespace AWS/DynamoDB \
  --dimensions Name=TableName,Value=<name> \
  --statistic Sum \
  --period 300 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:alerts

# Alert on high consumed capacity (warning before throttle)
aws cloudwatch put-metric-alarm \
  --alarm-name "<name>-high-capacity" \
  --alarm-description "Warning: approaching provisioned capacity" \
  --metric-name ConsumedWriteCapacityUnits \
  --namespace AWS/DynamoDB \
  --dimensions Name=TableName,Value=<name> \
  --statistic Average \
  --period 300 \
  --threshold 800 \
  --comparison-operator GreaterThanThreshold \
  --alarm-actions arn:aws:sns:us-east-1:123456789012:warnings
```

## Related

- [Performance Tuning](../performance.md) - Capacity planning formulas and optimization
- [Lambda Operations](lambda.md) - Aggregator throttling due to DynamoDB
- [Stream Processing](streams.md) - Stream processing affected by DynamoDB capacity
