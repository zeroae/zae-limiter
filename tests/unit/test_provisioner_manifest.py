"""Tests for provisioner manifest parsing, including the `disabled` field."""

import pytest

from zae_limiter_provisioner.manifest import LimitDecl, LimitsManifest


class TestLimitDeclValidation:
    @pytest.mark.parametrize("field", ["capacity", "refill_amount", "refill_period"])
    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_non_positive(self, field, bad):
        decl = {"capacity": 100, "refill_amount": 100, "refill_period": 60, field: bad}
        with pytest.raises(ValueError, match=field):
            LimitDecl.from_dict(decl)

    def test_accepts_positive(self):
        decl = LimitDecl.from_dict({"capacity": 100})
        assert (decl.capacity, decl.refill_amount, decl.refill_period) == (100, 100, 60)

    def test_burst_backcompat_still_validated(self):
        with pytest.raises(ValueError, match="capacity"):
            LimitDecl.from_dict({"capacity": 100, "burst": 0})


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
