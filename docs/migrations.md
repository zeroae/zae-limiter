# Migrations

This guide covers managing schema migrations for zae-limiter infrastructure.

## Overview

Schema migrations handle changes to the DynamoDB table structure when upgrading between major versions of zae-limiter. The migration framework provides:

- **Version tracking** - Infrastructure version stored in DynamoDB
- **Compatibility checking** - Automatic client/infrastructure compatibility validation
- **Migration registry** - Ordered list of migrations between versions
- **Rollback support** - Optional rollback functions for reversible changes

### When Migrations Are Needed

| Change Type | Migration Required? | Version Bump |
|-------------|---------------------|--------------|
| Add optional attribute | No | Patch (x.x.1) |
| Add new index (GSI) | Yes | Minor (x.1.0) |
| Change key structure | Yes | Major (1.0.0) |
| Remove attribute | Yes | Major (1.0.0) |
| Change attribute type | Yes | Major (1.0.0) |

## Version Compatibility

zae-limiter uses semantic versioning with specific compatibility rules.

### Compatibility Rules

```
Client 1.x.x + Schema 1.x.x = Compatible
Client 2.x.x + Schema 1.x.x = Incompatible (migration required)
Client 1.2.0 + Min Client 1.3.0 = Incompatible (upgrade client)
```

**Major version mismatch**: Always incompatible. Schema migration required before the client can operate.

**Minor/patch version mismatch**: Compatible. Client and infrastructure can operate together.

**Minimum client version**: Infrastructure can require a minimum client version. Older clients are rejected.

### Checking Compatibility

Use the CLI to check compatibility without modifying anything:

```bash
# Check current compatibility status
zae-limiter check --name limiter --region us-east-1
```

Output:

```
Compatibility Check
====================

Client:      1.2.0
Schema:      1.0.0
Lambda:      1.1.0

Result: COMPATIBLE (update available)

Lambda update available: 1.1.0 -> 1.2.0

Run 'zae-limiter upgrade' to update.
```

### Viewing Version Information

```bash
# Show detailed version information
zae-limiter version --name limiter --region us-east-1
```

Output:

```
zae-limiter Infrastructure Version
====================================

Client Version:     1.2.0
Schema Version:     1.0.0

Infra Schema:       1.0.0
Lambda Version:     1.1.0
Min Client Version: 0.0.0

Status: COMPATIBLE (Lambda update available)

  Lambda update available: 1.1.0 -> 1.2.0

Run 'zae-limiter upgrade' to update Lambda.
```

### Upgrading Infrastructure

For minor updates (Lambda code, no schema changes):

```bash
# Upgrade Lambda to match client version
zae-limiter upgrade --name limiter --region us-east-1
```

