#!/usr/bin/env python3
"""Run all benchmark tests and collect data for performance documentation.

This script automates the Phase 1 benchmark collection process:
1. Runs capacity tests (moto) to validate RCU/WCU claims
2. Runs latency benchmarks (moto) for baseline metrics
3. Runs throughput tests (moto) for TPS measurements
4. Starts LocalStack and runs realistic latency benchmarks
5. Optionally runs AWS benchmarks (requires --run-aws flag)
6. Generates a consolidated benchmark report

Usage:
    # Run moto + LocalStack benchmarks (default)
    python scripts/run_benchmarks.py

    # Include AWS benchmarks (requires AWS credentials)
    python scripts/run_benchmarks.py --run-aws

    # Specify AWS profile for AWS benchmarks
    python scripts/run_benchmarks.py --run-aws --aws-profile zeroae-code/AWSPowerUserAccess

    # Skip LocalStack benchmarks
    python scripts/run_benchmarks.py --skip-localstack

    # Custom output directory
    python scripts/run_benchmarks.py --output-dir ./benchmark-output

Output:
    - latency-moto.json: Moto benchmark results
    - latency-localstack.json: LocalStack benchmark results (if not skipped)
    - latency-aws.json: AWS benchmark results (if --run-aws)
    - benchmark-results.md: Consolidated markdown report
"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def run_command(
    cmd: list[str],
    env: dict[str, str] | None = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command with optional environment variables."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)

    print(f"\n{'=' * 60}")
    print(f"Running: {' '.join(cmd)}")
    print(f"{'=' * 60}\n")

    result = subprocess.run(
        cmd,
        env=full_env,
        check=check,
        capture_output=capture_output,
        text=True,
    )
    return result


def wait_for_localstack(endpoint: str, timeout: int = 60) -> bool:
    """Wait for LocalStack to be ready."""
    import urllib.error
    import urllib.request

    health_url = f"{endpoint}/_localstack/health"
    start_time = time.time()

    print(f"Waiting for LocalStack at {health_url}...")

    while time.time() - start_time < timeout:
        try:
            with urllib.request.urlopen(health_url, timeout=5) as response:
                if response.status == 200:
                    print("LocalStack is ready!")
                    return True
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2)

    print("LocalStack failed to start within timeout")
    return False


def start_localstack() -> bool:
    """Start LocalStack using docker compose."""
    try:
        # Check if already running
        result = run_command(
            ["docker", "compose", "ps", "--format", "json"],
            capture_output=True,
            check=False,
        )
        if "localstack" in result.stdout and "running" in result.stdout.lower():
            print("LocalStack is already running")
            return True

        # Start LocalStack
        run_command(["docker", "compose", "up", "-d"])

        # Wait for it to be ready
        return wait_for_localstack("http://localhost:4566")

    except subprocess.CalledProcessError as e:
        print(f"Failed to start LocalStack: {e}")
        return False


def run_capacity_tests() -> bool:
    """Run capacity tests to validate RCU/WCU claims."""
    print("\n" + "=" * 60)
    print("STEP 1: Running Capacity Tests (moto)")
    print("=" * 60)

    try:
        run_command(
            [
                "uv",
                "run",
                "pytest",
                "tests/benchmark/test_capacity.py",
                "-v",
                "-n0",
            ]
        )
        return True
    except subprocess.CalledProcessError:
        print("Capacity tests failed!")
        return False


def run_latency_benchmarks(output_dir: Path) -> bool:
    """Run moto-based latency benchmarks."""
    print("\n" + "=" * 60)
    print("STEP 2: Running Latency Benchmarks (moto)")
    print("=" * 60)

    output_file = output_dir / "latency-moto.json"

    try:
        run_command(
            [
                "uv",
                "run",
                "pytest",
                "tests/benchmark/test_latency.py",
                "-v",
                "-n0",
                f"--benchmark-json={output_file}",
            ]
        )
        return True
    except subprocess.CalledProcessError:
        print("Latency benchmarks failed!")
        return False


def run_throughput_tests() -> bool:
    """Run throughput tests to measure TPS."""
    print("\n" + "=" * 60)
    print("STEP 3: Running Throughput Tests (moto)")
    print("=" * 60)

    try:
        run_command(
            [
                "uv",
                "run",
                "pytest",
                "tests/benchmark/test_throughput.py",
                "-v",
                "-n0",
                "-s",
            ]
        )
        return True
    except subprocess.CalledProcessError:
        print("Throughput tests failed!")
        return False


def run_localstack_benchmarks(output_dir: Path) -> bool:
    """Run benchmarks against LocalStack."""
    print("\n" + "=" * 60)
    print("STEP 4: Running LocalStack Benchmarks")
    print("=" * 60)

    output_file = output_dir / "latency-localstack.json"

    localstack_env = {
        "AWS_ENDPOINT_URL": "http://localhost:4566",
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
    }

    try:
        run_command(
            [
                "uv",
                "run",
                "pytest",
                "tests/benchmark/test_localstack.py",
                "-v",
                "-n0",
                f"--benchmark-json={output_file}",
            ],
            env=localstack_env,
        )
        return True
    except subprocess.CalledProcessError:
        print("LocalStack benchmarks failed!")
        return False


def run_aws_benchmarks(output_dir: Path, aws_profile: str | None = None) -> bool:
    """Run benchmarks against real AWS."""
    print("\n" + "=" * 60)
    print("STEP 5: Running AWS Benchmarks")
    print("=" * 60)

    output_file = output_dir / "latency-aws.json"

    env = {}
    if aws_profile:
        env["AWS_PROFILE"] = aws_profile

    # Ensure LocalStack endpoint is not set
    env_copy = os.environ.copy()
    env_copy.pop("AWS_ENDPOINT_URL", None)
    env_copy.update(env)

    try:
        subprocess.run(
            [
                "uv",
                "run",
                "pytest",
                "tests/benchmark/test_aws.py",
                "--run-aws",
                "-v",
                "-n0",
                "-s",
                f"--benchmark-json={output_file}",
            ],
            env=env_copy,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        print("AWS benchmarks failed!")
        return False


def generate_report(output_dir: Path) -> bool:
    """Generate consolidated benchmark report."""
    print("\n" + "=" * 60)
    print("STEP 6: Generating Benchmark Report")
    print("=" * 60)

    json_files = list(output_dir.glob("latency-*.json"))
    if not json_files:
        print("No benchmark JSON files found!")
        return False

    output_file = output_dir / "benchmark-results.md"

    try:
        run_command(
            [
                "uv",
                "run",
                "python",
                "scripts/generate_benchmark_report.py",
                *[str(f) for f in json_files],
                "--output",
                str(output_file),
            ]
        )
        return True
    except subprocess.CalledProcessError:
        print("Report generation failed!")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run benchmark tests and collect performance data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--run-aws",
        action="store_true",
        help="Include AWS benchmarks (requires valid AWS credentials)",
    )
    parser.add_argument(
        "--aws-profile",
        default=None,
        help="AWS profile to use for AWS benchmarks",
    )
    parser.add_argument(
        "--skip-localstack",
        action="store_true",
        help="Skip LocalStack benchmarks",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory for benchmark output files (default: current directory)",
    )

    args = parser.parse_args()

    # Ensure output directory exists
    args.output_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, bool] = {}

    # Step 1: Capacity tests
    results["capacity"] = run_capacity_tests()
    if not results["capacity"]:
        print("\nCapacity tests failed - aborting")
        return 1

    # Step 2: Latency benchmarks (moto)
    results["latency_moto"] = run_latency_benchmarks(args.output_dir)

    # Step 3: Throughput tests
    results["throughput"] = run_throughput_tests()

    # Step 4: LocalStack benchmarks
    if not args.skip_localstack:
        if start_localstack():
            results["latency_localstack"] = run_localstack_benchmarks(args.output_dir)
        else:
            print("Skipping LocalStack benchmarks - failed to start")
            results["latency_localstack"] = False
    else:
        print("Skipping LocalStack benchmarks (--skip-localstack)")
        results["latency_localstack"] = None  # type: ignore[assignment]

    # Step 5: AWS benchmarks (optional)
    if args.run_aws:
        results["latency_aws"] = run_aws_benchmarks(args.output_dir, args.aws_profile)
    else:
        print("\nSkipping AWS benchmarks (use --run-aws to include)")
        results["latency_aws"] = None  # type: ignore[assignment]

    # Step 6: Generate report
    results["report"] = generate_report(args.output_dir)

    # Summary
    print("\n" + "=" * 60)
    print("BENCHMARK SUMMARY")
    print("=" * 60)

    for name, success in results.items():
        if success is None:
            status = "SKIPPED"
        elif success:
            status = "PASSED"
        else:
            status = "FAILED"
        print(f"  {name}: {status}")

    # List output files
    print("\nOutput files:")
    for f in args.output_dir.glob("latency-*.json"):
        print(f"  - {f}")
    report_file = args.output_dir / "benchmark-results.md"
    if report_file.exists():
        print(f"  - {report_file}")

    # Return success if all non-skipped tests passed
    failures = [k for k, v in results.items() if v is False]
    if failures:
        print(f"\nFailed: {', '.join(failures)}")
        return 1

    print("\nAll benchmarks completed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
