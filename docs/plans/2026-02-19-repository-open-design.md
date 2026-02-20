# Repository.open() Unified Entry Point Design

**Date:** 2026-02-19
**Issue:** #404
**Status:** Approved

## Problem

The Repository API has three competing entry points (`connect()`, `builder().build()`, deprecated constructor) creating onboarding friction. Namespace — the most important parameter for most users — is buried as a keyword arg. Users must know whether infrastructure exists before choosing a path.

## Design Decisions

### 1. `Repository.open()` — the one path

Namespace is the **primary positional argument**. Stack name defaults to `"zae-limiter"` and is rarely needed.

```python
@classmethod
async def open(
    cls,
    namespace: str = "default",
    *,
    stack: str | None = None,
    region: str | None = None,
    endpoint_url: str | None = None,
    config_cache_ttl: int = 60,
    auto_update: bool = True,
) -> "Repository":
```

**Stack resolution:** `stack` arg → `ZAE_LIMITER_STACK` env var → `"zae-limiter"`

**Usage:**

```python
# Most users
repo = await Repository.open("my-app")

# Multi-tenant
repo_alpha = await Repository.open("tenant-alpha")
repo_beta = await Repository.open("tenant-beta")

# Explicit stack
repo = await Repository.open("my-app", stack="custom-stack", region="eu-west-1")

# Absolute simplest (stack="zae-limiter", namespace="default")
repo = await Repository.open()

# LocalStack
repo = await Repository.open("my-app", endpoint_url="http://localhost:4566")
```

**Auto-provision behavior (always, no flag):**

```
open("my-app")
  │
  ├─ Try resolve namespace "my-app"
  │   ├─ Found → continue to version check
  │   │
  │   ├─ Table doesn't exist (InfrastructureNotFoundError)
  │   │   ├─ Deploy stack with defaults (aggregator enabled)
  │   │   ├─ Register "my-app" namespace
  │   │   └─ Continue to version check
  │   │
  │   └─ Table exists but namespace not found
  │       ├─ Register "my-app" namespace
  │       └─ Continue to version check
  │
  └─ Version check + Lambda auto-update (always)
```

Auto-provision uses default StackOptions. Users needing custom IAM, Lambda config, or permission boundaries use `builder()`.

### 2. Fully fluent `builder()` — zero args

For power users who need custom infrastructure options:

```python
# Minimal
repo = await Repository.builder().namespace("my-app").build()

# Enterprise
repo = await (
    Repository.builder()
    .stack("custom-stack")
    .region("us-east-1")
    .namespace("my-app")
    .permission_boundary("arn:aws:iam::aws:policy/PowerUserAccess")
    .role_name_format("PowerUserPB-{}")
    .policy_name_format("PowerUserPB-{}")
    .lambda_memory(512)
    .build()
)
```

New builder methods: `.stack()`, `.region()`, `.endpoint_url()` (previously positional args).

Defaults mirror `open()`: stack from env var or `"zae-limiter"`, namespace `"default"`.

**`build()` behavior unchanged:**
1. Construct Repository with materialized StackOptions
2. Ensure infrastructure exists
3. Register namespace
4. Resolve namespace
5. Version check + auto-update

### 3. Remove local endpoint special-casing

**Current:** `if not endpoint_url:` guards around version check and Lambda auto-update.

**Fix:** Remove all `endpoint_url` guards. The version record already handles this correctly:
- `lambda_version: None` → no Lambda → `requires_lambda_update = False`
- Version check runs everywhere (AWS, LocalStack, any endpoint)

**Cleanup locations:**

| Location | Current | Fix |
|----------|---------|-----|
| `repository.py:220-221` | `connect()` skips version check for local | Remove `if not endpoint_url` guard |
| `repository.py:1106` | Skips Lambda update for local | Remove `not self.endpoint_url` guard |
| `repository.py:1146` | `can_auto_update=not self.endpoint_url` | Always `can_auto_update=True` |
| `CLAUDE.md:344,390` | "skip for local endpoints" | Remove |
| `CLAUDE.md:375-381` | LocalStack with `.enable_aggregator(False)` | Remove that flag |

### 4. `connect()` removal

`connect()` was added in #381 and has not been released. No deprecation needed — just remove and replace with `open()`.

### 5. Entry points after this change

- `Repository.open()` — the one path (replaces `connect()`)
- `Repository.builder()` — power users needing custom infra options
- `Repository(...)` — deprecated, removed in v1.0.0 (message updated to point to `open()`)

**When to use `open()` vs `builder()`:**
- **`open()`**: 90% of users. Application code, prototyping, LocalStack dev.
- **`builder()`**: Enterprise deployments needing permission boundaries, custom Lambda config, IAM role naming.

### 6. Sync counterparts

All changes are auto-generated via `scripts/generate_sync.py`:
- `SyncRepository.open()` — generated from `Repository.open()`
- `SyncRepository.builder()` — generated from updated `Repository.builder()`

## Files Affected

### Implementation
- `src/zae_limiter/repository.py` — `open()`, remove `connect()`, version check cleanup
- `src/zae_limiter/repository_builder.py` — zero-arg constructor, new `.stack()`, `.region()`, `.endpoint_url()` methods
- `src/zae_limiter/__init__.py` — docstring examples
- `src/zae_limiter/sync_repository.py` — generated
- `src/zae_limiter/sync_repository_builder.py` — generated

### Tests
- `tests/unit/test_repository.py` — `connect()` → `open()` migration
- `tests/unit/test_repository_builder.py` — zero-arg builder, new chain methods
- `tests/unit/test_sync_repository.py` — generated
- New tests for auto-provision, stack name resolution, version check on local endpoints

### Documentation
- `CLAUDE.md` — examples, local endpoint cleanup
- `docs/getting-started.md` — `open()` as primary entry point
- `docs/api/repository.md` — `open()` reference, builder signature
- `docs/api/index.md` — `open()` examples
- `docs/guide/basic-usage.md` — `open()` examples
- `docs/infra/deployment.md` — `open()` vs `builder()` guidance
- `docs/infra/production.md` — multi-tenant examples
- `docs/contributing/localstack.md` — remove aggregator special-casing
- `docs/performance.md` — update if references `connect()` or `builder(name, region)`
