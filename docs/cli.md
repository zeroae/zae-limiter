# CLI Reference

zae-limiter provides a command-line interface for managing infrastructure and deployments.

## Installation

The CLI is included with the package:

```bash
pip install zae-limiter
# or
conda install -c conda-forge zae-limiter
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
| `--name` | Resource identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |
| `--snapshot-windows` | Comma-separated snapshot windows | `hourly,daily` |
| `--retention-days` | Usage snapshot retention (days) | `90` |
| `--enable-aggregator/--no-aggregator` | Deploy Lambda aggregator | `true` |
| `--pitr-recovery-days` | Point-in-time recovery (1-35 days) | None (disabled) |
| `--log-retention-days` | CloudWatch log retention (days) | `30` |
| `--lambda-timeout` | Lambda timeout (1-900 seconds) | `60` |
| `--lambda-memory` | Lambda memory (128-3008 MB) | `256` |
| `--enable-alarms/--no-alarms` | Deploy CloudWatch alarms | `true` |
| `--alarm-sns-topic` | SNS topic ARN for notifications | None |
| `--permission-boundary` | IAM permission boundary | None |
| `--role-name-format` | Lambda role name format | None |
| `--enable-audit-archival/--no-audit-archival` | Archive expired audit events to S3 | `true` |
| `--audit-archive-glacier-days` | Days before Glacier IR transition (1-3650) | `90` |
| `--enable-tracing/--no-tracing` | Enable AWS X-Ray tracing | `false` |
| `--enable-iam-roles/--no-iam-roles` | Create App/Admin/ReadOnly IAM roles | `true` |
| `--tag`, `-t` | User-defined tag in KEY=VALUE format (repeatable) | None |
| `--lambda-duration-threshold-pct` | Lambda duration alarm threshold (1-100%) | `80` |
| `--wait/--no-wait` | Wait for stack creation | `true` |

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

# With custom audit archival settings
zae-limiter deploy \
    --name limiter \
    --region us-east-1 \
    --audit-archive-glacier-days 180

# Disable audit archival
zae-limiter deploy \
    --name limiter \
    --region us-east-1 \
    --no-audit-archival

# With X-Ray tracing enabled
zae-limiter deploy \
    --name limiter \
    --region us-east-1 \
    --enable-tracing
```

---

### list

List all deployed rate limiter instances in a region.

```bash
zae-limiter list [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |

**Examples:**

```bash
# List all limiters in us-east-1
zae-limiter list --region us-east-1

# List limiters using default region
zae-limiter list

# List limiters in LocalStack
zae-limiter list --endpoint-url http://localhost:4566 --region us-east-1
```

**Output:**

```
Rate Limiter Instances (us-east-1)

Name                 Status                    Version      Created
--------------------------------------------------------------------
prod-api             CREATE_COMPLETE           0.2.0        2024-01-15
staging              CREATE_COMPLETE           0.2.0        2024-01-10
dev-test             UPDATE_IN_PROGRESS        0.1.0        2023-12-01

Total: 3 instance(s)
  1 failed
  1 in progress
