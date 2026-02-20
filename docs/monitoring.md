# Monitoring and Observability

This guide covers monitoring and observability practices for zae-limiter deployments, including structured logging, CloudWatch metrics, alerts, and dashboard templates.

## Overview

Effective monitoring of a rate limiter is critical for:

- **Availability** - Detecting service degradation before users are impacted
- **Latency** - Ensuring rate limit checks don't become a bottleneck
- **Throughput** - Understanding capacity and scaling needs
- **Errors** - Identifying and resolving issues quickly

zae-limiter provides built-in observability through:

| Component | Purpose |
|-----------|---------|
| CloudWatch Alarms | Proactive alerting on anomalies |
| Structured Logs | JSON-formatted logs for analysis |
| Dead Letter Queue | Capturing failed events for investigation |
| Usage Snapshots | Aggregated consumption metrics |
| Audit Logging | Security and compliance tracking |

!!! tip "Compliance Requirements"
    For tracking who changed what and when, see the [Audit Logging Guide](infra/auditing.md).

!!! tip "Usage Data"
    For querying historical consumption data (billing, capacity planning), see the [Usage Snapshots Guide](guide/usage-snapshots.md).

## Structured Logging

The Lambda aggregator uses structured JSON logging compatible with CloudWatch Logs Insights.

### Log Format

All log entries follow this JSON structure:

```json
{
  "timestamp": "2024-01-15T10:30:00.000000+00:00",
  "level": "INFO",
  "logger": "zae_limiter_aggregator.handler",
  "message": "Lambda invocation completed",
  "request_id": "abc123-def456",
  "processed": 50,
  "snapshots_updated": 100,
  "processing_time_ms": 45.23
}
```

### Log Fields Reference

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | string | ISO 8601 timestamp (UTC) |
| `level` | string | Log level: DEBUG, INFO, WARNING, ERROR |
| `logger` | string | Logger name (module path) |
| `message` | string | Human-readable message |
| `request_id` | string | Lambda request ID for correlation |
| `function_name` | string | Lambda function name |
| `record_count` | int | DynamoDB stream records in batch |
| `processed` | int | Records successfully processed |
| `deltas_extracted` | int | Consumption deltas found |
| `snapshots_updated` | int | Usage snapshots updated |
| `error_count` | int | Processing errors |
| `processing_time_ms` | float | Total execution time (ms) |

### Log Levels

| Level | When Used |
|-------|-----------|
| DEBUG | Detailed processing info (snapshot updates) |
| INFO | Invocation start/end, batch processing summary |
| WARNING | Recoverable errors (single record failures) |
| ERROR | Unrecoverable errors (batch failures) |

### Example Log Entries

**Invocation Start:**
```json
{
  "timestamp": "2024-01-15T10:30:00.000000+00:00",
  "level": "INFO",
  "logger": "zae_limiter_aggregator.handler",
  "message": "Lambda invocation started",
  "request_id": "abc123-def456",
  "function_name": "limiter-aggregator",
  "record_count": 50,
  "table_name": "limiter",
  "snapshot_windows": ["hourly", "daily"]
}
```

**Batch Complete:**
```json
{
  "timestamp": "2024-01-15T10:30:00.500000+00:00",
  "level": "INFO",
  "logger": "zae_limiter_aggregator.processor",
  "message": "Batch processing completed",
  "processed_count": 50,
  "deltas_extracted": 45,
  "snapshots_updated": 90,
  "error_count": 0,
  "processing_time_ms": 423.15
}
```

**Error with Exception:**
```json
{
  "timestamp": "2024-01-15T10:30:01.000000+00:00",
  "level": "ERROR",
  "logger": "zae_limiter_aggregator.processor",
  "message": "Error processing record",
  "record_index": 12,
  "exception": "Traceback (most recent call last):\n..."
}
```

## CloudWatch Metrics

### Lambda Metrics

Monitor the aggregator Lambda function:

