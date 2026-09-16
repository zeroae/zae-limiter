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


# --- Scheduled limits (#222, Task 6) -----------------------------------------

YAML = """
namespace: default
resources:
  gpt-4:
    limits:
      rpm:
        capacity: 1000
        schedule:
          - cron: "* 9-17 * * MON-FRI"
            tz: America/New_York
            scale: 0.5
          - cron: "* 0-6 * * *"
            tz: America/New_York
            capacity: 2000
      rpd:
        capacity: 10000
        reset_schedule:
          - cron: "0 0 * * *"
            tz: America/New_York
"""


class TestManifestSchedules:
    def test_parses_the_param_schedule(self):
        manifest = LimitsManifest.from_yaml(YAML)
        rpm = manifest.resources["gpt-4"].limits["rpm"]
        assert len(rpm.schedule) == 2
        assert rpm.schedule[0].cron == "* 9-17 * * MON-FRI"
        assert rpm.schedule[0].tz == "America/New_York"
        assert rpm.schedule[0].scale == 0.5
        assert rpm.schedule[1].capacity == 2000
        assert rpm.reset_schedule == ()

    def test_parses_the_reset_schedule(self):
        manifest = LimitsManifest.from_yaml(YAML)
        rpd = manifest.resources["gpt-4"].limits["rpd"]
        assert rpd.reset_schedule[0].cron == "0 0 * * *"
        assert rpd.reset_schedule[0].tz == "America/New_York"
        assert rpd.schedule == ()

    def test_reset_entries_are_reset_entries(self):
        """`ScheduleEntry.reset()`, not the plain constructor: the two tuples are
        validated by opposite rules and the flag is what separates them (§3.6)."""
        manifest = LimitsManifest.from_yaml(YAML)
        rpd = manifest.resources["gpt-4"].limits["rpd"]
        assert rpd.reset_schedule[0]._reset is True
        assert manifest.resources["gpt-4"].limits["rpm"].schedule[0]._reset is False

    def test_a_reset_schedule_flips_the_refill_amount_default_to_zero(self):
        """ADR-137: a quota does not drip, and the author never types the zero.
        The old default (`refill_amount = capacity`) would build the one
        configuration the ADR rejects out of the most natural manifest."""
        manifest = LimitsManifest.from_yaml(YAML)
        assert manifest.resources["gpt-4"].limits["rpd"].refill_amount == 0

    def test_an_unscheduled_limit_still_defaults_to_capacity(self):
        """The flip is conditional on the reset, and on nothing else."""
        manifest = LimitsManifest.from_yaml(YAML)
        rpm = manifest.resources["gpt-4"].limits["rpm"]
        assert rpm.refill_amount == 1000

    def test_a_param_schedule_alone_does_not_flip_the_default(self):
        """Only `reset_schedule` flips it. A limit with a param schedule and no
        reset still drips, so the capacity default is right there."""
        decl = LimitDecl.from_dict(
            {"capacity": 1000, "schedule": [{"cron": "* 0-6 * * *", "capacity": 2000}]}
        )
        assert decl.refill_amount == 1000

    def test_the_flip_respects_burst(self):
        """`burst` overrides capacity before the default is taken, so the
        unscheduled default follows burst; the quota default is still 0."""
        assert LimitDecl.from_dict({"capacity": 10, "burst": 99}).refill_amount == 99
        quota = LimitDecl.from_dict(
            {
                "capacity": 10,
                "burst": 99,
                "reset_schedule": [{"cron": "0 0 * * *"}],
            }
        )
        assert (quota.capacity, quota.refill_amount) == (99, 0)

    def test_rejects_an_explicit_rate_beside_a_reset(self):
        """A stated conflict is loud; only an omission gets the default."""
        bad = YAML.replace(
            "        capacity: 10000\n",
            "        capacity: 10000\n        refill_amount: 10000\n",
        )
        with pytest.raises(ValueError, match="reset"):
            LimitsManifest.from_yaml(bad)

    def test_an_explicit_zero_beside_a_reset_is_accepted(self):
        """`to_dict()` always emits refill_amount, so this is what a round trip
        produces; rejecting it would make the manifest unable to restate itself."""
        decl = LimitDecl.from_dict(
            {
                "capacity": 10_000,
                "refill_amount": 0,
                "reset_schedule": [{"cron": "0 0 * * *", "tz": "America/New_York"}],
            }
        )
        assert decl.refill_amount == 0

    def test_rejects_a_zero_rate_without_a_reset(self):
        """ADR-137: never neither. capacity and refill_period stay strictly
        positive; only refill_amount gained the conditional zero."""
        with pytest.raises(ValueError, match="reset_schedule"):
            LimitDecl.from_dict({"capacity": 10_000, "refill_amount": 0})

    def test_capacity_and_refill_period_stay_strictly_positive_beside_a_reset(self):
        """The conditional zero is `refill_amount`'s alone — a reset does not
        relax the other two out of the shared loop."""
        reset = [{"cron": "0 0 * * *"}]
        with pytest.raises(ValueError, match="capacity"):
            LimitDecl.from_dict({"capacity": 0, "reset_schedule": reset})
        with pytest.raises(ValueError, match="refill_period"):
            LimitDecl.from_dict({"capacity": 10, "refill_period": 0, "reset_schedule": reset})

    def test_absent_schedule_is_an_empty_tuple(self):
        manifest = LimitsManifest.from_yaml(
            "namespace: default\nresources:\n  gpt-4:\n"
            "    limits:\n      rpm:\n        capacity: 1000\n"
        )
        rpm = manifest.resources["gpt-4"].limits["rpm"]
        assert rpm.schedule == ()
        assert rpm.reset_schedule == ()

    def test_rejects_an_invalid_cron_at_parse_time(self):
        """A manifest that applies must be a manifest that evaluates, so
        `limits plan` surfaces this before anything is written."""
        with pytest.raises(ValueError, match="cron"):
            LimitsManifest.from_yaml(YAML.replace("* 9-17 * * MON-FRI", "* 99 * * *"))

    def test_rejects_an_unknown_timezone_at_parse_time(self):
        with pytest.raises(ValueError, match="timezone"):
            LimitsManifest.from_yaml(YAML.replace("America/New_York", "Mars/Olympus_Mons"))

    def test_rejects_extended_cron_tokens(self):
        """L/W/# parse without error in cronsim and would silently never
        fire — core plan Task 1 rejects them, and that must reach YAML."""
        with pytest.raises(ValueError, match="not supported"):
            LimitsManifest.from_yaml(YAML.replace('"0 0 * * *"', '"0 0 L * *"'))

    def test_rejects_a_six_field_cron(self):
        """`parse_cron` rejects a leading seconds field; that must reach YAML
        too, since nothing in the evaluator is finer than a minute."""
        with pytest.raises(ValueError, match="5 fields"):
            LimitsManifest.from_yaml(YAML.replace('"0 0 * * *"', '"0 0 0 * * *"'))

    def test_rejects_a_reset_entry_carrying_a_modifier(self):
        """A reset changes the balance, not the parameters (§3.6).

        `ScheduleEntry.reset(**entry)` raises *TypeError* for an unexpected
        keyword, so the entry shape has to be checked before the call for this
        to be the ValueError `limits plan` can report."""
        bad = YAML.replace(
            '          - cron: "0 0 * * *"\n            tz: America/New_York\n',
            '          - cron: "0 0 * * *"\n            tz: America/New_York\n'
            "            scale: 0.5\n",
        )
        with pytest.raises(ValueError, match="reset"):
            LimitsManifest.from_yaml(bad)


