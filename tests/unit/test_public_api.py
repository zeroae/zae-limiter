"""The package root's public surface (#534).

`__all__` is the contract, so these tests check it against what is actually
importable in both directions: every promised name resolves, and the names the
public-surface decision deliberately left out of the root stay out of it.
"""

import importlib

import pytest

import zae_limiter
from zae_limiter import schedule

# The only `schedule.py` name promoted to the package root. Everything else in
# `schedule.__all__` is cron parsing, evaluation or storage-encoding machinery
# that application code never names — see CLAUDE.md, "Public API".
PUBLIC_SCHEDULE_NAMES = {"ScheduleEntry"}


def test_documented_schedule_import_works():
    """The import every scheduled-limits example opens with."""
    from zae_limiter import Limit, ScheduleEntry

    assert ScheduleEntry is schedule.ScheduleEntry
    entry = ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5)
    assert Limit.per_minute("rpm", 1000).with_schedule((entry,)).schedule == (entry,)


@pytest.mark.parametrize("name", sorted(zae_limiter.__all__))
def test_every_exported_name_is_importable(name):
    """`from zae_limiter import <name>` succeeds for every name in `__all__`."""
    module = importlib.import_module("zae_limiter")
    assert hasattr(module, name), f"{name} is in __all__ but not importable from the root"


def test_all_has_no_duplicates():
    assert len(zae_limiter.__all__) == len(set(zae_limiter.__all__))


def test_only_schedule_entry_is_promoted_to_the_root():
    """The excluded `schedule` names stay module-scoped.

    They are reachable as `zae_limiter.schedule.*` and always will be; what this
    pins is that they are not part of the root's v1.0.0-frozen surface.
    """
    exported = set(zae_limiter.__all__) & set(schedule.__all__)
    assert exported == PUBLIC_SCHEDULE_NAMES

    for name in set(schedule.__all__) - PUBLIC_SCHEDULE_NAMES:
        assert not hasattr(zae_limiter, name), (
            f"{name} leaked onto the package root; the public surface decision "
            f"keeps it inside zae_limiter.schedule"
        )
