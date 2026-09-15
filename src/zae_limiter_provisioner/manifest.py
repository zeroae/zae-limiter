"""YAML manifest parsing and validation for declarative limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from zae_limiter.schedule import ScheduleEntry

# The fields a YAML schedule entry may carry, mirroring `ScheduleEntry`'s public
# ones. `_reset` is deliberately absent: it is what separates the two tuples, and
# a reset entry smuggled into `schedule` would win its window and then supply no
# parameters, shadowing every entry below it (design §3.6).
_ENTRY_FIELDS = ("cron", "tz", "scale", "capacity", "refill_amount", "refill_period_seconds")
_RESET_ENTRY_FIELDS = ("cron", "tz")


def _entry_to_dict(entry: ScheduleEntry) -> dict[str, Any]:
    """Emit only the fields that are set, so ``from_dict(to_dict(x)) == x``.

    An explicit ``None`` would round-trip into ``ScheduleEntry`` as "unset" for
    the absolutes but would break the exactly-one-of rule if it were emitted for
    ``scale``, so absent is the only faithful spelling.
    """
    d: dict[str, Any] = {"cron": entry.cron, "tz": entry.tz}
    for name in ("scale", "capacity", "refill_amount", "refill_period_seconds"):
        value = getattr(entry, name)
        if value is not None:
            d[name] = value
    return d


def _parse_entries(raw: Any, *, key: str, reset: bool) -> tuple[ScheduleEntry, ...]:
    """Parse a YAML ``schedule`` / ``reset_schedule`` list into ``ScheduleEntry``.

    Every failure is a ``ValueError`` naming the offending entry, because that is
    what ``limits plan`` can report back to the manifest author. Handing the
    entry straight to ``ScheduleEntry(**entry)`` would surface a mistyped or
    unsupported key as ``TypeError: __init__() got an unexpected keyword
    argument`` — and for a reset entry, which takes ``cron``/``tz`` only, *every*
    modifier arrives that way, so the shape has to be checked before the call
    rather than left to the constructor's own guard.

    ``ScheduleEntry.__post_init__`` then validates cron, timezone, the extended
    tokens and the modifier rules, so an unusable manifest fails here rather than
    inside the Lambda.
    """
    if raw is None:
        # `schedule:` with nothing under it is YAML null, not a list.
        return ()
    if not isinstance(raw, list):
        raise ValueError(
            f"{key} must be a list of entries, got {type(raw).__name__}. "
            "Limits are rejected at parse time so `limits plan` surfaces the "
            "problem before anything is written."
        )

    allowed = _RESET_ENTRY_FIELDS if reset else _ENTRY_FIELDS
    entries: list[ScheduleEntry] = []
    for i, entry in enumerate(raw):
        where = f"{key}[{i}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be a mapping, got {type(entry).__name__}.")
        unknown = sorted(str(k) for k in entry if k not in allowed)
        if unknown:
            raise ValueError(
                f"{where} has unknown field(s) {unknown}; allowed: {list(allowed)}."
                + (
                    " A reset entry carries `cron` and `tz` only — it names the "
                    "instant the balance goes back to the effective capacity and "
                    "overrides no parameters."
                    if reset
                    else ""
                )
            )
        if "cron" not in entry:
            raise ValueError(f"{where} is missing the required `cron` field.")
        try:
            entries.append(ScheduleEntry.reset(**entry) if reset else ScheduleEntry(**entry))
        except ValueError as exc:
            raise ValueError(f"{where}: {exc}") from exc
    return tuple(entries)


@dataclass(frozen=True)
class LimitDecl:
    """A single limit declaration with shorthand defaults.

    ``capacity`` is the bucket ceiling (max tokens). The separate ``burst``
    field was removed in the Limit model refactor — callers that want burst
    behaviour should set ``capacity`` to the burst value directly.

    ``schedule`` answers "what is the limit right now?" and ``reset_schedule``
    "when does the balance go back to full, in one lump?" (#222 §1.1). They are
    independent tuples: a quota may also be scaled by a parameter schedule.
    """

    capacity: int
    refill_amount: int
    refill_period: int
    schedule: tuple[ScheduleEntry, ...] = ()
    reset_schedule: tuple[ScheduleEntry, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LimitDecl:
        capacity = d["capacity"]
        # Accept "burst" from YAML for backwards-compat: use it as capacity
        if "burst" in d:
            capacity = d["burst"]
        refill_period = d.get("refill_period", 60)

        schedule = _parse_entries(d.get("schedule"), key="schedule", reset=False)
        reset_schedule = _parse_entries(d.get("reset_schedule"), key="reset_schedule", reset=True)

        # ADR-137: a limit drips or resets, never both. A reset flips the
        # shorthand default from `capacity` to 0 so the natural manifest — one
        # that names only the allowance and the schedule — is the valid one,
        # rather than failing with a message about a field the author never
        # wrote. That single boolean is the whole discriminator.
        resets = bool(reset_schedule)
        refill_amount = d.get("refill_amount", 0 if resets else capacity)

        for field_name, value in (
            ("capacity", capacity),
            ("refill_period", refill_period),
        ):
            if value <= 0:
                raise ValueError(
                    f"{field_name} must be positive, got {value}. "
                    "Limits are rejected at parse time so `limits plan` surfaces the "
                    "problem before anything is written."
                )
        if refill_amount < 0:
            raise ValueError(
                f"refill_amount must not be negative, got {refill_amount}. "
                "Limits are rejected at parse time so `limits plan` surfaces the "
                "problem before anything is written."
            )
        if refill_amount == 0 and not resets:
            raise ValueError(
                "refill_amount=0 means the limit does not drip, which is only valid "
                "alongside a reset_schedule; otherwise the bucket can never recover "
                "(ADR-137)."
            )
        if refill_amount > 0 and resets:
            raise ValueError(
                "a limit drips or resets, never both: a positive refill_amount "
                f"({refill_amount}) alongside a reset_schedule grants roughly twice "
                "the intended allowance per period. Omit refill_amount and it "
                "defaults to 0 (ADR-137)."
            )
        # ADR-137 is a rule about the limit, not about one field. A `schedule`
        # entry may override `refill_amount`, so a quota carrying both tuples can
        # be handed a positive rate inside a window — the drip-and-reset pairing
        # by the back door, and invisible in the base parameters. `scale` and the
        # `capacity` / `refill_period_seconds` overrides stay legal: scaling a
        # zero rate leaves it zero, so a quota can still be halved at weekends.
        if resets:
            for i, entry in enumerate(schedule):
                if entry.refill_amount is not None:
                    raise ValueError(
                        f"schedule[{i}] sets refill_amount={entry.refill_amount} on a "
                        "limit that also has a reset_schedule. A limit drips or resets, "
                        "never both, and that holds inside every scheduled window as "
                        "well as at the base (ADR-137)."
                    )
        return cls(
            capacity=capacity,
            refill_amount=refill_amount,
            refill_period=refill_period,
            schedule=schedule,
            reset_schedule=reset_schedule,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "capacity": self.capacity,
            "refill_amount": self.refill_amount,
            "refill_period": self.refill_period,
        }
        # Emitted only when declared, so an unscheduled manifest's wire shape is
        # byte-for-byte what it was before schedules existed.
        if self.schedule:
            result["schedule"] = [_entry_to_dict(e) for e in self.schedule]
        if self.reset_schedule:
            result["reset_schedule"] = [_entry_to_dict(e) for e in self.reset_schedule]
        return result


@dataclass(frozen=True)
class SystemDecl:
    """System-level limit declaration."""

    limits: dict[str, LimitDecl]
    on_unavailable: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SystemDecl:
        limits = {name: LimitDecl.from_dict(val) for name, val in d.get("limits", {}).items()}
        return cls(limits=limits, on_unavailable=d.get("on_unavailable"))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "limits": {name: lim.to_dict() for name, lim in self.limits.items()},
        }
        if self.on_unavailable is not None:
            result["on_unavailable"] = self.on_unavailable
        return result


@dataclass(frozen=True)
class ResourceDecl:
    """Resource-level limit declaration."""

    limits: dict[str, LimitDecl]
    disabled: bool | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ResourceDecl:
        limits = {name: LimitDecl.from_dict(val) for name, val in d.get("limits", {}).items()}
        return cls(limits=limits, disabled=d.get("disabled"))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "limits": {name: lim.to_dict() for name, lim in self.limits.items()}
        }
        if self.disabled is not None:
            result["disabled"] = self.disabled
        return result


@dataclass(frozen=True)
class EntityResourceDecl:
    """Entity-resource-level limit declaration."""

    limits: dict[str, LimitDecl]
    disabled: bool | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EntityResourceDecl:
        limits = {name: LimitDecl.from_dict(val) for name, val in d.get("limits", {}).items()}
        return cls(limits=limits, disabled=d.get("disabled"))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "limits": {name: lim.to_dict() for name, lim in self.limits.items()}
        }
        if self.disabled is not None:
            result["disabled"] = self.disabled
        return result


@dataclass(frozen=True)
class EntityDecl:
    """Entity-level declaration with per-resource limits."""

    resources: dict[str, EntityResourceDecl]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EntityDecl:
        resources = {
            name: EntityResourceDecl.from_dict(val) for name, val in d.get("resources", {}).items()
        }
        return cls(resources=resources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resources": {name: res.to_dict() for name, res in self.resources.items()},
        }


@dataclass(frozen=True)
class LimitsManifest:
    """Parsed YAML manifest for declarative limits management."""

    namespace: str
    system: SystemDecl | None = None
    resources: dict[str, ResourceDecl] = field(default_factory=dict)
    entities: dict[str, EntityDecl] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LimitsManifest:
        namespace = d.get("namespace")
        if not namespace:
            raise ValueError("'namespace' is required in limits manifest")

        system = SystemDecl.from_dict(d["system"]) if "system" in d else None

        resources = {
            name: ResourceDecl.from_dict(val) for name, val in d.get("resources", {}).items()
        }

        entities = {name: EntityDecl.from_dict(val) for name, val in d.get("entities", {}).items()}

        return cls(
            namespace=namespace,
            system=system,
            resources=resources,
            entities=entities,
        )

    @classmethod
    def from_yaml(cls, yaml_str: str) -> LimitsManifest:
        import yaml

        data = yaml.safe_load(yaml_str)
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"namespace": self.namespace}
        if self.system is not None:
            result["system"] = self.system.to_dict()
        if self.resources:
            result["resources"] = {name: res.to_dict() for name, res in self.resources.items()}
        if self.entities:
            result["entities"] = {name: ent.to_dict() for name, ent in self.entities.items()}
        return result

    def managed_set(self) -> dict[str, Any]:
        """Extract the set of managed items from this manifest.

        Returns:
            Dict with managed_system (bool), managed_resources (list[str]),
            managed_entities (dict[str, list[str]]).
        """
        return {
            "managed_system": self.system is not None,
            "managed_resources": sorted(self.resources.keys()),
            "managed_entities": {
                entity_id: sorted(entity.resources.keys())
                for entity_id, entity in self.entities.items()
            },
        }