| Metric | Namespace | Description | Recommended Threshold |
|--------|-----------|-------------|----------------------|
| `Invocations` | AWS/Lambda | Total executions | Baseline + 50% |
| `Errors` | AWS/Lambda | Failed executions | > 1 per 5 min |
| `Duration` | AWS/Lambda | Execution time (ms) | > 80% of timeout |
| `Throttles` | AWS/Lambda | Throttled invocations | > 0 |
| `IteratorAge` | AWS/Lambda | Stream processing lag (ms) | > 30,000 ms |
| `ConcurrentExecutions` | AWS/Lambda | Parallel executions | Account limit |

### DynamoDB Metrics

Monitor table performance:

| Metric | Namespace | Description | Recommended Threshold |
|--------|-----------|-------------|----------------------|
| `ConsumedReadCapacityUnits` | AWS/DynamoDB | RCU usage | Provisioned capacity |
| `ConsumedWriteCapacityUnits` | AWS/DynamoDB | WCU usage | Provisioned capacity |
| `ReadThrottleEvents` | AWS/DynamoDB | Read throttles | > 0 |
| `WriteThrottleEvents` | AWS/DynamoDB | Write throttles | > 0 |
| `SystemErrors` | AWS/DynamoDB | Service errors | > 0 |
| `SuccessfulRequestLatency` | AWS/DynamoDB | Request latency (ms) | p99 > 100ms |

### SQS Metrics (Dead Letter Queue)

Monitor failed event processing:

| Metric | Namespace | Description | Recommended Threshold |
|--------|-----------|-------------|----------------------|
| `ApproximateNumberOfMessagesVisible` | AWS/SQS | Messages in DLQ | > 0 |
| `ApproximateAgeOfOldestMessage` | AWS/SQS | Oldest message age (s) | > 3600 |

## CloudWatch Logs Insights Queries

### Batch Processing Performance

Analyze processing latency over time:

```sql
fields @timestamp, @message
| filter @message like /Batch processing completed/
| parse @message /processing_time_ms":(?<duration>[\d.]+)/
| stats avg(duration) as avg_ms,
        pct(duration, 50) as p50_ms,
        pct(duration, 95) as p95_ms,
        pct(duration, 99) as p99_ms
  by bin(1h)
| sort @timestamp desc
```

### Error Analysis

Find recent errors and warnings:

```sql
fields @timestamp, @message, @logStream
| filter level = "ERROR" or level = "WARNING"
| parse @message /message":"(?<error_message>[^"]+)/
| sort @timestamp desc
| limit 100
```

### Invocation Summary

Aggregate processing metrics:

```sql
fields @timestamp, @message
| filter @message like /Lambda invocation completed/
| parse @message /processed":(?<processed>\d+).*snapshots_updated":(?<snapshots>\d+)/
| stats sum(processed) as total_processed,
        sum(snapshots) as total_snapshots,
        count() as invocations
  by bin(1h)
| sort @timestamp desc
```

### Entity Usage Analysis

Find highest-usage entities:

```sql
fields @timestamp, @message
| filter @message like /Snapshot updated/
| parse @message /entity_id":"(?<entity>[^"]+)".*resource":"(?<resource>[^"]+)/
| stats count() as updates by entity, resource
| sort updates desc
| limit 50
```

### Cold Start Detection

Identify Lambda cold starts:

```sql
fields @timestamp, @message, @duration
| filter @type = "REPORT"
| filter @message like /Init Duration/
| parse @message /Init Duration: (?<init_duration>[\d.]+) ms/
| stats count() as cold_starts,
        avg(init_duration) as avg_init_ms
  by bin(1h)
```

### Error Rate Calculation

Calculate error rate percentage:

```sql
fields @timestamp
| filter @message like /Lambda invocation/
| parse @message /error_count":(?<errors>\d+)/
| stats sum(errors) as total_errors, count() as total_invocations
| display total_errors, total_invocations,
         (total_errors * 100.0 / total_invocations) as error_rate_pct
```

## X-Ray Tracing

AWS X-Ray tracing is available for the Lambda aggregator function. When enabled, traces are automatically captured for Lambda invocations, providing visibility into stream processing performance.

### Enabling X-Ray

X-Ray tracing is opt-in to avoid unexpected costs:

```bash
# Enable tracing via CLI
zae-limiter deploy --name my-app --enable-tracing
```

Or programmatically:

