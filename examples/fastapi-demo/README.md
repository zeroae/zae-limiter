# zae-limiter FastAPI Demo

A comprehensive demo of [zae-limiter](https://github.com/zeroae/zae-limiter) using FastAPI and LocalStack. This example demonstrates rate limiting for LLM APIs with:

- OpenAI-compatible chat completions API with rate limiting
- Dashboard for viewing entities and rate limit status
- Hierarchical limits (project -> API keys)
- Token estimation and post-hoc reconciliation
- LocalStack for AWS services (DynamoDB, DynamoDB Streams, Lambda)

## Quick Start

```bash
# Start all services
docker compose up -d

# Wait for initialization (about 30 seconds)
docker compose logs -f init

# Open the dashboard
open http://localhost:8080/dashboard

# Or check the API docs
open http://localhost:8080/docs
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Docker Network                        │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   │
│  │  LocalStack │   │   Init      │   │    API      │   │
│  │  :4566      │◄──│  (one-shot) │   │   :8080     │   │
│  │             │   │             │   │             │   │
│  │ - DynamoDB  │   │ - Create    │   │ - FastAPI   │   │
│  │ - Streams   │   │   table     │   │ - Dashboard │   │
│  │ - Lambda    │   │ - Seed data │   │ - Chat API  │   │
│  └─────────────┘   └─────────────┘   └─────────────┘   │
└─────────────────────────────────────────────────────────┘
```

## Demo Entities

The demo is pre-configured with hierarchical entities:

| Entity | Type | RPM Limit | TPM Limit | Description |
|--------|------|-----------|-----------|-------------|
| `proj-demo` | Project | 200 | 500,000 | Shared project limit |
| `key-alice` | API Key | 100 | 200,000 | Premium tier |
| `key-bob` | API Key | 50 | 50,000 | Standard tier |
| `key-charlie` | API Key | 60 | 100,000 | Default limits |

When cascade mode is enabled (default), API key limits and project limits are both enforced.

## API Endpoints

### Chat Completions

OpenAI-compatible chat API with rate limiting.

```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: key-alice" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

**Response (success):**
```json
{
  "id": "chatcmpl-abc123",
  "model": "gpt-4",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "..."},
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 10,
    "completion_tokens": 50,
    "total_tokens": 60
  }
}
```

**Response (rate limited):**
```json
{
  "error": "rate_limit_exceeded",
  "message": "Rate limit exceeded for limits: rpm",
  "retry_after_seconds": 5.2,
  "violations": [{"limit_name": "rpm", "available": 0, "requested": 1}]
}
```

### Entity Management

```bash
# List entity children
curl http://localhost:8080/api/entities/proj-demo/children

# Get entity details
curl http://localhost:8080/api/entities/key-alice

# Create new entity
curl -X POST http://localhost:8080/api/entities \
  -H "Content-Type: application/json" \
  -d '{
    "entity_id": "key-new",
    "name": "New API Key",
    "parent_id": "proj-demo"
  }'
```

### Rate Limit Configuration

```bash
# Get current limits
curl http://localhost:8080/api/limits/key-alice?resource=gpt-4

# Update limits
curl -X PUT http://localhost:8080/api/limits/key-alice \
  -H "Content-Type: application/json" \
  -d '{
    "resource": "gpt-4",
    "limits": [
      {"name": "rpm", "capacity": 120, "refill_rate": 2.0},
      {"name": "tpm", "capacity": 300000, "refill_rate": 5000.0}
    ]
  }'
```

### Dashboard Data

```bash
# Get all entities with status
curl http://localhost:8080/api/dashboard/entities

# Check availability
curl http://localhost:8080/api/dashboard/availability/key-alice

# Get wait time
curl "http://localhost:8080/api/dashboard/wait-time/key-alice?rpm=1&tpm=1000"
```

## Testing Rate Limits

### Manual Testing

1. Open the dashboard at http://localhost:8080/dashboard
2. Select an API key (e.g., `key-bob` with lower limits)
3. Click "Send Request" multiple times quickly
4. Watch the utilization bars fill up
5. See 429 responses when limits are exceeded

### Load Testing

```bash
# Install httpx if not available
pip install httpx

# Run load test (3 workers, 30 seconds)
python scripts/load_test.py --concurrency 3 --duration 30

# More aggressive test
python scripts/load_test.py -c 10 -d 60 --delay 0.05
```

## Development

### View Logs

```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f api
docker compose logs -f localstack
```

### Restart Services

```bash
# Restart API (keeps LocalStack data)
docker compose restart api

# Full restart
docker compose down && docker compose up -d
```

### Access LocalStack Directly

```bash
# List tables
aws --endpoint-url=http://localhost:4566 dynamodb list-tables

# Scan rate limits table
aws --endpoint-url=http://localhost:4566 dynamodb scan --table-name rate_limits
```

## Cleanup

```bash
# Stop and remove containers
docker compose down

# Also remove volumes (clears all data)
docker compose down -v
```

## Troubleshooting

### Init container fails

Check LocalStack health:
```bash
curl http://localhost:4566/_localstack/health
```

### API returns connection errors

Wait for init to complete:
```bash
docker compose logs init
# Should show "Initialization Complete"
```

### Rate limits not working

Verify table was created:
```bash
aws --endpoint-url=http://localhost:4566 dynamodb describe-table \
  --table-name rate_limits
```

## Learn More

- [zae-limiter Documentation](https://github.com/zeroae/zae-limiter)
- [Token Bucket Algorithm](https://en.wikipedia.org/wiki/Token_bucket)
- [LocalStack Documentation](https://docs.localstack.cloud/)
