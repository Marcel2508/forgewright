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
| `request_timeout_sec` | `30` | Timeout per platform API request. |
| `http_retries` | `3` | Retries on 5xx and timeouts. |

### Agent

| Key | Default | Description |
|---|---|---|
| `agent_type` | `"claude"` | `"claude"` (Claude Code) or `"opencode"` (OpenCode). |
| `claude_binary` | `"claude"` | Path to the `claude` CLI (only used when `agent_type: "claude"`). |
| `claude_model` | `null` | Optional model override, e.g. `"claude-opus-4-6"`, `"claude-sonnet-4-6"`, `"claude-haiku-4-5-20251001"`. `null` uses the CLI default. |
| `claude_timeout_sec` | `3600` | Hard cap per agent run (applies to both Claude Code and OpenCode). |
| `opencode_binary` | `"opencode"` | Path to the `opencode` CLI (only used when `agent_type: "opencode"`). |
| `opencode_model` | `null` | Optional OpenCode model override. |

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
- Applies to whichever agent is active (Claude Code or OpenCode).
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
