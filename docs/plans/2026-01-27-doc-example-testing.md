# Documentation Example Testing Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Automatically validate that all Python code examples in docs/ are syntactically correct, lint-clean, and executable against moto or LocalStack backends.

**Architecture:** Use `pytest-examples` to discover code blocks from markdown files, classify them via code fence tags (`{.python .lint-only}`, `{.python .requires-moto}`, `{.python .requires-localstack}`), and run them through the existing test pyramid (lint-only, moto-backed unit, LocalStack-backed integration). A single test file `tests/doctest/test_docs.py` parametrizes over all discovered examples and dispatches to the right execution strategy.

**Tech Stack:** pytest-examples (pydantic), moto, pytest-asyncio, existing conftest fixtures

---

## Code Fence Tag Convention

Markdown code fences use `prefix_tags()` from pytest-examples. The tag syntax is:

```
python                          <!-- default: lint + run with moto -->
{.python .lint-only}            <!-- lint only, don't execute -->
{.python .requires-localstack}  <!-- run with LocalStack backend -->
{.python .requires-external}    <!-- skip: needs tiktoken, openai, etc. -->
```

**Default behavior** (no tag): lint + attempt to run with moto. This means the majority of code blocks need zero annotation. Only exceptions get tagged.

---

## Task 1: Add pytest-examples dependency

**Files:**
- Modify: `pyproject.toml` (dev dependencies)

**Step 1: Add dependency**

In `pyproject.toml`, add `pytest-examples` to the `[project.optional-dependencies] dev` list:

```toml
dev = [
    # ... existing deps ...
    "pytest-examples>=0.0.18",
]
```

**Step 2: Install**

Run: `uv sync --all-extras`
Expected: Clean install with pytest-examples available.

**Step 3: Verify**

Run: `uv run python -c "from pytest_examples import find_examples, CodeExample, EvalExample; print('OK')"`
Expected: `OK`

**Step 4: Commit**

```
🔧 chore(test): add pytest-examples dependency for doc testing
```

---

## Task 2: Create lint-only test runner

**Files:**
- Create: `tests/doctest/__init__.py`
- Create: `tests/doctest/conftest.py`
- Create: `tests/doctest/test_docs_lint.py`

**Step 1: Create the test directory and conftest**

`tests/doctest/__init__.py`: empty file.

`tests/doctest/conftest.py`:

```python
"""Configuration for documentation example tests."""

import pytest


def pytest_collection_modifyitems(config, items):
    """Add doctest marker to all items in this directory."""
    for item in items:
        if "doctest" in str(item.fspath):
            item.add_marker(pytest.mark.doctest)
```

**Step 2: Write the lint test**

`tests/doctest/test_docs_lint.py`:

```python
"""Lint all Python code examples in documentation."""

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

# Discover all Python code blocks in docs/
@pytest.mark.parametrize("example", find_examples("docs/"), ids=str)
def test_docs_lint(example: CodeExample, eval_example: EvalExample):
    """Every Python code block in docs/ must pass ruff linting."""
    eval_example.lint(example)
```

**Step 3: Run and assess**

Run: `uv run pytest tests/doctest/test_docs_lint.py -v --no-header -x 2>&1 | head -80`
Expected: Parametrized tests, one per code block. Some may fail due to lint issues — that's the point. Record which files/blocks fail.

**Step 4: Fix lint failures or tag blocks as `{.python .lint-only}`**

Iterate: fix code blocks that have real lint issues (stale imports, syntax errors). For blocks that are intentionally partial (pseudo-code, fragments), change their fence from `` ```python `` to `` ```{.python .lint-only} `` — but note: lint-only blocks are still linted, so truly non-Python blocks should use `` ```text `` or `` ``` `` instead.

**Step 5: Commit when lint pass is green**

```
✅ test(docs): add pytest-examples lint pass for all doc code blocks
```

---

## Task 3: Add the doctest marker to pytest config

**Files:**
- Modify: `pyproject.toml` (pytest markers)

**Step 1: Add marker**

In `pyproject.toml` under `[tool.pytest.ini_options]` markers, add:

```toml
markers = [
    # ... existing markers ...
    "doctest: marks tests that validate documentation code examples",
]
```

**Step 2: Commit**

```
🔧 chore(test): register doctest pytest marker
```

---

## Task 4: Create moto-backed execution test

This is the core test that actually runs code blocks against a mocked DynamoDB.

**Files:**
- Create: `tests/doctest/test_docs_run.py`
- Modify: `tests/doctest/conftest.py`

