# Design: Replace aioboto3 with aiobotocore

**Date:** 2026-06-22
**Status:** Approved (design)
**Topic:** Drop the unmaintained `aioboto3` dependency in favor of calling `aiobotocore` directly.

## Problem & Rationale

`aioboto3` is effectively unmaintained. It is a thin wrapper over `aiobotocore`
(the actively-maintained async layer that wraps `botocore`); its main value-add
over `aiobotocore` is the high-level **resource** API (`session.resource(...)`,
`Table` objects with automatic type marshalling).

The zae-limiter async data path uses **only the low-level client API**
(`session.client("dynamodb")` with explicitly-typed attribute values such as
`{"S": ...}` / `{"N": ...}`). It never uses the resource API. Therefore dropping
`aioboto3` and creating clients directly from an `aiobotocore` session costs us
nothing functionally, removes an unmaintained dependency, and brings us one layer
closer to `botocore`.

## Scope

### In scope — the only modules importing `aioboto3`

| File | AWS clients created |
|------|---------------------|
| `src/zae_limiter/repository.py` | `dynamodb`, `sts` |
| `src/zae_limiter/infra/discovery.py` | `cloudformation`, `resourcegroupstaggingapi` |
| `src/zae_limiter/infra/stack_manager.py` | `cloudformation`, `lambda` |
| `scripts/generate_sync.py` | AST transformer (async → sync codegen) |
| `pyproject.toml` | runtime deps + mypy overrides |
| Tests patching `aioboto3.Session` | `tests/unit/test_limiter.py`, `tests/unit/test_stack_manager.py`, and generated `tests/unit/test_sync_*.py` (~40 patch sites) |

### Out of scope — already pure `boto3` (sync)

- `src/zae_limiter_aggregator/` (Lambda aggregator)
- `src/zae_limiter_provisioner/` (Lambda provisioner)
- `src/zae_limiter/loadtest/`
- `src/zae_limiter/limits_cli.py`
- All generated `sync_*.py` files (regenerated from async source, never hand-edited)

The aggregator's `boto3.resource("dynamodb")` usage is sync-only and out of scope.

## Approach Decision

**Chosen: direct call-site replacement** (not an internal factory/abstraction
module). The code already uses the low-level client uniformly, so the change is
faithful to "use aiobotocore directly" and keeps the diff minimal. The cost —
adapting the sync-gen transformer and the ~40 test patch targets to the new call
shape — is accepted.

**Process decision:** open a tracking GitHub issue (via `/issue`); **no ADR**
(treated as a maintenance/dependency change). Implement on branch
`refactor/aiobotocore-migration`; open a draft PR (via `/pr`) referencing the issue.

## Detailed Design

### 1. Call-site change (async source)

Two substitutions; the `await`, `.__aenter__()`, `async with`, and `__aexit__`
lifecycle are unchanged because `aioboto3` delegates the client path straight to
`aiobotocore`:

```python
# before
import aioboto3
self._session = aioboto3.Session()
self._client = await self._session.client(
    "dynamodb", region_name=..., endpoint_url=...
).__aenter__()

# after
from aiobotocore.session import get_session
self._session = get_session()
self._client = await self._session.create_client(
    "dynamodb", region_name=..., endpoint_url=...
).__aenter__()
```

- `close()` (`await self._client.__aexit__(None, None, None)`) needs **no change**:
  the object returned by `__aenter__()` supports `__aexit__` in both libraries.
- `async with self._session.client(...) as c:` becomes
  `async with self._session.create_client(...) as c:`.
- Credential/region/endpoint resolution is identical (same `botocore` underneath).
  `get_session()` takes no args; all config flows through `create_client(...)`
  exactly as it flowed through `aioboto3`'s `.client(...)` today.

### 2. Sync-generator transformer (`scripts/generate_sync.py`) — highest risk

The transformer currently rewrites `aioboto3 → boto3` and unwraps
`async with session.client(...)`. Direct replacement requires three new/updated
rules so generated sync remains idiomatic `boto3`:

