# Version Management

This guide covers version compatibility issues and upgrade procedures for zae-limiter.

## Decision Tree

```mermaid
flowchart TD
    START([Version Issue]) --> Q1{What's happening?}

    Q1 -->|VersionMismatchError| A1[Lambda needs update]
    Q1 -->|IncompatibleSchemaError| A2[Schema migration required]
    Q1 -->|Minimum client error| A3[Upgrade pip package]
    Q1 -->|Planning upgrade| A4[Follow upgrade procedure]

    A1 --> CMD1["zae-limiter upgrade --name X"]
    A2 --> CMD2[Follow migrations guide]
    A3 --> CMD3["pip install --upgrade zae-limiter"]
    A4 --> PROC[Pre-upgrade checklist]

    CMD1 --> VERIFY
    CMD2 --> VERIFY
    CMD3 --> VERIFY
    VERIFY([Verify: zae-limiter check])

    click A1 "#versionmismatcherror" "Version mismatch details"
    click A2 "#incompatibleschemaerror" "Schema error details"
    click CMD1 "#upgrade-procedure" "Upgrade steps"
    click CMD2 "../migrations/" "Migration guide"
    click A4 "#upgrade-procedure" "Upgrade checklist"
    click VERIFY "#verification" "Verify upgrade"
```

## Troubleshooting

### Symptoms

- `VersionMismatchError` exception raised
- `IncompatibleSchemaError` exception raised
- CLI commands fail with version errors
- Rate limiter initialization fails

### Diagnostic Steps

**Check compatibility with CLI:**

```bash
zae-limiter check --name <name> --region <region>
```

**View detailed version information:**

```bash
zae-limiter version --name <name> --region <region>
```

**Query version record directly:**

```bash
aws dynamodb get-item --table-name <name> \
  --key '{"PK": {"S": "SYSTEM#"}, "SK": {"S": "#VERSION"}}'
```

### VersionMismatchError

**Cause:** Client library version differs from deployed Lambda version.

**Example error:**
```
VersionMismatchError: Version mismatch: client=1.2.0, schema=1.0.0, lambda=1.0.0.
Lambda update available.
```

**Solution:** Upgrade Lambda to match client:

```bash
zae-limiter upgrade --name <name> --region <region>
```

Or programmatically:

```python
from zae_limiter import Repository, RateLimiter

# Auto-update Lambda on initialization (default behavior)
repo = await Repository.builder().build()
limiter = RateLimiter(repository=repo)
```

### IncompatibleSchemaError

**Cause:** Major version difference requiring schema migration.

**Example error:**
```
IncompatibleSchemaError: Incompatible schema: client 2.0.0 is not compatible
with schema 1.0.0. Migration required.
```

**Solution:** Follow the [Migration Guide](../migrations.md) to upgrade the schema:

1. Create a backup
2. Run migration
3. Update client

```bash
# Create backup before migration
aws dynamodb create-backup \
  --table-name <name> \
  --backup-name "pre-migration-$(date +%Y%m%d)"
```

Then follow the migration procedures in the [Migration Guide](../migrations.md#sample-migration-v200).

### Minimum Client Version Error

**Cause:** Infrastructure requires a newer client version. From v0.15.0, `Repository.open()`,
`connect()`, `builder().build()` and every CLI command raise `VersionMismatchError` (with
`can_auto_update=False`) when the client is below the version record's `client_min_version`:

```
VersionMismatchError: Version mismatch: client=0.15.0, schema=0.10.0, lambda=0.16.0.
Client version 0.15.0 is below minimum required version 0.16.0. Please upgrade.
```

The minimum is raised automatically: storing a `reset_after` limit raises it to 0.15.0
([Session Quotas](../guide/session-quotas.md)). `zae-limiter deploy`, `zae-limiter upgrade` and
a Lambda auto-update keep the stored minimum; they never lower it.

**Limits:**

- The check runs when a repository is opened. A process opened before the minimum was raised
  keeps running until it restarts.
- Clients older than v0.15.0 ignore the field entirely.

**Solution:** Upgrade the client library:

```bash
pip install --upgrade zae-limiter
```

### Refused `reset_after` write

**Cause:** `set_limits()`, `set_resource_defaults()`, `set_system_defaults()` or
`zae-limiter limits apply` was given a `reset_after` limit while the version record's
`lambda_version` is older than 0.15.0 (or the record is missing). An older aggregator would
over-admit the limit, so nothing is written.

```
VersionMismatchError: Version mismatch: client=0.15.0, schema=0.10.0, lambda=0.14.0.
Refusing to store a reset_after limit: the deployed Lambdas predate 0.15.0 and would
misread it (the aggregator over-admits it). Run 'zae-limiter upgrade' first, ...
```

**Solution:** `zae-limiter upgrade --name <name>`, or open the stack with
`Repository.open()` (which updates the Lambdas), then retry the write.

## Upgrade Procedure

### Pre-upgrade Checklist

Before upgrading, verify the following:

- [ ] Check current version: `zae-limiter version --name <name>`
- [ ] Check compatibility: `zae-limiter check --name <name>`
- [ ] Review release notes for breaking changes
- [ ] Verify PITR is enabled for rollback capability
- [ ] Schedule maintenance window (if major upgrade)
- [ ] Notify stakeholders

### Upgrade Execution

**Standard upgrade (Lambda + client):**

```bash
# Step 1: Upgrade client library
pip install --upgrade zae-limiter

# Step 2: Update infrastructure
zae-limiter upgrade --name <name> --region <region>

# Step 3: Verify
zae-limiter check --name <name> --region <region>
```

**Lambda-only upgrade:**

```bash
# Update Lambda without schema changes
zae-limiter upgrade --name <name> --region <region> --lambda-only
```

**Force upgrade (skip compatibility check):**

!!! warning "Use with caution"
    Only use `--force` when you understand the implications.

```bash
zae-limiter upgrade --name <name> --region <region> --force
```

### Post-upgrade Verification

After upgrading, verify the system is healthy:

1. **Check version alignment:**
   ```bash
   zae-limiter version --name <name>
   ```

2. **Run smoke tests:**
   ```python
   from zae_limiter import Repository, RateLimiter, Limit

   repo = await Repository.open()
   limiter = RateLimiter(repository=repo)

   # Test basic operation
   async with limiter.acquire(
       entity_id="test-entity",
       resource="test",
       limits=[Limit.per_minute("rpm", 100)],
       consume={"rpm": 1},
   ):
       print("Rate limiting working")
   ```

3. **Monitor for 15 minutes:**
   - Check Lambda error rate in CloudWatch
   - Verify usage snapshots are updating
   - Watch for unexpected exceptions in application logs

### Rollback

If issues occur after upgrade, see [Recovery & Rollback](recovery.md#emergency-rollback-decision-matrix).

## Related

- [Migration Guide](../migrations.md) - Schema versioning and migration procedures
- [Recovery & Rollback](recovery.md) - Emergency rollback procedures
- [CLI Reference](../cli.md) - Full CLI command documentation