**Step 1: Add shared fixtures to conftest**

Update `tests/doctest/conftest.py` to provide moto environment:

```python
"""Configuration for documentation example tests."""

import asyncio
from collections.abc import Awaitable
from unittest.mock import patch

import pytest
from moto import mock_aws

from zae_limiter import RateLimiter, SyncRateLimiter


SKIP_TAGS = {"requires-external", "lint-only", "requires-localstack"}


def should_skip(example, tags_to_skip: set[str]) -> str | None:
    """Return skip reason if example should be skipped, else None."""
    tags = example.prefix_tags()
    for tag in tags_to_skip:
        if tag in tags:
            return f"tagged with {tag}"
    return None


def _patch_aiobotocore_response():
    """Patch aiobotocore to work with moto's sync responses."""
    from aiobotocore import endpoint

    original_convert = endpoint.convert_to_response_dict

    async def patched_convert(http_response, operation_model):
        if hasattr(http_response, "_content") and not isinstance(
            http_response._content, Awaitable
        ):
            fut: asyncio.Future[bytes] = asyncio.Future()
            fut.set_result(http_response.content)
            http_response._content = fut
        return await original_convert(http_response, operation_model)

    return patch.object(endpoint, "convert_to_response_dict", patched_convert)


@pytest.fixture
def moto_env(monkeypatch):
    """Provide a moto-backed AWS environment for doc examples."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

    with mock_aws(), _patch_aiobotocore_response():
        yield
```

**Step 2: Write the execution test**

`tests/doctest/test_docs_run.py`:

```python
"""Run Python code examples in documentation against moto."""

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

from tests.doctest.conftest import SKIP_TAGS, should_skip


@pytest.mark.parametrize("example", find_examples("docs/"), ids=str)
def test_docs_run(example: CodeExample, eval_example: EvalExample, moto_env):
    """Run doc examples that don't require special infrastructure."""
    reason = should_skip(example, SKIP_TAGS)
    if reason:
        pytest.skip(reason)

    # Lint first (fast fail)
    eval_example.lint(example)

    # Run with moto backend
    try:
        eval_example.run(example)
    except Exception as e:
        # Provide helpful context on failure
        pytest.fail(
            f"Example failed at {example.path}:{example.start_line}\n"
            f"Source:\n{example.source}\n"
            f"Error: {e}"
        )
```

**Step 3: Run and assess**

Run: `uv run pytest tests/doctest/test_docs_run.py -v --no-header -x 2>&1 | head -80`
Expected: Many failures initially. Examples that use `async with limiter.acquire(...)` without wrapping in an async function will fail. Examples that call undefined functions like `call_llm()` will fail. This is expected — we'll triage in the next step.

**Step 4: Triage failures into categories**

For each failing example, decide:
1. **Fix the example** — if the code has a real bug (stale import, wrong API)
2. **Tag `{.python .requires-external}`** — needs tiktoken, openai, or undefined helpers
3. **Tag `{.python .lint-only}`** — pseudo-code, fragments, conceptual
4. **Tag `{.python .requires-localstack}`** — needs real CloudFormation/Lambda

**Step 5: Iterate until the moto run pass is green**

**Step 6: Commit**

```
✅ test(docs): add moto-backed execution pass for doc code examples
```

---

## Task 5: Create LocalStack-backed integration test

**Files:**
- Create: `tests/doctest/test_docs_integration.py`

**Step 1: Write the integration test**

`tests/doctest/test_docs_integration.py`:

```python
"""Run Python code examples that require LocalStack."""

import os

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples


@pytest.mark.integration
@pytest.mark.parametrize("example", find_examples("docs/"), ids=str)
def test_docs_integration(example: CodeExample, eval_example: EvalExample):
    """Run doc examples tagged with requires-localstack."""
    tags = example.prefix_tags()
    if "requires-localstack" not in tags:
        pytest.skip("not tagged requires-localstack")

    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("AWS_ENDPOINT_URL not set (LocalStack not running)")

    eval_example.lint(example)
    eval_example.run(example)
```

**Step 2: Run (requires LocalStack)**

```bash
export AWS_ENDPOINT_URL=http://localhost:4566
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
uv run pytest tests/doctest/test_docs_integration.py -v -m integration
```

Expected: Only examples tagged `{.python .requires-localstack}` run. Others skip.

**Step 3: Commit**

```
✅ test(docs): add LocalStack integration pass for doc code examples
```