class TestScheduleEntryShapeErrors:
    """Every malformed entry is a ValueError naming where it went wrong.

    `ScheduleEntry(**entry)` reports a mistyped key as `TypeError: __init__()
    got an unexpected keyword argument`, which `limits plan` cannot present as
    a manifest problem. The entry shape is therefore validated before the call.
    """

    def test_unknown_entry_field_is_a_value_error_naming_the_field(self):
        with pytest.raises(ValueError, match="scal3"):
            LimitDecl.from_dict({"capacity": 10, "schedule": [{"cron": "* * * * *", "scal3": 0.5}]})

    def test_the_private_reset_flag_is_not_settable_from_yaml(self):
        """A reset entry in the *parameter* tuple would win its window and then
        supply nothing, shadowing every entry below it (§3.6)."""
        with pytest.raises(ValueError, match="_reset"):
            LimitDecl.from_dict(
                {"capacity": 10, "schedule": [{"cron": "* * * * *", "_reset": True}]}
            )

    def test_a_missing_cron_is_a_value_error(self):
        with pytest.raises(ValueError, match="cron"):
            LimitDecl.from_dict({"capacity": 10, "schedule": [{"scale": 0.5}]})

    def test_a_null_schedule_is_an_empty_tuple(self):
        """`schedule:` with nothing under it is YAML null, not a list."""
        decl = LimitDecl.from_dict({"capacity": 10, "schedule": None, "reset_schedule": None})
        assert decl.schedule == ()
        assert decl.reset_schedule == ()
        assert decl.refill_amount == 10

    def test_a_non_list_schedule_is_a_value_error(self):
        with pytest.raises(ValueError, match="list"):
            LimitDecl.from_dict({"capacity": 10, "schedule": {"cron": "* * * * *"}})

    def test_a_non_mapping_entry_is_a_value_error(self):
        with pytest.raises(ValueError, match="mapping"):
            LimitDecl.from_dict({"capacity": 10, "schedule": ["* * * * *"]})

    def test_the_message_names_the_offending_entry_index(self):
        with pytest.raises(ValueError, match=r"schedule\[1\]"):
            LimitDecl.from_dict(
                {
                    "capacity": 10,
                    "schedule": [
                        {"cron": "* * * * *", "scale": 0.5},
                        {"cron": "nonsense", "scale": 0.5},
                    ],
                }
            )

    def test_the_message_distinguishes_the_two_tuples(self):
        with pytest.raises(ValueError, match=r"reset_schedule\[0\]"):
            LimitDecl.from_dict({"capacity": 10, "reset_schedule": [{"cron": "nonsense"}]})

    @pytest.mark.parametrize("field", ["capacity", "refill_amount", "refill_period_seconds"])
    @pytest.mark.parametrize("value", [1.5, 2.0, True])
    def test_a_non_integer_absolute_is_rejected_at_parse_time(self, field, value):
        """#569: the YAML path had no equivalent of `handler._coerce_int`.

        `capacity: 1.5` parsed, encoded as `c1.5` and then made every later read
        of that config item raise. `limits plan` must surface it before anything
        is written.
        """
        with pytest.raises(ValueError, match=rf"schedule\[0\].*{field} must be a whole number"):
            LimitDecl.from_dict({"capacity": 10, "schedule": [{"cron": "* * * * *", field: value}]})


