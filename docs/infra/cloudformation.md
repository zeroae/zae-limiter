# CloudFormation Template

This guide covers the CloudFormation template used by zae-limiter and how to customize it.

## Template Overview

The template creates:

```
┌─────────────────────────────────────────────────────┐
│                CloudFormation Stack                  │
├─────────────────────────────────────────────────────┤
│  ┌─────────────┐    ┌─────────────┐                 │
│  │  DynamoDB   │───▶│   Stream    │                 │
│  │   Table     │    │             │                 │
│  └─────────────┘    └──────┬──────┘                 │
│                            │                         │
│                            ▼                         │
│                    ┌─────────────┐                  │
│                    │   Lambda    │                  │
│                    │ Aggregator  │                  │
│                    └──────┬──────┘                  │
│                            │                         │
│                            ▼                         │
│                    ┌─────────────┐                  │
│                    │ CloudWatch  │                  │
│                    │    Logs     │                  │
│                    └─────────────┘                  │
└─────────────────────────────────────────────────────┘
```

## Export Template

```bash
# Export to file
zae-limiter cfn-template > template.yaml

# View template
zae-limiter cfn-template | less
```

## Template Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `SnapshotWindows` | String | `hourly,daily` | Comma-separated list of snapshot windows |
| `SnapshotRetentionDays` | Number | `90` | Days to retain usage snapshots (1-3650) |
| `LambdaMemorySize` | Number | `256` | Memory for aggregator Lambda (128-3008 MB) |
| `LambdaTimeout` | Number | `60` | Timeout for aggregator Lambda (1-900 seconds) |
| `EnableAggregator` | String | `true` | Whether to deploy the aggregator Lambda |
| `SchemaVersion` | String | `1.0.0` | Schema version for infrastructure |
| `PITRRecoveryPeriodDays` | String | _(empty)_ | PITR period (1-35 days, empty for AWS default) |
| `EnableAlarms` | String | `true` | Whether to deploy CloudWatch alarms |
| `AlarmSNSTopicArn` | String | _(empty)_ | SNS topic ARN for alarm notifications |
| `LogRetentionDays` | Number | `30` | CloudWatch log retention (standard periods) |

## DynamoDB Table

### Schema

```yaml
AttributeDefinitions:
  - AttributeName: PK
    AttributeType: S
  - AttributeName: SK
    AttributeType: S
  - AttributeName: GSI1PK
    AttributeType: S
  - AttributeName: GSI1SK
    AttributeType: S
  - AttributeName: GSI2PK
    AttributeType: S
  - AttributeName: GSI2SK
    AttributeType: S

KeySchema:
  - AttributeName: PK
    KeyType: HASH
  - AttributeName: SK
    KeyType: RANGE
```

### Global Secondary Indexes

**GSI1** - Parent to children lookups:

```yaml
GlobalSecondaryIndexes:
  - IndexName: GSI1
    KeySchema:
      - AttributeName: GSI1PK  # PARENT#{parent_id}
        KeyType: HASH
      - AttributeName: GSI1SK  # CHILD#{child_id}
        KeyType: RANGE
```

**GSI2** - Resource aggregation:

```yaml
  - IndexName: GSI2
    KeySchema:
      - AttributeName: GSI2PK  # RESOURCE#{resource}
        KeyType: HASH
      - AttributeName: GSI2SK  # BUCKET#{entity_id}#{limit_name}
        KeyType: RANGE
```

### Stream Configuration

```yaml
StreamSpecification:
  StreamViewType: NEW_AND_OLD_IMAGES
```

## Lambda Aggregator

### Function Configuration

```yaml
AggregatorFunction:
  Type: AWS::Lambda::Function
  Properties:
    Runtime: python3.12
    Handler: zae_limiter.aggregator.handler.lambda_handler
    MemorySize: 256
    Timeout: 60
    Environment:
      Variables:
        TABLE_NAME: !Ref TableName
        SNAPSHOT_WINDOWS: !Ref SnapshotWindows
        SNAPSHOT_TTL_DAYS: !Ref SnapshotRetentionDays
```

### Event Source Mapping

```yaml
StreamEventMapping:
  Type: AWS::Lambda::EventSourceMapping
  Properties:
    EventSourceArn: !GetAtt Table.StreamArn
    FunctionName: !Ref AggregatorFunction
    StartingPosition: LATEST
    BatchSize: 100
    MaximumBatchingWindowInSeconds: 5
```

