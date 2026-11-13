# CLAUDE.md

## Project overview

forgewright is a polling + webhook agent that monitors a GitLab or GitHub instance for `@forgewright` mentions (or whatever `bot_username` is configured) in issues and merge/pull requests, then dispatches an AI coding agent (Claude Code, Hermes Agent, or the deprecated OpenCode) to implement fixes, answer questions, or review code. It is designed to run as a systemd timer (every 5 min by default) and/or a long-lived webhook service, either natively or in Docker, against any self-hosted or cloud GitLab / GitHub instance.

## Commit conventions

Use **Conventional Commits** style: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`.

When an AI agent creates a commit, add a `Co-Authored-By` trailer so the commit is clearly attributed. The format is:

```
Co-Authored-By: Claude <model> <email>
```

- `<model>` is the exact model you are running as, e.g. `Opus 4.6 (1M context)`, `Sonnet 4.6`, `Haiku 4.5`.
- `<email>` should be the address configured for the bot in this deployment. When running as the hosted forgewright on `git.marcel-aust.de`, use `claude@marcel-aust.de`. When running a fork or a different deployment, use whatever address is appropriate for that deployment (e.g. a shared `forgewright@yourdomain` address, or the committer's own email). Do **not** use `noreply@anthropic.com`.

Example:

```
Co-Authored-By: Claude Opus 4.6 (1M context) <claude@marcel-aust.de>
```

## Architecture

### Package structure

```
forgewright/                 # Python package
  __init__.py                # Version string
  __main__.py                # `python -m forgewright` entry
  config.py                  # Config dataclass, YAML loading, mention regex
  state.py                   # JSON state persistence (fingerprints per issue/MR)
  helpers.py                 # slugify, has_mention, shortdt, run (subprocess), file_lock
  git.py                     # clone_or_update_mirror, make_worktree, cleanup_worktree, push_branch
  prompts.py                 # ISSUE_PROMPT, MR_UPDATE_PROMPT, MR_REVIEW_PROMPT templates
  formatting.py              # format_notes, format_discussions, format_diff_for_review
  parsing.py                 # parse_summary_replies, parse_review_comments, read_summary
  posting.py                 # post_mr_responses, post_review_comments
  decision.py                # is_review_mode, fingerprint_*, should_process_*, select_projects
  workspace.py               # .forgewright/ scratch dir paths + git exclude
  context_repos.py           # extra read-only repos per project (context_repos)
  context_requests.py        # agent asks for a repo, human allows/denies by comment
  handlers.py                # handle_issue, handle_mr, handle_mr_review, process_project
  webhook.py                 # Flask webhook server for near-real-time GitLab/GitHub events
  main.py                    # setup_logging, main(), argparse, --serve mode
  platform/
    __init__.py              # create_platform() factory
    base.py                  # Platform ABC (abstract interface for code forges)
    gitlab.py                # GitLabPlatform — GitLab v4 API implementation
    github.py                # GitHubPlatform — GitHub REST API v3 implementation
  agent/
    __init__.py              # create_agent() factory
    base.py                  # Agent ABC + AgentResult dataclass
    claude_code.py           # ClaudeCodeAgent — wraps `claude` CLI
    hermes.py                # HermesAgent — wraps `hermes` CLI (Hermes Agent, BYOK)
    opencode.py              # OpenCodeAgent — wraps `opencode` CLI (deprecated)
