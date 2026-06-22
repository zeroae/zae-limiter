# aioboto3 → aiobotocore Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unmaintained `aioboto3` dependency with direct `aiobotocore` client calls across the async code paths, with no behavioral change.

**Architecture:** The async data/infra paths use only the low-level client API (`session.client("dynamodb")` with typed attribute values). aiobotocore offers the same client API via `get_session().create_client(...)`. The migration is a near-mechanical call-site swap plus three additive rules in the async→sync AST transformer (`scripts/generate_sync.py`) so the generated `boto3` sync code stays identical. Generated sync files are never hand-edited — they are regenerated and the diff is the acceptance test.

**Tech Stack:** Python 3.11/3.12, `aiobotocore`, `botocore`, `boto3` (sync/Lambda), `moto` (unit mocks), LocalStack (integration/e2e), `hatch run generate-sync` (AST codegen), `uv`, `ruff`, `mypy`, `pytest`.

**Reference spec:** `docs/plans/2026-06-22-aiobotocore-migration-design.md`

## Global Constraints

- **Branch:** all work on `refactor/aiobotocore-migration` (already created). No direct commits to `main`.
- **`aiobotocore` version floor:** choose a floor whose pinned `botocore` is compatible with the existing `boto3>=1.34.0`; start with `aiobotocore>=2.13.0` and confirm with `uv pip list` that the resolved `botocore` matches the one `boto3` resolves. Keep `boto3>=1.34.0`.
- **Generated files are never hand-edited.** Files matching `sync_*.py` and `tests/unit/test_sync_*.py` are produced by `hatch run generate-sync`. Edit the async source + the transformer, then regenerate.
- **`generate-sync` must stay idempotent.** After each task that regenerates, running `hatch run generate-sync` twice produces no diff, and the regenerated files are committed.
- **Transformer rules are additive until the final cleanup task** — keep the existing `aioboto3 → boto3` rule in place while both libraries coexist, so regeneration never breaks mid-migration.
- **Production sync output must remain functionally identical.** The only acceptable diff in generated `src/.../sync_*.py` after migration is import-ordering normalization by ruff/isort. The session-construction and client-creation lines must be byte-identical to pre-migration.
- **No lint suppression.** Do not add `# noqa`, `# type: ignore`, or config ignores without explicit approval (`.claude/rules/lint-rules.md`).
- **Commit conventions:** gitmoji + Conventional Commits (`.claude/rules/commits.md`). End every commit body with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Per-commit green gate:** every commit must pass `pre-commit run --all-files` (ruff, ruff-format, mypy on `src/`, cfn-lint, the "Verify generated sync code is up-to-date" hook).

---

## File Structure

**Production source (async — hand-edited):**
- `src/zae_limiter/repository.py` — DynamoDB + STS clients (2 session creations)
- `src/zae_limiter/infra/stack_manager.py` — CloudFormation + Lambda clients (4 session creations)
- `src/zae_limiter/infra/discovery.py` — CloudFormation + resourcegroupstaggingapi (2 session creations)

**Codegen (hand-edited):**
- `scripts/generate_sync.py` — AST transformer; gains 5 additive aiobotocore rules, loses the `aioboto3` rule at cleanup.

**Generated (regenerated, never hand-edited):**
- `src/zae_limiter/sync_repository.py`, `src/zae_limiter/infra/sync_stack_manager.py`, `src/zae_limiter/infra/sync_discovery.py`
- `tests/unit/test_sync_limiter.py`, `tests/unit/test_sync_stack_manager.py`

**Tests (async — hand-edited):**
- `tests/unit/test_stack_manager.py` — 20 `aioboto3` refs, 22 `mock_session.client` sites
- `tests/unit/test_limiter.py` — 3 discovery tests (7 `aioboto3` refs)
- `tests/integration/test_lambda_builder.py`, `tests/unit/test_lambda_builder.py` — zip-content assertions

**Config & cosmetics:**
- `pyproject.toml` — deps + mypy overrides
- `src/zae_limiter/infra/lambda_builder.py`, `src/zae_limiter_provisioner/applier.py`, `tests/fixtures/capacity.py` — comment text only

