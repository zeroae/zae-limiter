"""Integration tests for declarative limits provisioner (LocalStack).

To run these tests locally:
    # Start LocalStack
    docker compose up -d

    # Set environment variables and run tests
    export AWS_ENDPOINT_URL=http://localhost:4566
    export AWS_ACCESS_KEY_ID=test
    export AWS_SECRET_ACCESS_KEY=test
    export AWS_DEFAULT_REGION=us-east-1
    pytest tests/integration/test_provisioner.py -v
"""

import pytest

from zae_limiter.models import Limit
from zae_limiter.schema import LIMIT_FIELD_SCHED, limit_attr, pk_system, sk_config
from zae_limiter_provisioner.applier import apply_changes
from zae_limiter_provisioner.differ import compute_diff
from zae_limiter_provisioner.handler import _handle_cfn, _handle_cli
from zae_limiter_provisioner.manifest import LimitsManifest

pytestmark = pytest.mark.integration


class TestProvisionerIntegration:
    """Full provisioner workflow against LocalStack."""

    @pytest.mark.asyncio
    async def test_apply_creates_and_reads_back(self, test_repo):
        """Apply creates limits that are readable via Repository API."""
        manifest = LimitsManifest.from_dict(
            {
                "namespace": "test",
                "system": {
                    "on_unavailable": "allow",
                    "limits": {"rpm": {"capacity": 1000}},
                },
                "resources": {
                    "gpt-4": {"limits": {"tpm": {"capacity": 50000}}},
                },
            }
        )
        previous = {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {},
        }
        changes = compute_diff(manifest, previous)

        result = apply_changes(changes, test_repo.table_name, test_repo._namespace_id)
        assert result.created == 2
        assert result.errors == []

        # Verify via Repository API
        system_limits, on_unavailable = await test_repo.get_system_defaults()
        assert any(lim.name == "rpm" and lim.capacity == 1000 for lim in system_limits)

        resource_limits = await test_repo.get_resource_defaults("gpt-4")
        assert any(lim.name == "tpm" and lim.capacity == 50000 for lim in resource_limits)

    @pytest.mark.asyncio
    async def test_idempotent_apply(self, test_repo):
        """Applying the same manifest twice produces update actions."""
        manifest = LimitsManifest.from_dict(
            {
                "namespace": "test",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
            }
        )

        # First apply
        previous = {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {},
        }
        changes1 = compute_diff(manifest, previous)
        apply_changes(changes1, test_repo.table_name, test_repo._namespace_id)

        # Second apply (same manifest, updated previous state)
        new_previous = manifest.managed_set()
        changes2 = compute_diff(manifest, new_previous)
        result2 = apply_changes(changes2, test_repo.table_name, test_repo._namespace_id)
        assert result2.updated == 1
        assert result2.created == 0
        assert result2.deleted == 0

    @pytest.mark.asyncio
    async def test_removal_deletes_managed_only(self, test_repo):
        """Removing from YAML deletes managed items, leaves unmanaged alone."""
        # Set an unmanaged resource limit directly
        await test_repo.set_resource_defaults("claude-3", [Limit.per_minute("rpm", 500)])

        # Apply manifest with gpt-4 only
        manifest = LimitsManifest.from_dict(
            {
                "namespace": "test",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
            }
        )
        previous = {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {},
        }
        changes = compute_diff(manifest, previous)
        apply_changes(changes, test_repo.table_name, test_repo._namespace_id)

        # Now remove gpt-4 from manifest
        manifest2 = LimitsManifest.from_dict({"namespace": "test"})
        previous2 = manifest.managed_set()
        changes2 = compute_diff(manifest2, previous2)
        result = apply_changes(changes2, test_repo.table_name, test_repo._namespace_id)

        assert result.deleted == 1  # gpt-4 deleted

        # claude-3 (unmanaged) should still exist
        claude_limits = await test_repo.get_resource_defaults("claude-3")
        assert any(lim.name == "rpm" for lim in claude_limits)


