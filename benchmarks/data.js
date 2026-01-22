window.BENCHMARK_DATA = {
  "lastUpdate": 1769094515512,
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
          "id": "acf485f1c56bcb725e70d3966fabcafac0a8847a",
          "message": "📝 docs: document stack lifecycle and cleanup best practices (#79) (#88)\n\n- Enhanced delete_stack() docstrings with examples and warnings\n- Added Stack Lifecycle section to README with cleanup examples\n- Added Stack Lifecycle Management section to deployment docs\n- Updated LocalStack docs with proper pytest fixtures including cleanup\n- Added use-case guidance for dev/test/production scenarios\n\nCo-authored-by: Claude <noreply@anthropic.com>",
          "timestamp": "2026-01-12T17:41:28-05:00",
          "tree_id": "d7dcd91fd6a96f962d4d8ffa2a4e5b86bea6dfd9",
          "url": "https://github.com/zeroae/zae-limiter/commit/acf485f1c56bcb725e70d3966fabcafac0a8847a"
        },
        "date": 1768257849590,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 92.68959513346157,
            "unit": "iter/sec",
            "range": "stddev: 0.013634189422221417",
            "extra": "mean: 10.788697464479412 msec\nrounds: 183"
          },
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.204309145371667,
            "unit": "iter/sec",
            "range": "stddev: 0.0654737491651672",
            "extra": "mean: 47.16022545909133 msec\nrounds: 220"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1062.4381566441052,
            "unit": "iter/sec",
            "range": "stddev: 0.0003082791123123492",
            "extra": "mean: 941.2312554348321 usec\nrounds: 920"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 48.89090598268989,
            "unit": "iter/sec",
            "range": "stddev: 0.08757468797971125",
            "extra": "mean: 20.45370156065539 msec\nrounds: 305"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 57.501034646197915,
            "unit": "iter/sec",
            "range": "stddev: 0.05403937526892342",
            "extra": "mean: 17.39099141698874 msec\nrounds: 259"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 32.642168613104026,
            "unit": "iter/sec",
            "range": "stddev: 0.08109914606166381",
            "extra": "mean: 30.63521948718062 msec\nrounds: 78"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 37.952539900586736,
            "unit": "iter/sec",
            "range": "stddev: 0.040182051154319616",
            "extra": "mean: 26.348697679243866 msec\nrounds: 53"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 17.079611886590847,
            "unit": "iter/sec",
            "range": "stddev: 0.009355601915834373",
            "extra": "mean: 58.549339799992595 msec\nrounds: 5"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 6.155785799171137,
            "unit": "iter/sec",
            "range": "stddev: 0.10414416467517386",
            "extra": "mean: 162.44879737931228 msec\nrounds: 29"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 20.005732602663947,
            "unit": "iter/sec",
            "range": "stddev: 0.004715097428076891",
            "extra": "mean: 49.985672600004705 msec\nrounds: 10"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 15.769030762527137,
            "unit": "iter/sec",
            "range": "stddev: 0.03479643273802465",
            "extra": "mean: 63.41543846666582 msec\nrounds: 15"
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
          "id": "5c5aeda4bc140ad5db7fd20b105b312dbda7932f",
          "message": "feat(infra): add IAM permission boundary and role name format support (#90)\n\n* 📝 docs(infra): add implementation plan for IAM permission boundaries (#83)\n\nPlan for adding:\n- Permission boundary support for Lambda execution role\n- Customizable role naming for enterprise compliance\n\n* 📝 docs(infra): update plan with final design for #83\n\n- permission_boundary: accepts policy name or full ARN\n- role_name_format: uses {} placeholder for internal role name\n- Handle substitution in Python, keep CFN template simple\n\n* ✨ feat(infra): add IAM permission boundary and role name format support (#83)\n\n- Add PermissionBoundary parameter to CloudFormation template\n- Add RoleName parameter with conditional logic for custom naming\n- Add permission_boundary and role_name_format fields to StackOptions\n- Add get_role_name() method for {} placeholder substitution\n- Add --permission-boundary and --role-name-format CLI options\n- Update AWS E2E tests to use permission boundary\n- Add comprehensive unit tests for new StackOptions features\n- Update CLAUDE.md documentation with new options\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude <noreply@anthropic.com>",
          "timestamp": "2026-01-12T20:28:19-05:00",
          "tree_id": "5e666c6e18e34eb63155577564be9384f088ecf3",
          "url": "https://github.com/zeroae/zae-limiter/commit/5c5aeda4bc140ad5db7fd20b105b312dbda7932f"
        },
        "date": 1768267854860,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 102.86233237646833,
            "unit": "iter/sec",
            "range": "stddev: 0.012016918482400083",
            "extra": "mean: 9.721731725274086 msec\nrounds: 182"
          },
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 24.086175293159368,
            "unit": "iter/sec",
            "range": "stddev: 0.06370496416459502",
            "extra": "mean: 41.517592055555895 msec\nrounds: 216"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1099.1418360291568,
            "unit": "iter/sec",
            "range": "stddev: 0.000019351736625499745",
            "extra": "mean: 909.8006892474186 usec\nrounds: 930"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 159.35361140721088,
            "unit": "iter/sec",
            "range": "stddev: 0.0023018509735592057",
            "extra": "mean: 6.27535197457564 msec\nrounds: 118"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 50.53219799699656,
            "unit": "iter/sec",
            "range": "stddev: 0.09972374753882803",
            "extra": "mean: 19.789362814960793 msec\nrounds: 254"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 36.240277995422716,
            "unit": "iter/sec",
            "range": "stddev: 0.0640571566842085",
            "extra": "mean: 27.593607315217167 msec\nrounds: 92"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 32.46714711941593,
            "unit": "iter/sec",
            "range": "stddev: 0.04458357139753476",
            "extra": "mean: 30.800365560976015 msec\nrounds: 82"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 16.542068443594378,
            "unit": "iter/sec",
            "range": "stddev: 0.010105197369256443",
            "extra": "mean: 60.45193220000442 msec\nrounds: 5"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.248421266223238,
            "unit": "iter/sec",
            "range": "stddev: 0.1469435694461826",
            "extra": "mean: 190.533486028571 msec\nrounds: 35"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 20.5384404091461,
            "unit": "iter/sec",
            "range": "stddev: 0.011287071498665318",
            "extra": "mean: 48.68918866666642 msec\nrounds: 9"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 17.16259108237426,
            "unit": "iter/sec",
            "range": "stddev: 0.03321751095772111",
            "extra": "mean: 58.26626033332379 msec\nrounds: 12"
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
          "id": "b09c8d68dbd9882976168230dc4b30ebffb394ad",
          "message": "feat(cli): add --endpoint-url option to all AWS-interacting commands (#89)\n\nAdd the --endpoint-url parameter to all AWS-interacting CLI commands\nfor LocalStack consistency:\n\n- status: Pass endpoint_url to StackManager\n- delete: Pass endpoint_url to StackManager\n- version: Pass endpoint_url to Repository\n- upgrade: Pass endpoint_url to both Repository and StackManager\n- check: Pass endpoint_url to Repository\n\nAlso adds tests for each command and updates CLAUDE.md documentation.\n\nCloses #78",
          "timestamp": "2026-01-12T20:35:39-05:00",
          "tree_id": "70c3bf1e585bb0a9a5a1f4dc9ecebf52eb11135f",
          "url": "https://github.com/zeroae/zae-limiter/commit/b09c8d68dbd9882976168230dc4b30ebffb394ad"
        },
        "date": 1768268286577,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 103.47075707786198,
            "unit": "iter/sec",
            "range": "stddev: 0.01159779925891029",
            "extra": "mean: 9.664566378377783 msec\nrounds: 185"
          },
          {
            "name": "tests/test_performance.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 23.357896272471805,
            "unit": "iter/sec",
            "range": "stddev: 0.062441863635488835",
            "extra": "mean: 42.81207469777744 msec\nrounds: 225"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1086.3696177106056,
            "unit": "iter/sec",
            "range": "stddev: 0.0002431758142278774",
            "extra": "mean: 920.4970239386673 usec\nrounds: 919"
          },
          {
            "name": "tests/test_performance.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 51.118216626501145,
            "unit": "iter/sec",
            "range": "stddev: 0.08528161089914642",
            "extra": "mean: 19.562497794212394 msec\nrounds: 311"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 60.10652145267505,
            "unit": "iter/sec",
            "range": "stddev: 0.054606227560115815",
            "extra": "mean: 16.637129812733406 msec\nrounds: 267"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 26.436647986424433,
            "unit": "iter/sec",
            "range": "stddev: 0.06729712832598551",
            "extra": "mean: 37.82627814666644 msec\nrounds: 150"
          },
          {
            "name": "tests/test_performance.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 29.97327238896217,
            "unit": "iter/sec",
            "range": "stddev: 0.09080379038828626",
            "extra": "mean: 33.36305716049395 msec\nrounds: 81"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.433450025547147,
            "unit": "iter/sec",
            "range": "stddev: 0.11524072234577602",
            "extra": "mean: 184.04512700000404 msec\nrounds: 31"
          },
          {
            "name": "tests/test_performance.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.760333621241159,
            "unit": "iter/sec",
            "range": "stddev: 0.18281387924377462",
            "extra": "mean: 210.0693101714309 msec\nrounds: 35"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.366310609924543,
            "unit": "iter/sec",
            "range": "stddev: 0.007529637099960871",
            "extra": "mean: 46.80265199999035 msec\nrounds: 9"
          },
          {
            "name": "tests/test_performance.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 18.45917759520124,
            "unit": "iter/sec",
            "range": "stddev: 0.030176964898677158",
            "extra": "mean: 54.173594400000034 msec\nrounds: 15"
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
          "id": "812766ad534fc066643e07b25dc2112c7136c769",
          "message": "⚡ perf(test): optimize test execution with parallel execution and shared fixtures (#80) (#92)\n\n* ⚡ perf(test): optimize E2E test execution with shared stack fixtures\n\nReduce CloudFormation stack creations from 9 to 4 by sharing stacks\nacross tests within each test class:\n\n- Add shared_stack fixture (class-scoped) for TestE2ELocalStackFullWorkflow\n- Add shared_stack_minimal fixture (class-scoped) for TestE2ELocalStackErrorHandling\n- Each test gets a fresh RateLimiter instance to avoid event loop issues\n- Add unique_table_name_class fixture for class-level table name sharing\n- Update localstack_endpoint and e2e_stack_options to session scope\n\nResults:\n- Stack creations: 9 → 4 (55% reduction)\n- Test time: ~5min → ~3:15 (35% faster)\n\nCloses #80\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ⚡ perf(test): optimize integration test execution with shared stack fixtures\n\nApply same shared stack pattern to integration tests:\n\n- Add shared_stack fixture (class-scoped) for TestLocalStackIntegration\n- Add shared_stack_sync fixture (class-scoped) for TestSyncLocalStackIntegration\n- Update StackOptions fixtures to session scope for compatibility\n- Fix entity ID collision in inline CloudFormation tests\n\nResults for integration tests:\n- Stack creations: 8 → 4 (50% reduction)\n- Test time: ~2 minutes (was creating redundant stacks)\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ⚡ perf(test): add pytest-xdist for parallel test execution\n\n- Add pytest-xdist>=3.5.0 to dev dependencies\n- Update integration tests to use unique table names for parallelization\n- Use --dist loadscope to group tests by class for shared fixtures\n\nPerformance improvements with -n auto --dist loadscope:\n- Unit tests: 56s → 10s (5.6x faster)\n- E2E tests: 3:15 → 1:15 (2.6x faster)\n- Integration tests: 2:03 → 1:36 (1.3x faster)\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ⚙️ chore(test): make parallel test execution the default\n\nAdd -n auto --dist loadscope to pytest addopts configuration.\nTests now run in parallel by default, significantly reducing CI time.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🎨 style: fix line too long in integration test\n\n* 🐛 fix(ci): disable xdist for benchmark tests\n\n* 🔥 chore: remove benchmark.json from repository\n\n* 🙈 chore: add benchmark.json to gitignore\n\n* ♻️ refactor(test): simplify class-scoped fixtures with loop_scope\n\n- Use pytest_asyncio.fixture with loop_scope=\"class\" to share event loop\n- Merge two-fixture pattern into single fixture that yields limiter directly\n- Add `test` as valid commit scope in CLAUDE.md\n\nThis removes the \"hack\" where we yielded the table name from a class-scoped\nfixture and created a new limiter per test. With loop_scope=\"class\", we can\nnow share the limiter directly across all tests in a class.\n\nFixes review comments from PR #92.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ⚡ perf(test): make LocalStack fixtures parallelizable with unique table names\n\n- Change hardcoded table names to use unique_table_name fixture\n- Add stack cleanup in fixture teardown\n- Enables parallel test execution with pytest-xdist\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat(test): add slow/monitoring/snapshots markers for AWS E2E tests\n\nAllow skipping long-running tests (>30s sleeps) with -m \"not slow\".\nMore granular control with -m \"not monitoring\" or -m \"not snapshots\".\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(test): add loop_scope to async tests using class-scoped fixtures\n\nTests using class-scoped async fixtures must have matching loop_scope\nto prevent \"Future attached to different loop\" errors.\n\n- Moved standalone CFN deployment tests to separate class\n- Added loop_scope=\"class\" to all tests in classes with class-scoped fixtures\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ♻️ refactor(test): reorganize tests into unit/integration/e2e/benchmark directories\n\n- Split flat tests/ directory into subdirectories by test type:\n  - tests/unit/ - moto-mocked tests (fast)\n  - tests/integration/ - LocalStack repository tests\n  - tests/e2e/ - full workflow tests (LocalStack + AWS)\n  - tests/benchmark/ - performance benchmarks (pytest-benchmark)\n- Add importlib mode to pytest config for same-named files in different dirs\n- Add __init__.py to all test subdirectories for proper package imports\n- Split conftest.py into per-directory fixtures\n- Delete redundant test_integration_localstack.py (duplicated E2E tests)\n- Move CloudFormation stack tests to e2e/test_localstack.py\n- Document test architecture in CLAUDE.md\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat(ci): add E2E job and fix benchmark path for new test structure\n\n- Add e2e job to run LocalStack E2E tests (tests/e2e/test_localstack.py)\n- Fix benchmark path from tests/test_performance.py to tests/benchmark/\n- E2E job runs on Python 3.11 and 3.12 with LocalStack service\n- Upload E2E coverage and test results to Codecov with 'e2e' flag\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ♻️ refactor(test): move root-level tests to subdirectories\n\nMove all test files from tests/ root to appropriate subdirectories:\n- Pure unit tests → tests/unit/\n- Integration tests with @integration marker → tests/integration/\n- Split test_stack_manager.py and test_lambda_builder.py (unit vs integration)\n- Remove localstack_endpoint from root conftest (stays in integration/conftest)\n\nNo test files remain at root level - all tests are now organized by:\n- unit/: Fast moto-based tests\n- integration/: LocalStack-based tests\n- e2e/: Full workflow tests\n- benchmark/: Performance benchmarks\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(test): use unique table names for parallel test isolation\n\nUpdate integration tests to use unique_table_name fixture instead of\nhardcoded table names. This prevents race conditions when tests run\nin parallel with pytest-xdist.\n\nAffected tests:\n- test_repository.py: localstack_repo, test_create_table_or_stack,\n  test_create_stack_with_custom_parameters\n- test_stack_manager.py: test_stack_create_and_delete_minimal\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🎨 style: fix formatting in tests/unit/conftest.py\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ♻️ refactor(ci): rename test job to unit for consistency\n\nRename the \"test\" job to \"unit\" to match the tests/unit/ directory structure.\nAlso explicitly targets tests/unit/ and adds the \"unit\" flag for coverage.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(test): use unique table names for parallel test isolation\n\nChange test_hierarchical_rate_limiting_workflow to use per_hour limits\ninstead of per_minute to prevent timing-related flakiness. The per_minute\nlimits refill ~1.67 tokens/second which can cause the test to see 100\ntokens available instead of 99 if there's any delay between consumption\nand verification.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🎨 style: shorten assertion messages to fix line length\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-13T01:58:35-05:00",
          "tree_id": "51a11d68c509ae65d97be5363af1ea587a345092",
          "url": "https://github.com/zeroae/zae-limiter/commit/812766ad534fc066643e07b25dc2112c7136c769"
        },
        "date": 1768287706255,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 24.225801769716508,
            "unit": "iter/sec",
            "range": "stddev: 0.011832575548474228",
            "extra": "mean: 41.27830358333284 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 22.280161665367885,
            "unit": "iter/sec",
            "range": "stddev: 0.005011253281689043",
            "extra": "mean: 44.88297773684436 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 73.51959348762719,
            "unit": "iter/sec",
            "range": "stddev: 0.020890889720997213",
            "extra": "mean: 13.601816231047207 msec\nrounds: 277"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 23.421288473339665,
            "unit": "iter/sec",
            "range": "stddev: 0.07069347479978932",
            "extra": "mean: 42.69619927777649 msec\nrounds: 216"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1078.0068202561165,
            "unit": "iter/sec",
            "range": "stddev: 0.00018394055251777416",
            "extra": "mean: 927.6379158365776 usec\nrounds: 903"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 52.400592638973315,
            "unit": "iter/sec",
            "range": "stddev: 0.09105078166256433",
            "extra": "mean: 19.083753630225985 msec\nrounds: 311"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 59.28339587260414,
            "unit": "iter/sec",
            "range": "stddev: 0.05450156029781071",
            "extra": "mean: 16.86812952059848 msec\nrounds: 267"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 34.24193499612388,
            "unit": "iter/sec",
            "range": "stddev: 0.0831020848896965",
            "extra": "mean: 29.203957081081956 msec\nrounds: 74"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 34.31778744593185,
            "unit": "iter/sec",
            "range": "stddev: 0.03370895746144563",
            "extra": "mean: 29.139407707315453 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 6.09720866352639,
            "unit": "iter/sec",
            "range": "stddev: 0.10876833557864925",
            "extra": "mean: 164.009476333329 msec\nrounds: 27"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.985062585457852,
            "unit": "iter/sec",
            "range": "stddev: 0.1720830707461193",
            "extra": "mean: 200.59928694117994 msec\nrounds: 34"
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
          "id": "bdefbfcf7fb6c9a5b4b4e9f5d5110ee2b62ecc52",
          "message": "✨ feat(models): add PEP 561 py.typed marker for type export support (#93)\n\n* ✨ feat(models): add PEP 561 py.typed marker for type export support\n\nAdd py.typed marker file to enable downstream type checkers (mypy,\npyright) to recognize this package as fully typed.\n\n* 🐛 fix(models): correct type ignore comment for _version import\n\nChange type ignore from `import-untyped` to `import-not-found` since\nthe _version module is generated at build time by hatch-vcs.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude <noreply@anthropic.com>",
          "timestamp": "2026-01-13T03:27:23-05:00",
          "tree_id": "fb74bf7867c99b915ec18209032a8246a9e29de7",
          "url": "https://github.com/zeroae/zae-limiter/commit/bdefbfcf7fb6c9a5b4b4e9f5d5110ee2b62ecc52"
        },
        "date": 1768293035511,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 24.095952105344153,
            "unit": "iter/sec",
            "range": "stddev: 0.011415370034880107",
            "extra": "mean: 41.5007464999988 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 23.262100826046936,
            "unit": "iter/sec",
            "range": "stddev: 0.007659549760779457",
            "extra": "mean: 42.98837871428553 msec\nrounds: 21"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 74.4085921213095,
            "unit": "iter/sec",
            "range": "stddev: 0.02077905173192902",
            "extra": "mean: 13.43930816981034 msec\nrounds: 265"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.407823640635844,
            "unit": "iter/sec",
            "range": "stddev: 0.07353449443824646",
            "extra": "mean: 46.71189452914881 msec\nrounds: 223"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1067.3167558742928,
            "unit": "iter/sec",
            "range": "stddev: 0.000024581156915393308",
            "extra": "mean: 936.9289805450958 usec\nrounds: 771"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 53.174503719013494,
            "unit": "iter/sec",
            "range": "stddev: 0.08692113381480057",
            "extra": "mean: 18.806005323232235 msec\nrounds: 297"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 58.158208944618295,
            "unit": "iter/sec",
            "range": "stddev: 0.04595345026886007",
            "extra": "mean: 17.194477239700753 msec\nrounds: 267"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 27.937432163223406,
            "unit": "iter/sec",
            "range": "stddev: 0.06235987781403676",
            "extra": "mean: 35.794270359478176 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 35.599483675936966,
            "unit": "iter/sec",
            "range": "stddev: 0.02536407417433995",
            "extra": "mean: 28.090295047619968 msec\nrounds: 84"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.160334689854191,
            "unit": "iter/sec",
            "range": "stddev: 0.12269008462080012",
            "extra": "mean: 193.78588020000223 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.611193054157807,
            "unit": "iter/sec",
            "range": "stddev: 0.16567534540496626",
            "extra": "mean: 178.21521917856944 msec\nrounds: 28"
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
          "id": "c22f355d63f63cc4b253f1eb2eeb7b8d9f486194",
          "message": "feat(infra): add docker-compose.yml for LocalStack development (#94)\n\n* ✨ feat(infra): add docker-compose.yml for LocalStack development\n\nAdd docker-compose.yml at project root as the preferred method for\nstarting LocalStack. Update documentation to reflect this change:\n\n- CLAUDE.md: Update Local Development and Running Tests sections\n- README.md: Update Local Development with LocalStack section\n- docs/infra/localstack.md: Reorder tabs to show Docker Compose first\n\n* 📝 docs: replace docker run with docker compose in examples and tests\n\nUpdate all remaining LocalStack setup instructions to use the new\ndocker-compose.yml instead of docker run commands:\n\n- docs/migrations.md\n- examples/basic_rate_limiting.py\n- examples/hierarchical_limits.py\n- examples/llm_token_reconciliation.py\n- tests/e2e/test_localstack.py\n- tests/integration/test_repository.py\n\n* 📝 docs: add AWS credentials to LocalStack test instructions\n\nLocalStack requires AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and\nAWS_DEFAULT_REGION environment variables to be set. Update all test\ndocumentation to include these:\n\n- docker-compose.yml header comments\n- CLAUDE.md Running Tests section\n- tests/e2e/test_localstack.py docstring\n- tests/integration/test_repository.py docstring\n- tests/benchmark/test_localstack.py docstring\n- docs/migrations.md integration test section\n\n---------\n\nCo-authored-by: Claude <noreply@anthropic.com>",
          "timestamp": "2026-01-13T04:15:04-05:00",
          "tree_id": "af29c33e20bf205fc851abf0dbb02026c24e1b5a",
          "url": "https://github.com/zeroae/zae-limiter/commit/c22f355d63f63cc4b253f1eb2eeb7b8d9f486194"
        },
        "date": 1768295896440,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 24.42220870181788,
            "unit": "iter/sec",
            "range": "stddev: 0.009235932649109433",
            "extra": "mean: 40.946337499997064 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.532068004945142,
            "unit": "iter/sec",
            "range": "stddev: 0.0056646978566288145",
            "extra": "mean: 46.44235750000121 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 72.94136127644289,
            "unit": "iter/sec",
            "range": "stddev: 0.020332056350609345",
            "extra": "mean: 13.70964268421132 msec\nrounds: 285"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 23.471978742822543,
            "unit": "iter/sec",
            "range": "stddev: 0.06696016962483169",
            "extra": "mean: 42.60399223076956 msec\nrounds: 221"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1093.482280388744,
            "unit": "iter/sec",
            "range": "stddev: 0.00002768408024654435",
            "extra": "mean: 914.5095608174739 usec\nrounds: 929"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 52.61489375513802,
            "unit": "iter/sec",
            "range": "stddev: 0.08869118332362076",
            "extra": "mean: 19.00602526451641 msec\nrounds: 310"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 69.23433050442829,
            "unit": "iter/sec",
            "range": "stddev: 0.025623472426484484",
            "extra": "mean: 14.443701451493624 msec\nrounds: 268"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 26.790431226097315,
            "unit": "iter/sec",
            "range": "stddev: 0.06849284862479947",
            "extra": "mean: 37.32676012418463 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 27.44649960990866,
            "unit": "iter/sec",
            "range": "stddev: 0.09940402339062228",
            "extra": "mean: 36.43451858024849 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.391030928582271,
            "unit": "iter/sec",
            "range": "stddev: 0.12000136081676797",
            "extra": "mean: 185.49327823333024 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.363959910920156,
            "unit": "iter/sec",
            "range": "stddev: 0.17647758826558285",
            "extra": "mean: 186.4294320999979 msec\nrounds: 30"
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
          "id": "fccf670778873dd7e9a28bef6df73a3cabab7a52",
          "message": "✨ feat(limiter): rename stack_name to name for cloud-agnostic API (#95)\n\n* ✨ feat(limiter): rename stack_name to name for cloud-agnostic API\n\nBREAKING CHANGE: The `stack_name` parameter has been renamed to `name` in the public API.\n\nThis change makes the API cloud-agnostic, hiding AWS CloudFormation terminology from end users:\n\n**Python API:**\n- Before: `RateLimiter(stack_name=\"rate-limits\", region=\"us-east-1\")`\n- After: `RateLimiter(name=\"my-app\", region=\"us-east-1\")`\n\n**CLI:**\n- Before: `zae-limiter deploy --stack-name rate-limits`\n- After: `zae-limiter deploy --name my-app` (or `-n my-app`)\n\n**Default value:**\n- Changed from `\"rate-limits\"` to `\"limiter\"` (creates `ZAEL-limiter` resources)\n\n**New module:**\n- Added `naming.py` with `validate_name()` and `normalize_name()` functions\n- Centralizes ZAEL- prefix logic and validation rules\n\n**Internal code unchanged:**\n- `Repository` and `StackManager` still use `stack_name` internally (AWS-specific)\n- Only the public-facing API uses the generic `name` parameter\n\n**Test fixtures:**\n- Renamed `unique_table_name` to `unique_name` for consistency\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(naming): add comprehensive unit tests for naming module\n\n- Add tests/unit/test_naming.py with 33 tests covering:\n  - validate_name() for valid names (simple, hyphenated, alphanumeric)\n  - Error cases (empty, underscore, period, space, starts with number)\n  - Length validation (max 38 chars)\n  - normalize_name() with/without ZAEL- prefix\n  - Backward compatibility aliases (validate_stack_name, normalize_stack_name)\n  - Edge cases (unicode, emoji, special chars)\n\n- Add CLI validation error tests to test_cli.py:\n  - All commands (deploy, delete, status, version, check, upgrade)\n  - Verify helpful error messages for invalid names\n\nCoverage improvement:\n- naming.py: 75% → 96%\n- cli.py: Error handling paths now covered\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(tests): widen tolerance for flaky lease adjustment test\n\nThe test_lease_adjustment_workflow test failed intermittently because\ntoken bucket refill was occurring during test execution. With a refill\nrate of ~167 tokens/second, even a few milliseconds of delay caused\nthe assertion to fail.\n\nWidened tolerance from (200-300) to (150-350) tokens consumed, providing\n±100 token variance around the expected 250 consumed.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-13T05:14:35-05:00",
          "tree_id": "7c31ec57ca33892f6070c72adec5e3cfedbfaa11",
          "url": "https://github.com/zeroae/zae-limiter/commit/fccf670778873dd7e9a28bef6df73a3cabab7a52"
        },
        "date": 1768299423939,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 22.145762300172827,
            "unit": "iter/sec",
            "range": "stddev: 0.008264701074089683",
            "extra": "mean: 45.15536590908844 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.717607629895728,
            "unit": "iter/sec",
            "range": "stddev: 0.006126497051596935",
            "extra": "mean: 46.04558738889056 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 74.61836266986057,
            "unit": "iter/sec",
            "range": "stddev: 0.020460293574706072",
            "extra": "mean: 13.40152697298348 msec\nrounds: 259"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 22.51144176751148,
            "unit": "iter/sec",
            "range": "stddev: 0.06617648361435995",
            "extra": "mean: 44.42185490949763 msec\nrounds: 221"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1081.377766664992,
            "unit": "iter/sec",
            "range": "stddev: 0.000020730377474360403",
            "extra": "mean: 924.7462180437055 usec\nrounds: 931"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 52.010321892697824,
            "unit": "iter/sec",
            "range": "stddev: 0.12821069382376307",
            "extra": "mean: 19.226952720329127 msec\nrounds: 118"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 63.30200035325784,
            "unit": "iter/sec",
            "range": "stddev: 0.027813065130639126",
            "extra": "mean: 15.797289097018794 msec\nrounds: 268"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 36.52124965150867,
            "unit": "iter/sec",
            "range": "stddev: 0.07231618036857076",
            "extra": "mean: 27.381319356324127 msec\nrounds: 87"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 36.42535952166524,
            "unit": "iter/sec",
            "range": "stddev: 0.048219623887067714",
            "extra": "mean: 27.453400958340996 msec\nrounds: 48"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 16.91026409401087,
            "unit": "iter/sec",
            "range": "stddev: 0.009385970655188484",
            "extra": "mean: 59.13568200003283 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.9571569025510085,
            "unit": "iter/sec",
            "range": "stddev: 0.14307850310504766",
            "extra": "mean: 201.72853505713906 msec\nrounds: 35"
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
          "id": "d6d4ff0f9b3a82a534aa560d79675c7eb691c2b3",
          "message": "📝 docs: complete rename of table_name to name for cloud-agnostic API (#103)\n\n* 📝 docs(readme): update table_name to name for cloud-agnostic API\n\nFix documentation missed in PR #95 that renamed stack_name to name.\n\nUpdates include:\n- CLI examples: --table-name → --name\n- Python examples: table_name → name parameter\n- create_stack=True → stack_options=StackOptions()\n- Underscore names → hyphen names (rate_limits → my-app)\n- Comments reflect ZAEL- prefix convention\n\n* 📝 docs: complete rename of table_name to name across docs and examples\n\nContinue the work from PR #95 to fully migrate documentation and examples\nfrom the old API (`table_name`, `create_stack=True`) to the new cloud-agnostic\nAPI (`name`, `stack_options=StackOptions()`).\n\nChanges made across 19 files:\n- CLI flag: `--table-name` → `--name`\n- Python parameter: `table_name=` → `name=`\n- Python parameter: `create_stack=True` → `stack_options=StackOptions()`\n- Add StackOptions import where needed\n- Naming convention: `rate_limits` → `limiter`, `demo`, etc.\n- Environment variable: `TABLE_NAME` → `NAME` (in fastapi-demo)\n- Comments updated to reflect `ZAEL-{name}` prefix pattern\n\nDocumentation files:\n- docs/api/index.md\n- docs/cli.md\n- docs/getting-started.md\n- docs/guide/failure-modes.md\n- docs/index.md\n- docs/infra/cloudformation.md\n- docs/infra/deployment.md\n- docs/infra/localstack.md\n- docs/migrations.md\n\nExample files:\n- examples/basic_rate_limiting.py\n- examples/hierarchical_limits.py\n- examples/llm_token_reconciliation.py\n- examples/fastapi-demo/* (7 files)\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(docs): address PR review comments\n\n- Revert CloudFormation resource name to RateLimitsTable (docs/cli.md)\n- Change \"Creates ZAEL-X resources\" to \"Connects to ZAEL-X\" when no\n  stack_options is provided (docs/guide/failure-modes.md, docs/index.md,\n  docs/infra/localstack.md)\n- Remove non-existent client_config parameter example\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(docs): align cloudformation.md with actual template\n\n- TABLE_NAME: !Ref AWS::StackName → !Ref TableName\n- RETENTION_DAYS → SNAPSHOT_TTL_DAYS (matches template)\n- QueueName: ${AWS::StackName}-dlq → ${TableName}-aggregator-dlq\n- Update alarm example to match actual template structure\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude <noreply@anthropic.com>",
          "timestamp": "2026-01-13T09:48:11-05:00",
          "tree_id": "cd3fcea1874de72ee16713803cb757bc34c02690",
          "url": "https://github.com/zeroae/zae-limiter/commit/d6d4ff0f9b3a82a534aa560d79675c7eb691c2b3"
        },
        "date": 1768316082576,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 342.17460554235487,
            "unit": "iter/sec",
            "range": "stddev: 0.0001739146695028625",
            "extra": "mean: 2.9224845555530816 msec\nrounds: 9"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 24.000406498920576,
            "unit": "iter/sec",
            "range": "stddev: 0.058031566746158",
            "extra": "mean: 41.66596095132702 msec\nrounds: 226"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 28.747457049407746,
            "unit": "iter/sec",
            "range": "stddev: 0.14336657950752646",
            "extra": "mean: 34.78568550537592 msec\nrounds: 93"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1099.13696043801,
            "unit": "iter/sec",
            "range": "stddev: 0.00001637371226594759",
            "extra": "mean: 909.8047249739436 usec\nrounds: 949"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 76.29538223724606,
            "unit": "iter/sec",
            "range": "stddev: 0.03969115278685948",
            "extra": "mean: 13.106953142857677 msec\nrounds: 105"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 71.59648159876558,
            "unit": "iter/sec",
            "range": "stddev: 0.023644209789191364",
            "extra": "mean: 13.967166789062457 msec\nrounds: 256"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 26.89578300897883,
            "unit": "iter/sec",
            "range": "stddev: 0.06461170061824796",
            "extra": "mean: 37.18054981578941 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 57.309711246324376,
            "unit": "iter/sec",
            "range": "stddev: 0.056034949280433684",
            "extra": "mean: 17.449049702970473 msec\nrounds: 303"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 21.14159197775024,
            "unit": "iter/sec",
            "range": "stddev: 0.0873716863839446",
            "extra": "mean: 47.30012768444384 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.296536082748778,
            "unit": "iter/sec",
            "range": "stddev: 0.2389088865460209",
            "extra": "mean: 158.81748104958783 msec\nrounds: 121"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 23.265242377414133,
            "unit": "iter/sec",
            "range": "stddev: 0.007551938410374485",
            "extra": "mean: 42.982573909085886 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 22.86876437115989,
            "unit": "iter/sec",
            "range": "stddev: 0.006096486711425128",
            "extra": "mean: 43.72776700000082 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 37.92080016261182,
            "unit": "iter/sec",
            "range": "stddev: 0.0034163388604596784",
            "extra": "mean: 26.370751558822707 msec\nrounds: 34"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 33.18425596228108,
            "unit": "iter/sec",
            "range": "stddev: 0.0037413984823842943",
            "extra": "mean: 30.134772379306956 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 24.92736537332791,
            "unit": "iter/sec",
            "range": "stddev: 0.007444963260683914",
            "extra": "mean: 40.11655403703403 msec\nrounds: 27"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 230.24824346885146,
            "unit": "iter/sec",
            "range": "stddev: 0.0007635304848259191",
            "extra": "mean: 4.343138453237679 msec\nrounds: 139"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 40.156650166464665,
            "unit": "iter/sec",
            "range": "stddev: 0.1964370124574328",
            "extra": "mean: 24.90247557638941 msec\nrounds: 288"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 23.851598476586297,
            "unit": "iter/sec",
            "range": "stddev: 0.06394007883632041",
            "extra": "mean: 41.925911212267 msec\nrounds: 212"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 403.38889609415145,
            "unit": "iter/sec",
            "range": "stddev: 0.047804863018468806",
            "extra": "mean: 2.478997339001118 msec\nrounds: 941"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 62.543817625312144,
            "unit": "iter/sec",
            "range": "stddev: 0.03116974005409648",
            "extra": "mean: 15.988790546666111 msec\nrounds: 300"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 59.720309514760864,
            "unit": "iter/sec",
            "range": "stddev: 0.05745248799805252",
            "extra": "mean: 16.744722325205522 msec\nrounds: 246"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 30.00325312331084,
            "unit": "iter/sec",
            "range": "stddev: 0.04624579911160646",
            "extra": "mean: 33.32971914379032 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 27.020473985542164,
            "unit": "iter/sec",
            "range": "stddev: 0.10303423256146427",
            "extra": "mean: 37.00897328947929 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 17.154252624657392,
            "unit": "iter/sec",
            "range": "stddev: 0.009422141523649659",
            "extra": "mean: 58.29458279998789 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.510866373472695,
            "unit": "iter/sec",
            "range": "stddev: 0.12235730978986997",
            "extra": "mean: 181.45967117142163 msec\nrounds: 35"
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
          "id": "22b701fbb5ed7858ca86272408aa5f325abb13de",
          "message": "♻️ refactor(infra): use AWS::StackName instead of TableName parameter (#105)\n\nReplace the redundant TableName parameter with the built-in\nAWS::StackName pseudo-parameter in CloudFormation template.\n\nChanges:\n- Remove TableName parameter from cfn_template.yaml\n- Replace all !Ref TableName with !Ref AWS::StackName\n- Replace ${TableName} with ${AWS::StackName} in !Sub expressions\n- Update stack_manager.py to stop passing TableName parameter\n- Update cloudformation.md documentation\n- Update unit tests to reflect the new behavior\n\nThis ensures the DynamoDB table name always matches the stack name,\neliminating configuration drift and simplifying maintenance.\n\nCloses #104\n\nCo-authored-by: Claude <noreply@anthropic.com>",
          "timestamp": "2026-01-13T10:26:08-05:00",
          "tree_id": "13a91d631faf77e42d27369ac94fd5648b59a650",
          "url": "https://github.com/zeroae/zae-limiter/commit/22b701fbb5ed7858ca86272408aa5f325abb13de"
        },
        "date": 1768318357011,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 335.92656040709585,
            "unit": "iter/sec",
            "range": "stddev: 0.0001695249191475929",
            "extra": "mean: 2.976841125001073 msec\nrounds: 8"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 22.51781871473826,
            "unit": "iter/sec",
            "range": "stddev: 0.06195140061584818",
            "extra": "mean: 44.40927483555432 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 23.4614779214756,
            "unit": "iter/sec",
            "range": "stddev: 0.12528333210908763",
            "extra": "mean: 42.62306080405294 msec\nrounds: 148"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1079.9135718751463,
            "unit": "iter/sec",
            "range": "stddev: 0.00002044606704055631",
            "extra": "mean: 926.000030042788 usec\nrounds: 932"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 53.71017334873729,
            "unit": "iter/sec",
            "range": "stddev: 0.0728024621428421",
            "extra": "mean: 18.618446704817973 msec\nrounds: 166"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 57.18819189310112,
            "unit": "iter/sec",
            "range": "stddev: 0.03456731109931138",
            "extra": "mean: 17.48612723880565 msec\nrounds: 268"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 25.019342712383878,
            "unit": "iter/sec",
            "range": "stddev: 0.07346228801593066",
            "extra": "mean: 39.96907558666711 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 50.450548723018336,
            "unit": "iter/sec",
            "range": "stddev: 0.05837481160437827",
            "extra": "mean: 19.82138996128985 msec\nrounds: 310"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 21.39621258203915,
            "unit": "iter/sec",
            "range": "stddev: 0.08616442634043588",
            "extra": "mean: 46.737243620370485 msec\nrounds: 216"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 9.342147117741922,
            "unit": "iter/sec",
            "range": "stddev: 0.19868793295210413",
            "extra": "mean: 107.04177395160832 msec\nrounds: 62"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.607554392311055,
            "unit": "iter/sec",
            "range": "stddev: 0.0063090727265726195",
            "extra": "mean: 46.28011027272227 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.86784132661745,
            "unit": "iter/sec",
            "range": "stddev: 0.00481724974856363",
            "extra": "mean: 47.92062505883036 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 34.3569441329885,
            "unit": "iter/sec",
            "range": "stddev: 0.004840184040018285",
            "extra": "mean: 29.106197458342347 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 31.269772845670154,
            "unit": "iter/sec",
            "range": "stddev: 0.0031628188562764442",
            "extra": "mean: 31.97976540908795 msec\nrounds: 22"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 28.00007726677202,
            "unit": "iter/sec",
            "range": "stddev: 0.005877618404369088",
            "extra": "mean: 35.71418716000153 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 226.30516311118274,
            "unit": "iter/sec",
            "range": "stddev: 0.000539908534398634",
            "extra": "mean: 4.418812130718839 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 57.35470912436883,
            "unit": "iter/sec",
            "range": "stddev: 0.035231240945909674",
            "extra": "mean: 17.435359977706185 msec\nrounds: 314"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.195186578328382,
            "unit": "iter/sec",
            "range": "stddev: 0.09282755459877594",
            "extra": "mean: 47.18052357333237 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1121.3181220173349,
            "unit": "iter/sec",
            "range": "stddev: 0.00005649182296572638",
            "extra": "mean: 891.8075792808248 usec\nrounds: 946"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 48.76739109897629,
            "unit": "iter/sec",
            "range": "stddev: 0.09948983768412197",
            "extra": "mean: 20.50550536875022 msec\nrounds: 320"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 57.92318838263065,
            "unit": "iter/sec",
            "range": "stddev: 0.059636019434330965",
            "extra": "mean: 17.264243007380248 msec\nrounds: 271"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 26.234681233018755,
            "unit": "iter/sec",
            "range": "stddev: 0.08104265656611687",
            "extra": "mean: 38.117482393550425 msec\nrounds: 155"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 53.822443103537644,
            "unit": "iter/sec",
            "range": "stddev: 0.003593903151359243",
            "extra": "mean: 18.579609960779944 msec\nrounds: 51"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.146253383554139,
            "unit": "iter/sec",
            "range": "stddev: 0.1478520079182256",
            "extra": "mean: 194.31612193750425 msec\nrounds: 32"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.051736430631566,
            "unit": "iter/sec",
            "range": "stddev: 0.24648871562151053",
            "extra": "mean: 197.9517367407429 msec\nrounds: 27"
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
          "id": "81942dddd28671fc438a98b56211f782cc34c28c",
          "message": "📝 docs: create monitoring and observability guide (#108)\n\n- Add comprehensive docs/monitoring.md with structured logging, CloudWatch metrics, Logs Insights queries, dashboard templates, alert configuration, and troubleshooting guide\n- Add monitoring page to mkdocs navigation\n- Update docs/index.md and docs/infra/deployment.md with cross-references\n- Create follow-up issue #107 for X-Ray tracing integration\n\nCloses #38",
          "timestamp": "2026-01-13T11:11:11-05:00",
          "tree_id": "1c5bb1c8d695fdb5342fc3059a5e9b1ce4a0ecac",
          "url": "https://github.com/zeroae/zae-limiter/commit/81942dddd28671fc438a98b56211f782cc34c28c"
        },
        "date": 1768321194294,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 338.6329897909992,
            "unit": "iter/sec",
            "range": "stddev: 0.0001780012410320597",
            "extra": "mean: 2.95304955555922 msec\nrounds: 9"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 24.36547648935214,
            "unit": "iter/sec",
            "range": "stddev: 0.056259142201424514",
            "extra": "mean: 41.041676342221585 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 25.421870295454355,
            "unit": "iter/sec",
            "range": "stddev: 0.11696962276301681",
            "extra": "mean: 39.33620887755094 msec\nrounds: 147"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1070.702561054968,
            "unit": "iter/sec",
            "range": "stddev: 0.000047785427551150656",
            "extra": "mean: 933.9661978716997 usec\nrounds: 940"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 84.47161816651236,
            "unit": "iter/sec",
            "range": "stddev: 0.012723178063069811",
            "extra": "mean: 11.838295769696012 msec\nrounds: 165"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 68.98420977800723,
            "unit": "iter/sec",
            "range": "stddev: 0.023921393242433517",
            "extra": "mean: 14.496070959108224 msec\nrounds: 269"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 27.322015217087273,
            "unit": "iter/sec",
            "range": "stddev: 0.06378912577924555",
            "extra": "mean: 36.600521303223516 msec\nrounds: 155"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 57.88498824650986,
            "unit": "iter/sec",
            "range": "stddev: 0.05220171835210471",
            "extra": "mean: 17.27563622784867 msec\nrounds: 316"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 21.66976261927851,
            "unit": "iter/sec",
            "range": "stddev: 0.08593401136815133",
            "extra": "mean: 46.14725216742106 msec\nrounds: 221"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.800018173728659,
            "unit": "iter/sec",
            "range": "stddev: 0.20719513106440526",
            "extra": "mean: 147.0584304999981 msec\nrounds: 122"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.317549296836123,
            "unit": "iter/sec",
            "range": "stddev: 0.009690481959013373",
            "extra": "mean: 46.90970740001603 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 22.590625805436247,
            "unit": "iter/sec",
            "range": "stddev: 0.0057548260152146865",
            "extra": "mean: 44.26614865000147 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 39.01746609568898,
            "unit": "iter/sec",
            "range": "stddev: 0.004537692151988674",
            "extra": "mean: 25.629547483876443 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 32.5690822241508,
            "unit": "iter/sec",
            "range": "stddev: 0.004412155121081424",
            "extra": "mean: 30.703966206897743 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 25.01440840346339,
            "unit": "iter/sec",
            "range": "stddev: 0.006686750865683713",
            "extra": "mean: 39.976959833339265 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 240.37948149998832,
            "unit": "iter/sec",
            "range": "stddev: 0.00043122908961592697",
            "extra": "mean: 4.160088846851301 msec\nrounds: 111"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 95.19232693729518,
            "unit": "iter/sec",
            "range": "stddev: 0.025147602455250117",
            "extra": "mean: 10.505048381249438 msec\nrounds: 160"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 23.774876453168705,
            "unit": "iter/sec",
            "range": "stddev: 0.06283264404541508",
            "extra": "mean: 42.06120700436786 msec\nrounds: 229"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1103.6205524660224,
            "unit": "iter/sec",
            "range": "stddev: 0.0002130267931386226",
            "extra": "mean: 906.1085331960483 usec\nrounds: 979"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 64.88979198158967,
            "unit": "iter/sec",
            "range": "stddev: 0.02669712123697526",
            "extra": "mean: 15.41074442469652 msec\nrounds: 332"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 70.9409059529263,
            "unit": "iter/sec",
            "range": "stddev: 0.022976015953534077",
            "extra": "mean: 14.096239490704589 msec\nrounds: 269"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 31.58156257528983,
            "unit": "iter/sec",
            "range": "stddev: 0.040142879583289055",
            "extra": "mean: 31.66404441249604 msec\nrounds: 160"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 37.78857682309808,
            "unit": "iter/sec",
            "range": "stddev: 0.023669567414364004",
            "extra": "mean: 26.46302359258883 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.398160131360912,
            "unit": "iter/sec",
            "range": "stddev: 0.1276751180720113",
            "extra": "mean: 185.2483023225718 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.000291430179624,
            "unit": "iter/sec",
            "range": "stddev: 0.1912052295936047",
            "extra": "mean: 199.98834347222783 msec\nrounds: 36"
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
          "id": "47a7ed0bfb69c3e760a33abb55a30059b4819fa5",
          "message": "📝 docs: create troubleshooting guide (#36) (#109)\n\n* 📝 docs: add implementation plan for troubleshooting guide (#36)\n\nCreate detailed plan for docs/troubleshooting.md covering:\n- Rate limit enforcement failures\n- DynamoDB throttling issues\n- Lambda aggregator malfunctions\n- Version compatibility errors\n- Stream processing lag\n- Recovery procedures\n\nPlan includes implementation steps, code references, CLI commands,\nand acceptance criteria for the troubleshooting documentation.\n\n* 📝 docs: update plan to consolidate troubleshooting from monitoring.md\n\n- Add consolidation decision section explaining the approach\n- Note existing content in monitoring.md to migrate (4 sections)\n- Add Step 9 to update monitoring.md (remove troubleshooting, add link)\n- Update file changes summary to include monitoring.md\n- Add acceptance criteria for consolidation\n\n* 📝 docs: create troubleshooting guide (#36)\n\nCreate comprehensive troubleshooting guide covering:\n- Rate limit enforcement failures\n- DynamoDB throttling issues\n- Lambda aggregator malfunctions\n- Version compatibility errors\n- Stream processing lag\n- Recovery procedures (backup/restore, migration rollback)\n\nIncludes quick reference tables for CLI commands, CloudWatch\nmetrics, exception types, and DynamoDB key patterns.\n\nConsolidates troubleshooting content from monitoring.md into\nthe new dedicated guide and adds cross-links from index.md\nand failure-modes.md.\n\n* 🐛 fix(docs): remove non-existent migrate CLI command\n\nReplace reference to future `zae-limiter migrate` command with\nlink to migration guide, since the CLI command doesn't exist yet.\n\n* 🗑️ chore: remove implementation plan file\n\n---------\n\nCo-authored-by: Claude <noreply@anthropic.com>",
          "timestamp": "2026-01-13T11:50:31-05:00",
          "tree_id": "fb3a27bc7904d8ebd602b2f124baba8715ea96e9",
          "url": "https://github.com/zeroae/zae-limiter/commit/47a7ed0bfb69c3e760a33abb55a30059b4819fa5"
        },
        "date": 1768323426194,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 339.7459388364391,
            "unit": "iter/sec",
            "range": "stddev: 0.00019383822634400574",
            "extra": "mean: 2.9433758749988215 msec\nrounds: 8"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 22.39792357056561,
            "unit": "iter/sec",
            "range": "stddev: 0.05924852474695455",
            "extra": "mean: 44.6469958185837 msec\nrounds: 226"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 28.35316521126844,
            "unit": "iter/sec",
            "range": "stddev: 0.1583791316345928",
            "extra": "mean: 35.26943085714355 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1094.881350938279,
            "unit": "iter/sec",
            "range": "stddev: 0.00005539456424230985",
            "extra": "mean: 913.3409744745688 usec\nrounds: 666"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 76.90817526447044,
            "unit": "iter/sec",
            "range": "stddev: 0.01690784451786466",
            "extra": "mean: 13.002518868263591 msec\nrounds: 167"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 64.35707156789456,
            "unit": "iter/sec",
            "range": "stddev: 0.029815797534922615",
            "extra": "mean: 15.538308000000175 msec\nrounds: 265"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 26.33029779356974,
            "unit": "iter/sec",
            "range": "stddev: 0.06563564423102211",
            "extra": "mean: 37.97906152980219 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 53.28286125329251,
            "unit": "iter/sec",
            "range": "stddev: 0.05704236873253452",
            "extra": "mean: 18.767760898692487 msec\nrounds: 306"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 20.753848424830938,
            "unit": "iter/sec",
            "range": "stddev: 0.08629625118777556",
            "extra": "mean: 48.18383460888874 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 5.9863375350148385,
            "unit": "iter/sec",
            "range": "stddev: 0.2302842636415042",
            "extra": "mean: 167.0470457355394 msec\nrounds: 121"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.62613009694978,
            "unit": "iter/sec",
            "range": "stddev: 0.008202357309914645",
            "extra": "mean: 46.24035809999327 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.260408298636197,
            "unit": "iter/sec",
            "range": "stddev: 0.006194380043293403",
            "extra": "mean: 47.03578529412098 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 38.83060231285526,
            "unit": "iter/sec",
            "range": "stddev: 0.003188346173030659",
            "extra": "mean: 25.75288407692146 msec\nrounds: 26"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 30.107143797991846,
            "unit": "iter/sec",
            "range": "stddev: 0.005333773513659819",
            "extra": "mean: 33.21470833333251 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 25.406006814751315,
            "unit": "iter/sec",
            "range": "stddev: 0.004524680202935645",
            "extra": "mean: 39.36077035999915 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 222.85891560641818,
            "unit": "iter/sec",
            "range": "stddev: 0.0006141308988496417",
            "extra": "mean: 4.487143793547206 msec\nrounds: 155"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 39.98226804786045,
            "unit": "iter/sec",
            "range": "stddev: 0.1792862963231713",
            "extra": "mean: 25.01108738511177 msec\nrounds: 309"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.311039448726273,
            "unit": "iter/sec",
            "range": "stddev: 0.08446215338565455",
            "extra": "mean: 46.924036831050415 msec\nrounds: 219"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1078.1581273248844,
            "unit": "iter/sec",
            "range": "stddev: 0.00022897987928357288",
            "extra": "mean: 927.5077325449379 usec\nrounds: 931"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 49.4557021374185,
            "unit": "iter/sec",
            "range": "stddev: 0.08993363526420757",
            "extra": "mean: 20.22011531089746 msec\nrounds: 312"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 58.7081752252191,
            "unit": "iter/sec",
            "range": "stddev: 0.05528884986361054",
            "extra": "mean: 17.03340286363445 msec\nrounds: 264"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 33.06397741927539,
            "unit": "iter/sec",
            "range": "stddev: 0.0373825610520135",
            "extra": "mean: 30.244395201438394 msec\nrounds: 139"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 35.48102878472947,
            "unit": "iter/sec",
            "range": "stddev: 0.028002029760994772",
            "extra": "mean: 28.184075666666857 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.890129944931604,
            "unit": "iter/sec",
            "range": "stddev: 0.11351194652332193",
            "extra": "mean: 169.77554134616156 msec\nrounds: 26"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.198030614655946,
            "unit": "iter/sec",
            "range": "stddev: 0.14120629964612103",
            "extra": "mean: 192.38055219999686 msec\nrounds: 35"
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
          "id": "4c2aa3da79baf923d197bbbed07c1729b6019c4d",
          "message": "feat(ci): add path-based filtering and PR docs preview (#110)\n\n* ✨ feat(ci): add path-based filtering and PR docs preview\n\n- Add change detection job using dorny/paths-filter\n- Skip CI jobs (lint, typecheck, unit, integration, e2e, benchmark)\n  when only docs change\n- Deploy docs preview to gh-pages on PR (using mike)\n- Auto-cleanup PR preview when PR is closed/merged\n- Add PR comment with preview link\n\n* ✨ feat(ci): trigger docs preview on 'docs-preview' label\n\n- Add 'labeled' event type to PR trigger\n- Check for 'docs-preview' label on PR\n- Deploy preview if docs changed OR label is present\n\n---------\n\nCo-authored-by: Claude <noreply@anthropic.com>",
          "timestamp": "2026-01-13T12:29:31-05:00",
          "tree_id": "fe26e28f16ca6c68011bc149a07049ea62630684",
          "url": "https://github.com/zeroae/zae-limiter/commit/4c2aa3da79baf923d197bbbed07c1729b6019c4d"
        },
        "date": 1768325739270,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 342.60662888999207,
            "unit": "iter/sec",
            "range": "stddev: 0.00018200713005738577",
            "extra": "mean: 2.9187993333342397 msec\nrounds: 6"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 21.937959165154837,
            "unit": "iter/sec",
            "range": "stddev: 0.06415455932031523",
            "extra": "mean: 45.583091502346775 msec\nrounds: 213"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 23.569085415784922,
            "unit": "iter/sec",
            "range": "stddev: 0.11926121046085396",
            "extra": "mean: 42.42846009333353 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1034.9666381215684,
            "unit": "iter/sec",
            "range": "stddev: 0.00010619444039845777",
            "extra": "mean: 966.2147195536353 usec\nrounds: 895"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 75.09460647979805,
            "unit": "iter/sec",
            "range": "stddev: 0.014453927440960324",
            "extra": "mean: 13.316535592593059 msec\nrounds: 162"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 89.68123254300045,
            "unit": "iter/sec",
            "range": "stddev: 0.03587536542967197",
            "extra": "mean: 11.1506049999984 msec\nrounds: 73"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 27.360918404183476,
            "unit": "iter/sec",
            "range": "stddev: 0.04010714194313852",
            "extra": "mean: 36.54848076470636 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 54.673097368448886,
            "unit": "iter/sec",
            "range": "stddev: 0.05373338554596662",
            "extra": "mean: 18.290531324041773 msec\nrounds: 287"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 20.526273198738263,
            "unit": "iter/sec",
            "range": "stddev: 0.07451017915815238",
            "extra": "mean: 48.718049804650825 msec\nrounds: 215"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 5.957467193039691,
            "unit": "iter/sec",
            "range": "stddev: 0.20400580793140022",
            "extra": "mean: 167.85656850420153 msec\nrounds: 119"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 19.51999603603363,
            "unit": "iter/sec",
            "range": "stddev: 0.008424252348648409",
            "extra": "mean: 51.22951860000455 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 19.162681859301298,
            "unit": "iter/sec",
            "range": "stddev: 0.008415187760496592",
            "extra": "mean: 52.184762411771395 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 31.652193224124943,
            "unit": "iter/sec",
            "range": "stddev: 0.005499843430229775",
            "extra": "mean: 31.593387318190995 msec\nrounds: 22"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 27.5065382491358,
            "unit": "iter/sec",
            "range": "stddev: 0.0038559565795379877",
            "extra": "mean: 36.35499279999067 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 23.248768846626536,
            "unit": "iter/sec",
            "range": "stddev: 0.005102651816009617",
            "extra": "mean: 43.013030349996484 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 170.96360398855074,
            "unit": "iter/sec",
            "range": "stddev: 0.0007073140199658543",
            "extra": "mean: 5.84919817241902 msec\nrounds: 116"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 57.76112853003792,
            "unit": "iter/sec",
            "range": "stddev: 0.028348997945241254",
            "extra": "mean: 17.312681130874427 msec\nrounds: 298"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 35.571176847807195,
            "unit": "iter/sec",
            "range": "stddev: 0.07874716863193053",
            "extra": "mean: 28.112648740257956 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1090.5123663077295,
            "unit": "iter/sec",
            "range": "stddev: 0.000043361339907305705",
            "extra": "mean: 917.0001468078831 usec\nrounds: 940"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 56.60179612106028,
            "unit": "iter/sec",
            "range": "stddev: 0.030994978238674274",
            "extra": "mean: 17.667283876666982 msec\nrounds: 300"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 52.757830989375755,
            "unit": "iter/sec",
            "range": "stddev: 0.049453226581598994",
            "extra": "mean: 18.954532080770676 msec\nrounds: 260"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 26.48439251871121,
            "unit": "iter/sec",
            "range": "stddev: 0.062381883169693855",
            "extra": "mean: 37.758087118422694 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 53.36982042487419,
            "unit": "iter/sec",
            "range": "stddev: 0.0036863564071238286",
            "extra": "mean: 18.73718127659144 msec\nrounds: 47"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 4.878656725847347,
            "unit": "iter/sec",
            "range": "stddev: 0.18455633599089927",
            "extra": "mean: 204.97445427999764 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.384431097321975,
            "unit": "iter/sec",
            "range": "stddev: 0.15482829221877942",
            "extra": "mean: 185.720641962967 msec\nrounds: 27"
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
          "id": "87c6bf54f049c51de5fecbf0921560c258ebf0ab",
          "message": "Revert \"feat(ci): add path-based filtering and PR docs preview (#110)\" (#113)\n\nThis reverts commit 4c2aa3da79baf923d197bbbed07c1729b6019c4d.",
          "timestamp": "2026-01-13T13:28:30-05:00",
          "tree_id": "4992c8a5dd01b4bcaf6ea72c6027ae840dd1a825",
          "url": "https://github.com/zeroae/zae-limiter/commit/87c6bf54f049c51de5fecbf0921560c258ebf0ab"
        },
        "date": 1768329265091,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 342.33792939959716,
            "unit": "iter/sec",
            "range": "stddev: 0.00018353413835247987",
            "extra": "mean: 2.9210902857122227 msec\nrounds: 7"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 24.15330831738358,
            "unit": "iter/sec",
            "range": "stddev: 0.05744423605948116",
            "extra": "mean: 41.40219579279256 msec\nrounds: 222"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 29.416556453573843,
            "unit": "iter/sec",
            "range": "stddev: 0.14759049641460814",
            "extra": "mean: 33.99446164197472 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1067.3125782160932,
            "unit": "iter/sec",
            "range": "stddev: 0.0002084410449938991",
            "extra": "mean: 936.9326478578566 usec\nrounds: 957"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 83.99246122123414,
            "unit": "iter/sec",
            "range": "stddev: 0.012267081395723883",
            "extra": "mean: 11.905830421685392 msec\nrounds: 166"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 71.69317829113885,
            "unit": "iter/sec",
            "range": "stddev: 0.02201353013820608",
            "extra": "mean: 13.948328471909836 msec\nrounds: 267"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 33.82482085925945,
            "unit": "iter/sec",
            "range": "stddev: 0.03375903396054684",
            "extra": "mean: 29.56408857746405 msec\nrounds: 142"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 59.837108045442164,
            "unit": "iter/sec",
            "range": "stddev: 0.04857737029928214",
            "extra": "mean: 16.712037607843094 msec\nrounds: 306"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 22.144141434471553,
            "unit": "iter/sec",
            "range": "stddev: 0.08109207166248353",
            "extra": "mean: 45.15867110762355 msec\nrounds: 223"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.564348219078334,
            "unit": "iter/sec",
            "range": "stddev: 0.20797230782133028",
            "extra": "mean: 152.33804890082519 msec\nrounds: 121"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 22.743088426033218,
            "unit": "iter/sec",
            "range": "stddev: 0.00906868262416639",
            "extra": "mean: 43.969402099995136 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.47411816100919,
            "unit": "iter/sec",
            "range": "stddev: 0.005588520325509957",
            "extra": "mean: 46.5676863888973 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 38.81277399058028,
            "unit": "iter/sec",
            "range": "stddev: 0.004139046884837694",
            "extra": "mean: 25.76471344827598 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 32.76728640052692,
            "unit": "iter/sec",
            "range": "stddev: 0.0032610779356879475",
            "extra": "mean: 30.518242730771853 msec\nrounds: 26"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 24.703083938187334,
            "unit": "iter/sec",
            "range": "stddev: 0.004352814597288496",
            "extra": "mean: 40.48077569999862 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 205.3175538338585,
            "unit": "iter/sec",
            "range": "stddev: 0.0006324336285982313",
            "extra": "mean: 4.870504159664754 msec\nrounds: 119"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 114.09119857137928,
            "unit": "iter/sec",
            "range": "stddev: 0.022354332816622753",
            "extra": "mean: 8.764918000001257 msec\nrounds: 110"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 23.506624014801055,
            "unit": "iter/sec",
            "range": "stddev: 0.05839386689566444",
            "extra": "mean: 42.541200274881895 msec\nrounds: 211"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1098.3365694970637,
            "unit": "iter/sec",
            "range": "stddev: 0.00002655412915822757",
            "extra": "mean: 910.4677270810597 usec\nrounds: 949"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 63.48069619124446,
            "unit": "iter/sec",
            "range": "stddev: 0.02893484340617802",
            "extra": "mean: 15.75282030599287 msec\nrounds: 317"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 59.17830823537582,
            "unit": "iter/sec",
            "range": "stddev: 0.052086946861473374",
            "extra": "mean: 16.898083602231406 msec\nrounds: 269"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 27.03460626694934,
            "unit": "iter/sec",
            "range": "stddev: 0.06948363819896514",
            "extra": "mean: 36.98962692948599 msec\nrounds: 156"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 28.682936365084245,
            "unit": "iter/sec",
            "range": "stddev: 0.1039838834673427",
            "extra": "mean: 34.86393398750138 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.715713835545945,
            "unit": "iter/sec",
            "range": "stddev: 0.10509865969497853",
            "extra": "mean: 174.9562747142822 msec\nrounds: 28"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.977739161495242,
            "unit": "iter/sec",
            "range": "stddev: 0.1873734266239214",
            "extra": "mean: 200.89441562856302 msec\nrounds: 35"
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
          "id": "6538975429285b75c7ba02a032752318f18cf8e5",
          "message": "📝 docs: create consolidated operations guide (#112)\n\n- Replaces docs/troubleshooting.md with component-centric docs/operations/ directory\n- Adds interactive Mermaid flowchart navigation with clickable nodes\n- Includes Mermaid decision trees in each component file\n- Adds operational runbooks for version upgrades, DynamoDB scaling, runtime limit adjustment, and emergency rollback\n\nCloses #37",
          "timestamp": "2026-01-13T14:15:05-05:00",
          "tree_id": "2317aa1ac6044b401b7654ee35698b9a661870e5",
          "url": "https://github.com/zeroae/zae-limiter/commit/6538975429285b75c7ba02a032752318f18cf8e5"
        },
        "date": 1768332124693,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 315.97699106092426,
            "unit": "iter/sec",
            "range": "stddev: 0.00032286750013227187",
            "extra": "mean: 3.164787400001501 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 19.74679136717664,
            "unit": "iter/sec",
            "range": "stddev: 0.07351019607998269",
            "extra": "mean: 50.64113867441838 msec\nrounds: 215"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 21.00570483138619,
            "unit": "iter/sec",
            "range": "stddev: 0.1415726442851429",
            "extra": "mean: 47.6061150067112 msec\nrounds: 149"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 480.27519821146393,
            "unit": "iter/sec",
            "range": "stddev: 0.03395596737104967",
            "extra": "mean: 2.0821395810651513 msec\nrounds: 845"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 63.41989707535888,
            "unit": "iter/sec",
            "range": "stddev: 0.024206913844996097",
            "extra": "mean: 15.767922152439748 msec\nrounds: 164"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 66.14037090828877,
            "unit": "iter/sec",
            "range": "stddev: 0.03876597274001301",
            "extra": "mean: 15.119358816215515 msec\nrounds: 185"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 23.22101420120585,
            "unit": "iter/sec",
            "range": "stddev: 0.06658952928744355",
            "extra": "mean: 43.06444117105233 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 45.43640012044292,
            "unit": "iter/sec",
            "range": "stddev: 0.06678818761926818",
            "extra": "mean: 22.008785848993263 msec\nrounds: 298"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 18.224624326891156,
            "unit": "iter/sec",
            "range": "stddev: 0.10227261713013922",
            "extra": "mean: 54.87081555499941 msec\nrounds: 200"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 5.407296847883517,
            "unit": "iter/sec",
            "range": "stddev: 0.24540881972870376",
            "extra": "mean: 184.93528802499762 msec\nrounds: 120"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 20.14416924080268,
            "unit": "iter/sec",
            "range": "stddev: 0.007839613651987222",
            "extra": "mean: 49.642156400000204 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.224164942260465,
            "unit": "iter/sec",
            "range": "stddev: 0.00626606570023327",
            "extra": "mean: 49.44579926315758 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 33.19474561378984,
            "unit": "iter/sec",
            "range": "stddev: 0.004850379948341369",
            "extra": "mean: 30.125249689655025 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 27.5609730833241,
            "unit": "iter/sec",
            "range": "stddev: 0.00349125790667861",
            "extra": "mean: 36.28318916667913 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 22.779971296304332,
            "unit": "iter/sec",
            "range": "stddev: 0.004726646651933567",
            "extra": "mean: 43.89821159090894 msec\nrounds: 22"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 193.8541548384558,
            "unit": "iter/sec",
            "range": "stddev: 0.0004729081924385611",
            "extra": "mean: 5.158517241135887 msec\nrounds: 141"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 52.01055183849446,
            "unit": "iter/sec",
            "range": "stddev: 0.03988149541446298",
            "extra": "mean: 19.226867715329107 msec\nrounds: 274"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 17.728284923216343,
            "unit": "iter/sec",
            "range": "stddev: 0.09622666428154858",
            "extra": "mean: 56.40703566820696 msec\nrounds: 217"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1108.62917806708,
            "unit": "iter/sec",
            "range": "stddev: 0.00003569870547702116",
            "extra": "mean: 902.0148664529312 usec\nrounds: 936"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 40.73079870804984,
            "unit": "iter/sec",
            "range": "stddev: 0.11816564684800532",
            "extra": "mean: 24.55144587681176 msec\nrounds: 276"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 46.9836949473123,
            "unit": "iter/sec",
            "range": "stddev: 0.06208865443952454",
            "extra": "mean: 21.28397949802381 msec\nrounds: 253"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 21.893433008480198,
            "unit": "iter/sec",
            "range": "stddev: 0.08478085530824232",
            "extra": "mean: 45.67579692105209 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 20.627837473463885,
            "unit": "iter/sec",
            "range": "stddev: 0.16838263409459772",
            "extra": "mean: 48.478179125001475 msec\nrounds: 40"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 4.569977998638207,
            "unit": "iter/sec",
            "range": "stddev: 0.15985025882601625",
            "extra": "mean: 218.81943420690138 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.51249361834122,
            "unit": "iter/sec",
            "range": "stddev: 0.23232805416623273",
            "extra": "mean: 221.6069616000027 msec\nrounds: 25"
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
          "id": "06636b70112e1583250767bbfff84b13e2ae016e",
          "message": "📝 docs: reorganize documentation by persona with production guide (#117)\n\n* 📝 docs: reorganize docs by persona with production guide (#39)\n\nReorganize documentation structure by audience (Option B):\n- User Guide: Getting started, basic usage, hierarchies, LLM integration\n- Operator Guide: Deployment, production, CloudFormation, monitoring\n- Reference: CLI, API documentation\n- Contributing: Development setup, LocalStack, testing, architecture\n\nKey changes:\n- Create docs/infra/production.md with security, multi-region, cost guidance\n- Move LocalStack from infra/ to contributing/ (developer-only feature)\n- Create contributing section with development, testing, architecture docs\n- Restructure README from ~465 lines to ~113 lines (link to full docs)\n- Update CLAUDE.md with new docs organization decisions\n- Fix broken links after LocalStack relocation\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: add audit logging documentation\n\nAdd comprehensive documentation for the audit capabilities:\n- Create docs/infra/auditing.md with operator guide for audit logging\n- Add AuditEvent and AuditAction to API reference\n- Add cross-references from production and monitoring guides\n- Add Auditing nav entry under Operator Guide\n\nPlanned capabilities tracked in #77 (S3 archival) and #114 (public API).\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(readme): reorganize quick start for clarity\n\n- Separate Installation and Usage sections\n- Move hierarchical entity example after basic acquire pattern\n- Add inline comment explaining commit/rollback behavior\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: add token bucket algorithm documentation\n\n- Create docs/guide/token-bucket.md explaining the algorithm\n  - Classic algorithm overview with Mermaid diagrams\n  - Comparison table: classic vs zae-limiter implementation\n  - Key concepts: capacity/burst, lazy refill, negative buckets\n  - Practical implications and limit selection guide\n\n- Expand docs/contributing/architecture.md\n  - Add mathematical formulas for refill, drift compensation, retry\n  - Add code walkthrough with function references\n  - Add design decisions table with rationale\n\n- Improve src/zae_limiter/bucket.py module docstring\n  - Document key features and functions\n  - Add links to documentation\n\n- Add cross-references between documentation pages\n- Frame negative buckets as general \"estimate-then-reconcile\" pattern\n  (not LLM-specific)\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: generalize project description and fix inaccurate claims\n\n- Replace LLM-specific framing with problem-focused language\n- Fix misleading \"Global\" claim → \"Regional\" (single-region design)\n- Add cost estimate (~$1/1M requests) to overview\n- Add 99.99% SLA to \"Why DynamoDB?\" section\n- Add \"tenant → user\" as additional hierarchy example\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(hierarchical): add warning about planned cascade API changes\n\nLink to issue #116 which proposes moving cascade from per-call\nparameter to per-entity configuration.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: restructure Getting Started with declarative infrastructure focus\n\n- Lead with programmatic API in Quick Start (moved from deployment-first)\n- Add Infrastructure Persistence section explaining AWS resources outlive Python sessions\n- Add Infrastructure Lifecycle section with tabbed Programmatic/CLI options\n- Add Connecting to Existing Infrastructure section (omit stack_options)\n- Add Declarative State Management warning about conflicting StackOptions\n- Expand Understanding Limits with entity_id/resource concepts and examples\n- Cross-link to Token Bucket Algorithm page\n\nMessaging changes applied across all docs:\n- \"auto-creation\" → \"declarative infrastructure\"\n- \"idempotent/creates if not exists\" → \"declare desired state\"\n- Soften production warnings to note about strict infra/app separation\n\nFiles updated: CLAUDE.md, README.md, docs/getting-started.md,\ndocs/infra/deployment.md, docs/index.md, docs/contributing/localstack.md,\nsrc/zae_limiter/__init__.py, src/zae_limiter/limiter.py,\nexamples/basic_rate_limiting.py\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(llm-integration): reframe intro to reflect general-purpose design\n\nLLM APIs fit the library's pattern, not the other way around.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: rename failure-modes.md to unavailability.md\n\nClarifies that the document covers infrastructure unavailability\n(DynamoDB errors) rather than all failure scenarios. Changes include:\n\n- Rename file and update nav/cross-references\n- Add scope note distinguishing from RateLimitExceeded\n- Document which exceptions trigger failure mode logic\n- Document no-op lease behavior during FAIL_OPEN\n- Remove redundant circuit breaker example\n- Remove thin DynamoDB Resilience section\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: fix PR review comments for docs structure and internal API\n\n- Update CLAUDE.md docs structure: failure-modes.md → unavailability.md\n- Add auditing.md to infra/ section in CLAUDE.md\n- Fix Repository import in auditing.md to use internal path\n- Add warning admonition about Repository being internal API\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-13T23:04:00-05:00",
          "tree_id": "57f96644f3e53b9e0501d96bbb5f8d1db60b02b7",
          "url": "https://github.com/zeroae/zae-limiter/commit/06636b70112e1583250767bbfff84b13e2ae016e"
        },
        "date": 1768363809036,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 344.3870528201472,
            "unit": "iter/sec",
            "range": "stddev: 0.00017736747721924093",
            "extra": "mean: 2.903709625002193 msec\nrounds: 8"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 23.399973077345845,
            "unit": "iter/sec",
            "range": "stddev: 0.059935048323369315",
            "extra": "mean: 42.735091903508525 msec\nrounds: 228"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 23.465880140259056,
            "unit": "iter/sec",
            "range": "stddev: 0.125432336701346",
            "extra": "mean: 42.61506468211937 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1083.5978905092468,
            "unit": "iter/sec",
            "range": "stddev: 0.00006648101734277023",
            "extra": "mean: 922.8515566139031 usec\nrounds: 945"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 57.407494981182126,
            "unit": "iter/sec",
            "range": "stddev: 0.07417746632061674",
            "extra": "mean: 17.419328265896198 msec\nrounds: 173"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 77.48062841001428,
            "unit": "iter/sec",
            "range": "stddev: 0.022054824537144597",
            "extra": "mean: 12.906451851528239 msec\nrounds: 229"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 26.481174538060543,
            "unit": "iter/sec",
            "range": "stddev: 0.06326269046830466",
            "extra": "mean: 37.76267546451658 msec\nrounds: 155"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 55.19280918965997,
            "unit": "iter/sec",
            "range": "stddev: 0.05836324307156195",
            "extra": "mean: 18.118302269480132 msec\nrounds: 308"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 21.06968457104034,
            "unit": "iter/sec",
            "range": "stddev: 0.09030403291136732",
            "extra": "mean: 47.46155532743335 msec\nrounds: 226"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.10160304170958,
            "unit": "iter/sec",
            "range": "stddev: 0.24077892283400462",
            "extra": "mean: 163.89135660975654 msec\nrounds: 123"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 23.528456109557883,
            "unit": "iter/sec",
            "range": "stddev: 0.006657208595507799",
            "extra": "mean: 42.50172622222218 msec\nrounds: 9"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 22.415829026802918,
            "unit": "iter/sec",
            "range": "stddev: 0.00393396400268562",
            "extra": "mean: 44.6113324117652 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 37.04763052045076,
            "unit": "iter/sec",
            "range": "stddev: 0.0063745492012561725",
            "extra": "mean: 26.992279558823263 msec\nrounds: 34"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 32.262901045886345,
            "unit": "iter/sec",
            "range": "stddev: 0.004851804845552674",
            "extra": "mean: 30.995352791670427 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 25.10568408433579,
            "unit": "iter/sec",
            "range": "stddev: 0.0064399302408668915",
            "extra": "mean: 39.83161728000596 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 221.9618768619595,
            "unit": "iter/sec",
            "range": "stddev: 0.000697628095286381",
            "extra": "mean: 4.50527817721559 msec\nrounds: 158"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 39.7819494759077,
            "unit": "iter/sec",
            "range": "stddev: 0.1880491074241299",
            "extra": "mean: 25.137028556270444 msec\nrounds: 311"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 20.922511253253514,
            "unit": "iter/sec",
            "range": "stddev: 0.09075299550794437",
            "extra": "mean: 47.79540982894665 msec\nrounds: 228"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1085.830031529341,
            "unit": "iter/sec",
            "range": "stddev: 0.0002764627453382746",
            "extra": "mean: 920.9544504783557 usec\nrounds: 939"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 47.68727823859975,
            "unit": "iter/sec",
            "range": "stddev: 0.10143649252134411",
            "extra": "mean: 20.969953348911513 msec\nrounds: 321"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 59.32002194681708,
            "unit": "iter/sec",
            "range": "stddev: 0.0634487558427228",
            "extra": "mean: 16.857714599238392 msec\nrounds: 262"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 26.44931259564986,
            "unit": "iter/sec",
            "range": "stddev: 0.0741529507413895",
            "extra": "mean: 37.80816595454624 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 28.199904678253038,
            "unit": "iter/sec",
            "range": "stddev: 0.12516856593904085",
            "extra": "mean: 35.46111277358932 msec\nrounds: 53"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 17.632783633069366,
            "unit": "iter/sec",
            "range": "stddev: 0.008794449417067168",
            "extra": "mean: 56.71254299999191 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.199156485154802,
            "unit": "iter/sec",
            "range": "stddev: 0.15145938836434128",
            "extra": "mean: 192.33889244443958 msec\nrounds: 36"
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
          "id": "324f90b3eaca82a59d9ceb019d150d6e0f3d5c62",
          "message": "🐛 fix(ci): fix git-cliff regex to strip emoji prefixes (#118)\n\nThe original regex `^[^\\w\\s]+` didn't reliably match Unicode emojis\nin Rust's regex engine. Changed to `^[^a-zA-Z]*` which matches any\nnon-letter prefix including emojis.\n\nAlso:\n- Added `ci` commit type parser (was missing)\n- Added preprocessor for GitHub auto-revert format\n- Fixed `doc` -> `docs` in commit_parsers\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-14T09:26:25-05:00",
          "tree_id": "e29563779198323a412766c9db4098cc87522598",
          "url": "https://github.com/zeroae/zae-limiter/commit/324f90b3eaca82a59d9ceb019d150d6e0f3d5c62"
        },
        "date": 1768401140797,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 346.9691837006827,
            "unit": "iter/sec",
            "range": "stddev: 0.00017250642320246751",
            "extra": "mean: 2.8821003333329522 msec\nrounds: 9"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 24.935192542078536,
            "unit": "iter/sec",
            "range": "stddev: 0.05549358080722956",
            "extra": "mean: 40.10396143171881 msec\nrounds: 227"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 24.846524314074177,
            "unit": "iter/sec",
            "range": "stddev: 0.11304739462877901",
            "extra": "mean: 40.24707791558418 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1087.7535892878382,
            "unit": "iter/sec",
            "range": "stddev: 0.0000733000411314298",
            "extra": "mean: 919.3258563777379 usec\nrounds: 933"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 60.02401336613982,
            "unit": "iter/sec",
            "range": "stddev: 0.07940322037490365",
            "extra": "mean: 16.659998955753107 msec\nrounds: 113"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 68.52405255423315,
            "unit": "iter/sec",
            "range": "stddev: 0.024301603526109272",
            "extra": "mean: 14.59341592805757 msec\nrounds: 278"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 35.652372911257906,
            "unit": "iter/sec",
            "range": "stddev: 0.06412994414748231",
            "extra": "mean: 28.04862393000022 msec\nrounds: 100"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 59.29837430080497,
            "unit": "iter/sec",
            "range": "stddev: 0.03403711896729978",
            "extra": "mean: 16.863868728125063 msec\nrounds: 320"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 22.21502550838145,
            "unit": "iter/sec",
            "range": "stddev: 0.08413863823330213",
            "extra": "mean: 45.01457806666539 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.696218884117227,
            "unit": "iter/sec",
            "range": "stddev: 0.21897381089755466",
            "extra": "mean: 149.33800960000005 msec\nrounds: 120"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 23.582688867397618,
            "unit": "iter/sec",
            "range": "stddev: 0.009150280799157265",
            "extra": "mean: 42.40398563636528 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 22.27523535735247,
            "unit": "iter/sec",
            "range": "stddev: 0.0077958134861544634",
            "extra": "mean: 44.8929038888887 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 40.12272176415126,
            "unit": "iter/sec",
            "range": "stddev: 0.004358252548030116",
            "extra": "mean: 24.923533499999923 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 33.08867996669964,
            "unit": "iter/sec",
            "range": "stddev: 0.003756806688501879",
            "extra": "mean: 30.22181607142979 msec\nrounds: 28"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 26.968608647409607,
            "unit": "iter/sec",
            "range": "stddev: 0.005010840622529588",
            "extra": "mean: 37.08014799999896 msec\nrounds: 26"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 237.00946716915436,
            "unit": "iter/sec",
            "range": "stddev: 0.000536611695307957",
            "extra": "mean: 4.219240741494504 msec\nrounds: 147"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 42.15776466856403,
            "unit": "iter/sec",
            "range": "stddev: 0.16425083227527515",
            "extra": "mean: 23.720422746836828 msec\nrounds: 316"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.84629156000282,
            "unit": "iter/sec",
            "range": "stddev: 0.08702358388301636",
            "extra": "mean: 45.77435933478272 msec\nrounds: 230"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1117.6849965992562,
            "unit": "iter/sec",
            "range": "stddev: 0.000025459920475493778",
            "extra": "mean: 894.7064718974195 usec\nrounds: 943"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 53.470102983454446,
            "unit": "iter/sec",
            "range": "stddev: 0.09288196876421062",
            "extra": "mean: 18.702039910217408 msec\nrounds: 323"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 61.992720159114555,
            "unit": "iter/sec",
            "range": "stddev: 0.056624917222692056",
            "extra": "mean: 16.130926299625745 msec\nrounds: 267"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 28.25106196994259,
            "unit": "iter/sec",
            "range": "stddev: 0.06656299697344684",
            "extra": "mean: 35.39689945333521 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 31.341028833476653,
            "unit": "iter/sec",
            "range": "stddev: 0.09881123239214992",
            "extra": "mean: 31.907057209680957 msec\nrounds: 62"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 17.730233455443276,
            "unit": "iter/sec",
            "range": "stddev: 0.00932158793564445",
            "extra": "mean: 56.40083659998254 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 6.056657543165811,
            "unit": "iter/sec",
            "range": "stddev: 0.11750869618862396",
            "extra": "mean: 165.10756846874665 msec\nrounds: 32"
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
          "id": "bf5ad36495b2a3bd343760d6b594d776e9a7f22d",
          "message": "🔧 chore: update vscode git worktrees extension settings (#120)\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-14T13:22:04-05:00",
          "tree_id": "322bb6dde1a19643dd488718bac58cddf7936bb2",
          "url": "https://github.com/zeroae/zae-limiter/commit/bf5ad36495b2a3bd343760d6b594d776e9a7f22d"
        },
        "date": 1768415327344,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 346.4175531851167,
            "unit": "iter/sec",
            "range": "stddev: 0.00017873403092257076",
            "extra": "mean: 2.886689750001281 msec\nrounds: 8"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 24.825460178816325,
            "unit": "iter/sec",
            "range": "stddev: 0.056255961327794674",
            "extra": "mean: 40.28122712719358 msec\nrounds: 228"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 24.731473076640818,
            "unit": "iter/sec",
            "range": "stddev: 0.11827465419297195",
            "extra": "mean: 40.43430801315723 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1080.2916834809505,
            "unit": "iter/sec",
            "range": "stddev: 0.00003133373048366693",
            "extra": "mean: 925.6759218748847 usec\nrounds: 896"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 61.088089322383325,
            "unit": "iter/sec",
            "range": "stddev: 0.06638537086336178",
            "extra": "mean: 16.3698031988306 msec\nrounds: 171"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 72.03140159735561,
            "unit": "iter/sec",
            "range": "stddev: 0.023642478483884045",
            "extra": "mean: 13.882834122676735 msec\nrounds: 269"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 27.40081719031752,
            "unit": "iter/sec",
            "range": "stddev: 0.06655967409534373",
            "extra": "mean: 36.495261913333174 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 56.69100549644677,
            "unit": "iter/sec",
            "range": "stddev: 0.055283828116096445",
            "extra": "mean: 17.6394825112544 msec\nrounds: 311"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 21.84663644665112,
            "unit": "iter/sec",
            "range": "stddev: 0.08858918402216256",
            "extra": "mean: 45.773636707965196 msec\nrounds: 226"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.4217649598691136,
            "unit": "iter/sec",
            "range": "stddev: 0.23358061037314803",
            "extra": "mean: 155.72042985833315 msec\nrounds: 120"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 22.900049007563812,
            "unit": "iter/sec",
            "range": "stddev: 0.009566125344832638",
            "extra": "mean: 43.668028818178655 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.72019254114837,
            "unit": "iter/sec",
            "range": "stddev: 0.0077651401337125985",
            "extra": "mean: 48.262099785708706 msec\nrounds: 14"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 38.11767678638583,
            "unit": "iter/sec",
            "range": "stddev: 0.005011762595483263",
            "extra": "mean: 26.234547441179878 msec\nrounds: 34"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 30.979193778122625,
            "unit": "iter/sec",
            "range": "stddev: 0.004758069915024527",
            "extra": "mean: 32.27972965217048 msec\nrounds: 23"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 28.46448512876,
            "unit": "iter/sec",
            "range": "stddev: 0.0037595465499590326",
            "extra": "mean: 35.13149791666592 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 228.4826033030443,
            "unit": "iter/sec",
            "range": "stddev: 0.0006645482497963175",
            "extra": "mean: 4.376700832113969 msec\nrounds: 137"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 42.38074856913445,
            "unit": "iter/sec",
            "range": "stddev: 0.1713947728073623",
            "extra": "mean: 23.59561909032187 msec\nrounds: 310"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 22.347856385844256,
            "unit": "iter/sec",
            "range": "stddev: 0.08472421986840825",
            "extra": "mean: 44.74702104464155 msec\nrounds: 224"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1108.6718542561468,
            "unit": "iter/sec",
            "range": "stddev: 0.000026303470472867647",
            "extra": "mean: 901.980145126838 usec\nrounds: 944"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 52.78290551948301,
            "unit": "iter/sec",
            "range": "stddev: 0.09070929788421253",
            "extra": "mean: 18.94552772641294 msec\nrounds: 318"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 70.65936522939343,
            "unit": "iter/sec",
            "range": "stddev: 0.025384473878247833",
            "extra": "mean: 14.152405654275707 msec\nrounds: 269"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 28.626968203608516,
            "unit": "iter/sec",
            "range": "stddev: 0.06459705286099712",
            "extra": "mean: 34.932095948391314 msec\nrounds: 155"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 36.635829774951524,
            "unit": "iter/sec",
            "range": "stddev: 0.025929291812914448",
            "extra": "mean: 27.2956831097549 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.334910841458176,
            "unit": "iter/sec",
            "range": "stddev: 0.13243558462496088",
            "extra": "mean: 187.44455712903212 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.739809287217841,
            "unit": "iter/sec",
            "range": "stddev: 0.1940404688580001",
            "extra": "mean: 174.22181643333184 msec\nrounds: 30"
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
          "id": "08583b03f7610ce9cd09b8dcece4049d34be87f9",
          "message": "✨ Add org-wide Claude Code instructions and issue templates (#138)\n\n* ✨ feat: add zeroae org-wide Claude Code instructions as submodule\n\nAdds .claude/rules/zeroae submodule pointing to zeroae/.claude repo\nwith organization-wide conventions for commits, changelogs, and\ndevelopment principles.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: add issue templates and release planning\n\n.github/ISSUE_TEMPLATE/:\n- bug.yml, feature.yml, task.yml, release-epic.yml\n- config.yml (disable blank issues)\n\nCLAUDE.md:\n- Add release planning with milestones (v0.1.0 - v1.0.0)\n- Add project scopes for commits and area labels\n- Reference org conventions in submodule\n\n.claude/rules/zeroae:\n- Update submodule (adds github.md)\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore: update submodule with capitalized title convention\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat: Add SessionStart hook to auto-init submodules\n\n- Add Claude Code hook to detect empty submodule and run git submodule update\n- Update setup docs with --recurse-submodules clone option\n- Remove user-specific permissions from settings.json (use settings.local.json)\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore: add Claude Code to recommended VS Code extensions\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-14T23:25:12-05:00",
          "tree_id": "9d6349888836d9dc6d242b5ed87bc9618243fae2",
          "url": "https://github.com/zeroae/zae-limiter/commit/08583b03f7610ce9cd09b8dcece4049d34be87f9"
        },
        "date": 1768451499376,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 324.7994620457535,
            "unit": "iter/sec",
            "range": "stddev: 0.00028720617132710347",
            "extra": "mean: 3.078822833330719 msec\nrounds: 6"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 22.74688426942193,
            "unit": "iter/sec",
            "range": "stddev: 0.0587522939805479",
            "extra": "mean: 43.96206478899069 msec\nrounds: 218"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 25.202317209745907,
            "unit": "iter/sec",
            "range": "stddev: 0.11453409349368344",
            "extra": "mean: 39.678891098684105 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1077.227690974528,
            "unit": "iter/sec",
            "range": "stddev: 0.000026810339091980232",
            "extra": "mean: 928.3088509313542 usec\nrounds: 644"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 56.08611695607958,
            "unit": "iter/sec",
            "range": "stddev: 0.06933653788925966",
            "extra": "mean: 17.82972425748584 msec\nrounds: 167"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 63.85317345115694,
            "unit": "iter/sec",
            "range": "stddev: 0.03132004143212026",
            "extra": "mean: 15.660928751879931 msec\nrounds: 266"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 26.209552073022728,
            "unit": "iter/sec",
            "range": "stddev: 0.07006947338208343",
            "extra": "mean: 38.15402862337704 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 168.53909976020265,
            "unit": "iter/sec",
            "range": "stddev: 0.00203591419172372",
            "extra": "mean: 5.933341292452609 msec\nrounds: 106"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 22.836965571877133,
            "unit": "iter/sec",
            "range": "stddev: 0.06736626619978374",
            "extra": "mean: 43.78865470776303 msec\nrounds: 219"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 7.305689543126712,
            "unit": "iter/sec",
            "range": "stddev: 0.19160965578001646",
            "extra": "mean: 136.87961883636473 msec\nrounds: 110"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.778102396363234,
            "unit": "iter/sec",
            "range": "stddev: 0.012522688753866619",
            "extra": "mean: 45.91768290000289 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.73018081734551,
            "unit": "iter/sec",
            "range": "stddev: 0.007001195325656187",
            "extra": "mean: 48.23884599999594 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 39.941187070313575,
            "unit": "iter/sec",
            "range": "stddev: 0.002892282300468718",
            "extra": "mean: 25.036812206897416 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 28.174709756847626,
            "unit": "iter/sec",
            "range": "stddev: 0.0033372449021288942",
            "extra": "mean: 35.49282347999906 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 25.181685810661616,
            "unit": "iter/sec",
            "range": "stddev: 0.005224296517363136",
            "extra": "mean: 39.71140008333407 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 214.54313895431778,
            "unit": "iter/sec",
            "range": "stddev: 0.000773530851701782",
            "extra": "mean: 4.661067256095884 msec\nrounds: 164"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 44.5552606703888,
            "unit": "iter/sec",
            "range": "stddev: 0.14907400606913876",
            "extra": "mean: 22.444038817274723 msec\nrounds: 301"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.811761162990326,
            "unit": "iter/sec",
            "range": "stddev: 0.08249772665730601",
            "extra": "mean: 45.84682513839259 msec\nrounds: 224"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1063.8223536338246,
            "unit": "iter/sec",
            "range": "stddev: 0.0002617795052644046",
            "extra": "mean: 940.0065683750497 usec\nrounds: 936"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 49.156922708276,
            "unit": "iter/sec",
            "range": "stddev: 0.0937313206749821",
            "extra": "mean: 20.343014674342935 msec\nrounds: 304"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 57.937679765907475,
            "unit": "iter/sec",
            "range": "stddev: 0.056549243535975235",
            "extra": "mean: 17.259924871696956 msec\nrounds: 265"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 28.89564621846715,
            "unit": "iter/sec",
            "range": "stddev: 0.06639487440564866",
            "extra": "mean: 34.60728970861022 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 36.70199579984552,
            "unit": "iter/sec",
            "range": "stddev: 0.026091459536529575",
            "extra": "mean: 27.24647470000008 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 6.183283205860088,
            "unit": "iter/sec",
            "range": "stddev: 0.10519234271090103",
            "extra": "mean: 161.72637848000704 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 21.939692515473652,
            "unit": "iter/sec",
            "range": "stddev: 0.009173618529745353",
            "extra": "mean: 45.57949019999796 msec\nrounds: 5"
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
          "id": "68a738157ac8a01ed06b32f0f56b885166843b20",
          "message": "📝 docs: add conda-forge installation instructions (#140)\n\nAdd conda-forge badge and installation instructions to documentation:\n- README.md: conda-forge badge and install option\n- docs/getting-started.md: conda tab in installation section\n- docs/cli.md: conda install option\n\nCloses #119\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-15T01:18:36-05:00",
          "tree_id": "4cad1dfd5a6703aa305552abc41c8b83e8260520",
          "url": "https://github.com/zeroae/zae-limiter/commit/68a738157ac8a01ed06b32f0f56b885166843b20"
        },
        "date": 1768458292647,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 341.81964072252117,
            "unit": "iter/sec",
            "range": "stddev: 0.0002226284526180622",
            "extra": "mean: 2.9255194285683825 msec\nrounds: 7"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 22.598191342436635,
            "unit": "iter/sec",
            "range": "stddev: 0.06109613893553231",
            "extra": "mean: 44.25132900446428 msec\nrounds: 224"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 28.038758489786577,
            "unit": "iter/sec",
            "range": "stddev: 0.15943059152122258",
            "extra": "mean: 35.66491720253095 msec\nrounds: 79"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1092.7263839119753,
            "unit": "iter/sec",
            "range": "stddev: 0.00004824981320352398",
            "extra": "mean: 915.1421753174718 usec\nrounds: 867"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 73.72341757009112,
            "unit": "iter/sec",
            "range": "stddev: 0.018614362998863695",
            "extra": "mean: 13.564211114457212 msec\nrounds: 166"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 59.381554424637606,
            "unit": "iter/sec",
            "range": "stddev: 0.034552268860896324",
            "extra": "mean: 16.840246263157717 msec\nrounds: 266"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 33.55322923905142,
            "unit": "iter/sec",
            "range": "stddev: 0.08486779557258138",
            "extra": "mean: 29.803390692307357 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 57.53997076419375,
            "unit": "iter/sec",
            "range": "stddev: 0.03384925654661904",
            "extra": "mean: 17.379223289808916 msec\nrounds: 314"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 20.229937495421986,
            "unit": "iter/sec",
            "range": "stddev: 0.0839246518277007",
            "extra": "mean: 49.43169004977395 msec\nrounds: 221"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 7.447252577663275,
            "unit": "iter/sec",
            "range": "stddev: 0.136567342909337",
            "extra": "mean: 134.27770705660288 msec\nrounds: 106"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 19.945022034304394,
            "unit": "iter/sec",
            "range": "stddev: 0.005550974060308747",
            "extra": "mean: 50.13782377778537 msec\nrounds: 9"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 18.89376130127119,
            "unit": "iter/sec",
            "range": "stddev: 0.006295356278167816",
            "extra": "mean: 52.92752375000731 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 34.26648708176364,
            "unit": "iter/sec",
            "range": "stddev: 0.005025684688722388",
            "extra": "mean: 29.183032320000848 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 27.242299605326966,
            "unit": "iter/sec",
            "range": "stddev: 0.004592791025832557",
            "extra": "mean: 36.70762066666574 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 23.802498105496845,
            "unit": "iter/sec",
            "range": "stddev: 0.0032777708884627233",
            "extra": "mean: 42.01239700000499 msec\nrounds: 21"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 206.98703985577293,
            "unit": "iter/sec",
            "range": "stddev: 0.0009179972524987868",
            "extra": "mean: 4.831220354167066 msec\nrounds: 144"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 59.58150462248793,
            "unit": "iter/sec",
            "range": "stddev: 0.030565160169250174",
            "extra": "mean: 16.78373190365133 msec\nrounds: 301"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 20.500779126915027,
            "unit": "iter/sec",
            "range": "stddev: 0.08765640784160278",
            "extra": "mean: 48.77863391480188 msec\nrounds: 223"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1082.9782748246503,
            "unit": "iter/sec",
            "range": "stddev: 0.00023375417206619037",
            "extra": "mean: 923.3795573248359 usec\nrounds: 942"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 57.748353345603725,
            "unit": "iter/sec",
            "range": "stddev: 0.030974584122918064",
            "extra": "mean: 17.316511070287135 msec\nrounds: 313"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 52.610124561180506,
            "unit": "iter/sec",
            "range": "stddev: 0.06330217173075171",
            "extra": "mean: 19.007748191835137 msec\nrounds: 245"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 137.69645669844323,
            "unit": "iter/sec",
            "range": "stddev: 0.0003761862040854996",
            "extra": "mean: 7.262350999997125 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 32.306737364648534,
            "unit": "iter/sec",
            "range": "stddev: 0.03181567639399049",
            "extra": "mean: 30.953295862498464 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 4.657735796932211,
            "unit": "iter/sec",
            "range": "stddev: 0.14656944211707787",
            "extra": "mean: 214.69659156250208 msec\nrounds: 32"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.3064856887055205,
            "unit": "iter/sec",
            "range": "stddev: 0.22391415412035495",
            "extra": "mean: 232.20790042857158 msec\nrounds: 35"
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
          "id": "4ac9808e5a7e35e7443cdf52279a063e98cc6756",
          "message": "✨ Add is_available() health check method (#142)\n\n* ✨ feat(limiter): add is_available() health check method\n\nAdd is_available() method to RateLimiter and SyncRateLimiter for\noperational health checks. This enables applications to verify\nDynamoDB connectivity before operations.\n\n- Add Repository.ping() for lightweight DynamoDB connectivity check\n- Add RateLimiter.is_available() with configurable timeout (default 3s)\n- Add SyncRateLimiter.is_available() sync wrapper\n- Returns False on any error (no exceptions thrown)\n- Works without requiring initialization\n\nCloses #127\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(guide): update health checks to use is_available()\n\nUpdate the unavailability guide to document the new is_available()\nmethod for health checks instead of the try/except workaround.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore(limiter): change is_available() default timeout to 1s\n\nUpdate default timeout from 3.0s to 1.0s to meet the acceptance\ncriteria of \"fast execution (< 1s timeout)\".\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(limiter): add coverage for SyncRateLimiter exception handler\n\nAdd test for edge case where event loop fails during is_available().\nThis covers the exception handler in SyncRateLimiter.is_available()\nthat was previously uncovered.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore: update .claude/rules/zeroae submodule\n\nUpdate to include PR metadata inheritance guidance.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-15T05:26:30-05:00",
          "tree_id": "f68a0ef95f8f57edb817aefb1e951cc011111302",
          "url": "https://github.com/zeroae/zae-limiter/commit/4ac9808e5a7e35e7443cdf52279a063e98cc6756"
        },
        "date": 1768473183345,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 345.14337752006566,
            "unit": "iter/sec",
            "range": "stddev: 0.00017071884457386694",
            "extra": "mean: 2.897346625003294 msec\nrounds: 8"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 24.069257635170427,
            "unit": "iter/sec",
            "range": "stddev: 0.057795026536143886",
            "extra": "mean: 41.54677369603549 msec\nrounds: 227"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 24.08563986452771,
            "unit": "iter/sec",
            "range": "stddev: 0.12095292773814408",
            "extra": "mean: 41.51851500000035 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1083.0071999183103,
            "unit": "iter/sec",
            "range": "stddev: 0.0000548621504125426",
            "extra": "mean: 923.3548955865008 usec\nrounds: 929"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 61.11128028351067,
            "unit": "iter/sec",
            "range": "stddev: 0.0713608884449955",
            "extra": "mean: 16.36359106470601 msec\nrounds: 170"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 96.16068250031172,
            "unit": "iter/sec",
            "range": "stddev: 0.028090798139672815",
            "extra": "mean: 10.399260633333776 msec\nrounds: 120"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 29.31963219581511,
            "unit": "iter/sec",
            "range": "stddev: 0.043325935154677704",
            "extra": "mean: 34.10683985806389 msec\nrounds: 155"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 55.370183155587384,
            "unit": "iter/sec",
            "range": "stddev: 0.05407751126772366",
            "extra": "mean: 18.06026173310735 msec\nrounds: 296"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 22.744340815791194,
            "unit": "iter/sec",
            "range": "stddev: 0.08031126999381408",
            "extra": "mean: 43.96698097777839 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.413072489728777,
            "unit": "iter/sec",
            "range": "stddev: 0.2398723561826013",
            "extra": "mean: 155.93149798347162 msec\nrounds: 121"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 23.104286711597965,
            "unit": "iter/sec",
            "range": "stddev: 0.00589801698680643",
            "extra": "mean: 43.282011363632215 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 22.557958411873983,
            "unit": "iter/sec",
            "range": "stddev: 0.008083660104152292",
            "extra": "mean: 44.330252842102205 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 41.153134703206604,
            "unit": "iter/sec",
            "range": "stddev: 0.004024301325564872",
            "extra": "mean: 24.29948549999719 msec\nrounds: 38"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 32.24243437601234,
            "unit": "iter/sec",
            "range": "stddev: 0.004365298104785161",
            "extra": "mean: 31.015027846159718 msec\nrounds: 26"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 22.690298484800017,
            "unit": "iter/sec",
            "range": "stddev: 0.010423343406909475",
            "extra": "mean: 44.071698777778934 msec\nrounds: 27"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 216.29003058791602,
            "unit": "iter/sec",
            "range": "stddev: 0.0008186645660315156",
            "extra": "mean: 4.623421603306525 msec\nrounds: 121"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 40.51053894669627,
            "unit": "iter/sec",
            "range": "stddev: 0.18108180075345875",
            "extra": "mean: 24.68493448867217 msec\nrounds: 309"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 22.04357063707315,
            "unit": "iter/sec",
            "range": "stddev: 0.073914158052983",
            "extra": "mean: 45.36470141176619 msec\nrounds: 221"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1103.475400026978,
            "unit": "iter/sec",
            "range": "stddev: 0.00004867315940551777",
            "extra": "mean: 906.227723767609 usec\nrounds: 934"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 50.117648134994745,
            "unit": "iter/sec",
            "range": "stddev: 0.09593190724783639",
            "extra": "mean: 19.95305121474262 msec\nrounds: 312"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 58.13256258471927,
            "unit": "iter/sec",
            "range": "stddev: 0.05751852645619582",
            "extra": "mean: 17.202062932330808 msec\nrounds: 266"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 31.792068767914262,
            "unit": "iter/sec",
            "range": "stddev: 0.0400765159727744",
            "extra": "mean: 31.454385913043733 msec\nrounds: 138"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 73.48700651983847,
            "unit": "iter/sec",
            "range": "stddev: 0.0005367152228080772",
            "extra": "mean: 13.607847800005857 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.47626963406985,
            "unit": "iter/sec",
            "range": "stddev: 0.12384704381292654",
            "extra": "mean: 182.60605609677052 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.029011200321,
            "unit": "iter/sec",
            "range": "stddev: 0.18626018093138205",
            "extra": "mean: 198.84624634285373 msec\nrounds: 35"
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
          "id": "d11c28fd6c20757d362f6e6f6a1919a280d906b5",
          "message": "✨ Add get_status() method for comprehensive status reporting (#141)\n\n* ✨ feat(limiter): add get_stack_status() method for API/CLI parity\n\nAdd `get_stack_status()` method to both `RateLimiter` and `SyncRateLimiter`\nclasses to provide programmatic access to CloudFormation stack status,\nmatching the functionality of the `zae-limiter status` CLI command.\n\nThis change:\n- Adds async `get_stack_status()` to `RateLimiter` returning stack status\n  string or None if stack doesn't exist\n- Adds sync `get_stack_status()` to `SyncRateLimiter` wrapping the async\n  implementation\n- Adds unit tests for both methods covering various CloudFormation states\n- Updates docs/getting-started.md with tabbed Programmatic/CLI options\n  for checking status\n- Updates docs/infra/deployment.md with the new programmatic option\n\nCloses #115\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🎨 style(test): fix formatting in test_limiter.py\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ♻️ refactor(limiter): rename get_stack_status() to stack_status\n\nAddress review feedback:\n- Rename `get_stack_status()` to `stack_status()` on RateLimiter (async method)\n- Rename `get_stack_status()` to `stack_status` property on SyncRateLimiter\n- Add `*.local.md` to .gitignore\n- Remove accidentally committed ralph-loop.local.md file\n- Update tests and documentation accordingly\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat(limiter): add get_status() method for comprehensive status reporting\n\nAdd Status dataclass and get_status() method to provide unified status\ninformation across API and CLI:\n\n- Status dataclass with connectivity, infrastructure, identity, versions,\n  and table metrics fields\n- Async get_status() in RateLimiter\n- Sync get_status() in SyncRateLimiter\n- Enhanced CLI status command with rich formatted output\n\nCloses #115\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(e2e): add get_status() integration tests with LocalStack\n\nAdd integration tests for RateLimiter.get_status() and\nSyncRateLimiter.get_status() to verify comprehensive status\nreporting works against real LocalStack infrastructure.\n\nTests verify:\n- Connectivity: available flag and latency measurement\n- Infrastructure: table status reporting\n- Identity: ZAEL-prefixed name and region\n- Versions: client version populated\n- Metrics: table item count available\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ♻️ refactor(cli): make status command read-only\n\n- Remove stack_status() method from RateLimiter and SyncRateLimiter\n  (superseded by get_status() which is more comprehensive)\n- Refactor CLI status command to use StackManager and Repository\n  directly instead of RateLimiter, making it truly read-only\n- Update CLI tests to mock Repository and StackManager\n- Remove stack_status tests from unit tests\n- Enhance e2e CLI output tests to verify all output sections\n\nThe CLI status command is now a read-only operation that won't\ntrigger any infrastructure upgrades or modifications.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat(cli): initialize version record during deploy\n\nDeploy command now creates the version record in DynamoDB after\nstack creation, so `zae-limiter status` shows schema/lambda versions\nimmediately instead of N/A.\n\n- Add version record initialization step to deploy command\n- Update docs to use get_status() instead of removed stack_status()\n- Update CLI status output example in docs\n- Add e2e test verifying schema version after deploy\n- Update unit tests to mock Repository for version record\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-15T05:35:51-05:00",
          "tree_id": "e8625477e759ad4e40cabf2d215be584df68126a",
          "url": "https://github.com/zeroae/zae-limiter/commit/d11c28fd6c20757d362f6e6f6a1919a280d906b5"
        },
        "date": 1768473748446,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 342.30611477380654,
            "unit": "iter/sec",
            "range": "stddev: 0.00016313726684753348",
            "extra": "mean: 2.921361777778328 msec\nrounds: 9"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 23.78719406460561,
            "unit": "iter/sec",
            "range": "stddev: 0.057893369581074376",
            "extra": "mean: 42.039426646287794 msec\nrounds: 229"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 24.721121964486006,
            "unit": "iter/sec",
            "range": "stddev: 0.12143925427699065",
            "extra": "mean: 40.45123847682096 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1082.4820885386396,
            "unit": "iter/sec",
            "range": "stddev: 0.0002997216313723586",
            "extra": "mean: 923.8028144650494 usec\nrounds: 954"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 58.02261492137649,
            "unit": "iter/sec",
            "range": "stddev: 0.06748038609632546",
            "extra": "mean: 17.234659302326335 msec\nrounds: 172"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 69.4536158944586,
            "unit": "iter/sec",
            "range": "stddev: 0.02310246378161336",
            "extra": "mean: 14.398098459259423 msec\nrounds: 270"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 30.16984385124605,
            "unit": "iter/sec",
            "range": "stddev: 0.04531326173492345",
            "extra": "mean: 33.14568033333387 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 58.14670396135864,
            "unit": "iter/sec",
            "range": "stddev: 0.054523338756598885",
            "extra": "mean: 17.197879361563633 msec\nrounds: 307"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 20.912613149586743,
            "unit": "iter/sec",
            "range": "stddev: 0.08997120827970367",
            "extra": "mean: 47.81803177092486 msec\nrounds: 227"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.48259127893426,
            "unit": "iter/sec",
            "range": "stddev: 0.22895834081470323",
            "extra": "mean: 154.2593010991741 msec\nrounds: 121"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 22.06972722419641,
            "unit": "iter/sec",
            "range": "stddev: 0.008238799150942189",
            "extra": "mean: 45.310936100000276 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.689911261296988,
            "unit": "iter/sec",
            "range": "stddev: 0.005871255225762972",
            "extra": "mean: 46.10438410526734 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 38.15421781952996,
            "unit": "iter/sec",
            "range": "stddev: 0.005898210973786602",
            "extra": "mean: 26.209422107144626 msec\nrounds: 28"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 31.793509173988802,
            "unit": "iter/sec",
            "range": "stddev: 0.003352883777371778",
            "extra": "mean: 31.452960870960702 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 29.592375714777813,
            "unit": "iter/sec",
            "range": "stddev: 0.0040294549671538804",
            "extra": "mean: 33.79248795832979 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 222.42217084344807,
            "unit": "iter/sec",
            "range": "stddev: 0.0006082713158767389",
            "extra": "mean: 4.495954680272635 msec\nrounds: 147"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 66.98895039359228,
            "unit": "iter/sec",
            "range": "stddev: 0.026336492525134227",
            "extra": "mean: 14.92783502539627 msec\nrounds: 315"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 23.495127182910636,
            "unit": "iter/sec",
            "range": "stddev: 0.06980846659722434",
            "extra": "mean: 42.562016890351536 msec\nrounds: 228"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1105.1186644621819,
            "unit": "iter/sec",
            "range": "stddev: 0.00005458715477306734",
            "extra": "mean: 904.880201699119 usec\nrounds: 942"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 50.50944709465569,
            "unit": "iter/sec",
            "range": "stddev: 0.09641865451370689",
            "extra": "mean: 19.79827651104516 msec\nrounds: 317"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 57.6295054649437,
            "unit": "iter/sec",
            "range": "stddev: 0.05840713405387388",
            "extra": "mean: 17.352222475834097 msec\nrounds: 269"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 27.534136468384556,
            "unit": "iter/sec",
            "range": "stddev: 0.07477822238093537",
            "extra": "mean: 36.31855319480337 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 29.04620451162432,
            "unit": "iter/sec",
            "range": "stddev: 0.10238386250429035",
            "extra": "mean: 34.427906048785104 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 17.62907469926835,
            "unit": "iter/sec",
            "range": "stddev: 0.008976449135918161",
            "extra": "mean: 56.72447459999148 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.352336590303699,
            "unit": "iter/sec",
            "range": "stddev: 0.1481092193344746",
            "extra": "mean: 186.83428875000155 msec\nrounds: 36"
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
          "id": "edd4476d4344a700a5ee442ceecdd0bc090b0ae6",
          "message": "📝 docs: update getting-started.md to use get_status() method (#146)\n\nReplace deprecated stack_status() method with the new get_status() method\nthat returns a Status dataclass with comprehensive infrastructure information.\n\nCloses #121\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-15T11:11:10-05:00",
          "tree_id": "b85623ff9103a17b7c462ba34aac60ed9de59d7e",
          "url": "https://github.com/zeroae/zae-limiter/commit/edd4476d4344a700a5ee442ceecdd0bc090b0ae6"
        },
        "date": 1768493844315,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 347.27901681037906,
            "unit": "iter/sec",
            "range": "stddev: 0.0001602583136673404",
            "extra": "mean: 2.8795290000087133 msec\nrounds: 9"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 23.889517670661746,
            "unit": "iter/sec",
            "range": "stddev: 0.05484361564937424",
            "extra": "mean: 41.85936333189684 msec\nrounds: 232"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 31.21691453201608,
            "unit": "iter/sec",
            "range": "stddev: 0.04326203902835981",
            "extra": "mean: 32.03391542666395 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1081.7965745465237,
            "unit": "iter/sec",
            "range": "stddev: 0.00026347521735226406",
            "extra": "mean: 924.3882108049639 usec\nrounds: 944"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 59.10412577851349,
            "unit": "iter/sec",
            "range": "stddev: 0.06532277701193784",
            "extra": "mean: 16.9192926352958 msec\nrounds: 170"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 64.31803007721318,
            "unit": "iter/sec",
            "range": "stddev: 0.030577575899405917",
            "extra": "mean: 15.547739860805896 msec\nrounds: 273"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 27.53423200095888,
            "unit": "iter/sec",
            "range": "stddev: 0.06863530180251276",
            "extra": "mean: 36.318427184211096 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 56.61709686969365,
            "unit": "iter/sec",
            "range": "stddev: 0.05480950771888103",
            "extra": "mean: 17.662509300000618 msec\nrounds: 310"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 21.482362076749144,
            "unit": "iter/sec",
            "range": "stddev: 0.08593590261800015",
            "extra": "mean: 46.549815910715104 msec\nrounds: 224"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.2243658508040145,
            "unit": "iter/sec",
            "range": "stddev: 0.22348972585947185",
            "extra": "mean: 160.65893682499848 msec\nrounds: 120"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 22.176110204265576,
            "unit": "iter/sec",
            "range": "stddev: 0.006443214624636315",
            "extra": "mean: 45.09357100000567 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.650394830049265,
            "unit": "iter/sec",
            "range": "stddev: 0.004784002662832985",
            "extra": "mean: 46.18853410525653 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 39.6953881615742,
            "unit": "iter/sec",
            "range": "stddev: 0.004512546657403411",
            "extra": "mean: 25.191843342850007 msec\nrounds: 35"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 32.224804359991616,
            "unit": "iter/sec",
            "range": "stddev: 0.0032084080673095727",
            "extra": "mean: 31.031995999998685 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 23.83034143313644,
            "unit": "iter/sec",
            "range": "stddev: 0.010323115645867988",
            "extra": "mean: 41.9633097916711 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 232.80420302359724,
            "unit": "iter/sec",
            "range": "stddev: 0.0006212254953887537",
            "extra": "mean: 4.29545509493503 msec\nrounds: 158"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 40.90139158864343,
            "unit": "iter/sec",
            "range": "stddev: 0.16748491413184494",
            "extra": "mean: 24.449045892063424 msec\nrounds: 315"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.42044199652209,
            "unit": "iter/sec",
            "range": "stddev: 0.0851499123639266",
            "extra": "mean: 46.68437748214365 msec\nrounds: 224"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1092.6470225380083,
            "unit": "iter/sec",
            "range": "stddev: 0.000044824466413907974",
            "extra": "mean: 915.2086441211297 usec\nrounds: 961"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 50.64329119787076,
            "unit": "iter/sec",
            "range": "stddev: 0.09077588042053356",
            "extra": "mean: 19.745952056963546 msec\nrounds: 316"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 56.21612690247631,
            "unit": "iter/sec",
            "range": "stddev: 0.05440526191903459",
            "extra": "mean: 17.788489799996345 msec\nrounds: 265"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 28.03076978180375,
            "unit": "iter/sec",
            "range": "stddev: 0.06908877340739324",
            "extra": "mean: 35.67508162580511 msec\nrounds: 155"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 26.57694335214852,
            "unit": "iter/sec",
            "range": "stddev: 0.09754410771852849",
            "extra": "mean: 37.62659937035832 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.205804584893633,
            "unit": "iter/sec",
            "range": "stddev: 0.13467084941448176",
            "extra": "mean: 192.0932650645073 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.389303048868246,
            "unit": "iter/sec",
            "range": "stddev: 0.20208476921786797",
            "extra": "mean: 185.55274975861676 msec\nrounds: 29"
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
          "id": "f1359ca4bfbde98fc668d0b38ba11803cbd95e2c",
          "message": "feat(limiter): expose audit API and principal tracking (#153)\n\n* ✨ feat(limiter): expose audit API and principal tracking (#114)\n\nAdd audit event retrieval and principal tracking to public interfaces:\n\n- Add get_audit_events() to RateLimiter and SyncRateLimiter\n- Add principal parameter to create_entity, delete_entity, set_limits, delete_limits\n- Auto-detect AWS caller identity (ARN) when principal is None\n- Add CLI command: zae-limiter audit list\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore: add docs-parity and api-cli-parity rules\n\nAdd Claude Code agent configurations:\n- docs-updater agent for documentation synchronization\n- api-cli-parity agent for interface consistency checks\n- Rules for when to invoke these agents\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: add audit to CLI command list in CLAUDE.md\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(repository): add coverage for STS failure handling\n\nTest that _get_caller_identity_arn() handles STS exceptions gracefully.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* chore: update zeroae rules submodule\n\nUpdates PR title convention documentation.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* test(cli): add unit tests for audit list command\n\nImproves patch coverage by testing:\n- No events found path\n- Events display with table format\n- Long principal truncation\n- Pagination hint when limit reached\n- Custom limit and start-event-id options\n- Endpoint URL handling\n- Exception handling\n- None resource/principal handling\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* style(test): format test_cli.py\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-16T01:31:16-05:00",
          "tree_id": "aed52d0693a631ae524b1a5fd057a9286379d01a",
          "url": "https://github.com/zeroae/zae-limiter/commit/f1359ca4bfbde98fc668d0b38ba11803cbd95e2c"
        },
        "date": 1768545469957,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 78.93270579214644,
            "unit": "iter/sec",
            "range": "stddev: 0.0183945164119518",
            "extra": "mean: 12.66901964102562 msec\nrounds: 234"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 21.899692416473467,
            "unit": "iter/sec",
            "range": "stddev: 0.0666814504187232",
            "extra": "mean: 45.662741785714594 msec\nrounds: 224"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 24.17062594397186,
            "unit": "iter/sec",
            "range": "stddev: 0.12611750185815493",
            "extra": "mean: 41.3725321933336 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1109.046353987052,
            "unit": "iter/sec",
            "range": "stddev: 0.00003142372322615674",
            "extra": "mean: 901.6755669453964 usec\nrounds: 956"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 61.11304537917682,
            "unit": "iter/sec",
            "range": "stddev: 0.06891594916463943",
            "extra": "mean: 16.363118443787 msec\nrounds: 169"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 66.6851645528801,
            "unit": "iter/sec",
            "range": "stddev: 0.02500279783859699",
            "extra": "mean: 14.995839130111444 msec\nrounds: 269"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 34.42733954721234,
            "unit": "iter/sec",
            "range": "stddev: 0.07847232820179717",
            "extra": "mean: 29.04668246666688 msec\nrounds: 75"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 60.44459200111965,
            "unit": "iter/sec",
            "range": "stddev: 0.03184649443942226",
            "extra": "mean: 16.544077259740234 msec\nrounds: 308"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 33.481117081553414,
            "unit": "iter/sec",
            "range": "stddev: 0.07714692111663274",
            "extra": "mean: 29.86758170476202 msec\nrounds: 105"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.820442960619334,
            "unit": "iter/sec",
            "range": "stddev: 0.15952203876014803",
            "extra": "mean: 146.61804310569212 msec\nrounds: 123"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 20.04681299696042,
            "unit": "iter/sec",
            "range": "stddev: 0.011366680273047013",
            "extra": "mean: 49.88324080000268 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.992846396160314,
            "unit": "iter/sec",
            "range": "stddev: 0.005672266571173639",
            "extra": "mean: 47.63527447058844 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 35.541555935708935,
            "unit": "iter/sec",
            "range": "stddev: 0.004284847001952166",
            "extra": "mean: 28.136078279997037 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 32.294650361566916,
            "unit": "iter/sec",
            "range": "stddev: 0.003676394404869819",
            "extra": "mean: 30.96488083333071 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 24.225306494037394,
            "unit": "iter/sec",
            "range": "stddev: 0.004380337312193525",
            "extra": "mean: 41.2791474999968 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 207.27378096184938,
            "unit": "iter/sec",
            "range": "stddev: 0.0009321996847212721",
            "extra": "mean: 4.8245368775516235 msec\nrounds: 147"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 58.21369157488414,
            "unit": "iter/sec",
            "range": "stddev: 0.032614830710328786",
            "extra": "mean: 17.178089431309015 msec\nrounds: 313"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 20.272096382112764,
            "unit": "iter/sec",
            "range": "stddev: 0.09048311453056145",
            "extra": "mean: 49.32888938325873 msec\nrounds: 227"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1094.010052412414,
            "unit": "iter/sec",
            "range": "stddev: 0.00007343931504622157",
            "extra": "mean: 914.0683833707822 usec\nrounds: 866"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 49.410890774421,
            "unit": "iter/sec",
            "range": "stddev: 0.09608877910187691",
            "extra": "mean: 20.23845318971014 msec\nrounds: 311"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 54.41224127558957,
            "unit": "iter/sec",
            "range": "stddev: 0.05515613422130581",
            "extra": "mean: 18.378217411320275 msec\nrounds: 265"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 27.484146008219444,
            "unit": "iter/sec",
            "range": "stddev: 0.048733457741176996",
            "extra": "mean: 36.38461241258647 msec\nrounds: 143"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 27.68345685673553,
            "unit": "iter/sec",
            "range": "stddev: 0.09884043923538872",
            "extra": "mean: 36.122656399997055 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 4.924679234896321,
            "unit": "iter/sec",
            "range": "stddev: 0.12471486362256147",
            "extra": "mean: 203.05891049999582 msec\nrounds: 32"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.3162860082789996,
            "unit": "iter/sec",
            "range": "stddev: 0.22922838366794387",
            "extra": "mean: 188.1012418148139 msec\nrounds: 27"
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
          "id": "f7456983c48bff0aa08c548c7f3b1a2dcb66a1ec",
          "message": "🐛 fix(aggregator): use flat schema for snapshots to fix overlapping paths error (#172)\n\nDynamoDB rejects UpdateExpressions that SET a map path AND ADD to paths\nwithin it in the same expression with \"overlapping document paths\" error.\n\nThis fix changes usage snapshots from nested `data` map to flat top-level\nattributes, enabling atomic upsert with ADD counters in a single call.\n\nOther record types (entities, buckets, audit) continue to use nested\n`data` maps since they use PUT-then-UPDATE pattern (separate operations).\n\nChanges:\n- Flatten snapshot schema: resource, window, counters at top-level\n- Use if_not_exists() for metadata fields (set once on creation)\n- Use if_not_exists() for TTL (prevent extending on every update)\n- Update unit tests to verify flat structure\n- Add integration tests against LocalStack\n- Document flat snapshot rationale in CLAUDE.md and architecture.md\n\nFixes #168\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-16T02:11:18-05:00",
          "tree_id": "a576ec7e929ebc198955bef82e42c997a1227fc5",
          "url": "https://github.com/zeroae/zae-limiter/commit/f7456983c48bff0aa08c548c7f3b1a2dcb66a1ec"
        },
        "date": 1768547858459,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 69.97988273296085,
            "unit": "iter/sec",
            "range": "stddev: 0.021400984399663918",
            "extra": "mean: 14.289821030651646 msec\nrounds: 261"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 20.473846557310278,
            "unit": "iter/sec",
            "range": "stddev: 0.07902383350281372",
            "extra": "mean: 48.842800360001014 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 69.35327481167111,
            "unit": "iter/sec",
            "range": "stddev: 0.003954800973838656",
            "extra": "mean: 14.418929786884627 msec\nrounds: 61"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1084.1547774589349,
            "unit": "iter/sec",
            "range": "stddev: 0.0001957215256319108",
            "extra": "mean: 922.3775246776308 usec\nrounds: 932"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 45.34422620587325,
            "unit": "iter/sec",
            "range": "stddev: 0.1292410091791384",
            "extra": "mean: 22.053524421384306 msec\nrounds: 159"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 63.89333319221709,
            "unit": "iter/sec",
            "range": "stddev: 0.0262613683398286",
            "extra": "mean: 15.651085176470508 msec\nrounds: 255"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 39.25534965188355,
            "unit": "iter/sec",
            "range": "stddev: 0.0673488327989176",
            "extra": "mean: 25.47423494805167 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 111.58476778651337,
            "unit": "iter/sec",
            "range": "stddev: 0.027623517668341115",
            "extra": "mean: 8.961796666667118 msec\nrounds: 84"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 21.412362950628193,
            "unit": "iter/sec",
            "range": "stddev: 0.06348013651600937",
            "extra": "mean: 46.701991849557274 msec\nrounds: 226"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.127041694069521,
            "unit": "iter/sec",
            "range": "stddev: 0.2033661879301599",
            "extra": "mean: 163.2109017583345 msec\nrounds: 120"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 20.476505105491412,
            "unit": "iter/sec",
            "range": "stddev: 0.006117560072286891",
            "extra": "mean: 48.83645890000139 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.580056744351964,
            "unit": "iter/sec",
            "range": "stddev: 0.0052788581795663115",
            "extra": "mean: 46.33908111764928 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 36.04484054579451,
            "unit": "iter/sec",
            "range": "stddev: 0.004006211411353258",
            "extra": "mean: 27.74322163332954 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 29.17837450463365,
            "unit": "iter/sec",
            "range": "stddev: 0.003011851016056108",
            "extra": "mean: 34.271957125000085 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 26.697890837011457,
            "unit": "iter/sec",
            "range": "stddev: 0.005803170946228195",
            "extra": "mean: 37.456142363639216 msec\nrounds: 22"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 223.255311013445,
            "unit": "iter/sec",
            "range": "stddev: 0.0008033575258022834",
            "extra": "mean: 4.479176757142309 msec\nrounds: 140"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 57.61544007292746,
            "unit": "iter/sec",
            "range": "stddev: 0.03366192058051773",
            "extra": "mean: 17.356458593985177 msec\nrounds: 266"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 31.127532816668996,
            "unit": "iter/sec",
            "range": "stddev: 0.06387608891764243",
            "extra": "mean: 32.12589979069892 msec\nrounds: 129"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1063.9007690549017,
            "unit": "iter/sec",
            "range": "stddev: 0.00029175884561593923",
            "extra": "mean: 939.9372846476397 usec\nrounds: 938"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 52.08881137444651,
            "unit": "iter/sec",
            "range": "stddev: 0.04745842584940292",
            "extra": "mean: 19.197980787301578 msec\nrounds: 315"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 55.04027215172111,
            "unit": "iter/sec",
            "range": "stddev: 0.0585134935211939",
            "extra": "mean: 18.1685148148151 msec\nrounds: 270"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 28.457156333505562,
            "unit": "iter/sec",
            "range": "stddev: 0.043816272384679826",
            "extra": "mean: 35.14054560759454 msec\nrounds: 158"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 24.65846672000457,
            "unit": "iter/sec",
            "range": "stddev: 0.10711528709001368",
            "extra": "mean: 40.554021925002104 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 4.735301841094325,
            "unit": "iter/sec",
            "range": "stddev: 0.1528977542188116",
            "extra": "mean: 211.17977978124003 msec\nrounds: 32"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.982792584580815,
            "unit": "iter/sec",
            "range": "stddev: 0.22188725911553078",
            "extra": "mean: 167.14602518182824 msec\nrounds: 22"
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
          "id": "26c111ffbf7b87e0e75da03b66311922a4463d3f",
          "message": "chore: update .claude submodule (#183)\n\n* 🔧 chore: update .claude submodule\n\nAdds documentation for REST API issue type creation.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: remove duplicate milestone queries from CLAUDE.md\n\nNow references .claude/rules/zeroae/github.md which is loaded automatically.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-16T08:57:22-05:00",
          "tree_id": "f2d0a0ebc2910de8f99ed4078c7b0cb1492ac2be",
          "url": "https://github.com/zeroae/zae-limiter/commit/26c111ffbf7b87e0e75da03b66311922a4463d3f"
        },
        "date": 1768572240087,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 68.04224876108258,
            "unit": "iter/sec",
            "range": "stddev: 0.025078749478438987",
            "extra": "mean: 14.69675118339063 msec\nrounds: 289"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 22.089714240594336,
            "unit": "iter/sec",
            "range": "stddev: 0.0815461528518229",
            "extra": "mean: 45.26993826666607 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 24.872923242706737,
            "unit": "iter/sec",
            "range": "stddev: 0.13438181623979192",
            "extra": "mean: 40.20436159602676 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1092.6350803084142,
            "unit": "iter/sec",
            "range": "stddev: 0.00003050863748526414",
            "extra": "mean: 915.2186471239177 usec\nrounds: 904"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 62.164263519836176,
            "unit": "iter/sec",
            "range": "stddev: 0.06729248441541022",
            "extra": "mean: 16.086412729411766 msec\nrounds: 170"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 70.92638261716039,
            "unit": "iter/sec",
            "range": "stddev: 0.0240656379646217",
            "extra": "mean: 14.09912592607047 msec\nrounds: 257"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 27.161055991805497,
            "unit": "iter/sec",
            "range": "stddev: 0.06619222507150142",
            "extra": "mean: 36.817419775641284 msec\nrounds: 156"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 57.701835806896014,
            "unit": "iter/sec",
            "range": "stddev: 0.05465671038076574",
            "extra": "mean: 17.330471136942386 msec\nrounds: 314"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 22.23937689161261,
            "unit": "iter/sec",
            "range": "stddev: 0.07973629282639934",
            "extra": "mean: 44.96528859030854 msec\nrounds: 227"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.418318855071623,
            "unit": "iter/sec",
            "range": "stddev: 0.24067391040906966",
            "extra": "mean: 155.80403881147487 msec\nrounds: 122"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 22.436830002602495,
            "unit": "iter/sec",
            "range": "stddev: 0.007149993855612662",
            "extra": "mean: 44.569575999996786 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 23.587866187926867,
            "unit": "iter/sec",
            "range": "stddev: 0.005927888937261748",
            "extra": "mean: 42.39467834999999 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 38.98130721578593,
            "unit": "iter/sec",
            "range": "stddev: 0.005243762216086692",
            "extra": "mean: 25.653321333334823 msec\nrounds: 33"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 32.439905735010896,
            "unit": "iter/sec",
            "range": "stddev: 0.003998001165830776",
            "extra": "mean: 30.826230142855998 msec\nrounds: 28"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 28.000964166533656,
            "unit": "iter/sec",
            "range": "stddev: 0.005816756487131996",
            "extra": "mean: 35.71305595237986 msec\nrounds: 21"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 242.45946329355078,
            "unit": "iter/sec",
            "range": "stddev: 0.000621799275807084",
            "extra": "mean: 4.124400781953719 msec\nrounds: 133"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 41.02430036984675,
            "unit": "iter/sec",
            "range": "stddev: 0.18131045214180785",
            "extra": "mean: 24.375796564102032 msec\nrounds: 312"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.941473391737908,
            "unit": "iter/sec",
            "range": "stddev: 0.0843458442381532",
            "extra": "mean: 45.575790747787764 msec\nrounds: 226"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1104.303901478151,
            "unit": "iter/sec",
            "range": "stddev: 0.000016174494617495556",
            "extra": "mean: 905.5478285112129 usec\nrounds: 933"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 49.21655199239728,
            "unit": "iter/sec",
            "range": "stddev: 0.09521357890573658",
            "extra": "mean: 20.31836769374813 msec\nrounds: 320"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 59.94158253805981,
            "unit": "iter/sec",
            "range": "stddev: 0.05463529763913043",
            "extra": "mean: 16.682909553898607 msec\nrounds: 269"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 30.75431921886295,
            "unit": "iter/sec",
            "range": "stddev: 0.04315490225301627",
            "extra": "mean: 32.51575796178434 msec\nrounds: 157"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 27.85836203376603,
            "unit": "iter/sec",
            "range": "stddev: 0.0935801605785129",
            "extra": "mean: 35.895864903612754 msec\nrounds: 83"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.4973441887777,
            "unit": "iter/sec",
            "range": "stddev: 0.12949878157755226",
            "extra": "mean: 181.90601964515957 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.1343646114035035,
            "unit": "iter/sec",
            "range": "stddev: 0.19548817599339308",
            "extra": "mean: 194.76606662857256 msec\nrounds: 35"
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
          "id": "c31a673520e6f1574b93ab8c63446e092407bb7f",
          "message": "🔧 chore: update .claude submodule (#184)\n\nAdds PR creation workflow with issue metadata inheritance.\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-16T09:12:07-05:00",
          "tree_id": "1f2b0d9ebd78b8c51af863c6cb5ab53e82138ac8",
          "url": "https://github.com/zeroae/zae-limiter/commit/c31a673520e6f1574b93ab8c63446e092407bb7f"
        },
        "date": 1768573117804,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 72.83858989408273,
            "unit": "iter/sec",
            "range": "stddev: 0.0213680078875152",
            "extra": "mean: 13.72898626200942 msec\nrounds: 229"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 20.12131122105841,
            "unit": "iter/sec",
            "range": "stddev: 0.07263400044406278",
            "extra": "mean: 49.69855040825708 msec\nrounds: 218"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 24.139005112353196,
            "unit": "iter/sec",
            "range": "stddev: 0.17988618782486698",
            "extra": "mean: 41.426728042252556 msec\nrounds: 71"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1076.2671851187456,
            "unit": "iter/sec",
            "range": "stddev: 0.000028230181442323855",
            "extra": "mean: 929.1373125806758 usec\nrounds: 771"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 66.57318177545882,
            "unit": "iter/sec",
            "range": "stddev: 0.017654456072756005",
            "extra": "mean: 15.021063637499665 msec\nrounds: 160"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 54.56819943193316,
            "unit": "iter/sec",
            "range": "stddev: 0.03644217999900461",
            "extra": "mean: 18.325691710743946 msec\nrounds: 242"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 26.172650610633514,
            "unit": "iter/sec",
            "range": "stddev: 0.05110416544602114",
            "extra": "mean: 38.20782292465696 msec\nrounds: 146"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 50.0760028277434,
            "unit": "iter/sec",
            "range": "stddev: 0.05985244603735489",
            "extra": "mean: 19.969645010203852 msec\nrounds: 294"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 33.60158031726746,
            "unit": "iter/sec",
            "range": "stddev: 0.10401312962977667",
            "extra": "mean: 29.760505028571874 msec\nrounds: 70"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.215749892346534,
            "unit": "iter/sec",
            "range": "stddev: 0.17723127221057045",
            "extra": "mean: 160.88163412612565 msec\nrounds: 111"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 22.728101786440245,
            "unit": "iter/sec",
            "range": "stddev: 0.007750549705347796",
            "extra": "mean: 43.998394999999846 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.768645656204765,
            "unit": "iter/sec",
            "range": "stddev: 0.006752180361911743",
            "extra": "mean: 45.93763047059236 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 38.1056410024378,
            "unit": "iter/sec",
            "range": "stddev: 0.00438394708569597",
            "extra": "mean: 26.242833703703482 msec\nrounds: 27"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 27.043910351768094,
            "unit": "iter/sec",
            "range": "stddev: 0.005897670242199507",
            "extra": "mean: 36.97690115788382 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 28.078248411191684,
            "unit": "iter/sec",
            "range": "stddev: 0.004128925990390815",
            "extra": "mean: 35.61475720833821 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 212.83471049782307,
            "unit": "iter/sec",
            "range": "stddev: 0.0008682588705813981",
            "extra": "mean: 4.698481735714008 msec\nrounds: 140"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 41.02148350766855,
            "unit": "iter/sec",
            "range": "stddev: 0.16073828655540398",
            "extra": "mean: 24.377470400675787 msec\nrounds: 297"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 20.101160731926385,
            "unit": "iter/sec",
            "range": "stddev: 0.0869068250376449",
            "extra": "mean: 49.748370919283005 msec\nrounds: 223"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1096.1600745962498,
            "unit": "iter/sec",
            "range": "stddev: 0.00004228814290470971",
            "extra": "mean: 912.275518124788 usec\nrounds: 938"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 43.37475027039677,
            "unit": "iter/sec",
            "range": "stddev: 0.1635544929965901",
            "extra": "mean: 23.054887780702664 msec\nrounds: 114"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 66.64930935198063,
            "unit": "iter/sec",
            "range": "stddev: 0.027791714750477713",
            "extra": "mean: 15.003906412876923 msec\nrounds: 264"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 28.381320480298655,
            "unit": "iter/sec",
            "range": "stddev: 0.0514774570012551",
            "extra": "mean: 35.234442340135864 msec\nrounds: 147"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 27.46288889801932,
            "unit": "iter/sec",
            "range": "stddev: 0.10694363023337101",
            "extra": "mean: 36.41277520778675 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 16.833959412971765,
            "unit": "iter/sec",
            "range": "stddev: 0.00989831537019207",
            "extra": "mean: 59.40373120000686 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.304965843127747,
            "unit": "iter/sec",
            "range": "stddev: 0.12637684494862447",
            "extra": "mean: 188.5026274571471 msec\nrounds: 35"
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
          "id": "9b8fc2f0d0578184c01e09f19d689eb091e878a7",
          "message": "✨ feat(limiter): expose usage snapshot data through RateLimiter API (#188)\n\n* ✨ feat(limiter): expose usage snapshot data through RateLimiter API\n\nAdd public API methods to query aggregated usage data created by the\nLambda aggregator from DynamoDB stream events.\n\nNew methods:\n- get_usage_snapshots(): Query raw snapshot records with filtering by\n  entity_id, resource, window_type, and time range. Supports pagination.\n- get_usage_summary(): Convenience method returning aggregated stats\n  including sum, average, and snapshot count.\n\nBoth async (RateLimiter) and sync (SyncRateLimiter) interfaces provided.\n\nNew CLI commands:\n- zae-limiter usage list: Query snapshots with table output\n- zae-limiter usage summary: Show aggregated statistics\n\nModels:\n- UsageSnapshot: Existing model, now returned by public API\n- UsageSummary: New model for aggregated statistics\n\nRepository:\n- Entity-centric queries via PK (ENTITY#{id})\n- Resource-centric queries via GSI2 (RESOURCE#{name})\n- Proper timezone handling for UTC timestamps\n- Window end calculation with microsecond precision\n\nDocumentation updated in CLAUDE.md, docs/cli.md, docs/api/models.md.\n\nCloses #128\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🎨 style: fix ruff formatting\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(limiter): add unit tests for usage snapshot API\n\nAdd comprehensive test coverage for get_usage_snapshots and\nget_usage_summary methods:\n\n- Repository tests: entity/resource queries, GSI2, filters, pagination\n- RateLimiter tests: datetime conversion, async methods\n- SyncRateLimiter tests: sync wrapper parity\n\nAlso:\n- Fix CLAUDE.md GSI2SK documentation (SK -> GSI2SK)\n- Add pagination behavior note to docstrings\n- Add user guide docs/guide/usage-snapshots.md\n- Update mkdocs.yml navigation\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(cli): add coverage tests for usage commands\n\nAdd 7 more tests to TestUsageCommands covering edge cases:\n- Long entity_id truncation (>18 chars)\n- Long resource name truncation (>14 chars)\n- ValueError exception handling in usage list\n- Generic exception handling in usage list\n- Window filter display in usage summary\n- ValueError exception handling in usage summary\n- Generic exception handling in usage summary\n\nThese tests increase patch coverage from 56.59% to 94.9%,\nexceeding the 66%+ target (56% minimum + 10%).\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore: update Claude Code permissions\n\nAdd git diff and gh label list to allowed commands.\nReorganize permissions list alphabetically.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(repository): add edge case tests for usage snapshots\n\nAdd tests for:\n- Malformed snapshot items (skipped during deserialization)\n- All window types (hourly, daily, monthly) via parameterized test\n- Monthly edge cases: January, December year rollover, leap year Feb\n- Unknown window type fallback behavior\n- Invalid window_start date format handling\n\nThese tests exercise all code paths in _calculate_window_end() and\n_deserialize_usage_snapshot() for comprehensive coverage.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(monitoring): add cross-reference to usage snapshots guide\n\nAdd tip box linking to the new Usage Snapshots Guide for users\nlooking to query historical consumption data.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🎨 style(repository): add comment for unknown window type fallback\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore: set docker compose project name to zae-limiter\n\nEnsures consistent stack name across all worktrees.\nAlso fixes deprecated --table-name flag in comment.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-17T20:05:11-05:00",
          "tree_id": "455e5dda825c052c6ab60f4df528325c0310bfd3",
          "url": "https://github.com/zeroae/zae-limiter/commit/9b8fc2f0d0578184c01e09f19d689eb091e878a7"
        },
        "date": 1768698705360,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 73.08522743873533,
            "unit": "iter/sec",
            "range": "stddev: 0.021450663266979583",
            "extra": "mean: 13.68265564800032 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 21.654485312163377,
            "unit": "iter/sec",
            "range": "stddev: 0.06991849127621681",
            "extra": "mean: 46.179809198157095 msec\nrounds: 217"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 23.312622201870578,
            "unit": "iter/sec",
            "range": "stddev: 0.12087102467425502",
            "extra": "mean: 42.89521750666732 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1080.83600295317,
            "unit": "iter/sec",
            "range": "stddev: 0.00005583430746606291",
            "extra": "mean: 925.2097425212505 usec\nrounds: 936"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 56.57854848876521,
            "unit": "iter/sec",
            "range": "stddev: 0.07163318650573959",
            "extra": "mean: 17.674543209580037 msec\nrounds: 167"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 59.30137047859381,
            "unit": "iter/sec",
            "range": "stddev: 0.03312062712229858",
            "extra": "mean: 16.86301668796968 msec\nrounds: 266"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 25.739723830483264,
            "unit": "iter/sec",
            "range": "stddev: 0.06901221648680231",
            "extra": "mean: 38.85045568421023 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 53.58260283548204,
            "unit": "iter/sec",
            "range": "stddev: 0.05643852789666039",
            "extra": "mean: 18.662773868420718 msec\nrounds: 304"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 21.28364271308177,
            "unit": "iter/sec",
            "range": "stddev: 0.08082768497523748",
            "extra": "mean: 46.98443840092093 msec\nrounds: 217"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 9.15000899903372,
            "unit": "iter/sec",
            "range": "stddev: 0.20118286833630233",
            "extra": "mean: 109.28951000000156 msec\nrounds: 72"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.752566285654762,
            "unit": "iter/sec",
            "range": "stddev: 0.007941804442864443",
            "extra": "mean: 45.97158729999933 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.5490345906047,
            "unit": "iter/sec",
            "range": "stddev: 0.005976783717390287",
            "extra": "mean: 46.405791210525805 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 37.08240229956544,
            "unit": "iter/sec",
            "range": "stddev: 0.005077228978473936",
            "extra": "mean: 26.966969181813738 msec\nrounds: 33"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 30.052984011868674,
            "unit": "iter/sec",
            "range": "stddev: 0.003372517722741663",
            "extra": "mean: 33.27456600000436 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 27.13727046753524,
            "unit": "iter/sec",
            "range": "stddev: 0.005791807104212251",
            "extra": "mean: 36.84968984615885 msec\nrounds: 26"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 227.70549993881565,
            "unit": "iter/sec",
            "range": "stddev: 0.0006312931905543178",
            "extra": "mean: 4.391637445159206 msec\nrounds: 155"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 61.140896295573135,
            "unit": "iter/sec",
            "range": "stddev: 0.02700424828650067",
            "extra": "mean: 16.355664711974534 msec\nrounds: 309"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.047583504855528,
            "unit": "iter/sec",
            "range": "stddev: 0.07117667522269598",
            "extra": "mean: 47.5113924488912 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1083.614500011938,
            "unit": "iter/sec",
            "range": "stddev: 0.00023201818373398106",
            "extra": "mean: 922.837411264784 usec\nrounds: 941"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 48.73659821790345,
            "unit": "iter/sec",
            "range": "stddev: 0.09509042039754662",
            "extra": "mean: 20.518461209150388 msec\nrounds: 306"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 57.53210095473526,
            "unit": "iter/sec",
            "range": "stddev: 0.05301195126631973",
            "extra": "mean: 17.38160059175961 msec\nrounds: 267"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 30.244351381801852,
            "unit": "iter/sec",
            "range": "stddev: 0.04276426756709931",
            "extra": "mean: 33.064025324137184 msec\nrounds: 145"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 27.80486298492773,
            "unit": "iter/sec",
            "range": "stddev: 0.08857784039230993",
            "extra": "mean: 35.96493176542798 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.131401556301911,
            "unit": "iter/sec",
            "range": "stddev: 0.11896975214898133",
            "extra": "mean: 194.8785315333377 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.117052060763725,
            "unit": "iter/sec",
            "range": "stddev: 0.139678698657464",
            "extra": "mean: 195.4250197428613 msec\nrounds: 35"
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
          "id": "ad9ce0ff84a3c0543adfef558f67d73f2876fd11",
          "message": "✨ feat(aggregator): archive expired audit events to S3 (#77) (#192)\n\n* ✨ feat(aggregator): archive expired audit events to S3 (#77)\n\nThis PR implements S3 audit archival for compliance and long-term retention.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat(aggregator): archive expired audit events to S3\n\nImplements issue #77 - S3 audit archival feature that archives expired\naudit events to S3 when DynamoDB TTL deletes them. This enables long-term\nretention and compliance requirements.\n\nKey changes:\n- Add archiver.py module with gzip-compressed JSONL output\n- Extend Lambda handler to process REMOVE events for AUDIT# records\n- Add S3 bucket with lifecycle rules (Glacier IR after 90 days)\n- Add CLI flags: --enable-audit-archival, --audit-archive-glacier-days\n- Add StackOptions fields: enable_audit_archival, audit_archive_glacier_days\n- S3 bucket naming: zael-{name}-data (lowercase)\n- Hive-style partitioning: audit/year=YYYY/month=MM/day=DD/\n\nInfrastructure:\n- CloudFormation: S3 bucket, IAM permissions, lifecycle rules, outputs\n- docker-compose: Add S3 to LocalStack services\n- Add boto3-stubs and types-aiobotocore for type checking\n\nTests:\n- Unit tests for archiver module (test_archiver.py)\n- E2E tests for archival workflow (test_audit_archival.py)\n- S3 client fixture in e2e/conftest.py\n\nCloses #77\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🧪 test(aggregator): add unit tests for handler and archiver\n\nImproves patch coverage for the S3 audit archival feature:\n- Add test_handler.py with 5 tests covering:\n  - Basic record processing\n  - Archival when enabled\n  - Error aggregation from both processors\n  - Skip archival when disabled or no bucket\n- Add JSONL error handling test to test_archiver.py\n\nBoth archiver.py and handler.py now have 100% test coverage.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: update documentation for S3 audit archival feature\n\n- Add archiver.py to project structure in CLAUDE.md\n- Add missing CloudFormation parameters to cloudformation.md:\n  - BaseName, LambdaDurationThreshold, PermissionBoundary, RoleName\n- Add S3 service to LocalStack Docker examples\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(infra): convert CloudFormation diagram to Mermaid\n\nReplace ASCII art diagram with Mermaid flowchart for better\nrendering in MkDocs Material.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(infra): add S3 audit archive documentation\n\n- Update cloudformation.md with interactive Mermaid diagram\n- Add clickable nodes linking to relevant sections\n- Add S3 Audit Archive Bucket section with CloudFormation resource\n- Document object structure and lifecycle policy\n- Update production.md checklist with audit archival options\n- Add S3 costs to cost estimation table\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-18T09:33:24-05:00",
          "tree_id": "ab34f05d8433ea89dc3eee7b0c4ece53e746e0c4",
          "url": "https://github.com/zeroae/zae-limiter/commit/ad9ce0ff84a3c0543adfef558f67d73f2876fd11"
        },
        "date": 1768747182771,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 75.3113033804217,
            "unit": "iter/sec",
            "range": "stddev: 0.020886659901870337",
            "extra": "mean: 13.278219272725599 msec\nrounds: 242"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 21.729777614635825,
            "unit": "iter/sec",
            "range": "stddev: 0.0724963983400351",
            "extra": "mean: 46.019799085585774 msec\nrounds: 222"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 29.319757925757102,
            "unit": "iter/sec",
            "range": "stddev: 0.15539392560332885",
            "extra": "mean: 34.10669360000106 msec\nrounds: 90"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1063.8220271800474,
            "unit": "iter/sec",
            "range": "stddev: 0.00023738306624657127",
            "extra": "mean: 940.0068568337269 usec\nrounds: 929"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 172.2756992794103,
            "unit": "iter/sec",
            "range": "stddev: 0.00018562529101085327",
            "extra": "mean: 5.804649199990308 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 69.62215415439968,
            "unit": "iter/sec",
            "range": "stddev: 0.021302285642120597",
            "extra": "mean: 14.363244173432493 msec\nrounds: 271"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 30.525626922679226,
            "unit": "iter/sec",
            "range": "stddev: 0.04049145429037861",
            "extra": "mean: 32.75935994805214 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 56.335794852128586,
            "unit": "iter/sec",
            "range": "stddev: 0.05013669533157979",
            "extra": "mean: 17.75070366229538 msec\nrounds: 305"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 21.05168597914687,
            "unit": "iter/sec",
            "range": "stddev: 0.08429267259625779",
            "extra": "mean: 47.50213360538288 msec\nrounds: 223"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 8.862639136307848,
            "unit": "iter/sec",
            "range": "stddev: 0.1918873980230533",
            "extra": "mean: 112.83320742500607 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 22.45924768486914,
            "unit": "iter/sec",
            "range": "stddev: 0.007386956387506147",
            "extra": "mean: 44.52508890908679 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 22.906537047613234,
            "unit": "iter/sec",
            "range": "stddev: 0.005531338587324506",
            "extra": "mean: 43.65566030000139 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 39.77070434214637,
            "unit": "iter/sec",
            "range": "stddev: 0.003733493665947249",
            "extra": "mean: 25.144136030305756 msec\nrounds: 33"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 33.10667939644361,
            "unit": "iter/sec",
            "range": "stddev: 0.0036741993527859822",
            "extra": "mean: 30.20538508333222 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 27.28940546284426,
            "unit": "iter/sec",
            "range": "stddev: 0.00501551372157572",
            "extra": "mean: 36.64425747059769 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 230.58673789406782,
            "unit": "iter/sec",
            "range": "stddev: 0.0005950790625978251",
            "extra": "mean: 4.33676285606418 msec\nrounds: 132"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 60.967232774870766,
            "unit": "iter/sec",
            "range": "stddev: 0.030838096226634436",
            "extra": "mean: 16.402253382446055 msec\nrounds: 319"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.903104703343562,
            "unit": "iter/sec",
            "range": "stddev: 0.07898068298819738",
            "extra": "mean: 45.65562798260959 msec\nrounds: 230"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1105.2218157605942,
            "unit": "iter/sec",
            "range": "stddev: 0.0002040386916350147",
            "extra": "mean: 904.7957484550896 usec\nrounds: 970"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 64.51604954982707,
            "unit": "iter/sec",
            "range": "stddev: 0.026545526861415755",
            "extra": "mean: 15.500019095677573 msec\nrounds: 324"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 285.90244219637896,
            "unit": "iter/sec",
            "range": "stddev: 0.0002696147294855323",
            "extra": "mean: 3.4976965999931053 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 31.35913753854939,
            "unit": "iter/sec",
            "range": "stddev: 0.037968592773771216",
            "extra": "mean: 31.888632101910098 msec\nrounds: 157"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 28.505345859473348,
            "unit": "iter/sec",
            "range": "stddev: 0.09991537158492296",
            "extra": "mean: 35.08113898809841 msec\nrounds: 84"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.6008584889683295,
            "unit": "iter/sec",
            "range": "stddev: 0.12035173433114636",
            "extra": "mean: 178.54405748148062 msec\nrounds: 27"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.485187122124796,
            "unit": "iter/sec",
            "range": "stddev: 0.17213043658761584",
            "extra": "mean: 182.30918612902857 msec\nrounds: 31"
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
          "id": "0b81581a2c282574ba7775352f85cc8d11239ab3",
          "message": "🔧 chore: add milestone skill and VSCode workspace (#193)\n\nAdd Claude Code skill for milestone status analysis and a basic VSCode\nworkspace configuration file.\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-18T11:22:59-05:00",
          "tree_id": "f4ae8323702899a8faea9eb42ed55c2fef44d6df",
          "url": "https://github.com/zeroae/zae-limiter/commit/0b81581a2c282574ba7775352f85cc8d11239ab3"
        },
        "date": 1768753776253,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 71.98781748590149,
            "unit": "iter/sec",
            "range": "stddev: 0.02156334885233214",
            "extra": "mean: 13.891239308593374 msec\nrounds: 256"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 20.76055490864747,
            "unit": "iter/sec",
            "range": "stddev: 0.06835751885710054",
            "extra": "mean: 48.168269316513616 msec\nrounds: 218"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 62.768347124062835,
            "unit": "iter/sec",
            "range": "stddev: 0.005403149713042793",
            "extra": "mean: 15.931596828947573 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1089.3496533897737,
            "unit": "iter/sec",
            "range": "stddev: 0.00004393554494208073",
            "extra": "mean: 917.9789031815994 usec\nrounds: 723"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 37.347829472187534,
            "unit": "iter/sec",
            "range": "stddev: 0.16883363918690691",
            "extra": "mean: 26.775317712765276 msec\nrounds: 94"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 63.23457420607758,
            "unit": "iter/sec",
            "range": "stddev: 0.027201978930011448",
            "extra": "mean: 15.814133526716914 msec\nrounds: 262"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 27.26020554847791,
            "unit": "iter/sec",
            "range": "stddev: 0.06297039893419512",
            "extra": "mean: 36.68350916216168 msec\nrounds: 148"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 53.50119187297783,
            "unit": "iter/sec",
            "range": "stddev: 0.05177596262104732",
            "extra": "mean: 18.691172383116125 msec\nrounds: 308"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 19.796654650703626,
            "unit": "iter/sec",
            "range": "stddev: 0.08954440791638228",
            "extra": "mean: 50.513585130630005 msec\nrounds: 222"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.183368294109838,
            "unit": "iter/sec",
            "range": "stddev: 0.22984193484825075",
            "extra": "mean: 161.72415299159545 msec\nrounds: 119"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.658104358911537,
            "unit": "iter/sec",
            "range": "stddev: 0.010419913533199314",
            "extra": "mean: 46.17209259999413 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.892015522938063,
            "unit": "iter/sec",
            "range": "stddev: 0.006124203839104368",
            "extra": "mean: 47.86517599999223 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 37.15397335113646,
            "unit": "iter/sec",
            "range": "stddev: 0.004365491871848744",
            "extra": "mean: 26.915021727263316 msec\nrounds: 33"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 30.031484590621492,
            "unit": "iter/sec",
            "range": "stddev: 0.004477467672604727",
            "extra": "mean: 33.29838713042808 msec\nrounds: 23"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 23.360186315239332,
            "unit": "iter/sec",
            "range": "stddev: 0.005423809893430803",
            "extra": "mean: 42.8078777500005 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 194.06379920622473,
            "unit": "iter/sec",
            "range": "stddev: 0.0011045732772183307",
            "extra": "mean: 5.152944568179537 msec\nrounds: 132"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 40.22541262449255,
            "unit": "iter/sec",
            "range": "stddev: 0.17965915440890806",
            "extra": "mean: 24.85990658032722 msec\nrounds: 305"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.423587358268403,
            "unit": "iter/sec",
            "range": "stddev: 0.07200232000133827",
            "extra": "mean: 46.67752338938005 msec\nrounds: 226"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1089.4026023753127,
            "unit": "iter/sec",
            "range": "stddev: 0.00002324828763434193",
            "extra": "mean: 917.9342860202637 usec\nrounds: 937"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 49.32164474813174,
            "unit": "iter/sec",
            "range": "stddev: 0.09673342303102024",
            "extra": "mean: 20.275074059404297 msec\nrounds: 303"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 56.35330581439477,
            "unit": "iter/sec",
            "range": "stddev: 0.05334672517123542",
            "extra": "mean: 17.745187891791115 msec\nrounds: 268"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 27.382603600083165,
            "unit": "iter/sec",
            "range": "stddev: 0.07019493079225396",
            "extra": "mean: 36.519536805366556 msec\nrounds: 149"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 26.804871241317187,
            "unit": "iter/sec",
            "range": "stddev: 0.09597398683930768",
            "extra": "mean: 37.306651876715385 msec\nrounds: 73"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.133232412228992,
            "unit": "iter/sec",
            "range": "stddev: 0.12059490226375105",
            "extra": "mean: 194.80902474193104 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 21.391398190209465,
            "unit": "iter/sec",
            "range": "stddev: 0.009423618992485526",
            "extra": "mean: 46.747762400013926 msec\nrounds: 5"
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
          "id": "e6f05f277d2dbe3727bf85dba6bd7ba6eb5c70e4",
          "message": "✨ feat(infra): add AWS X-Ray tracing support for Lambda aggregator (#196)\n\n* ✨ feat(infra): add AWS X-Ray tracing support for Lambda aggregator\n\nAdd opt-in X-Ray tracing configuration for the Lambda aggregator function.\n\nChanges:\n- Add `enable_tracing: bool = False` to StackOptions\n- Add `--enable-tracing/--no-tracing` CLI flag\n- Add EnableTracing CloudFormation parameter with TracingEnabled condition\n- Add conditional XRayAccess IAM policy (only when tracing enabled)\n- Add TracingConfig.Mode (Active when enabled, PassThrough otherwise)\n- Add AWS E2E tests for tracing configuration validation\n\nKey design decisions:\n- Uses Active mode (not PassThrough) because DynamoDB Streams don't\n  propagate trace context headers\n- IAM permissions conditionally added to follow least privilege\n- Default is disabled (opt-in) to avoid unexpected X-Ray costs\n\nNote: DynamoDB subsegments require aws-xray-sdk instrumentation (#194).\n\nCloses #107\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore(ci): add cfn-lint to CI and remove Claude review workflows\n\n- Add CloudFormation template linting step to lint job\n- Remove claude-infra-review.yml workflow\n- Remove claude-test-review.yml workflow\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix: resolve merge conflict in CLAUDE.md\n\nKeep both audit archival and X-Ray tracing examples.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-18T13:31:07-05:00",
          "tree_id": "562894fc1f560a2611e0c219695ea551ab4bce92",
          "url": "https://github.com/zeroae/zae-limiter/commit/e6f05f277d2dbe3727bf85dba6bd7ba6eb5c70e4"
        },
        "date": 1768761464368,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 66.38511620075532,
            "unit": "iter/sec",
            "range": "stddev: 0.022550593292424023",
            "extra": "mean: 15.063617527999781 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 20.57343164151782,
            "unit": "iter/sec",
            "range": "stddev: 0.07255236604732437",
            "extra": "mean: 48.606378236967 msec\nrounds: 211"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 23.83739432827239,
            "unit": "iter/sec",
            "range": "stddev: 0.11515118383899581",
            "extra": "mean: 41.9508938866673 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1051.4030173457643,
            "unit": "iter/sec",
            "range": "stddev: 0.00004461786737197159",
            "extra": "mean: 951.1100724482133 usec\nrounds: 911"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 59.528243071458355,
            "unit": "iter/sec",
            "range": "stddev: 0.06986154353496066",
            "extra": "mean: 16.798748768707803 msec\nrounds: 147"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 105.1894087627127,
            "unit": "iter/sec",
            "range": "stddev: 0.03166424208717338",
            "extra": "mean: 9.506660525641035 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 25.529952114259814,
            "unit": "iter/sec",
            "range": "stddev: 0.04801580425886814",
            "extra": "mean: 39.16967785620905 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 52.23335667110393,
            "unit": "iter/sec",
            "range": "stddev: 0.053438849227946596",
            "extra": "mean: 19.144854241259416 msec\nrounds: 286"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 19.876874447821322,
            "unit": "iter/sec",
            "range": "stddev: 0.07547936568206459",
            "extra": "mean: 50.3097206064814 msec\nrounds: 216"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 5.733327719419384,
            "unit": "iter/sec",
            "range": "stddev: 0.20719838905154686",
            "extra": "mean: 174.41877543697615 msec\nrounds: 119"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.034443073880322,
            "unit": "iter/sec",
            "range": "stddev: 0.010114947069566926",
            "extra": "mean: 47.54107329999897 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.803015730902825,
            "unit": "iter/sec",
            "range": "stddev: 0.0045609151362836255",
            "extra": "mean: 48.06995355555602 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 37.30294404662296,
            "unit": "iter/sec",
            "range": "stddev: 0.003942628995735968",
            "extra": "mean: 26.807535586203418 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 29.62099804642526,
            "unit": "iter/sec",
            "range": "stddev: 0.0039027795459202646",
            "extra": "mean: 33.75983477777119 msec\nrounds: 27"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 20.18037030724683,
            "unit": "iter/sec",
            "range": "stddev: 0.011569741453169797",
            "extra": "mean: 49.55310456522679 msec\nrounds: 23"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 195.01414156760595,
            "unit": "iter/sec",
            "range": "stddev: 0.0007136447445371076",
            "extra": "mean: 5.127833253330133 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 58.643004886423675,
            "unit": "iter/sec",
            "range": "stddev: 0.02778160618203428",
            "extra": "mean: 17.05233219097045 msec\nrounds: 288"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 20.20918984358085,
            "unit": "iter/sec",
            "range": "stddev: 0.07415543350223247",
            "extra": "mean: 49.482438818181286 msec\nrounds: 220"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1096.8213888694866,
            "unit": "iter/sec",
            "range": "stddev: 0.00002281249546780599",
            "extra": "mean: 911.7254733979229 usec\nrounds: 921"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 57.26004289367371,
            "unit": "iter/sec",
            "range": "stddev: 0.02932357723494129",
            "extra": "mean: 17.46418531080918 msec\nrounds: 296"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 172.14765236433473,
            "unit": "iter/sec",
            "range": "stddev: 0.0015823308404797985",
            "extra": "mean: 5.808966815786669 msec\nrounds: 76"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 24.883927984767542,
            "unit": "iter/sec",
            "range": "stddev: 0.07433351977290673",
            "extra": "mean: 40.18658150000034 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 26.79373453219197,
            "unit": "iter/sec",
            "range": "stddev: 0.08355061718018521",
            "extra": "mean: 37.32215823809578 msec\nrounds: 84"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 16.415476929257622,
            "unit": "iter/sec",
            "range": "stddev: 0.01114815476476117",
            "extra": "mean: 60.91812040000377 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.013241377048939,
            "unit": "iter/sec",
            "range": "stddev: 0.1243050717474499",
            "extra": "mean: 199.47174388571997 msec\nrounds: 35"
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
          "id": "a1e5b22f2c9f84bc47e8ba0f832a499c3d932235",
          "message": "✨ feat(cli,limiter): add list command to discover deployed rate limiters (#198)\n\n* ✨ feat(cli,limiter): add list command to discover deployed rate limiters\n\nAdd capability to list all deployed zae-limiter instances in a region:\n- New `zae-limiter list` CLI command with table output\n- New `RateLimiter.list_deployed()` class method for programmatic access\n- New `LimiterInfo` frozen dataclass for discovered stack information\n- New `InfrastructureDiscovery` class for CloudFormation stack discovery\n\nThe list feature queries CloudFormation for stacks with ZAEL- prefix and\nextracts version info from stack tags.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(cli,limiter): add unit tests for list command and discovery\n\n- Add 19 unit tests for CLI list command covering all display paths\n- Add 6 tests for InfrastructureDiscovery edge cases\n- Achieve 90% overall test coverage (exceeds 88% requirement)\n- Add Claude Code hooks: LocalStack auto-start, mypy pre-commit check\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(limiter): address code review compliance issues\n\n- Add SyncRateLimiter.list_deployed() for async/sync parity\n- Add discovery.py to CLAUDE.md Project Structure section\n- Add E2E test for zae-limiter list command\n- Add unit tests for SyncRateLimiter.list_deployed()\n\nAddresses all 3 issues from claude[bot] review on PR #198.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ♻️ refactor(cli): use rich table format across all CLI commands\n\n- Add _print_table() helper for consistent box-drawing tables\n- Apply to: list, audit list, usage list, usage summary\n- Auto-sized columns show full values (no truncation)\n- Supports left/right alignment per column\n- Consistent format across all tabular output\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🎨 style: apply ruff formatting to 5 files\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-18T16:35:04-05:00",
          "tree_id": "7c31b3736d6405d8c0dbeae2ee77730f60645d61",
          "url": "https://github.com/zeroae/zae-limiter/commit/a1e5b22f2c9f84bc47e8ba0f832a499c3d932235"
        },
        "date": 1768772495630,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 69.6484380792357,
            "unit": "iter/sec",
            "range": "stddev: 0.022521724392029718",
            "extra": "mean: 14.357823772908558 msec\nrounds: 251"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 21.157869623697852,
            "unit": "iter/sec",
            "range": "stddev: 0.07094895259747654",
            "extra": "mean: 47.26373769124425 msec\nrounds: 217"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 26.820408556500507,
            "unit": "iter/sec",
            "range": "stddev: 0.15872577243365965",
            "extra": "mean: 37.285039782051655 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 793.1857516048245,
            "unit": "iter/sec",
            "range": "stddev: 0.010146348319150539",
            "extra": "mean: 1.2607387336153422 msec\nrounds: 946"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 72.94319816166758,
            "unit": "iter/sec",
            "range": "stddev: 0.01844224689766829",
            "extra": "mean: 13.709297442424324 msec\nrounds: 165"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 58.788269388548784,
            "unit": "iter/sec",
            "range": "stddev: 0.03393705098600362",
            "extra": "mean: 17.01019625855473 msec\nrounds: 263"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 24.208171051692634,
            "unit": "iter/sec",
            "range": "stddev: 0.07435307645654639",
            "extra": "mean: 41.308366413334646 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 51.933008095080154,
            "unit": "iter/sec",
            "range": "stddev: 0.058396426839308256",
            "extra": "mean: 19.255576302631592 msec\nrounds: 304"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 20.037339810685033,
            "unit": "iter/sec",
            "range": "stddev: 0.08971899731260487",
            "extra": "mean: 49.906824431192405 msec\nrounds: 218"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 9.059243312521895,
            "unit": "iter/sec",
            "range": "stddev: 0.2038290211115313",
            "extra": "mean: 110.38449520588291 msec\nrounds: 68"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.52270406484321,
            "unit": "iter/sec",
            "range": "stddev: 0.011699483483384206",
            "extra": "mean: 46.46256330000256 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.776250894815668,
            "unit": "iter/sec",
            "range": "stddev: 0.006410678505962016",
            "extra": "mean: 45.92158699999516 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 33.14034824805769,
            "unit": "iter/sec",
            "range": "stddev: 0.007651127738493607",
            "extra": "mean: 30.174698000000905 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 30.27711847985053,
            "unit": "iter/sec",
            "range": "stddev: 0.004392406731807879",
            "extra": "mean: 33.0282421250061 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 26.719119304654086,
            "unit": "iter/sec",
            "range": "stddev: 0.006014962170309778",
            "extra": "mean: 37.426383279998845 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 221.38818660043296,
            "unit": "iter/sec",
            "range": "stddev: 0.0007589334603718469",
            "extra": "mean: 4.5169528480976515 msec\nrounds: 158"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 55.97917972144093,
            "unit": "iter/sec",
            "range": "stddev: 0.03456341864551608",
            "extra": "mean: 17.863784445862176 msec\nrounds: 314"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 20.567401113942395,
            "unit": "iter/sec",
            "range": "stddev: 0.0862434206581621",
            "extra": "mean: 48.620630018350354 msec\nrounds: 218"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1108.8498297932897,
            "unit": "iter/sec",
            "range": "stddev: 0.00005040634557269844",
            "extra": "mean: 901.8353731329144 usec\nrounds: 804"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 46.637156550312916,
            "unit": "iter/sec",
            "range": "stddev: 0.08913134107943091",
            "extra": "mean: 21.44213056645475 msec\nrounds: 316"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 52.38811589853022,
            "unit": "iter/sec",
            "range": "stddev: 0.061328212662254446",
            "extra": "mean: 19.088298612167797 msec\nrounds: 263"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 27.0346289514126,
            "unit": "iter/sec",
            "range": "stddev: 0.07266035647422889",
            "extra": "mean: 36.989595891892144 msec\nrounds: 148"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 24.786814690199712,
            "unit": "iter/sec",
            "range": "stddev: 0.0952805171382252",
            "extra": "mean: 40.344030182925565 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 4.977461970311048,
            "unit": "iter/sec",
            "range": "stddev: 0.1341707361846426",
            "extra": "mean: 200.9056032903268 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 21.60776434043329,
            "unit": "iter/sec",
            "range": "stddev: 0.009206202299605034",
            "extra": "mean: 46.27966059999835 msec\nrounds: 5"
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
          "id": "deb04b33394c2fbb1611939c81c53d9150869a66",
          "message": "✨ feat(cli): add ASCII plot visualization for usage snapshots (#201)\n\n* 🔧 chore(ci): add PostToolUse hook for uv sync on pyproject.toml changes\n\nAutomatically runs `uv sync --all-extras` when pyproject.toml is\nmodified via Edit or Write tools, keeping dependencies in sync.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat(cli): add ASCII plot visualization for usage snapshots (#200)\n\nAdd --plot flag to `usage list` command for ASCII chart visualization\nof usage snapshot timeseries data using asciichartpy.\n\nFeatures:\n- Side-by-side chart layout (2 counters per row)\n- Auto-downsampling for large datasets (>60 points)\n- Right-aligned Y-axis labels for proper alignment\n- Entity/resource context in chart header\n- Graceful fallback to table format if asciichartpy not installed\n\nInstall with: pip install 'zae-limiter[plot]'\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(cli): update plot examples with actual output format\n\nUpdate CLI and usage-snapshots docs to show:\n- Side-by-side chart layout\n- Entity/resource header\n- Feature list (downsampling, right-aligned Y-axis)\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 👷 ci(claude): gate code review on core CI jobs passing\n\nAdd a wait step that polls GitHub Checks API for lint, typecheck, and\nunit jobs before running Claude code review. This prevents wasting API\ncalls on PRs that will fail basic CI checks.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ♻️ refactor(cli): use TableRenderer for consistent table output\n\n- Add TableRenderer class for box-drawing tables with auto-sized columns\n- Replace _print_table function with TableRenderer across all CLI commands\n- Fix truncation issue in usage list by using TableRenderer instead of TableFormatter\n- Add plot extra to CI for e2e and unit tests\n- Fix mypy and lint errors in visualization module\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 👷 ci(claude): gate code review on core CI jobs passing\n\nAdd a wait step that polls GitHub Checks API for lint, typecheck, and\nunit jobs before running Claude code review. This prevents wasting API\ncalls on PRs that will fail basic CI checks.\n\nUses exact job names including matrix variants (unit (3.11), unit (3.12)).\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ⏪️ revert(ci): remove CI job gating from claude-code-review\n\nRevert the wait-for-CI-jobs step added in b62523c. The polling approach\nhad issues with matrix job naming and didn't fail fast properly.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-18T19:13:29-05:00",
          "tree_id": "d61260a10a999263ed479e35f964b8d79802ff7b",
          "url": "https://github.com/zeroae/zae-limiter/commit/deb04b33394c2fbb1611939c81c53d9150869a66"
        },
        "date": 1768781982973,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 76.32694832674972,
            "unit": "iter/sec",
            "range": "stddev: 0.020834846250855903",
            "extra": "mean: 13.10153257692261 msec\nrounds: 208"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 20.212523399768173,
            "unit": "iter/sec",
            "range": "stddev: 0.07047774280284144",
            "extra": "mean: 49.47427791284434 msec\nrounds: 218"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 24.689749459342572,
            "unit": "iter/sec",
            "range": "stddev: 0.18230738770780847",
            "extra": "mean: 40.50263862121133 msec\nrounds: 66"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1075.7122355394395,
            "unit": "iter/sec",
            "range": "stddev: 0.000022669043056326197",
            "extra": "mean: 929.6166455692754 usec\nrounds: 869"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 70.5761890213037,
            "unit": "iter/sec",
            "range": "stddev: 0.016794441081714087",
            "extra": "mean: 14.169084699347339 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 53.59962398030403,
            "unit": "iter/sec",
            "range": "stddev: 0.0362217097461343",
            "extra": "mean: 18.656847301157647 msec\nrounds: 259"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 24.99875383780528,
            "unit": "iter/sec",
            "range": "stddev: 0.07329933992367631",
            "extra": "mean: 40.0019939589034 msec\nrounds: 146"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 48.457921246832555,
            "unit": "iter/sec",
            "range": "stddev: 0.0578469004825426",
            "extra": "mean: 20.636460959731426 msec\nrounds: 298"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 19.296573724503688,
            "unit": "iter/sec",
            "range": "stddev: 0.09053796230784306",
            "extra": "mean: 51.822671437787605 msec\nrounds: 217"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 5.838894108106034,
            "unit": "iter/sec",
            "range": "stddev: 0.2278647865015846",
            "extra": "mean: 171.26530837607032 msec\nrounds: 117"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 20.407536295814783,
            "unit": "iter/sec",
            "range": "stddev: 0.00962392410142483",
            "extra": "mean: 49.00150539999686 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 19.076087769524275,
            "unit": "iter/sec",
            "range": "stddev: 0.0066971666161088515",
            "extra": "mean: 52.42165018749745 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 38.66672894265738,
            "unit": "iter/sec",
            "range": "stddev: 0.004611095993537012",
            "extra": "mean: 25.86202731249898 msec\nrounds: 32"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 32.242469622796875,
            "unit": "iter/sec",
            "range": "stddev: 0.0037229758229974028",
            "extra": "mean: 31.014993941188525 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 27.557536081619737,
            "unit": "iter/sec",
            "range": "stddev: 0.004860381333878348",
            "extra": "mean: 36.287714440006766 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 215.94525271910504,
            "unit": "iter/sec",
            "range": "stddev: 0.0010696126988187465",
            "extra": "mean: 4.630803351351138 msec\nrounds: 148"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 64.55048841952286,
            "unit": "iter/sec",
            "range": "stddev: 0.027352351667976012",
            "extra": "mean: 15.491749551155321 msec\nrounds: 303"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 20.989168974220224,
            "unit": "iter/sec",
            "range": "stddev: 0.08157330747362149",
            "extra": "mean: 47.643620441964224 msec\nrounds: 224"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1083.6186656570985,
            "unit": "iter/sec",
            "range": "stddev: 0.00025625221355288127",
            "extra": "mean: 922.8338636946672 usec\nrounds: 785"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 63.02793397974699,
            "unit": "iter/sec",
            "range": "stddev: 0.02747685546678024",
            "extra": "mean: 15.865980952530254 msec\nrounds: 316"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 64.6441665881592,
            "unit": "iter/sec",
            "range": "stddev: 0.026771520454242283",
            "extra": "mean: 15.469299904056136 msec\nrounds: 271"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 29.976597984161074,
            "unit": "iter/sec",
            "range": "stddev: 0.045919925801509914",
            "extra": "mean: 33.359355872483476 msec\nrounds: 149"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 29.241758699516957,
            "unit": "iter/sec",
            "range": "stddev: 0.09711437895259892",
            "extra": "mean: 34.19766951351387 msec\nrounds: 74"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.655073654834526,
            "unit": "iter/sec",
            "range": "stddev: 0.1105913432409043",
            "extra": "mean: 176.8323564000089 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.579375054534943,
            "unit": "iter/sec",
            "range": "stddev: 0.21371184892676698",
            "extra": "mean: 179.23154299999874 msec\nrounds: 29"
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
          "id": "80aaea10c3f56035fc6443931d7ad9b5b598ab42",
          "message": "chore: add release-prep skill and expand CLI permissions (#202)\n\n* 📝 docs: clarify milestone skill trigger conditions\n\nReword skill description to lead with trigger conditions (version\nmentions, release status questions) rather than generic \"use when\"\nguidance, making it clearer when to invoke the skill.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: add permission boundary and iteration tips for AWS testing\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(monitoring): update X-Ray section for Phase 1 implementation\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore: add release-prep skill and expand CLI permissions\n\n- Add release-prep skill for milestone release workflow\n- Expand allowed permissions for zae-limiter CLI commands\n- Add AWS profile permissions for deploy/delete operations\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🎨 style: sort permissions alphabetically in settings.json\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-18T19:26:11-05:00",
          "tree_id": "616159a8c497139ce25d9b8e2ad3e3ddeccc95d0",
          "url": "https://github.com/zeroae/zae-limiter/commit/80aaea10c3f56035fc6443931d7ad9b5b598ab42"
        },
        "date": 1768782743911,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 71.09877638254099,
            "unit": "iter/sec",
            "range": "stddev: 0.02276758231219193",
            "extra": "mean: 14.064939663934357 msec\nrounds: 244"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 20.703534312990577,
            "unit": "iter/sec",
            "range": "stddev: 0.07564132274755873",
            "extra": "mean: 48.30093185454538 msec\nrounds: 220"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 24.268489385109632,
            "unit": "iter/sec",
            "range": "stddev: 0.12607381240758403",
            "extra": "mean: 41.205696165562244 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1085.9552574460217,
            "unit": "iter/sec",
            "range": "stddev: 0.000044057528547021005",
            "extra": "mean: 920.8482514756882 usec\nrounds: 847"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 73.04587862420851,
            "unit": "iter/sec",
            "range": "stddev: 0.019284632563873742",
            "extra": "mean: 13.69002630722803 msec\nrounds: 166"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 60.87103483614871,
            "unit": "iter/sec",
            "range": "stddev: 0.032479810531657875",
            "extra": "mean: 16.428174791044338 msec\nrounds: 268"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 24.627504950988644,
            "unit": "iter/sec",
            "range": "stddev: 0.07233617862917752",
            "extra": "mean: 40.60500655629169 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 172.461091928055,
            "unit": "iter/sec",
            "range": "stddev: 0.0018040629649804835",
            "extra": "mean: 5.798409303920948 msec\nrounds: 102"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 20.365604494850434,
            "unit": "iter/sec",
            "range": "stddev: 0.08701367537084928",
            "extra": "mean: 49.10239714479657 msec\nrounds: 221"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.503903333376783,
            "unit": "iter/sec",
            "range": "stddev: 0.16903587792506367",
            "extra": "mean: 153.75382270338983 msec\nrounds: 118"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.729763300686674,
            "unit": "iter/sec",
            "range": "stddev: 0.008264223370776149",
            "extra": "mean: 46.019829400000845 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.83890792123949,
            "unit": "iter/sec",
            "range": "stddev: 0.008343055816293769",
            "extra": "mean: 45.789835444447625 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 36.61289026366121,
            "unit": "iter/sec",
            "range": "stddev: 0.004112042254002107",
            "extra": "mean: 27.312785000000765 msec\nrounds: 27"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 31.109265135709023,
            "unit": "iter/sec",
            "range": "stddev: 0.0037040468152108306",
            "extra": "mean: 32.14476445000116 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 26.604623885624058,
            "unit": "iter/sec",
            "range": "stddev: 0.003841612303336104",
            "extra": "mean: 37.58745112500369 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 226.97383767980764,
            "unit": "iter/sec",
            "range": "stddev: 0.0007555314848311635",
            "extra": "mean: 4.405794122451688 msec\nrounds: 147"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 57.0004521514916,
            "unit": "iter/sec",
            "range": "stddev: 0.034853130029601394",
            "extra": "mean: 17.543720483870437 msec\nrounds: 310"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 19.4101080437136,
            "unit": "iter/sec",
            "range": "stddev: 0.09382185918457994",
            "extra": "mean: 51.51954835840662 msec\nrounds: 226"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1092.657606146751,
            "unit": "iter/sec",
            "range": "stddev: 0.0002595843271859657",
            "extra": "mean: 915.1997793036857 usec\nrounds: 947"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 49.94396687593031,
            "unit": "iter/sec",
            "range": "stddev: 0.09699821371739827",
            "extra": "mean: 20.02243839549585 msec\nrounds: 311"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 55.4468065830214,
            "unit": "iter/sec",
            "range": "stddev: 0.05613391031632207",
            "extra": "mean: 18.03530377358494 msec\nrounds: 265"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 26.173262114999304,
            "unit": "iter/sec",
            "range": "stddev: 0.07085480912644482",
            "extra": "mean: 38.20693024836681 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 26.327190321511598,
            "unit": "iter/sec",
            "range": "stddev: 0.09938040449488098",
            "extra": "mean: 37.98354430487454 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.104685369322023,
            "unit": "iter/sec",
            "range": "stddev: 0.13500090043132174",
            "extra": "mean: 195.8984594838633 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.877682474825696,
            "unit": "iter/sec",
            "range": "stddev: 0.20738034202519",
            "extra": "mean: 205.01539515151302 msec\nrounds: 33"
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
          "id": "4f3f1a61cb620643949b3bd3c135baf7bf1e6f90",
          "message": "docs(adr): add ADR-001 centralized configuration access patterns (#195)\n\n* 📝 docs: add mkdocs serve --livereload --dirty workaround\n\nDue to Click 8.3.x bug (squidfunk/mkdocs-material#8478), the\n--livereload flag must be explicitly passed for live reload to work.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: add ADR-001 centralized config access patterns (#129)\n\nAdd Architecture Decision Record documenting:\n- Three-level config hierarchy (Entity > Resource > System)\n- Flat schema for atomic config_version counter\n- Caching strategy with negative caching for sparse traffic\n- Cost analysis for various traffic patterns\n- API change: stored limits as default behavior\n- v0.6.0 recommendation to flatten all existing records\n\nUpdate CLAUDE.md with centralized configuration section and\naccess patterns documentation.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(adr): refine ADR-001 with config scope, caching, and issue links\n\n- Clarify config field scope: on_unavailable at all levels, auto_update/strict_version system-only\n- Add detailed record structure examples for each level (system, resource, entity)\n- Update RCU costs to use eventually consistent reads (50% savings)\n- Add distributed cache consistency and read consistency sections\n- Remove config_version (TTL-based cache is sufficient)\n- Link implementation checklist to milestone issues (#129, #130, #131, #135)\n- Enable pymdownx.tasklist extension in mkdocs.yml\n- Convert checklist to admonition format for better rendering\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore: add mkdocs permission to project settings\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat(skills): add context-aware issue and PR skills\n\n- Issue skill: create, update, and track progress on GitHub issues\n  - Infers type, labels, milestone from conversation context\n  - Supports Bug, Feature, Task, Chore, Epic, Theme types\n  - Progress mode checks off checkboxes based on completed work\n\n- PR skill: create feature PRs and release prep PRs\n  - Feature PR inherits metadata from linked issues\n  - Release Prep PR verifies milestone readiness before cutting release\n  - Moved Release Epic functionality from issue to PR skill\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore(docs): remove navigation expand features from mkdocs\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat(skills): add ADR creation and review skill\n\nAdd tooling to enforce ADR best practices:\n- .claude/rules/adr-format.md: Auto-enforced rules (100 line limit,\n  one decision per ADR, required sections, excluded content)\n- .claude/skills/adr/SKILL.md: User-invocable skill for /adr create\n  and /adr review commands\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat(skills): add changelog skill for git-cliff setup\n\nConverts changelog.md rule to on-demand skill to save context.\nUse `/changelog init` to set up git-cliff in a new project.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(adr): split ADR-001 into focused decisions (101-105)\n\nSplit the 401-line ADR-001 into 6 focused ADRs under 100 lines each:\n- ADR-001: Summary/index linking to sub-decisions\n- ADR-101: Flat schema for config records\n- ADR-102: Three-level configuration hierarchy\n- ADR-103: Client-side config caching with TTL\n- ADR-104: Stored limits as default behavior\n- ADR-105: Eventually consistent reads for config\n\nNumbering starts at 100 to leave 001-099 available for backfilling\nhistorical decisions.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: add ADR-101 through ADR-105 to mkdocs nav\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore: add hook to prompt mkdocs.yml update for new docs files\n\nWhen a new .md file is created in docs/, Claude is prompted to check\nif mkdocs.yml nav needs updating. Also fixes duplicate PostToolUse\nkeys in settings.json.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(adr): rename ADR-001 to ADR-100\n\nAll centralized config ADRs now use 100-series numbering (100-105),\nleaving 001-099 available for backfilling historical decisions.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat(skills): add /pr edit mode to update PR body\n\nAdd Edit PR Mode to regenerate PR descriptions based on current commits.\nUseful when PR scope has changed significantly.\n\nTriggers:\n- `/pr edit` - Edit PR for current branch\n- `/pr edit 195` - Edit specific PR number\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-18T23:00:05-05:00",
          "tree_id": "7dadc6e85120e250e8ab5fb0add325c2e446b8aa",
          "url": "https://github.com/zeroae/zae-limiter/commit/4f3f1a61cb620643949b3bd3c135baf7bf1e6f90"
        },
        "date": 1768795592648,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 65.34614428034439,
            "unit": "iter/sec",
            "range": "stddev: 0.023387088395285378",
            "extra": "mean: 15.303121722222135 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 20.56382958881603,
            "unit": "iter/sec",
            "range": "stddev: 0.07154730090949278",
            "extra": "mean: 48.62907444748843 msec\nrounds: 219"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 21.62468062461691,
            "unit": "iter/sec",
            "range": "stddev: 0.12642766123893304",
            "extra": "mean: 46.243457527027196 msec\nrounds: 148"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1081.961217531116,
            "unit": "iter/sec",
            "range": "stddev: 0.00003459236378671372",
            "extra": "mean: 924.247545842595 usec\nrounds: 938"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 57.72442864386516,
            "unit": "iter/sec",
            "range": "stddev: 0.07393603550760068",
            "extra": "mean: 17.323688141974845 msec\nrounds: 162"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 150.82280919483728,
            "unit": "iter/sec",
            "range": "stddev: 0.008152362190369352",
            "extra": "mean: 6.6302968717959025 msec\nrounds: 78"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 27.831563805567868,
            "unit": "iter/sec",
            "range": "stddev: 0.044347130859435015",
            "extra": "mean: 35.93042802000023 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 51.2578693623136,
            "unit": "iter/sec",
            "range": "stddev: 0.053229875530078984",
            "extra": "mean: 19.509199512987006 msec\nrounds: 308"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 21.060802499884606,
            "unit": "iter/sec",
            "range": "stddev: 0.06663299965471572",
            "extra": "mean: 47.481571512076954 msec\nrounds: 207"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 5.5676340918352185,
            "unit": "iter/sec",
            "range": "stddev: 0.2320406224859751",
            "extra": "mean: 179.60950441525466 msec\nrounds: 118"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.477200105049217,
            "unit": "iter/sec",
            "range": "stddev: 0.007675276271258486",
            "extra": "mean: 46.56100400000014 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 19.00497949824244,
            "unit": "iter/sec",
            "range": "stddev: 0.006020508287303626",
            "extra": "mean: 52.61778893749813 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 34.93005019922934,
            "unit": "iter/sec",
            "range": "stddev: 0.004343322300004094",
            "extra": "mean: 28.62864480000269 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 29.787415555655176,
            "unit": "iter/sec",
            "range": "stddev: 0.004271614939677754",
            "extra": "mean: 33.57122400000053 msec\nrounds: 22"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 23.52754805577793,
            "unit": "iter/sec",
            "range": "stddev: 0.00649975001990095",
            "extra": "mean: 42.5033665909108 msec\nrounds: 22"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 209.61664291378193,
            "unit": "iter/sec",
            "range": "stddev: 0.000824716475384087",
            "extra": "mean: 4.770613564359548 msec\nrounds: 101"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 35.92057928647786,
            "unit": "iter/sec",
            "range": "stddev: 0.18174450816671864",
            "extra": "mean: 27.839194686274045 msec\nrounds: 306"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 19.609137032704982,
            "unit": "iter/sec",
            "range": "stddev: 0.08868638994005211",
            "extra": "mean: 50.996634799999406 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1087.656185274311,
            "unit": "iter/sec",
            "range": "stddev: 0.000027914815006011996",
            "extra": "mean: 919.408185729019 usec\nrounds: 953"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 343.9018735148748,
            "unit": "iter/sec",
            "range": "stddev: 0.00022276184947571203",
            "extra": "mean: 2.907806200005325 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 62.83483963618035,
            "unit": "iter/sec",
            "range": "stddev: 0.025360435066339164",
            "extra": "mean: 15.914737839550389 msec\nrounds: 268"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 27.085572935500064,
            "unit": "iter/sec",
            "range": "stddev: 0.04808837519719052",
            "extra": "mean: 36.92002389542725 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 25.525433315378933,
            "unit": "iter/sec",
            "range": "stddev: 0.10381204656753522",
            "extra": "mean: 39.176612112496656 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 4.9377391809175855,
            "unit": "iter/sec",
            "range": "stddev: 0.12313329474757424",
            "extra": "mean: 202.52183506666483 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.228637656355214,
            "unit": "iter/sec",
            "range": "stddev: 0.2221929804666495",
            "extra": "mean: 191.25440807406827 msec\nrounds: 27"
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
          "id": "8a1cd8c7f28c0ed9e5763a5d09eeb3a20f28a571",
          "message": "Merge pull request #206 from zeroae/ci/adr-enforcer\n\nci(adr): add Claude ADR Enforcer workflow",
          "timestamp": "2026-01-19T09:25:12-05:00",
          "tree_id": "0a765a249b6050ef9e3694cd86d2f1c32b450d00",
          "url": "https://github.com/zeroae/zae-limiter/commit/8a1cd8c7f28c0ed9e5763a5d09eeb3a20f28a571"
        },
        "date": 1768833071826,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 68.08909922511224,
            "unit": "iter/sec",
            "range": "stddev: 0.022751465681446117",
            "extra": "mean: 14.686638703999563 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 19.737661722149618,
            "unit": "iter/sec",
            "range": "stddev: 0.07499242493279232",
            "extra": "mean: 50.664562706422274 msec\nrounds: 218"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 22.77442148734609,
            "unit": "iter/sec",
            "range": "stddev: 0.12701315704817406",
            "extra": "mean: 43.90890897297301 msec\nrounds: 148"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1062.483272874927,
            "unit": "iter/sec",
            "range": "stddev: 0.00028109296449879755",
            "extra": "mean: 941.1912879288383 usec\nrounds: 903"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 55.89176069096278,
            "unit": "iter/sec",
            "range": "stddev: 0.07149092066517587",
            "extra": "mean: 17.89172478443127 msec\nrounds: 167"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 59.431517111822494,
            "unit": "iter/sec",
            "range": "stddev: 0.02808313217867491",
            "extra": "mean: 16.826089061776177 msec\nrounds: 259"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 25.649851461518963,
            "unit": "iter/sec",
            "range": "stddev: 0.07032977623428782",
            "extra": "mean: 38.98658054609962 msec\nrounds: 141"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 56.68663761643868,
            "unit": "iter/sec",
            "range": "stddev: 0.032020235615396846",
            "extra": "mean: 17.640841687706803 msec\nrounds: 301"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 19.783513728008653,
            "unit": "iter/sec",
            "range": "stddev: 0.08601759513256103",
            "extra": "mean: 50.54713807407441 msec\nrounds: 216"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 8.863939387873355,
            "unit": "iter/sec",
            "range": "stddev: 0.20062441816696786",
            "extra": "mean: 112.81665591803205 msec\nrounds: 61"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.67867689936443,
            "unit": "iter/sec",
            "range": "stddev: 0.00899848641422922",
            "extra": "mean: 46.12827639999182 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 9.005109806007136,
            "unit": "iter/sec",
            "range": "stddev: 0.2573065012416638",
            "extra": "mean: 111.0480628823559 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 36.0933534891289,
            "unit": "iter/sec",
            "range": "stddev: 0.0033668198962327226",
            "extra": "mean: 27.70593207143232 msec\nrounds: 28"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 27.626673086373295,
            "unit": "iter/sec",
            "range": "stddev: 0.004227872454220414",
            "extra": "mean: 36.19690278570837 msec\nrounds: 28"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 24.309283316626555,
            "unit": "iter/sec",
            "range": "stddev: 0.005595402695068938",
            "extra": "mean: 41.13654799999969 msec\nrounds: 23"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 178.57182291751428,
            "unit": "iter/sec",
            "range": "stddev: 0.001126147569208129",
            "extra": "mean: 5.599987633334062 msec\nrounds: 120"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 56.98758083441403,
            "unit": "iter/sec",
            "range": "stddev: 0.032741405360411806",
            "extra": "mean: 17.547682940001437 msec\nrounds: 300"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 20.31934810644116,
            "unit": "iter/sec",
            "range": "stddev: 0.08976989391410198",
            "extra": "mean: 49.21417728371923 msec\nrounds: 215"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1091.3050495715818,
            "unit": "iter/sec",
            "range": "stddev: 0.00005428470783944187",
            "extra": "mean: 916.3340721208742 usec\nrounds: 929"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 46.605860405212944,
            "unit": "iter/sec",
            "range": "stddev: 0.09413569369334654",
            "extra": "mean: 21.45652909967838 msec\nrounds: 311"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 53.70576584257641,
            "unit": "iter/sec",
            "range": "stddev: 0.05589204473940297",
            "extra": "mean: 18.61997467704349 msec\nrounds: 257"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 31.84984603945862,
            "unit": "iter/sec",
            "range": "stddev: 0.08181727141064477",
            "extra": "mean: 31.397326026666025 msec\nrounds: 75"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 30.064130371473762,
            "unit": "iter/sec",
            "range": "stddev: 0.045506940605557517",
            "extra": "mean: 33.26222936249792 msec\nrounds: 80"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 16.062742924292717,
            "unit": "iter/sec",
            "range": "stddev: 0.012484617777188315",
            "extra": "mean: 62.25586779999048 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.902041113251294,
            "unit": "iter/sec",
            "range": "stddev: 0.1543271292221803",
            "extra": "mean: 203.99665708571482 msec\nrounds: 35"
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
          "id": "2928225f111f8885fd294bb03f18faceffe9fe5a",
          "message": "🔧 chore(ci): add annotation demo workflow (#210)\n\nDemonstrates GitHub Actions annotation features:\n- notice/warning/error annotations\n- file/line references for inline PR comments\n- grouped output sections\n- debug messages and secret masking\n- problem matchers (single and multiline)\n- job summaries with markdown\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-20T22:56:55-05:00",
          "tree_id": "799c2778dd5759b4d5f98605adbab476b1d0cd06",
          "url": "https://github.com/zeroae/zae-limiter/commit/2928225f111f8885fd294bb03f18faceffe9fe5a"
        },
        "date": 1768968239578,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 68.74256759951257,
            "unit": "iter/sec",
            "range": "stddev: 0.02346616295585591",
            "extra": "mean: 14.547027190283341 msec\nrounds: 247"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 21.871373990264082,
            "unit": "iter/sec",
            "range": "stddev: 0.07197906335338791",
            "extra": "mean: 45.72186459090976 msec\nrounds: 220"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 23.25122691577593,
            "unit": "iter/sec",
            "range": "stddev: 0.12395270583538902",
            "extra": "mean: 43.00848310596036 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1086.3327136316566,
            "unit": "iter/sec",
            "range": "stddev: 0.0000573952802307806",
            "extra": "mean: 920.5282943721334 usec\nrounds: 924"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 73.2063618111517,
            "unit": "iter/sec",
            "range": "stddev: 0.018776628051066398",
            "extra": "mean: 13.660014994047524 msec\nrounds: 168"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 62.73945673994621,
            "unit": "iter/sec",
            "range": "stddev: 0.0299513187712161",
            "extra": "mean: 15.938933040892909 msec\nrounds: 269"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 28.772665062197426,
            "unit": "iter/sec",
            "range": "stddev: 0.045519149474272914",
            "extra": "mean: 34.75520942666643 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 54.29193947604138,
            "unit": "iter/sec",
            "range": "stddev: 0.056115544581853416",
            "extra": "mean: 18.418940447712178 msec\nrounds: 306"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 21.349422523480015,
            "unit": "iter/sec",
            "range": "stddev: 0.07442537615770306",
            "extra": "mean: 46.839674417432306 msec\nrounds: 218"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.261276291246638,
            "unit": "iter/sec",
            "range": "stddev: 0.2277292687172841",
            "extra": "mean: 159.71184683193354 msec\nrounds: 119"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 22.329242033979646,
            "unit": "iter/sec",
            "range": "stddev: 0.008750505692051352",
            "extra": "mean: 44.78432355555305 msec\nrounds: 9"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.87450242986679,
            "unit": "iter/sec",
            "range": "stddev: 0.00520558682825305",
            "extra": "mean: 47.905333473684216 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 35.58546015973609,
            "unit": "iter/sec",
            "range": "stddev: 0.005446553093317924",
            "extra": "mean: 28.101364869561838 msec\nrounds: 23"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 28.188241432345492,
            "unit": "iter/sec",
            "range": "stddev: 0.003949371630424738",
            "extra": "mean: 35.475785263160056 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 23.80479907216164,
            "unit": "iter/sec",
            "range": "stddev: 0.004898402574612035",
            "extra": "mean: 42.00833609091215 msec\nrounds: 22"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 178.22895087016357,
            "unit": "iter/sec",
            "range": "stddev: 0.0010488323228726257",
            "extra": "mean: 5.61076073846432 msec\nrounds: 130"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 33.81330319844469,
            "unit": "iter/sec",
            "range": "stddev: 0.19200476391834193",
            "extra": "mean: 29.574158848993992 msec\nrounds: 298"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 18.718838126917376,
            "unit": "iter/sec",
            "range": "stddev: 0.09417082883679953",
            "extra": "mean: 53.42211910909239 msec\nrounds: 220"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1078.0550113763027,
            "unit": "iter/sec",
            "range": "stddev: 0.000039088726347159545",
            "extra": "mean: 927.5964486481506 usec\nrounds: 925"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 47.26142756287791,
            "unit": "iter/sec",
            "range": "stddev: 0.1554593684808588",
            "extra": "mean: 21.158903815792957 msec\nrounds: 114"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 54.05777775440986,
            "unit": "iter/sec",
            "range": "stddev: 0.03338389181439926",
            "extra": "mean: 18.49872565134114 msec\nrounds: 261"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 24.408105813404724,
            "unit": "iter/sec",
            "range": "stddev: 0.0788450021821614",
            "extra": "mean: 40.96999610067278 msec\nrounds: 149"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 26.48414926216832,
            "unit": "iter/sec",
            "range": "stddev: 0.10556467875942471",
            "extra": "mean: 37.75843392592811 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.039516521644465,
            "unit": "iter/sec",
            "range": "stddev: 0.12371733366108954",
            "extra": "mean: 198.4317336206859 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.942015214061085,
            "unit": "iter/sec",
            "range": "stddev: 0.14413425724311754",
            "extra": "mean: 202.34660491428423 msec\nrounds: 35"
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
          "id": "2dfe8212a40802ffdd6f5e7b21e00d348e268668",
          "message": "✨ feat(ci): add ADR audit result script for CI pass/fail (#211)\n\n- Document GitHub Actions annotations in audit.md (notice, warning, error, group, GITHUB_STEP_SUMMARY)\n- Update workflow to execute .claude/audit-result.sh for proper exit codes\n- Annotations appear inline on PR diffs with file/line references\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-20T23:49:26-05:00",
          "tree_id": "ea5c1d5be83f3de7ce3bdce17a9517879266749b",
          "url": "https://github.com/zeroae/zae-limiter/commit/2dfe8212a40802ffdd6f5e7b21e00d348e268668"
        },
        "date": 1768971345053,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 66.3043468090985,
            "unit": "iter/sec",
            "range": "stddev: 0.024138748331376823",
            "extra": "mean: 15.081967444444784 msec\nrounds: 243"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 20.11853455138696,
            "unit": "iter/sec",
            "range": "stddev: 0.071133432318712",
            "extra": "mean: 49.70540957870416 msec\nrounds: 216"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 21.78862823439242,
            "unit": "iter/sec",
            "range": "stddev: 0.1432071037924896",
            "extra": "mean: 45.89550059060362 msec\nrounds: 149"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1071.8380453855261,
            "unit": "iter/sec",
            "range": "stddev: 0.00002936994028670942",
            "extra": "mean: 932.9767722886838 usec\nrounds: 931"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 68.15277146476133,
            "unit": "iter/sec",
            "range": "stddev: 0.01761278552468085",
            "extra": "mean: 14.672917601260782 msec\nrounds: 158"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 54.639498118360734,
            "unit": "iter/sec",
            "range": "stddev: 0.029599918132396583",
            "extra": "mean: 18.301778647999072 msec\nrounds: 250"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 25.65980144916375,
            "unit": "iter/sec",
            "range": "stddev: 0.07030479741968457",
            "extra": "mean: 38.971462892305034 msec\nrounds: 130"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 47.48282635988014,
            "unit": "iter/sec",
            "range": "stddev: 0.051773646500569925",
            "extra": "mean: 21.06024591756261 msec\nrounds: 279"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 19.195337500928144,
            "unit": "iter/sec",
            "range": "stddev: 0.085812625374816",
            "extra": "mean: 52.095984243655394 msec\nrounds: 197"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 5.847722426389252,
            "unit": "iter/sec",
            "range": "stddev: 0.20467602254394976",
            "extra": "mean: 171.00674879629375 msec\nrounds: 108"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 20.539942885935528,
            "unit": "iter/sec",
            "range": "stddev: 0.009845994020713022",
            "extra": "mean: 48.68562710000219 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 19.906451388158295,
            "unit": "iter/sec",
            "range": "stddev: 0.005067745948625183",
            "extra": "mean: 50.23497058822185 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 31.593061776627284,
            "unit": "iter/sec",
            "range": "stddev: 0.003576100748702577",
            "extra": "mean: 31.65251937499155 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 26.25733218769982,
            "unit": "iter/sec",
            "range": "stddev: 0.004917463355745119",
            "extra": "mean: 38.08460024999979 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 23.08759345222829,
            "unit": "iter/sec",
            "range": "stddev: 0.003830159555306923",
            "extra": "mean: 43.31330600000172 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 208.76528326946126,
            "unit": "iter/sec",
            "range": "stddev: 0.0007486776464037382",
            "extra": "mean: 4.790068465115735 msec\nrounds: 129"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 54.211212705830064,
            "unit": "iter/sec",
            "range": "stddev: 0.0387347862392228",
            "extra": "mean: 18.446368381876404 msec\nrounds: 309"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 19.326462084471167,
            "unit": "iter/sec",
            "range": "stddev: 0.09270258780186265",
            "extra": "mean: 51.74252771300035 msec\nrounds: 223"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1115.7335408100278,
            "unit": "iter/sec",
            "range": "stddev: 0.000054561811069991905",
            "extra": "mean: 896.271343849711 usec\nrounds: 951"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 46.4623226154346,
            "unit": "iter/sec",
            "range": "stddev: 0.09928303014566671",
            "extra": "mean: 21.522815556960637 msec\nrounds: 316"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 53.602407784010836,
            "unit": "iter/sec",
            "range": "stddev: 0.05888207207988026",
            "extra": "mean: 18.655878370790123 msec\nrounds: 267"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 24.86536271097901,
            "unit": "iter/sec",
            "range": "stddev: 0.0750271359514535",
            "extra": "mean: 40.216586084966366 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 24.501468888903286,
            "unit": "iter/sec",
            "range": "stddev: 0.10617172243869737",
            "extra": "mean: 40.813879548784925 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.842264740996356,
            "unit": "iter/sec",
            "range": "stddev: 0.11740155098507822",
            "extra": "mean: 171.16649866665531 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.65121781723166,
            "unit": "iter/sec",
            "range": "stddev: 0.18369942910578058",
            "extra": "mean: 214.99745642855876 msec\nrounds: 35"
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
          "id": "4ea8ab72fe1f3aa0dddf76755b2ba64d96cfe937",
          "message": "🐛 fix(ci): allow file writes in ADR enforcer workflow (#212)\n\nAdd allowed_tools to permit Bash, Write, Read, Glob, Grep, Task\nso Claude can create the audit-result.sh script.\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-21T00:02:37-05:00",
          "tree_id": "33679573f99d07cae01d3d9fcb02ac0f19b4df52",
          "url": "https://github.com/zeroae/zae-limiter/commit/4ea8ab72fe1f3aa0dddf76755b2ba64d96cfe937"
        },
        "date": 1768972150571,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 73.66532578944565,
            "unit": "iter/sec",
            "range": "stddev: 0.021070966932186536",
            "extra": "mean: 13.574907723319598 msec\nrounds: 253"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 22.918879621627106,
            "unit": "iter/sec",
            "range": "stddev: 0.06672803828327355",
            "extra": "mean: 43.63215028436045 msec\nrounds: 211"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 23.855510670256905,
            "unit": "iter/sec",
            "range": "stddev: 0.11610062045290441",
            "extra": "mean: 41.9190355562919 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1065.031471149387,
            "unit": "iter/sec",
            "range": "stddev: 0.0002148931971466694",
            "extra": "mean: 938.9393901391433 usec\nrounds: 933"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 121.25099548518504,
            "unit": "iter/sec",
            "range": "stddev: 0.0016381778109359763",
            "extra": "mean: 8.24735496808506 msec\nrounds: 94"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 77.62750791616652,
            "unit": "iter/sec",
            "range": "stddev: 0.020114388227962327",
            "extra": "mean: 12.882031471111317 msec\nrounds: 225"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 26.29189963222704,
            "unit": "iter/sec",
            "range": "stddev: 0.05901003401392427",
            "extra": "mean: 38.03452827631594 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 55.68558552261053,
            "unit": "iter/sec",
            "range": "stddev: 0.053330930487200036",
            "extra": "mean: 17.957968666666186 msec\nrounds: 303"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 19.345545818793653,
            "unit": "iter/sec",
            "range": "stddev: 0.08849721335120832",
            "extra": "mean: 51.69148543891318 msec\nrounds: 221"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 9.36854161668042,
            "unit": "iter/sec",
            "range": "stddev: 0.2089570514712046",
            "extra": "mean: 106.74019937313709 msec\nrounds: 67"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 22.35687474304962,
            "unit": "iter/sec",
            "range": "stddev: 0.008393103119246747",
            "extra": "mean: 44.728970909088424 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 22.477823361725086,
            "unit": "iter/sec",
            "range": "stddev: 0.007741337814452047",
            "extra": "mean: 44.48829336842221 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 39.3935237889467,
            "unit": "iter/sec",
            "range": "stddev: 0.0034086765885391135",
            "extra": "mean: 25.384883194444935 msec\nrounds: 36"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 32.04803026426104,
            "unit": "iter/sec",
            "range": "stddev: 0.0035850294377081236",
            "extra": "mean: 31.203165740740353 msec\nrounds: 27"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 28.22133199546054,
            "unit": "iter/sec",
            "range": "stddev: 0.005998780231351004",
            "extra": "mean: 35.43418858333306 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 221.49169302269883,
            "unit": "iter/sec",
            "range": "stddev: 0.000754341753780985",
            "extra": "mean: 4.514842007630138 msec\nrounds: 131"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 62.495533245155734,
            "unit": "iter/sec",
            "range": "stddev: 0.02655673563101051",
            "extra": "mean: 16.001143570968953 msec\nrounds: 310"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 20.68490684891151,
            "unit": "iter/sec",
            "range": "stddev: 0.0784238062274806",
            "extra": "mean: 48.344428490990396 msec\nrounds: 222"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 396.90588462728283,
            "unit": "iter/sec",
            "range": "stddev: 0.04828454980764955",
            "extra": "mean: 2.5194889739139463 msec\nrounds: 920"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 60.7999439102205,
            "unit": "iter/sec",
            "range": "stddev: 0.0277712841490532",
            "extra": "mean: 16.44738359424538 msec\nrounds: 313"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 66.06672513285694,
            "unit": "iter/sec",
            "range": "stddev: 0.026054460739333277",
            "extra": "mean: 15.136212639404313 msec\nrounds: 269"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 25.114932179658688,
            "unit": "iter/sec",
            "range": "stddev: 0.07392603999870441",
            "extra": "mean: 39.816950045755206 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 27.497885337082916,
            "unit": "iter/sec",
            "range": "stddev: 0.10314107581476721",
            "extra": "mean: 36.366432827160956 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.349549245117449,
            "unit": "iter/sec",
            "range": "stddev: 0.1140731766624763",
            "extra": "mean: 186.9316374482772 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.265978996707109,
            "unit": "iter/sec",
            "range": "stddev: 0.1789767644008442",
            "extra": "mean: 189.89821277777867 msec\nrounds: 27"
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
          "id": "f1fff23b71daac9e3447c7f9dda2dc5df5343ad2",
          "message": "🐛 fix(ci): use .github/audit-result.sh path and claude_args (#213)\n\n- Change output path from .claude/ to .github/ (sensitive dir blocked)\n- Use claude_args with --allowedTools instead of invalid allowed_tools\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-21T00:49:32-05:00",
          "tree_id": "fd261c432ff80bd691bd529b6eb524d43e6db428",
          "url": "https://github.com/zeroae/zae-limiter/commit/f1fff23b71daac9e3447c7f9dda2dc5df5343ad2"
        },
        "date": 1768974943046,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 79.71686411027154,
            "unit": "iter/sec",
            "range": "stddev: 0.017716225666908884",
            "extra": "mean: 12.544397112971103 msec\nrounds: 239"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 187.74075404960553,
            "unit": "iter/sec",
            "range": "stddev: 0.00026421666422734083",
            "extra": "mean: 5.3264939999962735 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 30.003683949037,
            "unit": "iter/sec",
            "range": "stddev: 0.039002541336465704",
            "extra": "mean: 33.329240559211264 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1073.881426150864,
            "unit": "iter/sec",
            "range": "stddev: 0.000034172405027453936",
            "extra": "mean: 931.2015047921271 usec\nrounds: 939"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 76.1453643390033,
            "unit": "iter/sec",
            "range": "stddev: 0.016581219275460628",
            "extra": "mean: 13.132775825301009 msec\nrounds: 166"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 62.52903200834956,
            "unit": "iter/sec",
            "range": "stddev: 0.031672295772627396",
            "extra": "mean: 15.992571256603958 msec\nrounds: 265"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 26.967263839252553,
            "unit": "iter/sec",
            "range": "stddev: 0.06493397698478089",
            "extra": "mean: 37.081997119204836 msec\nrounds: 151"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 51.59222352974036,
            "unit": "iter/sec",
            "range": "stddev: 0.05453907974263481",
            "extra": "mean: 19.382766075657692 msec\nrounds: 304"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 34.98755592039601,
            "unit": "iter/sec",
            "range": "stddev: 0.08351351137179372",
            "extra": "mean: 28.581590616824126 msec\nrounds: 107"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 59.57499721246064,
            "unit": "iter/sec",
            "range": "stddev: 0.0025681312250612786",
            "extra": "mean: 16.785565200007113 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.842631936147274,
            "unit": "iter/sec",
            "range": "stddev: 0.005532925755627138",
            "extra": "mean: 45.78202860000147 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.26386698277193,
            "unit": "iter/sec",
            "range": "stddev: 0.00585624037828646",
            "extra": "mean: 47.02813466667206 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 35.19737389036356,
            "unit": "iter/sec",
            "range": "stddev: 0.006563319172618151",
            "extra": "mean: 28.411210538459606 msec\nrounds: 26"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 28.809827354779742,
            "unit": "iter/sec",
            "range": "stddev: 0.005435565428812243",
            "extra": "mean: 34.71037808333458 msec\nrounds: 24"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 25.44653775297841,
            "unit": "iter/sec",
            "range": "stddev: 0.004810335375320463",
            "extra": "mean: 39.29807700000187 msec\nrounds: 22"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 219.7002452518662,
            "unit": "iter/sec",
            "range": "stddev: 0.0005432749702392155",
            "extra": "mean: 4.551656275365518 msec\nrounds: 138"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 52.78251597026362,
            "unit": "iter/sec",
            "range": "stddev: 0.0344867537176643",
            "extra": "mean: 18.945667549522945 msec\nrounds: 313"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 20.261094322495644,
            "unit": "iter/sec",
            "range": "stddev: 0.08581933757530484",
            "extra": "mean: 49.355675665046 msec\nrounds: 206"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1067.5522212153555,
            "unit": "iter/sec",
            "range": "stddev: 0.00009092662101546733",
            "extra": "mean: 936.7223262029742 usec\nrounds: 935"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 53.890807845936436,
            "unit": "iter/sec",
            "range": "stddev: 0.03113794695397029",
            "extra": "mean: 18.556040259385416 msec\nrounds: 293"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 54.088649976568476,
            "unit": "iter/sec",
            "range": "stddev: 0.059970463672131644",
            "extra": "mean: 18.488167118854065 msec\nrounds: 244"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 24.231847282913616,
            "unit": "iter/sec",
            "range": "stddev: 0.07156733007517237",
            "extra": "mean: 41.26800521333431 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 24.355112001103294,
            "unit": "iter/sec",
            "range": "stddev: 0.1008355991756502",
            "extra": "mean: 41.059141914629656 msec\nrounds: 82"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 4.500871268714699,
            "unit": "iter/sec",
            "range": "stddev: 0.15122228991386627",
            "extra": "mean: 222.17920493548513 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 5.079837261301208,
            "unit": "iter/sec",
            "range": "stddev: 0.22290940565476544",
            "extra": "mean: 196.85670004000258 msec\nrounds: 25"
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
          "id": "e2c6fb2988787ddbb1cfa1a417c3cb9cb4617f52",
          "message": "🐛 fix(deps): move type stubs to dev dependencies (#215)\n\nboto3-stubs and types-aiobotocore are only needed for static type\nchecking, not at runtime. Having them as runtime dependencies broke\nthe conda-forge build since boto3-stubs isn't available on conda-forge.\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-21T04:18:15-05:00",
          "tree_id": "4d08b99c51689ed95f89e8b1be28ca279b9c136a",
          "url": "https://github.com/zeroae/zae-limiter/commit/e2c6fb2988787ddbb1cfa1a417c3cb9cb4617f52"
        },
        "date": 1768987459052,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 74.95166992448857,
            "unit": "iter/sec",
            "range": "stddev: 0.018444551533833346",
            "extra": "mean: 13.341930887029845 msec\nrounds: 239"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 186.9978173613515,
            "unit": "iter/sec",
            "range": "stddev: 0.00023774883162019984",
            "extra": "mean: 5.347656000003553 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 29.692225170584038,
            "unit": "iter/sec",
            "range": "stddev: 0.03704050735674797",
            "extra": "mean: 33.67885007792194 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1085.7582281983518,
            "unit": "iter/sec",
            "range": "stddev: 0.00004367221148538052",
            "extra": "mean: 921.0153550107979 usec\nrounds: 938"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 74.7189948132911,
            "unit": "iter/sec",
            "range": "stddev: 0.017778128139059168",
            "extra": "mean: 13.383477688622744 msec\nrounds: 167"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 63.00268521024042,
            "unit": "iter/sec",
            "range": "stddev: 0.03137266131669155",
            "extra": "mean: 15.872339356060658 msec\nrounds: 264"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 27.067125178487505,
            "unit": "iter/sec",
            "range": "stddev: 0.06341217871211044",
            "extra": "mean: 36.945186953019416 msec\nrounds: 149"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 74.01154468458037,
            "unit": "iter/sec",
            "range": "stddev: 0.02155645854219867",
            "extra": "mean: 13.511405609243296 msec\nrounds: 238"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 20.27965616702303,
            "unit": "iter/sec",
            "range": "stddev: 0.07889420847143448",
            "extra": "mean: 49.31050071875039 msec\nrounds: 224"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 6.148736190473656,
            "unit": "iter/sec",
            "range": "stddev: 0.22639126691262587",
            "extra": "mean: 162.6350471092446 msec\nrounds: 119"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 19.645944443390086,
            "unit": "iter/sec",
            "range": "stddev: 0.008769671526601283",
            "extra": "mean: 50.90109069999187 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.172001020031253,
            "unit": "iter/sec",
            "range": "stddev: 0.004730430798836525",
            "extra": "mean: 47.23219118749711 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 37.08472888712536,
            "unit": "iter/sec",
            "range": "stddev: 0.003982286955733945",
            "extra": "mean: 26.965277352942127 msec\nrounds: 34"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 28.63418616269371,
            "unit": "iter/sec",
            "range": "stddev: 0.006455294213453983",
            "extra": "mean: 34.92329044444289 msec\nrounds: 27"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 26.203608784107075,
            "unit": "iter/sec",
            "range": "stddev: 0.004029274747043993",
            "extra": "mean: 38.16268240909308 msec\nrounds: 22"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 196.33663954518292,
            "unit": "iter/sec",
            "range": "stddev: 0.0008633391121795935",
            "extra": "mean: 5.093292837834632 msec\nrounds: 148"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 61.9048931614571,
            "unit": "iter/sec",
            "range": "stddev: 0.027354216270699467",
            "extra": "mean: 16.153811902911333 msec\nrounds: 309"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 19.04518667179408,
            "unit": "iter/sec",
            "range": "stddev: 0.09129318682603653",
            "extra": "mean: 52.50670509210602 msec\nrounds: 228"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1080.2028941317092,
            "unit": "iter/sec",
            "range": "stddev: 0.00022514040152230618",
            "extra": "mean: 925.752009583183 usec\nrounds: 939"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 49.659353902634656,
            "unit": "iter/sec",
            "range": "stddev: 0.09792634263366852",
            "extra": "mean: 20.137193124998458 msec\nrounds: 312"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 54.48428431899635,
            "unit": "iter/sec",
            "range": "stddev: 0.05661602028781848",
            "extra": "mean: 18.35391640909088 msec\nrounds: 264"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 26.32572421064541,
            "unit": "iter/sec",
            "range": "stddev: 0.07116841820393024",
            "extra": "mean: 37.98565965359566 msec\nrounds: 153"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 26.915485671063337,
            "unit": "iter/sec",
            "range": "stddev: 0.1059951757539136",
            "extra": "mean: 37.153332925925746 msec\nrounds: 81"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.749218610475384,
            "unit": "iter/sec",
            "range": "stddev: 0.11802532808276245",
            "extra": "mean: 173.93668040000193 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.715138828838253,
            "unit": "iter/sec",
            "range": "stddev: 0.16866485118757377",
            "extra": "mean: 212.08283282856945 msec\nrounds: 35"
          }
        ]
      },
      {
        "commit": {
          "author": {
            "email": "patrick@zero-ae.com",
            "name": "Patrick Sodré",
            "username": "sodre"
          },
          "committer": {
            "email": "noreply@github.com",
            "name": "GitHub",
            "username": "web-flow"
          },
          "distinct": true,
          "id": "5329a39e603105b70a22ce1de0d50039bbf6c8c9",
          "message": "🔧 chore(ci): limit code review to main branch and disable ADR enforcer (#221)\n\n- Add branch filter to claude-code-review.yml to only run on PRs against main/master\n- Disable claude-adr-enforcer.yml by renaming to .disabled\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-22T10:01:52-05:00",
          "tree_id": "0f4c0c17d150bbde4e84afa3c1007a7f98e38cd4",
          "url": "https://github.com/zeroae/zae-limiter/commit/5329a39e603105b70a22ce1de0d50039bbf6c8c9"
        },
        "date": 1769094515026,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_single_limit_latency",
            "value": 72.16834834335164,
            "unit": "iter/sec",
            "range": "stddev: 0.02184018449729952",
            "extra": "mean: 13.856490039682653 msec\nrounds: 252"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_two_limits_latency",
            "value": 22.433561518650023,
            "unit": "iter/sec",
            "range": "stddev: 0.06747191729666722",
            "extra": "mean: 44.57606961643854 msec\nrounds: 219"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_cascade_latency",
            "value": 23.910497723756507,
            "unit": "iter/sec",
            "range": "stddev: 0.116416292473531",
            "extra": "mean: 41.82263420666648 msec\nrounds: 150"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_available_check_latency",
            "value": 1070.6527371341006,
            "unit": "iter/sec",
            "range": "stddev: 0.00005278469229303141",
            "extra": "mean: 934.0096609445726 usec\nrounds: 932"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyBenchmarks::test_acquire_with_stored_limits_latency",
            "value": 57.62708989563782,
            "unit": "iter/sec",
            "range": "stddev: 0.06939141572918946",
            "extra": "mean: 17.352949833333447 msec\nrounds: 168"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_baseline_no_cascade",
            "value": 61.82503843809897,
            "unit": "iter/sec",
            "range": "stddev: 0.03234456283873111",
            "extra": "mean: 16.174676559258902 msec\nrounds: 270"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_with_cascade",
            "value": 27.29366338653585,
            "unit": "iter/sec",
            "range": "stddev: 0.06622174131185875",
            "extra": "mean: 36.63854081578901 msec\nrounds: 152"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_one_limit",
            "value": 55.7349826604573,
            "unit": "iter/sec",
            "range": "stddev: 0.05113626539516904",
            "extra": "mean: 17.9420527694804 msec\nrounds: 308"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_two_limits",
            "value": 22.58897585523877,
            "unit": "iter/sec",
            "range": "stddev: 0.06920756670258724",
            "extra": "mean: 44.26938195022609 msec\nrounds: 221"
          },
          {
            "name": "tests/benchmark/test_latency.py::TestLatencyComparison::test_five_limits",
            "value": 10.131956990103442,
            "unit": "iter/sec",
            "range": "stddev: 0.21342699878481247",
            "extra": "mean: 98.69761596666535 msec\nrounds: 60"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.98256546026539,
            "unit": "iter/sec",
            "range": "stddev: 0.008482730602581137",
            "extra": "mean: 45.49059580000119 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.78914204199935,
            "unit": "iter/sec",
            "range": "stddev: 0.006897454045274389",
            "extra": "mean: 45.89441833333612 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 36.85454352611764,
            "unit": "iter/sec",
            "range": "stddev: 0.003156352491457964",
            "extra": "mean: 27.133696535715654 msec\nrounds: 28"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 30.56576706041058,
            "unit": "iter/sec",
            "range": "stddev: 0.0034048971193439636",
            "extra": "mean: 32.716339099999914 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 23.584785244476144,
            "unit": "iter/sec",
            "range": "stddev: 0.00544412716999813",
            "extra": "mean: 42.400216479995834 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 218.68021644012245,
            "unit": "iter/sec",
            "range": "stddev: 0.000760600569620854",
            "extra": "mean: 4.572887370786983 msec\nrounds: 89"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_single_limit",
            "value": 54.09982399617512,
            "unit": "iter/sec",
            "range": "stddev: 0.035776026522069126",
            "extra": "mean: 18.48434849013003 msec\nrounds: 304"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestAcquireReleaseBenchmarks::test_acquire_release_multiple_limits",
            "value": 21.008182774611576,
            "unit": "iter/sec",
            "range": "stddev: 0.08453360391032985",
            "extra": "mean: 47.60049980184396 msec\nrounds: 217"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_available_check",
            "value": 1097.5626453438815,
            "unit": "iter/sec",
            "range": "stddev: 0.00021518051836743967",
            "extra": "mean: 911.1097250277558 usec\nrounds: 931"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestTransactionOverheadBenchmarks::test_transactional_acquire",
            "value": 50.59850171748199,
            "unit": "iter/sec",
            "range": "stddev: 0.08577857092389338",
            "extra": "mean: 19.763431051447437 msec\nrounds: 311"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_without_cascade",
            "value": 58.72544940503264,
            "unit": "iter/sec",
            "range": "stddev: 0.05344880518207147",
            "extra": "mean: 17.02839246240493 msec\nrounds: 266"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_acquire_with_cascade",
            "value": 26.425351895211186,
            "unit": "iter/sec",
            "range": "stddev: 0.07224756252192681",
            "extra": "mean: 37.842447811687244 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestCascadeOverheadBenchmarks::test_cascade_with_stored_limits",
            "value": 27.58819350346142,
            "unit": "iter/sec",
            "range": "stddev: 0.10202368102076487",
            "extra": "mean: 36.2473896623399 msec\nrounds: 77"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_sequential_acquisitions",
            "value": 5.884575417501932,
            "unit": "iter/sec",
            "range": "stddev: 0.10294830881766692",
            "extra": "mean: 169.93579469230616 msec\nrounds: 26"
          },
          {
            "name": "tests/benchmark/test_operations.py::TestConcurrentThroughputBenchmarks::test_same_entity_sequential",
            "value": 4.641569261648185,
            "unit": "iter/sec",
            "range": "stddev: 0.1894963325713023",
            "extra": "mean: 215.4443774571421 msec\nrounds: 35"
          }
        ]
      }
    ]
  }
}