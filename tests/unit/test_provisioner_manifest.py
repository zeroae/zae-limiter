"""Tests for provisioner manifest parsing, including the `disabled` field."""

from zae_limiter_provisioner.manifest import LimitsManifest


class TestManifestDisabled:
    """Tests for the tri-state `disabled` field on resource/entity decls."""

    def test_resource_disabled_parsed(self):
        m = LimitsManifest.from_dict(
            {
                "namespace": "default",
                "resources": {"gpt-4": {"disabled": True, "limits": {"rpm": {"capacity": 10}}}},
            }
        )
        assert m.resources["gpt-4"].disabled is True

    def test_resource_disabled_defaults_to_none(self):
        m = LimitsManifest.from_dict(
            {
                "namespace": "default",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 10}}}},
            }
        )
        assert m.resources["gpt-4"].disabled is None

    def test_entity_disabled_false_is_preserved(self):
        m = LimitsManifest.from_dict(
            {
                "namespace": "default",
                "entities": {
                    "vip-1": {
                        "resources": {
                            "gpt-4": {
                                "disabled": False,
                                "limits": {"rpm": {"capacity": 10}},
                            }
                        }
                    }
                },
            }
        )
        decl = m.entities["vip-1"].resources["gpt-4"]
        assert decl.disabled is False
        assert decl.to_dict()["disabled"] is False

    def test_to_dict_omits_disabled_when_unset(self):
        m = LimitsManifest.from_dict(
            {
                "namespace": "default",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 10}}}},
            }
        )
        assert "disabled" not in m.resources["gpt-4"].to_dict()

    def test_entity_disabled_defaults_to_none(self):
        m = LimitsManifest.from_dict(
            {
                "namespace": "default",
                "entities": {
                    "vip-1": {"resources": {"gpt-4": {"limits": {"rpm": {"capacity": 10}}}}}
                },
            }
        )
        decl = m.entities["vip-1"].resources["gpt-4"]
        assert decl.disabled is None
        assert "disabled" not in decl.to_dict()

    def test_system_decl_has_no_disabled_field(self):
        """System-level disable is out of scope (ADR-125) — no such attribute."""
        m = LimitsManifest.from_dict(
            {
                "namespace": "default",
                "system": {"limits": {"rpm": {"capacity": 10}}},
            }
        )
        assert not hasattr(m.system, "disabled")
        assert "disabled" not in m.system.to_dict()
