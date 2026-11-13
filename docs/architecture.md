# Architecture

forgewright is a small Python service that connects three moving parts: a
**code hosting platform** (GitLab or GitHub), an **AI coding agent** (Claude
Code or OpenCode), and the **git repository** being worked on. It reacts to
`@<bot_username>` mentions on issues and merge/pull requests and dispatches
the agent to produce code, answer questions, or review diffs. The mention
string is configurable — `bot_username: forgewright` triggers on
`@forgewright`, `bot_username: jarvis` triggers on `@jarvis`.

## High-level flow

```
┌─────────────┐   webhook    ┌──────────────┐
│  GitLab /   │──────────────▶│ forgewright  │
│  GitHub     │◀───posts─────│  (Flask +    │
└──────┬──────┘              │   timer)     │
       │  polling every 5m   └───────┬──────┘
       ▼                             │
┌─────────────┐                     ▼
│ Mentioned   │              ┌──────────────────┐
│ @bot on     │              │  git worktree    │
│ issue or MR │              │  on forgewright/ │
└─────────────┘              │  branch          │
                             └──────┬───────────┘
                                    ▼
                             ┌─────────────┐
                             │  AI agent   │
                             │ (Claude /   │
                             │  OpenCode)  │
                             └─────────────┘
```

Two dispatch paths exist and share all state safely:

- **Polling** — a systemd timer fires every 5 minutes, asks the platform for
  recently-updated issues and MRs, and processes anything whose fingerprint
  changed.
- **Webhook** — an optional long-running Flask server receives platform events
  in near-real-time and dispatches processing immediately. Debounces bursts of
  events on the same project.

Both run the same `process_project()` pipeline, guarded by a shared file lock
and state file so they cannot collide.

## Package structure

```
forgewright/
  __init__.py                # Version string
  __main__.py                # `python -m forgewright` entry point
  config.py                  # Config dataclass, YAML loading, mention regex
  state.py                   # JSON state persistence (fingerprints per issue/MR)
  helpers.py                 # slugify, has_mention, run (subprocess), file_lock
  git.py                     # clone_or_update_mirror, make_worktree, push_branch
  prompts.py                 # ISSUE / MR_UPDATE / MR_REVIEW prompt templates
  formatting.py              # Format notes, discussions, diffs for prompts
  parsing.py                 # Parse structured AI summary output
  posting.py                 # Post replies, inline comments, top-level notes
  decision.py                # Trigger logic, fingerprinting, mode detection
  handlers.py                # handle_issue, handle_mr, handle_mr_review
  webhook.py                 # Flask webhook server
  main.py                    # CLI entry point (poll mode + --serve for webhooks)
  types.py                   # Dataclasses (Project, Issue, MergeRequest, Note…)
  platform/
    __init__.py              # create_platform() factory
    base.py                  # Platform ABC (22 methods)
    gitlab.py                # GitLab v4 API implementation
    github.py                # GitHub REST API v3 implementation
  agent/
    __init__.py              # create_agent() factory
    base.py                  # Agent ABC + AgentResult dataclass
    claude_code.py           # Wraps the `claude` CLI
    opencode.py              # Wraps the `opencode` CLI
forgewright.py               # Thin entry-point shim (delegates to package)
```

## Key abstractions

### `Platform` (ABC) — `forgewright/platform/base.py`

An abstract interface for a code hosting platform. Defines 22 methods:
listing projects, fetching issues and MRs, posting comments, creating
discussions, reading pipeline status, etc.

Implementations:
- `GitLabPlatform` — GitLab v4 API
- `GitHubPlatform` — GitHub REST API v3

To add a new platform (e.g. Forgejo, Gitea): implement the ABC in
`forgewright/platform/<name>.py` and register it in
`forgewright/platform/__init__.py:create_platform()`.

### `Agent` (ABC) — `forgewright/agent/base.py`

Single method: `run(prompt, cwd) -> AgentResult`. `AgentResult` is
`(ok: bool, output: str, summary: str)`.

Implementations:
- `ClaudeCodeAgent` — invokes `claude -p ... --dangerously-skip-permissions`
- `OpenCodeAgent` — invokes `opencode --non-interactive --prompt ...`

To add a new agent, implement the ABC in `forgewright/agent/<name>.py` and
register it in `forgewright/agent/__init__.py:create_agent()`.

### `Config` — `forgewright/config.py`

A dataclass loaded from YAML. Holds platform selection, agent selection, paths
(workdir, state, logs), git identity, trigger knobs (`branch_prefix`,
`projects_include`/`exclude`), request timeouts, and webhook settings. Secrets
fall back to environment variables (`PLATFORM_TOKEN`, `GITLAB_TOKEN`,
`WEBHOOK_SECRET`).

## Three operating modes

`handlers.py` implements three orchestration entry points, chosen by
`decision.py:is_review_mode()` and the trigger context:

1. **Issue mode** (`handle_issue`) — `@<bot_username>` on an issue. Agent
   receives the `ISSUE_PROMPT` (issue body + notes). Output can be a Draft MR,
   a plain answer comment, or a clarification question.

2. **MR update mode** (`handle_mr`) — new activity on a bot-authored MR
   (branch starts with `branch_prefix`). Agent receives the `MR_UPDATE_PROMPT`
   with new discussions and pipeline status. Structured `## Reply to discussion <id>`
   sections become threaded replies on the right discussions.

