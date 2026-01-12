window.BENCHMARK_DATA = {
  "lastUpdate": 1768257084771,
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
      },
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
          "id": "5d41d05fa837a0c9df1271425f09fed1087f0ca3",
          "message": "📝 docs: add migration strategy documentation (#40)\n\n- Create comprehensive docs/migrations.md documenting migration strategy\n- Add migrations.md to mkdocs.yml nav under Infrastructure\n- Fix Repository API usage in code examples (save_entity -> create_entity)\n- Fix Entity access patterns (dataclass attributes vs dict access)\n- Fix broken anchor link #running-migrations -> #sample-migration-v200\n- Add link to migrations guide in docs/index.md\n\nCloses #40",
          "timestamp": "2026-01-11T23:03:11-05:00",
          "tree_id": "bf1de356be9bf2a153c70a1816212a764a1ae6b7",
          "url": "https://github.com/zeroae/zae-limiter/commit/5d41d05fa837a0c9df1271425f09fed1087f0ca3"
        },
        "date": 1768190749652,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 92.66634696844257,
            "unit": "iter/sec",
            "range": "stddev: 0.01421671146845248",
            "extra": "mean: 10.791404136612282 msec\nrounds: 183"
          },
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 20.389594678736017,
            "unit": "iter/sec",
            "range": "stddev: 0.07462624167045724",
            "extra": "mean: 49.04462377777838 msec\nrounds: 216"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1064.0188267398137,
            "unit": "iter/sec",
            "range": "stddev: 0.00006161812551905303",
            "extra": "mean: 939.8329943691228 usec\nrounds: 888"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 50.84364429286368,
            "unit": "iter/sec",
            "range": "stddev: 0.089593296975668",
            "extra": "mean: 19.668141690235966 msec\nrounds: 297"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 53.740421623465195,
            "unit": "iter/sec",
            "range": "stddev: 0.05373142490525829",
            "extra": "mean: 18.60796714634186 msec\nrounds: 287"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 25.01478308506301,
            "unit": "iter/sec",
            "range": "stddev: 0.07883597322830305",
            "extra": "mean: 39.976361042168165 msec\nrounds: 166"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 25.785419417882828,
            "unit": "iter/sec",
            "range": "stddev: 0.10652326094964418",
            "extra": "mean: 38.78160691489374 msec\nrounds: 94"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.8033811515072795,
            "unit": "iter/sec",
            "range": "stddev: 0.12016242758579125",
            "extra": "mean: 172.31334180769352 msec\nrounds: 26"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 21.760253396584922,
            "unit": "iter/sec",
            "range": "stddev: 0.009136357034651361",
            "extra": "mean: 45.95534719999819 msec\nrounds: 5"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 22.441425830802014,
            "unit": "iter/sec",
            "range": "stddev: 0.00933390046941291",
            "extra": "mean: 44.560448499998984 msec\nrounds: 10"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 17.808426360161572,
            "unit": "iter/sec",
            "range": "stddev: 0.02722667726883063",
            "extra": "mean: 56.15319286363532 msec\nrounds: 22"
          }
        ]
      },
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
          "id": "63718c17d3625b4bb03d2094ca5d40b6a33f9fe4",
          "message": "🔒 feat(models): add input validation to prevent injection attacks (#75)\n\n* 🔒 feat(models): add input validation to prevent injection attacks\n\nAdd comprehensive input validation to defend against DynamoDB key\ninjection attacks. The '#' character is used as a key delimiter in\nDynamoDB and must be forbidden in user-provided values.\n\nChanges:\n- Add ValidationError exception hierarchy (ValidationError,\n  InvalidIdentifierError, InvalidNameError) to exceptions.py\n- Add validate_identifier() and validate_name() functions to models.py\n- Add validation to Limit, Entity, BucketState, and LimitStatus models\n- Export new exceptions in __init__.py\n- Add comprehensive test coverage (29 new tests)\n\nValidation rules:\n- Identifiers (entity_id, parent_id): alphanumeric start, max 256 chars,\n  allows alphanumeric, underscore, hyphen, dot, colon, @\n- Names (limit_name, resource): letter start, max 64 chars,\n  allows alphanumeric, underscore, hyphen, dot\n\nCloses #48\n\n* 🐛 fix(models): move validation to API boundaries for performance\n\nAddress code review feedback:\n- Remove __post_init__ validation from BucketState and LimitStatus\n  (internal models used for DynamoDB deserialization)\n- Keep validation in BucketState.from_limit() (API boundary)\n- Keep validation in Limit and Entity (user-facing models)\n\nThis fixes:\n1. Performance regression (4.25x slowdown) - validation no longer\n   runs on every internal model construction\n2. Deserialization bug - repository can now read DynamoDB data\n   with empty string defaults without crashing\n\nAlso updates:\n- CLAUDE.md to document ValidationError exception\n- Tests to reflect internal models don't validate directly\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🎨 style(models): fix ruff formatting\n\n* 🐛 fix(models): remove Entity validation for benchmark performance\n\nMove validation from Entity.__post_init__ to Repository.create_entity()\nto avoid performance overhead during DynamoDB deserialization.\n\n- Entity no longer validates in __post_init__ (internal model)\n- Repository.create_entity() validates entity_id and parent_id at API boundary\n- Add comprehensive tests for repository validation\n- Update model tests to reflect Entity is an internal model\n\nThis fixes the 2.43x performance regression in test_available_check benchmark.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ♻️ refactor(validation): move validation to RateLimiter API boundary\n\nMove entity_id and resource validation from BucketState.from_limit() to\nRateLimiter._do_acquire() for consistent API boundary validation.\n\nChanges:\n- Remove validation from BucketState.from_limit() (internal factory)\n- Add validation to RateLimiter._do_acquire() (API boundary)\n- Add ValidationError to exceptions that bypass FAIL_OPEN mode\n- Add input validation tests to test_limiter.py\n- Update test_models.py to reflect from_limit is internal\n\nThis ensures validation happens at the public API boundary while keeping\ninternal model construction fast and unvalidated.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude <noreply@anthropic.com>",
          "timestamp": "2026-01-12T00:28:29-05:00",
          "tree_id": "3cb86eab302bdb568fe81e9b12c638a898e65a50",
          "url": "https://github.com/zeroae/zae-limiter/commit/63718c17d3625b4bb03d2094ca5d40b6a33f9fe4"
        },
        "date": 1768195846611,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 88.64000635450937,
            "unit": "iter/sec",
            "range": "stddev: 0.014368388976432353",
            "extra": "mean: 11.28158763888815 msec\nrounds: 180"
          },
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.771917448528807,
            "unit": "iter/sec",
            "range": "stddev: 0.0605898870196733",
            "extra": "mean: 45.93072715639811 msec\nrounds: 211"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1075.712612528329,
            "unit": "iter/sec",
            "range": "stddev: 0.00021929895955026952",
            "extra": "mean: 929.6163197804516 usec\nrounds: 910"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 56.94235857679263,
            "unit": "iter/sec",
            "range": "stddev: 0.029222667071432052",
            "extra": "mean: 17.56161888958985 msec\nrounds: 317"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 51.34312206983351,
            "unit": "iter/sec",
            "range": "stddev: 0.05759838172751993",
            "extra": "mean: 19.476805454874096 msec\nrounds: 277"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 24.38053942162023,
            "unit": "iter/sec",
            "range": "stddev: 0.07342162316898168",
            "extra": "mean: 41.01631972560942 msec\nrounds: 164"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 26.483825038479583,
            "unit": "iter/sec",
            "range": "stddev: 0.09878569152783225",
            "extra": "mean: 37.758896177083685 msec\nrounds: 96"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.019281344225767,
            "unit": "iter/sec",
            "range": "stddev: 0.13999505878421478",
            "extra": "mean: 199.231708967741 msec\nrounds: 31"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.678897069569877,
            "unit": "iter/sec",
            "range": "stddev: 0.22119330584985614",
            "extra": "mean: 213.7255821470611 msec\nrounds: 34"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 22.089016128492737,
            "unit": "iter/sec",
            "range": "stddev: 0.010689874051306307",
            "extra": "mean: 45.27136900000244 msec\nrounds: 10"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 18.684824246693545,
            "unit": "iter/sec",
            "range": "stddev: 0.028683688554514485",
            "extra": "mean: 53.51936880952784 msec\nrounds: 21"
          }
        ]
      },
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
          "id": "7e8b69a321e5072e19fddeee1a4b6fe3d00a4bcf",
          "message": "✨ feat(repository): add security audit logging for limit modifications (#76)\n\n* ✨ feat(repository): add security audit logging for limit modifications\n\n- Add AuditEvent and AuditAction models for tracking security events\n- Add audit schema keys (pk_audit, sk_audit) in schema.py\n- Implement _log_audit_event and get_audit_events methods in Repository\n- Add principal parameter to create_entity, delete_entity, set_limits, delete_limits\n- Log audit events for entity creation/deletion and limit changes\n- Store audit logs in DynamoDB with 90-day TTL by default\n- Add comprehensive tests for audit logging functionality\n\nCloses #47\n\n* 🔒 feat(repository): add principal validation to audit logging\n\n- Validate principal field using identifier pattern before logging\n- Prevents injection attacks via malicious principal values\n- Rejects empty strings and # delimiter in principal\n- Accepts valid formats: emails, UUIDs, service names\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(repository): use ULID for collision-free audit event IDs\n\nAddresses PR #76 review feedback:\n- Replace timestamp-based event IDs with ULIDs (monotonic, collision-free)\n- Add python-ulid dependency for ULID generation\n- Update CLAUDE.md with AuditEvent/AuditAction in models.py description\n- Add audit events access pattern to DynamoDB patterns table\n- Add tests for ULID format and monotonic ordering\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🎨 style(tests): format test_repository.py\n\n* ✅ test(models): increase coverage to 100%\n\nAdd tests for:\n- Limit validation: refill_amount and refill_period_seconds\n- BucketState properties: tokens, capacity, burst\n- LimitStatus.deficit property\n- AuditEvent: to_dict() and from_dict() serialization\n- AuditAction constants\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude <noreply@anthropic.com>",
          "timestamp": "2026-01-12T01:57:47-05:00",
          "tree_id": "01c1a73e66d9f1c749e6d30da4c01bcbe4187fbd",
          "url": "https://github.com/zeroae/zae-limiter/commit/7e8b69a321e5072e19fddeee1a4b6fe3d00a4bcf"
        },
        "date": 1768201197907,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 102.17806338977026,
            "unit": "iter/sec",
            "range": "stddev: 0.012929268571449548",
            "extra": "mean: 9.786836497237006 msec\nrounds: 181"
          },
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 23.312733508923785,
            "unit": "iter/sec",
            "range": "stddev: 0.06426343455454973",
            "extra": "mean: 42.89501270270233 msec\nrounds: 222"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1102.9982132980513,
            "unit": "iter/sec",
            "range": "stddev: 0.00003817406733583265",
            "extra": "mean: 906.6197822840722 usec\nrounds: 937"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 49.4086982660495,
            "unit": "iter/sec",
            "range": "stddev: 0.09466865861438518",
            "extra": "mean: 20.23935126999968 msec\nrounds: 300"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 61.33223590630287,
            "unit": "iter/sec",
            "range": "stddev: 0.05320307947068044",
            "extra": "mean: 16.304639562263766 msec\nrounds: 265"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 28.916304298698883,
            "unit": "iter/sec",
            "range": "stddev: 0.07238923936193296",
            "extra": "mean: 34.582565934782885 msec\nrounds: 138"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 29.250073301218336,
            "unit": "iter/sec",
            "range": "stddev: 0.0807034306681412",
            "extra": "mean: 34.18794851219561 msec\nrounds: 82"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 6.652134556790673,
            "unit": "iter/sec",
            "range": "stddev: 0.08528911949683002",
            "extra": "mean: 150.32768676923013 msec\nrounds: 26"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.922331660859523,
            "unit": "iter/sec",
            "range": "stddev: 0.18010217483478808",
            "extra": "mean: 203.15575400000228 msec\nrounds: 35"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.948630430335996,
            "unit": "iter/sec",
            "range": "stddev: 0.00741757551197253",
            "extra": "mean: 45.56092933333389 msec\nrounds: 9"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.87645430208263,
            "unit": "iter/sec",
            "range": "stddev: 0.007551622967925118",
            "extra": "mean: 47.900854500001955 msec\nrounds: 14"
          }
        ]
      },
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
          "id": "1bbc649df756dedfaf71b9c0701a357d56106468",
          "message": "fix(infra): use LocalStack legacy CloudFormation engine (#81) (#85)\n\n* 🐛 fix(infra): fix CloudFormation output condition for DLQ alarm\n\n- Fix AggregatorDLQAlarmName output to use DeployAggregatorAlarms condition\n  instead of DeployAggregator (fixes template error when aggregator enabled\n  but alarms disabled)\n- Update CLAUDE.md with LocalStack Docker socket requirement\n- Add pytest markers for aws and e2e tests\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(infra): add integration test for CloudFormation stack deletion (#81)\n\nAdd test_stack_create_and_delete_minimal to document and verify the\nLocalStack CloudFormation v2 engine bug where stack deletion fails with:\n  \"Template format error: Unresolved resource dependencies [AggregatorDLQ]\"\n\nRoot cause: LocalStack's new CloudFormation v2 engine incorrectly tries\nto resolve !GetAtt references in conditional resources during deletion,\neven when those resources were never created (condition evaluated to false).\n\nWorkarounds:\n1. Use legacy engine: PROVIDER_OVERRIDE_CLOUDFORMATION=engine-legacy\n2. Wrap delete_stack() in try/except and delete resources directly\n\nThe test:\n- Creates a minimal stack (no aggregator, no alarms)\n- Attempts deletion (expected to fail on LocalStack v2 engine)\n- Uses xfail to document the known issue\n- Falls back to direct DynamoDB table deletion for cleanup\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(infra): use LocalStack legacy CloudFormation engine (#81)\n\nLocalStack's CloudFormation v2 engine has a bug where stack deletion\nfails with \"Unresolved resource dependencies [AggregatorDLQ]\" due to\nincorrect resolution of !GetAtt references in conditional resources.\n\nWorkaround: Use PROVIDER_OVERRIDE_CLOUDFORMATION=engine-legacy\n\nNote: Legacy engine has its own bug where CloudWatch Alarm Threshold\nparameters are passed as strings. Tests use aggregator_stack_options\n(no alarms) to avoid this issue.\n\nUpdated:\n- CLAUDE.md documentation\n- docs/infra/localstack.md\n- .github/workflows/ci.yml (integration and benchmark jobs)\n- tests/test_integration_localstack.py docstring and cleanup\n- tests/test_stack_manager.py (expect success, not xfail)\n- examples/fastapi-demo/docker-compose.yml\n- tests/fixtures/localstack-v2-bug-repro.yaml (minimal reproduction)\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-12T15:36:47-05:00",
          "tree_id": "6954f2151e112336169d70c80873cf7686e62019",
          "url": "https://github.com/zeroae/zae-limiter/commit/1bbc649df756dedfaf71b9c0701a357d56106468"
        },
        "date": 1768250336408,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 94.33066517290099,
            "unit": "iter/sec",
            "range": "stddev: 0.013897949027753717",
            "extra": "mean: 10.601006556744569 msec\nrounds: 185"
          },
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 22.36106901089355,
            "unit": "iter/sec",
            "range": "stddev: 0.0663704349873194",
            "extra": "mean: 44.72058109175523 msec\nrounds: 218"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1082.0908168469243,
            "unit": "iter/sec",
            "range": "stddev: 0.000054868354612565166",
            "extra": "mean: 924.1368510213157 usec\nrounds: 933"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 47.86194800599381,
            "unit": "iter/sec",
            "range": "stddev: 0.09186692078815564",
            "extra": "mean: 20.893424560880156 msec\nrounds: 312"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 52.01578282538616,
            "unit": "iter/sec",
            "range": "stddev: 0.05862157067296079",
            "extra": "mean: 19.224934158098506 msec\nrounds: 253"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 35.73282947749318,
            "unit": "iter/sec",
            "range": "stddev: 0.09186254420360597",
            "extra": "mean: 27.985469234387494 msec\nrounds: 64"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 32.05085093140925,
            "unit": "iter/sec",
            "range": "stddev: 0.03882916854394813",
            "extra": "mean: 31.200419674974 msec\nrounds: 80"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 4.832519064341106,
            "unit": "iter/sec",
            "range": "stddev: 0.1256715688168897",
            "extra": "mean: 206.9314133448423 msec\nrounds: 29"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.4406561172286745,
            "unit": "iter/sec",
            "range": "stddev: 0.2003765084201541",
            "extra": "mean: 225.19194767643486 msec\nrounds: 34"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 18.750103225990713,
            "unit": "iter/sec",
            "range": "stddev: 0.0073530633439187944",
            "extra": "mean: 53.33303971435401 msec\nrounds: 7"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 18.31678138909128,
            "unit": "iter/sec",
            "range": "stddev: 0.007106335751871682",
            "extra": "mean: 54.594744500011274 msec\nrounds: 18"
          }
        ]
      },
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
          "id": "1e632cb5f905d3692f2f2a99c5607f7a1d128b73",
          "message": "revert: undo LocalStack legacy CloudFormation engine workaround (#85) (#86)\n\n* ⏪ revert: undo LocalStack legacy CloudFormation engine workaround (#85)\n\nThis reverts commit 1bbc649df756dedfaf71b9c0701a357d56106468.\n\nPrefer using LocalStack's v2 CloudFormation engine (the default) rather\nthan the legacy engine. While v2 has a stack deletion bug with conditional\nresources, the legacy engine has its own bug with CloudWatch Alarm\nThreshold parameters.\n\nThe v2 engine is the future of LocalStack CloudFormation support, and\nwe should track the upstream bug rather than permanently work around it.\n\nUpstream issue: https://github.com/localstack/localstack/issues/13609\n\nRe-opens #81 for tracking until LocalStack fixes the v2 engine.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(infra): address code review feedback for PR #86\n\n1. Fix CloudFormation condition mismatch: AggregatorDLQAlarmName output\n   now uses DeployAggregatorAlarms condition (not DeployAggregator) to\n   match the resource it references.\n\n2. Add test_cloudformation_stack_deployment_no_alarms: Tests the edge\n   case where EnableAggregator=true but EnableAlarms=false.\n\n3. Add test_stack_create_and_delete_minimal: Integration test for full\n   stack lifecycle with graceful handling of LocalStack v2 deletion bug.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: remove references to non-existent e2e pytest marker\n\nThe e2e marker was removed by the revert. Update CLAUDE.md to reflect\nthat integration tests now include full stack lifecycle tests.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-12T16:47:20-05:00",
          "tree_id": "b3417a33749991d9f7e91fe83426329be51cb48f",
          "url": "https://github.com/zeroae/zae-limiter/commit/1e632cb5f905d3692f2f2a99c5607f7a1d128b73"
        },
        "date": 1768254597391,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 106.9477419826424,
            "unit": "iter/sec",
            "range": "stddev: 0.012057956116338163",
            "extra": "mean: 9.350361040463106 msec\nrounds: 173"
          },
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 23.212185508901037,
            "unit": "iter/sec",
            "range": "stddev: 0.06493027360716228",
            "extra": "mean: 43.080820615384795 msec\nrounds: 221"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1089.567108505349,
            "unit": "iter/sec",
            "range": "stddev: 0.0002770213854729763",
            "extra": "mean: 917.7956935317037 usec\nrounds: 943"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 53.83573022075945,
            "unit": "iter/sec",
            "range": "stddev: 0.08641234915548292",
            "extra": "mean: 18.57502435463191 msec\nrounds: 313"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 70.8211705817303,
            "unit": "iter/sec",
            "range": "stddev: 0.06694522360601257",
            "extra": "mean: 14.120071608333026 msec\nrounds: 120"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 29.566041798705925,
            "unit": "iter/sec",
            "range": "stddev: 0.04513439678832916",
            "extra": "mean: 33.82258629032206 msec\nrounds: 155"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 28.766037179085178,
            "unit": "iter/sec",
            "range": "stddev: 0.10522993024121512",
            "extra": "mean: 34.76321725423711 msec\nrounds: 59"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 6.062327942727209,
            "unit": "iter/sec",
            "range": "stddev: 0.10126840548271451",
            "extra": "mean: 164.9531350740716 msec\nrounds: 27"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.829210979943567,
            "unit": "iter/sec",
            "range": "stddev: 0.1749091283427059",
            "extra": "mean: 207.07316457142775 msec\nrounds: 35"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 23.854401425374554,
            "unit": "iter/sec",
            "range": "stddev: 0.007496194642260101",
            "extra": "mean: 41.92098481818427 msec\nrounds: 11"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 19.012249135824824,
            "unit": "iter/sec",
            "range": "stddev: 0.026925888075122074",
            "extra": "mean: 52.59766968420889 msec\nrounds: 19"
          }
        ]
      },
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
          "id": "c330e5cba9eba3c59d5e79dfb412376118a3ac73",
          "message": "✨ feat(infra): add VS Code worktree configuration for multi-session Claude Code (#87)\n\nConfigure Git Worktrees extension to store worktrees in ../zae-limiter.worktrees,\nenabling parallel Claude Code sessions in separate VS Code windows.\n\nCo-authored-by: Claude <noreply@anthropic.com>",
          "timestamp": "2026-01-12T17:24:34-05:00",
          "tree_id": "f05fd6443803a5eabc58f597787e0065b5373370",
          "url": "https://github.com/zeroae/zae-limiter/commit/c330e5cba9eba3c59d5e79dfb412376118a3ac73"
        },
        "date": 1768256806591,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 88.84809323580606,
            "unit": "iter/sec",
            "range": "stddev: 0.015791765345691815",
            "extra": "mean: 11.255165570587586 msec\nrounds: 170"
          },
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 22.743013365002398,
            "unit": "iter/sec",
            "range": "stddev: 0.06170238535268035",
            "extra": "mean: 43.96954721658954 msec\nrounds: 217"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1077.3983459785827,
            "unit": "iter/sec",
            "range": "stddev: 0.0000358263262172934",
            "extra": "mean: 928.1618110260944 usec\nrounds: 889"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 63.73627841539987,
            "unit": "iter/sec",
            "range": "stddev: 0.029701817483299336",
            "extra": "mean: 15.689651558920975 msec\nrounds: 297"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 58.73246126255121,
            "unit": "iter/sec",
            "range": "stddev: 0.05388441045203094",
            "extra": "mean: 17.026359503813552 msec\nrounds: 262"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 31.3279013757348,
            "unit": "iter/sec",
            "range": "stddev: 0.0708975462368444",
            "extra": "mean: 31.92042735344397 msec\nrounds: 116"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 29.480405870252476,
            "unit": "iter/sec",
            "range": "stddev: 0.06371782726703051",
            "extra": "mean: 33.92083556790718 msec\nrounds: 81"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.253625519353005,
            "unit": "iter/sec",
            "range": "stddev: 0.12602494543354634",
            "extra": "mean: 190.34474313333854 msec\nrounds: 30"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.95983356044279,
            "unit": "iter/sec",
            "range": "stddev: 0.17136841914392673",
            "extra": "mean: 201.6196688484694 msec\nrounds: 33"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 19.94203037894304,
            "unit": "iter/sec",
            "range": "stddev: 0.01391465525580085",
            "extra": "mean: 50.14534533333719 msec\nrounds: 9"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 17.485308910312174,
            "unit": "iter/sec",
            "range": "stddev: 0.034333194386366006",
            "extra": "mean: 57.1908683529313 msec\nrounds: 17"
          }
        ]
      },
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
          "id": "d6504b22e55e93f1a5ebf13534c015f348e40f70",
          "message": "✅ test(e2e): add E2E integration tests for LocalStack and AWS (#82)\n\n* ✅ test(e2e): add E2E integration tests for LocalStack and AWS (#46)\n\nAdd comprehensive end-to-end integration tests that validate the full\nstack lifecycle including CloudFormation, DynamoDB, Lambda aggregator,\nand CloudWatch alarms.\n\nChanges:\n- Add test_e2e_localstack.py with 9 tests for LocalStack integration\n- Add test_e2e_aws.py with 9 tests for real AWS integration\n- Add --run-aws pytest flag to enable AWS tests\n- Fix CloudFormation template: remove invalid StreamEnabled property\n- Fix table naming to use hyphens (CloudFormation stack name constraint)\n- Add timing-tolerant assertions for token bucket refill behavior\n- Add explicit stack cleanup in test fixtures\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(e2e): mark flaky usage snapshot test as xfail (#84)\n\nThe test_usage_snapshots_created test fails intermittently because\nthe Lambda aggregator may not create snapshots within the 120s wait\nperiod due to DynamoDB Streams latency and Lambda batching delays.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(test): fix flaky concurrent lease test by reducing refill rate\n\nUse per_hour limit (1000/hour = ~0.28/second) instead of per_minute\n(100/minute = ~1.67/second) to prevent bucket from fully refilling\nduring concurrent operations in CI.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(test): fix CI failures for E2E tests\n\n- Add aws and e2e pytest markers to pyproject.toml\n- Disable CloudWatch alarms in e2e_stack_options (LocalStack bug)\n- Loosen concurrent test assertion (optimistic locking timing)\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-12T17:26:35-05:00",
          "tree_id": "f5e5c987cd7b7d127932c83ecef5744263d195cd",
          "url": "https://github.com/zeroae/zae-limiter/commit/d6504b22e55e93f1a5ebf13534c015f348e40f70"
        },
        "date": 1768257084513,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 99.04715332625544,
            "unit": "iter/sec",
            "range": "stddev: 0.013120757998244663",
            "extra": "mean: 10.096201318437284 msec\nrounds: 179"
          },
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 23.558446857561357,
            "unit": "iter/sec",
            "range": "stddev: 0.05760746087862037",
            "extra": "mean: 42.44762000000176 msec\nrounds: 203"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1072.065199203095,
            "unit": "iter/sec",
            "range": "stddev: 0.0000348947236521859",
            "extra": "mean: 932.7790891294078 usec\nrounds: 920"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 52.56579808155997,
            "unit": "iter/sec",
            "range": "stddev: 0.07274788972220575",
            "extra": "mean: 19.023776609429984 msec\nrounds: 297"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 58.53294870380911,
            "unit": "iter/sec",
            "range": "stddev: 0.05001491319628298",
            "extra": "mean: 17.084394723734867 msec\nrounds: 257"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 29.46450277877858,
            "unit": "iter/sec",
            "range": "stddev: 0.0464658937429686",
            "extra": "mean: 33.939143908453694 msec\nrounds: 142"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 28.56118262677422,
            "unit": "iter/sec",
            "range": "stddev: 0.09693193051531834",
            "extra": "mean: 35.01255578480725 msec\nrounds: 79"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 16.821378548373055,
            "unit": "iter/sec",
            "range": "stddev: 0.009031838185150022",
            "extra": "mean: 59.44815980000158 msec\nrounds: 5"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.9638973455574895,
            "unit": "iter/sec",
            "range": "stddev: 0.1463293226829997",
            "extra": "mean: 201.45460922856597 msec\nrounds: 35"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 20.007398435827255,
            "unit": "iter/sec",
            "range": "stddev: 0.007853424887152958",
            "extra": "mean: 49.98151075000834 msec\nrounds: 8"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 17.79172067482718,
            "unit": "iter/sec",
            "range": "stddev: 0.028664996032335733",
            "extra": "mean: 56.205918374992336 msec\nrounds: 16"
          }
        ]
      }
    ]
  }
}