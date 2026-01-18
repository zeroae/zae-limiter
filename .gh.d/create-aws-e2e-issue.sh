#!/usr/bin/env bash
# Create GitHub issue for AWS E2E testing with OIDC authentication
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

gh issue create \
  --title "feat(ci): Add AWS E2E tests with GitHub OIDC authentication" \
  --label "area/ci" \
  --label "area/infra" \
  --label "testing" \
  --body-file "${SCRIPT_DIR}/aws-e2e-issue-body.md"
