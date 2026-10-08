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

## Quota grants (changes touching a quota's `tk` or `gc`)
A sharded quota conserves its allowance only while every writer follows ADR-145 (I1–I8, listed
under CLAUDE.md "Important Invariants"). For any change to `models.plan_quota_grant` /
`quota_period_is_current`, `Repository.plan_quota_shard` and its donor debits, the grant-size
freeze, the rejected-move commit, a reset or roll, shard creation or seeding, or the aggregator's
Path 1 / Path 2 / refill:
- `tests/unit/test_quota_conservation_fuzz.py` (I8 against the real repository), the acceptance
  tests in `tests/unit/test_quota_shard_creation.py` and `tests/unit/test_window_shard_creation.py`,
  and the planner-vs-model differential in `tests/unit/test_quota_grant_plan.py` must pass
- A new bucket write must be declared in `tests/unit/test_bucket_writer_registry.py` (does it
  write a quota's `tk` or `gc`?) and added to `tests/unit/test_expression_tokens.py`
- The fast path must stay 0 RCU + 1 WCU and never read or write `gc`
  (`tests/benchmark/test_capacity.py::TestQuotaGrantCapacity`)
- Run the `design-validator` agent on any change to grant logic: "who funds this slot, and can two
  writers both fund it?" is a derivation question of the #179 kind

## The rejection cache only rejects (ADR-147)
The client-side rejection cache may reject a request locally or steer a write away from a
shard known to be short. It must never be the basis of an admission. Phase 3, which admitted
through one write built from a cached state, was merged and withdrawn before release after ten
reproduced over-admissions (`docs/plans/2026-10-07-adr147-phase3-withdrawn.md`). For any change
touching `rejection_cache.py` or the limiter's local-rejection path:
- No code path may admit, debit or skip a write because of what the cache holds. Reintroducing
  that needs a new ADR that clears the bar in the withdrawal record
- A new bucket writer, or a config change the slow path reads, must keep
  `tests/unit/test_rejection_cache.py::TestChangesElsewhere` passing; add a case there when the
  change is made from another process (a second `Repository`, or a raw `update_item`)