forgewright.py               # Thin entry-point shim (delegates to package)
```

### Key abstractions

**Platform** (`forgewright/platform/base.py`): Abstract base class defining 22 methods for interacting with a code hosting platform (list projects, issues, MRs, post comments, create discussions, etc.). Implementations: `GitLabPlatform` (GitLab v4 API) and `GitHubPlatform` (GitHub REST API v3). To add a new platform: implement the ABC in `platform/<name>.py` and add a case in `platform/__init__.py:create_platform()`.

**Agent** (`forgewright/agent/base.py`): Abstract base class with a single `run(prompt, cwd) -> AgentResult` method and a `name` property. `AgentResult` is a dataclass with `ok: bool`, `output: str`, `summary: str`. Implementations: `ClaudeCodeAgent` (runs `claude -p --dangerously-skip-permissions`, prompt on stdin), `HermesAgent` (runs `hermes [-p <profile>] chat --oneshot --yolo --format stream-json --query-file <tmp>`; with `hermes_base_url` set it maintains the managed profile `~/.hermes/profiles/<profile>/` — config.yaml + 0600 `.env` with the key, memory/self-learning off unless `hermes_learning: true`, which switches to one profile per project via `Agent.set_scope()` called from `process_project`) and the deprecated `OpenCodeAgent` (runs `opencode run --dir <wt> <prompt>`). Never pass a prompt as one argv string above 128 KiB (`MAX_ARG_BYTES`, Linux E2BIG); strip `FORGE_SECRET_ENV_VARS` from the agent env; start it with `start_new_session=True`. To add a new agent: implement the ABC and add a case in `agent/__init__.py:create_agent()`.

**Config** (`forgewright/config.py`): Dataclass loaded from YAML. Key fields: `platform_type` ("gitlab" or "github"), `agent_type` ("claude", "hermes" or "opencode"), plus all platform/agent/git settings (Hermes: `hermes_base_url`, `hermes_model`, `hermes_api_key`, `hermes_reasoning_effort`, `hermes_context_length`, `hermes_max_turns`, `hermes_profile`, `hermes_learning`, validated in `_hermes_fields`). Webhook fields: `webhook_enabled`, `webhook_host`, `webhook_port`, `webhook_secret`. Secrets fall back to env vars (`PLATFORM_TOKEN` / `GITLAB_TOKEN`, `ANTHROPIC_API_KEY`, `HERMES_API_KEY`, `WEBHOOK_SECRET`).

**Webhook** (`forgewright/webhook.py`): Flask HTTP server that receives GitLab/GitHub webhook events (Issue Hook, Note Hook, Merge Request Hook, Pipeline Hook — or their GitHub equivalents). Validates the platform-specific signature, dispatches `process_project()` in a background thread. Uses the same file locking and state management as the poller, so both can coexist safely.

### Data flow

```
# Polling path (timer-based, every 5 min):
main() → create_platform() + create_agent()
  → select_projects()
  → for each project: process_project()
    → list issues/MRs via Platform
    → should_process_issue/mr() checks fingerprints
    → handle_issue() / handle_mr() / handle_mr_review()
      → clone_or_update_mirror() + make_worktree()
      → agent.run(prompt, worktree) → AgentResult
      → push_branch() if commits exist
      → platform.create_mr() or platform.comment_issue() etc.
      → post_mr_responses() / post_review_comments() for threaded replies
      → cleanup_worktree()
      → save state fingerprint

# Webhook path (event-driven, near-real-time):
main(--serve) → create_app(cfg) → Flask server
  → POST /webhook → validate signature → extract project_id
  → background thread: _process_event()
    → process_project() (same as polling path above)
```

### Three operating modes

1. **Issue mode** (`handle_issue`): triggered by `@<bot_username>` in an issue. Agent receives `ISSUE_PROMPT` with issue context. Can result in: code changes + Draft MR, Q&A answer as comment, or clarification questions.

2. **MR update mode** (`handle_mr`): triggered by new activity on a bot-authored MR. Agent receives `MR_UPDATE_PROMPT` with new discussions and pipeline status. Agent writes structured `## Reply to discussion <id>` sections that get posted as threaded replies.

3. **MR review mode** (`handle_mr_review`): triggered by `@<bot_username>` on a non-bot MR. Agent receives `MR_REVIEW_PROMPT` with the full diff. Agent writes structured `## Inline: <file>:<line>` sections that get posted as inline code review discussions.

### Decision logic

