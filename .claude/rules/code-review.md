# Code Review Guidelines

When reviewing PRs, check the following based on files changed:

## Test Coverage (changes to src/)
- Verify corresponding tests exist in appropriate test directory (unit/integration/e2e)
- Check edge cases: negative values, empty collections, None
- Ensure async tests have sync counterparts
- Flag if new public methods lack tests

## Async/Sync Parity (changes to limiter.py, lease.py, repository.py)
- Verify SyncRateLimiter has matching sync methods
- Check SyncLease matches AsyncLease functionality
- Ensure error handling is consistent
- Confirm both unit test files updated (tests/unit/)

## Infrastructure (changes to infra/, zae_limiter_aggregator/)
- Validate CloudFormation template syntax
- Check IAM follows least privilege
- Verify Lambda handler signature
- Ensure all records use flat schema (top-level attributes, no nested `data.M`). See ADR-111
- Schema changes require version bumps, migrations, and careful rollout planning
- Only update version.py if schema change is unavoidable

## DynamoDB Schema (changes to schema.py, repository.py)
- Verify key builders follow single-table patterns
- Check GSI usage matches access patterns
- Validate transaction limits (max 100 items)
- Ensure patterns documented in CLAUDE.md

## API Documentation (changes to __init__.py, models.py)
- Verify docstrings exist and are accurate
- Check type hints match descriptions
- Flag public API changes without changelog entry

## LocalStack Configuration (changes to cli.py local commands, docker-compose.yml, .github/workflows/ci.yml)
- The CLI (`zae-limiter local up`) is the source of truth for LocalStack container configuration
- Verify `docker-compose.yml` matches CLI container settings (image, services, env vars, volumes, healthcheck)
- Verify CI workflow LocalStack service definitions match CLI container settings
- Flag any drift between the three: CLI, `docker-compose.yml`, CI workflows

## Design Validation (new features with derived data)
When implementing features that derive data from state changes (like consumption from token deltas), use the `design-validator` agent to validate the approach before implementation. See issue #179 for an example where the snapshot aggregator failed because `old_tokens - new_tokens` doesn't work when refill rate exceeds consumption rate.
