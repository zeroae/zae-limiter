"""Isolation between stacks deployed side by side, on LocalStack and real AWS (#691).

Users deploy several stacks into one account and region (``my-app-test`` and
``my-app-prod``) and rely on the stack boundary to roll changes out safely:
upgrade the test stack, write new limit shapes there, leave prod alone until
ready. Namespace isolation *within* a stack is covered by ``test_namespace``;
nothing else covers isolation *between* stacks. Three properties, one class
each, every stack deployed with ``--no-aggregator`` so that no stream timing
is involved (the provisioner is still deployed, so "Lambda code unchanged"
applies to ``{B}-limits-provisioner``):

- **Upgrade isolation.** ``zae-limiter upgrade --name A`` and
  ``Repository.open(stack=A, auto_update=True)`` must leave B's version record
  and B's provisioner code alone. Both paths push through ``skip_absent``
  (#644) and stamp through ``stack_lambdas_current``; a function name or probe
  resolving to the wrong stack would push code into B or stamp B from A's push.
- **Ratchet isolation (#638 C, ADR-141).** A ``reset_after`` write on A raises
  A's ``client_min_version``. B's must not move, so a client below the new
  minimum is refused by A and still serves B.
- **In-process isolation.** One process holding a repository for A and one for
  B, with the *same* entity ids, resource names and namespace names, must keep
  buckets, stored limits and config caches apart: a key built from names
  alone, instead of table plus namespace id, would collide here.

Each class deploys its own pair (never ``shared_minimal_stack``, which is one
stack per session, #577) and deletes only the stacks it created. The client
version is pinned the way ``test_upgrade_partial_stacks`` does it: the ambient
checkout version is not predictable (#655), and the ratchet is capped at the
writer's own version for a dev build.

Every class runs twice through the class-scoped ``backend`` fixture: on
LocalStack (marked ``integration``) and on real AWS (marked ``aws``, skipped
without ``--run-aws``). The test bodies are shared; on AWS the stacks also get
the PowerUser IAM flags, and assertions backed by an eventually consistent read
(a GSI, a default ``GetItem``) poll through ``eventually``.

To run on LocalStack::

    zae-limiter local up
    export AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test \\
           AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
    uv run pytest tests/e2e/test_multi_stack_isolation.py -m integration -v

To run on real AWS (``AWS_ENDPOINT_URL`` must be unset)::

    AWS_PROFILE=zeroae-code/AWSPowerUserAccess \\
      uv run pytest tests/e2e/test_multi_stack_isolation.py -m aws --run-aws -v

WARNING: the AWS run creates six real stacks (three pairs, no aggregator, no
alarms) and deletes them.
"""

import os
from datetime import timedelta
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tests.fixtures.stack_pairs import (
    REGION,
    Backend,
    StackPair,
    aws_backend,
    deployed_pair,
    eventually,
    function_config,
    localstack_backend,
    set_stamp,
    settled,
    snapshot,
    version_item,
    wait_until_visible,
    where,
)
from zae_limiter import Limit, RateLimiter, RateLimitExceeded, Repository
from zae_limiter.cli import cli
from zae_limiter.exceptions import VersionMismatchError
from zae_limiter.sync_repository import SyncRepository
from zae_limiter.version import (
    MIN_READER_VERSION_FOR_RESET_AFTER,
    ratcheted_client_min_version,
)

pytestmark = [pytest.mark.e2e]

CLIENT = "0.99.0"
"""The client version the version-sensitive legs run as. Pinned, not read from
``__version__`` (#655); its major matches the schema's, and it is above
``MIN_READER_VERSION_FOR_RESET_AFTER`` so the ratchet reaches that release."""

OLD = "0.1.0"
"""A stamp below ``CLIENT``: the version both stacks of the upgrade pair start at."""

BELOW = "0.14.0"
"""A client below the ratcheted minimum (``MIN_READER_VERSION_FOR_RESET_AFTER``)."""

