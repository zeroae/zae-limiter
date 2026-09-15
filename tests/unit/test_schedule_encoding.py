"""Tests for the compact schedule storage encoding (#222 §4).

Standard 5-field cron is the interface everywhere; the compact form exists only
so a bucket item does not cross DynamoDB's 1 KB WCU boundary. The property that
matters is the round trip: `encode` must be canonical (re-encoding a decoded
schedule is byte-identical) and `decode` must reproduce the semantics.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from zae_limiter.schedule import (
    ScheduleEntry,
    decode,
    decode_reset,
    effective_params,
    encode,
    encode_reset,
    matches,
    parse_cron,
    to_cron,
)

BUSINESS = ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5)
NIGHTS = ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000)


class TestEncode:
    def test_compact_shape(self):
        compact, tz = encode((BUSINESS, NIGHTS))
        assert compact == "h9-17w1-5s500;h0-6c2000"
        assert tz == "America/New_York"

    def test_is_much_smaller_than_json(self):
        compact, _ = encode((BUSINESS, NIGHTS))
        as_json = json.dumps(
            [
                {"c": BUSINESS.cron, "z": BUSINESS.tz, "s": 0.5},
                {"c": NIGHTS.cron, "z": NIGHTS.tz, "cp": 2000},
            ],
            separators=(",", ":"),
        )
        assert len(compact) * 4 < len(as_json)

    def test_empty_schedule(self):
        assert encode(()) == ("", None)

    def test_rejects_mixed_timezones(self):
        """tz is hoisted to one item-level attribute, so entries must agree."""
        with pytest.raises(ValueError, match="timezone"):
            encode((BUSINESS, ScheduleEntry(cron="* 0-6 * * *", tz="UTC", scale=0.5)))

    def test_an_all_wildcard_entry_encodes_to_its_modifiers_alone(self):
        """No field survives, but the entry must still be addressable."""
        compact, _ = encode((ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.5),))
        assert compact == "s500"

    def test_absolute_fields_encode_in_a_fixed_order(self):
        """Canonical output: the tag order cannot follow dataclass iteration luck."""
        entry = ScheduleEntry(
            cron="* * * * *", tz="UTC", capacity=7, refill_amount=3, refill_period_seconds=30
        )
        assert encode((entry,))[0] == "c7a3p30"

    @pytest.mark.parametrize(
        "kwargs,expected",
        [
            ({"capacity": 500}, "c500"),
            ({"refill_amount": 9}, "a9"),
            ({"refill_period_seconds": 86400}, "p86400"),
            ({"capacity": 4, "refill_period_seconds": 60}, "c4p60"),
        ],
    )
    def test_absolute_subsets_omit_what_is_unset(self, kwargs, expected):
        assert encode((ScheduleEntry(cron="* * * * *", tz="UTC", **kwargs),))[0] == expected

    def test_whitespace_in_the_source_cron_is_normalised_away(self):
        """Two spellings of the same cron must not store as two different values."""
        tidy = ScheduleEntry(cron="* 9-17 * * 1-5", tz="UTC", scale=0.5)
        messy = ScheduleEntry(cron="*  9-17   * *  1-5", tz="UTC", scale=0.5)
        assert encode((messy,)) == encode((tidy,))

    @pytest.mark.parametrize(
        "cron,expected",
        [
            # Names normalise to numbers, in every field that has names.
            ("* * * * MON-FRI", "w1-5"),
            ("* * * * mon-fri", "w1-5"),
            ("* * * JAN,JUL *", "M1,7"),
            ("* * * jan-mar *", "M1-3"),
            # Sunday has two numbers, and which one is correct is positional.
            ("* * * * SUN", "w7"),
            ("* * * * 0", "w7"),
            ("* * * * 7", "w7"),
            ("* * * * SAT,SUN", "w6,7"),
            ("* * * * 6,0", "w6,7"),
            ("* * * * SUN-THU", "w0-4"),
            ("* * * * SUN-SUN", "w0-0"),
            ("* * * * SUN/2", "w0/2"),
            ("* * * * MON-FRI,SUN", "w1-5,7"),
            # Steps and lists pass through untouched in the numeric fields.
            ("*/15 * * * *", "m*/15"),
            ("0-30/5 * * * *", "m0-30/5"),
            ("1,3,5 * * * *", "m1,3,5"),
            ("* * 1-7 * *", "D1-7"),
        ],
    )
    def test_field_normalisation(self, cron, expected):
        assert encode((ScheduleEntry(cron=cron, tz="UTC", scale=0.5),))[0] == expected + "s500"

    def test_sunday_spellings_all_converge(self):
        """`differ.py` must not read SUN vs 0 vs 7 as a change on every apply."""
        forms = [
            encode((ScheduleEntry(cron=f"* * * * {spelling}", tz="UTC", scale=0.5),))[0]
            for spelling in ("SUN", "sun", "0", "7")
        ]
        assert len(set(forms)) == 1


class TestScaleQuantisation:
    def test_scale_is_stored_as_integer_per_mille(self):
        assert encode((ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.25),))[0] == "s250"

    def test_scale_rounds_rather_than_truncates(self):
        """`int(2.3 * 1000)` is 2299: 2.3 has no exact binary representation."""
        assert encode((ScheduleEntry(cron="* * * * *", tz="UTC", scale=2.3),))[0] == "s2300"

    def test_scale_below_one_per_mille_floors_at_one(self):
        """`encode` must stay total: `s0` would decode to a scale of 0, which
        `ScheduleEntry` rejects."""
        compact, _ = encode((ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.0004),))
        assert compact == "s1"
        assert decode(compact, "UTC")[0].scale == 0.001

    def test_a_scale_that_is_not_a_whole_per_mille_is_quantised(self):
        """The one lossy dimension of the encoding, pinned so it is not a surprise."""
        compact, _ = encode((ScheduleEntry(cron="* * * * *", tz="UTC", scale=1 / 3),))
        assert compact == "s333"
        assert decode(compact, "UTC")[0].scale == 0.333


def _representative_schedules():
    """Every shape the encoding has to survive, as a flat list of tuples."""
    single = [
        (BUSINESS,),
        (NIGHTS,),
        (ScheduleEntry(cron="*/15 * * * SAT,SUN", tz="UTC", scale=0.25),),
        (ScheduleEntry(cron="0 0 1 JAN,JUL *", tz="UTC", capacity=5000),),
        (
            ScheduleEntry(
                cron="* * * * *", tz="UTC", capacity=7, refill_amount=3, refill_period_seconds=30
            ),
        ),
        (ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.5),),
        (ScheduleEntry(cron="* * * * SUN-THU", tz="Asia/Kolkata", scale=2.0),),
        (ScheduleEntry(cron="1,3,5 * * * *", tz="UTC", refill_amount=11),),
        (ScheduleEntry(cron="0 9 1-7 * MON", tz="Australia/Lord_Howe", scale=0.125),),
        (ScheduleEntry(cron="*/5 8-18 * 1-6 1-5", tz="Europe/Berlin", scale=0.75),),
    ]
    return [
        *single,
        (BUSINESS, NIGHTS),
        # Many entries, mixing scale and absolute forms under one timezone.
        tuple(
            ScheduleEntry(cron=f"* {h} * * *", tz="UTC", scale=(h + 1) / 100)
            if h % 2
            else ScheduleEntry(cron=f"* {h} * * *", tz="UTC", capacity=100 * (h + 1))
            for h in range(6)
        ),
    ]


def _assert_same_entry(restored: ScheduleEntry, original: ScheduleEntry) -> None:
    """Semantic equality: the cron text is normalised, the meaning is not."""
    assert parse_cron(restored.cron, restored.tz) == parse_cron(original.cron, original.tz)
    assert restored.tz == original.tz
    assert restored.scale == original.scale
    assert restored.capacity == original.capacity
    assert restored.refill_amount == original.refill_amount
    assert restored.refill_period_seconds == original.refill_period_seconds


class TestRoundTrip:
    @pytest.mark.parametrize("entries", _representative_schedules())
    def test_decode_reproduces_the_entries(self, entries):
        """`decode(encode(x))` means exactly `x` — the property the task exists for.

        Not *textually* `x`: `MON-FRI` comes back as `1-5`, deliberately (§4.3), so
        `differ.py` does not read two spellings of one schedule as a change. What
        must survive is the parsed field sets, the timezone and every modifier.
        """
        compact, tz = encode(entries)
        restored = decode(compact, tz or "UTC")
        assert len(restored) == len(entries)
        for got, want in zip(restored, entries, strict=True):
            _assert_same_entry(got, want)

    @pytest.mark.parametrize("entries", _representative_schedules())
    def test_decoding_is_idempotent(self, entries):
        """Storage is a fixed point: decoding, re-encoding and decoding again
        gives an *equal object*, so a rewrite never churns the attribute."""
        compact, tz = encode(entries)
        once = decode(compact, tz or "UTC")
        assert decode(*encode(once)) == once

    def test_an_already_canonical_schedule_decodes_to_an_equal_object(self):
        """With no names to normalise, the round trip is dataclass equality."""
        entries = (
            ScheduleEntry(cron="* 9-17 * * 1-5", tz="UTC", scale=0.5),
            ScheduleEntry(cron="0 0 1 1,7 *", tz="UTC", capacity=5000),
        )
        assert decode(*encode(entries)) == entries

    @pytest.mark.parametrize("entries", _representative_schedules())
    def test_re_encoding_is_byte_identical(self, entries):
        """The encoding is canonical, so storage never churns on a rewrite."""
        compact, tz = encode(entries)
        assert encode(decode(compact, tz or "UTC")) == (compact, tz)

    def test_the_empty_schedule_round_trips(self):
        compact, tz = encode(())
        assert decode(compact, tz or "UTC") == ()

    def test_a_name_form_round_trips_to_the_same_semantics_not_the_same_text(self):
        """Names are normalised, so the text changes and the meaning does not."""
        entries = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="UTC", scale=0.5),)
        restored = decode(*encode(entries))
        assert restored[0].cron == "* 9-17 * * 1-5"
        assert parse_cron(restored[0].cron, "UTC") == parse_cron(entries[0].cron, "UTC")

    def test_decoded_entries_evaluate_identically(self):
        compact, tz = encode((BUSINESS, NIGHTS))
        restored = decode(compact, tz)
        now = int(
            datetime(2026, 9, 15, 14, 0, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000
        )
        base = (1_000_000, 1_000_000, 60_000)
        assert effective_params(*base, restored, now) == effective_params(
            *base, (BUSINESS, NIGHTS), now
        )

    def test_the_hoisted_timezone_reaches_every_entry(self):
        restored = decode("h9-17s500;h0-6c2000", "Asia/Kolkata")
        assert [e.tz for e in restored] == ["Asia/Kolkata", "Asia/Kolkata"]


class TestRoundTripSweep:
    """A generated sweep over field-spec shapes, rather than a hand-picked few.

    Every combination is encoded, decoded, re-encoded and re-parsed. This is what
    catches a field the tokeniser mis-splits or a spec form the name substitution
    mangles; the hand-written cases above only cover the forms someone thought of.
    """

    MINUTES = ["*", "0", "*/15", "0-30/5", "1,3,5", "59"]
    HOURS = ["*", "9-17", "0", "*/6", "0,12,23"]
    DOMS = ["*", "1", "1-7", "*/10", "1,15,28", "31"]
    MONTHS = ["*", "JAN", "JAN,JUL", "1-3", "*/3", "DEC"]
    DOWS = ["*", "MON-FRI", "SAT,SUN", "SUN", "0", "7", "SUN-THU", "*/2", "1-5/2", "6,0"]

    @pytest.mark.parametrize(
        "cron",
        [
            " ".join(fields)
            for fields in itertools.chain(
                # One varying field at a time, against all-wildcard neighbours.
                ((m, "*", "*", "*", "*") for m in MINUTES),
                (("*", h, "*", "*", "*") for h in HOURS),
                (("*", "*", d, "*", "*") for d in DOMS),
                (("*", "*", "*", mo, "*") for mo in MONTHS),
                (("*", "*", "*", "*", w) for w in DOWS),
                # Then every field constrained at once, zipped so the sweep stays
                # linear instead of 9000 cases.
                zip(MINUTES, HOURS, DOMS, MONTHS, DOWS),
            )
        ],
    )
    def test_every_field_shape_round_trips(self, cron):
        entries = (ScheduleEntry(cron=cron, tz="America/New_York", scale=0.5),)
        compact, tz = encode(entries)
        restored = decode(compact, tz)
        # Canonical: re-encoding must be byte-identical.
        assert encode(restored) == (compact, tz)
        # Semantics preserved: the normalised cron parses to the same field sets.
        assert parse_cron(restored[0].cron, tz) == parse_cron(cron, tz)
        # And the display form re-encodes to exactly the same bytes.
        assert encode((ScheduleEntry(cron=to_cron(compact), tz=tz, scale=0.5),)) == (compact, tz)


class TestDecodeRejectsCorruption:
    @pytest.mark.parametrize(
        "compact",
        [
            "9h",  # value before any tag
            "h",  # tag with no value
            "h9-17!",  # trailing junk
            "?",  # nothing recognisable
            "h9-17w",  # trailing tag with no value
        ],
    )
    def test_rejects_unparseable_entries(self, compact):
        with pytest.raises(ValueError):
            decode(compact, "UTC")

    def test_rejects_junk_before_an_otherwise_valid_entry(self):
        """The tokeniser must consume the whole string, not scan for tags inside
        it: a leading corrupt byte would otherwise be silently dropped and the
        schedule would decode as if nothing were wrong."""
        assert decode("h9-17s500", "UTC")  # the same entry, uncorrupted
        with pytest.raises(ValueError, match="offset 0"):
            decode("Xh9-17s500", "UTC")

    def test_rejects_a_duplicated_tag(self):
        with pytest.raises(ValueError, match="duplicate"):
            decode("h9-17h0-6c10", "UTC")

    def test_rejects_a_field_that_is_not_valid_cron(self):
        """The tokeniser accepts the shape; cronsim rejects the content."""
        with pytest.raises(ValueError, match="invalid cron"):
            decode("h99c10", "UTC")

    def test_rejects_an_entry_with_no_modifier(self):
        with pytest.raises(ValueError, match="exactly one"):
            decode("h9-17", "UTC")

    def test_rejects_both_kinds_of_modifier(self):
        with pytest.raises(ValueError, match="exactly one"):
            decode("h9-17s500c10", "UTC")

    def test_rejects_an_unknown_timezone(self):
        with pytest.raises(ValueError, match="timezone"):
            decode("h9-17s500", "Mars/Olympus_Mons")


class TestDisplay:
    @pytest.mark.parametrize(
        "compact,expected",
        [
            ("h9-17w1-5s500", "* 9-17 * * MON-FRI"),
            ("h0-6c2000", "* 0-6 * * *"),
            ("m*/15w6,7s250", "*/15 * * * SAT,SUN"),
            ("m0h0D1M1,7c5000", "0 0 1 JAN,JUL *"),
        ],
    )
    def test_renders_canonical_cron_with_names(self, compact, expected):
        """Weekday and month always render as names, which is the one visible
        normalisation: an operator who typed 1-5 gets MON-FRI back."""
        assert to_cron(compact) == expected

    def test_a_step_divisor_is_not_a_weekday(self):
        """`1-5/2` is every other weekday, not `MON-FRI/TUE`."""
        assert to_cron("w1-5/2s500") == "* * * * MON-FRI/2"
        assert to_cron("w*/2s500") == "* * * * */2"

    def test_an_all_wildcard_entry_renders_as_all_wildcards(self):
        assert to_cron("c2000") == "* * * * *"

    @pytest.mark.parametrize(
        "cron",
        [
            "* 9-17 * * MON-FRI",
            "* 0-6 * * *",
            "*/15 * * * SAT,SUN",
            "0 0 1 JAN,JUL *",
            "* 9-17 * * 1-5",
            "* * * * SUN-THU",
            "* * * * 1-5/2",
        ],
    )
    def test_the_display_form_means_the_same_thing(self, cron):
        """`to_cron` is display-only, so it must not drift from the stored form."""
        compact, tz = encode((ScheduleEntry(cron=cron, tz="America/New_York", scale=0.5),))
        rendered = to_cron(compact)
        assert parse_cron(rendered, tz) == parse_cron(cron, tz)
        # Sampled over a year of hourly instants, the two agree minute for minute.
        start = int(datetime(2026, 1, 1, tzinfo=ZoneInfo(tz)).timestamp() * 1000)
        hour = 3_600_000
        original = parse_cron(cron, tz)
        shown = parse_cron(rendered, tz)
        assert all(
            matches(original, start + i * hour) == matches(shown, start + i * hour)
            for i in range(8760)
        )

    def test_display_does_not_crash_on_an_out_of_range_number(self):
        """A corrupt attribute must render, not raise, on a read path."""
        assert to_cron("w9c10") == "* * * * 9"


def _ddb_item_size(item: dict[str, dict[str, object]]) -> int:
    """DynamoDB's own item-size rule, near enough for a budget check.

    Attribute name in UTF-8, plus the value: strings are their UTF-8 length,
    numbers are ~1 byte per two significant digits plus one, booleans 1.
    """
    total = 0
    for name, value in item.items():
        total += len(name.encode())
        ((kind, raw),) = value.items()
        if kind == "S":
            total += len(str(raw).encode())
        elif kind == "N":
            digits = len(str(raw).lstrip("-").replace(".", "").lstrip("0")) or 1
            total += (digits + 1) // 2 + 1
        elif kind == "BOOL":
            total += 1
        else:  # pragma: no cover - the bucket item has no other types
            total += len(str(raw).encode())
    return total


def _bucket_item(limit_count: int, sched: tuple[ScheduleEntry, ...]) -> dict:
    """A bucket item shaped like `Repository.build_composite_create` writes it."""
    ns = "a7x3kq"
    entity, resource = "user-12345", "openai/gpt-4"
    item: dict[str, dict[str, object]] = {
        "PK": {"S": f"{ns}/BUCKET#{entity}#{resource}#0"},
        "SK": {"S": "#STATE"},
        "entity_id": {"S": entity},
        "resource": {"S": resource},
        "rf": {"N": "1789000000000"},
        "GSI2PK": {"S": f"{ns}/RESOURCE#{resource}"},
        "GSI2SK": {"S": f"BUCKET#{entity}#0"},
        "cascade": {"BOOL": False},
        "GSI3PK": {"S": f"{ns}/ENTITY#{entity}"},
        "GSI3SK": {"S": f"BUCKET#{resource}#0"},
        "GSI4PK": {"S": ns},
        "GSI4SK": {"S": f"BUCKET#{entity}#{resource}#0"},
        "shard_count": {"N": "1"},
        "ttl": {"N": "1789086400"},
    }
    for name in ["wcu", *[f"lim{i}" for i in range(limit_count)]]:
        for field, value in (
            ("tk", "1000000"),
            ("cp", "1000000"),
            ("ra", "1000000"),
            ("rp", "60000"),
            ("tc", "0"),
        ):
            item[f"b_{name}_{field}"] = {"N": value}
    if sched:
        compact, tz = encode(sched)
        item["sched"] = {"S": compact}
        item["sched_tz"] = {"S": tz or "UTC"}
        item["vu"] = {"N": "1789003600000"}
    return item


class TestSizeBudget:
    """A bucket item crossing 1 KB doubles the WCU cost of every acquire on it."""

    WORST_SHARED = tuple(
        ScheduleEntry(cron=f"* {h}-{h + 3} * * MON-FRI", tz="America/New_York", scale=0.5)
        for h in (0, 6, 12, 18)
    )

    def test_the_worst_shared_case_stays_under_one_kb(self):
        """§4.2's last row: 6 limits x 4 entries, one schedule shared item-wide."""
        assert _ddb_item_size(_bucket_item(6, self.WORST_SHARED)) < 1024

    def test_six_limits_already_cost_most_of_the_budget_before_any_schedule(self):
        """Why there is no write-time size gate: the limits dominate, not us."""
        assert _ddb_item_size(_bucket_item(6, ())) > 600

    def test_the_schedule_adds_well_under_a_hundred_bytes(self):
        bare = _ddb_item_size(_bucket_item(6, ()))
        scheduled = _ddb_item_size(_bucket_item(6, self.WORST_SHARED))
        assert scheduled - bare < 100

    def test_the_compact_form_is_several_times_smaller_than_json(self):
        """§4.2's headline ratio, measured rather than restated."""
        compact, tz = encode(self.WORST_SHARED)
        as_json = json.dumps(
            [{"cron": e.cron, "tz": e.tz, "scale": e.scale} for e in self.WORST_SHARED],
            separators=(",", ":"),
        )
        assert len(as_json) / (len(compact) + len(tz)) > 3.0


