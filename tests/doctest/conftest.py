"""Configuration for documentation example tests."""

import asyncio

import pytest
from moto import mock_aws

from tests.fixtures.doctest_helpers import (
    COMMON_ENTITIES,
    HTTPError,
    JSONResponse,
    MockOpenAI,
    stub_basic_operation,
    stub_call_api,
    stub_call_llm,
    stub_do_work,
    stub_execute_operation,
    stub_premium_operation,
)
from tests.fixtures.moto import _patch_aiobotocore_response


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
    from zae_limiter.repository import Repository as _Repository
    from zae_limiter.sync_limiter import SyncRateLimiter as _SyncRateLimiter

    _created_tables: set[tuple[str, str | None]] = set()

    _default_limits = [Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 10_000)]
    _default_resources = ["gpt-4", "gpt-3.5-turbo", "api", "llm-api", "test"]

    async def _auto_create_ensure(self):
        """Auto-create DynamoDB table instead of CloudFormation stack."""
        table_key = (self.table_name, self.region)
        if table_key not in _created_tables:
            await self.create_table()
            _created_tables.add(table_key)
            # Register namespaces and set defaults for each
            saved_ns_id = self._namespace_id
            for ns_name in ["default", "tenant-alpha", "tenant-beta"]:
                ns_id = await self._register_namespace(ns_name)
                self._namespace_id = ns_id
                self._reinitialize_config_cache(ns_id)
                _tmp = _RateLimiter(repository=self)
                await _tmp.set_system_defaults(limits=_default_limits)
                for _r in _default_resources:
                    await _tmp.set_resource_defaults(resource=_r, limits=_default_limits)
            # Restore original namespace_id (builder will set the resolved one)
            self._namespace_id = saved_ns_id

    monkeypatch.setattr(_Repository, "ensure_infrastructure", _auto_create_ensure)
    # Builder calls _ensure_infrastructure_internal, not ensure_infrastructure
    monkeypatch.setattr(_Repository, "_ensure_infrastructure_internal", _auto_create_ensure)

    # Set sys.argv for standalone scripts (e.g., migration script with argparse)
    import sys

    monkeypatch.setattr(sys, "argv", ["migrate", "--name", "my-app", "--region", "us-east-1"])

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
        kwargs.pop("stack_options", None)
        _original_init(self, *args, **kwargs)

    monkeypatch.setattr(_RateLimiter, "__init__", _patched_init)

    _original_sync_init = _SyncRateLimiter.__init__

    def _patched_sync_init(self, *args, **kwargs):
        kwargs.pop("stack_options", None)
        _original_sync_init(self, *args, **kwargs)

    monkeypatch.setattr(_SyncRateLimiter, "__init__", _patched_sync_init)


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
    _repo = Repository(name="limiter", region="us-east-1", _skip_deprecation_warning=True)
    _asyncio.run(_repo.create_table())
    _ns_id = _asyncio.run(_repo.register_namespace("default"))
    _repo._namespace_id = _ns_id
    _repo._reinitialize_config_cache(_ns_id)
    _limiter = RateLimiter(repository=_repo)

    # Create "my-app" table used by migration guide examples
    _my_app_repo = Repository(name="my-app", region="us-east-1", _skip_deprecation_warning=True)
    _asyncio.run(_my_app_repo.create_table())
    _my_app_ns_id = _asyncio.run(_my_app_repo.register_namespace("default"))
    _my_app_repo._namespace_id = _my_app_ns_id
    _my_app_repo._reinitialize_config_cache(_my_app_ns_id)

    # Pre-create common entities and set up stored limits
    async def _setup():
        for eid in COMMON_ENTITIES:
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
        # Pre-built limiter and repository
        "limiter": _limiter,
        "repo": _repo,
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
        "openai": MockOpenAI(),
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
        "JSONResponse": JSONResponse,
        "HTTPException": HTTPError,
        # Context variables used in doc examples
        "logger": _logging.getLogger("doctest"),
        "is_critical_operation": False,
        # Stub functions
        "call_llm": stub_call_llm,
        "call_api": stub_call_api,
        "do_work": stub_do_work,
        "premium_operation": stub_premium_operation,
        "basic_operation": stub_basic_operation,
        "execute_operation": stub_execute_operation,
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

    from zae_limiter import RateLimiter, Repository, StackOptions

    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("AWS_ENDPOINT_URL not set (LocalStack not running)")

    repo = Repository(
        name="limiter",
        endpoint_url=endpoint,
        region="us-east-1",
        stack_options=StackOptions(enable_aggregator=False, enable_alarms=False),
    )
    limiter = RateLimiter(repository=repo)

    # Deploy the stack
    async def _deploy():
        async with limiter:
            pass  # __aenter__ deploys the stack

    asyncio.run(_deploy())

    yield limiter

    # Cleanup after all tests
    async def _cleanup():
        try:
            await repo.delete_stack()
        except Exception:
            pass

    asyncio.run(_cleanup())
