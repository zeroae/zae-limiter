"""Tests for stress test configuration."""

import pytest

from zae_limiter.stress.config import LoadDistribution, StressConfig


class TestLoadDistribution:
    """Tests for LoadDistribution dataclass."""

    def test_default_values(self):
        """Default distribution has whale, spike, and powerlaw shares summing to 1."""
        dist = LoadDistribution()

        total = dist.whale_traffic_share + dist.spike_traffic_share + dist.powerlaw_traffic_share
        assert total == pytest.approx(1.0)

    def test_custom_distribution(self):
        """Custom distribution values are respected."""
        dist = LoadDistribution(
            whale_traffic_share=0.60,
            spike_traffic_share=0.05,
            powerlaw_traffic_share=0.35,
        )

        assert dist.whale_traffic_share == 0.60
        assert dist.spike_traffic_share == 0.05
        assert dist.powerlaw_traffic_share == 0.35

    def test_from_environment(self, monkeypatch):
        """LoadDistribution can be created from environment variables."""
        monkeypatch.setenv("WHALE_TRAFFIC_SHARE", "0.40")
        monkeypatch.setenv("SPIKE_RPM", "2000")
        monkeypatch.setenv("POWERLAW_ALPHA", "2.0")

        dist = LoadDistribution.from_environment()

        assert dist.whale_traffic_share == 0.40
        assert dist.spike_rpm == 2000
        assert dist.powerlaw_alpha == 2.0


class TestStressConfig:
    """Tests for StressConfig dataclass."""

    def test_default_values(self):
        """StressConfig has sensible defaults."""
        config = StressConfig(stack_name="test-stack")

        assert config.stack_name == "test-stack"
        assert config.region == "us-east-1"
        assert config.baseline_rpm == 400
        assert config.num_entities == 16_000
        assert config.num_apis == 8

    def test_from_environment(self, monkeypatch):
        """StressConfig can be created from environment variables."""
        monkeypatch.setenv("TARGET_STACK_NAME", "my-limiter")
        monkeypatch.setenv("TARGET_REGION", "us-west-2")
        monkeypatch.setenv("BASELINE_RPM", "600")

        config = StressConfig.from_environment()

        assert config.stack_name == "my-limiter"
        assert config.region == "us-west-2"
        assert config.baseline_rpm == 600
