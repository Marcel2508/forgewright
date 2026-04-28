# Installation

forgewright runs on Linux and is designed for either a systemd-managed VM or a
Docker host. Both layouts use the same code and config file. The Docker
deployment is fully self-contained — no host systemd or cron is required.

There are three things to decide before installing:

1. **Install method** — native (systemd + venv) or Docker.
2. **Platform** — GitLab or GitHub.
3. **Agent** — Claude Code or OpenCode.

## Prerequisites

- Linux host with systemd (Debian/Ubuntu tested).
- Python 3.10 or newer (only for native installs; Docker uses its own).
- A bot account on your platform with write access to the repos you want it
  to watch.
- The AI agent CLI installed for the bot user (see
  [Agent CLI setup](#agent-cli-setup)).

## Native install (systemd)

```bash
git clone https://github.com/Marcel2508/forgewright.git
cd forgewright
sudo bash install.sh
```

`install.sh` creates a `forgewright` system user, installs the package into
`/opt/forgewright/venv`, writes the systemd units, and starts the 5-minute
timer. It does not start the webhook service (opt-in).

Paths used in production:

| Path | Purpose |
|---|---|
| `/etc/forgewright/config.yaml` | Main config file (0640, owned by root:forgewright). |
| `/var/lib/forgewright/state.json` | Persistent fingerprint state. |
| `/var/lib/forgewright/locks/` | File locks (poller, per-branch). |
| `/var/lib/forgewright/work/mirrors/` | Bare git mirrors. |
| `/var/lib/forgewright/work/worktrees/` | Scratch worktrees per run. |
| `/var/log/forgewright/bot.log` | Log file (also goes to journald). |

After running the installer:

1. **Configure** — edit `/etc/forgewright/config.yaml` (see
   [Configuration](configuration.md)).
2. **Set the token** — either put it in the config file or use a systemd
   drop-in so it never hits disk unencrypted:
   ```bash
   sudo systemctl edit forgewright.service
   # [Service]
   # Environment=PLATFORM_TOKEN=glpat-xxxx   (or ghp_xxxx / github_pat_xxxx)
   # Environment=ANTHROPIC_API_KEY=sk-ant-…  (only if not using `claude login`)
   ```
3. **Install the agent CLI** — see [Agent CLI setup](#agent-cli-setup) below.
4. **Smoke-test** a dry run:
   ```bash
   sudo -u forgewright FORGEWRIGHT_CONFIG=/etc/forgewright/config.yaml \
     /opt/forgewright/venv/bin/python -m forgewright --dry-run -v
   ```
5. **Watch it run**:
   ```bash
   journalctl -u forgewright.service -f
   systemctl list-timers forgewright.timer
   ```

## Docker install

```bash
git clone https://github.com/Marcel2508/forgewright.git
cd forgewright
bash install.docker.sh --profile claude      # or --profile opencode
```

`install.docker.sh` copies `docker-compose.yml` into `/opt/forgewright`,
pulls the prebuilt agent-specific image from GHCR, and brings up the
relevant long-running containers via Docker Compose:

| Container | Role | Always started? |
|---|---|---|
| `forgewright-poller-<profile>` | Runs [supercronic](https://github.com/aptible/supercronic) in-container; fires `python -m forgewright` on `POLL_SCHEDULE` (default `*/5 * * * *`). | yes |
| `forgewright-webhook-<profile>` | Long-running Flask server receiving GitLab/GitHub events on `127.0.0.1:5000`. | only when `webhook_enabled: true` |

The installer reads `webhook_enabled` from `config.yaml` and activates the
right combination: the umbrella `--profile <agent>` (poller + webhook) when
the webhook is enabled, or `--profile <agent>-poll` (poller only) when it's
disabled.

Both containers share named volumes for state and logs, and restart on
failure via `restart: unless-stopped`. **No host systemd, cron, or timer is
involved** — if Docker is up, forgewright is up.

### Images

Two prebuilt images are published to GHCR for `linux/amd64` and
`linux/arm64`, one per agent:

- **`ghcr.io/marcel2508/forgewright-claude:latest`** — Python 3.12 slim +
  Node 22 + `@anthropic-ai/claude-code` + supercronic. Used by the `claude`
  compose profile.
- **`ghcr.io/marcel2508/forgewright-opencode:latest`** — Python 3.12 slim +
  opencode (via the official installer) + supercronic. Used by the
  `opencode` compose profile.

The corresponding `Dockerfile.claude` and `Dockerfile.opencode` in the repo
are what the [GitHub Actions workflow](https://github.com/Marcel2508/forgewright/blob/main/.github/workflows/docker.yml)
builds and publishes on every push to `main` and on semver tags.

Pick an agent at install time with `--profile claude` or `--profile opencode`.
Switching later just needs a pull of the other image:

```bash
cd /opt/forgewright
docker compose --profile opencode down
docker compose --profile claude pull
docker compose --profile claude up -d
```

### Agent auth

Secrets come from `/opt/forgewright/.env` (see `.env.example` for the full
list). The relevant keys per agent:

**Claude Code** — pick one of:

- **Pro / Max subscription (OAuth):** log in with `claude login` on the host,
  then set
  ```
  CLAUDE_HOME=/home/youruser
  ```
  in `.env`. The container bind-mounts both `$CLAUDE_HOME/.claude.json` (the
  OAuth session) and `$CLAUDE_HOME/.claude/` (projects, todos, settings) and
  inherits the login.
- **API key:** leave `CLAUDE_HOME` unset and set
  ```
  ANTHROPIC_API_KEY=sk-ant-…
  ```
  in `.env`. The container falls back to a seeded scratch dir
  (`docker/claude-state/`) that the installer creates.

**OpenCode** — set
```
OPENCODE_CONFIG_HOME=/home/youruser/.config/opencode
```
in `.env` to mount your host's OpenCode config (including provider keys and
custom model configs) into the container. Leave it unset to use a named
volume instead.

### Config

- `/opt/forgewright/config.yaml` — forgewright config (bind-mounted read-only
  into both containers).
- `/opt/forgewright/.env` — secrets and host-path overrides (read by Docker
  Compose, not by the bot).

After editing either file, recreate the containers:

```bash
cd /opt/forgewright
docker compose --profile claude up -d --force-recreate
```

## Updating

```bash
cd forgewright
git pull
sudo bash update.sh                           # native
bash update.docker.sh --profile claude        # Docker (or --profile opencode)
```

The native updater refreshes the package, dependencies, and systemd units.
The Docker updater refreshes `docker-compose.yml`, pulls the latest prebuilt
image from GHCR, and recreates the containers. Neither touches
`config.yaml`, `.env`, or any persistent volume.

## Agent CLI setup

Install the agent CLI you want to use as the `forgewright` user:

### Claude Code (default)

```bash
sudo -iu forgewright
npm install -g @anthropic-ai/claude-code
claude login                    # interactive Claude Max OAuth, or
# set ANTHROPIC_API_KEY in the systemd drop-in instead
which claude                    # verify and put the path in config.yaml
```

### OpenCode

```bash
sudo -iu forgewright
# install opencode per its own docs
which opencode                  # and set agent_type: "opencode" in config.yaml
```

## Platform setup

### GitLab

1. Create a bot user (e.g. `forgewright`) and invite it to every project you
   want it to watch as **Developer** or **Maintainer**.
2. Generate a Personal Access Token with scopes `api`, `read_repository`,
   `write_repository`.
3. Put the token in config.yaml (`platform_token`) or the systemd drop-in
   (`Environment=PLATFORM_TOKEN=glpat-…`).

### GitHub

1. Create a GitHub account for the bot (or use your own).
2. Generate a fine-grained Personal Access Token with these repo permissions:
   - **Contents** — Read and write (clone, push)
   - **Pull requests** — Read and write (create/update, review comments)
   - **Issues** — Read and write (read, comment)
3. Configure:
   ```yaml
   platform_type: "github"
   platform_url: "https://api.github.com"    # or your GHE API URL
   platform_token: "github_pat_…"            # or PLATFORM_TOKEN env var
   bot_username: "your-bot-username"
   ```
4. Invite the bot as a collaborator on every repo it should watch.

### GitLab vs GitHub cheatsheet

| Aspect | GitLab | GitHub |
|---|---|---|
| Project IDs | Numeric (`42`) | String (`owner/repo`) |
| Auth header | `PRIVATE-TOKEN` | `Bearer` token |
| Clone URL token | `oauth2@` | `x-access-token@` |
| Webhook signing | Shared secret token | HMAC-SHA256 |
| CI | GitLab CI pipelines | GitHub Actions / Check Runs |

## Enable the webhook server (optional)

The webhook server reduces the response time from ~5 minutes (polling) to
sub-second. The polling timer stays on as a safety net — both can run in
parallel thanks to file locking and fingerprinting.

### Services

| Deployment | Poller | Webhook |
|---|---|---|
| Native | `forgewright.timer` → `forgewright.service` (oneshot, every 5 min) | `forgewright-webhook.service` (simple, always-on) |
| Docker | `forgewright-poller-<profile>` container (supercronic, every 5 min by default) | `forgewright-webhook-<profile>` container (always-on) |

Both run in parallel thanks to file locking and fingerprinting — the poller
acts as a safety net if webhooks are lost or delayed.

### Steps

1. Enable in config:
   ```yaml
   webhook_enabled: true
   webhook_port: 5000
   webhook_secret: "your-secret-token"       # or WEBHOOK_SECRET env var
   webhook_debounce_sec: 60                   # collapse bursts into one run
   ```
2. Configure the webhook on the platform side:
   - **GitLab** — *Settings → Webhooks* (or an Admin System Hook for all
     projects):
     - URL: `http://your-server:5000/webhook`
     - Secret token: same as `webhook_secret`
     - Triggers: Issues events, Comments, Merge request events, Pipeline events.
   - **GitHub** — *Settings → Webhooks → Add webhook*:
     - Payload URL: `http://your-server:5000/webhook`
     - Content type: `application/json`
     - Secret: same as `webhook_secret` (HMAC-SHA256 is validated)
     - Events: Issues, Issue comments, Pull requests, Pull request reviews,
       Pull request review comments, Check runs, Workflow runs.
3. Start the service:
   - **Native:** `sudo systemctl enable --now forgewright-webhook.service`
   - **Docker:** the webhook container is only started if `webhook_enabled:
     true` is set in `config.yaml`. After flipping it from `false` to `true`,
     re-run `bash install.docker.sh --profile <claude|opencode>` to bring up
     the webhook container alongside the existing poller. (Or, manually:
     `docker compose --profile <claude|opencode> up -d`.)
4. Verify:
   ```bash
   curl http://localhost:5000/health
   # native:
   journalctl -u forgewright-webhook.service -f
   # docker:
   docker compose --profile claude logs -f webhook-claude
   ```

The Docker deployment runs the webhook as the `webhook-<profile>` container
(always-on, `restart: unless-stopped`) and the poller as the
`poller-<profile>` container (supercronic → `python -m forgewright` on
`POLL_SCHEDULE`). The port defaults to `127.0.0.1:5000` and can be overridden
with `WEBHOOK_PORT` / `WEBHOOK_BIND` in `.env`.

## Manual run and live monitoring

Native:

```bash
# Trigger a one-off poll:
sudo systemctl start forgewright.service

# Watch the journal:
journalctl -u forgewright.service -f

# Follow the live agent output for an in-flight run:
tail -f /var/lib/forgewright/work/worktrees/<project>/<branch>/.claude/claude-live.log
```

Docker:

```bash
cd /opt/forgewright

# Trigger a one-off poll (runs outside the cron loop):
docker compose --profile claude run --rm poller-claude --dry-run -v

# Tail both containers:
docker compose --profile claude logs -f

# Follow the live agent output for an in-flight run:
docker compose --profile claude exec poller-claude \
  tail -f /var/lib/forgewright/work/worktrees/<project>/<branch>/.claude/claude-live.log
```

## Uninstall

Native:

```bash
sudo systemctl disable --now forgewright.timer forgewright.service \
                             forgewright-webhook.service
sudo rm /etc/systemd/system/forgewright.{service,timer}
sudo rm /etc/systemd/system/forgewright-webhook.service
sudo rm -rf /opt/forgewright /var/lib/forgewright /var/log/forgewright /etc/forgewright
sudo userdel -r forgewright
```

Docker:

```bash
cd /opt/forgewright
docker compose --profile claude down -v          # or --profile opencode
sudo rm -rf /opt/forgewright
```

The `-v` flag drops the named volumes (`bot-state`, `bot-logs`,
`claude-config`, `opencode-config`). Omit it if you want to keep the state
for a reinstall.