class TestResetEncoding:
    """Reset entries share the field grammar and drop the modifier tokens."""

    def test_encodes_without_a_modifier_token(self):
        compact, tz = encode_reset((ScheduleEntry.reset("0 0 * * *", "America/New_York"),))
        assert compact == "m0h0"
        assert tz == "America/New_York"

    def test_a_daily_reset_is_four_bytes(self):
        """The size claim in §4.1, asserted exactly rather than as `<= 8` — an
        encoder that returned the empty string would satisfy a bound."""
        compact, _ = encode_reset((ScheduleEntry.reset("0 0 * * *"),))
        assert len(compact) == 4

    def test_joins_entries_with_a_semicolon(self):
        compact, tz = encode_reset(
            (
                ScheduleEntry.reset("0 0 * * *", "America/New_York"),
                ScheduleEntry.reset("0 12 * * SUN", "America/New_York"),
            )
        )
        assert compact == "m0h0;m0h12w7"
        assert tz == "America/New_York"

    def test_weekday_names_normalise_exactly_as_the_param_encoder(self):
        """Storage is canonical (§4.3) so `differ.py` does not read SUN against
        7 as a change on every apply. Sunday inside a range must be 0, not 7."""
        compact, _ = encode_reset((ScheduleEntry.reset("0 0 * * SUN-THU"),))
        assert compact == "m0h0w0-4"

    def test_empty_schedule_encodes_to_nothing(self):
        assert encode_reset(()) == ("", None)

    def test_rejects_entries_that_disagree_on_timezone(self):
        with pytest.raises(ValueError, match="one timezone"):
            encode_reset(
                (
                    ScheduleEntry.reset("0 0 * * *", "America/New_York"),
                    ScheduleEntry.reset("0 0 * * *", "UTC"),
                )
            )

    def test_a_reset_is_smaller_than_the_same_cron_as_a_param_entry(self):
        """The modifier tokens are the whole difference: a param entry must
        carry one (`__post_init__` requires exactly one), a reset must not."""
        reset, _ = encode_reset((ScheduleEntry.reset("0 0 * * *"),))
        param, _ = encode((ScheduleEntry(cron="0 0 * * *", scale=0.5),))
        assert param.startswith(reset)
        assert len(param) > len(reset)