RESOURCE = "gpt-4"
ENTITY = "user-1"


@pytest.fixture(
    scope="class",
    params=[
        pytest.param("localstack", marks=pytest.mark.integration),
        pytest.param("aws", marks=pytest.mark.aws),
    ],
)
def backend(request) -> Backend:
    """Where this class deploys its pair: LocalStack, or real AWS under ``--run-aws``."""
    if request.param == "localstack":
        return localstack_backend(request.getfixturevalue("localstack_endpoint"))
    if os.getenv("AWS_ENDPOINT_URL"):
        # boto3 honours the variable, so "real AWS" would silently be LocalStack.
        pytest.skip("AWS_ENDPOINT_URL is set; unset it to run against real AWS")
    return aws_backend()


def _client_min(stack: str, endpoint: str | None) -> str | None:
    attr = version_item(stack, endpoint).get("client_min_version")
    return None if attr is None else attr["S"]


def _lambda_version(stack: str, endpoint: str | None) -> str | None:
    attr = version_item(stack, endpoint).get("lambda_version")
    return None if attr is None else attr["S"]


# ---------------------------------------------------------------------------
# 1. Upgrade isolation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="class")
def upgrade_pair(backend, unique_name_class):
    with deployed_pair(unique_name_class, backend, version=OLD) as pair:
        # Pin both stamps rather than trust what a deploy under a patched version wrote.
        set_stamp(pair.a, pair.endpoint_url, OLD)
        set_stamp(pair.b, pair.endpoint_url, OLD)
        yield pair


class TestUpgradeIsolation:
    """Upgrading A leaves B's version record, Lambda code, tags and parameters alone.

    Both stacks are deployed as ``OLD``, so B is genuinely behind the client
    that upgrades A: a leak would have something to change.
    """

    def _assert_b_untouched(self, upgrade_pair: StackPair, before: dict) -> None:
        after = snapshot(upgrade_pair.b, upgrade_pair.endpoint_url)
        assert after["version_item"] == before["version_item"]
        for attribute in ("lambda_version", "client_min_version", "schema_version"):
            assert after["version_item"][attribute] == before["version_item"][attribute]
        assert after["provisioner"]["CodeSha256"] == before["provisioner"]["CodeSha256"]
        assert after["provisioner"]["LastModified"] == before["provisioner"]["LastModified"]
        assert after == before

    def test_the_cli_upgrade_of_a_leaves_b_alone(self, upgrade_pair):
        endpoint = upgrade_pair.endpoint_url
        before = snapshot(upgrade_pair.b, endpoint)
        assert before["version_item"]["lambda_version"]["S"] == OLD

        # An unknown stamp: open() never updates on one, so the CLI's own push
        # and stamp steps are what run (no --force needed).
        a = upgrade_pair.a
        set_stamp(a, endpoint, None)
        a_before = function_config(upgrade_pair.a, endpoint)
        with patch("zae_limiter.__version__", CLIENT):
            result = CliRunner().invoke(
                cli,
                ["upgrade", *where(upgrade_pair.a, endpoint)],
            )
        assert result.exit_code == 0, f"Upgrade failed: {result.output}"
        assert "Upgrade complete" in result.output

        assert _lambda_version(upgrade_pair.a, endpoint) == CLIENT
        # The upgrade really pushed to A's provisioner, so B staying put is not
        # because nothing moved anywhere.
        assert function_config(upgrade_pair.a, endpoint)["LastModified"] != a_before["LastModified"]
        self._assert_b_untouched(upgrade_pair, before)

    def test_open_with_auto_update_of_a_leaves_b_alone(self, upgrade_pair):
        endpoint = upgrade_pair.endpoint_url
        before = snapshot(upgrade_pair.b, endpoint)

        # A faked library upgrade: A's stamp is behind the client that opens it.
        set_stamp(upgrade_pair.a, endpoint, OLD)
        a_before = function_config(upgrade_pair.a, endpoint)
        with patch("zae_limiter.__version__", CLIENT):
            repo = SyncRepository.open(stack=upgrade_pair.a, region=REGION, endpoint_url=endpoint)
            repo.close()

        assert _lambda_version(upgrade_pair.a, endpoint) == CLIENT
        assert function_config(upgrade_pair.a, endpoint)["LastModified"] != a_before["LastModified"]
        self._assert_b_untouched(upgrade_pair, before)