## IAM Permissions

### Lambda Execution Role

```yaml
AggregatorRole:
  Type: AWS::IAM::Role
  Properties:
    AssumeRolePolicyDocument:
      Statement:
        - Effect: Allow
          Principal:
            Service: lambda.amazonaws.com
          Action: sts:AssumeRole
    Policies:
      - PolicyName: DynamoDBAccess
        PolicyDocument:
          Statement:
            - Effect: Allow
              Action:
                - dynamodb:GetItem
                - dynamodb:PutItem
                - dynamodb:UpdateItem
                - dynamodb:Query
              Resource: !GetAtt Table.Arn
            - Effect: Allow
              Action:
                - dynamodb:GetRecords
                - dynamodb:GetShardIterator
                - dynamodb:DescribeStream
                - dynamodb:ListStreams
              Resource: !Sub "${Table.Arn}/stream/*"
```

## Customization

### Add Dead Letter Queue

```yaml
Parameters:
  EnableDLQ:
    Type: String
    Default: "false"
    AllowedValues: ["true", "false"]

Conditions:
  CreateDLQ: !Equals [!Ref EnableDLQ, "true"]

Resources:
  DeadLetterQueue:
    Type: AWS::SQS::Queue
    Condition: CreateDLQ
    Properties:
      QueueName: !Sub "${TableName}-aggregator-dlq"
      MessageRetentionPeriod: 1209600  # 14 days

  StreamEventMapping:
    Properties:
      DestinationConfig:
        OnFailure:
          Destination: !If
            - CreateDLQ
            - !GetAtt DeadLetterQueue.Arn
            - !Ref AWS::NoValue
```

### Add CloudWatch Alarms

```yaml
ReadThrottleAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: !Sub "${TableName}-read-throttle"
    AlarmDescription: Alert when DynamoDB read requests are throttled
    MetricName: ReadThrottleEvents
    Namespace: AWS/DynamoDB
    Statistic: Sum
    Period: 300  # 5 minutes
    EvaluationPeriods: 2
    Threshold: 1
    ComparisonOperator: GreaterThanThreshold
    Dimensions:
      - Name: TableName
        Value: !Ref RateLimitsTable
    TreatMissingData: notBreaching
    AlarmActions: !If
      - HasSNSTopic
      - [!Ref AlarmSNSTopicArn]
      - !Ref AWS::NoValue
```

### Enable Encryption with CMK

```yaml
Parameters:
  KmsKeyArn:
    Type: String
    Default: ""

Conditions:
  UseCustomKey: !Not [!Equals [!Ref KmsKeyArn, ""]]

Resources:
  Table:
    Properties:
      SSESpecification:
        SSEEnabled: true
        SSEType: !If [UseCustomKey, "KMS", "AWS_OWNED_KEY"]
        KMSMasterKeyId: !If [UseCustomKey, !Ref KmsKeyArn, !Ref AWS::NoValue]
```

## Deployment Examples

### Basic Deployment

```bash
aws cloudformation deploy \
    --template-file template.yaml \
    --stack-name zae-limiter \
    --capabilities CAPABILITY_NAMED_IAM
```

### With Custom Parameters

```bash
aws cloudformation deploy \
    --template-file template.yaml \
    --stack-name ZAEL-prod \
    --parameter-overrides \
        PITRRecoveryPeriodDays=35 \
        SnapshotRetentionDays=365 \
        LogRetentionDays=90 \
        EnableAlarms=true \
    --capabilities CAPABILITY_NAMED_IAM
```

### Using SAM

```yaml
# samconfig.toml
[default.deploy.parameters]
stack_name = "ZAEL-limiter"
capabilities = "CAPABILITY_NAMED_IAM"
```

```bash
sam deploy --guided
```

## Outputs

The template exports:

| Output | Description |
|--------|-------------|
| `TableArn` | DynamoDB table ARN |
| `StreamArn` | DynamoDB stream ARN |
| `FunctionArn` | Lambda function ARN |

Access outputs:

```bash
aws cloudformation describe-stacks \
    --stack-name zae-limiter \
    --query "Stacks[0].Outputs"
```

## Next Steps

- [Deployment](deployment.md) - Deployment guide
- [LocalStack](localstack.md) - Local development