class TestNonIntegerAbsolutesEndToEnd:
    """The full manifest path from #569's second repro."""

    def test_a_manifest_with_a_fractional_capacity_is_rejected(self):
        import yaml

        doc = """
namespace: default
resources:
  gpt-4:
    limits:
      rpm:
        capacity: 1000
        schedule:
          - cron: "* 9-17 * * MON-FRI"
            capacity: 1.5
"""
        with pytest.raises(ValueError, match="capacity must be a whole number"):
            LimitsManifest.from_dict(yaml.safe_load(doc))

    def test_an_integer_capacity_still_parses_and_round_trips(self):
        import yaml

        from zae_limiter.schedule import decode, encode

        doc = """
namespace: default
resources:
  gpt-4:
    limits:
      rpm:
        capacity: 1000
        schedule:
          - cron: "* 9-17 * * MON-FRI"
            capacity: 2
"""
        m = LimitsManifest.from_dict(yaml.safe_load(doc))
        entries = m.resources["gpt-4"].limits["rpm"].schedule
        assert entries[0].capacity == 2
        assert decode(*encode(entries))[0].capacity == 2


class TestDripAndResetAcrossWindows:
    """ADR-137 is a rule about the limit, not about one field.

    A `schedule` entry may override `refill_amount`, so a quota carrying both
    tuples can be handed a positive rate inside a window — the drip-and-reset
    pairing the ADR rejects, reintroduced through the back door.
    """

    RESET = [{"cron": "0 0 * * *", "tz": "America/New_York"}]

    def test_both_tuples_on_one_limit_are_legal(self):
        """The two tuples are independent (§1.1): a quota may be scaled."""
        decl = LimitDecl.from_dict(
            {
                "capacity": 10_000,
                "schedule": [{"cron": "* * * * SAT,SUN", "scale": 0.5}],
                "reset_schedule": self.RESET,
            }
        )
        assert decl.refill_amount == 0
        assert len(decl.schedule) == 1
        assert len(decl.reset_schedule) == 1

    def test_a_capacity_override_beside_a_reset_is_legal(self):
        decl = LimitDecl.from_dict(
            {
                "capacity": 10_000,
                "schedule": [{"cron": "* * * * SAT,SUN", "capacity": 5_000}],
                "reset_schedule": self.RESET,
            }
        )
        assert decl.schedule[0].capacity == 5_000

    def test_rejects_a_schedule_entry_that_reintroduces_the_drip(self):
        with pytest.raises(ValueError, match="drips or resets"):
            LimitDecl.from_dict(
                {
                    "capacity": 10_000,
                    "schedule": [{"cron": "* * * * SAT,SUN", "refill_amount": 500}],
                    "reset_schedule": self.RESET,
                }
            )

    def test_the_message_names_the_offending_entry(self):
        with pytest.raises(ValueError, match=r"schedule\[1\]"):
            LimitDecl.from_dict(
                {
                    "capacity": 10_000,
                    "schedule": [
                        {"cron": "* * * * SAT", "scale": 0.5},
                        {"cron": "* * * * SUN", "refill_amount": 500},
                    ],
                    "reset_schedule": self.RESET,
                }
            )

    def test_a_refill_amount_override_without_a_reset_is_untouched(self):
        """The new rule is conditional on the reset, and on nothing else."""
        decl = LimitDecl.from_dict(
            {
                "capacity": 10_000,
                "schedule": [{"cron": "* * * * SAT,SUN", "refill_amount": 500}],
            }
        )
        assert decl.schedule[0].refill_amount == 500


