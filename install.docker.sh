#!/usr/bin/env bash
# Installer for forgewright using Docker Compose.
# No systemd required — the webhook and poller both run as long-lived
# containers with `restart: unless-stopped`. Polling is driven by
# supercronic inside the poller container.
#
# Uses the prebuilt images published to GHCR
# (ghcr.io/marcel2508/forgewright-{claude,opencode}:latest).
#
# Usage:
#   bash install.docker.sh [--profile claude|opencode] [--dir /opt/forgewright]
#
# Defaults to --profile claude and --dir /opt/forgewright.
set -euo pipefail

PROFILE="claude"
INSTALL_DIR="/opt/forgewright"

while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --dir)     INSTALL_DIR="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ "$PROFILE" != "claude" ] && [ "$PROFILE" != "opencode" ]; then
  echo "error: --profile must be 'claude' or 'opencode'" >&2
  exit 2
fi

# --- Pre-flight checks -------------------------------------------------------

if ! command -v docker &>/dev/null; then
  echo "error: docker is not installed" >&2
  exit 1
fi
if ! docker compose version &>/dev/null; then
  echo "error: 'docker compose' plugin is not available" >&2
  echo "       (install the docker-compose-plugin package)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Copy project into install dir -------------------------------------------

SUDO=""
if [ ! -w "$(dirname "$INSTALL_DIR")" ] && [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

echo "==> installing to $INSTALL_DIR (profile: $PROFILE)"
$SUDO install -d -m 0755 "$INSTALL_DIR"

# Only docker-compose.yml is needed on the host — everything else
# (source, entrypoint) is baked into the prebuilt image.
$SUDO install -m 0644 "$SCRIPT_DIR/docker-compose.yml" "$INSTALL_DIR/docker-compose.yml"

# Seed the fallback Claude state dir used when CLAUDE_HOME is unset.
$SUDO install -d -m 0755 "$INSTALL_DIR/docker/claude-state/.claude"
if [ ! -f "$INSTALL_DIR/docker/claude-state/.claude.json" ]; then
  $SUDO sh -c "printf '{}\n' > '$INSTALL_DIR/docker/claude-state/.claude.json'"
fi

# --- Config + .env -----------------------------------------------------------

if [ -d "$INSTALL_DIR/config.yaml" ]; then
  echo "error: $INSTALL_DIR/config.yaml exists as a directory, not a file." >&2
  echo "       This usually means 'docker compose up' ran before the file was" >&2
  echo "       created, so Docker auto-created an empty placeholder. Remove it" >&2
  echo "       and re-run this installer:" >&2
  echo "         sudo rm -rf $INSTALL_DIR/config.yaml" >&2
  exit 1
fi

if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
  # 0644 (not 0640): the in-container `forgewright` user is a non-root system
  # user not in root's group, so it can't read a root-owned 0640 file. The
  # config has no secrets by default — those go in .env or env vars.
  $SUDO install -m 0644 "$SCRIPT_DIR/config.example.yaml" "$INSTALL_DIR/config.yaml"
  echo "   -> wrote $INSTALL_DIR/config.yaml (edit it!)"
else
  echo "   -> $INSTALL_DIR/config.yaml already exists, leaving untouched"
fi

if [ ! -f "$INSTALL_DIR/.env" ]; then
  $SUDO install -m 0600 "$SCRIPT_DIR/.env.example" "$INSTALL_DIR/.env"
  echo "   -> wrote $INSTALL_DIR/.env (edit it!)"
else
  echo "   -> $INSTALL_DIR/.env already exists, leaving untouched"
fi

# --- Decide active compose profile based on webhook_enabled ------------------

# Pull always uses the umbrella profile so both images are local even if the
# user later flips webhook_enabled. Up uses the umbrella when webhook_enabled
# is true, otherwise the `-poll` per-component profile (poller only).
ACTIVE_PROFILE="${PROFILE}-poll"
if $SUDO grep -Eqi '^[[:space:]]*webhook_enabled[[:space:]]*:[[:space:]]*true[[:space:]]*$' "$INSTALL_DIR/config.yaml"; then
  ACTIVE_PROFILE="$PROFILE"
  echo "   -> webhook_enabled: true detected — will start poller + webhook"
else
  echo "   -> webhook_enabled: false (or unset) — will start poller only"
fi

# --- Pull and start ----------------------------------------------------------

echo "==> pulling prebuilt images"
(cd "$INSTALL_DIR" && $SUDO docker compose --profile "$PROFILE" pull)

# If switching to a poller-only setup, stop any webhook container left over
# from a previous (umbrella) run.
if [ "$ACTIVE_PROFILE" != "$PROFILE" ]; then
  (cd "$INSTALL_DIR" && $SUDO docker compose rm -fs "webhook-$PROFILE" 2>/dev/null || true)
fi

echo "==> starting services (profile: $ACTIVE_PROFILE)"
(cd "$INSTALL_DIR" && $SUDO docker compose --profile "$ACTIVE_PROFILE" up -d)

cat <<EOF

Done. Services are running under profile "$ACTIVE_PROFILE".

Next steps:
  1. Edit config:   $INSTALL_DIR/config.yaml
  2. Edit secrets:  $INSTALL_DIR/.env  (PLATFORM_TOKEN, ANTHROPIC_API_KEY, etc.)
  3. After editing either file, recreate:
       cd $INSTALL_DIR && bash $(basename "$0") --profile $PROFILE
     (re-running the installer picks up webhook_enabled changes; for unrelated
     edits a plain \`docker compose --profile $ACTIVE_PROFILE up -d --force-recreate\` is enough)

Useful commands (from $INSTALL_DIR):
  docker compose --profile $ACTIVE_PROFILE ps
  docker compose --profile $ACTIVE_PROFILE logs -f
  docker compose --profile $ACTIVE_PROFILE run --rm poller-$PROFILE --dry-run -v
  docker compose --profile $PROFILE down       # umbrella stops everything
EOF
