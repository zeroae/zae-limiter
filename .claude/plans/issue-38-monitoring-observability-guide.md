# Plan: Monitoring and Observability Guide (Issue #38)

**Issue:** https://github.com/zeroae/zae-limiter/issues/38
**Status:** Ready for Implementation
**Dependencies:**
- #45 (performance benchmarks) - CLOSED
- #46 (E2E tests) - CLOSED

## Overview

Create comprehensive documentation at `docs/monitoring.md` covering monitoring and observability practices for zae-limiter deployments.

## Existing Monitoring Capabilities

The codebase already has robust monitoring infrastructure:

### CloudWatch Alarms (6 total)
| Alarm | Metric | Threshold | Purpose |
|-------|--------|-----------|---------|
| `{name}-aggregator-error-rate` | Lambda Errors | >1 in 5 min | Detect Lambda failures |
| `{name}-aggregator-duration` | Lambda Duration | >80% of timeout | Warn before timeout |
| `{name}-stream-iterator-age` | IteratorAge | >30s | Detect processing lag |
| `{name}-aggregator-dlq-alarm` | SQS Messages | ≥1 | Alert on failed events |
| `{name}-read-throttle` | ReadThrottleEvents | >1 | DynamoDB read throttling |
| `{name}-write-throttle` | WriteThrottleEvents | >1 | DynamoDB write throttling |

### Structured Logging
- `StructuredLogger` class in `aggregator/processor.py:15-46`
- JSON format compatible with CloudWatch Logs Insights
- Fields: timestamp, level, logger, message, plus contextual data
- Logs per invocation: request_id, function_name, record_count, processing_time_ms

### Dead Letter Queue (DLQ)
- 14-day message retention
- Captures failed Lambda batch events
- Automatic retry (3 attempts) before DLQ

## Document Structure

### 1. Overview (Introduction)
- Purpose of monitoring for rate limiters
- Key metrics categories: availability, latency, throughput, errors
- Architecture diagram reference

### 2. Structured Logging Patterns
- Document the JSON log format from `StructuredLogger`
- Log fields reference table
- Example log entries for common operations
- Log levels and when each is used

### 3. CloudWatch Metrics to Track

#### Lambda Metrics
| Metric | Namespace | Description |
|--------|-----------|-------------|
| Invocations | AWS/Lambda | Total Lambda executions |
| Duration | AWS/Lambda | Execution time (ms) |
| Errors | AWS/Lambda | Failed executions |
| Throttles | AWS/Lambda | Throttled invocations |
| IteratorAge | AWS/Lambda | Stream processing lag |
| ConcurrentExecutions | AWS/Lambda | Parallel executions |

#### DynamoDB Metrics
| Metric | Namespace | Description |
|--------|-----------|-------------|
| ConsumedReadCapacityUnits | AWS/DynamoDB | RCU usage |
| ConsumedWriteCapacityUnits | AWS/DynamoDB | WCU usage |
| ReadThrottleEvents | AWS/DynamoDB | Read throttles |
| WriteThrottleEvents | AWS/DynamoDB | Write throttles |
| SystemErrors | AWS/DynamoDB | Service errors |
| SuccessfulRequestLatency | AWS/DynamoDB | Request latency |

#### SQS Metrics (DLQ)
| Metric | Namespace | Description |
|--------|-----------|-------------|
| ApproximateNumberOfMessagesVisible | AWS/SQS | DLQ depth |
| ApproximateAgeOfOldestMessage | AWS/SQS | Message age |

### 4. CloudWatch Logs Insights Queries

Provide ready-to-use queries for common analysis:

```sql
-- Batch processing performance
fields @timestamp, @message
| filter @message like /Batch processing completed/
| parse @message /processing_time_ms=(?<duration>\d+\.?\d*)/
| stats avg(duration), p50(duration), p95(duration), p99(duration) by bin(1h)

-- Error analysis
fields @timestamp, @message, @logStream
| filter level = "ERROR" or level = "WARNING"
| sort @timestamp desc
| limit 100

-- Invocation summary
fields @timestamp, @message
| filter @message like /Lambda invocation completed/
| parse @message /processed=(?<processed>\d+), snapshots_updated=(?<snapshots>\d+)/
| stats sum(processed) as total_processed, sum(snapshots) as total_snapshots by bin(1h)

-- Entity usage analysis
fields @timestamp, @message
| filter @message like /Snapshot updated/
| parse @message /entity_id=(?<entity>[^,]+), resource=(?<resource>[^,]+)/
| stats count() by entity, resource
| sort count desc
| limit 50
```

### 5. X-Ray Tracing Integration (Future Work)
- Mark as future enhancement
- Describe potential integration points:
  - Lambda function tracing
  - DynamoDB SDK instrumentation
  - Custom subsegments for business logic
- Link to AWS X-Ray documentation

### 6. Dashboard Templates

Provide CloudFormation-compatible dashboard JSON:

#### Operations Dashboard
- Lambda invocations and errors (timeseries)
- Lambda duration percentiles
- DynamoDB capacity utilization
- Stream iterator age
- DLQ message count

#### Capacity Planning Dashboard
- RCU/WCU consumption trends
- Request latency distribution
- Throttle event counts
- Entity count growth

### 7. Alert Thresholds

Document default thresholds and tuning guidance:

| Alarm | Default | Tuning Guidance |
|-------|---------|-----------------|
| Lambda Error Rate | >1 error/5min | Increase for high-volume, decrease for critical |
| Lambda Duration | 80% of timeout | Lower if latency-sensitive |
| Iterator Age | >30 seconds | Increase for batch-tolerant workloads |
| DLQ Messages | ≥1 | Keep at 1 for immediate awareness |
| Read Throttle | >1/5min | Consider provisioned mode if frequent |
| Write Throttle | >1/5min | Consider provisioned mode if frequent |

Include CLI examples:
```bash
# Deploy with custom duration threshold (70%)
zae-limiter deploy --name limiter --lambda-duration-threshold-pct 70

# Deploy with SNS notifications
zae-limiter deploy --name limiter --alarm-sns-topic arn:aws:sns:...
```

### 8. Troubleshooting Guide

Common issues and diagnostic steps:
- High Lambda duration → Check memory, batch size, DynamoDB latency
- Iterator age increasing → Scale Lambda concurrency, check errors
- DLQ messages → Analyze failed records, check table schema
- Throttling → Switch to provisioned capacity, check access patterns

## Implementation Steps

1. [ ] Create `docs/monitoring.md` with document structure
2. [ ] Write Overview section with architecture context
3. [ ] Document Structured Logging patterns with examples from code
4. [ ] Create CloudWatch Metrics reference tables
5. [ ] Write CloudWatch Logs Insights queries (test against real logs)
6. [ ] Add X-Ray section marked as future work
7. [ ] Create Dashboard templates (JSON for CloudFormation)
8. [ ] Document Alert thresholds with tuning guidance
9. [ ] Write Troubleshooting guide
10. [ ] Add cross-references to performance.md and deployment.md
11. [ ] Update docs/index.md with link to new guide

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `docs/monitoring.md` | Create | Main monitoring guide |
| `docs/index.md` | Update | Add navigation link |
| `docs/infra/deployment.md` | Update | Cross-reference to monitoring guide |

## Testing the Documentation

- Verify all CLI commands work
- Test Logs Insights queries in CloudWatch console
- Validate dashboard JSON syntax
- Ensure links resolve correctly

## References

- CloudFormation template: `src/zae_limiter/infra/cfn_template.yaml:216-477`
- Structured logger: `src/zae_limiter/aggregator/processor.py:15-48`
- Lambda handler logging: `src/zae_limiter/aggregator/handler.py:39-78`
- CLI options: `src/zae_limiter/cli.py` (--enable-alarms, --alarm-sns-topic, etc.)
- Performance guide: `docs/performance.md:448-477` (cost monitoring section)
