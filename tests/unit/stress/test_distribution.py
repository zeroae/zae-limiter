"""Tests for traffic distribution."""

import random
import time
from unittest.mock import patch

import pytest

from zae_limiter.stress.config import LoadDistribution
from zae_limiter.stress.distribution import TrafficDistributor


class TestTrafficDistributor:
    """Tests for TrafficDistributor."""

    @pytest.fixture
    def distributor(self):
        """Create a distributor with default config."""
        return TrafficDistributor(LoadDistribution())

    def test_pick_entity_returns_tuple(self, distributor):
        """pick_entity returns (entity_id, traffic_type)."""
        entity_id, traffic_type = distributor.pick_entity()

        assert isinstance(entity_id, str)
        assert traffic_type in ("whale", "spike", "spike-baseline", "powerlaw")

    def test_whale_traffic_share(self, distributor):
        """Whale entity receives approximately 50% of non-spike traffic."""
        random.seed(42)
        samples = 10000

        # Disable spike for this test to measure whale share accurately
        distributor.config.spike_probability = 0.0

        whale_count = sum(1 for _ in range(samples) if distributor.pick_entity()[1] == "whale")

        # Allow 5% tolerance
        expected = samples * 0.50
        assert abs(whale_count - expected) < samples * 0.05

    def test_powerlaw_distribution(self, distributor):
        """Power law entities follow Zipf distribution."""
        random.seed(42)
        samples = 10000
        entity_counts: dict[str, int] = {}

        for _ in range(samples):
            entity_id, traffic_type = distributor.pick_entity()
            if traffic_type == "powerlaw":
                entity_counts[entity_id] = entity_counts.get(entity_id, 0) + 1

        # Top entities should have significantly more traffic
        if entity_counts:
            counts = sorted(entity_counts.values(), reverse=True)
            # Top 10% of seen entities should have majority of powerlaw traffic
            top_10_pct = int(len(counts) * 0.1) or 1
            top_traffic = sum(counts[:top_10_pct])
            total_traffic = sum(counts)
            assert top_traffic / total_traffic > 0.3  # Top 10% gets >30%

    def test_spike_activation(self, distributor):
        """Spike activates probabilistically and lasts for duration."""
        # Force spike activation
        with patch("random.random", return_value=0.001):
            distributor._check_spike()

        assert distributor._state.in_spike is True
        assert distributor._state.spike_end_time > time.time()

    def test_spike_deactivation(self, distributor):
        """Spike deactivates after duration expires."""
        # Activate spike with expired end time
        distributor._state.in_spike = True
        distributor._state.spike_end_time = time.time() - 1

        distributor._check_spike()

        assert distributor._state.in_spike is False