**No change needed:** `tests/fixtures/moto.py` (already patches `aiobotocore`), `tests/doctest/conftest.py`, `test_repository.py` (moto-based), aggregator/provisioner runtime code (pure sync `boto3`).

---

### Task 1: Add aiobotocore dependency (keep aioboto3)

**Files:**
- Modify: `pyproject.toml` (dependencies list ~line 26; mypy overrides ~line 148-154)

**Interfaces:**
- Produces: `aiobotocore` importable in the env; `types-aiobotocore` already present (dev dep, line 53). `aioboto3` stays installed so the tree remains green.

- [ ] **Step 1: Add aiobotocore alongside aioboto3 in runtime deps**

In `pyproject.toml`, change the `dependencies` block so it reads:

```toml
dependencies = [
    "aioboto3>=12.0.0",
    "aiobotocore>=2.13.0",
    "aws-lambda-builders>=1.40.0",
    "boto3>=1.34.0",
    "click>=8.0.0",
    "pip",
    "python-ulid>=3.0.0",
    "pyyaml>=6.0",
    "questionary>=2.0",
]
```

- [ ] **Step 2: Add aiobotocore to the mypy ignore-missing-imports override (temporarily, alongside aioboto3)**

In the `[[tool.mypy.overrides]]` block, change the `module` list to include both:

```toml
[[tool.mypy.overrides]]
module = [
    "aws_lambda_builders.*",
    "aioboto3",
    "aioboto3.*",
    "aiobotocore",
    "aiobotocore.*",
    "ulid",
    "zae_limiter._version",
]
ignore_missing_imports = true
```

- [ ] **Step 3: Resolve the env and verify import + botocore compatibility**