class TestSchedulesAtEveryConfigLevel:
    """The manifest carries limits at three levels; the rule holds at each."""

    LIMITS = {
        "rpm": {"capacity": 1000, "schedule": [{"cron": "* 0-6 * * *", "scale": 0.5}]},
        "rpd": {"capacity": 10_000, "reset_schedule": [{"cron": "0 0 * * *"}]},
    }

    def _manifest(self, key):
        bodies = {
            "system": {"system": {"limits": self.LIMITS}},
            "resource": {"resources": {"gpt-4": {"limits": self.LIMITS}}},
            "entity": {"entities": {"u1": {"resources": {"gpt-4": {"limits": self.LIMITS}}}}},
        }
        return LimitsManifest.from_dict({"namespace": "default", **bodies[key]})

    def _limits(self, key):
        m = self._manifest(key)
        return {
            "system": lambda: m.system.limits,
            "resource": lambda: m.resources["gpt-4"].limits,
            "entity": lambda: m.entities["u1"].resources["gpt-4"].limits,
        }[key]()

    @pytest.mark.parametrize("level", ["system", "resource", "entity"])
    def test_every_limit_keeps_its_own_schedule(self, level):
        limits = self._limits(level)
        assert limits["rpm"].schedule[0].scale == 0.5
        assert limits["rpm"].reset_schedule == ()
        assert limits["rpd"].reset_schedule[0].cron == "0 0 * * *"
        assert limits["rpd"].schedule == ()

    @pytest.mark.parametrize("level", ["system", "resource", "entity"])
    def test_the_refill_amount_flip_applies_per_limit(self, level):
        limits = self._limits(level)
        assert limits["rpm"].refill_amount == 1000
        assert limits["rpd"].refill_amount == 0

    @pytest.mark.parametrize("level", ["system", "resource", "entity"])
    def test_a_bad_schedule_is_rejected_at_every_level(self, level):
        bad = dict(self.LIMITS, bad={"capacity": 1, "schedule": [{"cron": "nonsense"}]})
        bodies = {
            "system": {"system": {"limits": bad}},
            "resource": {"resources": {"gpt-4": {"limits": bad}}},
            "entity": {"entities": {"u1": {"resources": {"gpt-4": {"limits": bad}}}}},
        }
        with pytest.raises(ValueError, match="cron"):
            LimitsManifest.from_dict({"namespace": "default", **bodies[level]})

    @pytest.mark.parametrize("level", ["system", "resource", "entity"])
    def test_the_whole_manifest_round_trips(self, level):
        m = self._manifest(level)
        assert LimitsManifest.from_dict(m.to_dict()) == m


