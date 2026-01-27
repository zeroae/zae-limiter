#!/usr/bin/env python3
"""Split gh-pages benchmark data.js into tier-specific directories.

One-time migration script for issue #237. Splits the existing
benchmarks/data.js (which contains mixed moto + LocalStack results)
into separate benchmarks/moto/data.js and benchmarks/localstack/data.js.

Usage:
    # Clone gh-pages branch and run the split
    git worktree add /tmp/gh-pages gh-pages
    python scripts/split_benchmark_data.py /tmp/gh-pages/benchmarks
    cd /tmp/gh-pages && git add -A && git commit -m "Split benchmark data into tiers"
    git push origin gh-pages
    git worktree remove /tmp/gh-pages

    # Dry-run mode (prints stats without writing)
    python scripts/split_benchmark_data.py /tmp/gh-pages/benchmarks --dry-run
"""

import argparse
import copy
import json
import shutil
import sys
from pathlib import Path

# Benchmark names containing these patterns are classified as LocalStack
LOCALSTACK_PATTERNS = [
    "test_localstack.py::",
    "TestLocalStackBenchmarks::",
    "TestLocalStackLatencyBenchmarks::",
]


def is_localstack_bench(name: str) -> bool:
    """Return True if the benchmark name belongs to the LocalStack tier."""
    return any(pattern in name for pattern in LOCALSTACK_PATTERNS)


def parse_data_js(content: str) -> dict:
    """Parse the benchmark-action data.js format.

    The file starts with ``window.BENCHMARK_DATA = `` followed by JSON.
    """
    prefix = "window.BENCHMARK_DATA = "
    if not content.startswith(prefix):
        raise ValueError(f"data.js does not start with expected prefix: {content[:50]!r}")
    return json.loads(content[len(prefix) :])


def serialize_data_js(data: dict) -> str:
    """Serialize back to the benchmark-action data.js format."""
    return "window.BENCHMARK_DATA = " + json.dumps(data, indent=2)


def split_entries(data: dict) -> tuple[dict, dict]:
    """Split benchmark data into moto and localstack tiers.

    Returns:
        (moto_data, localstack_data) — two copies of the original data
        structure with bench entries filtered by tier.
    """
    moto_data = copy.deepcopy(data)
    localstack_data = copy.deepcopy(data)

    for suite_name in data.get("entries", {}):
        moto_commits = []
        localstack_commits = []

        for commit_entry in data["entries"][suite_name]:
            benches = commit_entry.get("benches", [])

            moto_benches = [b for b in benches if not is_localstack_bench(b["name"])]
            ls_benches = [b for b in benches if is_localstack_bench(b["name"])]

            if moto_benches:
                moto_entry = copy.deepcopy(commit_entry)
                moto_entry["benches"] = moto_benches
                moto_commits.append(moto_entry)

            if ls_benches:
                ls_entry = copy.deepcopy(commit_entry)
                ls_entry["benches"] = ls_benches
                localstack_commits.append(ls_entry)

        moto_data["entries"][suite_name] = moto_commits
        localstack_data["entries"][suite_name] = localstack_commits

    return moto_data, localstack_data


def count_benches(data: dict) -> tuple[int, int]:
    """Return (commits, total_bench_entries) in the data."""
    commits = 0
    benches = 0
    for suite in data.get("entries", {}).values():
        commits += len(suite)
        for entry in suite:
            benches += len(entry.get("benches", []))
    return commits, benches


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Split benchmark data.js into moto and localstack tiers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "benchmarks_dir",
        type=Path,
        help="Path to the gh-pages benchmarks/ directory (containing data.js and index.html)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print split statistics without writing files",
    )

    args = parser.parse_args()
    benchmarks_dir: Path = args.benchmarks_dir

    data_js_path = benchmarks_dir / "data.js"
    index_html_path = benchmarks_dir / "index.html"

    if not data_js_path.exists():
        print(f"Error: {data_js_path} not found", file=sys.stderr)
        return 1

    if not index_html_path.exists():
        print(f"Error: {index_html_path} not found", file=sys.stderr)
        return 1

    # Parse
    content = data_js_path.read_text()
    data = parse_data_js(content)

    orig_commits, orig_benches = count_benches(data)
    print(f"Original: {orig_commits} commit entries, {orig_benches} bench results")

    # Split
    moto_data, localstack_data = split_entries(data)

    moto_commits, moto_benches = count_benches(moto_data)
    ls_commits, ls_benches = count_benches(localstack_data)

    print(f"Moto:       {moto_commits} commit entries, {moto_benches} bench results")
    print(f"LocalStack: {ls_commits} commit entries, {ls_benches} bench results")
    print(f"Total:      {moto_benches + ls_benches} bench results (expected {orig_benches})")

    if moto_benches + ls_benches != orig_benches:
        print("ERROR: bench count mismatch after split!", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\nDry run — no files written.")
        return 0

    # Write tier directories
    moto_dir = benchmarks_dir / "moto"
    ls_dir = benchmarks_dir / "localstack"

    moto_dir.mkdir(exist_ok=True)
    ls_dir.mkdir(exist_ok=True)

    (moto_dir / "data.js").write_text(serialize_data_js(moto_data))
    (ls_dir / "data.js").write_text(serialize_data_js(localstack_data))

    # Copy index.html to both tiers
    shutil.copy2(index_html_path, moto_dir / "index.html")
    shutil.copy2(index_html_path, ls_dir / "index.html")

    # Remove originals
    data_js_path.unlink()
    index_html_path.unlink()

    print(f"\nWritten: {moto_dir / 'data.js'}")
    print(f"Written: {moto_dir / 'index.html'}")
    print(f"Written: {ls_dir / 'data.js'}")
    print(f"Written: {ls_dir / 'index.html'}")
    print(f"Deleted: {data_js_path}")
    print(f"Deleted: {index_html_path}")
    print("\nDone. Review changes, then commit and push to gh-pages.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
