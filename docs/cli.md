# CLI Reference

## Commands

::: mkdocs-click
    :module: zae_limiter.cli
    :command: cli
    :prog_name: zae-limiter
    :depth: 2
    :style: table
    :list_subcommands: true

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

## Namespace Flag

Most data-access commands accept `--namespace` / `-N` to scope operations to a specific namespace. When omitted, operations default to the `"default"` namespace.

```bash
# Entity operations in a specific namespace
zae-limiter entity set-limits user-123 --namespace tenant-alpha -l rpm:1000

# System defaults for a namespace
zae-limiter system set-defaults --namespace tenant-alpha -l rpm:5000

# Usage and audit scoped to a namespace
zae-limiter usage list --namespace tenant-alpha
zae-limiter audit list --namespace tenant-alpha
```

## Namespace Lifecycle

The `namespace` command group manages the namespace registry:

```bash
# Register namespaces
zae-limiter namespace register tenant-alpha tenant-beta

# List active namespaces
zae-limiter namespace list

# Show namespace details (including opaque ID)
zae-limiter namespace show tenant-alpha

# Soft delete (data preserved, forward lookup removed)
zae-limiter namespace delete tenant-alpha

# Recover a soft-deleted namespace
zae-limiter namespace recover <namespace-id>

# List deleted namespaces (candidates for purge)
zae-limiter namespace orphans

# Hard delete all data in a namespace (irreversible)
zae-limiter namespace purge <namespace-id> --yes
```