3. **MR review mode** (`handle_mr_review`) — `@<bot_username>` on an MR not
   authored by the bot. Agent receives the `MR_REVIEW_PROMPT` with the full
   diff. Structured `## Inline: <file>:<line>` sections become inline
   code-review comments.

## Data flow

### Polling path (systemd timer, every 5 min)

```
main() → Config.load() → create_platform() + create_agent()
  → select_projects()
  → for each project:
    process_project()
      → list issues / MRs via Platform
      → decision.should_process_issue / should_process_mr (fingerprint check)
      → handle_issue / handle_mr / handle_mr_review
        → git.clone_or_update_mirror() + git.make_worktree()
        → agent.run(prompt, worktree) → AgentResult
        → git.push_branch() if commits exist
        → Platform.create_mr() or Platform.comment_issue() etc.
        → posting.post_mr_responses / posting.post_review_comments
        → git.cleanup_worktree()
      → state.save(fingerprint)
```

### Webhook path (Flask server, event-driven)

```
main(--serve) → webhook.create_app(cfg) → Flask server listening on :5000
  → POST /webhook
    → validate platform-specific signature / token
    → extract project id
    → debounce for webhook_debounce_sec (default 60s)
    → background thread: process_project(...)  # same pipeline as polling
```

## Decision logic

Centralised in `forgewright/decision.py`. All return `(bool, reason)` so the
log line explains *why* something did or didn't trigger.

- `should_process_issue()` — first run on a `@<bot_username>` mention; after
  that only on a **new** mention comment (newer than the newest human note
  recorded at the last run). Plain comments, labels and description edits
  do not re-run the agent.
- `should_process_mr()` — bot-owned MRs (`<branch_prefix>…`, not a fork):
  new human comment, or a newly failed pipeline (`ci_fix_trigger`, capped by
  `max_ci_fix_attempts`; the counter lives next to the fingerprint as
  `ci_fix_attempts` and resets on a human comment). Review-mode MRs: new
  mention, bot newly requested as reviewer/assignee, mention newly added to
  the description, or a human reply in a thread the bot takes part in.
- `is_review_mode()` — true for fork MRs, and when the MR author is not the
  bot and the branch does not start with `branch_prefix`.

### Fingerprinting

Issue fingerprint: `description_hash, last_note_id, last_note_at, labels`.
MR fingerprint additionally has `pipeline_id, pipeline_sha, pipeline_status,
head_sha, bot_requested, desc_mention`. Only **human notes** count toward
`last_note_*` — the bot's own replies are excluded to prevent self-triggering
loops. The fingerprint is stored as soon as the agent has run, even if
pushing or posting fails afterwards, so a crash cannot cause repeated agent
runs.

## The agent's scratch dir: `.forgewright/`

Everything exchanged with the agent lives in `.forgewright/` at the worktree
root (`forgewright/workspace.py`) — deliberately not `.claude/`, which is
Claude Code's real per-repo config dir and often committed:

| Path | Direction | Content |
|---|---|---|
| `.forgewright/last-run-summary.md` | agent → bot | the structured result (see below) |
| `.forgewright/agent-live.log` | bot | live agent output (`tail -f`) |
| `.forgewright/mr.diff` | bot → agent | complete MR diff (review mode), never truncated |
| `.forgewright/ci/<job>.log` | bot → agent | complete logs of failed CI jobs |

`/.forgewright/` is added to the mirror's `info/exclude`, which covers every
worktree of that mirror — an agent running `git add -A` can't commit or push
any of it. The review prompt keeps a file overview plus the diff inline up to
40 000 chars and points at `mr.diff` and `git diff <base>...<head>` for the
rest; CI failures get the log tail inline and the full log as a file.

## Summary parsing

The AI agent writes its output to `.forgewright/last-run-summary.md` inside the
worktree. The bot parses this structured file:

- `## Reply to discussion <id>` → threaded reply on that MR discussion
- `## Inline: <file>:<line>` → inline review comment on that file/line
- `## General` → top-level MR or issue comment
- Everything else → treated as a general comment

Parsing lives in `parsing.py`; posting lives in `posting.py`.

## Safety constraints

- Issue work is pushed only to `branch_prefix` branches (default
  `forgewright/`). Review mode may push to the MR's source branch when a
  user asks for a change, but never for fork MRs (detached read-only
  checkout of `refs/merge-requests/<iid>/head` / `refs/pull/<n>/head`) and
  never onto the target/default branch.
- All bot-created MRs/PRs are Draft.
- A per-branch lockfile prevents two concurrent agent runs on the same
  worktree.
- A global poller lockfile prevents overlapping poll cycles.
- Agent runs are capped at `claude_timeout_sec` (default 1 hour).
- Tokens are passed via `GIT_ASKPASS`, so they never appear in git config or
  error messages.
- The bot's own notes are excluded from fingerprints to avoid self-triggering.

## Where to look next

- **[Installation](installation.md)** — how to deploy it.
- **[Configuration](configuration.md)** — every config key explained.
- **[Contributing](contributing.md)** — dev setup and how to add platforms,
  agents, or change prompts.
