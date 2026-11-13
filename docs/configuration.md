# Configuration

forgewright reads its configuration from a YAML file. The default location is
`/etc/forgewright/config.yaml`; override with `--config PATH` or the
`FORGEWRIGHT_CONFIG` environment variable.

Start by copying the template:

```bash
sudo install -m 0640 config.example.yaml /etc/forgewright/config.yaml
sudo chown root:forgewright /etc/forgewright/config.yaml
```

## Secrets

Three secrets are recognised. In every case an **environment variable takes
precedence over the config file**, so production deployments can keep secrets
out of disk files:

| Secret | Env var (preferred) | Config key | Purpose |
|---|---|---|---|
| Platform API token | `PLATFORM_TOKEN` (or legacy `GITLAB_TOKEN`) | `platform_token` | GitLab/GitHub API + git push |
| Webhook shared secret | `WEBHOOK_SECRET` | `webhook_secret` | Validates incoming webhook requests |
| Agent provider key | `ANTHROPIC_API_KEY` (Claude Code) or OpenCode's provider key | (not in config) | Only if the agent CLI is not already logged in |
| Hermes endpoint key | `HERMES_API_KEY` | `hermes_api_key` | Key for `hermes_base_url` (BYOK). Written only to the managed profile's `.env` (0600) |

For native installs the recommended pattern is a systemd drop-in:

```ini
# /etc/systemd/system/forgewright.service.d/secrets.conf
[Service]
Environment=PLATFORM_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx
Environment=WEBHOOK_SECRET=some-long-random-string
Environment=ANTHROPIC_API_KEY=sk-ant-...
```

```bash
sudo systemctl edit forgewright.service            # opens the drop-in editor
sudo systemctl edit forgewright-webhook.service    # same for the webhook
```

For Docker, set them in the `environment:` block of `docker-compose.yml`.

## Full reference

Every key with its default and a short description. See
`config.example.yaml` for a copy-pasteable template.

### Platform

| Key | Default | Description |
|---|---|---|
| `platform_type` | `"gitlab"` | `"gitlab"` or `"github"`. |
| `platform_url` | — (required) | GitLab base URL, or `https://api.github.com` for GitHub. `gitlab_url` is still accepted as a fallback. |
| `platform_token` | — (env fallback) | API token; env var `PLATFORM_TOKEN` / `GITLAB_TOKEN` wins if set. |
| `bot_username` | `"forgewright"` | Username of the bot account. **Also defines the mention trigger**: the bot reacts to `@<bot_username>` in issues and MRs (so `bot_username: jarvis` ⇒ tag with `@jarvis`). Used to detect the bot's own comments and filter them out of fingerprints. At startup the token owner's username is compared against this value; if they differ, the token owner wins and a warning is logged. |
| `authorization_min_role` | `"write"` | Minimum role on the repo needed to trigger the bot (`read`/`guest`, `triage`/`reporter`, `write`/`developer`, `maintain`/`maintainer`, `admin`/`owner`). Comments from users below it are also hidden from the agent. `"none"` disables the check. |
| `request_timeout_sec` | `30` | Timeout per platform API request. |
| `http_retries` | `3` | Retries on 5xx and timeouts. |

### Agent

