"""LLM gateway scenario: multiple models, RPM + TPM limits, lease adjustments.

Usage:
    locust -f locustfiles/llm_gateway.py --host <stack-name>

Simulates an LLM gateway where each user sends requests to one of 8 models.
Each acquire consumes 1 RPM and an estimated TPM, then adjusts the lease
with the actual token count after the "LLM call" completes.
"""

from __future__ import annotations

import os
import random
import uuid

from locust import between, events, task
from locust.runners import WorkerRunner

from zae_limiter import Limit, SyncRateLimiter
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

_created_entities: set[str] = set()


def _make_limiter(environment: object) -> SyncRateLimiter:
    host = getattr(environment, "host", None)
    stack_name = host or os.environ.get("TARGET_STACK_NAME", "")
    region = os.environ.get("TARGET_REGION", "us-east-1")
    return SyncRateLimiter(name=stack_name, region=region)


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Set resource defaults once (master or standalone only)."""
    if isinstance(environment.runner, WorkerRunner):
        return
    limiter = _make_limiter(environment)
    for model, limits in MODEL_LIMITS.items():
        limiter.set_resource_defaults(model, limits)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Clean up entities created by this process."""
    if not _created_entities:
        return
    limiter = _make_limiter(environment)
    for entity_id in _created_entities:
        limiter.delete_entity(entity_id)
    _created_entities.clear()


class LLMGatewayUser(RateLimiterUser):
    """Simulates an LLM gateway user sending requests across multiple models."""

    wait_time = between(0.5, 2.0)  # type: ignore[no-untyped-call]

    def on_start(self) -> None:
        """Assign a unique entity ID."""
        self.entity_id = f"apikey-{uuid.uuid4().hex[:8]}"
        _created_entities.add(self.entity_id)

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
