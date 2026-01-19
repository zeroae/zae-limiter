#!/bin/bash
result='[]'

while IFS= read -r line; do
    path=$(echo "$line" | awk '{print $1}')
    branch=$(echo "$line" | sed -n 's/.*\[\([^]]*\)\].*/\1/p')

    [[ -z "$branch" ]] && continue

    pr_number="null"
    pr_state="null"
    ci_success=0
    ci_failure=0
    ci_pending=0
    ci_skipped=0

    if [[ "$branch" != "main" && "$branch" != "master" ]]; then
        pr_json=$(gh pr list --state all --head "$branch" --json number,state --limit 1 2>/dev/null)
        if [[ -n "$pr_json" && "$pr_json" != "[]" ]]; then
            pr_number=$(echo "$pr_json" | jq -r '.[0].number')
            pr_state=$(echo "$pr_json" | jq -r '.[0].state')

            if [[ "$pr_number" != "null" ]]; then
                checks_json=$(gh pr checks "$pr_number" --json name,state 2>/dev/null)
                if [[ -n "$checks_json" ]]; then
                    ci_success=$(echo "$checks_json" | jq '[.[].state | select(. == "SUCCESS")] | length')
                    ci_failure=$(echo "$checks_json" | jq '[.[].state | select(. == "FAILURE" or . == "ERROR")] | length')
                    ci_pending=$(echo "$checks_json" | jq '[.[].state | select(. == "PENDING" or . == "QUEUED" or . == "IN_PROGRESS")] | length')
                    ci_skipped=$(echo "$checks_json" | jq '[.[].state | select(. == "SKIPPED")] | length')
                fi
            fi
        fi
    fi

    safe_to_remove="false"
    if [[ "$branch" == "main" || "$branch" == "master" ]]; then
        safe_reason="main branch"
    elif [[ "$pr_state" == "MERGED" || "$pr_state" == "CLOSED" ]]; then
        safe_to_remove="true"
        safe_reason="PR $pr_state"
    else
        safe_reason="PR open or no PR"
    fi

    entry=$(jq -n \
        --arg path "$path" \
        --arg branch "$branch" \
        --argjson pr_number "$pr_number" \
        --arg pr_state "$pr_state" \
        --argjson ci_success "$ci_success" \
        --argjson ci_failure "$ci_failure" \
        --argjson ci_pending "$ci_pending" \
        --argjson ci_skipped "$ci_skipped" \
        --argjson safe_to_remove "$safe_to_remove" \
        --arg safe_reason "$safe_reason" \
        '{
            path: $path,
            branch: $branch,
            pr: (if $pr_number == null then null else {number: $pr_number, state: $pr_state} end),
            ci: {success: $ci_success, failure: $ci_failure, pending: $ci_pending, skipped: $ci_skipped},
            safe_to_remove: $safe_to_remove,
            safe_reason: $safe_reason
        }')

    result=$(echo "$result" | jq --argjson entry "$entry" '. + [$entry]')
done < <(git worktree list)

echo "$result" | jq .