`decision.py` controls when the bot acts:
- `should_process_issue()`: first run on a `@<bot_username>` mention, afterwards only on a NEW mention comment (plain comments, labels, description edits don't re-run)
- `should_process_mr()`: bot-owned MR → new human comment or newly failed pipeline (capped by `max_ci_fix_attempts`, counter `ci_fix_attempts` in state, reset by a human comment); review-mode MR → new mention, newly requested as reviewer/assignee, mention newly in description, or human reply in a bot thread
- `is_review_mode()`: True for fork MRs, or when MR author is not the bot AND branch doesn't start with `branch_prefix`
- Fingerprints include: `description_hash`, `last_note_id`/`last_note_at` (human notes only), `labels`, `pipeline_id`/`pipeline_sha`/`pipeline_status`, `head_sha`, `bot_requested`, `desc_mention`
- Handlers save the fingerprint as soon as the agent has run (in a `finally`), so a crash while pushing/posting never re-runs the agent

### Agent scratch dir and context repos

Everything exchanged with the agent lives in `<worktree>/.forgewright/` (`workspace.py`: `last-run-summary.md`, `agent-live.log`, `mr.diff`, `ci/<job>.log`, `context/<name>/`) — never `.claude/`, which is Claude Code's real, often committed config dir. `make_worktree` adds `/.forgewright/` to the mirror's `info/exclude`, so `git add -A` in any worktree can't commit it. Review prompts carry a file overview + diff excerpt (40k chars) and point at `.forgewright/mr.diff` / `git diff <base>...<head>`; CI failures get log tails inline, full logs as files.

`context_repos` (`context_repos.py`, config: `{project_path or "*": [path | {path, ref}]}`) checks extra repos out read-only under `.forgewright/context/<name>/` via their own mirrors and the bot's token; `attach_context_repos` never raises (unavailable repos are listed as such), `detach_context_repos` runs before `cleanup_worktree`. Prompts get them through the `{context_block}` placeholder.

`context_requests` (default on, `context_requests.py`): the agent may write `## Request repo: <path>` in its summary; handlers strip it (`_split_repo_requests`) and `request_access` asks in the issue/MR (once; unreadable-by-token repos are reported once and recorded). `process_project` runs `apply_access_commands` on the authz-filtered notes before `should_process_*`: `@<bot> allow|deny|revoke repo <path>`, each note once (`seen_notes`, key `type:id`); `allow` needs `Platform.CODE_READ_LEVEL` on the requested repo (GitLab 20/reporter, GitHub 10/read) and fails closed; the bot's own notes are never commands (its question contains the command text!). State: `context_access.{granted,denied,unavailable,pending}` per project; grants feed `attach_context_repos(extra=...)`.

### Summary parsing

The agent writes to `.forgewright/last-run-summary.md`. The bot parses this structured output:
- `## Reply to discussion <id>` blocks → threaded replies on MR discussions
- `## Inline: <file>:<line>` blocks → inline code review comments
- `## General` block → top-level MR comment
- Unstructured text → treated as general comment

Parsing logic lives in `parsing.py`. Posting logic in `posting.py`.

## Development

### Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

### Running tests

```bash
.venv/bin/pytest              # full suite
.venv/bin/pytest tests/ -v    # verbose
.venv/bin/pytest tests/test_decision.py  # single module
```

Tests use `pytest` with fixtures defined in `tests/conftest.py`:
- `tmp_config`: Config with all paths in `tmp_path`
- `mock_platform`: `MockPlatform` (concrete Platform subclass recording calls)
- `mock_agent`: `MockAgent` (returns fixed AgentResult without subprocess)
- Helper factories: `make_note()`, `make_issue()`, `make_mr()`

Handler tests mock git operations (`clone_or_update_mirror`, `make_worktree`, `push_branch`, `cleanup_worktree`, `run`) to avoid real git/subprocess calls.

`tests/test_regressions.py` holds cross-cutting guards: rendered prompts (no unfilled placeholders, no `origin/` refs), argv limit + secret hygiene + process group for **every** agent, multi-poll lifecycles through `process_project`, and drift checks between code, `config.example.yaml`, Dockerfiles, compose, CI matrix and installer. When adding an agent, add it to `AGENTS` there. Hermes tests must keep `HERMES_HOME` pointed at `tmp_path` (autouse fixtures do this) so they never touch a real `~/.hermes`.

### Entry points

- `python -m forgewright` — package entry point (polling mode)
- `python -m forgewright --serve` — webhook server mode (long-running)
- `python forgewright.py` — thin shim that delegates to the package (equivalent)
- `forgewright` — installed script (via pyproject.toml `[project.scripts]`)

### Dependencies

Runtime: `requests>=2.31`, `PyYAML>=6.0`, `flask>=3.0` (Python 3.10+)
Dev: `pytest>=7.0`

## Deployment

Two deployment modes:

**Native (systemd):** `install.sh` creates a `forgewright` system user, copies the package to `/opt/forgewright/`, creates a venv, installs systemd timer + webhook service. Config at `/etc/forgewright/config.yaml`, state at `/var/lib/forgewright/`, logs at `/var/log/forgewright/`.

**Docker:** `install.docker.sh --profile claude|hermes|opencode` copies `docker-compose.yml` to `/opt/forgewright`, pulls the prebuilt agent-specific image from GHCR (`ghcr.io/marcel2508/forgewright-claude:latest`, `…-hermes:latest` or `…-opencode:latest`), and brings up two long-running containers per profile — `poller-<profile>` (runs [supercronic](https://github.com/aptible/supercronic) in-container, firing `python -m forgewright` on `POLL_SCHEDULE`) and `webhook-<profile>` (Flask server). No host systemd or cron is involved. Config bind-mounted from `/opt/forgewright/config.yaml`; secrets and host-path overrides come from `/opt/forgewright/.env`; state in named volumes. The `Dockerfile.claude` / `Dockerfile.hermes` / `Dockerfile.opencode` in the repo are what the GHCR workflow (`.github/workflows/docker.yml`) builds for `linux/amd64` + `linux/arm64` on every push to `main` and on semver tags.

Native polling uses a 5-minute systemd timer (one-shot). Docker polling uses supercronic inside the `poller-<profile>` container with the same 5-minute default (tune via `POLL_SCHEDULE` in `.env`). The webhook is always-on in both deployments. All instances share file locks and state to avoid conflicts.

### Key paths in production

- Config: `/etc/forgewright/config.yaml`
- State: `/var/lib/forgewright/state.json`
- Locks: `/var/lib/forgewright/locks/`
- Git mirrors: `/var/lib/forgewright/work/mirrors/`
- Worktrees: `/var/lib/forgewright/work/worktrees/`
- Log: `/var/log/forgewright/bot.log`
- Live agent output: `<worktree>/.forgewright/agent-live.log`

## Common patterns

### Adding a new Platform method

1. Add abstract method to `platform/base.py`
2. Implement in `platform/gitlab.py` and `platform/github.py`
3. Call from handlers or posting modules

### Adding a new Agent

1. Create `agent/myagent.py` with a class implementing `Agent`
2. Add factory case in `agent/__init__.py:create_agent()`
3. Add any new config fields to `Config` in `config.py`
4. Add config defaults to `Config.load()` and `config.example.yaml` (a drift test checks every key is documented)
5. Add `Dockerfile.<agent>`, the `poller-/webhook-<agent>` compose services, the CI matrix entry and the `install.docker.sh` profile, then add the agent to `AGENTS` in `tests/test_regressions.py`

### Modifying prompt templates

All prompts live in `prompts.py`. They use `.format()` with named placeholders. The handler functions in `handlers.py` supply the format kwargs. If adding a new placeholder, update both the template and the corresponding handler.

### Modifying trigger conditions

Edit `decision.py`. The `should_process_issue()` and `should_process_mr()` functions return `(bool, reason_string)`. The `fingerprint_*` functions determine what constitutes a "change".

## Safety constraints

- Issue work is pushed only to `<branch_prefix>…` branches (default `forgewright/`). Review mode may push to a non-fork MR's source branch when the user asks for a change; never for fork MRs (detached checkout of the MR head ref) and never onto the target/default branch (`push_branch` refuses `branch == base_branch`)
- MRs/PRs are always Draft
- Per-branch lockfile prevents concurrent agent runs on the same worktree
- Global poller lock prevents overlapping poll cycles
- Agent timeout defaults to 1 hour; the agent runs in its own process group, which is killed on timeout and cleaned up after exit
- `authorization_min_role` defaults to `write` when missing from the config
- Git tokens use `GIT_ASKPASS` (never in git config or error messages)
- Bot's own notes are excluded from fingerprints to prevent self-triggering loops
