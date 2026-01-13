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
    --table-name rate_limits \
    --endpoint-url http://localhost:4566 \
    --region us-east-1
```

### 3. Use in Code

```python
from zae_limiter import RateLimiter, Limit

limiter = RateLimiter(
    table_name="rate_limits",
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

## Auto-Creation Mode

For quick iteration, use auto-creation:

```python
limiter = RateLimiter(
    table_name="rate_limits",
    endpoint_url="http://localhost:4566",
    region="us-east-1",
    create_stack=True,  # Creates CloudFormation stack
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
    table_name="rate_limits",
    endpoint_url=os.getenv("AWS_ENDPOINT_URL"),
    region=os.getenv("AWS_DEFAULT_REGION", "us-east-1"),
)
```

## Testing with LocalStack

### pytest Fixture with Cleanup

For integration tests, use fixtures that properly clean up resources:

```python
import os
import uuid
import pytest
from zae_limiter import RateLimiter, StackOptions

@pytest.fixture
def localstack_endpoint():
    """Get LocalStack endpoint from environment."""
    return os.getenv("AWS_ENDPOINT_URL", "http://localhost:4566")

@pytest.fixture(scope="function")
async def limiter(localstack_endpoint):
    """
    Create a rate limiter connected to LocalStack with automatic cleanup.

    This fixture:
    1. Creates a unique stack for test isolation
    2. Yields the limiter for test use
    3. Deletes the stack in teardown
    """
    # Unique table name prevents test interference
    table_name = f"test_{uuid.uuid4().hex[:8]}"

    limiter = RateLimiter(
        table_name=table_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=StackOptions(enable_aggregator=False),
    )

    async with limiter:
        yield limiter

    # Cleanup: delete the CloudFormation stack
    await limiter.delete_stack()

@pytest.mark.integration
async def test_rate_limiting(limiter):
    async with limiter.acquire(
        entity_id="test-user",
        resource="api",
        limits=[Limit.per_minute("requests", 10)],
        consume={"requests": 1},
    ):
        pass  # Success
```

### Session-Scoped Fixture (Faster)

For test suites where stack creation overhead is significant:

```python
@pytest.fixture(scope="session")
async def shared_limiter(localstack_endpoint):
    """
    Session-scoped limiter for faster test execution.

    Trade-off: Tests share state, less isolation.
    """
    limiter = RateLimiter(
        table_name="integration_test_shared",
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=StackOptions(enable_aggregator=False),
    )

    async with limiter:
        yield limiter

    await limiter.delete_stack()
```

### Sync Fixture Example

```python
@pytest.fixture(scope="function")
def sync_limiter(localstack_endpoint):
    """Synchronous rate limiter with cleanup."""
    from zae_limiter import SyncRateLimiter, StackOptions
    import uuid

    table_name = f"test_sync_{uuid.uuid4().hex[:8]}"

    limiter = SyncRateLimiter(
        table_name=table_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=StackOptions(enable_aggregator=False),
    )

    with limiter:
        yield limiter

    limiter.delete_stack()
```

### CI Configuration

```yaml
# .github/workflows/ci.yml
jobs:
  integration:
    runs-on: ubuntu-latest
    services:
      localstack:
        image: localstack/localstack
        ports:
          - 4566:4566
        env:
          SERVICES: dynamodb,dynamodbstreams,lambda,cloudformation,logs,iam,cloudwatch,sqs
        options: >-
          --mount type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e ".[dev]"
      - run: pytest -m integration
        env:
          AWS_ENDPOINT_URL: http://localhost:4566
          AWS_ACCESS_KEY_ID: test
          AWS_SECRET_ACCESS_KEY: test
```

## Debugging

### Check Stack Status

```bash
# List stacks
aws --endpoint-url=http://localhost:4566 cloudformation list-stacks

# Describe stack
aws --endpoint-url=http://localhost:4566 cloudformation describe-stacks \
    --stack-name zae-limiter-rate_limits
```

### Inspect DynamoDB

```bash
# List tables
aws --endpoint-url=http://localhost:4566 dynamodb list-tables

# Scan table
aws --endpoint-url=http://localhost:4566 dynamodb scan \
    --table-name rate_limits
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

- [Deployment](deployment.md) - Production deployment
- [CloudFormation](cloudformation.md) - Template customization