```

The output includes:

- **Name**: User-friendly name
- **Status**: CloudFormation stack status
- **Version**: Client version at deployment (from stack tag)
- **Created**: Stack creation date

---

### status

Check the status of a deployed CloudFormation stack.

```bash
zae-limiter status [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name`, `-n` | Resource identifier | Required |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint | None |

**Example:**

```bash
zae-limiter status --name limiter --region us-east-1
```

**Output:**

```
Status: limiter
==================================================

Connectivity
  Available:     ✓ Yes
  Latency:       42ms
  Region:        us-east-1

Infrastructure
  Stack:         CREATE_COMPLETE
  Table:         ACTIVE
  Aggregator:    Enabled

Versions
  Client:        0.2.0
  Schema:        1.0.0
  Lambda:        0.2.0

Table Metrics
  Items:         1,234
  Size:          128.5 KB

✓ Infrastructure is ready
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
| `--name`, `-n` | Resource identifier | Required |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint | None |
| `--wait/--no-wait` | Wait for stack deletion | `true` |
| `--yes`, `-y` | Skip confirmation prompt | `false` |

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
| `--output`, `-o` | Output file path | stdout |

**Examples:**

```bash
# Export template to stdout
zae-limiter cfn-template > template.yaml

# Export template to file
zae-limiter cfn-template --output template.yaml

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
| `--output`, `-o` | Output file path | `lambda.zip` |
| `--info` | Show package info without building | `false` |
| `--force`, `-f` | Overwrite existing file | `false` |

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
  Handler: zae_limiter_aggregator.handler.handler
  Runtime: python3.12
  Estimated size: ~30KB
  Dependencies: boto3 (provided by Lambda runtime)
```

---

### version

Show infrastructure version information for a deployed stack.

```bash
zae-limiter version [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name` | Resource identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint | None |

**Example:**

```bash
zae-limiter version --name limiter --region us-east-1
```

**Output:**

```
zae-limiter Infrastructure Version
====================================

Client Version:     0.1.0
Schema Version:     1.0.0

Infra Schema:       1.0.0
Lambda Version:     0.1.0
Min Client Version: 0.0.0

Status: COMPATIBLE
```

---

### check

Check infrastructure compatibility without modifying.

```bash
zae-limiter check [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name` | Resource identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint | None |

**Example:**

```bash
zae-limiter check --name limiter --region us-east-1
```

**Output:**

```
Compatibility Check
====================

Client:      0.1.0
Schema:      1.0.0
Lambda:      0.1.0

Result: COMPATIBLE

Client and infrastructure are fully compatible.
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
| `--name` | Resource identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint | None |
| `--lambda-only` | Only update Lambda code | `false` |
| `--force` | Force update even if version matches | `false` |

**Example:**

```bash
# Upgrade infrastructure
zae-limiter upgrade --name limiter --region us-east-1

# Force Lambda update only
zae-limiter upgrade --name limiter --region us-east-1 --lambda-only --force
```

---

## audit list

List audit events for an entity.

```bash
zae-limiter audit list [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name`, `-n` | Resource identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint | None |
| `--entity-id`, `-e` | Entity ID to query (required) | - |
| `--limit`, `-l` | Maximum events to return | `100` |
| `--start-event-id` | Event ID for pagination | None |

**Examples:**

```bash
# List audit events for an entity
zae-limiter audit list --name limiter --entity-id proj-1

# Limit results
zae-limiter audit list --entity-id proj-1 --limit 10

# Paginate through results
zae-limiter audit list --entity-id proj-1 --start-event-id 01HXYZ...
```

**Output:**

```
Audit Events for: proj-1
================================================================================

Timestamp                Action             Principal            Resource
--------------------------------------------------------------------------------
2024-01-15T10:30:00Z     limits_set         admin@example.com    gpt-4
2024-01-15T10:00:00Z     entity_created     admin@example.com    -

Total: 2 events
```

---

## usage list

List usage snapshots for historical consumption data.

Usage snapshots are created by the Lambda aggregator from DynamoDB stream events.
They track token consumption per entity/resource within time windows (hourly, daily).

```bash
zae-limiter usage list [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name`, `-n` | Resource identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint | None |
| `--entity-id`, `-e` | Entity ID to query | - |
| `--resource`, `-r` | Resource name filter | - |
| `--window`, `-w` | Window type (`hourly`, `daily`) | None |
| `--start` | Start time (ISO format) | None |
| `--end` | End time (ISO format) | None |
| `--limit`, `-l` | Maximum snapshots to return | `100` |
| `--plot`, `-p` | Display as ASCII charts instead of table | `false` |

!!! note
    Either `--entity-id` or `--resource` must be provided.

!!! tip "ASCII Charts"
    The `--plot` flag requires the optional `plot` extra. Install with:
    ```bash
    pip install 'zae-limiter[plot]'
    ```

**Examples:**

```bash
# List all snapshots for an entity
zae-limiter usage list --entity-id user-123

# Filter by resource and window type
zae-limiter usage list --entity-id user-123 --resource gpt-4 --window hourly

# Query by resource across all entities
zae-limiter usage list --resource gpt-4 --window daily

# Filter by time range
zae-limiter usage list --entity-id user-123 \
    --start 2024-01-01T00:00:00Z \
    --end 2024-01-31T23:59:59Z

# Limit results
zae-limiter usage list --entity-id user-123 --limit 10

# Display as ASCII chart (requires: pip install 'zae-limiter[plot]')
zae-limiter usage list --entity-id user-123 --plot
```

**Table Output (default):**

```
Usage Snapshots
====================================================================================================

Window Start           Type     Resource         Entity               Events Counters
----------------------------------------------------------------------------------------------------
2024-01-15T14:00:00Z   hourly   gpt-4            user-123                 25 rpm=25, tpm=12,500
2024-01-15T13:00:00Z   hourly   gpt-4            user-123                 18 rpm=18, tpm=9,000
2024-01-15T12:00:00Z   hourly   gpt-4            user-123                 32 rpm=32, tpm=16,000

Total: 3 snapshots
```

**Plot Output (`--plot` flag):**

```
Usage Plot: gpt-4 (hourly)
Entity: user-123
================================================================================

RPM                             TPM
----------------------------    -------------------------------
32  ┤      ╭                    16,000  ┤      ╭
28  ┤     ╭╯                    14,000  ┤     ╭╯
25  ┤    ╭╯                     12,500  ┤    ╭╯
21  ┤   ╭╯                      11,000  ┤   ╭╯
18  ┼───╯                        9,000  ┼───╯

Time range: 2024-01-15T12:00:00Z to 2024-01-15T14:00:00Z
Data points: 3

Total: 3 snapshots
```

The plot shows counters side-by-side (2 per row) with:

- **Header**: Resource name, window type, and entity ID
- **Y-axis**: Right-aligned labels with thousands separators
- **Downsampling**: Large datasets (>60 points) are automatically downsampled with a note

---

## usage summary

Show aggregated usage summary across multiple snapshots.

Computes total and average consumption statistics over matching snapshots.
Useful for billing, reporting, and capacity planning.

```bash
zae-limiter usage summary [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name`, `-n` | Resource identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint | None |
| `--entity-id`, `-e` | Entity ID to query | - |
| `--resource`, `-r` | Resource name filter | - |
| `--window`, `-w` | Window type (`hourly`, `daily`) | None |
| `--start` | Start time (ISO format) | None |
| `--end` | End time (ISO format) | None |

!!! note
    Either `--entity-id` or `--resource` must be provided.

**Examples:**

```bash
# Get summary for an entity
zae-limiter usage summary --entity-id user-123

# Summary for a specific resource
zae-limiter usage summary --entity-id user-123 --resource gpt-4

# Summary for a time range
zae-limiter usage summary --entity-id user-123 \
    --resource gpt-4 \
    --window hourly \
    --start 2024-01-01T00:00:00Z \
    --end 2024-01-31T23:59:59Z
```

**Output:**

```
Usage Summary
============================================================

Entity:     user-123
Resource:   gpt-4
Window:     hourly
Snapshots:  720
Time Range: 2024-01-01T00:00:00Z to 2024-01-31T23:00:00Z

Limit                   Total         Average
------------------------------------------------------------
rpm                     18,000           25.00
tpm                  9,000,000       12,500.00
```

---

## resource

Manage resource-level limit configurations. These limits apply to a specific resource and override system-level defaults.

### resource set-defaults

Set default limits for a resource.

```bash
zae-limiter resource set-defaults <RESOURCE_NAME> [OPTIONS]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `RESOURCE_NAME` | The resource to configure (e.g., 'gpt-4', 'claude-3') |

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name`, `-n` | Stack identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |
| `--limit`, `-l` | Limit specification (required, repeatable) | - |

**Limit format:** `name:capacity[:burst]`

- `name` - Limit name (e.g., `tpm`, `rpm`)
- `capacity` - Maximum tokens per period
- `burst` - Optional burst capacity (defaults to capacity)

**Examples:**

```bash
# Set TPM and RPM defaults for gpt-4
zae-limiter resource set-defaults gpt-4 -l tpm:100000 -l rpm:1000

# Set defaults with burst capacity
zae-limiter resource set-defaults claude-3 -l tpm:50000:75000 -l rpm:500:750

# Use a specific limiter instance
zae-limiter resource set-defaults gpt-4 --name prod -l tpm:100000
```

---

### resource get-defaults

Get default limits for a resource.

```bash
zae-limiter resource get-defaults <RESOURCE_NAME> [OPTIONS]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `RESOURCE_NAME` | The resource to query (e.g., 'gpt-4', 'claude-3') |

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name`, `-n` | Stack identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |

**Examples:**

```bash
# Get defaults for gpt-4
zae-limiter resource get-defaults gpt-4

# Query from a specific instance
zae-limiter resource get-defaults gpt-4 --name prod --region us-west-2
```

**Output:**

```
Defaults for resource 'gpt-4':
  tpm: 100,000/min (burst: 100,000)
  rpm: 1,000/min (burst: 1,000)
```

---

### resource delete-defaults

Delete default limits for a resource.

```bash
zae-limiter resource delete-defaults <RESOURCE_NAME> [OPTIONS]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `RESOURCE_NAME` | The resource to delete defaults from |

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name`, `-n` | Stack identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |
| `--yes`, `-y` | Skip confirmation prompt | `false` |

**Examples:**

```bash
# Delete with confirmation
zae-limiter resource delete-defaults gpt-4

# Skip confirmation
zae-limiter resource delete-defaults gpt-4 --yes
```

---

### resource list

List all resources with configured defaults.

```bash
zae-limiter resource list [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name`, `-n` | Stack identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |

**Examples:**

```bash
# List all resources with defaults
zae-limiter resource list

# List from a specific instance
zae-limiter resource list --name prod
```

**Output:**

```
Resources with configured defaults:
  claude-3
  gpt-4
  gpt-4-turbo
```

---

## system

Manage system-wide default limit configurations. These are global defaults that apply to **all resources** unless overridden at the resource or entity level.

### system set-defaults

Set system-wide default limits (applies to all resources).

```bash
zae-limiter system set-defaults [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name`, `-n` | Stack identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |
| `--limit`, `-l` | Limit specification (required, repeatable) | - |
| `--on-unavailable` | Behavior when DynamoDB unavailable (`allow` or `block`) | None |

**Limit format:** `name:capacity[:burst]`

**Examples:**

```bash
# Set system-wide defaults (applies to ALL resources)
zae-limiter system set-defaults -l tpm:10000 -l rpm:100

# Set defaults with on_unavailable behavior
zae-limiter system set-defaults -l tpm:10000 -l rpm:100 --on-unavailable allow

# Set defaults with burst
zae-limiter system set-defaults -l tpm:5000:7500 -l rpm:50:75
```

---

### system get-defaults

Get system-wide default limits.

```bash
zae-limiter system get-defaults [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name`, `-n` | Stack identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |

**Examples:**

```bash
# Get system-wide defaults
zae-limiter system get-defaults
```

**Output:**

```
System-wide defaults:
  tpm: 10,000/min (burst: 10,000)
  rpm: 100/min (burst: 100)
  on_unavailable: allow
```

---

### system delete-defaults

Delete system-wide default limits.

```bash
zae-limiter system delete-defaults [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name`, `-n` | Stack identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |
| `--yes`, `-y` | Skip confirmation prompt | `false` |

**Examples:**

```bash
# Delete with confirmation
zae-limiter system delete-defaults

# Skip confirmation
zae-limiter system delete-defaults --yes
```

---

## entity

Manage entity-level limit configurations. These limits apply to a specific entity and resource combination, overriding both system and resource defaults.

### entity create

Create a new entity.

```bash
zae-limiter entity create <ENTITY_ID> [OPTIONS]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `ENTITY_ID` | Unique identifier for the entity (e.g., 'user-123', 'api-key-abc') |

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--display-name` | Human-readable name | entity_id |
| `--parent` | Parent entity ID (for hierarchical limits) | None |
| `--cascade/--no-cascade` | Cascade acquire() to parent entity | `false` |
| `--name`, `-n` | Stack identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |

**Examples:**

```bash
# Create a standalone entity
zae-limiter entity create user-123

# Create with a display name
zae-limiter entity create api-key-abc --display-name "Production API Key"

# Create with parent and cascade
zae-limiter entity create user-123 --parent org-456 --cascade
```

---

### entity show

Show details for an entity.

```bash
zae-limiter entity show <ENTITY_ID> [OPTIONS]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `ENTITY_ID` | The entity to query |

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--name`, `-n` | Stack identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |

**Examples:**

```bash
# Show entity details
zae-limiter entity show user-123
```

**Output:**

```
Entity: user-123
  Name:       Production User
  Parent:     org-456
  Cascade:    True
  Created:    2024-01-15T10:00:00Z
```

---

### entity set-limits

Set limits for a specific entity and resource.

```bash
zae-limiter entity set-limits <ENTITY_ID> [OPTIONS]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `ENTITY_ID` | The entity to configure (e.g., 'user-123', 'api-key-abc') |

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--resource`, `-r` | Resource name (required) | - |
| `--name`, `-n` | Stack identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |
| `--limit`, `-l` | Limit specification (required, repeatable) | - |

**Limit format:** `name:capacity[:burst]`

**Examples:**

```bash
# Set premium user limits for gpt-4
zae-limiter entity set-limits user-premium --resource gpt-4 -l tpm:100000 -l rpm:1000

# Set API key limits with burst
zae-limiter entity set-limits api-key-123 --resource claude-3 -l tpm:50000:75000
```

---

### entity get-limits

Get limits for a specific entity and resource.

```bash
zae-limiter entity get-limits <ENTITY_ID> [OPTIONS]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `ENTITY_ID` | The entity to query |

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--resource`, `-r` | Resource name (required) | - |
| `--name`, `-n` | Stack identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |

**Examples:**

```bash
# Get entity limits for gpt-4
zae-limiter entity get-limits user-premium --resource gpt-4
```

**Output:**

```
Limits for entity 'user-premium' on resource 'gpt-4':
  tpm: 100,000/min (burst: 100,000)
  rpm: 1,000/min (burst: 1,000)
```

---

### entity delete-limits

Delete limits for a specific entity and resource.

```bash
zae-limiter entity delete-limits <ENTITY_ID> [OPTIONS]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `ENTITY_ID` | The entity to delete limits from |

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--resource`, `-r` | Resource name (required) | - |
| `--name`, `-n` | Stack identifier | `limiter` |
| `--region` | AWS region | boto3 default |
| `--endpoint-url` | Custom AWS endpoint (LocalStack) | None |
| `--yes`, `-y` | Skip confirmation prompt | `false` |

**Examples:**

```bash
# Delete with confirmation
zae-limiter entity delete-limits user-premium --resource gpt-4

# Skip confirmation
zae-limiter entity delete-limits user-premium --resource gpt-4 --yes
```

---

## local

Manage LocalStack for local development. Requires Docker.

### local up

Start a LocalStack container for local development.

```bash
zae-limiter local up [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--docker-host` | Docker daemon URL | `DOCKER_HOST` env var |
| `--image` | LocalStack Docker image | `localstack/localstack:4` |
| `--name`, `-n` | Stack name for deploy instructions | None |
| `--port` | Host port to bind | `4566` |

**Examples:**

```bash
# Start LocalStack
zae-limiter local up

# Start and show deploy instructions for a named stack
zae-limiter local up --name my-app
```

---

### local down

Stop and remove the LocalStack container.

```bash
zae-limiter local down [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--docker-host` | Docker daemon URL | `DOCKER_HOST` env var |

---

### local status

Show LocalStack container status, endpoint, health, and image.

```bash
zae-limiter local status [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--docker-host` | Docker daemon URL | `DOCKER_HOST` env var |

**Output (when running):**

```
LocalStack: running
Endpoint:   http://localhost:4566
Health:     healthy
Image:      localstack/localstack:4
Services:   dynamodb,dynamodbstreams,lambda,cloudformation,logs,iam,cloudwatch,sqs,s3,sts,resourcegroupstaggingapi

To use with zae-limiter:
  export AWS_ENDPOINT_URL=http://localhost:4566
  export AWS_ACCESS_KEY_ID=test
  export AWS_SECRET_ACCESS_KEY=test
  export AWS_DEFAULT_REGION=us-east-1
```

---

### local logs

Show LocalStack container logs.

```bash
zae-limiter local logs [OPTIONS]
```

**Options:**

| Option | Description | Default |
|--------|-------------|---------|
| `--docker-host` | Docker daemon URL | `DOCKER_HOST` env var |
| `--follow`, `-f` | Follow log output | `false` |
| `--tail` | Number of lines from end of logs | `100` |

**Examples:**

```bash
# Show recent logs
zae-limiter local logs

# Follow logs
zae-limiter local logs --follow

# Show last 50 lines
zae-limiter local logs --tail 50
```

---

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
- [LocalStack](contributing/localstack.md) - Local development
- [API Reference](api/index.md) - Python API documentation
