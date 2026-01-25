---
description: Create and run autonomous Claude agents for GitHub issues
allowed-tools: Bash(git:*), Bash(gh:*), Bash(docker:*), Bash(ls:*), Bash(mkdir:*), Bash(cat:*), Glob, Grep, Read, Write, Edit, WebFetch, AskUserQuestion, Task
argument-hint: <create|run|status> <#issue-number> [options]
---

# Autonomous Agent Manager

Create, configure, and run autonomous Claude Code agents for implementing GitHub issues.

## Commands

Parse `$ARGUMENTS` to determine the action:

### `create #<issue-number>` or `create <issue-number>`

Create an agent definition for a GitHub issue. This involves:

1. **Fetch issue details:**
```bash
gh issue view <issue-number> --json title,body,labels,milestone,assignees
```

2. **Analyze the issue:**
   - Determine issue type from emoji/labels (feat, fix, refactor, etc.)
   - Identify related issues mentioned in the body
   - Extract acceptance criteria if present
   - Identify key files likely to be affected

3. **Research the codebase:**
   - Use Task tool with Explore agent to understand relevant code
   - Identify existing patterns and conventions
   - Find related ADRs that apply

4. **Ask clarifying questions** using AskUserQuestion:
   - Design decisions that affect implementation
   - Scope boundaries (what's in vs out)
   - Testing requirements (unit only, integration, e2e)
   - Any invariants that must be preserved

5. **Create agent definition** at `.claude/agents/<type>-<issue-number>.md`:

```markdown
---
name: <type>-<issue-number>
description: <one-line description from issue title>
tools: Read, Edit, Write, Bash, Glob, Grep, TodoWrite, Skill
model: opus
---

# <Issue Title> Agent

You are implementing issue #<number>: <title>

## Critical Invariants

<List invariants based on user answers and codebase analysis>

## Background

<Context from issue body and codebase exploration>

## Implementation Phases

<Break down into phases with checkpoints>

### Phase 1: <name>
<steps>

**Checkpoint:** `git add -A && git commit -m "<message> #<issue>"`

### Phase N: Validation
<validation steps>

## Key Files

| File | Purpose |
|------|---------|
<files identified during exploration>

## Design Decisions

<Decisions confirmed with user via AskUserQuestion>

## Success Criteria

<From issue acceptance criteria or inferred>

## Rollback Strategy

If stuck:
1. `git stash push -m "work in progress"`
2. Report what failed and why
3. Ask human for guidance

Do NOT:
- Modify existing test files (unless explicitly allowed)
- Skip validation steps
- Proceed if tests are failing
```

6. **Create/update Docker files** if they don't exist:
   - `Dockerfile.claude-agent` - Claude Code container image
   - `docker-compose.claude-agent.yml` - Compose file with all mounts

7. **Show next steps:**
```
Agent created: .claude/agents/<type>-<issue-number>.md

To run the agent:
  /agent run #<issue-number>

To run in Docker (autonomous mode):
  docker compose -f docker-compose.claude-agent.yml run --rm --build agent

To customize the agent, edit:
  .claude/agents/<type>-<issue-number>.md
```

### `run #<issue-number>` or `run <issue-number>`

Run an existing agent interactively in the current session:

1. **Find agent definition:**
```bash
ls .claude/agents/*-<issue-number>.md
```

2. **Read and execute the agent:**
   - Read the agent definition file
   - Follow its instructions phase by phase
   - Use TodoWrite to track progress
   - Commit after each phase checkpoint

### `docker #<issue-number>` or `docker <issue-number>`

Set up Docker for autonomous agent execution:

1. **Ensure Dockerfile.claude-agent exists**, create if not:

```dockerfile
FROM node:22-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    ca-certificates \
    sudo \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user with same UID as host user (for file permissions)
ARG USER_ID=501
ARG GROUP_ID=20
RUN groupadd -g ${GROUP_ID} agent || true && \
    useradd -m -u ${USER_ID} -g ${GROUP_ID} -s /bin/bash agent && \
    echo "agent ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Switch to non-root user
USER agent

# Install uv for the agent user
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/home/agent/.local/bin:$PATH"

# Set working directory
WORKDIR /workspace

# Default entrypoint
ENTRYPOINT ["claude"]
```

2. **Ensure docker-compose.claude-agent.yml exists**, create/update for the issue:

```yaml
# Docker Compose for running Claude Code agent autonomously
#
# Usage:
#   docker compose -f docker-compose.claude-agent.yml run --rm --build agent

services:
  agent:
    build:
      context: .
      dockerfile: Dockerfile.claude-agent
    working_dir: /workspace
    volumes:
      # Mount the project directory
      - .:/workspace
      # Mount the parent git repo (required for worktrees)
      - <REPO_ROOT>/.git:<REPO_ROOT>/.git
      # Git config for commits
      - ~/.gitconfig:/home/agent/.gitconfig:ro
      # SSH keys if needed for git push
      - ~/.ssh:/home/agent/.ssh:ro
      # Claude credentials (OAuth tokens from Max Plan)
      - ~/.claude:/home/agent/.claude
      # Docker socket for LocalStack Lambda execution
      - /var/run/docker.sock:/var/run/docker.sock
    stdin_open: true
    tty: true
    command:
      - "--dangerously-skip-permissions"
      - "--print"
      - "Read .claude/agents/<type>-<issue>.md and follow those instructions exactly to implement issue #<issue>. Start with Phase 1. After each phase, commit your work and proceed to the next phase."
```

**Important:** Detect if running in a worktree and mount the parent `.git` directory:
```bash
# Get the main repo's .git directory
REPO_ROOT=$(git rev-parse --show-toplevel)
GIT_COMMON_DIR=$(git rev-parse --git-common-dir)
# If GIT_COMMON_DIR != REPO_ROOT/.git, we're in a worktree
```

3. **Show run command:**
```
Docker setup complete.

To run the agent autonomously:
  docker compose -f docker-compose.claude-agent.yml run --rm --build agent

To run with a custom prompt:
  docker compose -f docker-compose.claude-agent.yml run --rm agent \
    --dangerously-skip-permissions "Do Phase 2 only"

To run interactively (for debugging):
  docker compose -f docker-compose.claude-agent.yml run --rm agent
```

### `status`

Show status of all agents:

1. **List agent definitions:**
```bash
ls -la .claude/agents/*.md
```

2. **For each agent, check:**
   - Issue status (open/closed) via `gh issue view`
   - Associated branch/PR if any
   - Last commit mentioning the issue

3. **Display as table:**
```
┌─────────────────────┬────────┬─────────┬──────────────────────┐
│ Agent               │ Issue  │ Status  │ Last Activity        │
├─────────────────────┼────────┼─────────┼──────────────────────┤
│ refactor-150        │ #150   │ Open    │ Phase 2 committed    │
│ feat-42             │ #42    │ Closed  │ PR #145 merged       │
└─────────────────────┴────────┴─────────┴──────────────────────┘
```

### `list`

List available agents (alias for `status`).

## Argument Parsing

- `create #<number>` or `create <number>`: Create new agent for issue
- `run #<number>` or `run <number>`: Run agent interactively
- `docker #<number>` or `docker <number>`: Setup Docker for autonomous run
- `status` or `list`: Show all agents and their status
- No arguments: Show help/usage

## Examples

```
/agent create #150       # Create agent for issue #150
/agent run #150          # Run agent interactively
/agent docker #150       # Setup Docker for autonomous execution
/agent status            # Show all agents
```

## Phase Design Guidelines

When creating phases, follow these principles:

1. **Each phase should be independently committable**
   - Clear deliverable at the end
   - Tests should pass after each phase

2. **Include validation after each phase**
   - Type checking: `uv run mypy src/`
   - Linting: `uv run ruff check .`
   - Tests: `uv run pytest tests/unit/ -x`

3. **Order phases by dependency**
   - Research/exploration first
   - Core implementation next
   - Integration last
   - Documentation/cleanup at end

4. **Include ADR enforcement if project uses ADRs**
   - Check for `docs/adr/` directory
   - Add `/adr enforce` after each phase if ADRs exist

## Docker Considerations

### Worktree Support
When the project uses git worktrees, the `.git` file points to the main repo. Mount the main repo's `.git` directory to allow git operations.

### OAuth Authentication
Mount `~/.claude` to use OAuth tokens from Max Plan instead of API keys.

### LocalStack Integration
Mount `/var/run/docker.sock` to allow LocalStack to spawn Lambda containers.

### File Permissions
Create a non-root user with matching UID/GID to avoid permission issues with mounted volumes.