```python
from zae_limiter import RateLimiter, Repository

repo = await (
    Repository.builder()
    .stack("my-app").region("us-east-1")
    .enable_tracing(True)
    .build()
)
limiter = RateLimiter(repository=repo)
```

### What's Traced (Phase 1)

- **Lambda Active Tracing** - End-to-end request visibility for aggregator invocations
- **Automatic segments** - AWS SDK calls (DynamoDB, S3) are automatically instrumented

### Future Enhancements

Track progress on additional X-Ray features in [Issue #107](https://github.com/zeroae/zae-limiter/issues/107):

- DynamoDB SDK instrumentation for client-side operations
- Custom subsegments for acquire/release operations
- Trace header propagation for cross-service correlation

## Dashboard Templates

### Operations Dashboard

Create a CloudWatch dashboard for day-to-day operations:

```json
{
  "widgets": [
    {
      "type": "metric",
      "properties": {
        "title": "Lambda Invocations & Errors",
        "region": "${AWS::Region}",
        "metrics": [
          ["AWS/Lambda", "Invocations", "FunctionName", "${TableName}-aggregator", {"stat": "Sum"}],
          [".", "Errors", ".", ".", {"stat": "Sum", "color": "#d62728"}]
        ],
        "period": 300,
        "view": "timeSeries"
      }
    },
    {
      "type": "metric",
      "properties": {
        "title": "Lambda Duration (p50/p95/p99)",
        "region": "${AWS::Region}",
        "metrics": [
          ["AWS/Lambda", "Duration", "FunctionName", "${TableName}-aggregator", {"stat": "p50"}],
          ["...", {"stat": "p95"}],
          ["...", {"stat": "p99"}]
        ],
        "period": 300,
        "view": "timeSeries"
      }
    },
    {
      "type": "metric",
      "properties": {
        "title": "Stream Iterator Age",
        "region": "${AWS::Region}",
        "metrics": [
          ["AWS/Lambda", "IteratorAge", "FunctionName", "${TableName}-aggregator", {"stat": "Maximum"}]
        ],
        "period": 60,
        "view": "timeSeries",
        "annotations": {
          "horizontal": [{"value": 30000, "label": "Threshold (30s)"}]
        }
      }
    },
    {
      "type": "metric",
      "properties": {
        "title": "DynamoDB Capacity",
        "region": "${AWS::Region}",
        "metrics": [
          ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", "${TableName}", {"stat": "Sum"}],
          [".", "ConsumedWriteCapacityUnits", ".", ".", {"stat": "Sum"}]
        ],
        "period": 300,
        "view": "timeSeries"
      }
    },
    {
      "type": "metric",
      "properties": {
        "title": "DynamoDB Throttles",
        "region": "${AWS::Region}",
        "metrics": [
          ["AWS/DynamoDB", "ReadThrottleEvents", "TableName", "${TableName}", {"stat": "Sum"}],
          [".", "WriteThrottleEvents", ".", ".", {"stat": "Sum"}]
        ],
        "period": 300,
        "view": "timeSeries"
      }
    },
    {
      "type": "metric",
      "properties": {
        "title": "Dead Letter Queue",
        "region": "${AWS::Region}",
        "metrics": [
          ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "${TableName}-aggregator-dlq"]
        ],
        "period": 60,
        "view": "singleValue"
      }
    }
  ]
}
```

### Capacity Planning Dashboard

Create a dashboard for capacity analysis:

```json
{
  "widgets": [
    {
      "type": "metric",
      "properties": {
        "title": "RCU/WCU Consumption Trend",
        "region": "${AWS::Region}",
        "metrics": [
          ["AWS/DynamoDB", "ConsumedReadCapacityUnits", "TableName", "${TableName}", {"stat": "Sum", "period": 3600}],
          [".", "ConsumedWriteCapacityUnits", ".", ".", {"stat": "Sum", "period": 3600}]
        ],
        "view": "timeSeries",
        "stacked": false
      }
    },
    {
      "type": "metric",
      "properties": {
        "title": "Request Latency Distribution",
        "region": "${AWS::Region}",
        "metrics": [
          ["AWS/DynamoDB", "SuccessfulRequestLatency", "TableName", "${TableName}", "Operation", "GetItem", {"stat": "p50"}],
          ["...", {"stat": "p95"}],
          ["...", {"stat": "p99"}]
        ],
        "period": 300,
        "view": "timeSeries"
      }
    },
    {
      "type": "metric",
      "properties": {
        "title": "Lambda Concurrent Executions",
        "region": "${AWS::Region}",
        "metrics": [
          ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", "${TableName}-aggregator", {"stat": "Maximum"}]
        ],
        "period": 60,
        "view": "timeSeries"
      }
    },
    {
      "type": "metric",
      "properties": {
        "title": "Throttle Events (7 Day)",
        "region": "${AWS::Region}",
        "metrics": [
          ["AWS/DynamoDB", "ReadThrottleEvents", "TableName", "${TableName}", {"stat": "Sum", "period": 86400}],
          [".", "WriteThrottleEvents", ".", ".", {"stat": "Sum", "period": 86400}]
        ],
        "view": "bar"
      }
    }
  ]
}
```

!!! tip "Dashboard Deployment"
    Replace `${TableName}` with your actual table name (e.g., `limiter`) and `${AWS::Region}` with your region before deploying.

## Alert Configuration

### Default Alarms

The stack deploys these alarms when `--enable-alarms` is set:

| Alarm | Metric | Threshold | Period | Evaluation |
|-------|--------|-----------|--------|------------|
| `{name}-aggregator-error-rate` | Lambda Errors | > 1 | 5 min | 2 periods |
| `{name}-aggregator-duration` | Lambda Duration | > 80% timeout | 5 min | 2 periods |
| `{name}-stream-iterator-age` | IteratorAge | > 30,000 ms | 5 min | 2 periods |
| `{name}-aggregator-dlq-alarm` | SQS Messages | >= 1 | 5 min | 1 period |
| `{name}-read-throttle` | ReadThrottleEvents | > 1 | 5 min | 2 periods |
| `{name}-write-throttle` | WriteThrottleEvents | > 1 | 5 min | 2 periods |

### Deploying with Alarms

```bash
# Deploy with alarms enabled (default)
zae-limiter deploy --name limiter --region us-east-1

# Deploy with SNS notifications
zae-limiter deploy --name limiter --region us-east-1 \
    --alarm-sns-topic arn:aws:sns:us-east-1:123456789012:alerts

# Customize duration threshold (70% of timeout)
zae-limiter deploy --name limiter --region us-east-1 \
    --lambda-duration-threshold-pct 70

# Disable alarms (not recommended for production)
zae-limiter deploy --name limiter --region us-east-1 --no-alarms
```

### Threshold Tuning Guide

| Alarm | Default | When to Increase | When to Decrease |
|-------|---------|------------------|------------------|
| **Error Rate** | >1/5min | High-volume systems with rare transient errors | Critical systems requiring immediate response |
| **Duration** | 80% timeout | Batch workloads with variable processing time | Latency-sensitive applications |
| **Iterator Age** | 30 seconds | Batch-tolerant analytics workloads | Real-time processing requirements |
| **DLQ Messages** | >=1 | Never (always investigate DLQ messages) | N/A |
| **Throttles** | >1/5min | During planned traffic spikes | Before hitting capacity limits |

### Programmatic Configuration

```python
from zae_limiter import RateLimiter, Repository

repo = await (
    Repository.builder()
    .stack("limiter").region("us-east-1")
    .enable_alarms(True)
    .alarm_sns_topic("arn:aws:sns:us-east-1:123456789012:alerts")
    .lambda_duration_threshold_pct(75)  # Alert at 75% of timeout
    .log_retention_days(90)
    .build()
)
limiter = RateLimiter(repository=repo)
```

## Next Steps

- [Operations Guide](operations/index.md) - Troubleshooting and operational procedures
- [Audit Logging](infra/auditing.md) - Security and compliance tracking
- [Performance Tuning](performance.md) - Capacity planning and optimization
- [Deployment Guide](infra/deployment.md) - Infrastructure setup
- [CloudFormation Reference](infra/cloudformation.md) - Template customization