# ---------------------------------------------------------------------------
# 2. client_min_version ratchet isolation
# ---------------------------------------------------------------------------


@pytest.fixture(scope="class")
def ratchet_pair(backend, unique_name_class):
    with deployed_pair(unique_name_class, backend, version=CLIENT) as pair:
        # Both stamped current, so the reset_after gate admits a write on either.
        set_stamp(pair.a, pair.endpoint_url, CLIENT)
        set_stamp(pair.b, pair.endpoint_url, CLIENT)
        yield pair


@pytest.fixture(scope="class")
def ratcheted(ratchet_pair):
    """The pair after one ``reset_after`` write on A (and a plain limit on B)."""
    endpoint = ratchet_pair.endpoint_url
    before_b = version_item(ratchet_pair.b, endpoint)
    with patch("zae_limiter.__version__", CLIENT):
        repo_a = SyncRepository.open(stack=ratchet_pair.a, region=REGION, endpoint_url=endpoint)
        repo_b = SyncRepository.open(stack=ratchet_pair.b, region=REGION, endpoint_url=endpoint)
        try:
            repo_a.set_resource_defaults(
                RESOURCE, [Limit.quota("session", 100, reset_after=timedelta(hours=1))]
            )
            # B gets a limit with no reset_after: the pinned-client leg acquires on it.
            repo_b.set_resource_defaults(RESOURCE, [Limit.per_minute("rpm", 100)])
        finally:
            repo_a.close()
            repo_b.close()
    # connect() and open() read the record eventually consistently: let the
    # ratchet reach every replica before a test asks A to refuse a client.
    expected = ratcheted_client_min_version(None, CLIENT)
    wait_until_visible(
        ratchet_pair.a,
        endpoint,
        lambda item: item.get("client_min_version", {}).get("S") == expected,
    )
    return before_b


class TestClientMinVersionRatchetIsolation:
    """A ``reset_after`` write on A ratchets A's minimum and never B's."""

    def test_the_write_on_a_ratchets_a_and_not_b(self, ratchet_pair, ratcheted):
        endpoint = ratchet_pair.endpoint_url
        expected = ratcheted_client_min_version(None, CLIENT)

        assert expected == MIN_READER_VERSION_FOR_RESET_AFTER
        assert _client_min(ratchet_pair.a, endpoint) == expected
        # B's whole version record is exactly as it was before the write on A.
        assert version_item(ratchet_pair.b, endpoint) == ratcheted
        assert _client_min(ratchet_pair.b, endpoint) != expected

    async def test_a_client_below_the_minimum_is_refused_by_a(self, ratchet_pair, ratcheted):
        endpoint = ratchet_pair.endpoint_url
        a_before = version_item(ratchet_pair.a, endpoint)

        with patch("zae_limiter.__version__", BELOW):
            with pytest.raises(VersionMismatchError) as connect_error:
                await Repository.connect(stack=ratchet_pair.a, region=REGION, endpoint_url=endpoint)
            with pytest.raises(VersionMismatchError) as open_error:
                await Repository.open(
                    stack=ratchet_pair.a, region=REGION, endpoint_url=endpoint, auto_update=True
                )

        for error in (connect_error.value, open_error.value):
            assert not error.can_auto_update, "a newer client is the fix, not a Lambda deploy"
            assert MIN_READER_VERSION_FOR_RESET_AFTER in str(error)
        # Refused, not repaired: nothing about A moved.
        assert version_item(ratchet_pair.a, endpoint) == a_before

    async def test_the_same_client_opens_b_and_acquires(self, ratchet_pair, ratcheted):
        endpoint = ratchet_pair.endpoint_url
        b_before = version_item(ratchet_pair.b, endpoint)

        with patch("zae_limiter.__version__", BELOW):
            connected = await Repository.connect(
                stack=ratchet_pair.b, region=REGION, endpoint_url=endpoint
            )
            opened = await Repository.open(
                stack=ratchet_pair.b, region=REGION, endpoint_url=endpoint, auto_update=True
            )
            try:
                for repo in (connected, opened):
                    # B's limit was written by the ``ratcheted`` fixture; a fresh
                    # repository's first config read can still miss it on AWS.
                    await settled(
                        repo, lambda: repo.get_resource_defaults(RESOURCE), _capacities(100)
                    )
                    limiter = RateLimiter(repository=repo)
                    async with limiter.acquire(ENTITY, RESOURCE, consume={"rpm": 1}) as lease:
                        assert lease.consumed == {"rpm": 1}
            finally:
                await connected.close()
                await opened.close()

        assert version_item(ratchet_pair.b, endpoint) == b_before


