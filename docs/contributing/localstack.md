# LocalStack Development

LocalStack provides a local AWS environment for development and testing. This guide covers setting up zae-limiter with LocalStack.

## Why LocalStack?

- **Free** - No AWS costs during development
- **Fast** - No network latency
- **Isolated** - No risk to production data
- **Full stack** - DynamoDB, Lambda, Streams, CloudFormation

## Quick Start

### 1. Start LocalStack

=== "Docker Compose (Preferred)"

    The project includes a pre-configured `docker-compose.yml` at the repository root:

    ```bash
    # From the project root
    docker compose up -d
    ```

    This is the preferred method as it includes all required configuration for Lambda execution.

=== "Docker"

    ```bash
    docker run -d \
      --name localstack \
      -p 4566:4566 \
      -e SERVICES=dynamodb,dynamodbstreams,lambda,cloudformation,logs,iam,cloudwatch,sqs \
      -v /var/run/docker.sock:/var/run/docker.sock \
      -v "${TMPDIR:-/tmp}/localstack:/var/lib/localstack" \
      localstack/localstack
    ```

    !!! important "Docker Socket Required"
        The Docker socket mount (`-v /var/run/docker.sock:/var/run/docker.sock`) is required for LocalStack to spawn Lambda functions as Docker containers.

=== "LocalStack CLI"

    ```bash
    pip install localstack
    localstack start -d
    ```

### 2. Deploy Infrastructure

```bash
zae-limiter deploy \
    --name limiter \
    --endpoint-url http://localhost:4566 \
    --region us-east-1
```

### 3. Use in Code

```python
from zae_limiter import RateLimiter, Limit

limiter = RateLimiter(
    name="limiter",  # Creates ZAEL-limiter resources
    endpoint_url="http://localhost:4566",
    region="us-east-1",
)

async with limiter.acquire(
    entity_id="test-user",
    resource="api",
    limits=[Limit.per_minute("requests", 100)],
    consume={"requests": 1},
) as lease:
    print("Rate limited request!")
```

## Declarative Infrastructure

For quick iteration, declare infrastructure in code:

```python
from zae_limiter import RateLimiter, StackOptions

limiter = RateLimiter(
    name="limiter",  # ZAEL-limiter resources
    endpoint_url="http://localhost:4566",
    region="us-east-1",
    stack_options=StackOptions(),  # Declare desired state
)
```

## Environment Variables

Configure via environment variables for easy switching:

```bash
# .env.local
AWS_ENDPOINT_URL=http://localhost:4566
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
AWS_DEFAULT_REGION=us-east-1
```

```python
import os

limiter = RateLimiter(
    name="limiter",  # Connects to ZAEL-limiter
    endpoint_url=os.getenv("AWS_ENDPOINT_URL"),
    region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
)
```

## Debugging

### Check Stack Status

```bash
# List stacks
aws --endpoint-url=http://localhost:4566 cloudformation list-stacks

# Describe stack
aws --endpoint-url=http://localhost:4566 cloudformation describe-stacks \
    --stack-name ZAEL-limiter
```

### Inspect DynamoDB

```bash
# List tables
aws --endpoint-url=http://localhost:4566 dynamodb list-tables

# Scan table
aws --endpoint-url=http://localhost:4566 dynamodb scan \
    --table-name ZAEL-limiter
```

### View Lambda Logs

```bash
# List functions
aws --endpoint-url=http://localhost:4566 lambda list-functions

# Get logs
aws --endpoint-url=http://localhost:4566 logs tail \
    /aws/lambda/zae-limiter-aggregator
```

## LocalStack vs DynamoDB Local

| Feature | LocalStack | DynamoDB Local |
|---------|------------|----------------|
| DynamoDB | Yes | Yes |
| Streams | Yes | Limited |
| Lambda | Yes | No |
| CloudFormation | Yes | No |
| Cost | Free | Free |
| Fidelity | High | Medium |

**Recommendation**: Use LocalStack for full integration testing, DynamoDB Local for quick unit tests.

## Troubleshooting

### Connection Refused

```
Cannot connect to http://localhost:4566
```

**Solution**: Ensure LocalStack is running:

```bash
docker ps | grep localstack
# or
curl http://localhost:4566/_localstack/health
```

### Lambda Not Executing

Check Lambda logs:

```bash
docker logs localstack 2>&1 | grep -i lambda
```

Ensure the Lambda service is enabled:

```bash
docker run -e SERVICES=dynamodb,dynamodbstreams,lambda,...
```

### Slow Performance

LocalStack can be slow on first request. Consider:

- Pre-warming containers
- Using persistence for faster restarts
- Reducing DEBUG level

## Next Steps

- [Testing](testing.md) - pytest fixtures and CI configuration
- [Development Setup](development.md) - Local development environment
