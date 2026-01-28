"""Configuration for documentation example tests."""

import asyncio
import re
import textwrap
from collections.abc import Awaitable
from unittest.mock import patch

import pytest
from moto import mock_aws
from pytest_examples import CodeExample
from pytest_examples.config import ExamplesConfig

# Ruff config for doc examples: ignore rules that are inherent to documentation style.
# - F704: yield/await outside function (async examples use bare await)
# - F706: return outside function (snippets showing return values)
# - F811: redefinition of unused name (multiple examples redefine same var)
# - F821: undefined name (snippets reference variables from prose context)
# - F841: local variable assigned but never used (illustrative assignments)
# - E741: ambiguous variable name (e.g., single-letter vars in examples)
# - I001: import not sorted (cosmetic, doc examples prioritize readability)
# - F401: imported but unused (doc examples import for illustration)
# - N807: function name should not start/end with __ (e.g., __version__)
DOCS_EXAMPLES_CONFIG = ExamplesConfig(
    line_length=100,
    target_version="py311",
    ruff_select=["E", "F", "I", "N", "W", "UP"],
    ruff_ignore=["F401", "F704", "F706", "F811", "F821", "F841", "E741", "I001", "N807"],
)

# Tags that cause an example to be skipped during execution tests.
# - lint-only: fragments, pseudo-code, or blocks that reference undefined helpers
# - requires-external: needs packages not in project dependencies
# - requires-localstack: needs real CloudFormation/Lambda (LocalStack or AWS)
SKIP_TAGS = {"requires-external", "lint-only", "requires-localstack"}


def should_skip(example: CodeExample, tags_to_skip: set[str]) -> str | None:
    """Return a skip reason if the example has any skip tags, else None."""
    tags = example.prefix_tags()
    matched = tags & tags_to_skip
    if matched:
        return f"tagged with {', '.join(sorted(matched))}"
    return None


def _patch_aiobotocore_response():
    """
    Patch aiobotocore to work with moto's sync responses.

    Moto returns botocore.awsrequest.AWSResponse which has sync content,
    but aiobotocore expects async content. This patch wraps the response
    handling to convert sync content to async.

    See: https://github.com/aio-libs/aiobotocore/discussions/1300
    """
    from aiobotocore import endpoint

    original_convert = endpoint.convert_to_response_dict

    async def patched_convert(http_response, operation_model):
        # If content is not awaitable (moto's sync response), wrap it
        if hasattr(http_response, "_content") and not isinstance(http_response._content, Awaitable):
            fut: asyncio.Future[bytes] = asyncio.Future()
            fut.set_result(http_response.content)
            http_response._content = fut
        return await original_convert(http_response, operation_model)

    return patch.object(endpoint, "convert_to_response_dict", patched_convert)


@pytest.fixture
def moto_env(monkeypatch):
    """Provide a moto-backed AWS mock environment for doc example execution."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

    with mock_aws(), _patch_aiobotocore_response():
        yield


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Add doctest marker to all items in this directory."""
    for item in items:
        if "doctest" in str(item.fspath):
            item.add_marker(pytest.mark.doctest)


# ---------------------------------------------------------------------------
# Async detection and wrapping helpers
# ---------------------------------------------------------------------------

_ASYNC_USAGE_RE = re.compile(
    r"(?:^|\s)(?:await |async with |async for )",
    re.MULTILINE,
)
_ASYNC_DEF_RE = re.compile(r"^\s*async\s+def\s+", re.MULTILINE)


def has_bare_async(source: str) -> bool:
    """Check if source contains async statements without an async def wrapper.

    Returns True when the source uses await/async with/async for but does
    NOT define its own async function (meaning these statements are bare
    module-level code that needs wrapping).
    """
    if not _ASYNC_USAGE_RE.search(source):
        return False
    # If source defines its own async function, assume async code is inside it
    if _ASYNC_DEF_RE.search(source):
        return False
    return True


def wrap_async_source(source: str) -> str:
    """Wrap bare-async source in async def _run() + asyncio.run(_run()).

    Keeps top-level imports at module scope. Wraps everything else
    inside async def _run() so bare await/async with work.
    """
    lines = source.splitlines(keepends=True)
    import_lines: list[str] = []
    body_lines: list[str] = []
    in_imports = True
    for line in lines:
        stripped = line.strip()
        if in_imports and (
            stripped.startswith(("import ", "from ")) or stripped == "" or stripped.startswith("#")
        ):
            import_lines.append(line)
        else:
            in_imports = False
            body_lines.append(line)
    import_block = "".join(import_lines)
    indented_body = textwrap.indent("".join(body_lines), "    ")
    return (
        f"{import_block}import asyncio\n\nasync def _run():\n{indented_body}\nasyncio.run(_run())\n"
    )