---

## Task 6: Annotate all doc code blocks

This is the bulk work: go through every markdown file in docs/ and tag code blocks that need special handling.

**Files:**
- Modify: All markdown files in `docs/` that have Python code blocks

**Step 1: Run the lint pass to identify all blocks**

Run: `uv run pytest tests/doctest/test_docs_lint.py -v 2>&1 | grep -E "(PASSED|FAILED)"`

**Step 2: Run the moto execution pass to identify failures**

Run: `uv run pytest tests/doctest/test_docs_run.py -v 2>&1 | grep -E "(PASSED|FAILED|SKIPPED)"`

**Step 3: Annotate blocks systematically**

For each failing block, apply the appropriate tag:

| Failure reason | Tag to apply |
|----------------|-------------|
| Uses `call_llm()`, `openai`, `tiktoken` | `{.python .requires-external}` |
| Uses `StackOptions()` with real CloudFormation | `{.python .requires-localstack}` |
| Is pseudo-code, fragment, or illustration | `{.python .lint-only}` |
| Has a real bug (stale import, wrong API) | **Fix the code** |

**Convention for MkDocs compatibility:** The `{.python .tag}` syntax renders identically to `` ```python `` in MkDocs Material — MkDocs ignores the `.tag` classes. No visual impact to the docs site.

**Step 4: Verify all three passes are green**

```bash
# Lint (all blocks)
uv run pytest tests/doctest/test_docs_lint.py -v

# Unit (moto)
uv run pytest tests/doctest/test_docs_run.py -v

# Integration (LocalStack — if running)
uv run pytest tests/doctest/test_docs_integration.py -v -m integration
```

**Step 5: Commit**

```
📝 docs: annotate code blocks with test classification tags
```

---

## Task 7: Add to CI

**Files:**
- Modify: `.github/workflows/ci.yml`

**Step 1: Add doc lint job**

Add a new job or step to the existing lint job:

```yaml
- name: Lint documentation examples
  run: uv run pytest tests/doctest/test_docs_lint.py -v
```

**Step 2: Add doc run to unit test job**

Add to the existing test job (which already has moto):

```yaml
- name: Test documentation examples (moto)
  run: uv run pytest tests/doctest/test_docs_run.py -v -p no:xdist
```

Note: `-p no:xdist` disables parallel execution since `pytest-examples` may not be compatible with xdist.

**Step 3: Add doc integration to LocalStack job**

Add to the existing integration job (which already has LocalStack running):

```yaml
- name: Test documentation examples (LocalStack)
  run: uv run pytest tests/doctest/test_docs_integration.py -v -m integration -p no:xdist
```

**Step 4: Commit**

```
👷 ci: add documentation example testing to CI pipeline
```

---

## Task 8: Update testing documentation

**Files:**
- Modify: `docs/contributing/testing.md`
- Modify: `CLAUDE.md` (testing section)

**Step 1: Add doctest section to testing.md**

Add a new section documenting:
- The tag convention
- How to run doc tests locally
- How to add tags to new code blocks
- The three test passes (lint, moto, LocalStack)

**Step 2: Update CLAUDE.md**

Add to the "Build & Development" and "Testing" sections:
- New commands for running doc tests
- New marker: `doctest`
- Brief explanation of the tag convention

**Step 3: Commit**

```
📝 docs(test): document code example testing workflow
```

---

## Execution Order & Dependencies

```
Task 1 (dependency) ──→ Task 2 (lint) ──→ Task 3 (marker) ──→ Task 4 (moto run)
                                                                    │
                                                                    ├──→ Task 5 (LocalStack)
                                                                    │
                                                                    └──→ Task 6 (annotate all blocks)
                                                                              │
                                                                              ├──→ Task 7 (CI)
                                                                              └──→ Task 8 (docs)
```

Tasks 5 and 6 can overlap. Task 6 is the largest task and will be iterative. Tasks 7 and 8 are independent of each other.

---

## Verification Checklist

- [ ] `uv run pytest tests/doctest/test_docs_lint.py -v` — all green
- [ ] `uv run pytest tests/doctest/test_docs_run.py -v` — all green (moto)
- [ ] `uv run pytest tests/doctest/test_docs_integration.py -v -m integration` — all green (LocalStack)
- [ ] `uv run pre-commit run --all-files` — no lint regressions
- [ ] MkDocs builds without errors: `uv run mkdocs build --strict`
- [ ] CI pipeline passes all three doc test jobs