| Async (aiobotocore) | Generated sync (boto3) | Transformer rule |
|---|---|---|
| `from aiobotocore.session import get_session` | `import boto3` | import rewrite (handle the `from … import` form) |
| `get_session()` | `boto3.Session()` | call rewrite (Name `get_session()` → `boto3.Session()`) |
| `.create_client(...)` | `.client(...)` | attribute rename `create_client → client`, applied **before** the existing `.client` context-manager unwrap so that logic continues to fire unchanged |

The `IMPORT_MODULE_REWRITES = {"aioboto3": "boto3"}` entry is replaced by the
above rules.

**Verification of the transformer:** regenerate the full sync tree and diff
against the current generated files. The generated `sync_repository.py`,
`sync_discovery.py`, `sync_stack_manager.py`, etc. must come out **functionally
identical to today** — only the async source changed, so the sync output should
be unchanged modulo the session-construction lines (`boto3.Session()` /
`.client(...)`), which must match the current output exactly. This is the primary
checkpoint.

### 3. Dependencies & typing (`pyproject.toml`)

- Runtime: replace `aioboto3>=12.0.0` with `aiobotocore` at a floor whose
  pinned `botocore` range is compatible with the existing `boto3>=1.34.0`
  (e.g. `aiobotocore>=2.13.0`); the exact floor is confirmed at implementation
  time by resolving the env and checking the installed `botocore` matches.
  Keep `boto3>=1.34.0` (used by sync code, aggregator, provisioner, loadtest).
- Typing: `types-aiobotocore[dynamodb,s3]` is **already** a dev dependency — no
  change needed there.
- mypy overrides: replace the `aioboto3` / `aioboto3.*` `ignore_missing_imports`
  entries. First attempt: remove them entirely (since `types-aiobotocore` is
  installed and ships types). If the base `aiobotocore` / `aiobotocore.session`
  import still errors under mypy, add `aiobotocore` / `aiobotocore.*` overrides
  instead.

### 4. Tests

- Replace `patch("….aioboto3.Session")` with a patch of the new seam in each
  module — e.g. `patch("zae_limiter.repository.get_session")`,
  `patch("zae_limiter.infra.stack_manager.get_session")` — returning a mock
  session whose `.create_client(...)` returns the mock client (mirroring how the
  current mocks wire `.client(...)`).
- The Lambda-packaging tests that assert `aioboto3/` is absent from the built zip
  (`tests/integration/test_lambda_builder.py`, `tests/unit/test_lambda_builder.py`)
  update to assert `aiobotocore/` is absent (the aggregator zip should contain
  neither).
- Generated `tests/unit/test_sync_*.py` are **regenerated**, not hand-edited; the
  async test sources (`test_limiter.py`, `test_stack_manager.py`) are the source
  of truth and must be updated so their sync counterparts regenerate correctly.

## Verification Gates (run in order)

1. `hatch run generate-sync` — clean regeneration, **no drift** vs committed sync files (beyond intended session-line changes).
2. `pre-commit run --all-files` — includes the "generated code up-to-date" hook, ruff, and cfn-lint.
3. `uv run mypy src/zae_limiter` — type check passes.
4. `uv run pytest tests/unit/` — unit suite (incl. regenerated `test_sync_*`).
5. Integration / e2e against LocalStack (`zae-limiter local up`; `pytest -m integration`, `-m e2e`).

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Transformer produces non-idiomatic or broken sync code | Diff regenerated sync against current output; treat any non-session-line diff as a bug. Checkpoint before proceeding. |
| Hidden resource-API usage | Confirmed via grep: only `session.client(...)` is used in async code; no `session.resource(...)` / `Table(...)`. |
| Subtle credential/endpoint behavior change | Same `botocore` resolution path; LocalStack e2e exercises `endpoint_url`, real-AWS e2e exercises default credential chain. |
| mypy regressions from stub differences | `types-aiobotocore` already installed; gate #3 catches issues. |

## Out of Scope / Non-Goals

- No migration to the resource API or any change to the DynamoDB
  serialization/marshalling code.
- No changes to sync `boto3` usage in the aggregator, provisioner, loadtest, or CLI.
- No ADR (per process decision above).
