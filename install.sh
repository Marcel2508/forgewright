#!/usr/bin/env bash
# Installer for forgewright on a Debian/Ubuntu VM.
# Run as root: sudo bash install.sh
set -euo pipefail

BOT_USER="forgewright"
INSTALL_DIR="/opt/forgewright"
STATE_DIR="/var/lib/forgewright"
LOG_DIR="/var/log/forgewright"
ETC_DIR="/etc/forgewright"

echo "==> apt deps"
apt-get update
apt-get install -y python3 python3-venv python3-pip git curl ca-certificates

echo "==> user + dirs"
id -u "$BOT_USER" &>/dev/null || useradd --system --create-home --shell /bin/bash "$BOT_USER"
install -d -o "$BOT_USER" -g "$BOT_USER" -m 0750 "$INSTALL_DIR" "$STATE_DIR" "$LOG_DIR"
install -d -o root        -g "$BOT_USER" -m 0750 "$ETC_DIR"

echo "==> copy bot files"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
install -o "$BOT_USER" -g "$BOT_USER" -m 0755 "$SCRIPT_DIR/forgewright.py"      "$INSTALL_DIR/forgewright.py"
install -o "$BOT_USER" -g "$BOT_USER" -m 0644 "$SCRIPT_DIR/requirements.txt"    "$INSTALL_DIR/requirements.txt"
install -o "$BOT_USER" -g "$BOT_USER" -m 0644 "$SCRIPT_DIR/pyproject.toml"      "$INSTALL_DIR/pyproject.toml"
cp -r "$SCRIPT_DIR/forgewright" "$INSTALL_DIR/forgewright"
chown -R "$BOT_USER:$BOT_USER" "$INSTALL_DIR/forgewright"
if [ ! -f "$ETC_DIR/config.yaml" ]; then
  install -o root -g "$BOT_USER" -m 0640 "$SCRIPT_DIR/config.example.yaml" "$ETC_DIR/config.yaml"
  echo "   -> wrote $ETC_DIR/config.yaml (edit it!)"
else
  echo "   -> $ETC_DIR/config.yaml already exists, leaving untouched"
fi

echo "==> python venv"
sudo -u "$BOT_USER" python3 -m venv "$INSTALL_DIR/venv"
sudo -u "$BOT_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip wheel
sudo -u "$BOT_USER" "$INSTALL_DIR/venv/bin/pip" install "$INSTALL_DIR"

echo "==> systemd units"
install -m 0644 "$SCRIPT_DIR/systemd/forgewright.service"         /etc/systemd/system/forgewright.service
install -m 0644 "$SCRIPT_DIR/systemd/forgewright.timer"           /etc/systemd/system/forgewright.timer
install -m 0644 "$SCRIPT_DIR/systemd/forgewright-webhook.service" /etc/systemd/system/forgewright-webhook.service
systemctl daemon-reload
systemctl enable --now forgewright.timer

cat <<EOF

Done. Next steps:
  1. Edit $ETC_DIR/config.yaml (at minimum: platform_token or use PLATFORM_TOKEN env).
  2. Make sure the agent CLI is installed for the $BOT_USER user, e.g.:
       sudo -iu $BOT_USER
       # Claude Code: npm install -g @anthropic-ai/claude-code, then 'claude login'
       # OpenCode:    follow the opencode install instructions
     or set ANTHROPIC_API_KEY (or the OpenCode provider key) in the systemd drop-in:
       sudo systemctl edit forgewright.service
  3. Run once manually to verify:
       sudo -u $BOT_USER FORGEWRIGHT_CONFIG=$ETC_DIR/config.yaml \\
         $INSTALL_DIR/venv/bin/python $INSTALL_DIR/forgewright.py --dry-run -v
  4. Watch logs:
       journalctl -u forgewright.service -f
       tail -f $LOG_DIR/bot.log

  Optional — enable the webhook server for near-real-time events:
  5. Set webhook_enabled: true in $ETC_DIR/config.yaml
  6. Configure a webhook in GitLab/GitHub pointing to http://localhost:5000/webhook
  7. Start the webhook service:
       sudo systemctl enable --now forgewright-webhook.service
EOF