| Key | Default | Description |
|---|---|---|
| `agent_type` | `"claude"` | `"claude"` (Claude Code), `"hermes"` (Hermes Agent) or `"opencode"` (OpenCode, **deprecated**). |
| `claude_binary` | `"claude"` | Path to the `claude` CLI (only used when `agent_type: "claude"`). |
| `claude_model` | `null` | Optional model override, e.g. `"claude-opus-4-6"`, `"claude-sonnet-4-6"`, `"claude-haiku-4-5-20251001"`. `null` uses the CLI default. |
| `claude_timeout_sec` | `3600` | Hard cap per agent run (applies to every agent). |
| `max_ci_fix_attempts` | `3` | Consecutive automatic fix runs for a failing pipeline on a bot MR before the bot waits for a human comment. |
| `hermes_binary` | `"hermes"` | Path to the `hermes` CLI (only used when `agent_type: "hermes"`). |
| `hermes_base_url` | `null` | OpenAI-compatible endpoint (e.g. `https://llm.example.com/v1`). Setting it switches to **managed BYOK mode** (see below). Env fallback `HERMES_BASE_URL`. |
| `hermes_model` | `null` | Model id at that endpoint. Required with `hermes_base_url`; otherwise an optional override. |
| `hermes_api_key` | `null` | Endpoint key; prefer the `HERMES_API_KEY` env var. Leave empty for local servers without auth. |
| `hermes_reasoning_effort` | `null` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max`. `null` = Hermes default. Passed through to the endpoint as `reasoning_effort`. |
| `hermes_context_length` | `null` | Total context window (input + output) in tokens. `null` = auto-detect; set it when detection is wrong (common with local servers). Hermes needs ≥ 64000. |
| `hermes_max_turns` | `null` | Max tool-calling iterations per run. `null` = Hermes default. |
| `hermes_profile` | `null` | Managed mode: name of the profile forgewright owns (default `forgewright`). Unmanaged: an existing profile of yours to use as-is. |
| `hermes_learning` | `false` | Managed mode only. `true` lets Hermes keep persistent memory, write skills and self-review after runs — in a separate profile **per project** (`<profile>-<project>-<hash>`). See the security note below. |
| `opencode_binary` | `"opencode"` | Path to the `opencode` CLI (only used when `agent_type: "opencode"`). **Deprecated** — prefer Hermes. |
| `opencode_model` | `null` | Optional OpenCode model override. |

#### Hermes: managed (BYOK) vs. unmanaged

- **Managed** (`hermes_base_url` set): before every run forgewright writes
  `~/.hermes/profiles/<hermes_profile>/config.yaml` (provider `custom`,
  endpoint, model, `context_length`, `agent.reasoning_effort`,
  `agent.max_turns`) and a `.env` with the key (mode 0600 — Hermes profiles
  read keys only from their own `.env`). It runs `hermes -p <profile> chat
  --oneshot --yolo --format stream-json`. By default persistent memory, user
  profile and background self-improvement are off in that profile, so one
  issue's text cannot plant instructions for later runs. Don't point
  `hermes_profile` at a profile you use yourself — it gets overwritten.
- **Learning** (`hermes_learning: true`, managed mode only): memory, skill
  creation and the post-run self-review are on, and each watched project runs
  in its own profile `<hermes_profile>-<project-slug>-<hash>` — what Hermes
  learns about one codebase never reaches another. The "user profile" stays
  off (the bot serves many people). **Trade-off:** everything that reaches the
  prompt — issue and MR text, diffs, CI logs — can now leave traces that steer
  later runs of that project. Keep `authorization_min_role` at `write` or
  stricter, and reset a project by deleting its profile directory
  (`~/.hermes/profiles/forgewright-<project>-<hash>`). In Docker the profiles
  live in the `hermes-profiles` volume, so they survive image updates.
- **Unmanaged** (no `hermes_base_url`): your existing Hermes setup is used
  unchanged (default profile, or `hermes_profile`). Configure model and keys
  with `hermes model` / `hermes setup` as the bot user.

### Paths

| Key | Default | Description |
|---|---|---|
| `workdir` | — (required) | Where git mirrors and worktrees live. |
| `state_file` | — (required) | JSON file with per-issue/MR fingerprints. |
| `lock_dir` | — (required) | Directory for file locks (poller, per-branch). |
| `log_file` | — (required) | Log file path (journald also captures stdout). |

### Git

| Key | Default | Description |
|---|---|---|
| `branch_prefix` | `"forgewright/"` | Namespace for bot-created branches. Also used by `is_review_mode()`. |
| `default_base_branch` | `null` | Override the project's default branch as the base. `null` = use whatever the platform reports. |
| `git_user_name` | `"Forgewright"` | `user.name` set on the worktree before the agent runs. |
| `git_user_email` | `"forgewright@example.com"` | `user.email` set on the worktree; also used in the `Co-Authored-By` trailer embedded in prompts. |

### Project scoping

| Key | Default | Description |
|---|---|---|
| `projects_include` | `[]` | Allowlist of `path_with_namespace` (e.g. `["team/repo", "team/sub/proj"]`). Empty = every project the bot can see. |
| `projects_exclude` | `[]` | Denylist, applied after `projects_include`. |

### Webhook server

| Key | Default | Description |
|---|---|---|
| `webhook_enabled` | `false` | Must be `true` to use `--serve`. |
| `webhook_host` | `"127.0.0.1"` | Bind address. Bind to `0.0.0.0` only if fronted by TLS. |
| `webhook_port` | `5000` | Bind port. |
| `webhook_secret` | `""` (env fallback) | Shared secret the platform includes with each webhook. Falls back to `WEBHOOK_SECRET`. |
| `webhook_debounce_sec` | `60` | When multiple events for the same project arrive within this window, only the last one triggers processing. Set to `0` to disable. |

## Choosing values

### `projects_include` vs `projects_exclude`

- Leave both empty in small deployments — the bot will watch every project its
  token can read.
- For larger instances, prefer `projects_include` for explicit scoping. It
  matches against `path_with_namespace`.
- Use `projects_exclude` to carve out specific repos inside a group you
  otherwise want watched.

### `claude_model`

- Leave `null` to use whatever the `claude` CLI defaults to.
- `claude-haiku-*` is cheaper and fine for small, well-scoped issues.
- `claude-opus-*` handles larger refactors and ambiguous tasks better.

### `claude_timeout_sec`

- Default is 1 hour. Long enough for almost any single-issue task.
- Applies to whichever agent is active.
- Shorten to keep costs bounded on noisy platforms; lengthen for large
  refactors.

### `webhook_debounce_sec`

- Defaults to 60s. When someone edits an issue, then quickly adds a comment,
  then changes a label, the bot will run once for the final state instead of
  three times back to back.
- `0` disables debouncing (immediate processing on every event).
- Tune up to 120–300 in noisy repos; tune down if you need snappier response.

## Validating your config

Run a dry scan to verify the token, URL, and project selection work:

```bash
sudo -u forgewright FORGEWRIGHT_CONFIG=/etc/forgewright/config.yaml \
  /opt/forgewright/venv/bin/python -m forgewright --dry-run -v
```

This exercises the auth path and lists which projects would be scanned without
processing any issues or MRs.
