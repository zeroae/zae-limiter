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
