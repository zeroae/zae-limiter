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