Run:
```bash
uv sync --all-extras
uv run python -c "import aiobotocore.session; from aiobotocore.session import get_session, AioSession; print('ok', aiobotocore.__version__)"
uv run python -c "import botocore, boto3; print('botocore', botocore.__version__)"
```
Expected: prints `ok <version>` and a `botocore` version (confirm it's the same one both `boto3` and `aiobotocore` resolved — no resolver conflict).

- [ ] **Step 4: Confirm the tree is still green**

Run: `uv run pytest tests/unit/ -q`
Expected: PASS (no source changed yet; both libs available).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "$(cat <<'EOF'
⬆️ deps(repository): add aiobotocore alongside aioboto3

First step of the aioboto3 → aiobotocore migration. Both libraries
coexist until all async call sites are moved over.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Teach the sync transformer about aiobotocore (additive)

Add aiobotocore handling to `scripts/generate_sync.py` while the async source still uses aioboto3. Because no async source uses `aiobotocore.session`, `get_session`, `AioSession`, or `create_client` yet, these rules are no-ops and regeneration must produce **zero diff**.

**Files:**
- Modify: `scripts/generate_sync.py`

**Interfaces:**
- Produces (transformer guarantees, relied on by Tasks 3-5):
  - `from aiobotocore.session import <names>` → `import boto3`
  - bare name `get_session` → `boto3.Session` (so `get_session()` → `boto3.Session()`)
  - type name `AioSession` → `boto3.Session`
  - method/attr `create_client` → `client`
  - `async with x.create_client(...) as v:` unwraps like `.client(...)` does
  - test patch-target strings containing `get_session` → `boto3.Session`

- [ ] **Step 1: Add the `create_client → client` method rewrite**

In `scripts/generate_sync.py`, extend `METHOD_NAME_REWRITES` (currently ~line 71):

```python
# Method name rewrites for context manager calls
METHOD_NAME_REWRITES = {
    "__aenter__": "__enter__",
    "__aexit__": "__exit__",
    # aiobotocore's session.create_client(...) -> boto3's session.client(...)
    "create_client": "client",
}
```

- [ ] **Step 2: Add the `get_session` name rewrite**

Extend `IMPORT_NAME_REWRITES` (currently ~line 100) with one entry:

```python
    # Decorator rewrites
    "asynccontextmanager": "contextmanager",
    # aiobotocore's get_session() -> boto3.Session()
    "get_session": "boto3.Session",
```

(`visit_Name` consults `IMPORT_NAME_REWRITES`, so the call `get_session()` becomes `boto3.Session()`.)

- [ ] **Step 3: Add the `AioSession` type rewrite**

Extend `TYPE_REWRITES` (currently ~line 113):

```python
# Type annotation rewrites
TYPE_REWRITES = {
    "AsyncIterator": "Iterator",
    "AsyncContextManager": "ContextManager",
    "AsyncGenerator": "Generator",
    # aiobotocore's AioSession type -> boto3.Session
    "AioSession": "boto3.Session",
}
```

- [ ] **Step 4: Add the test patch-target string rewrite map**

Near the other test-rewrite maps (search for `TEST_IMPORT_PATH_REWRITES`), add:

```python
# Patch-target string rewrites for generated sync tests.
# Async tests patch "<module>.get_session"; sync tests patch
# "<module>.boto3.Session".
TEST_PATCH_TARGET_REWRITES = {
    "get_session": "boto3.Session",
}
```

- [ ] **Step 5: Special-case the `from aiobotocore.session import ...` statement → `import boto3`**

In the base transformer's `visit_ImportFrom` (search for `def visit_ImportFrom` inside the production transformer class, ~line 759), add this as the first statement in the method body, before the `if node.module:` block:

```python
        # aiobotocore session import collapses to a plain boto3 import in sync
        if node.module == "aiobotocore.session":
            return ast.copy_location(ast.Import(names=[ast.alias(name="boto3")]), node)
```

- [ ] **Step 6: Make the `async with` unwrap match `create_client`**

In `visit_AsyncWith` (~line 538-543), widen the attribute match. Change:

```python
                isinstance(ctx_expr, ast.Call)
                and isinstance(ctx_expr.func, ast.Attribute)
                and ctx_expr.func.attr == "client"
                and item.optional_vars is not None
```

to:

```python
                isinstance(ctx_expr, ast.Call)
                and isinstance(ctx_expr.func, ast.Attribute)
                and ctx_expr.func.attr in ("client", "create_client")
                and item.optional_vars is not None
```

- [ ] **Step 7: Make the `.__enter__()` removal match `create_client` (defensive)**

In `visit_Call` (~line 498-504), change:

```python
            and node.func.value.func.attr == "client"
```

to:

```python
            and node.func.value.func.attr in ("client", "create_client")
```

- [ ] **Step 8: Apply the test patch-target rewrite in the test transformer's `visit_Constant`**

In the **test** transformer class's `visit_Constant` (search for the second `def visit_Constant`, ~line 988, which already loops over `TEST_IMPORT_PATH_REWRITES` and `IMPORT_MODULE_REWRITES`), add a loop for the new map after the existing ones:

```python
            for old_path, new_path in TEST_IMPORT_PATH_REWRITES.items():
                node.value = node.value.replace(old_path, new_path)
            # Also rewrite module names (aioboto3 -> boto3) in patch targets
            for old_mod, new_mod in IMPORT_MODULE_REWRITES.items():
                node.value = node.value.replace(old_mod, new_mod)
            # Rewrite aiobotocore get_session patch targets -> boto3.Session
            for old_t, new_t in TEST_PATCH_TARGET_REWRITES.items():
                node.value = node.value.replace(old_t, new_t)
```

- [ ] **Step 9: Regenerate and prove zero drift**

Run:
```bash
hatch run generate-sync
git diff --stat
```
Expected: **no files changed** (async source still uses aioboto3; new rules are no-ops). If anything changed, a rule is firing prematurely — fix it before continuing.

- [ ] **Step 10: Run unit tests + the sync-up-to-date hook**

Run:
```bash
uv run pytest tests/unit/ -q
pre-commit run --all-files
```
Expected: PASS / all hooks pass.

- [ ] **Step 11: Commit**

```bash
git add scripts/generate_sync.py
git commit -m "$(cat <<'EOF'
🔨 build(ci): teach sync transformer to map aiobotocore → boto3

Additive rules: from aiobotocore.session import → import boto3,
get_session() → boto3.Session(), AioSession → boto3.Session,
create_client → client, and the matching async-with unwrap plus test
patch-target rewrite. No-ops until async source adopts aiobotocore, so
regeneration produces zero drift.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Migrate `repository.py` (no async-test patches)

`repository.py` has no tests that patch `aioboto3` (repository tests use moto). After this edit, regenerated `sync_repository.py` must be functionally identical.

**Files:**
- Modify: `src/zae_limiter/repository.py` (import line 10; annotation line 102; session creation lines 296 & 435; `.client(` calls lines 297 & 437)
- Regenerate: `src/zae_limiter/sync_repository.py`

- [ ] **Step 1: Swap the import**

Change line 10 from:
```python
import aioboto3
```
to:
```python
from aiobotocore.session import AioSession, get_session
```

- [ ] **Step 2: Swap the session type annotation**

Change line ~102 from:
```python
        self._session: aioboto3.Session | None = None
```
to:
```python
        self._session: AioSession | None = None
```

- [ ] **Step 3: Swap the DynamoDB client creation (`_get_client`)**

Change the block at ~line 295-301 from:
```python
        if self._client is None:
            self._session = aioboto3.Session()
            self._client = await self._session.client(
                "dynamodb",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
            ).__aenter__()
        return self._client
```
to:
```python
        if self._client is None:
            self._session = get_session()
            self._client = await self._session.create_client(
                "dynamodb",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
            ).__aenter__()
        return self._client
```

- [ ] **Step 4: Swap the STS client creation (`_get_caller_identity_arn`)**

Change the block at ~line 434-441 from:
```python
            if self._session is None:
                self._session = aioboto3.Session()

            async with self._session.client(
                "sts",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
            ) as sts_client:
```
to:
```python
            if self._session is None:
                self._session = get_session()

            async with self._session.create_client(
                "sts",
                region_name=self.region,
                endpoint_url=self.endpoint_url,
            ) as sts_client:
```

- [ ] **Step 5: Regenerate and verify the generated sync diff is empty (modulo import order)**

Run:
```bash
hatch run generate-sync
git diff -- src/zae_limiter/sync_repository.py
```
Expected: no diff, **or** at most a one-line ruff/isort reordering of the `import boto3` line. The lines `self._session = boto3.Session()`, `self._session.client("dynamodb", ...)`, and `sts_client = self._session.client("sts", ...)` must be byte-identical to before. If any other line differs, fix the transformer rule, do not hand-edit the generated file.

- [ ] **Step 6: Run repository + limiter unit tests (moto path exercises aiobotocore)**

Run:
```bash
uv run pytest tests/unit/test_repository.py tests/unit/test_sync_repository.py tests/unit/test_limiter.py tests/unit/test_sync_limiter.py -q
```
Expected: PASS. (These run against moto, which `tests/fixtures/moto.py` already patches for aiobotocore.)

- [ ] **Step 7: Type-check src**

Run: `uv run mypy src/zae_limiter`
Expected: Success (types-aiobotocore provides `AioSession` / `create_client` types).

- [ ] **Step 8: Commit**

```bash
git add src/zae_limiter/repository.py src/zae_limiter/sync_repository.py
git commit -m "$(cat <<'EOF'
♻️ refactor(repository): create DynamoDB/STS clients via aiobotocore

Use aiobotocore.session.get_session().create_client(...) instead of
aioboto3.Session().client(...). Generated sync_repository.py is
unchanged (transformer maps it back to boto3).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Migrate `infra/stack_manager.py` + its async tests

**Files:**
- Modify: `src/zae_limiter/infra/stack_manager.py` (import line 9; annotation line 65; session creations lines 74, 549, 656, 745; `.client(` calls lines 84, 558, 665, 757)
- Modify: `tests/unit/test_stack_manager.py` (20 `aioboto3` refs, 22 `mock_session.client` sites)
- Regenerate: `src/zae_limiter/infra/sync_stack_manager.py`, `tests/unit/test_sync_stack_manager.py`

**Interfaces:**
- Consumes: transformer rules from Task 2.
- Produces: async tests patch `zae_limiter.infra.stack_manager.get_session` (alias kept as `mock_session_class`) and wire `mock_session.create_client.return_value` / `mock_session_class.return_value`.

- [ ] **Step 1: Swap the import**

Change line 9 from `import aioboto3` to:
```python
from aiobotocore.session import AioSession, get_session
```
Keep `from botocore.exceptions import ClientError` (line 10) unchanged.

- [ ] **Step 2: Swap the annotation**

Change line ~65 `self._session: aioboto3.Session | None = None` →
```python
        self._session: AioSession | None = None
```

- [ ] **Step 3: Swap all four `aioboto3.Session()` → `get_session()`**

There are four occurrences (`_get_client` ~line 74, and three Lambda-deploy paths ~lines 549, 656, 745). Replace each:
```python
            self._session = aioboto3.Session()
```
with:
```python
            self._session = get_session()
```
Run to confirm all four are gone:
```bash
grep -n "aioboto3" src/zae_limiter/infra/stack_manager.py
```
Expected: no output.

- [ ] **Step 4: Swap all four `session.client(` → `session.create_client(`**

The four client-creation call sites:
- `_get_client`: `self._client = await session.client("cloudformation", **kwargs).__aenter__()` → `...await session.create_client("cloudformation", **kwargs).__aenter__()`
- the three `async with session.client("lambda", **kwargs) as lambda_client:` (and any `self._session.client(...)`) → `async with session.create_client("lambda", **kwargs) as lambda_client:`

Confirm:
```bash
grep -n "\.client(" src/zae_limiter/infra/stack_manager.py
```
Expected: no output (all are now `.create_client(`).

- [ ] **Step 5: Update async test patch targets**

In `tests/unit/test_stack_manager.py`, replace every patch target. Run:
```bash
sed -i 's/zae_limiter\.infra\.stack_manager\.aioboto3\.Session/zae_limiter.infra.stack_manager.get_session/g' tests/unit/test_stack_manager.py
```
Then verify none remain:
```bash
grep -n "aioboto3" tests/unit/test_stack_manager.py
```
Expected: no output.

- [ ] **Step 6: Update async test mock wiring (`.client` → `.create_client`)**

The mocks set up the production-called method, which is now `create_client`. Run:
```bash
sed -i 's/mock_session\.client/mock_session.create_client/g' tests/unit/test_stack_manager.py
```
This updates both `mock_session.create_client.return_value = ...` and `mock_session.create_client.assert_called_once_with(...)` sites. Verify the count moved over:
```bash
grep -c "mock_session.create_client" tests/unit/test_stack_manager.py   # expect 22
grep -c "mock_session.client" tests/unit/test_stack_manager.py          # expect 0
```

- [ ] **Step 7: Run the async stack-manager tests directly**

Run: `uv run pytest tests/unit/test_stack_manager.py -q`
Expected: PASS. (Production now calls `get_session().create_client(...)`; the patched `get_session` returns `mock_session_class.return_value`, whose `.create_client(...)` returns the context-manager mock.)

- [ ] **Step 8: Regenerate and verify**

Run:
```bash
hatch run generate-sync
git diff -- src/zae_limiter/infra/sync_stack_manager.py
```
Expected: `sync_stack_manager.py` unchanged except possibly the `import boto3` ordering line. The `boto3.Session()` and `session.client("...", **kwargs)` lines must be byte-identical.

- [ ] **Step 9: Run the generated sync stack-manager tests**

Run: `uv run pytest tests/unit/test_sync_stack_manager.py -q`
Expected: PASS. If a regenerated patch target or mock line is wrong, fix the transformer rule (Task 2) and regenerate — do not hand-edit the generated test.

- [ ] **Step 10: Commit**

```bash
git add src/zae_limiter/infra/stack_manager.py src/zae_limiter/infra/sync_stack_manager.py tests/unit/test_stack_manager.py tests/unit/test_sync_stack_manager.py
git commit -m "$(cat <<'EOF'
♻️ refactor(infra): create CFN/Lambda clients via aiobotocore

Migrate StackManager off aioboto3 to aiobotocore get_session()/
create_client(); update async tests to patch get_session and wire
create_client mocks. Generated sync code is unchanged.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Migrate `infra/discovery.py` + its async tests

**Files:**
- Modify: `src/zae_limiter/infra/discovery.py` (import line 10; annotation line 59; session creations lines 68 & 137; `.client(` calls lines 77 & 147)
- Modify: `tests/unit/test_limiter.py` (3 discovery tests, ~lines 3209-3278)
- Regenerate: `src/zae_limiter/infra/sync_discovery.py`, `tests/unit/test_sync_limiter.py`

- [ ] **Step 1: Swap import, annotation, sessions, and client calls in `discovery.py`**

- Line 10: `import aioboto3` → `from aiobotocore.session import AioSession, get_session`
- Line ~59: `self._session: aioboto3.Session | None = None` → `self._session: AioSession | None = None`
- Lines ~68 and ~137: `self._session = aioboto3.Session()` → `self._session = get_session()`
- Line ~77: `self._client = await session.client("cloudformation", **kwargs).__aenter__()` → `... await session.create_client("cloudformation", **kwargs).__aenter__()`
- Line ~147: `async with session.client("resourcegroupstaggingapi", **kwargs) as tagging_client:` → `async with session.create_client("resourcegroupstaggingapi", **kwargs) as tagging_client:`

Verify:
```bash
grep -n "aioboto3\|\.client(" src/zae_limiter/infra/discovery.py
```
Expected: no output.

- [ ] **Step 2: Rewrite the three discovery tests in `test_limiter.py`**

These patch the whole module today. Replace each of the three blocks (at ~3212, ~3237, ~3263). The pattern transformation, applied to all three:

Replace:
```python
        with patch("zae_limiter.infra.discovery.aioboto3") as mock_aioboto3:
            mock_session = MagicMock()
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_session.client.return_value = mock_client
            mock_aioboto3.Session.return_value = mock_session
```
with:
```python
        with patch("zae_limiter.infra.discovery.get_session") as mock_get_session:
            mock_session = MagicMock()
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock()
            mock_session.create_client.return_value = mock_client
            mock_get_session.return_value = mock_session
```

And update the two assertion sites:
- `mock_aioboto3.Session.assert_called_once()` (in `test_get_client_caches_client`) → `mock_get_session.assert_called_once()`
- `mock_session.client.assert_called_once_with(...)` (two sites, in `test_get_client_passes_region_and_endpoint` and `test_get_client_without_region_or_endpoint`) → `mock_session.create_client.assert_called_once_with(...)`

Verify no stragglers:
```bash
grep -n "aioboto3\|mock_session\.client" tests/unit/test_limiter.py
```
Expected: no output.

- [ ] **Step 3: Run the affected async tests**

Run:
```bash
uv run pytest tests/unit/test_limiter.py -k "get_client" -q
```
Expected: PASS (3 tests).

- [ ] **Step 4: Regenerate and verify production sync**

Run:
```bash
hatch run generate-sync
git diff -- src/zae_limiter/infra/sync_discovery.py
```
Expected: unchanged except possible `import boto3` ordering. `boto3.Session()` and `session.client(...)` lines byte-identical.

- [ ] **Step 5: Run the generated sync tests**

Run:
```bash
uv run pytest tests/unit/test_sync_limiter.py -k "get_client" -q
uv run pytest tests/unit/test_sync_limiter.py -q
```
Expected: PASS. The regenerated sync test should patch `zae_limiter.infra.sync_discovery.boto3.Session` and use `mock_session.client...` (via the transformer). If wrong, fix the Task 2 rules and regenerate.

- [ ] **Step 6: Commit**

```bash
git add src/zae_limiter/infra/discovery.py src/zae_limiter/infra/sync_discovery.py tests/unit/test_limiter.py tests/unit/test_sync_limiter.py
git commit -m "$(cat <<'EOF'
♻️ refactor(infra): create discovery clients via aiobotocore

Migrate InfrastructureDiscovery off aioboto3; update the three
_get_client tests to patch get_session and wire create_client mocks.
Generated sync code is unchanged.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Remove aioboto3 and finalize config/comments

No async source references `aioboto3` now. Remove the dependency, the transformer's aioboto3 rule, the mypy override, update the zip-content tests, and fix stale comments.

**Files:**
- Modify: `pyproject.toml`, `scripts/generate_sync.py`, `tests/integration/test_lambda_builder.py`, `tests/unit/test_lambda_builder.py`, `src/zae_limiter/infra/lambda_builder.py`, `src/zae_limiter_provisioner/applier.py`, `tests/fixtures/capacity.py`

- [ ] **Step 1: Confirm no remaining aioboto3 imports/usages in source**

Run:
```bash
grep -rn "import aioboto3\|aioboto3\.Session" src/ tests/
```
Expected: no output (only comment text may remain, handled below).

- [ ] **Step 2: Drop `aioboto3` from runtime deps**

In `pyproject.toml`, remove the `"aioboto3>=12.0.0",` line so only `"aiobotocore>=2.13.0",` remains.

- [ ] **Step 3: Drop `aioboto3` from the mypy override**

In the `[[tool.mypy.overrides]]` `module` list, remove the `"aioboto3",` and `"aioboto3.*",` entries, leaving `"aiobotocore"` / `"aiobotocore.*"`.

- [ ] **Step 4: Remove the aioboto3 rule from the transformer**

In `scripts/generate_sync.py`, change `IMPORT_MODULE_REWRITES` (~line 66) from:
```python
IMPORT_MODULE_REWRITES = {
    "aioboto3": "boto3",
}
```
to:
```python
IMPORT_MODULE_REWRITES: dict[str, str] = {}
```
Also update the module docstring at the top (lines ~4-7) replacing "aioboto3" mentions with "aiobotocore" (e.g., "transforms async code (aiobotocore) to sync code (boto3)" and "Rewriting imports (aiobotocore -> boto3)"). Update the inline comments at ~line 65 and ~line 494/529 that name aioboto3.

- [ ] **Step 5: Update the Lambda zip-content tests**

In `tests/integration/test_lambda_builder.py` (~line 50-60), change the docstring and the assertion to target aiobotocore:
```python
        """Built zip does NOT include core deps (aiobotocore, aiohttp, click, etc.)."""
```
```python
            aiobotocore_files = [f for f in files if f.startswith("aiobotocore/")]
            assert len(aiobotocore_files) == 0, "aiobotocore should not be in zip"
```

In `tests/unit/test_lambda_builder.py`:
- Line ~91: change the tuple `("aioboto3", "boto3", "click", "pip", "python-ulid")` to `("aiobotocore", "boto3", "click", "pip", "python-ulid")`.
- Line ~241: change `assert not any(line.startswith("aioboto3") for line in reqs_lines)` to `assert not any(line.startswith("aiobotocore") for line in reqs_lines)`.

- [ ] **Step 6: Fix stale comments (cosmetic, no behavior change)**

- `src/zae_limiter/infra/lambda_builder.py` lines ~11, ~27, ~60, ~136: replace "aioboto3" with "aiobotocore" in the comment text.
- `src/zae_limiter_provisioner/applier.py` line ~4: "Lambda where aioboto3 is not available." → "Lambda where aiobotocore is not available."
- `tests/fixtures/capacity.py` line ~169: "tracks calls at the aioboto3 client level." → "...at the aiobotocore client level."

- [ ] **Step 7: Re-sync env, regenerate, prove idempotency**

Run:
```bash
uv sync --all-extras
hatch run generate-sync
git diff --stat -- 'src/**/sync_*.py' 'tests/unit/test_sync_*.py'
```
Expected: no generated-file changes from this task (the aioboto3 rule was already unused). Confirm `aioboto3` is gone from the env:
```bash
uv pip list | grep -i aioboto3 || echo "aioboto3 removed"
```
Expected: `aioboto3 removed`.

- [ ] **Step 8: Full gate**

Run:
```bash
uv run pytest tests/unit/ -q
uv run mypy src/zae_limiter
pre-commit run --all-files
```
Expected: all PASS / all hooks pass.

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
🔥 deps(repository): drop unmaintained aioboto3

Remove the aioboto3 runtime dep, mypy override, and transformer rule now
that all async clients use aiobotocore. Update Lambda zip-content tests
to assert aiobotocore is excluded, and refresh stale comments.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Integration & E2E verification against LocalStack

No code changes expected — this validates the real async client path (DynamoDB transactions, GSI queries, CloudFormation, Lambda deploy) against LocalStack. If something fails, fix in the relevant prior task's file and re-run.

**Files:**
- None (verification). Any fix lands in the owning module + regenerate.

- [ ] **Step 1: Start LocalStack**

Run:
```bash
zae-limiter local up
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
```
Expected: container healthy (`zae-limiter local status`).

- [ ] **Step 2: Run integration tests (repository operations, transactions, GSI, optimistic locking)**

Run: `uv run pytest tests/integration/ -m integration -q`
Expected: PASS.

- [ ] **Step 3: Run the LocalStack e2e workflow tests (CLI, hierarchical limits, aggregator, stack lifecycle)**

Run: `uv run pytest tests/e2e/test_localstack.py -q`
Expected: PASS.

- [ ] **Step 4: Run the lambda-builder integration test (zip excludes aiobotocore)**

Run: `uv run pytest tests/integration/test_lambda_builder.py -q`
Expected: PASS.

- [ ] **Step 5: Tear down**

Run: `zae-limiter local down`

- [ ] **Step 6: Final branch sanity**

Run:
```bash
grep -rn "aioboto3" src/ tests/ pyproject.toml scripts/ | grep -v "\.md:"
```
Expected: no output (no `aioboto3` anywhere in code/config).

- [ ] **Step 7: (If any fix was needed) commit**

```bash
git add -A
git commit -m "$(cat <<'EOF'
✅ test(repository): verify aiobotocore path on LocalStack

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Post-Plan: Issue & PR

After Task 7, per project rules (`.claude/rules/issue-skill.md`, `.claude/rules/pull-request-workflow.md`):
- Open the tracking issue via the `/issue` skill (area `area/repository` + `area/infra`; "drop unmaintained aioboto3 in favor of aiobotocore").
- Open a draft PR via the `/pr` skill referencing the issue, with a test plan covering unit + LocalStack integration/e2e.

---

## Self-Review

**Spec coverage:**
- Scope (3 async files, transformer, deps, tests) → Tasks 1-6. ✓
- Mechanical call-site change → Tasks 3-5. ✓
- Transformer 3-rule table (import / get_session / create_client) → Task 2 (implemented as 5 additive edits + 2 unwrap-condition widenings). ✓
- Deps & typing (aiobotocore floor, mypy override, types-aiobotocore already present) → Tasks 1 & 6. ✓
- Tests (patch targets, mock wiring, lambda zip assertions) → Tasks 4, 5, 6. ✓
- Verification gates (generate-sync idempotent, pre-commit, mypy, pytest, LocalStack) → embedded per task + Task 7. ✓
- Process (issue + PR, no ADR) → Post-Plan section. ✓
- Risk "hidden resource-API usage" → confirmed none during planning (grep showed only `.client(`). ✓

**Placeholder scan:** No TBD/TODO; every code/sed/command step is concrete. The `...` inside code blocks denote unchanged surrounding kwargs already present in the file, not omissions.

**Type/name consistency:** `get_session`, `AioSession`, `create_client`, `boto3.Session`, `IMPORT_NAME_REWRITES`, `TYPE_REWRITES`, `METHOD_NAME_REWRITES`, `TEST_PATCH_TARGET_REWRITES`, `TEST_IMPORT_PATH_REWRITES`, `IMPORT_MODULE_REWRITES`, `visit_ImportFrom`, `visit_AsyncWith`, `visit_Call`, `visit_Constant` are used consistently across tasks and match the symbols in `scripts/generate_sync.py`.
