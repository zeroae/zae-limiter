"""Traffic distribution for stress testing."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import LoadDistribution


@dataclass
class _SpikeState:
    """Internal state for spike tracking."""

    in_spike: bool = False
    spike_end_time: float = 0.0


@dataclass
class TrafficDistributor:
    """Selects entities based on configured distribution.

    Implements:
    - Whale entity: Single entity receiving majority of traffic
    - Spike entity: Entity with periodic 1500 RPM bursts
    - Power law: Zipf distribution across remaining entities
    """

    config: LoadDistribution
    _state: _SpikeState = field(default_factory=_SpikeState)
    _powerlaw_probs: list[float] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        """Pre-compute power law probabilities."""
        self._powerlaw_probs = self._compute_powerlaw_probs()

    def _compute_powerlaw_probs(self) -> list[float]:
        """Compute Zipf distribution probabilities."""
        # P(rank) ∝ 1 / rank^alpha
        weights = [
            1.0 / (rank**self.config.powerlaw_alpha)
            for rank in range(1, self.config.powerlaw_entity_count + 1)
        ]
        total = sum(weights)
        return [w / total for w in weights]

    def pick_entity(self) -> tuple[str, str]:
        """Pick entity based on distribution.

        Returns:
            Tuple of (entity_id, traffic_type) where traffic_type is one of:
            - "whale": The whale entity
            - "spike": Spike entity during active spike
            - "spike-baseline": Spike entity during baseline
            - "powerlaw": Entity selected via power law distribution
        """
        # Check for spike first (spike entity dominates during spike)
        if self._check_spike():
            return self.config.spike_entity_id, "spike"

        roll = random.random()

        # Whale: 50% of non-spike traffic
        if roll < self.config.whale_traffic_share:
            return self.config.whale_entity_id, "whale"

        # Spike entity baseline: 3%
        threshold = self.config.whale_traffic_share + self.config.spike_traffic_share
        if roll < threshold:
            return self.config.spike_entity_id, "spike-baseline"

        # Power law for the rest: 47%
        idx = self._pick_powerlaw_index()
        return f"entity-{idx:05d}", "powerlaw"

    def _pick_powerlaw_index(self) -> int:
        """Select entity index using power law distribution."""
        r = random.random()
        cumulative = 0.0
        for idx, prob in enumerate(self._powerlaw_probs):
            cumulative += prob
            if r < cumulative:
                return idx
        return len(self._powerlaw_probs) - 1

    def _check_spike(self) -> bool:
        """Check and update spike state.

        Returns:
            True if spike is currently active.
        """
        now = time.time()

        # End current spike if expired
        if self._state.in_spike and now > self._state.spike_end_time:
            self._state.in_spike = False

        # Maybe start new spike (probability scaled per-request)
        if not self._state.in_spike:
            # Check once per ~60 requests on average
            if random.random() < self.config.spike_probability / 60:
                self._state.in_spike = True
                self._state.spike_end_time = now + self.config.spike_duration_seconds

        return self._state.in_spike