class TestResetDecoding:
    def test_decodes_through_the_reset_constructor(self):
        """A reset entry carries no modifier, so `ScheduleEntry(...)` would
        raise its "exactly one" rule. `decode_reset` must use the classmethod."""
        (entry,) = decode_reset("m0h0", "America/New_York")
        assert entry.cron == "0 0 * * *"
        assert entry.tz == "America/New_York"
        assert entry.scale is None
        assert entry.capacity is None
        assert entry.refill_amount is None
        assert entry.refill_period_seconds is None
        assert entry._reset is True

    def test_a_decoded_entry_is_accepted_by_reset_schedule_and_rejected_by_schedule(self):
        """`_reset` is what `Limit.__post_init__` sorts the two tuples by, so a
        decoder that built a plain entry would be caught here even if every
        field above happened to match."""
        from zae_limiter.models import Limit

        entries = decode_reset("m0h0", "UTC")
        assert Limit.quota("rpd", 10, cron="0 0 * * *").with_reset_schedule(entries)
        with pytest.raises(ValueError, match="parameter entries only"):
            Limit.per_minute("rpm", 10).with_schedule(entries)

    def test_empty_compact_decodes_to_an_empty_tuple(self):
        assert decode_reset("", "UTC") == ()

    def test_rejects_a_modifier_token(self):
        """A reset overrides no parameters, so a stored `s500` is either
        corruption or a param schedule read out of the wrong attribute. Either
        way it must not decode into something that silently resets."""
        with pytest.raises(ValueError, match="modifier"):
            decode_reset("m0h0s500", "UTC")

    @pytest.mark.parametrize("tag", ["s500", "c2000", "a100", "p60"])
    def test_rejects_every_modifier_tag(self, tag):
        """All four, not just `scale` — `c`/`a`/`p` reach the same wrong place."""
        with pytest.raises(ValueError, match="modifier"):
            decode_reset(f"m0h0{tag}", "UTC")

    def test_rejects_junk(self):
        with pytest.raises(ValueError):
            decode_reset("this is not a schedule", "UTC")

    def test_round_trip_is_byte_identical_and_semantically_equal(self):
        """Three assertions, because `encode_reset(decode_reset(x)) == x` alone
        is satisfied by an encoder that throws information away."""
        entries = (
            ScheduleEntry.reset("0 0 * * *", "America/New_York"),
            ScheduleEntry.reset("30 2 1 JAN,JUL *", "America/New_York"),
        )
        compact, tz = encode_reset(entries)
        restored = decode_reset(compact, tz)

        assert len(restored) == len(entries)
        for original, back in zip(entries, restored, strict=True):
            assert parse_cron(back.cron, back.tz) == parse_cron(original.cron, original.tz)
        assert encode_reset(restored) == (compact, tz)
        assert decode_reset(*encode_reset(restored)) == restored

    def test_the_display_form_re_encodes_unchanged(self):
        """`to_cron` renders names back; feeding that to a fresh reset entry
        must produce the same bytes, or the CLI's output is not round-trippable
        (§4.3)."""
        compact, tz = encode_reset((ScheduleEntry.reset("0 0 * * 1-5", "UTC"),))
        rendered = to_cron(compact)
        assert rendered == "0 0 * * MON-FRI"
        assert encode_reset((ScheduleEntry.reset(rendered, tz),)) == (compact, tz)

    def test_a_param_schedule_read_out_of_the_reset_attribute_is_caught(self):
        """The realistic corruption: `sched` and `rsched` swapped. Every param
        entry carries a modifier, so every one of them is rejected here."""
        param_compact, tz = encode((BUSINESS, NIGHTS))
        with pytest.raises(ValueError, match="modifier"):
            decode_reset(param_compact, tz)


