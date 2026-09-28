"""Unit test fixtures using moto."""

import pytest

from zae_limiter import RateLimiter, Repository, __version__
from zae_limiter.sync_repository import SyncRepository
from zae_limiter.version import get_schema_version

# Filenames that construct SyncRepository directly to test parallel_mode
# resolution itself; they touch no DynamoDB/moto state (#656).
_PARALLEL_MODE_TEST_FILES = frozenset({"test_parallel_mode.py", "test_parallel_mode_gevent.py"})


@pytest.fixture(autouse=True)
def _serial_sync_repository_for_moto(request, monkeypatch):
    """Pin every default-mode (``parallel_mode="auto"``) ``SyncRepository``
    built under ``tests/unit/`` onto a serial executor (#656).

    moto's in-process backend is not thread-safe: a real ``ThreadPoolExecutor``
    — which is what ``"auto"`` resolves to on any multi-CPU host — racing two
    ``UpdateItem``s against the *same* bucket item (e.g. the session-quota
    window rollover fan-out writing two due limits to one sibling shard) can
    raise "dictionary changed size during iteration" inside moto's own
    ``copy.deepcopy``. Real DynamoDB has no such race.

    ``SyncRepository._resolve_parallel_mode("auto")`` already falls back to a
    serial executor when ``os.cpu_count() == 1``, so pinning ``os.cpu_count``
    for the duration of the test reuses that existing branch instead of
    reimplementing serial dispatch here — and it reaches every moto-backed
    ``SyncRepository`` in this directory, fixture-built or inline-constructed,
    including generated test files (e.g. ``test_sync_repository.py``'s own
    ``repo`` fixture) that cannot take a ``parallel_mode`` kwarg because their
    async source has no such concept.

    Exempted: the files in ``_PARALLEL_MODE_TEST_FILES`` exercise
    ``_resolve_parallel_mode``/``auto``/``threadpool``/``gevent`` resolution
    directly and patch ``os.cpu_count`` themselves — they touch no
    DynamoDB/moto state, so this fixture would only add noise there.
    """
    if request.node.path.name in _PARALLEL_MODE_TEST_FILES:
        yield
        return
    monkeypatch.setattr("os.cpu_count", lambda: 1)
    yield


async def _setup_moto_table(name: str = "test-rate-limits", region: str = "us-east-1") -> None:
    """Create table, default namespace and a deployed stack's version record (#638)."""
    repo = Repository(name=name, region=region, _skip_deprecation_warning=True)
    await repo.create_table()
    await repo._register_namespace("default")
    await repo.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)
    await repo.close()


def _setup_moto_table_sync(name: str = "test-rate-limits", region: str = "us-east-1") -> None:
    """Create table and register default namespace in moto (sync test setup)."""
    repo = SyncRepository(name=name, region=region, _skip_deprecation_warning=True)
    repo.create_table()
    repo._register_namespace("default")
    repo.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)
    repo.close()


@pytest.fixture
async def limiter(mock_dynamodb):
    """Create a RateLimiter with mocked DynamoDB."""
    await _setup_moto_table()
    repo = await Repository.open(stack="test-rate-limits")
    limiter = RateLimiter(repository=repo)
    async with limiter:
        yield limiter


@pytest.fixture
def sync_repository(mock_dynamodb):
    """Create a SyncRepository with mocked DynamoDB."""
    _setup_moto_table_sync()
    repo = SyncRepository.open(stack="test-rate-limits")
    yield repo
    repo.close()