# ---------------------------------------------------------------------------
# 3. In-process isolation
# ---------------------------------------------------------------------------

A_CAPACITY = 5
B_CAPACITY = 100
ONE_HOUR = 3600


def _limits(capacity: int) -> list[Limit]:
    """Slow refill (one token per hour): a slow LocalStack call cannot refill a drained bucket."""
    return [Limit.custom("rpm", capacity, 1, ONE_HOUR)]


def _capacities(capacity: int):
    """A ``settled`` predicate: the stored limits are exactly one ``rpm`` of ``capacity``."""
    return lambda limits: [limit.capacity for limit in limits] == [capacity]


@pytest.fixture(scope="class")
def process_pair(backend, unique_name_class):
    with deployed_pair(unique_name_class, backend, version=CLIENT) as pair:
        yield pair


class TestInProcessIsolation:
    """One process, one event loop, a repository per stack, identical names."""

    @pytest.fixture
    async def repos(self, process_pair):
        """``(repo_a, repo_b)`` on the test's loop, config caches that outlive the test."""
        opened = []
        try:
            for stack in (process_pair.a, process_pair.b):
                with patch("zae_limiter.__version__", CLIENT):
                    opened.append(
                        await Repository.open(
                            stack=stack,
                            region=REGION,
                            endpoint_url=process_pair.endpoint_url,
                            config_cache_ttl=300,
                        )
                    )
            yield opened[0], opened[1]
        finally:
            for repo in opened:
                await repo.close()

    async def test_consumption_and_rejection_stay_on_their_own_stack(self, repos):
        repo_a, repo_b = repos
        # Same entity id and resource, different stored limits: A is tiny, B is not.
        await repo_a.set_resource_defaults(RESOURCE, _limits(A_CAPACITY))
        await repo_b.set_resource_defaults(RESOURCE, _limits(B_CAPACITY))
        await settled(
            repo_a, lambda: repo_a.get_resource_defaults(RESOURCE), _capacities(A_CAPACITY)
        )
        await settled(
            repo_b, lambda: repo_b.get_resource_defaults(RESOURCE), _capacities(B_CAPACITY)
        )
        limiter_a, limiter_b = RateLimiter(repository=repo_a), RateLimiter(repository=repo_b)

        # B has never been touched: full capacity, and no bucket for the entity.
        untouched = await limiter_b.check_availability(ENTITY, RESOURCE)
        assert untouched.available == {"rpm": B_CAPACITY}
        assert await repo_b.get_buckets(ENTITY) == []

        async with limiter_a.acquire(ENTITY, RESOURCE, consume={"rpm": A_CAPACITY}):
            pass
        with pytest.raises(RateLimitExceeded) as rejected:
            async with limiter_a.acquire(ENTITY, RESOURCE, consume={"rpm": 1}):
                pass
        assert {v.limit_name for v in rejected.value.violations} == {"rpm"}

        # A is drained, and none of it shows on B: still full, still no bucket.
        drained = await eventually(
            lambda: limiter_a.check_availability(ENTITY, RESOURCE),
            lambda availability: availability.available == {"rpm": 0},
        )
        assert drained.available == {"rpm": 0}
        after = await limiter_b.check_availability(ENTITY, RESOURCE)
        assert after.available == {"rpm": B_CAPACITY}
        assert after.allowed
        assert await repo_b.get_buckets(ENTITY) == []

        # ... and B admits the very same request, in the same loop.
        async with limiter_b.acquire(ENTITY, RESOURCE, consume={"rpm": 1}):
            pass
        (bucket_b,) = await eventually(
            lambda: repo_b.get_buckets(ENTITY, RESOURCE), lambda buckets: len(buckets) == 1
        )
        assert bucket_b.capacity_milli == B_CAPACITY * 1000
        assert bucket_b.tokens_milli == (B_CAPACITY - 1) * 1000
        (bucket_a,) = await eventually(
            lambda: repo_a.get_buckets(ENTITY, RESOURCE), lambda buckets: len(buckets) == 1
        )
        assert bucket_a.capacity_milli == A_CAPACITY * 1000
        assert bucket_a.tokens_milli < 1000
        spent_on_b = await eventually(
            lambda: limiter_b.check_availability(ENTITY, RESOURCE),
            lambda availability: availability.available == {"rpm": B_CAPACITY - 1},
        )
        assert spent_on_b.available == {"rpm": B_CAPACITY - 1}

    async def test_a_config_write_on_a_does_not_reach_a_warm_cache_on_b(self, repos):
        repo_a, repo_b = repos
        await repo_a.set_resource_defaults(RESOURCE, _limits(A_CAPACITY))
        await repo_b.set_resource_defaults(RESOURCE, _limits(B_CAPACITY))
        await settled(
            repo_a, lambda: repo_a.get_resource_defaults(RESOURCE), _capacities(A_CAPACITY)
        )
        await settled(
            repo_b, lambda: repo_b.get_resource_defaults(RESOURCE), _capacities(B_CAPACITY)
        )

        # Warm both caches, then confirm each is serving from itself.
        for repo in (repo_a, repo_b):
            await repo.resolve_limits(ENTITY, RESOURCE)
        stats_b = repo_b.get_cache_stats()
        limits_b, _, _ = await repo_b.resolve_limits(ENTITY, RESOURCE)
        assert repo_b.get_cache_stats().hits > stats_b.hits, "B is serving from its warm cache"
        stats_b = repo_b.get_cache_stats()

        # A's limits change; B is never told (no invalidate_config_cache() on B).
        await repo_a.set_resource_defaults(RESOURCE, _limits(A_CAPACITY + 2))
        stored_a = await eventually(
            lambda: repo_a.get_resource_defaults(RESOURCE),
            lambda limits: [limit.capacity for limit in limits] == [A_CAPACITY + 2],
        )
        assert [limit.capacity for limit in stored_a] == [A_CAPACITY + 2]

        # A resource-level write does not evict the writer's own cache either (it
        # propagates by TTL, ADR-122), so A is evicted by hand to see the change.
        async def resolve_a_fresh():
            await repo_a.invalidate_config_cache()
            return await repo_a.resolve_limits(ENTITY, RESOURCE)

        limits_a, _, _ = await eventually(
            resolve_a_fresh,
            lambda resolved: [limit.capacity for limit in resolved[0] or []] == [A_CAPACITY + 2],
        )
        assert limits_a is not None
        assert [limit.capacity for limit in limits_a] == [A_CAPACITY + 2]

        # B's warm entry survived both the write and A's eviction untouched: same
        # limits, no miss, no refill, served from the cache.
        limits_b_after, _, _ = await repo_b.resolve_limits(ENTITY, RESOURCE)
        assert limits_b_after is not None
        assert limits_b_after == limits_b
        assert [limit.capacity for limit in limits_b_after] == [B_CAPACITY]
        stats_after = repo_b.get_cache_stats()
        assert stats_after.misses == stats_b.misses
        assert stats_after.hits > stats_b.hits
        assert stats_after.size == stats_b.size

    async def test_default_namespaces_resolve_to_different_ids(self, repos):
        repo_a, repo_b = repos
        assert repo_a.namespace_name == repo_b.namespace_name == "default"
        assert repo_a.namespace_id != repo_b.namespace_id

        # Each stack's registry knows its own id for the name, and only that.
        listed_a = await eventually(
            repo_a.list_namespaces, lambda names: any(n["name"] == "default" for n in names)
        )
        listed_b = await eventually(
            repo_b.list_namespaces, lambda names: any(n["name"] == "default" for n in names)
        )
        registry_a = {n["name"]: n["namespace_id"] for n in listed_a}
        registry_b = {n["name"]: n["namespace_id"] for n in listed_b}
        assert registry_a["default"] == repo_a.namespace_id
        assert registry_b["default"] == repo_b.namespace_id

    async def test_a_namespace_registered_on_both_holds_separate_data(self, repos):
        repo_a, repo_b = repos
        id_a = await repo_a.register_namespace("shared-name")
        id_b = await repo_b.register_namespace("shared-name")
        assert id_a != id_b

        scoped_a = await repo_a.namespace("shared-name")
        scoped_b = await repo_b.namespace("shared-name")
        assert scoped_a.namespace_id == id_a
        assert scoped_b.namespace_id == id_b

        # Data written under A's namespace: an entity, stored limits, a bucket.
        await scoped_a.create_entity(ENTITY, name="only on A")
        await scoped_a.set_limits(ENTITY, _limits(A_CAPACITY), resource=RESOURCE)
        await settled(
            scoped_a, lambda: scoped_a.get_limits(ENTITY, RESOURCE), _capacities(A_CAPACITY)
        )
        limiter_a = RateLimiter(repository=scoped_a)
        async with limiter_a.acquire(ENTITY, RESOURCE, consume={"rpm": 1}):
            pass
        entity_a = await eventually(lambda: scoped_a.get_entity(ENTITY), lambda e: e is not None)
        assert entity_a is not None
        buckets_a = await eventually(
            lambda: scoped_a.get_buckets(ENTITY, RESOURCE), lambda buckets: len(buckets) == 1
        )
        assert len(buckets_a) == 1

        # ... none of it visible under the same name on B.
        assert await scoped_b.get_entity(ENTITY) is None
        assert await scoped_b.get_limits(ENTITY, RESOURCE) == []
        assert await scoped_b.get_buckets(ENTITY) == []
        await scoped_b.set_resource_defaults(RESOURCE, _limits(A_CAPACITY))
        await settled(
            scoped_b, lambda: scoped_b.get_resource_defaults(RESOURCE), _capacities(A_CAPACITY)
        )
        limiter_b = RateLimiter(repository=scoped_b)
        availability = await limiter_b.check_availability(ENTITY, RESOURCE)
        assert availability.available == {"rpm": A_CAPACITY}
        # ... and the two registries share no id.
        listed_a = await eventually(
            repo_a.list_namespaces,
            lambda names: {n["name"] for n in names} >= {"default", "shared-name"},
        )
        assert {n["name"] for n in listed_a} >= {"default", "shared-name"}
        assert {n["namespace_id"] for n in await repo_a.list_namespaces()}.isdisjoint(
            {n["namespace_id"] for n in await repo_b.list_namespaces()}
        )
