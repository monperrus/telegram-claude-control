#!/usr/bin/env bash
# Installs the Telegram Claude controller as a systemd --user service running
# straight out of this checkout (no copy to ~/.local/share -- the unit's
# ExecStart points back at this directory via %h-relative paths).
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${TELEGRAM_CLAUDE_CONFIG:-$HOME/.config/telegram-claude-control.env}"
UNIT_NAME="telegram-claude-controller.service"
UNIT_SRC="$REPO_DIR/systemd/telegram-claude-controller.user.service"
UNIT_DEST="$HOME/.config/systemd/user/$UNIT_NAME"

FORCE=0
START=1
BOT_TOKEN="${BOT_TOKEN:-}"
PAIR_CODE="${PAIR_CODE:-}"

usage() {
    echo "Usage: $0 [--bot-token TOKEN] [--pair-code CODE] [--force] [--no-start]"
    echo "Non-interactive: BOT_TOKEN=... PAIR_CODE=... $0"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --bot-token) BOT_TOKEN="$2"; shift 2 ;;
        --pair-code) PAIR_CODE="$2"; shift 2 ;;
        --force) FORCE=1; shift ;;
        --no-start) START=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 1 ;;
    esac
done

command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemctl is required (systemd --user)." >&2; exit 1; }
command -v tmux >/dev/null || echo "Warning: tmux not found on PATH -- /tmux, /screen, /status, /interrupt will not work." >&2
command -v claude >/dev/null || [ -x "$HOME/.local/bin/claude" ] || echo "Warning: claude CLI not found -- set TELEGRAM_CLAUDE_BIN or install it before pairing." >&2

if [ -f "$CONFIG_PATH" ] && [ "$FORCE" -eq 0 ]; then
    echo "Config already exists at $CONFIG_PATH (use --force to regenerate)."
else
    if [ -z "$BOT_TOKEN" ]; then
        read -r -p "Telegram bot token (from @BotFather): " BOT_TOKEN
    fi
    [ -n "$BOT_TOKEN" ] || { echo "A bot token is required." >&2; exit 1; }

    if [ -z "$PAIR_CODE" ]; then
        PAIR_CODE="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
        echo "Generated pairing code: $PAIR_CODE"
        echo "Send this to the bot from Telegram as: /pair $PAIR_CODE"
    fi

    mkdir -p "$(dirname "$CONFIG_PATH")"
    (
        umask 077
        cat > "$CONFIG_PATH" <<EOF
BOT_TOKEN=$BOT_TOKEN
PAIR_CODE=$PAIR_CODE
EOF
    )
    chmod 600 "$CONFIG_PATH"
    echo "Wrote $CONFIG_PATH (mode 600)."
fi

mkdir -p "$(dirname "$UNIT_DEST")"
cp "$UNIT_SRC" "$UNIT_DEST"
echo "Installed unit to $UNIT_DEST."

systemctl --user daemon-reload
systemctl --user enable "$UNIT_NAME"
if [ "$START" -eq 1 ]; then
    systemctl --user restart "$UNIT_NAME"
    echo "Started $UNIT_NAME. Check status: systemctl --user status $UNIT_NAME"
else
    echo "Skipped starting the service (--no-start). Start it with: systemctl --user start $UNIT_NAME"
fi

echo
echo "For the bot to keep running after you log out, enable lingering once:"
echo "  loginctl enable-linger \"\$USER\""
echo
echo "Validate the install without starting the polling loop:"
echo "  python3 $REPO_DIR/telegram-claude-control.py --check"