class TestHandlerIntegration:
    """Test the Lambda handler functions against LocalStack DynamoDB."""

    def _cli_event(
        self,
        action: str,
        table_name: str,
        namespace_id: str,
        manifest: dict,
    ) -> dict:
        return {
            "action": action,
            "table_name": table_name,
            "namespace_id": namespace_id,
            "manifest": manifest,
        }

    def test_handler_plan_returns_changes(self, test_repo):
        """Plan action returns changes without modifying state."""
        manifest = {
            "namespace": "test",
            "system": {"limits": {"rpm": {"capacity": 500}}},
            "resources": {"gpt-4": {"limits": {"tpm": {"capacity": 25000}}}},
        }
        event = self._cli_event("plan", test_repo.table_name, test_repo._namespace_id, manifest)
        result = _handle_cli(event, None)

        assert result["status"] == "planned"
        assert len(result["changes"]) == 2
        actions = {c["action"] for c in result["changes"]}
        assert "create" in actions

    @pytest.mark.asyncio
    async def test_handler_apply_persists_state(self, test_repo):
        """Apply persists provisioner state; subsequent plan shows updates."""
        manifest = {
            "namespace": "test",
            "system": {"limits": {"rpm": {"capacity": 800}}},
        }

        # First apply
        event = self._cli_event("apply", test_repo.table_name, test_repo._namespace_id, manifest)
        result = _handle_cli(event, None)
        assert result["status"] == "applied"
        assert result["created"] == 1
        assert result["errors"] == []

        # Verify via Repository API
        system_limits, _ = await test_repo.get_system_defaults()
        assert any(lim.name == "rpm" and lim.capacity == 800 for lim in system_limits)

        # Second plan should show update (not create) due to persisted state
        result2 = _handle_cli(event, None)
        assert result2["status"] == "applied"
        assert result2["updated"] == 1
        assert result2["created"] == 0

    @pytest.mark.asyncio
    async def test_handler_apply_entity_limits(self, test_repo):
        """Apply creates entity-level limits readable via Repository API."""
        manifest = {
            "namespace": "test",
            "entities": {
                "user-premium": {
                    "resources": {
                        "gpt-4": {"limits": {"rpm": {"capacity": 2000}}},
                    },
                },
            },
        }
        event = self._cli_event("apply", test_repo.table_name, test_repo._namespace_id, manifest)
        result = _handle_cli(event, None)
        assert result["created"] == 1
        assert result["errors"] == []

        # Verify via Repository API
        entity_limits = await test_repo.get_limits("user-premium", "gpt-4")
        assert any(lim.name == "rpm" and lim.capacity == 2000 for lim in entity_limits)

    @pytest.mark.asyncio
    async def test_handler_removal_flow(self, test_repo):
        """Apply then remove items: handler tracks state and deletes correctly."""
        # Apply full manifest
        manifest_full = {
            "namespace": "test",
            "system": {"limits": {"rpm": {"capacity": 600}}},
            "resources": {
                "gpt-4": {"limits": {"tpm": {"capacity": 40000}}},
                "claude-3": {"limits": {"tpm": {"capacity": 30000}}},
            },
        }
        event_full = self._cli_event(
            "apply", test_repo.table_name, test_repo._namespace_id, manifest_full
        )
        r1 = _handle_cli(event_full, None)
        assert r1["created"] == 3
        assert r1["errors"] == []

        # Apply reduced manifest (remove claude-3 and system)
        manifest_reduced = {
            "namespace": "test",
            "resources": {"gpt-4": {"limits": {"tpm": {"capacity": 40000}}}},
        }
        event_reduced = self._cli_event(
            "apply", test_repo.table_name, test_repo._namespace_id, manifest_reduced
        )
        r2 = _handle_cli(event_reduced, None)
        assert r2["deleted"] == 2  # system + claude-3
        assert r2["updated"] == 1  # gpt-4

        # Verify gpt-4 still exists
        gpt4_limits = await test_repo.get_resource_defaults("gpt-4")
        assert any(lim.name == "tpm" for lim in gpt4_limits)

        # Verify claude-3 is gone
        claude_limits = await test_repo.get_resource_defaults("claude-3")
        assert claude_limits == []

    def test_handler_cfn_create_and_delete(self, test_repo):
        """CFN Create event applies limits; Delete event removes them."""
        # CFN Create
        cfn_create = {
            "RequestType": "Create",
            "ResourceProperties": {
                "TableName": test_repo.table_name,
                "NamespaceId": test_repo._namespace_id,
                "Namespace": "test",
                "System": {
                    "OnUnavailable": "block",
                    "Limits": {"rpm": {"Capacity": 1500}},
                },
                "Resources": {
                    "gpt-4": {"Limits": {"tpm": {"Capacity": 60000}}},
                },
            },
        }
        r1 = _handle_cfn(cfn_create, None)
        assert r1["created"] == 2
        assert r1["errors"] == []

        # CFN Delete — removes all managed items
        cfn_delete = {
            "RequestType": "Delete",
            "ResourceProperties": {
                "TableName": test_repo.table_name,
                "NamespaceId": test_repo._namespace_id,
                "Namespace": "test",
            },
        }
        r2 = _handle_cfn(cfn_delete, None)
        assert r2["deleted"] == 2

    @pytest.mark.asyncio
    async def test_handler_cfn_create_with_stringified_properties(self, test_repo):
        """The same Create event as delivered by real CloudFormation (#554).

        Every other custom-resource test in this file (and every unit test
        before #554) builds `ResourceProperties` out of native Python types —
        `{"Capacity": 1500, "Disabled": False}` — which is a shape CloudFormation
        cannot produce. It stringifies every scalar leaf, so `'1500' <= 0` raised
        `TypeError` and `bool('false')` was `True`, silently disabling the
        resource an operator was explicitly re-admitting (ADR-125 carve-out).

        This drives the stringified payload all the way into DynamoDB and reads
        the result back through the async `Repository`, so the assertions are
        against stored state rather than against the converter's return value.
        """
        cfn_create = {
            "RequestType": "Create",
            "ResourceProperties": {
                "TableName": test_repo.table_name,
                "NamespaceId": test_repo._namespace_id,
                "Namespace": "test",
                "System": {
                    "OnUnavailable": "block",
                    "Limits": {"rpm": {"Capacity": "1500", "RefillPeriod": "60"}},
                },
                "Resources": {
                    # The carve-out shape: an explicit re-admission carrying no
                    # numeric field of its own, so nothing trips a TypeError and
                    # the inversion is silent.
                    "gpt-4": {
                        "Disabled": "false",
                        "Limits": {"tpm": {"Capacity": "60000", "RefillAmount": "60000"}},
                    },
                    "legacy": {"Disabled": "true"},
                },
            },
        }
        result = _handle_cfn(cfn_create, None)
        assert result["errors"] == []
        assert result["created"] == 3

        system_limits, on_unavailable = await test_repo.get_system_defaults()
        assert on_unavailable == "block"
        rpm = next(limit for limit in system_limits if limit.name == "rpm")
        assert rpm.capacity == 1500
        assert isinstance(rpm.capacity, int)
        assert rpm.refill_period_seconds == 60

        tpm = next(
            limit for limit in await test_repo.get_resource_defaults("gpt-4") if limit.name == "tpm"
        )
        assert tpm.capacity == 60000
        assert isinstance(tpm.capacity, int)

        # The headline assertion: `'false'` resolved to a real False, so the
        # resource is enabled. Before the fix this stored disabled=True.
        assert await test_repo.resolve_disabled("anyone", "gpt-4") == (False, "resource")
        assert await test_repo.resolve_disabled("anyone", "legacy") == (True, "resource")

    @pytest.mark.asyncio
    async def test_handler_on_unavailable_persisted(self, test_repo):
        """System on_unavailable setting is persisted and readable."""
        manifest = {
            "namespace": "test",
            "system": {
                "on_unavailable": "allow",
                "limits": {"rpm": {"capacity": 300}},
            },
        }
        event = self._cli_event("apply", test_repo.table_name, test_repo._namespace_id, manifest)
        _handle_cli(event, None)

        _, on_unavailable = await test_repo.get_system_defaults()
        assert on_unavailable == "allow"

    @pytest.mark.asyncio
    async def test_apply_with_existing_bucket_stamps_it_and_persists_state(
        self, test_repo, localstack_limiter
    ):
        """An apply that fans out over a live bucket must complete fully.

        Every other handler test in this file applies a manifest to a
        namespace with no bucket items, so `_fanout_disabled_changes` finds
        nothing to stamp and `fanout.stamp_bucket` -- the only `update_item`
        call in the provisioner package -- never runs. That gap is what let
        the missing `dynamodb:UpdateItem` grant on `ProvisionerRole` reach
        `main`: the code path was only ever exercised against an empty table.

        This test puts a real bucket in the table first, so the apply
        actually reaches `update_item` against real DynamoDB, and then
        asserts both halves of the operation landed:

        1. the bucket carries the resource's `disabled` value, and
        2. the `#PROVISIONER` state record was written.

        The ordering in `_handle_cli` is apply -> fan-out -> write state, so
        anything the fan-out raises aborts the handler *after* the config
        items are written but *before* the state record is, leaving managed
        state silently out of step with the table. Asserting (2) after a
        fan-out that had real work to do is what catches that.

        This test deliberately does NOT claim to cover the IAM grant, and
        cannot: `_handle_cli` runs in-process with the test suite's own
        credentials, so `ProvisionerRole` is never assumed. LocalStack could
        not enforce it anyway -- `ENFORCE_IAM` is Pro-gated, and community
        edition accepts the flag and ignores it (verified against 4.14: a
        role without `dynamodb:UpdateItem` still performs the update).

        The grant is covered in two other places instead:
        `tests/unit/test_cfn_iam_parity.py::TestProvisionerRoleParity` guards
        the template statically, and
        `tests/e2e/test_aws.py::TestE2EAWSProvisionerCFNStack::test_provisioner_lambda_can_stamp_a_live_bucket`
        invokes the deployed Lambda under its real role on AWS.
        """
        await test_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 1_000)])
        async with localstack_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass

        buckets = await test_repo.get_buckets("user-1", resource="gpt-4")
        assert buckets, "a bucket must exist before the apply for the fan-out to have work"

        manifest = {
            "namespace": "test",
            "resources": {"gpt-4": {"disabled": True, "limits": {"rpm": {"capacity": 1000}}}},
        }
        event = self._cli_event("apply", test_repo.table_name, test_repo._namespace_id, manifest)
        result = _handle_cli(event, None)

        assert result["status"] == "applied"
        assert result["errors"] == []

        # 1. the fan-out reached the live bucket
        assert await test_repo.get_resource_disabled("gpt-4") is True

        # 2. the handler got past the fan-out and persisted its state
        state = await test_repo.get_provisioner_state()
        assert state is not None, (
            "#PROVISIONER was not written -- the handler aborted during fan-out, "
            "so managed state has drifted from what apply_changes wrote"
        )
        assert "gpt-4" in state.get("managed_resources", [])

    @pytest.mark.asyncio
    async def test_undecodable_stored_schedule_still_records_state(self, test_repo):
        """#563: a post-commit fan-out failure must not lose `#PROVISIONER`.

        `bucket_sync._decode_limits` raises on a stored compact schedule this
        provisioner cannot read (PR #549, deliberately). Reaching that from the
        handler needs a level the apply does NOT rewrite, or `apply_changes`'
        own `put_item` overwrites the corrupt item before the sync reads it and
        the whole scenario passes for the wrong reason. System is such a level:
        an apply that drops an entity's config reconciles that entity down
        through entity(`_default_`) -> resource -> system, and rewrites none of
        them.

        Against moto this is pinned in `tests/unit/test_provisioner_handler.py`;
        here the config item, the walk and the `#PROVISIONER` record are all
        real DynamoDB.
        """
        client = await test_repo._get_client()
        ns = test_repo._namespace_id

        await test_repo.set_system_defaults([Limit.per_minute("rpm", 1_000)])
        await client.update_item(
            TableName=test_repo.table_name,
            Key={"PK": {"S": pk_system(ns)}, "SK": {"S": sk_config()}},
            UpdateExpression="SET #a = :v",
            ExpressionAttributeNames={"#a": limit_attr("rpm", LIMIT_FIELD_SCHED)},
            ExpressionAttributeValues={":v": {"S": "zz!!garbage"}},
        )

        # Apply 1: takes ownership of an entity config. Scoped to "gpt-4", so
        # the sync plans from the manifest and never walks up to system.
        first = self._cli_event(
            "apply",
            test_repo.table_name,
            ns,
            {
                "namespace": "test",
                "entities": {
                    "user-1": {"resources": {"gpt-4": {"limits": {"rpm": {"capacity": 2000}}}}}
                },
            },
        )
        r1 = _handle_cli(first, None)
        assert r1["errors"] == []
        assert (await test_repo.get_provisioner_state())["managed_entities"] == {
            "user-1": ["gpt-4"]
        }

        # Apply 2: drops it. The delete commits, then the reconciliation walk
        # reaches the corrupt system item and raises.
        second = self._cli_event("apply", test_repo.table_name, ns, {"namespace": "test"})
        r2 = _handle_cli(second, None)

        assert r2["status"] == "applied"
        assert r2["deleted"] == 1
        assert any("user-1/gpt-4" in e for e in r2["errors"]), r2["errors"]

        # The config write committed...
        assert await test_repo.get_limits("user-1", "gpt-4") == []
        # ...so the record must describe it, not the previous apply.
        assert (await test_repo.get_provisioner_state())["managed_entities"] == {}