class TestDecodeRaisesValueErrorForTheAggregatorsSake:
    """`processor._decode_schedule` catches `ValueError` specifically (#222 §6).

    Raising anything else from the parser slips through that catch and aborts
    the whole stream batch — `aggregate_bucket_states` is outside any try —
    which is the poison pill core plan Task 14 fixed, and it would take usage
    snapshots down with it. The client-side conversion to
    `RateLimiterUnavailable` belongs at the Repository boundary, not here, and
    `schedule.py` must stay free of any `zae_limiter` import so `models` can use
    it without a cycle and both Lambdas can vendor it.

    A regression guard, so it passes on the day it is written.
    """

    @pytest.mark.parametrize(
        "compact", ["this is not a schedule", "Xh9-17s500", "h9-17s500s600", "v9:h9-17"]
    )
    def test_decode_raises_value_error(self, compact):
        with pytest.raises(ValueError):
            decode(compact, "UTC")

    @pytest.mark.parametrize("compact", ["this is not a schedule", "m0h0s500", "zzz"])
    def test_decode_reset_raises_value_error(self, compact):
        """The reset decoder is the second parser and fails into the same
        channel — including for a modifier token, which it rejects rather than
        ignores."""
        with pytest.raises(ValueError):
            decode_reset(compact, "UTC")

    def test_decode_does_not_raise_an_infrastructure_error(self):
        """Explicit, because `RateLimiterUnavailable` is not a `ValueError`:
        the aggregator's `except ValueError` would not catch it and the failure
        would be invisible until a stream stalled in production."""
        from zae_limiter.exceptions import InfrastructureError

        with pytest.raises(ValueError) as excinfo:
            decode("this is not a schedule", "UTC")
        assert not isinstance(excinfo.value, InfrastructureError)

    def test_schedule_module_imports_nothing_from_the_package(self):
        """The other half of why the conversion cannot live here: importing
        `exceptions` would end the one-way dependency that lets `models` import
        `ScheduleEntry` and both Lambda stubs vendor this file."""
        import ast
        import pathlib

        import zae_limiter.schedule as sched_mod

        tree = ast.parse(pathlib.Path(sched_mod.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                imported.add(node.module or "")
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("zae_limiter"):
                imported.add(node.module or "")
            elif isinstance(node, ast.Import):
                imported.update(
                    alias.name for alias in node.names if alias.name.startswith("zae_limiter")
                )
        assert imported == set(), f"schedule.py must import nothing from zae_limiter: {imported}"

    def test_an_unknown_tag_only_reaches_the_tokeniser_at_an_entry_boundary(self):
        """The heuristic that replaces the version marker (#515) is *weaker*
        than Task 10's Decision 1 claims, and this is where that is pinned.

        Decision 1 says a newer client's unknown tag "lands there with a precise
        offset", so the log can tell a forward-compatibility problem from
        corruption. It only does so when the unknown tag stands where a *tag* is
        expected — the start of an entry. `_TOKEN_RE` takes a value as "anything
        that is not a known tag letter", so an unknown tag anywhere *after* a
        value is swallowed into that value and fails downstream instead, as a
        cronsim rejection or a bare `int()` error that names neither the tag nor
        the offset. That covers the realistic shape of a new modifier tag, which
        a newer encoder would append after the cron fields.

        So the distinction is not merely "not a proof" (corruption can fail at
        an offset too); it is also incomplete in the other direction. The §6
        text says so rather than overselling it.
        """
        # Entry-initial: the tokeniser sees it and reports the offset.
        with pytest.raises(ValueError, match="cannot parse from offset 0"):
            decode("q42h9-17s500", "UTC")
        with pytest.raises(ValueError, match="cannot parse from offset 0"):
            decode("h9-17s500;q42m0", "UTC")

        # Mid-entry: absorbed into the preceding value. Neither message
        # mentions a tag or an offset.
        with pytest.raises(ValueError, match="invalid cron expression"):
            decode("h9-17q42s500", "UTC")
        with pytest.raises(ValueError, match="invalid literal for int"):
            decode("h9-17s500q42", "UTC")

        # And a genuinely bad cron field reads the same way as that third case,
        # which is the collision the heuristic cannot see through.
        with pytest.raises(ValueError, match="invalid cron expression"):
            decode("h99", "UTC")
