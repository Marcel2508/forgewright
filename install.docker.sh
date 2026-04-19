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

if [ ! -f "$INSTALL_DIR/config.yaml" ]; then
  $SUDO install -m 0640 "$SCRIPT_DIR/config.example.yaml" "$INSTALL_DIR/config.yaml"
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

# --- Pull and start ----------------------------------------------------------

echo "==> pulling prebuilt images"
(cd "$INSTALL_DIR" && $SUDO docker compose --profile "$PROFILE" pull)

echo "==> starting services"
(cd "$INSTALL_DIR" && $SUDO docker compose --profile "$PROFILE" up -d)

cat <<EOF

Done. Services are running under profile "$PROFILE".

Next steps:
  1. Edit config:   $INSTALL_DIR/config.yaml
  2. Edit secrets:  $INSTALL_DIR/.env  (PLATFORM_TOKEN, ANTHROPIC_API_KEY, etc.)
  3. After editing either file, restart:
       cd $INSTALL_DIR && docker compose --profile $PROFILE up -d --force-recreate

Useful commands (from $INSTALL_DIR):
  docker compose --profile $PROFILE ps
  docker compose --profile $PROFILE logs -f
  docker compose --profile $PROFILE run --rm poller-$PROFILE --dry-run -v
  docker compose --profile $PROFILE down
EOF
