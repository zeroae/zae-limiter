window.BENCHMARK_DATA = {
  "lastUpdate": 1768401141549,
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
      }
    ]
  }
}