"""Configuration models for stress testing."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class LoadDistribution:
    """Configurable traffic distribution for stress testing.

    Traffic is distributed among three categories:
    - Whale: Single entity that receives majority of traffic
    - Spike: Entity that periodically generates 1500 RPM bursts
    - Power law: Remaining entities following Zipf distribution
    """

    # Whale: single entity dominates traffic
    whale_entity_id: str = "entity-whale"
    whale_traffic_share: float = 0.50

    # Spike entity: responsible for 1500 RPM bursts
    spike_entity_id: str = "entity-spiker"
    spike_traffic_share: float = 0.03  # baseline share
    spike_rpm: int = 1500
    spike_probability: float = 0.10  # per minute
    spike_duration_seconds: int = 30

    # Power law for the rest
    powerlaw_traffic_share: float = 0.47
    powerlaw_alpha: float = 1.5  # Zipf exponent (higher = more skewed)
    powerlaw_entity_count: int = 15_998

    @classmethod
    def from_environment(cls) -> LoadDistribution:
        """Create LoadDistribution from environment variables."""
        return cls(
            whale_entity_id=os.environ.get("WHALE_ENTITY_ID", "entity-whale"),
            whale_traffic_share=float(os.environ.get("WHALE_TRAFFIC_SHARE", "0.50")),
            spike_entity_id=os.environ.get("SPIKE_ENTITY_ID", "entity-spiker"),
            spike_traffic_share=float(os.environ.get("SPIKE_TRAFFIC_SHARE", "0.03")),
            spike_rpm=int(os.environ.get("SPIKE_RPM", "1500")),
            spike_probability=float(os.environ.get("SPIKE_PROBABILITY", "0.10")),
            spike_duration_seconds=int(os.environ.get("SPIKE_DURATION_SECONDS", "30")),
            powerlaw_traffic_share=float(os.environ.get("POWERLAW_TRAFFIC_SHARE", "0.47")),
            powerlaw_alpha=float(os.environ.get("POWERLAW_ALPHA", "1.5")),
            powerlaw_entity_count=int(os.environ.get("POWERLAW_ENTITY_COUNT", "15998")),
        )


@dataclass
class StressConfig:
    """Configuration for stress test execution."""

    # Target stack
    stack_name: str
    region: str = "us-east-1"

    # Traffic shape
    baseline_rpm: int = 400
    duration_seconds: int = 300

    # Distribution
    distribution: LoadDistribution = field(default_factory=LoadDistribution)

    # Entity setup
    num_entities: int = 16_000
    num_apis: int = 8
    num_custom_limit_entities: int = 300

    @classmethod
    def from_environment(cls) -> StressConfig:
        """Create StressConfig from environment variables."""
        return cls(
            stack_name=os.environ["TARGET_STACK_NAME"],
            region=os.environ.get("TARGET_REGION", "us-east-1"),
            baseline_rpm=int(os.environ.get("BASELINE_RPM", "400")),
            duration_seconds=int(os.environ.get("DURATION_SECONDS", "300")),
            distribution=LoadDistribution.from_environment(),
            num_entities=int(os.environ.get("NUM_ENTITIES", "16000")),
            num_apis=int(os.environ.get("NUM_APIS", "8")),
            num_custom_limit_entities=int(os.environ.get("NUM_CUSTOM_LIMITS", "300")),
        )
