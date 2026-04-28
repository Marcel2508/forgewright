#!/usr/bin/env bash
# Update an existing Docker-based forgewright installation.
# Refreshes docker-compose.yml, pulls the latest prebuilt image from GHCR,
# and recreates the services. Config and persistent state (named volumes)
# are NOT touched.
#
# Usage:
#   bash update.docker.sh [--profile claude|opencode] [--dir /opt/forgewright]
set -euo pipefail

PROFILE="claude"
INSTALL_DIR="/opt/forgewright"

while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2 ;;
    --dir)     INSTALL_DIR="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ ! -d "$INSTALL_DIR" ]; then
  echo "error: $INSTALL_DIR does not exist — run install.docker.sh first" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

SUDO=""
if [ ! -w "$INSTALL_DIR" ] && [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

echo "==> refreshing docker-compose.yml in $INSTALL_DIR"
$SUDO install -m 0644 "$SCRIPT_DIR/docker-compose.yml" "$INSTALL_DIR/docker-compose.yml"

# Preserve runtime state under claude-state/; seed only if missing.
$SUDO install -d -m 0755 "$INSTALL_DIR/docker/claude-state/.claude"
if [ ! -f "$INSTALL_DIR/docker/claude-state/.claude.json" ]; then
  $SUDO sh -c "printf '{}\n' > '$INSTALL_DIR/docker/claude-state/.claude.json'"
fi

# Decide which compose profile to bring up based on webhook_enabled.
ACTIVE_PROFILE="${PROFILE}-poll"
if [ -f "$INSTALL_DIR/config.yaml" ] && \
   $SUDO grep -Eqi '^[[:space:]]*webhook_enabled[[:space:]]*:[[:space:]]*true[[:space:]]*$' "$INSTALL_DIR/config.yaml"; then
  ACTIVE_PROFILE="$PROFILE"
fi

echo "==> pulling latest images (profile: $PROFILE)"
(cd "$INSTALL_DIR" && $SUDO docker compose --profile "$PROFILE" pull)

# If we're updating to a poller-only setup, explicitly stop the webhook
# container in case it was running from a previous (umbrella) install.
if [ "$ACTIVE_PROFILE" != "$PROFILE" ]; then
  (cd "$INSTALL_DIR" && $SUDO docker compose rm -fs "webhook-$PROFILE" 2>/dev/null || true)
fi

echo "==> recreating containers (profile: $ACTIVE_PROFILE)"
(cd "$INSTALL_DIR" && $SUDO docker compose --profile "$ACTIVE_PROFILE" up -d --force-recreate)

cat <<EOF

Update complete (profile: $ACTIVE_PROFILE).
  - Config untouched:  $INSTALL_DIR/config.yaml
  - .env untouched:    $INSTALL_DIR/.env
  - Volumes untouched: bot-state, bot-logs, claude-config, opencode-config
  - Check status:      docker compose --profile $ACTIVE_PROFILE ps
  - Tail logs:         docker compose --profile $ACTIVE_PROFILE logs -f
EOF
