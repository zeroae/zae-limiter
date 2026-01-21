"""Client-side config caching with TTL support.

This module provides in-memory caching for config data (system defaults,
resource defaults, entity limits) to reduce DynamoDB reads during acquire().

See ADR-103 for design rationale.
"""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from .models import Limit

# Sentinel value to distinguish "no config exists" from "not yet cached"
_NO_CONFIG: object = object()


@dataclass
class CacheEntry:
    """A cached value with expiration time."""

    value: Any
    expires_at: float


@dataclass
class CacheStats:
    """Statistics for cache performance monitoring."""

    hits: int = 0
    misses: int = 0
    size: int = 0
    ttl_seconds: int = 0

    def as_dict(self) -> dict[str, int]:
        """Return stats as a dictionary."""
        return {
            "hits": self.hits,
            "misses": self.misses,
            "size": self.size,
            "ttl": self.ttl_seconds,
        }


@dataclass
class ConfigCache:
    """
    In-memory cache for config data with TTL expiration.

    Provides thread-safe (for sync) and async-safe caching of:
    - System defaults (limits, on_unavailable)
    - Resource defaults (per resource)
    - Entity limits (per entity×resource, with negative caching)

    Thread/async safety:
    - Uses asyncio.Lock for async operations
    - Uses threading.Lock for sync operations
    - Locks are per-cache-instance (no global locking)

    Negative caching:
    - When entity config is not found, caches the "miss" to avoid repeated lookups
    - Uses sentinel _NO_CONFIG to distinguish None from "not cached"

    Args:
        ttl_seconds: Time-to-live for cached entries (0 = disabled)
    """

    ttl_seconds: int = 60
    _enabled: bool = field(init=False, default=True)

    # Cache storage
    _system_defaults: CacheEntry | None = field(init=False, default=None)
    _resource_defaults: dict[str, CacheEntry] = field(init=False, default_factory=dict)
    _entity_limits: dict[tuple[str, str], CacheEntry] = field(init=False, default_factory=dict)

    # Statistics
    _hits: int = field(init=False, default=0)
    _misses: int = field(init=False, default=0)

    # Locks for thread/async safety
    _async_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    _sync_lock: threading.Lock = field(init=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        """Initialize derived state after dataclass init."""
        self._enabled = self.ttl_seconds > 0

    def _is_expired(self, entry: CacheEntry) -> bool:
        """Check if a cache entry has expired."""
        return time.time() > entry.expires_at

    def _make_entry(self, value: Any) -> CacheEntry:
        """Create a new cache entry with TTL."""
        return CacheEntry(value=value, expires_at=time.time() + self.ttl_seconds)

    # -------------------------------------------------------------------------
    # System defaults
    # -------------------------------------------------------------------------

    async def get_system_defaults(
        self,
        fetch_fn: Callable[[], Awaitable[tuple[list["Limit"], str | None]]],
    ) -> tuple[list["Limit"], str | None]:
        """
        Get system defaults, using cache if valid.

        Args:
            fetch_fn: Async function to fetch system defaults from repository

        Returns:
            Tuple of (limits, on_unavailable_str)
        """
        if not self._enabled:
            return await fetch_fn()

        async with self._async_lock:
            # Check cache
            if self._system_defaults is not None and not self._is_expired(self._system_defaults):
                self._hits += 1
                return cast(tuple[list["Limit"], str | None], self._system_defaults.value)

            # Cache miss - fetch and store
            self._misses += 1
            value = await fetch_fn()
            self._system_defaults = self._make_entry(value)
            return value

    def get_system_defaults_sync(
        self,
        fetch_fn: Callable[[], tuple[list["Limit"], str | None]],
    ) -> tuple[list["Limit"], str | None]:
        """
        Get system defaults synchronously, using cache if valid.

        Args:
            fetch_fn: Sync function to fetch system defaults from repository

        Returns:
            Tuple of (limits, on_unavailable_str)
        """
        if not self._enabled:
            return fetch_fn()

        with self._sync_lock:
            # Check cache
            if self._system_defaults is not None and not self._is_expired(self._system_defaults):
                self._hits += 1
                return cast(tuple[list["Limit"], str | None], self._system_defaults.value)

            # Cache miss - fetch and store
            self._misses += 1
            value = fetch_fn()
            self._system_defaults = self._make_entry(value)
            return value

    # -------------------------------------------------------------------------
    # Resource defaults
    # -------------------------------------------------------------------------

    async def get_resource_defaults(
        self,
        resource: str,
        fetch_fn: Callable[[str], Awaitable[list["Limit"]]],
    ) -> list["Limit"]:
        """
        Get resource defaults, using cache if valid.

        Args:
            resource: Resource name
            fetch_fn: Async function to fetch resource defaults from repository

        Returns:
            List of limits (empty if none configured)
        """
        if not self._enabled:
            return await fetch_fn(resource)

        async with self._async_lock:
            # Check cache
            entry = self._resource_defaults.get(resource)
            if entry is not None and not self._is_expired(entry):
                self._hits += 1
                return cast(list["Limit"], entry.value)

            # Cache miss - fetch and store
            self._misses += 1
            value = await fetch_fn(resource)
            self._resource_defaults[resource] = self._make_entry(value)
            return value

    def get_resource_defaults_sync(
        self,
        resource: str,
        fetch_fn: Callable[[str], list["Limit"]],
    ) -> list["Limit"]:
        """
        Get resource defaults synchronously, using cache if valid.

        Args:
            resource: Resource name
            fetch_fn: Sync function to fetch resource defaults from repository

        Returns:
            List of limits (empty if none configured)
        """
        if not self._enabled:
            return fetch_fn(resource)

        with self._sync_lock:
            # Check cache
            entry = self._resource_defaults.get(resource)
            if entry is not None and not self._is_expired(entry):
                self._hits += 1
                return cast(list["Limit"], entry.value)

            # Cache miss - fetch and store
            self._misses += 1
            value = fetch_fn(resource)
            self._resource_defaults[resource] = self._make_entry(value)
            return value

    # -------------------------------------------------------------------------
    # Entity limits (with negative caching)
    # -------------------------------------------------------------------------

    async def get_entity_limits(
        self,
        entity_id: str,
        resource: str,
        fetch_fn: Callable[[str, str], Awaitable[list["Limit"]]],
    ) -> list["Limit"]:
        """
        Get entity limits, using cache if valid (with negative caching).

        Negative caching: If no limits exist for this entity×resource,
        caches the empty result to avoid repeated lookups.

        Args:
            entity_id: Entity ID
            resource: Resource name
            fetch_fn: Async function to fetch entity limits from repository

        Returns:
            List of limits (empty if none configured)
        """
        if not self._enabled:
            return await fetch_fn(entity_id, resource)

        cache_key = (entity_id, resource)

        async with self._async_lock:
            # Check cache
            entry = self._entity_limits.get(cache_key)
            if entry is not None and not self._is_expired(entry):
                self._hits += 1
                # Handle negative cache
                if entry.value is _NO_CONFIG:
                    return []
                return cast(list["Limit"], entry.value)

            # Cache miss - fetch and store
            self._misses += 1
            value = await fetch_fn(entity_id, resource)

            # Store with negative caching
            if value:
                self._entity_limits[cache_key] = self._make_entry(value)
            else:
                # Negative cache: store sentinel to remember "no config"
                self._entity_limits[cache_key] = self._make_entry(_NO_CONFIG)

            return value

    def get_entity_limits_sync(
        self,
        entity_id: str,
        resource: str,
        fetch_fn: Callable[[str, str], list["Limit"]],
    ) -> list["Limit"]:
        """
        Get entity limits synchronously, using cache if valid (with negative caching).

        Args:
            entity_id: Entity ID
            resource: Resource name
            fetch_fn: Sync function to fetch entity limits from repository

        Returns:
            List of limits (empty if none configured)
        """
        if not self._enabled:
            return fetch_fn(entity_id, resource)

        cache_key = (entity_id, resource)

        with self._sync_lock:
            # Check cache
            entry = self._entity_limits.get(cache_key)
            if entry is not None and not self._is_expired(entry):
                self._hits += 1
                # Handle negative cache
                if entry.value is _NO_CONFIG:
                    return []
                return cast(list["Limit"], entry.value)

            # Cache miss - fetch and store
            self._misses += 1
            value = fetch_fn(entity_id, resource)

            # Store with negative caching
            if value:
                self._entity_limits[cache_key] = self._make_entry(value)
            else:
                # Negative cache: store sentinel to remember "no config"
                self._entity_limits[cache_key] = self._make_entry(_NO_CONFIG)

            return value

    # -------------------------------------------------------------------------
    # Cache management
    # -------------------------------------------------------------------------

    def invalidate(self) -> None:
        """
        Invalidate all cached entries.

        Thread-safe. Call this after config changes to force refresh.
        """
        with self._sync_lock:
            self._system_defaults = None
            self._resource_defaults.clear()
            self._entity_limits.clear()

    async def invalidate_async(self) -> None:
        """
        Invalidate all cached entries (async version).

        Async-safe. Call this after config changes to force refresh.
        """
        async with self._async_lock:
            self._system_defaults = None
            self._resource_defaults.clear()
            self._entity_limits.clear()

    def get_stats(self) -> CacheStats:
        """
        Get cache performance statistics.

        Returns:
            CacheStats with hits, misses, size, and TTL
        """
        with self._sync_lock:
            size = (
                (1 if self._system_defaults is not None else 0)
                + len(self._resource_defaults)
                + len(self._entity_limits)
            )
            return CacheStats(
                hits=self._hits,
                misses=self._misses,
                size=size,
                ttl_seconds=self.ttl_seconds,
            )

    @property
    def enabled(self) -> bool:
        """Whether caching is enabled (TTL > 0)."""
        return self._enabled
