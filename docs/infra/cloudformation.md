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
| `TableName` | String | `rate_limits` | DynamoDB table name |
| `SnapshotWindows` | String | `hourly,daily` | Aggregation windows |
| `SnapshotRetentionDays` | Number | `90` | Days to retain usage data |
| `EnablePITR` | Boolean | `false` | Point-in-time recovery |
| `LogRetentionDays` | Number | `14` | CloudWatch log retention |

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
        RETENTION_DAYS: !Ref SnapshotRetentionDays
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
      QueueName: !Sub "${TableName}-dlq"
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
ThrottleAlarm:
  Type: AWS::CloudWatch::Alarm
  Properties:
    AlarmName: !Sub "${TableName}-throttle"
    MetricName: ThrottledRequests
    Namespace: AWS/DynamoDB
    Dimensions:
      - Name: TableName
        Value: !Ref Table
    Statistic: Sum
    Period: 60
    EvaluationPeriods: 1
    Threshold: 1
    ComparisonOperator: GreaterThanOrEqualToThreshold
    AlarmActions:
      - !Ref AlertTopic
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
    --stack-name zae-limiter-prod \
    --parameter-overrides \
        TableName=prod_rate_limits \
        EnablePITR=true \
        SnapshotRetentionDays=365 \
        LogRetentionDays=90 \
    --capabilities CAPABILITY_NAMED_IAM
```

### Using SAM

```yaml
# samconfig.toml
[default.deploy.parameters]
stack_name = "zae-limiter"
capabilities = "CAPABILITY_NAMED_IAM"
parameter_overrides = "TableName=rate_limits"
```

```bash
sam deploy --guided
```

## Outputs

The template exports:

| Output | Description |
|--------|-------------|
| `TableName` | DynamoDB table name |
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