# ---------------------------------------------------------------------------
# Stub functions for doc examples
# ---------------------------------------------------------------------------


async def _stub_call_llm(*args, **kwargs):
    """Stub for call_llm() used in doc examples."""
    return {
        "choices": [{"message": {"content": "Hello"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


async def _stub_call_api(*args, **kwargs):
    """Stub for call_api() used in doc examples."""
    return {"status": "ok"}


async def _stub_do_work(*args, **kwargs):
    """Stub for do_work() used in doc examples."""
    pass


async def _stub_premium_operation(*args, **kwargs):
    """Stub for premium_operation() used in doc examples."""
    return "premium result"


async def _stub_basic_operation(*args, **kwargs):
    """Stub for basic_operation() used in doc examples."""
    return "basic result"


# ---------------------------------------------------------------------------
# Fixtures for doc example execution
# ---------------------------------------------------------------------------


@pytest.fixture
def doctest_env(moto_env, monkeypatch):
    """Extended moto environment for doc example execution.

    Patches RateLimiter and Repository so that doc examples that construct
    their own instances work against moto without CloudFormation.
    """
    from zae_limiter.limiter import RateLimiter as _RateLimiter
    from zae_limiter.limiter import SyncRateLimiter as _SyncRateLimiter
    from zae_limiter.repository import Repository

    _created_tables: set[str] = set()

    async def _auto_create_ensure(self):
        """Auto-create DynamoDB table instead of CloudFormation stack."""
        if self.table_name not in _created_tables:
            await self.create_table()
            _created_tables.add(self.table_name)

    monkeypatch.setattr(Repository, "ensure_infrastructure", _auto_create_ensure)

    _original_init = _RateLimiter.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["skip_version_check"] = True
        kwargs.pop("stack_options", None)
        _original_init(self, *args, **kwargs)

    monkeypatch.setattr(_RateLimiter, "__init__", _patched_init)

    _original_sync_init = _SyncRateLimiter.__init__

    def _patched_sync_init(self, *args, **kwargs):
        kwargs["skip_version_check"] = True
        kwargs.pop("stack_options", None)
        _original_sync_init(self, *args, **kwargs)

    monkeypatch.setattr(_SyncRateLimiter, "__init__", _patched_sync_init)


# Entity IDs commonly referenced in documentation examples
_COMMON_ENTITIES = [
    "user-123",
    "user-free",
    "user-premium",
    "api-key-123",
    "api-key-456",
    "key-123",
    "proj-1",
    "org-456",
    "tenant-acme",
]


@pytest.fixture
def doctest_globals(doctest_env):
    """Pre-built globals for doc example execution.

    Provides a working limiter, common entities, stub functions,
    and commonly-used imports so doc code blocks can execute.
    """
    import asyncio as _asyncio

    from zae_limiter import (
        AuditAction,
        AuditEvent,
        BackendCapabilities,
        BucketState,
        CacheStats,
        Entity,
        Lease,
        Limit,
        LimiterInfo,
        LimitStatus,
        OnUnavailable,
        RateLimiter,
        RateLimiterUnavailable,
        RateLimitExceeded,
        Repository,
        StackOptions,
        Status,
        SyncLease,
        SyncRateLimiter,
        UsageSnapshot,
        UsageSummary,
    )

    # Create pre-built limiter with table
    _limiter = RateLimiter(name="limiter", region="us-east-1")
    _asyncio.run(_limiter._repository.create_table())

    # Pre-create common entities
    async def _setup():
        for eid in _COMMON_ENTITIES:
            try:
                await _limiter.create_entity(entity_id=eid)
            except Exception:
                pass

    _asyncio.run(_setup())

    return {
        # Pre-built limiter
        "limiter": _limiter,
        # Common classes
        "RateLimiter": RateLimiter,
        "SyncRateLimiter": SyncRateLimiter,
        "Repository": Repository,
        "Limit": Limit,
        "Lease": Lease,
        "SyncLease": SyncLease,
        "StackOptions": StackOptions,
        "Entity": Entity,
        "LimitStatus": LimitStatus,
        "BucketState": BucketState,
        "AuditEvent": AuditEvent,
        "AuditAction": AuditAction,
        "UsageSnapshot": UsageSnapshot,
        "UsageSummary": UsageSummary,
        "CacheStats": CacheStats,
        "LimiterInfo": LimiterInfo,
        "BackendCapabilities": BackendCapabilities,
        "Status": Status,
        "RateLimitExceeded": RateLimitExceeded,
        "RateLimiterUnavailable": RateLimiterUnavailable,
        "OnUnavailable": OnUnavailable,
        # Stub functions
        "call_llm": _stub_call_llm,
        "call_api": _stub_call_api,
        "do_work": _stub_do_work,
        "premium_operation": _stub_premium_operation,
        "basic_operation": _stub_basic_operation,
    }
