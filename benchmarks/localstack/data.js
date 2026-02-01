window.BENCHMARK_DATA = {
  "lastUpdate": 1769928824851,
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
          "id": "1172e428bd1abbd8313b32de93d699b223c9e730",
          "message": "Merge pull request #203 from zeroae/release/0.5.0\n\n🔖 chore: release prep v0.5.0",
          "timestamp": "2026-01-25T05:07:56-05:00",
          "tree_id": "06e57f0bd18a4f6affe597179c482fc7baf146e0",
          "url": "https://github.com/zeroae/zae-limiter/commit/1172e428bd1abbd8313b32de93d699b223c9e730"
        },
        "date": 1769336461618,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 18.122454227851282,
            "unit": "iter/sec",
            "range": "stddev: 0.010958511445143612",
            "extra": "mean: 55.18016420001004 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 22.700517201206857,
            "unit": "iter/sec",
            "range": "stddev: 0.007476778626306889",
            "extra": "mean: 44.05185974999881 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 38.40489360685244,
            "unit": "iter/sec",
            "range": "stddev: 0.004499835682193155",
            "extra": "mean: 26.038348399995925 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 33.369082434668876,
            "unit": "iter/sec",
            "range": "stddev: 0.007439228487032043",
            "extra": "mean: 29.967860277783604 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 30.519520863385402,
            "unit": "iter/sec",
            "range": "stddev: 0.003957195144191171",
            "extra": "mean: 32.76591413332805 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 227.30712355127758,
            "unit": "iter/sec",
            "range": "stddev: 0.0006852030493608422",
            "extra": "mean: 4.399334188813545 msec\nrounds: 143"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 34.71663118534857,
            "unit": "iter/sec",
            "range": "stddev: 0.006303486225139077",
            "extra": "mean: 28.804638176472295 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 23.762919879118787,
            "unit": "iter/sec",
            "range": "stddev: 0.007690972785466134",
            "extra": "mean: 42.08237056249686 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 34.89214837993728,
            "unit": "iter/sec",
            "range": "stddev: 0.004777943319630124",
            "extra": "mean: 28.659742848479695 msec\nrounds: 33"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 14.081152389495035,
            "unit": "iter/sec",
            "range": "stddev: 0.005605943530861599",
            "extra": "mean: 71.01691483333639 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 35.113637198541284,
            "unit": "iter/sec",
            "range": "stddev: 0.0037945515747656003",
            "extra": "mean: 28.478963724143703 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9169222277029903,
            "unit": "iter/sec",
            "range": "stddev: 0.009959487380023252",
            "extra": "mean: 521.6695729999856 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9366589484055068,
            "unit": "iter/sec",
            "range": "stddev: 0.0007206355174445323",
            "extra": "mean: 516.3531766000006 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9416635344191245,
            "unit": "iter/sec",
            "range": "stddev: 0.009448933221210855",
            "extra": "mean: 1.0619504349999715 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.9222550980402642,
            "unit": "iter/sec",
            "range": "stddev: 0.005274811526811281",
            "extra": "mean: 1.0842986958000438 sec\nrounds: 5"
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
          "id": "9e4a8449bea5bbdd5f06537fe7b18c7ff4167fd5",
          "message": "🔧 chore(ci): skip claude code review for bot PRs (#230)\n\nBots like dependabot and renovate don't need AI code review.\nSkip the job when the PR author type is 'Bot'.\n\nFixes #226 workflow failure.\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-25T05:48:27-05:00",
          "tree_id": "1ec9047fd3830987d7ef393bc1fcc3eb327a2b30",
          "url": "https://github.com/zeroae/zae-limiter/commit/9e4a8449bea5bbdd5f06537fe7b18c7ff4167fd5"
        },
        "date": 1769338854511,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 21.195660071602056,
            "unit": "iter/sec",
            "range": "stddev: 0.0031328881572121416",
            "extra": "mean: 47.17946959999608 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 25.38414724236223,
            "unit": "iter/sec",
            "range": "stddev: 0.0032963161190171586",
            "extra": "mean: 39.39466590908968 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 36.98465684818563,
            "unit": "iter/sec",
            "range": "stddev: 0.0056637757898994326",
            "extra": "mean: 27.038239238092522 msec\nrounds: 21"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 33.866727181069784,
            "unit": "iter/sec",
            "range": "stddev: 0.007555560802222053",
            "extra": "mean: 29.527506294111642 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 30.92950233507266,
            "unit": "iter/sec",
            "range": "stddev: 0.004320337638408308",
            "extra": "mean: 32.331590374994335 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 225.19843986965188,
            "unit": "iter/sec",
            "range": "stddev: 0.0007504598165380447",
            "extra": "mean: 4.440528098590801 msec\nrounds: 142"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 33.02535374571623,
            "unit": "iter/sec",
            "range": "stddev: 0.004690542350320777",
            "extra": "mean: 30.27976649999431 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 27.413947387846402,
            "unit": "iter/sec",
            "range": "stddev: 0.004152137375624574",
            "extra": "mean: 36.477782124997304 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 35.00993509099757,
            "unit": "iter/sec",
            "range": "stddev: 0.0031714294113267772",
            "extra": "mean: 28.563320594591424 msec\nrounds: 37"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 13.219019701359217,
            "unit": "iter/sec",
            "range": "stddev: 0.0057543828919987975",
            "extra": "mean: 75.64857475000035 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 29.6399689992978,
            "unit": "iter/sec",
            "range": "stddev: 0.004101849054654478",
            "extra": "mean: 33.73822691999749 msec\nrounds: 25"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9260670132165978,
            "unit": "iter/sec",
            "range": "stddev: 0.0010349084310271662",
            "extra": "mean: 519.1927348000036 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9244697321391107,
            "unit": "iter/sec",
            "range": "stddev: 0.002633957792454566",
            "extra": "mean: 519.6236570000337 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9477305781565214,
            "unit": "iter/sec",
            "range": "stddev: 0.0020958683707225263",
            "extra": "mean: 1.0551521951999803 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.9139557656837906,
            "unit": "iter/sec",
            "range": "stddev: 0.004305157144234695",
            "extra": "mean: 1.094144856399953 sec\nrounds: 5"
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
          "id": "e3e2ab3dc46f323afcd9c1c93a1b2f879df58c55",
          "message": "Merge pull request #240 from zeroae/chore/237-split-benchmark-ci-tiers\n\n👷 ci(benchmark): split benchmark job into moto/localstack/aws tiers",
          "timestamp": "2026-01-26T20:49:13-05:00",
          "tree_id": "d7ebad8e3e9eede40191741ede95137689e4f865",
          "url": "https://github.com/zeroae/zae-limiter/commit/e3e2ab3dc46f323afcd9c1c93a1b2f879df58c55"
        },
        "date": 1769479222522,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 18.786670335879663,
            "unit": "iter/sec",
            "range": "stddev: 0.009352216221946163",
            "extra": "mean: 53.229230199997346 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.961824408504334,
            "unit": "iter/sec",
            "range": "stddev: 0.007137618502379949",
            "extra": "mean: 45.53355775000038 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 35.214772205662875,
            "unit": "iter/sec",
            "range": "stddev: 0.00622812343495312",
            "extra": "mean: 28.397173611112848 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 32.20758560184901,
            "unit": "iter/sec",
            "range": "stddev: 0.0023131444523724026",
            "extra": "mean: 31.048586266664795 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 28.306063787064332,
            "unit": "iter/sec",
            "range": "stddev: 0.0042442907637721145",
            "extra": "mean: 35.32811935713198 msec\nrounds: 14"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 214.674621832266,
            "unit": "iter/sec",
            "range": "stddev: 0.0007959456592950405",
            "extra": "mean: 4.6582124680826995 msec\nrounds: 141"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 27.964274267504987,
            "unit": "iter/sec",
            "range": "stddev: 0.006337435881000411",
            "extra": "mean: 35.759912466672475 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 27.013469915589347,
            "unit": "iter/sec",
            "range": "stddev: 0.0036468358500324886",
            "extra": "mean: 37.01856900001228 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 26.99735021445612,
            "unit": "iter/sec",
            "range": "stddev: 0.04705510197268293",
            "extra": "mean: 37.040672216213856 msec\nrounds: 37"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 13.483078091488526,
            "unit": "iter/sec",
            "range": "stddev: 0.0048702972683863826",
            "extra": "mean: 74.16704058335692 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 31.282657446433895,
            "unit": "iter/sec",
            "range": "stddev: 0.0036238511939166306",
            "extra": "mean: 31.966593685729094 msec\nrounds: 35"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9212305983038975,
            "unit": "iter/sec",
            "range": "stddev: 0.003046179380788334",
            "extra": "mean: 520.4997260000027 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9342322272968155,
            "unit": "iter/sec",
            "range": "stddev: 0.0008251372727801358",
            "extra": "mean: 517.0010022000042 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.940855969441218,
            "unit": "iter/sec",
            "range": "stddev: 0.007071098523633261",
            "extra": "mean: 1.0628619389999812 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.9148230111058652,
            "unit": "iter/sec",
            "range": "stddev: 0.00785079494409552",
            "extra": "mean: 1.0931076151999832 sec\nrounds: 5"
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
          "id": "2676a7262d29e1114c02ae40bd0d64082fff166f",
          "message": "Merge pull request #241 from zeroae/feat/199-tags-resource-discovery\n\n✨ feat(infra): use tags instead of ZAEL- prefix for resource discovery",
          "timestamp": "2026-01-26T22:39:20-05:00",
          "tree_id": "7fb1a0a6847b91f4f2a9f5b23e5109b838c7321a",
          "url": "https://github.com/zeroae/zae-limiter/commit/2676a7262d29e1114c02ae40bd0d64082fff166f"
        },
        "date": 1769485824046,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 18.99373956087459,
            "unit": "iter/sec",
            "range": "stddev: 0.01751707796213719",
            "extra": "mean: 52.64892659999987 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 23.370012582245124,
            "unit": "iter/sec",
            "range": "stddev: 0.005457324158706334",
            "extra": "mean: 42.78987854545397 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 32.30162911651449,
            "unit": "iter/sec",
            "range": "stddev: 0.006265967906031071",
            "extra": "mean: 30.95819088235216 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 34.1304226180797,
            "unit": "iter/sec",
            "range": "stddev: 0.004408695551555609",
            "extra": "mean: 29.299373500001025 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 30.528750459330027,
            "unit": "iter/sec",
            "range": "stddev: 0.003397215081694118",
            "extra": "mean: 32.75600818750135 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 221.23921813719096,
            "unit": "iter/sec",
            "range": "stddev: 0.0006939332816220914",
            "extra": "mean: 4.519994277777179 msec\nrounds: 162"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 30.67165067518308,
            "unit": "iter/sec",
            "range": "stddev: 0.00433989910720962",
            "extra": "mean: 32.60339688235677 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 29.416466760147603,
            "unit": "iter/sec",
            "range": "stddev: 0.003006943149234462",
            "extra": "mean: 33.994565294114956 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 25.933988072820792,
            "unit": "iter/sec",
            "range": "stddev: 0.04855586177331456",
            "extra": "mean: 38.559437800004815 msec\nrounds: 35"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 13.167111570565686,
            "unit": "iter/sec",
            "range": "stddev: 0.010982287961806893",
            "extra": "mean: 75.94680083333098 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 34.58866231542042,
            "unit": "iter/sec",
            "range": "stddev: 0.003455248898184103",
            "extra": "mean: 28.911207692301442 msec\nrounds: 39"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9235482945050864,
            "unit": "iter/sec",
            "range": "stddev: 0.004200360763334078",
            "extra": "mean: 519.8725724000042 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9160994741095496,
            "unit": "iter/sec",
            "range": "stddev: 0.0051843393964345124",
            "extra": "mean: 521.8935725999927 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9365778944583616,
            "unit": "iter/sec",
            "range": "stddev: 0.007872869140416263",
            "extra": "mean: 1.067716850800025 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.911327602130959,
            "unit": "iter/sec",
            "range": "stddev: 0.016690903230779895",
            "extra": "mean: 1.097300243800032 sec\nrounds: 5"
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
          "id": "349c0d1b5b1070b78fe769f4db54abc9d196cac6",
          "message": "Merge pull request #243 from zeroae/fix/242-batchgetitem-permission\n\n🐛 fix(infra): add dynamodb:BatchGetItem to AppRole and AdminRole",
          "timestamp": "2026-01-27T00:20:02-05:00",
          "tree_id": "ae04b3f5e01de6c8dcf80472d2b91871c22cf67d",
          "url": "https://github.com/zeroae/zae-limiter/commit/349c0d1b5b1070b78fe769f4db54abc9d196cac6"
        },
        "date": 1769491864518,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 19.047323433159963,
            "unit": "iter/sec",
            "range": "stddev: 0.020029438256355215",
            "extra": "mean: 52.50081479999835 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 22.873793628263638,
            "unit": "iter/sec",
            "range": "stddev: 0.005575252914779427",
            "extra": "mean: 43.71815258332864 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 38.131074271352645,
            "unit": "iter/sec",
            "range": "stddev: 0.005832912348981079",
            "extra": "mean: 26.225329842104298 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 36.63331223699892,
            "unit": "iter/sec",
            "range": "stddev: 0.0027564928516643546",
            "extra": "mean: 27.29755894117649 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 32.87170034007715,
            "unit": "iter/sec",
            "range": "stddev: 0.005088931365033784",
            "extra": "mean: 30.42130433334478 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 227.0420126888283,
            "unit": "iter/sec",
            "range": "stddev: 0.0007188067660619926",
            "extra": "mean: 4.404471173229718 msec\nrounds: 127"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 32.70935178070274,
            "unit": "iter/sec",
            "range": "stddev: 0.00436039460167561",
            "extra": "mean: 30.57229647057578 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 29.50765416745268,
            "unit": "iter/sec",
            "range": "stddev: 0.0027589471322529407",
            "extra": "mean: 33.88951200000889 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 30.365124252322147,
            "unit": "iter/sec",
            "range": "stddev: 0.006191327944258581",
            "extra": "mean: 32.93251796667771 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 12.817094449388403,
            "unit": "iter/sec",
            "range": "stddev: 0.004611672260529887",
            "extra": "mean: 78.02080291666395 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 33.38786282370812,
            "unit": "iter/sec",
            "range": "stddev: 0.002999286641248176",
            "extra": "mean: 29.95100361110619 msec\nrounds: 36"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9250207409049207,
            "unit": "iter/sec",
            "range": "stddev: 0.0018252485120041166",
            "extra": "mean: 519.4749224000134 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9167766110562543,
            "unit": "iter/sec",
            "range": "stddev: 0.00842680829714272",
            "extra": "mean: 521.7092040000125 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9442255923087822,
            "unit": "iter/sec",
            "range": "stddev: 0.004605372852662187",
            "extra": "mean: 1.0590689429999884 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.916622583536755,
            "unit": "iter/sec",
            "range": "stddev: 0.004877552085664747",
            "extra": "mean: 1.090961556000002 sec\nrounds: 5"
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
          "id": "4a83663ab2dc1ae689c0463d7e65a0d0f93d3465",
          "message": "Merge pull request #244 from zeroae/feat/151-local-commands\n\n✨ feat(local): add `zae-limiter local` commands for LocalStack management",
          "timestamp": "2026-01-27T02:41:31-05:00",
          "tree_id": "a734929966716480a8cfc965b8fb70ce7d800c02",
          "url": "https://github.com/zeroae/zae-limiter/commit/4a83663ab2dc1ae689c0463d7e65a0d0f93d3465"
        },
        "date": 1769500355530,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 18.66557915527331,
            "unit": "iter/sec",
            "range": "stddev: 0.006520783512902197",
            "extra": "mean: 53.574549799998294 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.423951460558513,
            "unit": "iter/sec",
            "range": "stddev: 0.0064069368322098455",
            "extra": "mean: 46.67673010000044 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 35.20450080182974,
            "unit": "iter/sec",
            "range": "stddev: 0.005230489424703365",
            "extra": "mean: 28.405458882349087 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 33.1491955602889,
            "unit": "iter/sec",
            "range": "stddev: 0.006028262348688915",
            "extra": "mean: 30.166644562498846 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 31.073623909645264,
            "unit": "iter/sec",
            "range": "stddev: 0.003203647976622412",
            "extra": "mean: 32.18163426666175 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 226.31065453143796,
            "unit": "iter/sec",
            "range": "stddev: 0.0005218760394607212",
            "extra": "mean: 4.418704908394337 msec\nrounds: 131"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 28.43965621416169,
            "unit": "iter/sec",
            "range": "stddev: 0.006119622268588732",
            "extra": "mean: 35.162169066658564 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 29.309054625950445,
            "unit": "iter/sec",
            "range": "stddev: 0.002936742880677877",
            "extra": "mean: 34.11914893749568 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 25.872989764907004,
            "unit": "iter/sec",
            "range": "stddev: 0.0488435791134191",
            "extra": "mean: 38.650345750005144 msec\nrounds: 32"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 13.465525972785894,
            "unit": "iter/sec",
            "range": "stddev: 0.005952317185439162",
            "extra": "mean: 74.26371625000172 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 34.297459547399406,
            "unit": "iter/sec",
            "range": "stddev: 0.0034677290813734988",
            "extra": "mean: 29.15667845946405 msec\nrounds: 37"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9250769973719781,
            "unit": "iter/sec",
            "range": "stddev: 0.0029345413065242935",
            "extra": "mean: 519.4597418000171 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9239101981824998,
            "unit": "iter/sec",
            "range": "stddev: 0.0026621261648750454",
            "extra": "mean: 519.7747799999661 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.945700592387437,
            "unit": "iter/sec",
            "range": "stddev: 0.0014539058009102024",
            "extra": "mean: 1.0574171234000005 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.917070505297156,
            "unit": "iter/sec",
            "range": "stddev: 0.0066351088714380856",
            "extra": "mean: 1.090428701199994 sec\nrounds: 5"
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
          "id": "565be0cdb7ed3a212b4fc9d6d383918a9f1df4fb",
          "message": "Merge pull request #245 from zeroae/chore/154-lambda-packaging-alternatives\n\n✨ feat(infra): use aws-lambda-builders for Lambda packaging",
          "timestamp": "2026-01-27T20:36:24-05:00",
          "tree_id": "8ecd14ed71a29ef40ec4cd72dab4cdd14069f060",
          "url": "https://github.com/zeroae/zae-limiter/commit/565be0cdb7ed3a212b4fc9d6d383918a9f1df4fb"
        },
        "date": 1769564884281,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 18.75775978040817,
            "unit": "iter/sec",
            "range": "stddev: 0.00924787185608807",
            "extra": "mean: 53.31127019999826 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 22.77332003533667,
            "unit": "iter/sec",
            "range": "stddev: 0.0076427913638879",
            "extra": "mean: 43.911032666661264 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 34.632006834710126,
            "unit": "iter/sec",
            "range": "stddev: 0.005176151514650761",
            "extra": "mean: 28.875023176472244 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 32.60576997228858,
            "unit": "iter/sec",
            "range": "stddev: 0.004120532989515605",
            "extra": "mean: 30.669418352944685 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 30.06779762177222,
            "unit": "iter/sec",
            "range": "stddev: 0.0029853908563739484",
            "extra": "mean: 33.25817250000033 msec\nrounds: 14"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 216.404103857181,
            "unit": "iter/sec",
            "range": "stddev: 0.0005787786084237097",
            "extra": "mean: 4.620984455359332 msec\nrounds: 112"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 29.114690328306754,
            "unit": "iter/sec",
            "range": "stddev: 0.005207032227147826",
            "extra": "mean: 34.34692207691971 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 23.81432458835799,
            "unit": "iter/sec",
            "range": "stddev: 0.006331041681684713",
            "extra": "mean: 41.99153313333379 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 29.982113345774177,
            "unit": "iter/sec",
            "range": "stddev: 0.0046528030126206385",
            "extra": "mean: 33.35321925000142 msec\nrounds: 36"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 13.245810923186882,
            "unit": "iter/sec",
            "range": "stddev: 0.005741964583481172",
            "extra": "mean: 75.49556654545727 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 33.04351924005073,
            "unit": "iter/sec",
            "range": "stddev: 0.003461010989113516",
            "extra": "mean: 30.263120363642745 msec\nrounds: 33"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9238406463769657,
            "unit": "iter/sec",
            "range": "stddev: 0.001719118807603629",
            "extra": "mean: 519.7935712000003 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9165910051479564,
            "unit": "iter/sec",
            "range": "stddev: 0.00428008941702017",
            "extra": "mean: 521.7597272000148 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9405835097467997,
            "unit": "iter/sec",
            "range": "stddev: 0.0007945440556033703",
            "extra": "mean: 1.063169819199993 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.9073526490782595,
            "unit": "iter/sec",
            "range": "stddev: 0.0037313830544680317",
            "extra": "mean: 1.1021073240000532 sec\nrounds: 5"
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
          "id": "66a3d10832cf260fbfd5be5699ca6602f9f82364",
          "message": "Merge pull request #247 from zeroae/feat/246-local-env-command\n\n✨ feat(local): add `zae-limiter local env` command",
          "timestamp": "2026-01-27T22:13:20-05:00",
          "tree_id": "82659f9420aaaf970ca1894ed29bcc67ffe8c049",
          "url": "https://github.com/zeroae/zae-limiter/commit/66a3d10832cf260fbfd5be5699ca6602f9f82364"
        },
        "date": 1769570692811,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 18.255664802159103,
            "unit": "iter/sec",
            "range": "stddev: 0.012136789583264298",
            "extra": "mean: 54.7775176000016 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.239675720194413,
            "unit": "iter/sec",
            "range": "stddev: 0.008008735893461618",
            "extra": "mean: 47.081698099995606 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 31.617321677837033,
            "unit": "iter/sec",
            "range": "stddev: 0.0050081704437691605",
            "extra": "mean: 31.62823246666638 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 29.736733966258427,
            "unit": "iter/sec",
            "range": "stddev: 0.003418302582320958",
            "extra": "mean: 33.6284408750025 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 27.090967455082954,
            "unit": "iter/sec",
            "range": "stddev: 0.005245577145765427",
            "extra": "mean: 36.912672153846415 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 181.1513623310796,
            "unit": "iter/sec",
            "range": "stddev: 0.0007168189536417218",
            "extra": "mean: 5.520245540148681 msec\nrounds: 137"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 28.141438942478576,
            "unit": "iter/sec",
            "range": "stddev: 0.008119117324476264",
            "extra": "mean: 35.5347856249999 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 26.95907973718012,
            "unit": "iter/sec",
            "range": "stddev: 0.004726815007876171",
            "extra": "mean: 37.0932542857117 msec\nrounds: 14"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 29.1790290197398,
            "unit": "iter/sec",
            "range": "stddev: 0.0033533850637470905",
            "extra": "mean: 34.27118837037016 msec\nrounds: 27"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 12.05731886725308,
            "unit": "iter/sec",
            "range": "stddev: 0.004190492516506165",
            "extra": "mean: 82.93717790909031 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 31.99225983236575,
            "unit": "iter/sec",
            "range": "stddev: 0.0039740224567248374",
            "extra": "mean: 31.257560586211714 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9171307995918978,
            "unit": "iter/sec",
            "range": "stddev: 0.005785671343832361",
            "extra": "mean: 521.6128185999992 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9196418664533363,
            "unit": "iter/sec",
            "range": "stddev: 0.0014167594448196697",
            "extra": "mean: 520.9305014000165 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.8933200351378369,
            "unit": "iter/sec",
            "range": "stddev: 0.11319769567185954",
            "extra": "mean: 1.1194196487999988 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.9028569983381198,
            "unit": "iter/sec",
            "range": "stddev: 0.006987258896969581",
            "extra": "mean: 1.1075951139999916 sec\nrounds: 5"
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
          "id": "663b687a3748078e447fe2c14692387a4f7e4034",
          "message": "✨ feat: v0.7.0 developer experience improvements (#251)\n\n* ♻️ refactor(docs): slim down CLAUDE.md by extracting rules files\n\nExtract procedural content from CLAUDE.md (1017→470 lines, 54% reduction)\ninto 5 new .claude/rules/ files: testing, code-review, pull-request-workflow,\nrelease-planning, and localstack-parity. Condense verbose CLI/Python examples,\nremove sections duplicated in existing rules files, and update cross-references.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: fix documentation drift accumulated since v0.4.0\n\nAudit and fix ~25 discrepancies between docs and code across CLI,\nAPI, infrastructure, and project structure documentation.\n\nCLI (docs/cli.md):\n- Document local command group (up/down/status/logs)\n- Document entity create and entity show commands\n- Add missing deploy options (--enable-iam-roles, --tag, --lambda-duration-threshold-pct)\n- Add --wait/--no-wait to delete command\n- Fix incorrect required/optional flags for status and delete\n- Fix list output columns (4, not 6)\n\nAPI (docs/api/):\n- Add Repository and RepositoryProtocol documentation (new file)\n- Add StackOptions, Status, BackendCapabilities, ResourceCapacity,\n  EntityCapacity to models.md\n- Update public exports to match full __all__ (45 exports)\n- Update module structure tree\n\nInfrastructure (docs/contributing/localstack.md, docs/infra/deployment.md):\n- Make zae-limiter local CLI the preferred LocalStack method\n- Fix container name (zae-limiter-localstack, not localstack)\n- Fix services list (add sts, resourcegroupstaggingapi)\n- Fix hardcoded lambda function names to use {name}-aggregator pattern\n\nDeprecated API cleanup:\n- Replace use_stored_limits=True with limits=None across guides,\n  operations runbooks, and performance docs\n\nProject structure (mkdocs.yml, architecture.md):\n- Update project structure trees (add 7 missing modules + aggregator)\n- Add 7 missing ADRs to mkdocs nav (000, 012, 013, 107, 111, 112, 113)\n- Add Repository to API nav, benchmarks.md to Reference nav\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(docs): add pytest-examples lint pass for documentation code blocks\n\nAdd pytest-examples infrastructure to lint all Python code blocks in docs/.\nConfigure ruff with doc-appropriate ignore rules (F704, F706, F811, F821,\nF841, E741, I001, F401, N807) and fix 9 code blocks with real issues\n(syntax errors, mixed fences, deprecated patterns). All 170 blocks pass.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(docs): add moto-backed execution pass for doc code examples\n\nAdd test_docs_run.py that runs Python code blocks against moto. Update\nconftest with moto_env fixture, skip tag infrastructure, and aiobotocore\npatch. Tag 121 blocks as lint-only and 2 as requires-external across 24\ndoc files. Result: 47 blocks execute successfully, 123 skip by tag.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 👷 ci(docs): add documentation example testing to CI and update testing guide\n\nAdd doc lint and moto execution steps to unit job, LocalStack doc\nintegration step to integration job. Create test_docs_integration.py\nfor requires-localstack tagged blocks. Document the doctest workflow,\ncode fence tag convention, and commands in contributing/testing.md.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(docs): enhance runner with async wrapping and globals injection\n\nConvert 32 doc blocks from lint-only to fully executable against moto by\nadding async wrapping, pre-built limiter fixtures, stub functions, and\nRateLimiter/Repository monkeypatching to the test harness. Results: 79\nblocks now execute (up from 47), 91 skipped (down from 123).\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(docs): fix doc drift and enhance harness to execute 125 blocks\n\nFix API drift in doc examples (Limit.custom missing refill_amount,\nacquire() missing limits param, stale attribute names) and enhance the\ntest harness with EntityExistsError tolerance, auto-seeded defaults for\nnew tables, mock OpenAI objects, and common globals injection. Promotes\n~80 blocks from lint-only to fully executing against moto.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(docs): rewrite 16 doc blocks from lint-only to fully executable\n\nReplace ellipsis arguments, placeholder values, and nonexistent API calls\nin doc code examples so they execute against moto. Add web framework stubs\n(JSONResponse, HTTPException) and context variables to the test harness.\n\nResults: 142 passed, 34 skipped (up from 125 passed, 45 skipped).\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(schema): add composite bucket items design with test strategy\n\nDesign for consolidating per-limit DynamoDB items into single composite\nitems per entity+resource. Includes ADD-based writes with lazy refill,\noptimistic locking via shared rf timestamp, four write paths, and\ncategorized test strategy (untouchable/rewrite/benchmark).\n\nRefs: #248\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔥 remove(docs): delete doc-example-testing plan file\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(schema): add ADR-114 and ADR-115 for composite bucket items\n\nADR-114: Composite bucket items — consolidate per-limit DynamoDB items\ninto a single item per entity+resource with b_{name}_{field} prefix.\n\nADR-115: ADD-based writes with lazy refill — atomic consumption via ADD,\nlazy refill on read, optimistic locking via shared rf timestamp, four\nwrite paths (Create/Normal/Retry/Adjust). Depends on ADR-114.\n\nRefs: #248\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ♻️ refactor(schema): implement composite bucket items with ADD-based writes\n\nConsolidate per-limit DynamoDB items into single composite items per\nentity+resource (ADR-114) and switch from PutItem to ADD-based writes\nwith lazy refill and optimistic locking (ADR-115).\n\nKey changes:\n- schema.py: sk_bucket(resource) drops limit_name, add b_{name}_{field}\n  attribute helpers\n- repository.py: add _deserialize_composite_bucket, build_composite_create/\n  normal/retry/adjust write paths, update all read methods\n- limiter.py: _do_acquire captures original tk/rf for ADD delta computation\n- lease.py: _commit groups entries by (entity_id, resource), uses Normal\n  path with Retry fallback on ConditionalCheckFailedException\n- processor.py: extract_deltas enumerates b_{name}_tc from composite\n  stream events\n- version.py: bump CURRENT_SCHEMA_VERSION to 2.0.0\n\nCloses #248\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(schema): add coverage for composite write paths and lease retry\n\nCover uncovered lines in schema.py (parse_bucket_attr, parse_bucket_sk),\nrepository.py (build_composite_retry, build_composite_adjust, edge cases),\nand lease.py (_is_condition_check_failure, _build_retry_failure_statuses,\nretry path).\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(test): update e2e schema version assertion to 2.0.0\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔧 chore(schema): change schema version to 0.7.0\n\nAlign schema version with the release milestone instead of using\nsemver major bump.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(test): shorten e2e stack names to fit IAM role name limit\n\nIAM role names must be <= 64 chars. With PowerUserPB-{name}-readonly-role\nformat, the old 30-char names could exceed this. Shortened to 16 chars.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(test): relax schema version assertion for pre-1.0 versions\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ⚡ perf(test): optimize e2e fixtures to share stacks across test classes\n\nReduce CloudFormation stack deployments from ~13 to 5 for AWS e2e tests by\nconverting fixtures from function-scoped to class-scoped with loop_scope=\"class\".\n\nChanges:\n- test_aws.py: Convert all limiter fixtures to @pytest_asyncio.fixture(scope=\"class\", loop_scope=\"class\")\n- test_aws.py: Split TestE2EAWSXRayTracing into two classes (tracing-enabled vs disabled need separate stacks)\n- test_aws.py: Add @pytest.mark.asyncio(loop_scope=\"class\") to all test methods\n- test_aws.py: Use unique_name_class instead of unique_name for class-level isolation\n- test_localstack.py: Convert TestE2ELocalStackAggregatorWorkflow fixture to class-scoped\n\nStack reduction per test run:\n- TestE2EAWSFullWorkflow: 5 tests → 1 shared stack\n- TestE2EAWSUsageSnapshots: 1 test → 1 shared stack\n- TestE2EAWSRateLimiting: 2 tests → 1 shared stack\n- TestE2EAWSXRayTracingEnabled: 3 tests → 1 shared stack\n- TestE2EAWSXRayTracingDisabled: 2 tests → 1 shared stack\n\nTime savings: ~10 minutes per AWS e2e test run (each stack deployment ~2-3 minutes).\n\nCo-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(infra): deploy Lambda code after stack creation\n\nensure_infrastructure() was creating CloudFormation stacks but never\ndeploying the actual Lambda code, leaving only a placeholder. Events\nwere being consumed by the placeholder and lost.\n\nChanges:\n- Repository.ensure_infrastructure() now calls deploy_lambda_code()\n- Placeholder Lambda errors out to force ESM retries until real code deployed\n- Added function_active waiter after function_updated waiter\n- Added --keep-stacks-on-failure pytest flag to preserve stacks for debugging\n- Removed dead skipped_local code from previous DynamoDB Local removal\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: add --keep-stacks-on-failure to AWS testing rules\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(infra): wait for ESM stabilization after Lambda deployment\n\nESM with StartingPosition: LATEST needs ~45s after stack creation to\nestablish its stream position before reliably capturing events. Without\nthis wait, the Lambda aggregator shows 0 invocations despite ESM\nreporting State: Enabled.\n\n- Add wait_for_esm_ready() to StackManager that waits for ESM to be\n  enabled AND have processed at least one poll (LastProcessingResult)\n  with a minimum 45s stabilization time\n- Call wait_for_esm_ready() at end of deploy_lambda_code() when wait=True\n- Add \"✓ Event Source Mapping ready\" CLI message\n- Simplify test helper since infrastructure now handles the wait\n- Remove should_delete_stack fixture (didn't work at class scope)\n- Reduce snapshot polling timeout from 180s to 90s\n\nCloses #249\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔥 remove: --keep-stacks-on-failure (never implemented)\n\nThe pytest flag was defined but the actual implementation\n(should_delete_stack fixture) was removed because it didn't\nwork at class scope. Remove the dead code and misleading docs.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(test): update capacity tests for composite bucket items\n\nComposite buckets store all limits in single item, so BatchGetItem\nfetches 2 items (META + composite) regardless of number of limits,\nand transaction writes 1 item.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✅ test(doctest): add LocalStack fixture for doc integration tests\n\nAdd localstack_limiter fixture to deploy a 'limiter' stack before running\ndoc examples tagged with 'requires-localstack'. This matches the CLI deploy\nexample in docs/contributing/localstack.md.\n\nAlso add async wrapping for bare async code in doc examples.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: update cost estimates for ADR-114/115 O(1) optimizations\n\nComposite bucket items and ADD-based writes reduced acquire() costs\nfrom O(N) to O(1) where N is limit count. Updated all cost references\nfrom ~$1/1M to ~$0.75/1M requests (~50% reduction).\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: clean up ADR 104 and remove implemented plan\n\n- ADR 104: Use lint-only fence since example reflects existing code\n- Remove composite bucket items plan (implemented as ADR-114/115)\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs: add stored-config-default design document\n\nDesign for making stored config the default approach:\n- limits parameter defaults to None in acquire()\n- ValidationError when no stored config exists\n- Restructure getting-started.md and basic-usage.md\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* ✨ feat(limiter): make limits=None the default in acquire()\n\nBREAKING CHANGE: The `limits` parameter in `acquire()` now defaults to\n`None` and comes after `consume`. Limits are resolved automatically from\nstored config (Entity > Resource > System).\n\n- Change parameter order: `acquire(entity_id, resource, consume, limits=None)`\n- Raise ValidationError if no limits configured at any level\n- Restructure docs to lead with stored config as recommended approach\n- Update getting-started.md with Minimalist and Stored Config sections\n- Update basic-usage.md examples to use new parameter order\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🐛 fix(docs): fix markdown code fences for doctests\n\n- Add missing closing ``` in getting-started.md\n- Add lint-only tag to design doc pseudo-code\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(performance): update acquire() parameter order\n\nUpdate examples to use new parameter order (consume before limits)\nintroduced by the stored-config-default change.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🔥 remove: stored-config-default design plan (implemented)\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 📝 docs(performance): update benchmark results from fresh measurements\n\n- Update latency table with Moto, LocalStack, and AWS p50 values\n- Add AWS (in-region) column for production latency estimates\n- Split latency breakdown into external vs in-region diagrams\n- Replace throughput ranges with actual measured TPS values\n- Add notes about concurrent performance and network overhead\n\nAlso fix AWS benchmark tests to use shorter stack names to avoid\nIAM role name length limit (64 chars). See issue #252.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-28T22:43:35-05:00",
          "tree_id": "86ae6ea3162cf1836c87c447301333dd7595d38a",
          "url": "https://github.com/zeroae/zae-limiter/commit/663b687a3748078e447fe2c14692387a4f7e4034"
        },
        "date": 1769659079030,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 16.39377882979259,
            "unit": "iter/sec",
            "range": "stddev: 0.019317446576963555",
            "extra": "mean: 60.99874899999804 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.017225591989586,
            "unit": "iter/sec",
            "range": "stddev: 0.006617780002796929",
            "extra": "mean: 47.58001933333844 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 32.53309482579981,
            "unit": "iter/sec",
            "range": "stddev: 0.007244269453868837",
            "extra": "mean: 30.737930263153668 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 34.02663857065648,
            "unit": "iter/sec",
            "range": "stddev: 0.0075195854030932",
            "extra": "mean: 29.388738999989528 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 27.121395096629847,
            "unit": "iter/sec",
            "range": "stddev: 0.003376731307400435",
            "extra": "mean: 36.87125962499849 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 234.29789751686732,
            "unit": "iter/sec",
            "range": "stddev: 0.000719863506867233",
            "extra": "mean: 4.2680707364350505 msec\nrounds: 129"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 26.762058060081195,
            "unit": "iter/sec",
            "range": "stddev: 0.006146948910717279",
            "extra": "mean: 37.36633399998558 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 28.25579169999198,
            "unit": "iter/sec",
            "range": "stddev: 0.005708703252481828",
            "extra": "mean: 35.39097437500871 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 32.79230059235191,
            "unit": "iter/sec",
            "range": "stddev: 0.0038884703181893128",
            "extra": "mean: 30.494963205882186 msec\nrounds: 34"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 13.400633115832202,
            "unit": "iter/sec",
            "range": "stddev: 0.004543756462365067",
            "extra": "mean: 74.62333990910834 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 32.217203970807184,
            "unit": "iter/sec",
            "range": "stddev: 0.003963311786700377",
            "extra": "mean: 31.039316785718743 msec\nrounds: 28"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.919861238204737,
            "unit": "iter/sec",
            "range": "stddev: 0.0016388068756290673",
            "extra": "mean: 520.870977599975 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.912663653861867,
            "unit": "iter/sec",
            "range": "stddev: 0.0024231603271064163",
            "extra": "mean: 522.8310779999902 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9279851979441645,
            "unit": "iter/sec",
            "range": "stddev: 0.012661091108316367",
            "extra": "mean: 1.0776033952000261 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.900348297614118,
            "unit": "iter/sec",
            "range": "stddev: 0.012837444511546572",
            "extra": "mean: 1.1106812803999901 sec\nrounds: 5"
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
          "id": "817e62c7fb227422816d6de0084e04b83400d196",
          "message": "🔧 chore(docs): add ZeroAE brand theme and logos (#254)\n\n* 🎨 style(docs): add ZeroAE brand theme and logos\n\n- Add custom color scheme with ZeroAE brand colors (#2b0548 primary)\n- Add logo assets with light/dark mode variants\n- Configure auto-detection of system color preference\n- Update README with responsive logo using picture element\n- Hide auto-generated title on homepage via template override\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🎨 style(docs): trim logo margins and left-align on homepage\n\n- Remove excess whitespace from logo SVGs (40px margins)\n- Left-align logo on docs homepage\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n* 🎨 style(docs): use percentage width for README logo\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>\n\n---------\n\nCo-authored-by: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-29T01:50:39-05:00",
          "tree_id": "c9874d315e54e1651615e1896bc13bf870efe1a2",
          "url": "https://github.com/zeroae/zae-limiter/commit/817e62c7fb227422816d6de0084e04b83400d196"
        },
        "date": 1769670339370,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 16.36999399308564,
            "unit": "iter/sec",
            "range": "stddev: 0.012450677092788055",
            "extra": "mean: 61.08737733333195 msec\nrounds: 6"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.01140579331686,
            "unit": "iter/sec",
            "range": "stddev: 0.00842837432612004",
            "extra": "mean: 47.593198181821414 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 32.82126519836219,
            "unit": "iter/sec",
            "range": "stddev: 0.003426939672340902",
            "extra": "mean: 30.46805155000243 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 35.66381344083485,
            "unit": "iter/sec",
            "range": "stddev: 0.007555392877808454",
            "extra": "mean: 28.039626263157995 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 28.007231389584835,
            "unit": "iter/sec",
            "range": "stddev: 0.0043483574222467295",
            "extra": "mean: 35.70506438461726 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 229.12095635425288,
            "unit": "iter/sec",
            "range": "stddev: 0.0005773595218455319",
            "extra": "mean: 4.364506922072466 msec\nrounds: 154"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 26.043471906545335,
            "unit": "iter/sec",
            "range": "stddev: 0.00534042496677712",
            "extra": "mean: 38.397338250000246 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 24.68320115105723,
            "unit": "iter/sec",
            "range": "stddev: 0.005269656122268688",
            "extra": "mean: 40.51338373333995 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 28.483981121726785,
            "unit": "iter/sec",
            "range": "stddev: 0.0036082036764472215",
            "extra": "mean: 35.10745200000248 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 12.432797632553088,
            "unit": "iter/sec",
            "range": "stddev: 0.00550701548843009",
            "extra": "mean: 80.43241992306513 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 29.72657967639336,
            "unit": "iter/sec",
            "range": "stddev: 0.005170946973585744",
            "extra": "mean: 33.63992799999543 msec\nrounds: 28"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9208589028690282,
            "unit": "iter/sec",
            "range": "stddev: 0.0020961968236095033",
            "extra": "mean: 520.6004452000002 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9041360554993294,
            "unit": "iter/sec",
            "range": "stddev: 0.005165411285310106",
            "extra": "mean: 525.1725564000026 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9305928918445504,
            "unit": "iter/sec",
            "range": "stddev: 0.009130488565468577",
            "extra": "mean: 1.0745837505999816 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.8959434732548124,
            "unit": "iter/sec",
            "range": "stddev: 0.021638585916697865",
            "extra": "mean: 1.1161418435999848 sec\nrounds: 5"
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
          "id": "b68df774bd6f71820f4a43e25db044e511f8ea1d",
          "message": "🎨 style(docs): add social card image (#255)\n\nAdd 1280x640 social card image for Open Graph/Twitter cards.\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-29T02:28:22-05:00",
          "tree_id": "99d2e8570de8a446bcdd39540fc6d65aa0964ead",
          "url": "https://github.com/zeroae/zae-limiter/commit/b68df774bd6f71820f4a43e25db044e511f8ea1d"
        },
        "date": 1769672576416,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 16.01992966398565,
            "unit": "iter/sec",
            "range": "stddev: 0.0208082593932469",
            "extra": "mean: 62.42224660000204 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.57731686524915,
            "unit": "iter/sec",
            "range": "stddev: 0.007660072469826577",
            "extra": "mean: 48.597200818188014 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 31.56872884780558,
            "unit": "iter/sec",
            "range": "stddev: 0.006313019872249216",
            "extra": "mean: 31.67691688889502 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 31.33067124035413,
            "unit": "iter/sec",
            "range": "stddev: 0.006335537037830356",
            "extra": "mean: 31.917605349993035 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 23.72176470652656,
            "unit": "iter/sec",
            "range": "stddev: 0.005107385056000362",
            "extra": "mean: 42.15537976923237 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 228.63311524256906,
            "unit": "iter/sec",
            "range": "stddev: 0.000671112506117529",
            "extra": "mean: 4.373819597126368 msec\nrounds: 139"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 28.23871940982449,
            "unit": "iter/sec",
            "range": "stddev: 0.004893688986719761",
            "extra": "mean: 35.41237070587881 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 28.642858746347184,
            "unit": "iter/sec",
            "range": "stddev: 0.005354038373587429",
            "extra": "mean: 34.912716249998255 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 30.347819341677464,
            "unit": "iter/sec",
            "range": "stddev: 0.0034221273123398732",
            "extra": "mean: 32.95129672222193 msec\nrounds: 36"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 12.753818891135914,
            "unit": "iter/sec",
            "range": "stddev: 0.0062933189527348415",
            "extra": "mean: 78.40788774999889 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 29.39004962872811,
            "unit": "iter/sec",
            "range": "stddev: 0.006007967307330799",
            "extra": "mean: 34.025121176472005 msec\nrounds: 34"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9204035654656932,
            "unit": "iter/sec",
            "range": "stddev: 0.0012566243181834627",
            "extra": "mean: 520.7238821999908 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.912979587004935,
            "unit": "iter/sec",
            "range": "stddev: 0.0025678984694822186",
            "extra": "mean: 522.7447312000095 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9285790777150553,
            "unit": "iter/sec",
            "range": "stddev: 0.012040319374007078",
            "extra": "mean: 1.0769142057999943 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.8976673695985179,
            "unit": "iter/sec",
            "range": "stddev: 0.013889383841822598",
            "extra": "mean: 1.113998385000059 sec\nrounds: 5"
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
          "id": "9a4ee0ff3b10d0efed3f4817d73b6eaee75b5b9f",
          "message": "🐛 fix(cli): improve milestone skill mode detection and routing (#263)\n\n## Summary\n\n- Restructure SKILL.md with explicit mode detection priority\n- Status mode now correctly triggers when version number provided\n- Use imperative language (\"Run now\") for clearer agent instructions\n- Inline commands in SKILL.md to reduce file indirection\n\n## Test plan\n\n- [x] `/milestone list` correctly lists all milestones\n- [x] `/milestone 1.0.0` correctly shows milestone status (not list)\n- [x] `/milestone v1.0.0` correctly shows milestone status\n\n🤖 Generated with [Claude Code](https://claude.ai/code)",
          "timestamp": "2026-01-30T09:12:58-05:00",
          "tree_id": "efb0a9a74f51d0e65e36ea936267dc0e4735ead7",
          "url": "https://github.com/zeroae/zae-limiter/commit/9a4ee0ff3b10d0efed3f4817d73b6eaee75b5b9f"
        },
        "date": 1769783240651,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 16.704896053303102,
            "unit": "iter/sec",
            "range": "stddev: 0.016993719485627046",
            "extra": "mean: 59.86268916664509 msec\nrounds: 6"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.917242481799082,
            "unit": "iter/sec",
            "range": "stddev: 0.006730882132219989",
            "extra": "mean: 45.626177692309525 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 33.475880700473475,
            "unit": "iter/sec",
            "range": "stddev: 0.004438250624437684",
            "extra": "mean: 29.872253666678176 msec\nrounds: 21"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 34.14413527111156,
            "unit": "iter/sec",
            "range": "stddev: 0.004748050257426786",
            "extra": "mean: 29.287606555556067 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 29.095386357420246,
            "unit": "iter/sec",
            "range": "stddev: 0.004724367263254859",
            "extra": "mean: 34.36971029411913 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 223.2599610266737,
            "unit": "iter/sec",
            "range": "stddev: 0.0005749624037682132",
            "extra": "mean: 4.4790834657564345 msec\nrounds: 73"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 30.436364439494383,
            "unit": "iter/sec",
            "range": "stddev: 0.003587196174096407",
            "extra": "mean: 32.85543521427923 msec\nrounds: 14"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 29.622499118755357,
            "unit": "iter/sec",
            "range": "stddev: 0.005715070630944884",
            "extra": "mean: 33.758124052634514 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 33.41097973872473,
            "unit": "iter/sec",
            "range": "stddev: 0.002702095940425365",
            "extra": "mean: 29.930280638881058 msec\nrounds: 36"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 13.57658599941628,
            "unit": "iter/sec",
            "range": "stddev: 0.0038963694833240655",
            "extra": "mean: 73.65621961537272 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 31.34909789225972,
            "unit": "iter/sec",
            "range": "stddev: 0.00578903879669323",
            "extra": "mean: 31.898844535711692 msec\nrounds: 28"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9246770121942407,
            "unit": "iter/sec",
            "range": "stddev: 0.0022821393410883918",
            "extra": "mean: 519.5676955999716 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9219148136306141,
            "unit": "iter/sec",
            "range": "stddev: 0.0014284392179718674",
            "extra": "mean: 520.3144243999759 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9327193538307079,
            "unit": "iter/sec",
            "range": "stddev: 0.004115587826390572",
            "extra": "mean: 1.0721338588000435 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.9102877824449258,
            "unit": "iter/sec",
            "range": "stddev: 0.011312139663304745",
            "extra": "mean: 1.0985536873999535 sec\nrounds: 5"
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
          "id": "3efc7cda4f5112dba31a2cff537ab6fe41d5a6ac",
          "message": "✨ feat(limiter): allow \"/\" in resource names for provider/model grouping (#265)\n\n## Summary\n\n- Add `RESOURCE_PATTERN` regex that allows `/` character in resource\nnames\n- Add `validate_resource()` function for resource-specific validation\n- Update `limiter.py` and `repository.py` to use `validate_resource()`\nfor resource validation\n- Add comprehensive unit tests for slash in resource names\n- Update `CLAUDE.md` with resource naming documentation\n\nThis enables intuitive resource naming that matches common LLM ecosystem\nconventions (e.g., `openai/gpt-4`, `anthropic/claude-3-opus`).\n\nEntity IDs and limit names remain unchanged to preserve future\npath-based hierarchy options.\n\n## Test plan\n\n- [x] Verify `openai/gpt-4` is a valid resource name\n- [x] Verify `anthropic/claude-3/opus` is a valid resource name (nested)\n- [x] Verify `/gpt-4` is rejected (must start with letter)\n- [x] Verify `gpt-4/` is valid (trailing slash allowed)\n- [x] Verify limit names still reject `/` (e.g., `rpm/tpm` is invalid)\n- [x] Verify entity IDs still reject `/` (e.g., `org/user` is invalid)\n- [x] Run `uv run pytest tests/unit/test_models.py -v -k resource` to\nverify tests pass\n\nCloses #264\n\n🤖 Generated with [Claude Code](https://claude.ai/code)",
          "timestamp": "2026-01-30T21:26:22-05:00",
          "tree_id": "6a1e34bc683f5c18fd3ab2312d9f8ecb4f240961",
          "url": "https://github.com/zeroae/zae-limiter/commit/3efc7cda4f5112dba31a2cff537ab6fe41d5a6ac"
        },
        "date": 1769827286162,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 15.777893333315564,
            "unit": "iter/sec",
            "range": "stddev: 0.016328728582309562",
            "extra": "mean: 63.37981749999955 msec\nrounds: 6"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.266429371097885,
            "unit": "iter/sec",
            "range": "stddev: 0.007747150333168237",
            "extra": "mean: 49.342683000001365 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 28.48111166051348,
            "unit": "iter/sec",
            "range": "stddev: 0.006424742537628051",
            "extra": "mean: 35.110989062495435 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 28.577994116085208,
            "unit": "iter/sec",
            "range": "stddev: 0.006951238298108016",
            "extra": "mean: 34.99195905555692 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 25.64139593847745,
            "unit": "iter/sec",
            "range": "stddev: 0.004567267534375197",
            "extra": "mean: 38.99943678570951 msec\nrounds: 14"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 206.1966621859249,
            "unit": "iter/sec",
            "range": "stddev: 0.0007565233013208199",
            "extra": "mean: 4.849739027774914 msec\nrounds: 108"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 27.213367693887527,
            "unit": "iter/sec",
            "range": "stddev: 0.0036557192662525204",
            "extra": "mean: 36.746646399982794 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 24.75648389062141,
            "unit": "iter/sec",
            "range": "stddev: 0.005335061572241966",
            "extra": "mean: 40.39345831250429 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 29.856426611322537,
            "unit": "iter/sec",
            "range": "stddev: 0.0029410418932051567",
            "extra": "mean: 33.49362644827587 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 11.23457492479911,
            "unit": "iter/sec",
            "range": "stddev: 0.006561136057625626",
            "extra": "mean: 89.0109333636298 msec\nrounds: 11"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 28.592248489008142,
            "unit": "iter/sec",
            "range": "stddev: 0.0036603676504119186",
            "extra": "mean: 34.97451417241407 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.909874372894599,
            "unit": "iter/sec",
            "range": "stddev: 0.0016598270955063225",
            "extra": "mean: 523.5946480000166 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.8952376769753765,
            "unit": "iter/sec",
            "range": "stddev: 0.005583275050851567",
            "extra": "mean: 527.6383074000023 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9230823521077062,
            "unit": "iter/sec",
            "range": "stddev: 0.012078007262007341",
            "extra": "mean: 1.0833269617999577 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.8965509695141102,
            "unit": "iter/sec",
            "range": "stddev: 0.00999691379135051",
            "extra": "mean: 1.115385554199952 sec\nrounds: 5"
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
          "id": "d877ce2a0020e1bf9490c1532e38c9699a86f5a8",
          "message": "🐛 fix(infra): use AWS::Partition for GovCloud and China region support (#267)\n\n## Summary\n\n- Replace hardcoded `arn:aws:` with `${AWS::Partition}` pseudo-parameter\nfor GovCloud/China support\n- Add pre-commit hook to prevent future hardcoded partitions\n- Add `.claude/rules/aws-partition.md` for code review guidance\n\n## Test plan\n\n- [x] Verify cfn-lint passes\n- [x] Verify pre-commit hook catches `arn:aws:` (tested locally)\n- [x] Template deploys in standard AWS partition\n\nCloses #266\n\n🤖 Generated with [Claude Code](https://claude.ai/code)",
          "timestamp": "2026-01-31T00:27:50-05:00",
          "tree_id": "0eaca953a87fe3b1c325c844e18147b5845066c1",
          "url": "https://github.com/zeroae/zae-limiter/commit/d877ce2a0020e1bf9490c1532e38c9699a86f5a8"
        },
        "date": 1769838174722,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 15.389365395109124,
            "unit": "iter/sec",
            "range": "stddev: 0.020864945764040934",
            "extra": "mean: 64.97993740000538 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.83585374817339,
            "unit": "iter/sec",
            "range": "stddev: 0.007630738307139761",
            "extra": "mean: 47.99419366665821 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 29.251921666608546,
            "unit": "iter/sec",
            "range": "stddev: 0.007553385492032033",
            "extra": "mean: 34.18578824999088 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 34.89404717429707,
            "unit": "iter/sec",
            "range": "stddev: 0.0035357469378513085",
            "extra": "mean: 28.658183300004225 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 28.927537718501974,
            "unit": "iter/sec",
            "range": "stddev: 0.0032956898315220974",
            "extra": "mean: 34.56913650000715 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 230.31198809312136,
            "unit": "iter/sec",
            "range": "stddev: 0.0006059797156726199",
            "extra": "mean: 4.341936380644124 msec\nrounds: 155"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 27.361525418989984,
            "unit": "iter/sec",
            "range": "stddev: 0.005496984459268133",
            "extra": "mean: 36.54766993750869 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 28.914671701837694,
            "unit": "iter/sec",
            "range": "stddev: 0.0046188548851610394",
            "extra": "mean: 34.584518555555455 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 31.152671634915627,
            "unit": "iter/sec",
            "range": "stddev: 0.004395890308479796",
            "extra": "mean: 32.0999756206851 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 12.998118309205514,
            "unit": "iter/sec",
            "range": "stddev: 0.004126500687080427",
            "extra": "mean: 76.93421280000052 msec\nrounds: 10"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 30.475921239671738,
            "unit": "iter/sec",
            "range": "stddev: 0.0038056754964251524",
            "extra": "mean: 32.81278987879321 msec\nrounds: 33"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9130602697399441,
            "unit": "iter/sec",
            "range": "stddev: 0.0055814695333012265",
            "extra": "mean: 522.7226845999667 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9185616951242843,
            "unit": "iter/sec",
            "range": "stddev: 0.002170965896785464",
            "extra": "mean: 521.2237909999658 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9297374389638211,
            "unit": "iter/sec",
            "range": "stddev: 0.006767864865431022",
            "extra": "mean: 1.0755724767999937 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.9031354679666123,
            "unit": "iter/sec",
            "range": "stddev: 0.004952521917703081",
            "extra": "mean: 1.1072536020000143 sec\nrounds: 5"
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
          "id": "989f24d8f458d8fdc9160864cb344b94bc71919d",
          "message": "build(deps): bump actions/setup-python from 5 to 6 (#268)\n\nBumps [actions/setup-python](https://github.com/actions/setup-python)\nfrom 5 to 6.\n<details>\n<summary>Release notes</summary>\n<p><em>Sourced from <a\nhref=\"https://github.com/actions/setup-python/releases\">actions/setup-python's\nreleases</a>.</em></p>\n<blockquote>\n<h2>v6.0.0</h2>\n<h2>What's Changed</h2>\n<h3>Breaking Changes</h3>\n<ul>\n<li>Upgrade to node 24 by <a\nhref=\"https://github.com/salmanmkc\"><code>@​salmanmkc</code></a> in <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1164\">actions/setup-python#1164</a></li>\n</ul>\n<p>Make sure your runner is on version v2.327.1 or later to ensure\ncompatibility with this release. <a\nhref=\"https://github.com/actions/runner/releases/tag/v2.327.1\">See\nRelease Notes</a></p>\n<h3>Enhancements:</h3>\n<ul>\n<li>Add support for <code>pip-version</code> by <a\nhref=\"https://github.com/priyagupta108\"><code>@​priyagupta108</code></a>\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1129\">actions/setup-python#1129</a></li>\n<li>Enhance reading from .python-version by <a\nhref=\"https://github.com/krystof-k\"><code>@​krystof-k</code></a> in <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/787\">actions/setup-python#787</a></li>\n<li>Add version parsing from Pipfile by <a\nhref=\"https://github.com/aradkdj\"><code>@​aradkdj</code></a> in <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1067\">actions/setup-python#1067</a></li>\n</ul>\n<h3>Bug fixes:</h3>\n<ul>\n<li>Clarify pythonLocation behaviour for PyPy and GraalPy in environment\nvariables by <a\nhref=\"https://github.com/aparnajyothi-y\"><code>@​aparnajyothi-y</code></a>\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1183\">actions/setup-python#1183</a></li>\n<li>Change missing cache directory error to warning by <a\nhref=\"https://github.com/aparnajyothi-y\"><code>@​aparnajyothi-y</code></a>\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1182\">actions/setup-python#1182</a></li>\n<li>Add Architecture-Specific PATH Management for Python with --user\nFlag on Windows by <a\nhref=\"https://github.com/aparnajyothi-y\"><code>@​aparnajyothi-y</code></a>\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1122\">actions/setup-python#1122</a></li>\n<li>Include python version in PyPy python-version output by <a\nhref=\"https://github.com/cdce8p\"><code>@​cdce8p</code></a> in <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1110\">actions/setup-python#1110</a></li>\n<li>Update docs: clarification on pip authentication with setup-python\nby <a\nhref=\"https://github.com/priya-kinthali\"><code>@​priya-kinthali</code></a>\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1156\">actions/setup-python#1156</a></li>\n</ul>\n<h3>Dependency updates:</h3>\n<ul>\n<li>Upgrade idna from 2.9 to 3.7 in /<strong>tests</strong>/data by <a\nhref=\"https://github.com/dependabot\"><code>@​dependabot</code></a>[bot]\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/843\">actions/setup-python#843</a></li>\n<li>Upgrade form-data to fix critical vulnerabilities <a\nhref=\"https://redirect.github.com/actions/setup-python/issues/182\">#182</a>\n&amp; <a\nhref=\"https://redirect.github.com/actions/setup-python/issues/183\">#183</a>\nby <a\nhref=\"https://github.com/aparnajyothi-y\"><code>@​aparnajyothi-y</code></a>\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1163\">actions/setup-python#1163</a></li>\n<li>Upgrade setuptools to 78.1.1 to fix path traversal vulnerability in\nPackageIndex.download by <a\nhref=\"https://github.com/aparnajyothi-y\"><code>@​aparnajyothi-y</code></a>\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1165\">actions/setup-python#1165</a></li>\n<li>Upgrade actions/checkout from 4 to 5 by <a\nhref=\"https://github.com/dependabot\"><code>@​dependabot</code></a>[bot]\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1181\">actions/setup-python#1181</a></li>\n<li>Upgrade <code>@​actions/tool-cache</code> from 2.0.1 to 2.0.2 by <a\nhref=\"https://github.com/dependabot\"><code>@​dependabot</code></a>[bot]\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1095\">actions/setup-python#1095</a></li>\n</ul>\n<h2>New Contributors</h2>\n<ul>\n<li><a href=\"https://github.com/krystof-k\"><code>@​krystof-k</code></a>\nmade their first contribution in <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/787\">actions/setup-python#787</a></li>\n<li><a href=\"https://github.com/cdce8p\"><code>@​cdce8p</code></a> made\ntheir first contribution in <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1110\">actions/setup-python#1110</a></li>\n<li><a href=\"https://github.com/aradkdj\"><code>@​aradkdj</code></a> made\ntheir first contribution in <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1067\">actions/setup-python#1067</a></li>\n</ul>\n<p><strong>Full Changelog</strong>: <a\nhref=\"https://github.com/actions/setup-python/compare/v5...v6.0.0\">https://github.com/actions/setup-python/compare/v5...v6.0.0</a></p>\n<h2>v5.6.0</h2>\n<h2>What's Changed</h2>\n<ul>\n<li>Workflow updates related to Ubuntu 20.04 by <a\nhref=\"https://github.com/aparnajyothi-y\"><code>@​aparnajyothi-y</code></a>\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1065\">actions/setup-python#1065</a></li>\n<li>Fix for Candidate Not Iterable Error by <a\nhref=\"https://github.com/aparnajyothi-y\"><code>@​aparnajyothi-y</code></a>\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1082\">actions/setup-python#1082</a></li>\n<li>Upgrade semver and <code>@​types/semver</code> by <a\nhref=\"https://github.com/dependabot\"><code>@​dependabot</code></a> in <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1091\">actions/setup-python#1091</a></li>\n<li>Upgrade prettier from 2.8.8 to 3.5.3 by <a\nhref=\"https://github.com/dependabot\"><code>@​dependabot</code></a> in <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1046\">actions/setup-python#1046</a></li>\n<li>Upgrade ts-jest from 29.1.2 to 29.3.2 by <a\nhref=\"https://github.com/dependabot\"><code>@​dependabot</code></a> in <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1081\">actions/setup-python#1081</a></li>\n</ul>\n<p><strong>Full Changelog</strong>: <a\nhref=\"https://github.com/actions/setup-python/compare/v5...v5.6.0\">https://github.com/actions/setup-python/compare/v5...v5.6.0</a></p>\n<h2>v5.5.0</h2>\n<h2>What's Changed</h2>\n<h3>Enhancements:</h3>\n<ul>\n<li>Support free threaded Python versions like '3.13t' by <a\nhref=\"https://github.com/colesbury\"><code>@​colesbury</code></a> in <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/973\">actions/setup-python#973</a></li>\n<li>Enhance Workflows: Include ubuntu-arm runners, Add e2e Testing for\nfree threaded and Upgrade <code>@​action/cache</code> from 4.0.0 to\n4.0.3 by <a\nhref=\"https://github.com/priya-kinthali\"><code>@​priya-kinthali</code></a>\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1056\">actions/setup-python#1056</a></li>\n<li>Add support for .tool-versions file in setup-python by <a\nhref=\"https://github.com/mahabaleshwars\"><code>@​mahabaleshwars</code></a>\nin <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1043\">actions/setup-python#1043</a></li>\n</ul>\n<h3>Bug fixes:</h3>\n<ul>\n<li>Fix architecture for pypy on Linux ARM64 by <a\nhref=\"https://github.com/mayeut\"><code>@​mayeut</code></a> in <a\nhref=\"https://redirect.github.com/actions/setup-python/pull/1011\">actions/setup-python#1011</a>\nThis update maps arm64 to aarch64 for Linux ARM64 PyPy\ninstallations.</li>\n</ul>\n<!-- raw HTML omitted -->\n</blockquote>\n<p>... (truncated)</p>\n</details>\n<details>\n<summary>Commits</summary>\n<ul>\n<li><a\nhref=\"https://github.com/actions/setup-python/commit/a309ff8b426b58ec0e2a45f0f869d46889d02405\"><code>a309ff8</code></a>\nBump urllib3 from 2.6.0 to 2.6.3 in /<strong>tests</strong>/data (<a\nhref=\"https://redirect.github.com/actions/setup-python/issues/1264\">#1264</a>)</li>\n<li><a\nhref=\"https://github.com/actions/setup-python/commit/bfe8cc55a7890e3d6672eda6460ef37bfcc70755\"><code>bfe8cc5</code></a>\nUpgrade <a href=\"https://github.com/actions\"><code>@​actions</code></a>\ndependencies to Node 24 compatible versions (<a\nhref=\"https://redirect.github.com/actions/setup-python/issues/1259\">#1259</a>)</li>\n<li><a\nhref=\"https://github.com/actions/setup-python/commit/4f41a90a1f38628c7ccc608d05fbafe701bc20ae\"><code>4f41a90</code></a>\nBump urllib3 from 2.5.0 to 2.6.0 in /<strong>tests</strong>/data (<a\nhref=\"https://redirect.github.com/actions/setup-python/issues/1253\">#1253</a>)</li>\n<li><a\nhref=\"https://github.com/actions/setup-python/commit/83679a892e2d95755f2dac6acb0bfd1e9ac5d548\"><code>83679a8</code></a>\nBump <code>@​types/node</code> from 24.1.0 to 24.9.1 and update macos-13\nto macos-15-intel ...</li>\n<li><a\nhref=\"https://github.com/actions/setup-python/commit/bfc4944b43a5d84377eca3cf6ab5b7992ba61923\"><code>bfc4944</code></a>\nBump prettier from 3.5.3 to 3.6.2 (<a\nhref=\"https://redirect.github.com/actions/setup-python/issues/1234\">#1234</a>)</li>\n<li><a\nhref=\"https://github.com/actions/setup-python/commit/97aeb3efb8a852c559869050c7fb175b4efcc8cf\"><code>97aeb3e</code></a>\nBump requests from 2.32.2 to 2.32.4 in /<strong>tests</strong>/data (<a\nhref=\"https://redirect.github.com/actions/setup-python/issues/1130\">#1130</a>)</li>\n<li><a\nhref=\"https://github.com/actions/setup-python/commit/443da59188462e2402e2942686db5aa6723f4bed\"><code>443da59</code></a>\nBump actions/publish-action from 0.3.0 to 0.4.0 &amp; Documentation\nupdate for pi...</li>\n<li><a\nhref=\"https://github.com/actions/setup-python/commit/cfd55ca82492758d853442341ad4d8010466803a\"><code>cfd55ca</code></a>\ngraalpy: add graalpy early-access and windows builds (<a\nhref=\"https://redirect.github.com/actions/setup-python/issues/880\">#880</a>)</li>\n<li><a\nhref=\"https://github.com/actions/setup-python/commit/bba65e51ff35d50c6dbaaacd8a4681db13aa7cb4\"><code>bba65e5</code></a>\nBump typescript from 5.4.2 to 5.9.3 and update docs/advanced-usage.md\n(<a\nhref=\"https://redirect.github.com/actions/setup-python/issues/1094\">#1094</a>)</li>\n<li><a\nhref=\"https://github.com/actions/setup-python/commit/18566f86b301499665bd3eb1a2247e0849c64fa5\"><code>18566f8</code></a>\nImprove wording and &quot;fix example&quot; (remove 3.13) on testing\nagainst pre-releas...</li>\n<li>Additional commits viewable in <a\nhref=\"https://github.com/actions/setup-python/compare/v5...v6\">compare\nview</a></li>\n</ul>\n</details>\n<br />\n\n\n[![Dependabot compatibility\nscore](https://dependabot-badges.githubapp.com/badges/compatibility_score?dependency-name=actions/setup-python&package-manager=github_actions&previous-version=5&new-version=6)](https://docs.github.com/en/github/managing-security-vulnerabilities/about-dependabot-security-updates#about-compatibility-scores)\n\nDependabot will resolve any conflicts with this PR as long as you don't\nalter it yourself. You can also trigger a rebase manually by commenting\n`@dependabot rebase`.\n\n[//]: # (dependabot-automerge-start)\n[//]: # (dependabot-automerge-end)\n\n---\n\n<details>\n<summary>Dependabot commands and options</summary>\n<br />\n\nYou can trigger Dependabot actions by commenting on this PR:\n- `@dependabot rebase` will rebase this PR\n- `@dependabot recreate` will recreate this PR, overwriting any edits\nthat have been made to it\n- `@dependabot merge` will merge this PR after your CI passes on it\n- `@dependabot squash and merge` will squash and merge this PR after\nyour CI passes on it\n- `@dependabot cancel merge` will cancel a previously requested merge\nand block automerging\n- `@dependabot reopen` will reopen this PR if it is closed\n- `@dependabot close` will close this PR and stop Dependabot recreating\nit. You can achieve the same result by closing it manually\n- `@dependabot show <dependency name> ignore conditions` will show all\nof the ignore conditions of the specified dependency\n- `@dependabot ignore this major version` will close this PR and stop\nDependabot creating any more for this major version (unless you reopen\nthe PR or upgrade to it yourself)\n- `@dependabot ignore this minor version` will close this PR and stop\nDependabot creating any more for this minor version (unless you reopen\nthe PR or upgrade to it yourself)\n- `@dependabot ignore this dependency` will close this PR and stop\nDependabot creating any more for this dependency (unless you reopen the\nPR or upgrade to it yourself)\n\n\n</details>",
          "timestamp": "2026-01-31T03:15:51-05:00",
          "tree_id": "4acbd2901633ae4055349ded1f7b99220b76a1e1",
          "url": "https://github.com/zeroae/zae-limiter/commit/989f24d8f458d8fdc9160864cb344b94bc71919d"
        },
        "date": 1769848220613,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 16.345311254787973,
            "unit": "iter/sec",
            "range": "stddev: 0.01774651089823584",
            "extra": "mean: 61.179624200002536 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.655518367017354,
            "unit": "iter/sec",
            "range": "stddev: 0.007695233864205271",
            "extra": "mean: 48.413212499996895 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 29.67217239375328,
            "unit": "iter/sec",
            "range": "stddev: 0.005617299642648737",
            "extra": "mean: 33.70161061110998 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 34.4624828597024,
            "unit": "iter/sec",
            "range": "stddev: 0.005463437838846587",
            "extra": "mean: 29.017061947365317 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 25.289911030579717,
            "unit": "iter/sec",
            "range": "stddev: 0.003909154066640454",
            "extra": "mean: 39.541459785715865 msec\nrounds: 14"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 232.34613511663895,
            "unit": "iter/sec",
            "range": "stddev: 0.0007175107796314815",
            "extra": "mean: 4.303923538465552 msec\nrounds: 169"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 29.150110639849494,
            "unit": "iter/sec",
            "range": "stddev: 0.004300791397271784",
            "extra": "mean: 34.30518711764187 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 27.794792764021757,
            "unit": "iter/sec",
            "range": "stddev: 0.003609842380613507",
            "extra": "mean: 35.97796207692629 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 32.27944657952981,
            "unit": "iter/sec",
            "range": "stddev: 0.0033366634832162814",
            "extra": "mean: 30.979465448275544 msec\nrounds: 29"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 12.85487444140219,
            "unit": "iter/sec",
            "range": "stddev: 0.005228566446791045",
            "extra": "mean: 77.79150271427478 msec\nrounds: 14"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 31.25184724460506,
            "unit": "iter/sec",
            "range": "stddev: 0.004154797438319744",
            "extra": "mean: 31.998108533332474 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9175181757990885,
            "unit": "iter/sec",
            "range": "stddev: 0.0018804939658866233",
            "extra": "mean: 521.5074426000001 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9054794081332669,
            "unit": "iter/sec",
            "range": "stddev: 0.005798123362121862",
            "extra": "mean: 524.8023125999907 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9254032643315698,
            "unit": "iter/sec",
            "range": "stddev: 0.014581547801885406",
            "extra": "mean: 1.0806099768000195 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.9002657092648666,
            "unit": "iter/sec",
            "range": "stddev: 0.014451705131162684",
            "extra": "mean: 1.1107831717999943 sec\nrounds: 5"
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
          "id": "ee91aa0e55433a1c2d694b7281d23ba45d678387",
          "message": "🐛 fix(infra): component-based IAM role naming to prevent 64-char limit overflow (#269)\n\n## Summary\n\n- Implements component-based IAM role naming per ADR-116 to prevent role\nnames exceeding AWS 64-character limit\n- Adds `ROLE_COMPONENTS` constant defining role suffixes (aggregator,\napp, admin, readonly)\n- Updates `get_role_name()` to accept component parameter and validate\ncombined length\n- Validates role names at construction time with clear error messages\nshowing which component exceeds the limit\n\n## Test Plan\n\n- [x] Unit tests for role name validation with various stack name\nlengths\n- [x] Unit tests for `role_name_format` template validation\n- [x] LocalStack e2e tests for role naming with permission boundaries\n- [x] All 1051 tests passing with 100% patch coverage\n\nFixes #252\n\n🤖 Generated with [Claude Code](https://claude.ai/code)",
          "timestamp": "2026-01-31T12:42:53-05:00",
          "tree_id": "2cda943e4575adb21951e675ab586d75905e8b2a",
          "url": "https://github.com/zeroae/zae-limiter/commit/ee91aa0e55433a1c2d694b7281d23ba45d678387"
        },
        "date": 1769882211354,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 15.167320602661201,
            "unit": "iter/sec",
            "range": "stddev: 0.023207495407104938",
            "extra": "mean: 65.93122319999907 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.595611956376786,
            "unit": "iter/sec",
            "range": "stddev: 0.005386636136198635",
            "extra": "mean: 46.30570330769064 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 28.59129521021442,
            "unit": "iter/sec",
            "range": "stddev: 0.007656329532475043",
            "extra": "mean: 34.97568027777712 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 36.32882832611355,
            "unit": "iter/sec",
            "range": "stddev: 0.004475067787276056",
            "extra": "mean: 27.526348800002154 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 30.368456957934438,
            "unit": "iter/sec",
            "range": "stddev: 0.0033246704420161963",
            "extra": "mean: 32.92890387500336 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 236.76732409679963,
            "unit": "iter/sec",
            "range": "stddev: 0.000559380097418361",
            "extra": "mean: 4.223555779137671 msec\nrounds: 163"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 29.283475897346,
            "unit": "iter/sec",
            "range": "stddev: 0.00471681128749872",
            "extra": "mean: 34.14895156249642 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 31.909874961723887,
            "unit": "iter/sec",
            "range": "stddev: 0.003073450293355558",
            "extra": "mean: 31.338261312509275 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 32.38911925110065,
            "unit": "iter/sec",
            "range": "stddev: 0.004486100049057437",
            "extra": "mean: 30.874566000000694 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 13.35987600761489,
            "unit": "iter/sec",
            "range": "stddev: 0.005222486208513519",
            "extra": "mean: 74.85099408332967 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 33.227632121663554,
            "unit": "iter/sec",
            "range": "stddev: 0.0037764748613327375",
            "extra": "mean: 30.095433714279807 msec\nrounds: 21"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9246015168949744,
            "unit": "iter/sec",
            "range": "stddev: 0.0016464133763138126",
            "extra": "mean: 519.5880764000094 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9242717441085222,
            "unit": "iter/sec",
            "range": "stddev: 0.002410430753463402",
            "extra": "mean: 519.6771210000179 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9342401433140707,
            "unit": "iter/sec",
            "range": "stddev: 0.019377694071809635",
            "extra": "mean: 1.0703886010000132 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.9076615364106198,
            "unit": "iter/sec",
            "range": "stddev: 0.00820530255466466",
            "extra": "mean: 1.1017322646000138 sec\nrounds: 5"
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
          "id": "23c7782612815d53c2e3374599611d0dd6b5005c",
          "message": "🐛 fix(cli): record lambda_version for LocalStack deployments (#275)\n\n## Summary\n\n- Removes the `endpoint_url` condition that was preventing\n`lambda_version` from being recorded when deploying to LocalStack\n- The Lambda function IS deployed to LocalStack, so its version should\nbe tracked\n\n## Root Cause\n\nIn `cli.py:327`, the deploy command explicitly set `lambda_version=None`\nwhen `endpoint_url` was set:\n\n```python\nlambda_version=__version__ if not endpoint_url else None,\n```\n\n## Test plan\n\n- [x] Reproduced issue with conda-forge `zae-limiter=0.7.0`\n- [x] Verified fix shows Lambda version in status output\n- [x] All unit tests pass\n\nFixes #274\n\n🤖 Generated with [Claude Code](https://claude.ai/code)",
          "timestamp": "2026-01-31T19:38:34-05:00",
          "tree_id": "1a4c6bf0b18b066026c0f9fcd50e3c38ecede328",
          "url": "https://github.com/zeroae/zae-limiter/commit/23c7782612815d53c2e3374599611d0dd6b5005c"
        },
        "date": 1769907187413,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 16.759212153459888,
            "unit": "iter/sec",
            "range": "stddev: 0.016231831016600248",
            "extra": "mean: 59.6686759999964 msec\nrounds: 6"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.07816510493302,
            "unit": "iter/sec",
            "range": "stddev: 0.005709835869173397",
            "extra": "mean: 47.442459769231306 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 28.070233430715053,
            "unit": "iter/sec",
            "range": "stddev: 0.0042964254882705815",
            "extra": "mean: 35.62492639999846 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 34.893050879939885,
            "unit": "iter/sec",
            "range": "stddev: 0.004499547969494956",
            "extra": "mean: 28.659001571424728 msec\nrounds: 14"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 28.200231764435326,
            "unit": "iter/sec",
            "range": "stddev: 0.004288534994161308",
            "extra": "mean: 35.46070147058679 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 215.71840003899263,
            "unit": "iter/sec",
            "range": "stddev: 0.0008693708475921893",
            "extra": "mean: 4.635673173077692 msec\nrounds: 156"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 28.409188464783558,
            "unit": "iter/sec",
            "range": "stddev: 0.006063912645722548",
            "extra": "mean: 35.19987912500966 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 29.01785977691717,
            "unit": "iter/sec",
            "range": "stddev: 0.006462798859634876",
            "extra": "mean: 34.46153533333529 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 31.616532972073507,
            "unit": "iter/sec",
            "range": "stddev: 0.0029890025738762618",
            "extra": "mean: 31.6290214642854 msec\nrounds: 28"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 12.377727782463582,
            "unit": "iter/sec",
            "range": "stddev: 0.005520989160111737",
            "extra": "mean: 80.7902724615395 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 28.84199961496127,
            "unit": "iter/sec",
            "range": "stddev: 0.005164784895624852",
            "extra": "mean: 34.671659848482484 msec\nrounds: 33"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9207423589929693,
            "unit": "iter/sec",
            "range": "stddev: 0.0015132333385612907",
            "extra": "mean: 520.6320333999884 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.906835932068556,
            "unit": "iter/sec",
            "range": "stddev: 0.007041502178657261",
            "extra": "mean: 524.4289679999838 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.921626249161356,
            "unit": "iter/sec",
            "range": "stddev: 0.027719151872149565",
            "extra": "mean: 1.0850385401999574 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.9052225568564555,
            "unit": "iter/sec",
            "range": "stddev: 0.009881920114460081",
            "extra": "mean: 1.1047007085999667 sec\nrounds: 5"
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
          "id": "c5417f02cb6a9439de92093f706b12c3a2211e85",
          "message": "✨ feat(infra): add DynamoDB table deletion protection option (#277)\n\n## Summary\n\n- Add `--enable-deletion-protection/--no-deletion-protection` CLI flag\nto the `deploy` command\n- Add `enable_deletion_protection: bool = False` field to `StackOptions`\n- Configure CloudFormation template to set `DeletionProtectionEnabled`\non the DynamoDB table\n- This prevents accidental table deletion in production environments\n\n## Test plan\n\n- [x] Unit test verifies `StackOptions(enable_deletion_protection=True)`\nformats the CloudFormation parameter correctly\n(`test_format_parameters_with_deletion_protection`)\n- [x] E2E test: deploy with `--enable-deletion-protection` and verify\nCLI output shows \"enabled\"\n(`test_deploy_with_deletion_protection_enabled` - LocalStack skips\nDynamoDB assertion as it doesn't support this property)\n- [x] E2E test: deploy with `--no-deletion-protection` (default) and\nverify table has `DeletionProtectionEnabled=false`\n(`test_deploy_without_deletion_protection`)\n\nCloses #273\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)",
          "timestamp": "2026-01-31T20:16:15-05:00",
          "tree_id": "790cd2375f8e12f77a39d4ad8d5090b3e49c85aa",
          "url": "https://github.com/zeroae/zae-limiter/commit/c5417f02cb6a9439de92093f706b12c3a2211e85"
        },
        "date": 1769909417028,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 14.035312560080103,
            "unit": "iter/sec",
            "range": "stddev: 0.027050527415357073",
            "extra": "mean: 71.24885860000347 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 18.855797595858533,
            "unit": "iter/sec",
            "range": "stddev: 0.006275308840170864",
            "extra": "mean: 53.034086461536845 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 28.104375761933557,
            "unit": "iter/sec",
            "range": "stddev: 0.0057331959143870044",
            "extra": "mean: 35.58164780000084 msec\nrounds: 15"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 32.316142821601375,
            "unit": "iter/sec",
            "range": "stddev: 0.0030005680263596943",
            "extra": "mean: 30.944287055556668 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 26.13284028302251,
            "unit": "iter/sec",
            "range": "stddev: 0.002688417627997018",
            "extra": "mean: 38.26602807692744 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 232.10506142470967,
            "unit": "iter/sec",
            "range": "stddev: 0.0006806257475056455",
            "extra": "mean: 4.308393767295679 msec\nrounds: 159"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 27.415183332843668,
            "unit": "iter/sec",
            "range": "stddev: 0.005292174146378746",
            "extra": "mean: 36.47613761539175 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 25.96926762650403,
            "unit": "iter/sec",
            "range": "stddev: 0.004727368834867706",
            "extra": "mean: 38.50705435294632 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 30.200813939925073,
            "unit": "iter/sec",
            "range": "stddev: 0.004850913472347013",
            "extra": "mean: 33.11169036666305 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 12.549275139100349,
            "unit": "iter/sec",
            "range": "stddev: 0.007229654414892625",
            "extra": "mean: 79.68587738460323 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 29.50437595904049,
            "unit": "iter/sec",
            "range": "stddev: 0.0060635522534295975",
            "extra": "mean: 33.893277437497815 msec\nrounds: 32"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9237015408285025,
            "unit": "iter/sec",
            "range": "stddev: 0.001178404212673991",
            "extra": "mean: 519.8311581999974 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.914020602088974,
            "unit": "iter/sec",
            "range": "stddev: 0.005243588872060382",
            "extra": "mean: 522.4604160000126 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9302252305606868,
            "unit": "iter/sec",
            "range": "stddev: 0.011661157222039833",
            "extra": "mean: 1.07500846800001 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.8856209138663265,
            "unit": "iter/sec",
            "range": "stddev: 0.03337837693771875",
            "extra": "mean: 1.1291512929999954 sec\nrounds: 5"
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
          "id": "0d5059e958f2e47f6a9c5984c225c25f1ebd678a",
          "message": "🐛 fix(ci): include _version.py in sdist for correct version reporting (#279)\n\n## Summary\n\n- Configure hatch-vcs to write `_version.py` during build, ensuring it's\nincluded in the sdist\n- Add `_version.py` to `.gitignore` since it's a generated file that\nshould not be committed\n\nThis fixes the issue where `zae_limiter.__version__` returns\n`0.0.0+unknown` when installed from PyPI sdist, because the generated\nversion file was not being included in the source distribution.\n\nFixes #278\n\n## Test plan\n\n- [ ] Verify the PR passes CI checks\n- [x] Build sdist locally and verify `_version.py` is included: `uv\nbuild --sdist && tar -tzf dist/*.tar.gz | grep _version`\n- [x] Install from sdist and verify version: `pip install dist/*.tar.gz\n&& python -c \"import zae_limiter; print(zae_limiter.__version__)\"`\n\n🤖 Generated with [Claude Code](https://claude.ai/code)\n\nCo-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>",
          "timestamp": "2026-01-31T23:25:44-05:00",
          "tree_id": "18871037fb7cc61951beeaeb83b5b0764427ce02",
          "url": "https://github.com/zeroae/zae-limiter/commit/0d5059e958f2e47f6a9c5984c225c25f1ebd678a"
        },
        "date": 1769920782674,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 16.866621447816097,
            "unit": "iter/sec",
            "range": "stddev: 0.01876146124106589",
            "extra": "mean: 59.28869650000242 msec\nrounds: 6"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 21.562424061909645,
            "unit": "iter/sec",
            "range": "stddev: 0.007417547509594546",
            "extra": "mean: 46.376974923079985 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 32.08203834052251,
            "unit": "iter/sec",
            "range": "stddev: 0.006785042145980295",
            "extra": "mean: 31.17008929999656 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 36.76507123524853,
            "unit": "iter/sec",
            "range": "stddev: 0.003227466490233737",
            "extra": "mean: 27.199729699999864 msec\nrounds: 20"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 28.60771515927479,
            "unit": "iter/sec",
            "range": "stddev: 0.004402745716665169",
            "extra": "mean: 34.95560531249886 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 156.62060892544332,
            "unit": "iter/sec",
            "range": "stddev: 0.0015998388998855562",
            "extra": "mean: 6.384855778948182 msec\nrounds: 95"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 29.796170835867876,
            "unit": "iter/sec",
            "range": "stddev: 0.0026150056836113825",
            "extra": "mean: 33.561359461539446 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 27.227945347260224,
            "unit": "iter/sec",
            "range": "stddev: 0.005068564043761085",
            "extra": "mean: 36.72697249998791 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 31.0010180041975,
            "unit": "iter/sec",
            "range": "stddev: 0.0034109537219121446",
            "extra": "mean: 32.257005233331405 msec\nrounds: 30"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 12.856111672664266,
            "unit": "iter/sec",
            "range": "stddev: 0.007488679675875674",
            "extra": "mean: 77.78401630768992 msec\nrounds: 13"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 30.38831982557557,
            "unit": "iter/sec",
            "range": "stddev: 0.0034418783495671983",
            "extra": "mean: 32.90738039285657 msec\nrounds: 28"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.920112712152238,
            "unit": "iter/sec",
            "range": "stddev: 0.0012348116586808134",
            "extra": "mean: 520.802760000015 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.9226359731716733,
            "unit": "iter/sec",
            "range": "stddev: 0.0007099761669535034",
            "extra": "mean: 520.1192602000219 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9265046595122155,
            "unit": "iter/sec",
            "range": "stddev: 0.020432412831620058",
            "extra": "mean: 1.0793253867999737 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.8893411736591501,
            "unit": "iter/sec",
            "range": "stddev: 0.027753975208001675",
            "extra": "mean: 1.1244278681999504 sec\nrounds: 5"
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
          "id": "5c144323f000406cb88a02fa60038a5c61ef0134",
          "message": "📝 docs(cli): auto-generate CLI docs with mkdocs-click (#280)\n\n## Summary\n- Integrate `mkdocs-click` plugin to auto-generate CLI documentation\nfrom Click command groups\n- Replace ~1200 lines of manually-maintained CLI docs with\nauto-generated content\n- Add detailed help strings to all CLI commands and options for better\ndiscoverability\n- Improve help text formatting for mkdocs-click rendering (multi-line\ndescriptions, examples)\n\n## Changes\n- **`pyproject.toml`**: Add `mkdocs-click` as docs dependency\n- **`mkdocs.yml`**: Configure `mkdocs-click` plugin\n- **`docs/cli.md`**: Replace manual documentation with auto-generated\ncommand reference\n- **`src/zae_limiter/cli.py`**: Add comprehensive help strings to all\ncommands and options\n- **`src/zae_limiter/local.py`**: Add help strings to local subcommands\n- **`tests/unit/test_cli.py`**: Update test assertions to match new help\ntext format\n\n## Benefits\n- Documentation stays automatically in sync with CLI implementation\n- Help text is available both in terminal (`--help`) and documentation\nsite\n- Reduces maintenance burden - no more manual updates when commands\nchange\n\n## Test plan\n- [x] All unit tests pass (1060 passed)\n- [x] Pre-commit hooks pass (ruff, mypy, patch coverage 100%)\n- [ ] Verify docs build: `uv run mkdocs serve --livereload --dirty`\n- [ ] Review generated CLI docs at http://localhost:8000/cli/\n\n🤖 Generated with [Claude Code](https://claude.ai/code)",
          "timestamp": "2026-02-01T01:38:57-05:00",
          "tree_id": "2ed965b33692315a6b10c91219f1a052b6bd519a",
          "url": "https://github.com/zeroae/zae-limiter/commit/5c144323f000406cb88a02fa60038a5c61ef0134"
        },
        "date": 1769928823678,
        "tool": "pytest",
        "benches": [
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_acquire_release_localstack",
            "value": 18.592961465243715,
            "unit": "iter/sec",
            "range": "stddev: 0.01599684429114562",
            "extra": "mean: 53.78379349999326 msec\nrounds: 6"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackBenchmarks::test_cascade_localstack",
            "value": 20.945185130965996,
            "unit": "iter/sec",
            "range": "stddev: 0.005745284887751637",
            "extra": "mean: 47.74366966666577 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_realistic_latency",
            "value": 29.217856486497013,
            "unit": "iter/sec",
            "range": "stddev: 0.004831875698898802",
            "extra": "mean: 34.225645555557726 msec\nrounds: 18"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_acquire_two_limits_realistic_latency",
            "value": 34.807246937704775,
            "unit": "iter/sec",
            "range": "stddev: 0.004139790111268758",
            "extra": "mean: 28.729649368412275 msec\nrounds: 19"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_cascade_realistic_latency",
            "value": 28.272932542251034,
            "unit": "iter/sec",
            "range": "stddev: 0.003672849609906274",
            "extra": "mean: 35.36951812499822 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackLatencyBenchmarks::test_available_realistic_latency",
            "value": 227.6186657981082,
            "unit": "iter/sec",
            "range": "stddev: 0.0007203876966311225",
            "extra": "mean: 4.393312808919519 msec\nrounds: 157"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_batchgetitem_optimization",
            "value": 29.5031907553327,
            "unit": "iter/sec",
            "range": "stddev: 0.0038720405359016783",
            "extra": "mean: 33.89463899999529 msec\nrounds: 17"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_multiple_resources",
            "value": 30.595103386405793,
            "unit": "iter/sec",
            "range": "stddev: 0.0027895995559697726",
            "extra": "mean: 32.68496881250371 msec\nrounds: 16"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestCascadeOptimizationBenchmarks::test_cascade_with_config_cache_optimization",
            "value": 31.615951515436603,
            "unit": "iter/sec",
            "range": "stddev: 0.003085858911847784",
            "extra": "mean: 31.6296031612949 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_disabled_localstack",
            "value": 13.048058129273954,
            "unit": "iter/sec",
            "range": "stddev: 0.008075474952955181",
            "extra": "mean: 76.63975666666072 msec\nrounds: 12"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLocalStackOptimizationComparison::test_cascade_cache_enabled_localstack",
            "value": 31.32204974476831,
            "unit": "iter/sec",
            "range": "stddev: 0.004238958777111564",
            "extra": "mean: 31.926390774187087 msec\nrounds: 31"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_first_invocation",
            "value": 1.9207985516220398,
            "unit": "iter/sec",
            "range": "stddev: 0.0009858990306645511",
            "extra": "mean: 520.6168023999908 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_subsequent_invocation",
            "value": 1.8898086409061519,
            "unit": "iter/sec",
            "range": "stddev: 0.012860066936761453",
            "extra": "mean: 529.1541050000205 msec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_cold_start_multiple_concurrent_events",
            "value": 0.9316031866507362,
            "unit": "iter/sec",
            "range": "stddev: 0.006049814237167767",
            "extra": "mean: 1.0734183977999918 sec\nrounds: 5"
          },
          {
            "name": "tests/benchmark/test_localstack.py::TestLambdaColdStartBenchmarks::test_lambda_warm_start_sustained_load",
            "value": 0.9023412397740772,
            "unit": "iter/sec",
            "range": "stddev: 0.01601156751616052",
            "extra": "mean: 1.1082281912000098 sec\nrounds: 5"
          }
        ]
      }
    ]
  }
}