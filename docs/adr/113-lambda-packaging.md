# ADR-113: Lambda Packaging

**Status:** Proposed
**Date:** 2026-01-27
**Issue:** [#154](https://github.com/zeroae/zae-limiter/issues/154)

## Context

The Lambda aggregator previously bundled only the `zae_limiter` Python source files without runtime dependencies. The Lambda runtime provides `boto3` but not `aioboto3` or other libraries required by the package's public API. To avoid import failures when Lambda loaded the `zae_limiter` package, `__init__.py` used a PEP 562 `__getattr__` hack to lazily import modules that depend on `aioboto3`. This workaround was fragile, hard to maintain, and prevented static analysis tools from resolving imports.

The project needed a packaging approach that bundles all runtime dependencies into the Lambda zip, works cross-platform (macOS/Windows host building for Linux Lambda), does not require Docker, and is compatible with LocalStack free tier (which does not support Lambda Layers).

## Decision

Add `aws-lambda-builders` and `pip` as base dependencies to install cross-platform runtime dependencies into the Lambda zip, then copy the locally installed `zae_limiter` package on top. This eliminates the `__getattr__` lazy-import hack entirely.

## Consequences

**Positive:**
- All runtime dependencies (aioboto3, boto3, aws-lambda-powertools) are bundled in the Lambda zip
- The `__getattr__` hack is removed — all imports in `__init__.py` are direct
- Cross-platform builds work without Docker (aws-lambda-builders handles platform-specific wheels)
- Dev/unreleased versions work because the local package is copied, not downloaded from PyPI
- Compatible with LocalStack free tier (no Lambda Layers required)

**Negative:**
- Lambda zip size increases from ~30KB to several MB due to bundled dependencies
- `aws-lambda-builders` and `pip` are added as base dependencies, increasing install footprint
- Build step is slower due to pip dependency resolution

## Alternatives Considered

### Lambda Layers
Rejected because: LocalStack free tier does not support Lambda Layers, breaking local development.

### Separate PyPI package (zae-limiter-lambda)
Rejected because: Requires shipping a zip file artifact and keeping two packages in sync, adding maintenance burden.

### Docker-based builds (SAM/CDK)
Rejected because: Requires Docker installed on the build machine, adding a heavy dependency for what should be a lightweight CLI operation.

### pip --platform with --only-binary
Rejected because: Only works for packages with pre-built wheels; fails for packages requiring compilation, and aws-lambda-builders already wraps this approach with better error handling.
