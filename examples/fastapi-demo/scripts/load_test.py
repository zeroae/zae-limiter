#!/usr/bin/env python3
"""Simple load testing script for the FastAPI demo."""

import argparse
import asyncio
import random
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class Stats:
    """Statistics tracker."""

    total_requests: int = 0
    successful: int = 0
    rate_limited: int = 0
    errors: int = 0
    total_tokens: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    @property
    def requests_per_second(self) -> float:
        return self.total_requests / self.elapsed if self.elapsed > 0 else 0


API_KEYS = ["key-alice", "key-bob", "key-charlie"]
MESSAGES = [
    "Hello, how are you?",
    "Explain quantum computing in simple terms.",
    "Write a haiku about programming.",
    "What is the meaning of life?",
    "Tell me a joke about developers.",
    "How does rate limiting work?",
    "Describe the token bucket algorithm.",
    "What are the benefits of async programming?",
]


async def make_request(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    stats: Stats,
) -> None:
    """Make a single API request."""
    message = random.choice(MESSAGES)

    try:
        response = await client.post(
            f"{base_url}/v1/chat/completions",
            headers={"X-API-Key": api_key},
            json={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": message}],
            },
            timeout=30.0,
        )

        stats.total_requests += 1

        if response.status_code == 200:
            stats.successful += 1
            data = response.json()
            stats.total_tokens += data.get("usage", {}).get("total_tokens", 0)
        elif response.status_code == 429:
            stats.rate_limited += 1
        else:
            stats.errors += 1
            print(f"Unexpected status: {response.status_code}")

    except Exception as e:
        stats.total_requests += 1
        stats.errors += 1
        print(f"Error: {e}")


async def worker(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    stats: Stats,
    duration: float,
    delay: float,
) -> None:
    """Worker that sends requests continuously."""
    end_time = time.time() + duration

    while time.time() < end_time:
        await make_request(client, base_url, api_key, stats)
        await asyncio.sleep(delay)


async def run_load_test(
    base_url: str,
    concurrency: int,
    duration: float,
    delay: float,
) -> Stats:
    """Run the load test."""
    stats = Stats()

    async with httpx.AsyncClient() as client:
        # Create workers with different API keys
        tasks = []
        for i in range(concurrency):
            api_key = API_KEYS[i % len(API_KEYS)]
            task = asyncio.create_task(worker(client, base_url, api_key, stats, duration, delay))
            tasks.append(task)

        # Wait for all workers
        await asyncio.gather(*tasks)

    return stats


def print_stats(stats: Stats) -> None:
    """Print test statistics."""
    print("\n" + "=" * 50)
    print("LOAD TEST RESULTS")
    print("=" * 50)
    print(f"Duration:          {stats.elapsed:.1f}s")
    print(f"Total Requests:    {stats.total_requests}")
    print(f"Successful:        {stats.successful}")
    print(f"Rate Limited:      {stats.rate_limited}")
    print(f"Errors:            {stats.errors}")
    print(f"Requests/second:   {stats.requests_per_second:.1f}")
    print(f"Total Tokens:      {stats.total_tokens:,}")
    print("=" * 50)

    if stats.total_requests > 0:
        success_rate = (stats.successful / stats.total_requests) * 100
        rate_limit_rate = (stats.rate_limited / stats.total_requests) * 100
        print(f"Success Rate:      {success_rate:.1f}%")
        print(f"Rate Limit Rate:   {rate_limit_rate:.1f}%")
    print("=" * 50 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Load test the zae-limiter demo API")
    parser.add_argument(
        "--url",
        default="http://localhost:8080",
        help="Base URL of the API (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--concurrency",
        "-c",
        type=int,
        default=3,
        help="Number of concurrent workers (default: 3)",
    )
    parser.add_argument(
        "--duration",
        "-d",
        type=float,
        default=30.0,
        help="Test duration in seconds (default: 30)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.1,
        help="Delay between requests per worker in seconds (default: 0.1)",
    )

    args = parser.parse_args()

    print(f"Starting load test against {args.url}")
    print(f"Concurrency: {args.concurrency} workers")
    print(f"Duration: {args.duration}s")
    print(f"Request delay: {args.delay}s")
    print("Press Ctrl+C to stop early...\n")

    try:
        stats = asyncio.run(
            run_load_test(
                base_url=args.url,
                concurrency=args.concurrency,
                duration=args.duration,
                delay=args.delay,
            )
        )
        print_stats(stats)
    except KeyboardInterrupt:
        print("\nInterrupted by user")


if __name__ == "__main__":
    main()
