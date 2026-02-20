"""Tests for LimitsManifest YAML parsing and validation."""

import pytest

from zae_limiter_provisioner.manifest import LimitsManifest


class TestLimitsManifestParsing:
    """Tests for YAML parsing into LimitsManifest."""

    def test_parse_minimal(self):
        """Minimal YAML with just namespace parses correctly."""
        raw = {"namespace": "test-ns"}
        manifest = LimitsManifest.from_dict(raw)
        assert manifest.namespace == "test-ns"
        assert manifest.system is None
        assert manifest.resources == {}
        assert manifest.entities == {}

    def test_parse_system_limits(self):
        """System-level limits parse with shorthand defaults."""
        raw = {
            "namespace": "test-ns",
            "system": {
                "on_unavailable": "allow",
                "limits": {
                    "rpm": {"capacity": 1000},
                },
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        assert manifest.system is not None
        assert manifest.system.on_unavailable == "allow"
        assert len(manifest.system.limits) == 1
        limit = manifest.system.limits["rpm"]
        assert limit.capacity == 1000
        assert limit.burst == 1000  # default: capacity
        assert limit.refill_amount == 1000  # default: capacity
        assert limit.refill_period == 60  # default: 60

    def test_parse_explicit_limit_fields(self):
        """Explicit limit fields override shorthand defaults."""
        raw = {
            "namespace": "test-ns",
            "system": {
                "limits": {
                    "tpm": {
                        "capacity": 50000,
                        "burst": 75000,
                        "refill_amount": 50000,
                        "refill_period": 120,
                    },
                },
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        limit = manifest.system.limits["tpm"]
        assert limit.capacity == 50000
        assert limit.burst == 75000
        assert limit.refill_amount == 50000
        assert limit.refill_period == 120

    def test_parse_resources(self):
        """Resource-level limits parse correctly."""
        raw = {
            "namespace": "test-ns",
            "resources": {
                "gpt-4": {
                    "limits": {
                        "rpm": {"capacity": 1000},
                        "tpm": {"capacity": 50000},
                    },
                },
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        assert "gpt-4" in manifest.resources
        assert len(manifest.resources["gpt-4"].limits) == 2

    def test_parse_entities(self):
        """Entity-level limits parse correctly with resource scoping."""
        raw = {
            "namespace": "test-ns",
            "entities": {
                "user-123": {
                    "resources": {
                        "gpt-4": {
                            "limits": {"rpm": {"capacity": 500}},
                        },
                        "_default_": {
                            "limits": {"rpm": {"capacity": 200}},
                        },
                    },
                },
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        assert "user-123" in manifest.entities
        entity = manifest.entities["user-123"]
        assert "gpt-4" in entity.resources
        assert "_default_" in entity.resources
        assert entity.resources["gpt-4"].limits["rpm"].capacity == 500

    def test_parse_from_yaml_string(self):
        """from_yaml parses a YAML string into a manifest."""
        yaml_str = """
namespace: test-ns
system:
  limits:
    rpm:
      capacity: 1000
resources:
  gpt-4:
    limits:
      tpm:
        capacity: 50000
"""
        manifest = LimitsManifest.from_yaml(yaml_str)
        assert manifest.namespace == "test-ns"
        assert manifest.system.limits["rpm"].capacity == 1000
        assert manifest.resources["gpt-4"].limits["tpm"].capacity == 50000

    def test_missing_namespace_raises(self):
        """Missing namespace field raises ValueError."""
        with pytest.raises(ValueError, match="namespace"):
            LimitsManifest.from_dict({})

    def test_to_dict_roundtrip(self):
        """to_dict produces a dict that round-trips through from_dict."""
        raw = {
            "namespace": "test-ns",
            "system": {
                "on_unavailable": "allow",
                "limits": {"rpm": {"capacity": 1000}},
            },
            "resources": {
                "gpt-4": {"limits": {"tpm": {"capacity": 50000}}},
            },
            "entities": {
                "user-1": {
                    "resources": {
                        "gpt-4": {"limits": {"rpm": {"capacity": 500}}},
                    },
                },
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        roundtripped = LimitsManifest.from_dict(manifest.to_dict())
        assert roundtripped.namespace == manifest.namespace
        assert roundtripped.system.limits["rpm"].capacity == 1000
        assert roundtripped.resources["gpt-4"].limits["tpm"].capacity == 50000
        assert roundtripped.entities["user-1"].resources["gpt-4"].limits["rpm"].capacity == 500


class TestManifestManagedSet:
    """Tests for extracting the managed set from a manifest."""

    def test_managed_set_system(self):
        """Manifest with system section reports managed_system=True."""
        raw = {"namespace": "ns", "system": {"limits": {"rpm": {"capacity": 1}}}}
        manifest = LimitsManifest.from_dict(raw)
        ms = manifest.managed_set()
        assert ms["managed_system"] is True

    def test_managed_set_no_system(self):
        """Manifest without system section reports managed_system=False."""
        manifest = LimitsManifest.from_dict({"namespace": "ns"})
        ms = manifest.managed_set()
        assert ms["managed_system"] is False

    def test_managed_set_resources(self):
        """Managed set includes all resource names."""
        raw = {
            "namespace": "ns",
            "resources": {
                "gpt-4": {"limits": {"rpm": {"capacity": 1}}},
                "claude-3": {"limits": {"tpm": {"capacity": 1}}},
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        ms = manifest.managed_set()
        assert sorted(ms["managed_resources"]) == ["claude-3", "gpt-4"]

    def test_managed_set_entities(self):
        """Managed set includes entity-to-resource mapping."""
        raw = {
            "namespace": "ns",
            "entities": {
                "user-1": {
                    "resources": {
                        "gpt-4": {"limits": {"rpm": {"capacity": 1}}},
                        "_default_": {"limits": {"rpm": {"capacity": 1}}},
                    },
                },
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        ms = manifest.managed_set()
        assert ms["managed_entities"] == {"user-1": ["_default_", "gpt-4"]}
