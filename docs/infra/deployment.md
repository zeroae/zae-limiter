# Deployment

This guide covers deploying zae-limiter infrastructure to AWS.

## Overview

zae-limiter uses CloudFormation to deploy:

- **DynamoDB Table** - Stores rate limit state, entities, and usage data
- **DynamoDB Streams** - Captures changes for usage aggregation
- **Lambda Function** - Aggregates usage into hourly/daily snapshots
- **IAM Roles** - Least-privilege access for Lambda
- **CloudWatch Logs** - Lambda function logs

## CLI Deployment (Recommended)

The simplest way to deploy:

```bash
# Deploy with defaults
zae-limiter deploy --table-name rate_limits --region us-east-1

# Deploy with custom settings
zae-limiter deploy \
    --table-name rate_limits \
    --region us-east-1 \
    --log-retention-days 90 \
    --pitr-recovery-days 7
```

### CLI Options

| Option | Description | Default |
|--------|-------------|---------|
| `--table-name` | DynamoDB table name | Required |
| `--region` | AWS region | Required |
| `--stack-name` | CloudFormation stack name | `zae-limiter-{table}` |
| `--no-aggregator` | Skip Lambda deployment | `false` |
| `--log-retention-days` | CloudWatch log retention | `14` |
| `--pitr-recovery-days` | Point-in-time recovery | `0` (disabled) |
| `--endpoint-url` | Custom endpoint (LocalStack) | None |

### Check Stack Status

```bash
zae-limiter status --stack-name zae-limiter-rate_limits --region us-east-1
```

### Delete Stack

```bash
zae-limiter delete --stack-name zae-limiter-rate_limits --region us-east-1 --yes
```

## CloudFormation Template

Export and customize the template:

```bash
# Export template
zae-limiter cfn-template > template.yaml

# Deploy with AWS CLI
aws cloudformation deploy \
    --template-file template.yaml \
    --stack-name zae-limiter \
    --parameter-overrides \
        TableName=rate_limits \
        SnapshotRetentionDays=90 \
        EnablePITR=true \
    --capabilities CAPABILITY_NAMED_IAM

# Deploy Lambda code separately
zae-limiter lambda-export --output lambda.zip
aws lambda update-function-code \
    --function-name zae-limiter-aggregator \
    --zip-file fileb://lambda.zip
```

### Template Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `TableName` | DynamoDB table name | `rate_limits` |
| `SnapshotWindows` | Aggregation windows | `hourly,daily` |
| `SnapshotRetentionDays` | Usage data retention | `90` |
| `EnablePITR` | Point-in-time recovery | `false` |
| `LogRetentionDays` | CloudWatch log retention | `14` |

## Auto-Creation in Code

For development, create infrastructure programmatically:

```python
from zae_limiter import RateLimiter

limiter = RateLimiter(
    table_name="rate_limits",
    region="us-east-1",
    create_stack=True,
    stack_parameters={
        "snapshot_windows": "hourly,daily",
        "retention_days": "90",
    },
)
```

!!! warning "Production Use"
    Auto-creation is convenient for development but has limitations:

    - No control over IAM policies
    - Limited error handling
    - Not idempotent

    Use CLI or CloudFormation for production.

## Infrastructure Details

### DynamoDB Table

- **Billing**: Pay-per-request (on-demand)
- **Encryption**: AWS-managed keys (default)
- **Streams**: NEW_AND_OLD_IMAGES for Lambda trigger

### DynamoDB Schema

| Key | Pattern | Purpose |
|-----|---------|---------|
| PK | `ENTITY#{id}` | Partition key |
| SK | `#META`, `#BUCKET#...`, `#LIMIT#...` | Sort key |
| GSI1PK | `PARENT#{id}` | Parent lookups |
| GSI2PK | `RESOURCE#{name}` | Resource aggregation |

### Lambda Function

- **Runtime**: Python 3.12
- **Memory**: 256 MB
- **Timeout**: 60 seconds
- **Trigger**: DynamoDB Streams

### IAM Permissions

The Lambda function has minimal permissions:

```yaml
- dynamodb:GetItem
- dynamodb:PutItem
- dynamodb:UpdateItem
- dynamodb:Query
```

## Monitoring

### CloudWatch Metrics

DynamoDB provides built-in metrics:

- `ConsumedReadCapacityUnits`
- `ConsumedWriteCapacityUnits`
- `ThrottledRequests`
- `SystemErrors`

### CloudWatch Alarms

The stack includes optional alarms:

```bash
zae-limiter deploy \
    --table-name rate_limits \
    --enable-alarms \
    --alarm-sns-topic arn:aws:sns:us-east-1:123456789:alerts
```

### Lambda Monitoring

Monitor the aggregator function:

- Invocation count
- Error rate
- Duration
- Iterator age (stream lag)

## Cost Estimation

| Component | Cost Driver |
|-----------|-------------|
| DynamoDB | Read/write capacity units |
| Lambda | Invocations, duration |
| CloudWatch | Log storage, metrics |

For a typical workload (1M requests/day):

- DynamoDB: ~$10-50/month
- Lambda: ~$1-5/month
- CloudWatch: ~$1-5/month

## Next Steps

- [LocalStack](localstack.md) - Local development setup
- [CloudFormation](cloudformation.md) - Template details
