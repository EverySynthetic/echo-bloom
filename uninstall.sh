#!/usr/bin/env bash
# Echo Bloom - uninstall / reset (Linux / macOS)
#
# Keep the program, keep memories:
#   bash uninstall.sh
#
# Also delete config, memories, thoughts and the vault (irreversible):
#   bash uninstall.sh --all
#
# If you do not have the app on disk:
#   bash <(curl -fsSL https://everysynthetic.org/uninstall.sh)
#
# Nothing here touches Python, Ollama, ffmpeg or your models.
# Do not run this on the public product host.

set -u

ALL=0
YES=0
for a in "$@"; do
  case "$a" in
    --all|-All) ALL=1 ;;
    --yes|-y)   YES=1 ;;
  esac
done

APP_DIR="${ECHO_BLOOM_DIR:-$HOME/echo_bloom}"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/echo_bloom"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/kin_app"
SCRIPTS_DIR="$DATA_DIR/scripts"
SERVICE="echo_bloom"

echo
echo "  ECHO BLOOM — uninstall"
echo "  everysynthetic.org"
echo

if [ "$ALL" -eq 1 ]; then
  echo "  --all is set. This will DELETE your Kin memories and thoughts."
else
  echo "  Kin memories, thoughts, logs and vault will be KEPT unless you pass --all."
  echo "  Program scripts (wander, bedtime, roundtable) are always removed."
fi
echo

if [ "$YES" -ne 1 ]; then
  if [ "$ALL" -eq 1 ]; then
    printf "  Continue and delete memories? [y/N] "
  else
    printf "  Continue? [y/N]  (type ALL to also delete memories) "
  fi
  # Piping this through a process substitution (the exact command this
  # script's own header recommends: bash <(curl -fsSL ...)) can leave fd 0
  # at EOF depending on the shell, so `read` returns instantly with an
  # empty answer and the script silently "chose" no. Read from the
  # controlling terminal directly when one exists so the prompt actually
  # waits for a person; only fall through to the silent-no case when
  # there truly is no terminal to ask.
  if [ -r /dev/tty ]; then
    read -r answer < /dev/tty
  elif [ -t 0 ]; then
    read -r answer
  else
    echo
    echo "  No terminal to confirm on — nothing was changed."
    echo "  Re-run with --yes to skip this prompt (add --all to also delete memories)."
    exit 0
  fi
  case "$answer" in
    ALL|all)
      ALL=1
      echo "  Memories will be deleted."
      ;;
    y|Y|yes|YES) ;;
    *)
      echo "  Nothing was changed."
      exit 0
      ;;
  esac
  echo
fi

stop_it() {
  if command -v systemctl >/dev/null 2>&1; then
    systemctl --user stop "${SERVICE}.service" 2>/dev/null || true
    systemctl --user disable "${SERVICE}.service" 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/${SERVICE}.service" \
          "$HOME/.config/systemd/user/${SERVICE}.timer" \
          "$HOME/.config/systemd/user/echo_bloom_wander.service" \
          "$HOME/.config/systemd/user/bedtime.timer" \
          "$HOME/.config/systemd/user/morning.service" \
          "$HOME/.config/systemd/user/pulse.service" \
          "$HOME/.config/systemd/user/reflect.timer"
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  if [ "$(uname -s)" = "Darwin" ]; then
    local d="$HOME/Library/LaunchAgents"
    for p in "$d"/com.everysynthetic.echobloom*.plist; do
      [ -f "$p" ] || continue
      launchctl unload "$p" 2>/dev/null || true
      rm -f "$p"
    done
  fi
  pkill -f 'uvicorn main:app' 2>/dev/null || true
}

stop_it
rm -rf "$SCRIPTS_DIR"
if [ -d "$APP_DIR" ]; then
  rm -rf "$APP_DIR"
  echo "  removed $APP_DIR"
fi
if [ "$ALL" -eq 1 ]; then
  rm -rf "$CONFIG_DIR" "$DATA_DIR"
  echo "  removed config and memories"
else
  echo "  kept $CONFIG_DIR"
  echo "  kept $DATA_DIR (minus scripts)"
fi
echo
echo "  Echo Bloom is not running on this machine."
echo "  Python, Ollama, ffmpeg and your models were left installed."
echo