class TestScheduleSurvivesToChangeData:
    """`differ.py` compares nothing — it emits every manifest item every time
    (`compute_diff`). What matters is that the schedule is carried in
    `Change.data` so the applier and the bucket fan-out can see it."""

    def test_to_dict_round_trips_the_schedule(self):
        decl = LimitDecl.from_dict(
            {
                "capacity": 1000,
                "schedule": [
                    {"cron": "* 9-17 * * MON-FRI", "tz": "America/New_York", "scale": 0.5}
                ],
            }
        )
        restored = LimitDecl.from_dict(decl.to_dict())
        assert restored == decl

    def test_to_dict_round_trips_every_modifier(self):
        """`_entry_to_dict` emits only the fields that are set, so an entry
        whose absolutes are partly None comes back identical rather than
        acquiring explicit Nones the constructor would reject."""
        decl = LimitDecl.from_dict(
            {
                "capacity": 1000,
                "schedule": [
                    {"cron": "* 0-6 * * *", "capacity": 2000},
                    {
                        "cron": "* 7-8 * * *",
                        "refill_amount": 30,
                        "refill_period_seconds": 120,
                    },
                ],
            }
        )
        assert LimitDecl.from_dict(decl.to_dict()) == decl

    def test_to_dict_round_trips_a_quota(self):
        """The emitted `refill_amount: 0` must survive re-parsing — the applier
        writes `l_rpd_ra = 0` from it, and a CFN round trip re-reads it."""
        decl = LimitDecl.from_dict(
            {
                "capacity": 10_000,
                "reset_schedule": [{"cron": "0 0 * * *", "tz": "America/New_York"}],
            }
        )
        assert decl.to_dict()["refill_amount"] == 0
        assert LimitDecl.from_dict(decl.to_dict()) == decl

    def test_a_reset_entry_emits_no_modifier_keys(self):
        decl = LimitDecl.from_dict({"capacity": 10_000, "reset_schedule": [{"cron": "0 0 * * *"}]})
        assert decl.to_dict()["reset_schedule"] == [{"cron": "0 0 * * *", "tz": "UTC"}]

    def test_change_data_carries_the_schedule(self):
        from zae_limiter_provisioner.differ import compute_diff

        manifest = LimitsManifest.from_yaml(YAML)
        changes = compute_diff(manifest, previous={})
        resource_change = next(c for c in changes if c.level == "resource")
        rpm = resource_change.data["limits"]["rpm"]
        assert rpm["schedule"][0]["cron"] == "* 9-17 * * MON-FRI"
        assert rpm["schedule"][0]["scale"] == 0.5
        rpd = resource_change.data["limits"]["rpd"]
        assert rpd["reset_schedule"][0]["cron"] == "0 0 * * *"
        assert rpd["refill_amount"] == 0

    @pytest.mark.parametrize("level", ["system", "resource", "entity"])
    def test_change_data_carries_the_schedule_at_every_level(self, level):
        from zae_limiter_provisioner.differ import compute_diff

        limits = {"rpd": {"capacity": 10_000, "reset_schedule": [{"cron": "0 0 * * *"}]}}
        bodies = {
            "system": {"system": {"limits": limits}},
            "resource": {"resources": {"gpt-4": {"limits": limits}}},
            "entity": {"entities": {"u1": {"resources": {"gpt-4": {"limits": limits}}}}},
        }
        manifest = LimitsManifest.from_dict({"namespace": "default", **bodies[level]})
        change = next(c for c in compute_diff(manifest, previous={}) if c.level == level)
        assert change.data["limits"]["rpd"]["reset_schedule"][0]["cron"] == "0 0 * * *"

    def test_change_data_is_json_serialisable(self):
        """`limits plan` returns changes through the Lambda as JSON."""
        import json

        from zae_limiter_provisioner.differ import compute_diff

        changes = compute_diff(LimitsManifest.from_yaml(YAML), previous={})
        json.dumps([c.data for c in changes])

    def test_unscheduled_limits_carry_no_schedule_key(self):
        """Keep the wire shape minimal so an unscheduled manifest is unchanged."""
        from zae_limiter_provisioner.differ import compute_diff

        manifest = LimitsManifest.from_yaml(
            "namespace: default\nresources:\n  gpt-4:\n"
            "    limits:\n      rpm:\n        capacity: 1000\n"
        )
        changes = compute_diff(manifest, previous={})
        rpm = next(c for c in changes if c.level == "resource").data["limits"]["rpm"]
        assert "schedule" not in rpm
        assert "reset_schedule" not in rpm
