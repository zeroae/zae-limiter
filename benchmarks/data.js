window.BENCHMARK_DATA = {
  "lastUpdate": 1768187357849,
  "repoUrl": "https://github.com/zeroae/zae-limiter",
  "entries": {
    "Benchmark": [
      {
        "commit": {
          "author": {
            "email": "psodre@gmail.com",
            "name": "Patrick Sodré",
            "username": "sodre"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "88731ba4ebd0390d6d2adbbb47ebd42de9599bd4",
          "message": "✅ test(benchmark): add performance benchmarks (#45) (#73)\n\n* ✅ test(benchmark): add performance benchmarks (#45)\n\nAdd pytest-benchmark tests to measure:\n- Acquire/release latency (single and multiple limits)\n- DynamoDB transaction overhead\n- Cascade overhead (hierarchical limits)\n- Concurrent throughput\n\nAlso adds github-action-benchmark CI workflow for tracking\nperformance over time with dashboard at /benchmarks/.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat(ci): add LocalStack to benchmark workflow\n\nRun integration benchmarks with realistic DynamoDB latency\nby adding LocalStack service container.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ♻️ refactor(ci): move benchmarks into integration job\n\n- Delete separate benchmark.yml workflow\n- Add benchmark steps to integration job (Python 3.12 only)\n- Add permissions and concurrency group for gh-pages access\n- Use --all-extras to include benchmark dependencies\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(tests): unset AWS_ENDPOINT_URL in moto fixtures\n\nWhen AWS_ENDPOINT_URL is set (e.g., in CI integration job), boto3\nroutes requests to that endpoint instead of being intercepted by\nmoto's mock_aws decorator. This caused benchmark tests using\nsync_limiter (moto-based) to fail.\n\nFix by unsetting AWS_ENDPOINT_URL in the aws_credentials fixture.\nLocalStack tests use localstack_endpoint fixture which reads from\nthe environment before aws_credentials runs.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ♻️ refactor(ci): move benchmarks to separate job\n\n- Extract benchmark steps from integration job to dedicated benchmark job\n- Run benchmarks only on Python 3.12\n- Fix test_cascade_localstack entity ID collision by using unique IDs\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ♻️ refactor(ci): use --extra dev for integration job\n\nApply review comment: integration tests only need dev dependencies,\nnot all extras.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-11T22:06:20-05:00",
          "tree_id": "98975233218bec47f8c1b740d305ff2c2e072b0d",
          "url": "https://github.com/zeroae/zae-limiter/commit/88731ba4ebd0390d6d2adbbb47ebd42de9599bd4"
        },
        "date": 1768187357224,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 92.93394533191497,
            "unit": "iter/sec",
            "range": "stddev: 0.014238632517272977",
            "extra": "mean: 10.760330861113074 msec\nrounds: 180"
          },
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.758721542521425,
            "unit": "iter/sec",
            "range": "stddev: 0.06346719925621735",
            "extra": "mean: 45.95858254106408 msec\nrounds: 207"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1064.7532618834994,
            "unit": "iter/sec",
            "range": "stddev: 0.00021397229091727905",
            "extra": "mean: 939.1847255119427 usec\nrounds: 929"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 47.49021228926307,
            "unit": "iter/sec",
            "range": "stddev: 0.08113651679292141",
            "extra": "mean: 21.056970516556046 msec\nrounds: 302"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 48.41907343314512,
            "unit": "iter/sec",
            "range": "stddev: 0.05954896509036178",
            "extra": "mean: 20.65301810000051 msec\nrounds: 280"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 23.41193492141142,
            "unit": "iter/sec",
            "range": "stddev: 0.08463764627858043",
            "extra": "mean: 42.71325729192287 msec\nrounds: 161"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 26.257421636482984,
            "unit": "iter/sec",
            "range": "stddev: 0.11026747364631549",
            "extra": "mean: 38.08447051063707 msec\nrounds: 94"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 4.416178257058002,
            "unit": "iter/sec",
            "range": "stddev: 0.1571429085491885",
            "extra": "mean: 226.44013483871154 msec\nrounds: 31"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.294807642669262,
            "unit": "iter/sec",
            "range": "stddev: 0.22129424478229487",
            "extra": "mean: 232.83929880000187 msec\nrounds: 35"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 5.342119329426973,
            "unit": "iter/sec",
            "range": "stddev: 0.4064908372577444",
            "extra": "mean: 187.19162533332363 msec\nrounds: 9"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 18.716855731259784,
            "unit": "iter/sec",
            "range": "stddev: 0.006780573326925634",
            "extra": "mean: 53.427777312503366 msec\nrounds: 16"
          }
        ]
      }
    ]
  }
}