For major version upgrades requiring schema migration, see [Sample Migration: v2.0.0](#sample-migration-v200).

## Schema Modification Approaches

When modifying the DynamoDB schema, prefer solutions that preserve backward compatibility.

### Non-Breaking Changes (Preferred)

These changes don't require migrations:

**Adding optional attributes:**
```python
# Old code works - attribute simply missing
item = {"PK": "ENTITY#123", "SK": "#META", "name": "test"}

# New code adds optional attribute
item = {"PK": "ENTITY#123", "SK": "#META", "name": "test", "tags": ["prod"]}
```

**Using `if_not_exists()` for new nested structures:**
```python
# Initialize nested map only if missing
update_expression = "SET #data.#metrics = if_not_exists(#data.#metrics, :empty_map)"
```

**Adding conditional logic for missing attributes:**
```{.python .lint-only}
# Handle missing attribute gracefully
metrics = item.get("data", {}).get("metrics", {})
request_count = metrics.get("requests", 0)
```

### Breaking Changes (Major Version)

These changes require migrations and major version bumps:

- Changing partition or sort key structure
- Removing required attributes
- Changing attribute data types
- Renaming attributes
- Restructuring nested data

### DynamoDB-Specific Considerations

**GSI Changes:**

- Adding a GSI: Can be done without migration (CloudFormation update)
- Removing a GSI: Requires ensuring no code depends on it
- Changing GSI keys: Requires data migration

**Key Pattern Changes:**

```{.python .lint-only}
# v1.0.0 pattern
PK = f"ENTITY#{entity_id}"
SK = f"#BUCKET#{resource}#{limit_name}"

# v2.0.0 pattern (breaking change!)
PK = f"ENT#{entity_id}"  # Changed prefix
SK = f"BKT#{resource}#{limit_name}"  # Changed prefix
```

Key pattern changes require migrating all existing data.

## Creating a Migration

### Migration File Structure

Create a new file in `src/zae_limiter/migrations/`:

```{.python .lint-only}
# src/zae_limiter/migrations/v1_1_0.py
"""
Migration: v1.1.0 (Add metrics tracking)

This migration adds a metrics attribute to entity metadata
for tracking request statistics.

Changes:
- Add 'metrics' map to entity #META records
- Initialize with empty counters
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import Migration, register_migration

if TYPE_CHECKING:
    from ..repository import Repository


async def migrate_v1_1_0(repository: Repository) -> None:
    """
    Add metrics attribute to all entity metadata records.

    This is a forward-only migration that initializes the
    metrics structure for existing entities.
    """
    # Query all entity metadata records
    # Update each to add metrics if missing
    # Use conditional updates to be idempotent
    pass


async def rollback_v1_1_0(repository: Repository) -> None:
    """
    Remove metrics attribute from entity metadata.

    Note: This loses all collected metrics data.
    """
    # Remove metrics attribute from all entities
    pass


# Register the migration
register_migration(
    Migration(
        version="1.1.0",
        description="Add metrics tracking to entities",
        reversible=True,
        migrate=migrate_v1_1_0,
        rollback=rollback_v1_1_0,
    )
)
```

### Migration Protocol

Migration functions must follow this protocol:

```python
async def __call__(self, repository: Repository) -> None:
    """Execute the migration."""
    ...
```

Key requirements:

1. **Async**: All migrations are async functions
2. **Repository parameter**: Receives a configured Repository instance
3. **Idempotent**: Safe to run multiple times (use conditional updates)
4. **Atomic where possible**: Use transactions for related changes

### Registering Migrations

Migrations are auto-registered when imported. Ensure your migration module is imported in `migrations/__init__.py`:

```{.python .lint-only}
# src/zae_limiter/migrations/__init__.py

# ... existing code ...

# Import built-in migrations to register them
from . import v1_0_0 as _v1_0_0  # noqa: F401, E402
from . import v1_1_0 as _v1_1_0  # noqa: F401, E402  # Add new migration
```

### Updating Schema Version

After adding a migration, update the current schema version:

```python
# src/zae_limiter/version.py

# Current schema version - increment when schema changes
CURRENT_SCHEMA_VERSION = "1.1.0"  # Updated from "1.0.0"
```

## Validating Migrations

### Unit Testing with Moto

Test migrations using moto for fast, isolated tests:

```python
# tests/test_migrations.py
import pytest
from moto import mock_aws

from zae_limiter.migrations import get_migrations_between, apply_migrations
from zae_limiter.repository import Repository


@pytest.fixture
def mock_dynamodb():
    with mock_aws():
        # Create table and seed test data
        yield


@pytest.mark.asyncio
async def test_migration_v1_1_0(mock_dynamodb):
    """Test v1.1.0 migration adds metrics to entities."""
    repo = await Repository.open(stack="test_table")

    # Create test entity without metrics
    await repo.create_entity(entity_id="test-1", name="Test Entity")

    # Apply migration
    applied = await apply_migrations(repo, "1.0.0", "1.1.0")

    assert applied == ["1.1.0"]

    # Verify metrics added - use raw DynamoDB query since Entity
    # dataclass won't have the new 'metrics' field until code is updated
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={"PK": {"S": "{ns}/ENTITY#test-1"}, "SK": {"S": "#META"}},
    )
    item = response["Item"]
    assert "metrics" in item
    assert item["metrics"]["M"]["requests"]["N"] == "0"


@pytest.mark.asyncio
async def test_migration_idempotent(mock_dynamodb):
    """Test migration can be safely run multiple times."""
    repo = await Repository.open(stack="test_table")

    # Create test entity
    await repo.create_entity(entity_id="test-1", name="Test Entity")

    # Run migration twice
    await apply_migrations(repo, "1.0.0", "1.1.0")
    await apply_migrations(repo, "1.0.0", "1.1.0")  # Should not fail

    # Verify single application via raw DynamoDB query
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={"PK": {"S": "{ns}/ENTITY#test-1"}, "SK": {"S": "#META"}},
    )
    item = response["Item"]
    assert item["metrics"]["M"]["requests"]["N"] == "0"
```

### Integration Testing with LocalStack

Test migrations against real AWS-compatible infrastructure:

```python
# tests/test_migrations_integration.py
import os
import pytest

# Skip if LocalStack not available
pytestmark = pytest.mark.skipif(
    not os.environ.get("AWS_ENDPOINT_URL"),
    reason="LocalStack not available"
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_with_localstack():
    """Test migration against LocalStack."""
    endpoint_url = os.environ["AWS_ENDPOINT_URL"]

    repo = await Repository.open(
        stack="test_migrations",
        region="us-east-1",
        endpoint_url=endpoint_url,
    )

    # Deploy infrastructure
    # ... create table with CloudFormation ...

    # Seed test data
    # ... create entities ...

    # Apply migration
    applied = await apply_migrations(repo, "1.0.0", "1.1.0")

    # Verify changes persisted
    # ... assertions ...
```

Run integration tests:

```bash
# Start LocalStack (from project root)
docker compose up -d

# Set environment variables and run integration tests
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
pytest -m integration -v
```

### Production Validation Checklist

Before running migrations in production:

- [ ] **Backup**: Enable Point-in-Time Recovery (PITR) or create on-demand backup
- [ ] **Test**: Run migration against production data copy
- [ ] **Monitor**: Set up CloudWatch alarms for errors
- [ ] **Rollback plan**: Document rollback procedure
- [ ] **Maintenance window**: Schedule during low-traffic period
- [ ] **Communication**: Notify stakeholders of potential downtime

```bash
# Create on-demand backup before migration
aws dynamodb create-backup \
  --table-name limiter \
  --backup-name "pre-migration-$(date +%Y%m%d)"

# Verify PITR is enabled
aws dynamodb describe-continuous-backups \
  --table-name limiter
```

## Rollback Strategies

### Reversible Migrations

For migrations that can be safely undone:

```{.python .lint-only}
register_migration(
    Migration(
        version="1.1.0",
        description="Add metrics tracking",
        reversible=True,  # Can be rolled back
        migrate=migrate_v1_1_0,
        rollback=rollback_v1_1_0,  # Rollback function
    )
)
```

Rollback removes or reverts the changes:

```python
async def rollback_v1_1_0(repository: Repository) -> None:
    """Remove metrics attribute from all entities."""
    # Implementation to remove metrics attribute
    pass
```

### Forward-Only Migrations

Some migrations cannot be reversed:

```{.python .lint-only}
register_migration(
    Migration(
        version="2.0.0",
        description="Restructure key patterns",
        reversible=False,  # Cannot be rolled back
        migrate=migrate_v2_0_0,
        rollback=None,  # No rollback function
    )
)
```

Forward-only migrations typically involve:

- Data transformation with information loss
- Key structure changes
- Removing deprecated attributes

### Emergency Rollback Procedures

If a migration fails or causes issues:

**1. Stop the bleeding:**
```bash
# Revert to previous client version
pip install zae-limiter==1.0.0
```

**2. Restore from backup (if needed):**
```bash
# Restore from PITR
aws dynamodb restore-table-to-point-in-time \
  --source-table-name limiter \
  --target-table-name limiter-restored \
  --restore-date-time "2024-01-15T10:00:00Z"
```

**3. Run rollback (if reversible):**
```python
from zae_limiter.migrations import get_migrations
from zae_limiter.repository import Repository

async def emergency_rollback():
    repo = await Repository.open(stack="limiter", region="us-east-1")

    migrations = get_migrations()
    target_migration = next(m for m in migrations if m.version == "1.1.0")

    if target_migration.reversible and target_migration.rollback:
        await target_migration.rollback(repo)
        print("Rollback complete")
    else:
        print("Migration is not reversible - restore from backup")
```

**4. Update version record:**
```{.python .lint-only}
await repo.set_version_record(
    schema_version="1.0.0",  # Reverted version
    lambda_version="1.0.0",
    updated_by="emergency_rollback",
)
```

## Sample Migration: v2.0.0

This example demonstrates a complete migration scenario for a hypothetical v2.0.0 release that adds a new Global Secondary Index for querying entities by creation date.

### Scenario

**Goal**: Add ability to query entities by creation timestamp for audit purposes.

**Changes**:
1. Add `created_at` attribute to entity metadata
2. Add GSI3 for querying by creation date
3. Backfill `created_at` for existing entities

### Migration Implementation

```{.python .lint-only}
# src/zae_limiter/migrations/v2_0_0.py
"""
Migration: v2.0.0 (Add creation timestamp tracking)

This migration adds a created_at timestamp to all entities and
creates a new GSI for querying entities by creation date.

Schema changes:
- Add 'created_at' attribute to entity #META records
- Add GSI3: GSI3PK=CREATED#{YYYY-MM}, GSI3SK=ENTITY#{id}

Breaking changes:
- Requires CloudFormation stack update for GSI3
- All queries using GSI3 require v2.0.0+ client

Rollback:
- This migration is NOT reversible (GSI removal loses query capability)
- Restore from backup if rollback needed
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from . import Migration, register_migration

if TYPE_CHECKING:
    from ..repository import Repository


async def migrate_v2_0_0(repository: Repository) -> None:
    """
    Add created_at timestamp to all existing entities.

    Note: GSI3 must be added via CloudFormation update before
    running this migration. The migration only backfills data.

    Steps:
    1. Scan all entity metadata records
    2. Add created_at if missing (set to migration timestamp)
    3. Add GSI3 keys for index population
    """
    migration_time = datetime.now(UTC).isoformat()
    migration_month = datetime.now(UTC).strftime("%Y-%m")

    # Get DynamoDB client
    client = await repository._get_client()

    # Scan for all entity metadata records
    paginator = client.get_paginator('scan')

    async for page in paginator.paginate(
        TableName=repository.table_name,
        FilterExpression="begins_with(SK, :meta)",
        ExpressionAttributeValues={":meta": {"S": "#META"}},
    ):
        items = page.get("Items", [])

        # Process in batches of 25 (DynamoDB limit)
        for i in range(0, len(items), 25):
            batch = items[i:i + 25]

            # Build batch update
            update_requests = []
            for item in batch:
                pk = item["PK"]["S"]
                sk = item["SK"]["S"]

                # Use conditional update - only if created_at missing
                update_requests.append({
                    "Update": {
                        "TableName": repository.table_name,
                        "Key": {"PK": {"S": pk}, "SK": {"S": sk}},
                        "UpdateExpression": (
                            "SET #created = if_not_exists(#created, :ts), "
                            "GSI3PK = if_not_exists(GSI3PK, :gsi3pk), "
                            "GSI3SK = if_not_exists(GSI3SK, :gsi3sk)"
                        ),
                        "ExpressionAttributeNames": {
                            "#created": "created_at",
                        },
                        "ExpressionAttributeValues": {
                            ":ts": {"S": migration_time},
                            ":gsi3pk": {"S": f"CREATED#{migration_month}"},
                            ":gsi3sk": {"S": pk},
                        },
                    }
                })

            # Execute batch (transactions limited to 100 items)
            if update_requests:
                await client.transact_write_items(
                    TransactItems=update_requests
                )

        # Rate limiting to avoid throttling
        await asyncio.sleep(0.1)

    # Update version record
    await repository.set_version_record(
        schema_version="2.0.0",
        client_min_version="2.0.0",  # Require v2.0.0+ clients
        updated_by="migration:v2.0.0",
    )


# Register the migration
register_migration(
    Migration(
        version="2.0.0",
        description="Add creation timestamp tracking with GSI3",
        reversible=False,  # GSI changes are not easily reversible
        migrate=migrate_v2_0_0,
        rollback=None,
    )
)
```

### CloudFormation Update

The GSI must be added before running the migration:

```yaml
# Addition to cfn_template.yaml
Resources:
  RateLimitsTable:
    Type: AWS::DynamoDB::Table
    Properties:
      # ... existing properties ...
      GlobalSecondaryIndexes:
        # ... existing GSIs ...
        - IndexName: GSI3
          KeySchema:
            - AttributeName: GSI3PK
              KeyType: HASH
            - AttributeName: GSI3SK
              KeyType: RANGE
          Projection:
            ProjectionType: ALL
      AttributeDefinitions:
        # ... existing attributes ...
        - AttributeName: GSI3PK
          AttributeType: S
        - AttributeName: GSI3SK
          AttributeType: S
```

### Running the Migration

```bash
# 1. Create backup
aws dynamodb create-backup \
  --table-name limiter \
  --backup-name "pre-v2-migration-$(date +%Y%m%d)"

# 2. Update CloudFormation stack (adds GSI3)
aws cloudformation update-stack \
  --stack-name limiter \
  --template-body file://updated-template.yaml \
  --capabilities CAPABILITY_NAMED_IAM

# 3. Wait for GSI to be active
aws dynamodb wait table-exists --table-name limiter

# 4. Install new client version
pip install zae-limiter==2.0.0

# 5. Run migration (via upgrade command or programmatically)
python -c "
import asyncio
from zae_limiter.migrations import apply_migrations
from zae_limiter.repository import Repository

async def run():
    repo = await Repository.open(stack='limiter', region='us-east-1')
    applied = await apply_migrations(repo, '1.0.0', '2.0.0')
    print(f'Applied migrations: {applied}')
    await repo.close()

asyncio.run(run())
"

# 6. Verify migration
zae-limiter version --name limiter --region us-east-1
```

### Testing the Migration

```{.python .lint-only}
@pytest.mark.asyncio
async def test_v2_migration_adds_created_at(mock_dynamodb):
    """Test v2.0.0 migration adds created_at to entities."""
    repo = await Repository.open(stack="test_table")

    # Create entities without created_at (v1 schema)
    await repo.create_entity(entity_id="entity-1", name="Test 1")
    await repo.create_entity(entity_id="entity-2", name="Test 2")

    # Verify no created_at before migration
    entity = await repo.get_entity("entity-1")
    assert entity.created_at is None

    # Apply migration
    applied = await apply_migrations(repo, "1.0.0", "2.0.0")
    assert applied == ["2.0.0"]

    # Verify created_at added - check raw DynamoDB for GSI3 keys
    # (Entity dataclass has created_at, but GSI3PK/GSI3SK are schema-level)
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={"PK": {"S": "{ns}/ENTITY#entity-1"}, "SK": {"S": "#META"}},
    )
    item = response["Item"]
    assert "created_at" in item
    assert item["created_at"]["S"].startswith("20")  # Valid ISO timestamp
    assert "GSI3PK" in item  # GSI key was added
    assert item["GSI3PK"]["S"].startswith("CREATED#")


@pytest.mark.asyncio
async def test_v2_migration_idempotent(mock_dynamodb):
    """Test v2.0.0 migration is idempotent."""
    repo = await Repository.open(stack="test_table")

    # Create entity with created_at already set
    # (simulating an entity created after v2.0.0 code deployed)
    original_time = "2024-01-01T00:00:00Z"
    client = await repo._get_client()
    await client.put_item(
        TableName=repo.table_name,
        Item={
            "PK": {"S": "{ns}/ENTITY#entity-1"},
            "SK": {"S": "#META"},
            "entity_id": {"S": "entity-1"},
            "name": {"S": "Test"},
            "created_at": {"S": original_time},
            "GSI3PK": {"S": "CREATED#2024-01"},
            "GSI3SK": {"S": "{ns}/ENTITY#entity-1"},
        },
    )

    # Apply migration - should not overwrite existing values
    await apply_migrations(repo, "1.0.0", "2.0.0")

    # Verify original created_at preserved (if_not_exists)
    response = await client.get_item(
        TableName=repo.table_name,
        Key={"PK": {"S": "{ns}/ENTITY#entity-1"}, "SK": {"S": "#META"}},
    )
    item = response["Item"]
    assert item["created_at"]["S"] == original_time
```

## Reference

### Version Record Structure

The version record is stored in DynamoDB:

| Attribute | Value | Description |
|-----------|-------|-------------|
| PK | `{ns}/SYSTEM#` | Partition key (namespace-prefixed) |
| SK | `#VERSION` | Sort key |
| schema_version | `"1.0.0"` | Current schema version |
| lambda_version | `"1.2.0"` | Deployed Lambda version |
| client_min_version | `"1.0.0"` | Minimum client version |
| updated_at | ISO timestamp | Last update time |
| updated_by | `"cli:1.2.0"` | What performed the update |

### Key Patterns

| Pattern | Example | Since | Description |
|---------|---------|-------|-------------|
| Entity metadata | `PK={ns}/ENTITY#123, SK=#META` | v0.1.0 | Entity configuration |
| Bucket state (pre-v0.9.0) | `PK={ns}/ENTITY#123, SK=#BUCKET#gpt-4` | v0.1.0 | Token bucket state (composite, one item per resource). Orphaned after v0.9.0 migration |
| Bucket state (v0.9.0+) | `PK={ns}/BUCKET#123#gpt-4#0, SK=#STATE` | v0.9.0 | Token bucket state (per entity/resource/shard partition key) |
| Entity config | `PK={ns}/ENTITY#123, SK=#CONFIG#gpt-4` | v0.5.0 | Stored limit config |
| Usage snapshot | `PK={ns}/ENTITY#123, SK=#USAGE#gpt-4#2024-01-15` | v0.1.0 | Usage data |
| Version | `PK={ns}/SYSTEM#, SK=#VERSION` | v0.1.0 | Infrastructure version |

### Migration API Reference

```python
from zae_limiter.migrations import (
    Migration,           # Migration dataclass
    register_migration,  # Register a migration
    get_migrations,      # Get all registered migrations
    get_migrations_between,  # Get migrations between versions
    apply_migrations,    # Apply migrations
)

from zae_limiter.version import (
    CURRENT_SCHEMA_VERSION,  # Current schema version constant
    parse_version,           # Parse version string
    check_compatibility,     # Check client/infra compatibility
    get_schema_version,      # Get current schema version
    InfrastructureVersion,   # Version info dataclass
    CompatibilityResult,     # Compatibility check result
)
```

### CLI Commands

```bash
# Check compatibility
zae-limiter check --name NAME --region REGION

# Show version information
zae-limiter version --name NAME --region REGION

# Upgrade infrastructure
zae-limiter upgrade --name NAME --region REGION [--lambda-only] [--force]
```

## Version-Specific Migration Guides

- [Namespace-Prefixed Keys (v0.10.0)](migrations/namespace-keys.md) -- Migrate from bare DynamoDB keys to namespace-prefixed keys for multi-tenant isolation
