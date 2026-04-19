#!/usr/bin/env bash
# Update an existing forgewright installation from the repo checkout.
# Run as root: sudo bash update.sh
#
# This updates the bot script, dependencies, and systemd units without
# touching config, state, or the agent CLI installation.
set -euo pipefail

BOT_USER="forgewright"
INSTALL_DIR="/opt/forgewright"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Sanity checks
if [ "$(id -u)" -ne 0 ]; then
  echo "error: must run as root (sudo bash update.sh)" >&2
  exit 1
fi
if [ ! -d "$INSTALL_DIR" ]; then
  echo "error: $INSTALL_DIR does not exist — run install.sh first" >&2
  exit 1
fi

echo "==> updating bot files"
install -o "$BOT_USER" -g "$BOT_USER" -m 0755 "$SCRIPT_DIR/forgewright.py"   "$INSTALL_DIR/forgewright.py"
install -o "$BOT_USER" -g "$BOT_USER" -m 0644 "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
install -o "$BOT_USER" -g "$BOT_USER" -m 0644 "$SCRIPT_DIR/pyproject.toml"   "$INSTALL_DIR/pyproject.toml"
rm -rf "$INSTALL_DIR/forgewright"
cp -r "$SCRIPT_DIR/forgewright" "$INSTALL_DIR/forgewright"
chown -R "$BOT_USER:$BOT_USER" "$INSTALL_DIR/forgewright"

echo "==> updating python dependencies"
sudo -u "$BOT_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip wheel -q
# --force-reinstall ensures the package is re-installed even if the version
# string hasn't changed (important for migration from the old single-file layout).
sudo -u "$BOT_USER" "$INSTALL_DIR/venv/bin/pip" install --force-reinstall --no-deps "$INSTALL_DIR" -q
sudo -u "$BOT_USER" "$INSTALL_DIR/venv/bin/pip" install "$INSTALL_DIR" -q

echo "==> updating systemd units"
install -m 0644 "$SCRIPT_DIR/systemd/forgewright.service"         /etc/systemd/system/forgewright.service
install -m 0644 "$SCRIPT_DIR/systemd/forgewright.timer"           /etc/systemd/system/forgewright.timer
install -m 0644 "$SCRIPT_DIR/systemd/forgewright-webhook.service" /etc/systemd/system/forgewright-webhook.service
systemctl daemon-reload

if systemctl is-active --quiet forgewright-webhook.service 2>/dev/null; then
  echo "==> restarting webhook service"
  systemctl restart forgewright-webhook.service
fi

echo ""
echo "Update complete."
echo "  - Config untouched:  /etc/forgewright/config.yaml"
echo "  - State untouched:   /var/lib/forgewright/"
echo "  - Timer status:      systemctl status forgewright.timer"
echo "  - Webhook status:    systemctl status forgewright-webhook.service"
echo "  - Next run will use the updated script automatically."
