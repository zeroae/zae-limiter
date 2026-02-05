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


class _MockUsage:
    """Mock OpenAI-style usage object for doc examples."""

    prompt_tokens = 100
    completion_tokens = 50
    total_tokens = 150


class _MockChoice:
    """Mock OpenAI-style choice object."""

    class Message:
        content = "Hello"

    message = Message()


class _MockResponse:
    """Mock OpenAI-style response with .usage attribute access."""

    usage = _MockUsage()
    choices = [_MockChoice()]


class _MockOpenAICompletions:
    """Mock openai.chat.completions for doc examples."""

    async def create(self, **kwargs):
        return _MockResponse()


class _MockOpenAIChat:
    completions = _MockOpenAICompletions()


class _MockOpenAI:
    chat = _MockOpenAIChat()


class _MockResult:
    """Mock result for execute_operation() stubs."""

    units_consumed = 12


async def _stub_call_llm(*args, **kwargs):
    """Stub for call_llm() used in doc examples."""
    return _MockResponse()


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


async def _stub_execute_operation(*args, **kwargs):
    """Stub for execute_operation() used in doc examples."""
    return _MockResult()


class _JSONResponse:
    """Stub for starlette.responses.JSONResponse used in doc examples."""

    def __init__(self, status_code=200, content=None, headers=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}


class _HTTPError(Exception):
    """Stub for fastapi.HTTPException used in doc examples."""

    def __init__(self, status_code=500, detail=None):
        self.status_code = status_code
        self.detail = detail


# ---------------------------------------------------------------------------
# Fixtures for doc example execution
# ---------------------------------------------------------------------------


@pytest.fixture
def doctest_env(moto_env, monkeypatch):
    """Extended moto environment for doc example execution.

    Patches RateLimiter and Repository so that doc examples that construct
    their own instances work against moto without CloudFormation.
    """
    from zae_limiter import Limit
    from zae_limiter.exceptions import EntityExistsError
    from zae_limiter.limiter import RateLimiter as _RateLimiter
    from zae_limiter.repository import Repository
    from zae_limiter.sync_limiter import SyncRateLimiter as _SyncRateLimiter

    _created_tables: set[str] = set()

    async def _auto_create_ensure(self):
        """Auto-create DynamoDB table instead of CloudFormation stack."""
        if self.table_name not in _created_tables:
            await self.create_table()
            _created_tables.add(self.table_name)
            # Auto-set system/resource defaults so blocks with limits=None work
            _tmp = _RateLimiter(name=self.table_name, region="us-east-1")
            _tmp._repository = self  # reuse same repo
            await _tmp.set_system_defaults(
                limits=[Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 10_000)],
            )
            for _r in ["gpt-4", "gpt-3.5-turbo", "api", "llm-api", "test"]:
                await _tmp.set_resource_defaults(
                    resource=_r,
                    limits=[Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 10_000)],
                )

    monkeypatch.setattr(Repository, "ensure_infrastructure", _auto_create_ensure)

    # Monkeypatch create_entity to silently ignore EntityExistsError.
    # Doc examples often call create_entity() on pre-existing entities.
    _original_create_entity = _RateLimiter.create_entity

    async def _safe_create_entity(self, *args, **kwargs):
        try:
            return await _original_create_entity(self, *args, **kwargs)
        except EntityExistsError:
            # Entity already exists — return the existing entity
            entity_id = kwargs.get("entity_id") or (args[0] if args else None)
            if entity_id:
                return await self.get_entity(entity_id)
            raise

    monkeypatch.setattr(_RateLimiter, "create_entity", _safe_create_entity)

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
    "test-entity",
    "entity",
    "entity-1",
    "entity-2",
    "project-prod",
    "project-1",
    "api-key",
]


@pytest.fixture
def doctest_globals(doctest_env):
    """Pre-built globals for doc example execution.

    Provides a working limiter, common entities, stub functions,
    and commonly-used imports so doc code blocks can execute.
    """
    import asyncio as _asyncio
    import datetime as _datetime
    import json as _json
    import logging as _logging
    import os as _os
    import time as _time

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

    # Pre-create common entities and set up stored limits
    async def _setup():
        for eid in _COMMON_ENTITIES:
            try:
                await _limiter.create_entity(entity_id=eid)
            except Exception:
                pass
        # Set system defaults so blocks using limits=None can resolve
        await _limiter.set_system_defaults(
            limits=[
                Limit.per_minute("rpm", 100),
                Limit.per_minute("tpm", 10_000),
            ],
        )
        # Set resource defaults for common resources
        for resource in ["gpt-4", "gpt-3.5-turbo", "api", "llm-api", "test"]:
            await _limiter.set_resource_defaults(
                resource=resource,
                limits=[
                    Limit.per_minute("rpm", 100),
                    Limit.per_minute("tpm", 10_000),
                ],
            )

    _asyncio.run(_setup())

    # Common limit objects referenced in doc examples
    _rpm_limit = Limit.per_minute("rpm", 100)
    _tpm_limit = Limit.per_minute("tpm", 10_000)
    _daily_limit = Limit.per_day("rpd", 10_000)

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
        # Standard library modules (inject both module and common class)
        "datetime": _datetime.datetime,
        "json": _json,
        "os": _os,
        "time": _time,
        "logging": _logging,
        "asyncio": _asyncio,
        # Mock objects
        "openai": _MockOpenAI(),
        # Common limit objects
        "rpm_limit": _rpm_limit,
        "tpm_limit": _tpm_limit,
        "daily_limit": _daily_limit,
        "parent_rpm_limit": Limit.per_minute("rpm", 1000),
        "limits": [_rpm_limit],
        # Common variables
        "prompt": "Hello, how are you?",
        "messages": [{"role": "user", "content": "Hello"}],
        "estimated_tokens": 500,
        "input_tokens": 100,
        "max_tokens": 1000,
        "entity_id": "user-123",
        "elapsed_ms": 50,
        "request_id": "req-123",
        "num_shards": 10,
        # Web framework stubs
        "JSONResponse": _JSONResponse,
        "HTTPException": _HTTPError,
        # Context variables used in doc examples
        "logger": _logging.getLogger("doctest"),
        "is_critical_operation": False,
        # Stub functions
        "call_llm": _stub_call_llm,
        "call_api": _stub_call_api,
        "do_work": _stub_do_work,
        "premium_operation": _stub_premium_operation,
        "basic_operation": _stub_basic_operation,
        "execute_operation": _stub_execute_operation,
    }


# ---------------------------------------------------------------------------
# LocalStack integration fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def localstack_limiter():
    """Deploy and provide a RateLimiter stack named 'limiter' on LocalStack.

    This fixture deploys a minimal stack (no aggregator, no alarms) to LocalStack
    for doc examples tagged with 'requires-localstack'. The stack matches what
    the CLI deploy example in docs/contributing/localstack.md creates.

    Session-scoped to avoid redeploying for each test.
    """
    import os

    from zae_limiter import RateLimiter, StackOptions

    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("AWS_ENDPOINT_URL not set (LocalStack not running)")

    limiter = RateLimiter(
        name="limiter",
        endpoint_url=endpoint,
        region="us-east-1",
        stack_options=StackOptions(enable_aggregator=False, enable_alarms=False),
    )

    # Deploy the stack
    import asyncio

    async def _deploy():
        async with limiter:
            pass  # __aenter__ deploys the stack

    asyncio.run(_deploy())

    yield limiter

    # Cleanup after all tests
    async def _cleanup():
        try:
            await limiter.delete_stack()
        except Exception:
            pass

    asyncio.run(_cleanup())
