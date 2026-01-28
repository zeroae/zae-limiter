# ADR-113: Lambda Packaging

**Status:** Accepted
**Date:** 2026-01-27
**Issue:** [#154](https://github.com/zeroae/zae-limiter/issues/154)

## Context

The Lambda aggregator previously bundled the entire `zae_limiter` package with all runtime dependencies into the Lambda zip. This resulted in a ~17 MB deployment package containing libraries like `aioboto3`, `aiohttp`, `click`, `pip`, and other packages that the aggregator never uses. The aggregator only needs `boto3` (provided by the Lambda runtime), `aws-lambda-powertools`, and `zae_limiter.schema`.

The project needed a packaging approach that:
- Produces a small Lambda zip (~1-2 MB instead of ~17 MB)
- Only bundles what the aggregator actually uses
- Works cross-platform (macOS/Windows host building for Linux Lambda)
- Does not require Docker
- Is compatible with LocalStack free tier (no Lambda Layers)

## Decision

1. **Separate the aggregator** into its own top-level package `zae_limiter_aggregator` (installed alongside `zae_limiter` from the same wheel).

2. **Minimal Lambda zip** — The Lambda builder copies only:
   - `zae_limiter_aggregator/` (all `.py` files)
   - `zae_limiter/__init__.py` (empty stub — avoids importing `aioboto3`)
   - `zae_limiter/schema.py` (full copy — only imports `typing`, no external deps)
   - `[lambda]` extra dependencies via `aws-lambda-builders` (only `aws-lambda-powertools`)

3. **Empty `__init__.py` stub** — Python's import system executes `zae_limiter/__init__.py` when the aggregator does `from zae_limiter.schema import ...`. The real `__init__.py` imports `aioboto3` which isn't available in Lambda. The empty stub avoids this.

4. **`aws-lambda-builders`** remains a base dependency for cross-platform pip installs of the `[lambda]` extra.

## Consequences

**Positive:**
- Lambda zip size reduced from ~17 MB to ~1-2 MB (aws-lambda-powertools + source files only)
- No `aioboto3`, `aiohttp`, `botocore`, `click`, `pip` in the Lambda zip
- `boto3` provided by Lambda runtime — no need to bundle
- Cross-platform builds work without Docker
- Compatible with LocalStack free tier (no Lambda Layers required)
- Dev/unreleased versions work because local packages are copied, not downloaded from PyPI
- Clean separation of concerns: aggregator is independently importable

**Negative:**
- Two packages to manage in the wheel (`zae_limiter` + `zae_limiter_aggregator`)
- Empty `__init__.py` stub is a packaging workaround (not a true standalone package)
- `aws-lambda-builders` and `pip` remain as base dependencies

## Alternatives Considered

### Lambda Layers
Rejected because: LocalStack free tier does not support Lambda Layers, breaking local development.

### Separate PyPI package (zae-limiter-lambda)
Rejected because: Requires shipping a zip file artifact and keeping two packages in sync, adding maintenance burden.

### Docker-based builds (SAM/CDK)
Rejected because: Requires Docker installed on the build machine, adding a heavy dependency for what should be a lightweight CLI operation.

### pip --platform with --only-binary
Rejected because: Only works for packages with pre-built wheels; fails for packages requiring compilation, and aws-lambda-builders already wraps this approach with better error handling.

### Bundle full zae_limiter with all deps
Rejected because: Produces ~17 MB zip with unnecessary packages (aioboto3, aiohttp, click, pip) that the aggregator never imports.
