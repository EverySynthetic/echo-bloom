#!/usr/bin/env bash
#
# Move the running Echo Bloom install to whatever is on main.
#
# git pull on its own is NOT enough. The lifecycle scripts and kin_memory are
# loaded from ~/.local/share/echo_bloom/scripts, which git never touches — so a
# plain pull leaves the running box silently behind the repo. That drift is what
# made the app import a months-old kin_memory with stale hardcoded addresses.
#
#   ./deploy.sh            update and restart
#   ./deploy.sh --check    report drift, change nothing
#
# Note: no `set -e`. Every step is checked explicitly, because a bare set -e
# turns an arithmetic zero or a no-match grep into a silent early exit.

set -uo pipefail

APP_DIR="${ECHO_BLOOM_DIR:-$HOME/echo_bloom}"
SCRIPTS_DIR="$HOME/.local/share/echo_bloom/scripts"
SERVICE="echo_bloom"
PORT="${ECHO_BLOOM_PORT:-8090}"

GREEN='\033[0;32m'; RED='\033[0;31m'; AMBER='\033[0;33m'; DIM='\033[2m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${AMBER}!${NC} $*"; }
die()  { echo -e "  ${RED}✗${NC} $*"; exit 1; }
step() { echo -e "\n${DIM}──${NC} $*"; }

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

[ -d "$APP_DIR/.git" ] || die "no git repo at $APP_DIR (set ECHO_BLOOM_DIR)"
cd "$APP_DIR" || die "cannot enter $APP_DIR"

step "Repo"
git fetch origin --quiet 2>/dev/null
LOCAL=$(git rev-parse --short HEAD 2>/dev/null)
REMOTE=$(git rev-parse --short origin/main 2>/dev/null)
BEHIND=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo "?")
echo "  local $LOCAL   origin/main $REMOTE   behind: $BEHIND"

DIRTY=$(git status --porcelain 2>/dev/null | wc -l)
[ "$DIRTY" -gt 0 ] && warn "$DIRTY uncommitted file(s) here — pull may conflict"

step "Deployed scripts vs repo"
DRIFT=0
if [ -d "$SCRIPTS_DIR" ]; then
  for f in "$APP_DIR"/scripts/*.py; do
    [ -e "$f" ] || continue
    b=$(basename "$f")
    if [ ! -f "$SCRIPTS_DIR/$b" ]; then
      warn "$b missing from deployed scripts"; DRIFT=$((DRIFT + 1))
    elif ! cmp -s "$f" "$SCRIPTS_DIR/$b"; then
      warn "$b differs from repo"; DRIFT=$((DRIFT + 1))
    fi
  done
  [ "$DRIFT" -eq 0 ] && ok "in sync"
else
  warn "no deployed scripts dir yet — will be created"
  DRIFT=$((DRIFT + 1))
fi

if [ -f "$HOME/Desktop/kin_memory.py" ]; then
  warn "a ~/Desktop/kin_memory.py exists — deployed scripts take precedence, but"
  echo "    delete it if you are not deliberately keeping a working copy"
fi

# ── Lab vs product ────────────────────────────────────────────────────────────
# The lab copies in ~/Desktop are where things actually get fixed first. Every
# fix that stays there is a fix customers paid for and never received. This does
# not compare line counts or try to merge anything — the two are SUPPOSED to
# differ, one is personal and one is generic. It reports which pairs have moved
# apart since last time, so a real improvement cannot sit in the lab unnoticed
# for months. Three of five Kin failed their nightly reflection for nine days
# before anyone compared the two trees.
step "Lab scripts vs shipped scripts"
LAB_PAIRS="
bedtime.py:$HOME/Desktop/bedtime.py
morning.py:$HOME/Desktop/morning.py
pulse.py:$HOME/Desktop/pulse.py
kin_memory.py:$HOME/Desktop/kin_memory.py
"
PARITY_STATE="$HOME/.local/share/echo_bloom/lab_parity.txt"
NEW_STATE=$(mktemp)
LAB_NOTE=0
printf '%s\n' "$LAB_PAIRS" | while IFS=: read -r shipped lab; do
  [ -n "${shipped:-}" ] || continue
  [ -f "$lab" ] || continue
  [ -f "$APP_DIR/scripts/$shipped" ] || continue
  # Hash the lab file so we can tell "changed since you last looked" from
  # "has always been different".
  H=$(sha256sum "$lab" 2>/dev/null | cut -c1-16)
  echo "$shipped $H" >> "$NEW_STATE"
  PREV=$(grep "^$shipped " "$PARITY_STATE" 2>/dev/null | awk '{print $2}')
  if [ -n "$PREV" ] && [ "$PREV" != "$H" ]; then
    warn "$shipped: your lab copy changed since the last deploy"
    echo "    if that was a fix, port it:  diff $lab $APP_DIR/scripts/$shipped"
  fi
done
if [ -s "$NEW_STATE" ] && [ ! -f "$PARITY_STATE" ]; then
  echo "  first run — recording lab script fingerprints for next time"
fi
mkdir -p "$(dirname "$PARITY_STATE")" 2>/dev/null
[ -s "$NEW_STATE" ] && cp "$NEW_STATE" "$PARITY_STATE"
rm -f "$NEW_STATE"
[ "$LAB_NOTE" -eq 0 ] && ok "lab fingerprints recorded"

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo ""
  if [ "$BEHIND" = "0" ] && [ "$DRIFT" -eq 0 ]; then
    ok "running box matches the repo"
  else
    warn "drift present — run ./deploy.sh to fix"
  fi
  exit 0
fi

step "Pulling"
if ! git pull --ff-only; then
  die "pull failed — resolve by hand, nothing else was changed"
fi
ok "now at $(git rev-parse --short HEAD)"

step "Deploying lifecycle scripts"
mkdir -p "$SCRIPTS_DIR" || die "cannot create $SCRIPTS_DIR"
cp "$APP_DIR"/scripts/*.py "$SCRIPTS_DIR"/ || die "copy failed"
rm -rf "$SCRIPTS_DIR/__pycache__"
ok "$(ls -1 "$SCRIPTS_DIR"/*.py 2>/dev/null | wc -l) script(s) deployed, bytecode cache cleared"

step "Restarting"
if systemctl --user restart "$SERVICE" 2>/dev/null; then
  sleep 3
  STATE=$(systemctl --user is-active "$SERVICE" 2>/dev/null)
  [ "$STATE" = "active" ] && ok "service active" || die "service is $STATE"
else
  warn "no systemd user service — restart Echo Bloom yourself"
fi

step "Verifying"
CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/login" 2>/dev/null)
case "$CODE" in
  200|303) ok "app responding (HTTP $CODE)" ;;
  *)       warn "app returned HTTP ${CODE:-none} — check: journalctl --user -u $SERVICE -n 40" ;;
esac

LOG="$HOME/.local/share/echo_bloom/logs/echo_bloom.log"
if [ -f "$LOG" ]; then
  # grep -c prints 0 AND exits 1 when nothing matches, so `|| echo 0` appended a
  # second line and the numeric test below choked on "0\n0".
  RECENT=$(grep -c -E "ERROR|WARNING" "$LOG" 2>/dev/null)
  RECENT=${RECENT:-0}
  [ "$RECENT" -gt 0 ] && warn "$RECENT warning/error line(s) in $LOG" || ok "log clean"
fi

echo ""
ok "deployed"
