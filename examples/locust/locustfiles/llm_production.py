"""Production-realistic LLM gateway load testing with custom load shapes.

Usage:
    locust -f locustfiles/llm_production.py --host <stack-name>

Includes:
- LLMGatewayUser: Basic user with single task
- LLMGatewayUserReal: Realistic user with weighted tasks (chat, list_models, available)
- ProductionDailyLoad: 24-hour production traffic pattern
- MorningSpike: Morning traffic spike simulation
"""

from __future__ import annotations

import random
import uuid

from locust import LoadTestShape, between, task

from zae_limiter import Limit
from zae_limiter.locust import RateLimiterUser

MODELS = [
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/o1",
    "openai/o3-mini",
    "anthropic/claude-sonnet",
    "anthropic/claude-haiku",
    "google/gemini-pro",
    "google/gemini-flash",
]

MODEL_LIMITS: dict[str, list[Limit]] = {
    "openai/gpt-4o": [Limit.per_minute("rpm", 500), Limit.per_minute("tpm", 800_000)],
    "openai/gpt-4o-mini": [Limit.per_minute("rpm", 2000), Limit.per_minute("tpm", 4_000_000)],
    "openai/o1": [Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 200_000)],
    "openai/o3-mini": [Limit.per_minute("rpm", 200), Limit.per_minute("tpm", 400_000)],
    "anthropic/claude-sonnet": [Limit.per_minute("rpm", 1000), Limit.per_minute("tpm", 400_000)],
    "anthropic/claude-haiku": [Limit.per_minute("rpm", 2000), Limit.per_minute("tpm", 4_000_000)],
    "google/gemini-pro": [Limit.per_minute("rpm", 360), Limit.per_minute("tpm", 1_000_000)],
    "google/gemini-flash": [Limit.per_minute("rpm", 1500), Limit.per_minute("tpm", 4_000_000)],
}

# Token estimation: average prompt ~500 tokens, response ~300 tokens
TPM_ESTIMATE = 800
TPM_ACTUAL_RANGE = (200, 2000)


class LLMGatewayUser(RateLimiterUser):
    """Simulates an LLM gateway user sending requests across multiple models."""

    wait_time = between(0.5, 2.0)  # type: ignore[no-untyped-call]

    _defaults_configured: bool = False

    def on_start(self) -> None:
        """Assign a unique entity ID and configure per-model resource limits."""
        self.entity_id = f"apikey-{uuid.uuid4().hex[:8]}"

        if not LLMGatewayUser._defaults_configured:
            for model, limits in MODEL_LIMITS.items():
                self.client.set_resource_defaults(resource=model, limits=limits)
            LLMGatewayUser._defaults_configured = True

    @task
    def call_model(self) -> None:
        """Send a request to a random model with post-hoc token reconciliation."""
        model = random.choice(MODELS)

        with self.client.acquire(
            entity_id=self.entity_id,
            resource=model,
            consume={"rpm": 1, "tpm": TPM_ESTIMATE},
            name=model,
        ) as lease:
            # Simulate LLM call completing with actual token usage
            actual_tpm = random.randint(*TPM_ACTUAL_RANGE)
            lease.adjust(tpm=actual_tpm - TPM_ESTIMATE)


class LLMGatewayUserReal(RateLimiterUser):
    """Simulates an LLM gateway user sending requests across multiple models."""

    wait_time = between(20, 45)  # type: ignore[no-untyped-call]

    _defaults_configured: bool = False

    def on_start(self) -> None:
        """Assign a unique entity ID and configure per-model resource limits."""
        self.entity_id = f"apikey-{uuid.uuid4().hex[:8]}"

        if not LLMGatewayUser._defaults_configured:
            for model, limits in MODEL_LIMITS.items():
                self.client.set_resource_defaults(resource=model, limits=limits)
            LLMGatewayUser._defaults_configured = True

    @task(15)
    def chat_completion(self) -> None:
        """Send a request to a random model with post-hoc token reconciliation."""
        model = random.choice(MODELS)

        with self.client.acquire(
            entity_id=self.entity_id,
            resource=model,
            consume={"rpm": 1, "tpm": TPM_ESTIMATE},
            name=model,
        ) as lease:
            # Simulate LLM call completing with actual token usage
            actual_tpm = random.randint(*TPM_ACTUAL_RANGE)
            lease.adjust(tpm=actual_tpm - TPM_ESTIMATE)

    @task(2)
    def list_models(self) -> None:
        model = random.choice(MODELS)
        with self.client.acquire(
            entity_id=self.entity_id,
            resource=model,
            consume={"rpm": 1, "tpm": TPM_ESTIMATE},
            name=model,
        ) as lease:
            # Simulate LLM call completing with actual token usage
            actual_tpm = random.randint(*TPM_ACTUAL_RANGE)
            lease.adjust(tpm=actual_tpm - TPM_ESTIMATE)

    @task(1)
    def available(self) -> None:
        model = random.choice(MODELS)
        with self.client.acquire(
            entity_id=self.entity_id,
            resource=model,
            consume={"rpm": 1, "tpm": TPM_ESTIMATE},
            name=model,
        ) as lease:
            # Simulate LLM call completing with actual token usage
            actual_tpm = random.randint(*TPM_ACTUAL_RANGE)
            lease.adjust(tpm=actual_tpm - TPM_ESTIMATE)


class ProductionDailyLoad(LoadTestShape):
    """Simulates a typical production 24-hour load pattern.

    Stages represent different periods of the day with varying user counts
    and spawn rates. Format: (duration_seconds, user_count, spawn_rate)
    """

    stages = [
        # (duration, users, spawn_rate)
        (21600, 15, 1),  # 00-6am: night shift
        (7200, 100, 1),  # 6-8am: early arrivals
        (3600, 150, 2),  # 8-9am: morning rush (everyone starts work)
        (28800, 150, 1),  # 9-17: active work hours
        (7200, 120, 0.5),  # 17-19: late afternoon activity continues
        (14400, 30, 0.5),  # 19-23: evening winding down
        (3600, 15, 0.5),  # 23-00: night shift only
    ]

    def tick(self) -> tuple[int, float] | None:
        """Return (user_count, spawn_rate) based on current run time."""
        run_time = self.get_run_time()

        cumulative = 0
        for duration, users, spawn_rate in self.stages:
            cumulative += duration
            if run_time < cumulative:
                return (users, spawn_rate)

        return None  # Stop test after all stages complete


class MorningSpike(LoadTestShape):
    """Simulates a morning traffic spike pattern.

    Rapid ramp-up to peak, sustained load, then gradual decline.
    Total duration: ~1 hour.
    """

    stages = [
        # (duration, users, spawn_rate)
        (600, 50, 2),  # 10min: ramp to 50 users
        (600, 150, 3),  # 10min: spike to 150 users
        (1800, 150, 1),  # 30min: sustain peak
        (600, 100, 1),  # 10min: decline to 100
    ]

    def tick(self) -> tuple[int, float] | None:
        """Return (user_count, spawn_rate) based on current run time."""
        run_time = self.get_run_time()

        cumulative = 0
        for duration, users, spawn_rate in self.stages:
            cumulative += duration
            if run_time < cumulative:
                return (users, spawn_rate)

        return None  # Stop test after all stages complete
