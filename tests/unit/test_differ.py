"""Tests for the provisioner diff engine."""

from zae_limiter_provisioner.differ import compute_diff
from zae_limiter_provisioner.manifest import LimitsManifest


class TestComputeDiff:
    """Tests for diff computation between manifest and previous state."""

    def test_first_apply_creates_everything(self):
        """First apply (empty previous state) creates all items."""
        manifest = LimitsManifest.from_dict(
            {
                "namespace": "ns",
                "system": {"limits": {"rpm": {"capacity": 1000}}},
                "resources": {"gpt-4": {"limits": {"tpm": {"capacity": 50000}}}},
                "entities": {
                    "user-1": {
                        "resources": {
                            "gpt-4": {"limits": {"rpm": {"capacity": 500}}},
                        },
                    },
                },
            }
        )
        previous = {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {},
        }

        changes = compute_diff(manifest, previous)
        actions = {(c.level, c.target, c.action) for c in changes}

        assert ("system", None, "create") in actions
        assert ("resource", "gpt-4", "create") in actions
        assert ("entity", "user-1/gpt-4", "create") in actions

    def test_no_changes_on_same_state(self):
        """Re-applying same manifest produces update actions (idempotent overwrites)."""
        manifest = LimitsManifest.from_dict(
            {
                "namespace": "ns",
                "system": {"limits": {"rpm": {"capacity": 1000}}},
                "resources": {"gpt-4": {"limits": {"tpm": {"capacity": 50000}}}},
            }
        )
        previous = {
            "managed_system": True,
            "managed_resources": ["gpt-4"],
            "managed_entities": {},
        }

        changes = compute_diff(manifest, previous)
        actions = {(c.level, c.target, c.action) for c in changes}

        assert ("system", None, "update") in actions
        assert ("resource", "gpt-4", "update") in actions

    def test_removed_resource_produces_delete(self):
        """Resource in previous but not in manifest produces delete."""
        manifest = LimitsManifest.from_dict({"namespace": "ns"})
        previous = {
            "managed_system": False,
            "managed_resources": ["gpt-4"],
            "managed_entities": {},
        }

        changes = compute_diff(manifest, previous)
        actions = {(c.level, c.target, c.action) for c in changes}

        assert ("resource", "gpt-4", "delete") in actions

    def test_removed_system_produces_delete(self):
        """System in previous but not in manifest produces delete."""
        manifest = LimitsManifest.from_dict({"namespace": "ns"})
        previous = {
            "managed_system": True,
            "managed_resources": [],
            "managed_entities": {},
        }

        changes = compute_diff(manifest, previous)
        actions = {(c.level, c.target, c.action) for c in changes}

        assert ("system", None, "delete") in actions

    def test_removed_entity_resource_produces_delete(self):
        """Entity resource in previous but not in manifest produces delete."""
        manifest = LimitsManifest.from_dict({"namespace": "ns"})
        previous = {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {"user-1": ["gpt-4"]},
        }

        changes = compute_diff(manifest, previous)
        actions = {(c.level, c.target, c.action) for c in changes}

        assert ("entity", "user-1/gpt-4", "delete") in actions

    def test_unmanaged_items_not_touched(self):
        """Items never in previous managed set produce no changes."""
        manifest = LimitsManifest.from_dict(
            {
                "namespace": "ns",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1}}}},
            }
        )
        previous = {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {},
        }

        changes = compute_diff(manifest, previous)
        # gpt-4 is new (create), but no deletes for things never managed
        assert all(c.action != "delete" for c in changes)

    def test_empty_manifest_deletes_all_managed(self):
        """Empty manifest (CFN Delete) deletes all previously managed items."""
        manifest = LimitsManifest.from_dict({"namespace": "ns"})
        previous = {
            "managed_system": True,
            "managed_resources": ["gpt-4", "claude-3"],
            "managed_entities": {"user-1": ["gpt-4"]},
        }

        changes = compute_diff(manifest, previous)
        actions = {(c.level, c.target, c.action) for c in changes}

        assert ("system", None, "delete") in actions
        assert ("resource", "gpt-4", "delete") in actions
        assert ("resource", "claude-3", "delete") in actions
        assert ("entity", "user-1/gpt-4", "delete") in actions
        assert len(changes) == 4

    def test_change_data_included_for_create(self):
        """Create changes include manifest data."""
        manifest = LimitsManifest.from_dict(
            {
                "namespace": "ns",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
            }
        )
        previous = {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {},
        }

        changes = compute_diff(manifest, previous)
        resource_change = next(c for c in changes if c.level == "resource")
        assert resource_change.data is not None
        assert "limits" in resource_change.data

    def test_delete_has_no_data(self):
        """Delete changes have no data payload."""
        manifest = LimitsManifest.from_dict({"namespace": "ns"})
        previous = {
            "managed_system": False,
            "managed_resources": ["gpt-4"],
            "managed_entities": {},
        }

        changes = compute_diff(manifest, previous)
        delete_change = next(c for c in changes if c.action == "delete")
        assert delete_change.data is None
