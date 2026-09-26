"""Shared constants for duration-window (``reset_after``) tests (ADR-139)."""

from datetime import timedelta

from zae_limiter import Limit

FIVE_HOURS_MS = 5 * 3_600_000
"""The length of ``SESSION_10``'s window, in the millisecond unit ``ws`` uses."""

SESSION_10 = Limit.quota("session", 10, reset_after=timedelta(hours=5))
"""A ten-request session quota: small enough to spend down in a few acquires."""

T0 = 1_757_000_000_000
"""A fixed epoch-ms clock reading, so windows anchor at a known instant."""
