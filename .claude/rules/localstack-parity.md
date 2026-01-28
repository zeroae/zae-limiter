# LocalStack Configuration Parity

The **CLI** (`zae-limiter local up`) is the **source of truth** for LocalStack container configuration.

## Settings That Must Stay in Sync

| Setting | Source of Truth |
|---------|----------------|
| LocalStack image | CLI (`DEFAULT_IMAGE`) |
| Services list | CLI (`LOCALSTACK_SERVICES`) |
| Environment variables | CLI container config |
| Volume mounts | CLI container config |
| Healthcheck | CLI container config |
| Container name | CLI (`CONTAINER_NAME`) |

## Consumers That Must Match

1. **`docker-compose.yml`** — for developers who prefer `docker compose up -d`
2. **`.github/workflows/ci.yml`** — LocalStack service containers in integration, e2e, and benchmark jobs

## When Updating LocalStack Configuration

1. Update the CLI code (source of truth)
2. Update `docker-compose.yml` to match
3. Update CI workflow LocalStack service definitions to match
