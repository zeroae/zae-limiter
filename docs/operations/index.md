# Operations Guide

This guide consolidates troubleshooting and operational procedures for zae-limiter deployments. Navigate using the interactive map below or jump directly to a topic.

## Navigation

```markmap
# Operations Guide

## Alerts & Issues
### Lambda Aggregator
- [Error rate alarm](lambda.md#error-rate-issues)
- [Duration/timeout](lambda.md#high-lambda-duration)
- [DLQ messages](lambda.md#messages-in-dead-letter-queue)
### DynamoDB
- [Read/write throttling](dynamodb.md#throttling)
- [Capacity alarms](dynamodb.md#capacity-planning)
### Streams
- [Iterator age alarm](streams.md#high-iterator-age)
- [Processing lag](streams.md#common-causes-and-solutions)
### Version
- [VersionMismatchError](version.md#versionmismatcherror)
- [IncompatibleSchemaError](version.md#incompatibleschemaerror)
### Rate Limits
- [Unexpected RateLimitExceeded](rate-limits.md#unexpected-ratelimitexceeded)
- [Limits not enforcing](rate-limits.md#limits-not-enforcing)

## Planned Operations
### Upgrades
- [Version upgrade procedure](version.md#upgrade-procedure)
- [Lambda code update](lambda.md#lambda-redeployment)
### Scaling
- [Adjust rate limits](rate-limits.md#adjust-limits-at-runtime)
- [DynamoDB capacity](dynamodb.md#scaling-procedures)
### Recovery
- [Emergency rollback](recovery.md#emergency-rollback-decision-matrix)
- [Backup/restore](recovery.md#dynamodb-backup-and-restore)
- [PITR recovery](recovery.md#point-in-time-recovery-pitr)
```

## Quick Reference

| Symptom | Go To |
|---------|-------|
| `RateLimitExceeded` unexpected | [Rate Limits](rate-limits.md#unexpected-ratelimitexceeded) |
| `ProvisionedThroughputExceededException` | [DynamoDB](dynamodb.md#throttling) |
| DLQ messages accumulating | [Lambda](lambda.md#messages-in-dead-letter-queue) |
| `VersionMismatchError` | [Version](version.md#versionmismatcherror) |
| High `IteratorAge` | [Streams](streams.md#high-iterator-age) |
| Need to rollback | [Recovery](recovery.md#emergency-rollback-decision-matrix) |

## CLI Diagnostic Commands

| Command | Description |
|---------|-------------|
| `zae-limiter status --name <name>` | Check stack status and resources |
| `zae-limiter version --name <name>` | Show version information |
| `zae-limiter check --name <name>` | Check client/infrastructure compatibility |

## CloudWatch Metrics Overview

| Metric | Namespace | Threshold | Guide |
|--------|-----------|-----------|-------|
| `Errors` | AWS/Lambda | > 1/5min | [Lambda](lambda.md) |
| `Duration` | AWS/Lambda | > 80% timeout | [Lambda](lambda.md) |
| `IteratorAge` | AWS/Lambda | > 30,000ms | [Streams](streams.md) |
| `ReadThrottleEvents` | AWS/DynamoDB | > 0 | [DynamoDB](dynamodb.md) |
| `WriteThrottleEvents` | AWS/DynamoDB | > 0 | [DynamoDB](dynamodb.md) |
| `ApproximateNumberOfMessagesVisible` | AWS/SQS | > 0 | [Lambda](lambda.md) |

## Exception Reference

| Exception | Cause | Guide |
|-----------|-------|-------|
| `RateLimitExceeded` | Rate limit violated | [Rate Limits](rate-limits.md) |
| `RateLimiterUnavailable` | DynamoDB unavailable | [DynamoDB](dynamodb.md) |
| `EntityNotFoundError` | Entity doesn't exist | [Rate Limits](rate-limits.md) |
| `VersionMismatchError` | Client/Lambda version mismatch | [Version](version.md) |
| `IncompatibleSchemaError` | Major version difference | [Version](version.md) |
| `StackCreationError` | CloudFormation failed | [Recovery](recovery.md) |

## Related Documentation

- [Monitoring Guide](../monitoring.md) - CloudWatch metrics, dashboards, and alerts
- [Performance Tuning](../performance.md) - Capacity planning and optimization
- [Migration Guide](../migrations.md) - Schema versioning and upgrades
- [Failure Modes](../guide/failure-modes.md) - Configure behavior when DynamoDB is unavailable
