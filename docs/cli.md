# CLI Reference

zae-limiter provides a command-line interface for managing infrastructure and deployments.

## Installation

The CLI is included with the package:

```bash
pip install zae-limiter
```

Verify installation:

```bash
zae-limiter --version
```

## Commands

### deploy

Deploy the CloudFormation stack with DynamoDB table and Lambda aggregator.

```bash
zae-limiter deploy [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name` | Resource identifier (creates ZAEL-{name} resources) | Required |
| `--region` | AWS region | Required |
| `--no-aggregator` | Skip Lambda aggregator | `false` |
| `--log-retention-days` | CloudWatch log retention (days) | `14` |
| `--pitr-recovery-days` | Point-in-time recovery (days, 0=disabled) | `0` |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |

**Examples:**

```bash
# Basic deployment
zae-limiter deploy --name limiter --region us-east-1

# With custom settings
zae-limiter deploy \
    --name prod \
    --region us-west-2 \
    --log-retention-days 90 \
    --pitr-recovery-days 7

# Deploy to LocalStack
zae-limiter deploy \
    --name limiter \
    --endpoint-url http://localhost:4566 \
    --region us-east-1

# Without Lambda aggregator
zae-limiter deploy \
    --name limiter \
    --region us-east-1 \
    --no-aggregator
```

---

### status

Check the status of a deployed CloudFormation stack.

```bash
zae-limiter status [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name` | Resource identifier (ZAEL-{name}) | Required |
| `--region` | AWS region | Required |
| `--endpoint-url` | Custom AWS endpoint | None |

**Example:**

```bash
zae-limiter status --name limiter --region us-east-1
```

**Output:**

```
Stack: ZAEL-limiter
Status: CREATE_COMPLETE
Created: 2024-01-15 10:30:00

Resources:
  - LimiterTable (AWS::DynamoDB::Table): CREATE_COMPLETE
  - AggregatorFunction (AWS::Lambda::Function): CREATE_COMPLETE
  - AggregatorRole (AWS::IAM::Role): CREATE_COMPLETE

Outputs:
  - TableName: ZAEL-limiter
  - TableArn: arn:aws:dynamodb:us-east-1:123456789:table/ZAEL-limiter
```

---

### delete

Delete a CloudFormation stack and all its resources.

```bash
zae-limiter delete [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name` | Resource identifier (ZAEL-{name}) | Required |
| `--region` | AWS region | Required |
| `--yes` | Skip confirmation prompt | `false` |
| `--endpoint-url` | Custom AWS endpoint | None |

**Example:**

```bash
# With confirmation
zae-limiter delete --name limiter --region us-east-1

# Skip confirmation
zae-limiter delete --name limiter --region us-east-1 --yes
```

!!! warning "Data Loss"
    Deleting a stack removes the DynamoDB table and all its data.
    This action cannot be undone.

---

### cfn-template

Export the CloudFormation template to stdout.

```bash
zae-limiter cfn-template [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--format` | Output format (yaml, json) | `yaml` |

**Examples:**

```bash
# Export YAML template
zae-limiter cfn-template > template.yaml

# Export JSON template
zae-limiter cfn-template --format json > template.json

# View template
zae-limiter cfn-template | less
```

---

### lambda-export

Export the Lambda deployment package.

```bash
zae-limiter lambda-export [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--output` | Output file path | Required (unless `--info`) |
| `--info` | Show package info without building | `false` |

**Examples:**

```bash
# Export Lambda package
zae-limiter lambda-export --output lambda.zip

# Show package info
zae-limiter lambda-export --info
```

**Info output:**

```
Lambda Package Info:
  Handler: zae_limiter.aggregator.handler.lambda_handler
  Runtime: python3.12
  Estimated size: ~30KB
  Dependencies: boto3 (provided by Lambda runtime)
```

---

### version

Show the installed version.

```bash
zae-limiter version
```

**Output:**

```
zae-limiter 0.1.0
```

---

### check

Check schema compatibility with a deployed table.

```bash
zae-limiter check [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name` | Resource identifier (ZAEL-{name}) | Required |
| `--region` | AWS region | Required |
| `--endpoint-url` | Custom AWS endpoint | None |

**Example:**

```bash
zae-limiter check --name limiter --region us-east-1
```

**Output:**

```
Schema version: 1.0.0
Library version: 1.0.0
Status: Compatible
```

---

### upgrade

Upgrade a table schema to the latest version.

```bash
zae-limiter upgrade [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name` | Resource identifier (ZAEL-{name}) | Required |
| `--region` | AWS region | Required |
| `--endpoint-url` | Custom AWS endpoint | None |
| `--dry-run` | Show changes without applying | `false` |

**Example:**

```bash
# Preview changes
zae-limiter upgrade --name limiter --region us-east-1 --dry-run

# Apply upgrade
zae-limiter upgrade --name limiter --region us-east-1
```

## Environment Variables

The CLI respects standard AWS environment variables:

| Variable | Description |
|----------|-------------|
| `AWS_ACCESS_KEY_ID` | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key |
| `AWS_SESSION_TOKEN` | AWS session token |
| `AWS_DEFAULT_REGION` | Default AWS region |
| `AWS_PROFILE` | AWS profile name |
| `AWS_ENDPOINT_URL` | Custom endpoint URL |

## Exit Codes

| Code | Description |
|------|-------------|
| `0` | Success |
| `1` | General error |
| `2` | Invalid arguments |
| `3` | AWS API error |
| `4` | Stack not found |

## Next Steps

- [Deployment](infra/deployment.md) - Deployment guide
- [LocalStack](infra/localstack.md) - Local development
- [API Reference](api/index.md) - Python API documentation
