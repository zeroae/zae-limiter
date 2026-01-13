# zae-limiter FastAPI Demo

A comprehensive demo of [zae-limiter](https://github.com/zeroae/zae-limiter) using FastAPI and LocalStack. This example demonstrates rate limiting for LLM APIs with:

- OpenAI-compatible chat completions API with rate limiting
- Real-time dashboard with Server-Sent Events (SSE) for live updates
- Hierarchical limits (project -> API keys)
- Token estimation and post-hoc reconciliation
- Configurable `max_tokens` to generate variable-length responses
- Low TPM limits for visible rate limiting in the demo
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

The demo is pre-configured with hierarchical entities and balanced limits to demonstrate both RPM and TPM rate limiting:

| Entity | Type | RPM | TPM | Threshold | Description |
|--------|------|-----|-----|-----------|-------------|
| `proj-demo` | Project | 50 | 10,000 | 200 tok/req | Shared project limit |
| `key-alice` | API Key | 30 | 3,000 | 100 tok/req | Premium tier |
| `key-bob` | API Key | 10 | 1,000 | 100 tok/req | Easy to hit both limits |
| `key-charlie` | API Key | 20 | 2,000 | 100 tok/req | Default limits |

**Threshold** = TPM / RPM. This is the tokens-per-request where both limits are equally restrictive:
- **Below threshold**: RPM limit is hit first (many small requests)
- **Above threshold**: TPM limit is hit first (fewer large requests)

Example with `key-bob` (10 RPM, 1000 TPM, threshold = 100):
- At 50 max_tokens: Can make ~20 requests before TPM, but only 10 before RPM → **hits RPM**
- At 200 max_tokens: Can make ~5 requests before TPM, but 10 before RPM → **hits TPM**

When cascade mode is enabled (default), API key limits and project limits are both enforced.

## API Endpoints

### Chat Completions

OpenAI-compatible chat API with rate limiting. Use `max_tokens` to control response length and token consumption.

```bash
# Small request (50 tokens) - tests RPM limit
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: key-bob" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 50
  }'

# Large request (200 tokens) - tests TPM limit
# key-bob has 1000 TPM, so ~5 of these will exhaust the limit
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: key-bob" \
  -d '{
    "model": "gpt-4",
    "messages": [{"role": "user", "content": "Hello!"}],
    "max_tokens": 200
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

# Stream real-time updates (SSE)
curl -N http://localhost:8080/api/dashboard/stream
```

## Testing Rate Limits

### Manual Testing

1. Open the dashboard at http://localhost:8080/dashboard
2. Notice the "Live" indicator showing real-time SSE connection
3. Select `key-bob` (10 RPM, 1000 TPM) for easy limit testing

**Test TPM limit (large requests):**
4. Set `max_tokens` to 200 or higher
5. Click "Send Request" a few times - watch TPM utilization spike
6. After ~5 requests, you'll hit the TPM limit (429 error)

**Test RPM limit (small requests):**
7. Set `max_tokens` to 50 (below the 100 token threshold)
8. Click "Send Request" rapidly - watch RPM utilization increase
9. After 10 requests, you'll hit the RPM limit

**Watch real-time refill:**
10. Wait and watch the utilization bars decrease as tokens refill (~1 second updates)

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

# Scan rate limits table (name "demo" creates table "ZAEL-demo")
aws --endpoint-url=http://localhost:4566 dynamodb scan --table-name ZAEL-demo
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
  --table-name ZAEL-demo
```

## Learn More

- [zae-limiter Documentation](https://github.com/zeroae/zae-limiter)
- [Token Bucket Algorithm](https://en.wikipedia.org/wiki/Token_bucket)
- [LocalStack Documentation](https://docs.localstack.cloud/)
