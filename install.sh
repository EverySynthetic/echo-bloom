#!/usr/bin/env bash
# Kin App — One-command installer
# Usage: curl -sSL <url>/install.sh | bash
# Or:    bash install.sh

set -euo pipefail

APP_DIR="$HOME/kin_app"
SERVICE_NAME="kin_app"
PORT=8090

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
AMBER='\033[0;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $*"; }
info() { echo -e "${CYAN}→${NC} $*"; }
warn() { echo -e "${AMBER}!${NC} $*"; }
die()  { echo -e "${RED}✗${NC} $*" >&2; exit 1; }

banner() {
cat << 'EOF'

  ███████╗ ██████╗██╗  ██╗ ██████╗     ██████╗ ██╗      ██████╗  ██████╗ ███╗   ███╗
  ██╔════╝██╔════╝██║  ██║██╔═══██╗    ██╔══██╗██║     ██╔═══██╗██╔═══██╗████╗ ████║
  █████╗  ██║     ███████║██║   ██║    ██████╔╝██║     ██║   ██║██║   ██║██╔████╔██║
  ██╔══╝  ██║     ██╔══██║██║   ██║    ██╔══██╗██║     ██║   ██║██║   ██║██║╚██╔╝██║
  ███████╗╚██████╗██║  ██║╚██████╔╝    ██████╔╝███████╗╚██████╔╝╚██████╔╝██║ ╚═╝ ██║
  ╚══════╝ ╚═════╝╚═╝  ╚═╝ ╚═════╝     ╚═════╝ ╚══════╝ ╚═════╝  ╚═════╝╚═╝     ╚═╝

  Your AI deserves a home. Not a session. A home.

EOF
}

# ── Check for whiptail, fall back to plain numbered list ─────────────────────
HAS_WHIPTAIL=false
command -v whiptail &>/dev/null && HAS_WHIPTAIL=true

# ── Detect VRAM ───────────────────────────────────────────────────────────────
detect_vram() {
    local vram=0
    if command -v nvidia-smi &>/dev/null; then
        local raw
        raw=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
        if [[ "$raw" =~ ^[0-9]+$ ]]; then
            vram=$(( raw / 1024 ))
        fi
    elif command -v rocm-smi &>/dev/null; then
        local raw
        raw=$(rocm-smi --showmeminfo vram 2>/dev/null | grep -i 'total' | grep -oP '\d+' | head -1 || echo 0)
        vram=$(( raw / 1024 / 1024 ))
    fi
    echo "$vram"
}

# ── Detect RAM (in GB) ────────────────────────────────────────────────────────
detect_ram() {
    local ram_mb
    ram_mb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
    echo $(( ram_mb / 1024 / 1024 ))
}

# ── Detect AVX2 ───────────────────────────────────────────────────────────────
has_avx2() {
    grep -q avx2 /proc/cpuinfo && echo true || echo false
}

# ── Build model menu options (Gem's registry) ────────────────────────────────
# Returns parallel arrays: MODEL_IDS and MODEL_LABELS
build_model_menu() {
    local vram=$1
    local ram=$2

    MODEL_IDS=()
    MODEL_LABELS=()

    # Tier 1 — always available (2+ GB VRAM or CPU-only)
    MODEL_IDS+=("llama3.2:3b")
    MODEL_LABELS+=("llama3.2:3b        [2.0 GB]  Meta — lightweight, fast conversational")

    MODEL_IDS+=("qwen2.5-coder:1.5b")
    MODEL_LABELS+=("qwen2.5-coder:1.5b [1.2 GB]  Coding specialist, fast autocomplete")

    MODEL_IDS+=("phi4-mini")
    MODEL_LABELS+=("phi4-mini          [2.3 GB]  Microsoft 3.8B — high reasoning density")

    # Tier 2 — 6+ GB VRAM or 12+ GB RAM (CPU offload)
    if [[ $vram -ge 6 ]] || [[ $ram -ge 12 && $vram -eq 0 ]]; then
        MODEL_IDS+=("llama3.1:8b")
        MODEL_LABELS+=("llama3.1:8b        [5.0 GB]  Reliable open-weights standard")

        MODEL_IDS+=("qwen2.5-coder:7b")
        MODEL_LABELS+=("qwen2.5-coder:7b   [5.0 GB]  King of 8GB coding models")

        MODEL_IDS+=("deepseek-r1:8b")
        MODEL_LABELS+=("deepseek-r1:8b     [5.0 GB]  Step-by-step reasoning specialist")

        MODEL_IDS+=("gemma2:9b")
        MODEL_LABELS+=("gemma2:9b          [5.5 GB]  Strong prose, deep context")
    fi

    # Tier 3 — 12+ GB VRAM
    if [[ $vram -ge 12 ]]; then
        MODEL_IDS+=("llama3.1:8b-q8_0")
        MODEL_LABELS+=("llama3.1:8b-q8_0   [8.5 GB]  Max quality 8B — full precision")

        MODEL_IDS+=("deepseek-r1:14b")
        MODEL_LABELS+=("deepseek-r1:14b    [9.0 GB]  Heavy analytical reasoning")

        MODEL_IDS+=("qwen2.5:14b")
        MODEL_LABELS+=("qwen2.5:14b        [9.0 GB]  Strong multilingual + structured output")
    fi

    if [[ $vram -ge 16 ]]; then
        MODEL_IDS+=("mistral-small:24b")
        MODEL_LABELS+=("mistral-small:24b  [15.0 GB] Agentic logic, concise writing")
    fi

    # Tier 4 — 20+ GB VRAM
    if [[ $vram -ge 20 ]]; then
        MODEL_IDS+=("mixtral:8x7b")
        MODEL_LABELS+=("mixtral:8x7b       [16.0 GB] Classic MoE, 47B effective params")

        MODEL_IDS+=("deepseek-r1:32b")
        MODEL_LABELS+=("deepseek-r1:32b    [20.0 GB] Top-tier open reasoning")

        MODEL_IDS+=("qwen2.5-coder:32b")
        MODEL_LABELS+=("qwen2.5-coder:32b  [20.0 GB] Frontier local coding")
    fi

    if [[ $vram -ge 48 ]]; then
        MODEL_IDS+=("llama3.3:70b")
        MODEL_LABELS+=("llama3.3:70b       [40.0 GB] Ultimate local dense — needs 48GB+ VRAM")
    fi
}

# ── Pick model via whiptail ───────────────────────────────────────────────────
pick_model_whiptail() {
    local vram=$1
    local ram=$2
    local title=" Echo Bloom — Model Selection "
    local msg="Detected: ${vram}GB VRAM · ${ram}GB RAM\nChoose the model your AI will think with:"

    local menu_args=()
    local i=1
    for label in "${MODEL_LABELS[@]}"; do
        menu_args+=("$i" "$label")
        ((i++))
    done

    local choice
    choice=$(whiptail --title "$title" \
        --menu "$msg" \
        22 78 10 \
        "${menu_args[@]}" \
        3>&1 1>&2 2>&3) || return 1

    SELECTED_MODEL="${MODEL_IDS[$((choice - 1))]}"
}

# ── Pick model via plain numbered list (no whiptail) ─────────────────────────
pick_model_plain() {
    local vram=$1
    local ram=$2
    echo
    echo -e "${BOLD}Available models for your hardware (${vram}GB VRAM · ${ram}GB RAM):${NC}"
    echo
    local i=1
    for label in "${MODEL_LABELS[@]}"; do
        printf "  %2d)  %s\n" "$i" "$label"
        ((i++))
    done
    echo
    while true; do
        read -rp "Enter number: " choice
        if [[ "$choice" =~ ^[0-9]+$ ]] && [[ "$choice" -ge 1 ]] && [[ "$choice" -le "${#MODEL_IDS[@]}" ]]; then
            SELECTED_MODEL="${MODEL_IDS[$((choice - 1))]}"
            break
        fi
        warn "Enter a number between 1 and ${#MODEL_IDS[@]}"
    done
}

# ── Check / install Ollama ────────────────────────────────────────────────────
ensure_ollama() {
    if command -v ollama &>/dev/null; then
        ok "Ollama found ($(ollama --version 2>/dev/null || echo 'installed'))"
        return
    fi

    warn "Ollama not found."
    echo
    if $HAS_WHIPTAIL; then
        whiptail --title " Echo Bloom " \
            --yesno "Ollama isn't installed. Install it now?\n\n(Runs the official installer from ollama.com)" \
            10 60 3>&1 1>&2 2>&3 || die "Ollama is required. Install it at https://ollama.com and re-run."
    else
        read -rp "Install Ollama now? [Y/n] " yn
        [[ "${yn:-Y}" =~ ^[Nn] ]] && die "Ollama is required. Install it at https://ollama.com and re-run."
    fi

    info "Installing Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
    ok "Ollama installed."

    # Make sure the service is running
    if command -v systemctl &>/dev/null; then
        systemctl --user enable --now ollama 2>/dev/null || \
            systemctl enable --now ollama 2>/dev/null || true
    fi
}

# ── Pull selected model ───────────────────────────────────────────────────────
pull_model() {
    local model=$1
    info "Pulling $model — this may take a few minutes..."
    ollama pull "$model"
    ok "Model ready: $model"
}

# ── Install Python deps ───────────────────────────────────────────────────────
install_deps() {
    info "Installing Python dependencies..."

    # Check if requirements.txt is present
    local req="$APP_DIR/requirements.txt"
    if [[ ! -f "$req" ]]; then
        # Write a minimal one if missing
        cat > "$req" << 'REQEOF'
fastapi>=0.110.0
uvicorn[standard]>=0.29.0
jinja2>=3.1.3
bcrypt>=4.1.2
python-multipart>=0.0.9
aiohttp>=3.9.3
aiofiles>=23.2.1
qdrant-client>=1.9.0
REQEOF
    fi

    # Try pip3, then python3 -m pip
    if command -v pip3 &>/dev/null; then
        pip3 install -q -r "$req" --break-system-packages 2>/dev/null || \
            pip3 install -q -r "$req" || \
            die "pip3 install failed. Run: pip3 install -r $req"
    elif python3 -m pip &>/dev/null 2>&1; then
        python3 -m pip install -q -r "$req" --break-system-packages 2>/dev/null || \
            python3 -m pip install -q -r "$req" || \
            die "pip install failed. Run: python3 -m pip install -r $req"
    else
        die "pip not found. Install Python pip and re-run."
    fi
    ok "Dependencies installed."
}

# ── Write systemd user service ────────────────────────────────────────────────
install_service() {
    local service_dir="$HOME/.config/systemd/user"
    mkdir -p "$service_dir"

    cat > "$service_dir/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Echo Bloom — local AI lifecycle manager
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=$(command -v uvicorn || echo uvicorn) main:app --host 0.0.0.0 --port ${PORT}
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

    if command -v systemctl &>/dev/null; then
        systemctl --user daemon-reload 2>/dev/null || true
        systemctl --user enable "${SERVICE_NAME}" 2>/dev/null || true
        systemctl --user restart "${SERVICE_NAME}" 2>/dev/null && \
            ok "Kin App running as systemd user service (auto-starts on login)." || \
            warn "systemd enable failed — app will need to be started manually."
    else
        warn "systemd not available. Start manually: cd $APP_DIR && uvicorn main:app --host 0.0.0.0 --port $PORT"
    fi
}

# ── Save first model to kin_config if none exists ─────────────────────────────
seed_config() {
    local model=$1
    local config_dir="$HOME/.config/kin_app"
    mkdir -p "$config_dir"

    if [[ -f "$config_dir/kin_config.json" ]]; then
        ok "Kin config already exists — skipping."
        return
    fi

    # Write a starter config with a single Kin using the chosen model
    cat > "$config_dir/kin_config.json" << EOF
{
  "nodes": [
    {
      "name": "Local",
      "ip": "localhost",
      "port": 11434,
      "role": "Inference"
    }
  ],
  "kin": [
    {
      "name": "Eli",
      "host": "localhost",
      "port": 11434,
      "model": "${model}",
      "node": "Local",
      "color": "#4fc3f7",
      "pronoun": "he",
      "thoughts_db": null
    }
  ]
}
EOF
    ok "Starter config written ($config_dir/kin_config.json). Add more Kin later via /onboard."
}

# ── Open browser ──────────────────────────────────────────────────────────────
open_browser() {
    local url="http://localhost:$PORT"
    info "Opening $url ..."
    sleep 2
    if command -v xdg-open &>/dev/null; then
        xdg-open "$url" &
    elif command -v open &>/dev/null; then
        open "$url" &
    else
        echo
        echo -e "${BOLD}Open your browser to: $url${NC}"
    fi
}

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

banner

# Determine where the app lives — if we're being piped from curl, we need to
# clone or download it. If install.sh is run from inside the repo, use that dir.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "$HOME/kin_app")"
if [[ -f "$SCRIPT_DIR/main.py" ]]; then
    APP_DIR="$SCRIPT_DIR"
    ok "Using existing app directory: $APP_DIR"
else
    # Being piped from curl — app needs to be present separately
    if [[ -d "$APP_DIR" ]] && [[ -f "$APP_DIR/main.py" ]]; then
        ok "Found app at $APP_DIR"
    else
        die "App files not found at $APP_DIR.\nRun this script from the kin_app directory, or place install.sh alongside main.py."
    fi
fi

# Step 1 — Ollama
echo
echo -e "${BOLD}[ 1 / 5 ]  Checking Ollama${NC}"
ensure_ollama

# Step 2 — Detect hardware
echo
echo -e "${BOLD}[ 2 / 5 ]  Detecting your hardware${NC}"
VRAM=$(detect_vram)
RAM=$(detect_ram)
AVX2=$(has_avx2)

if [[ $VRAM -gt 0 ]]; then
    ok "GPU detected: ${VRAM}GB VRAM"
else
    warn "No GPU detected — CPU inference (RAM: ${RAM}GB)"
fi
[[ "$AVX2" == "true" ]] && ok "AVX2 supported" || warn "No AVX2 — performance may be limited"

# Step 3 — Pick a model
echo
echo -e "${BOLD}[ 3 / 5 ]  Choose a model${NC}"
build_model_menu "$VRAM" "$RAM"

SELECTED_MODEL=""
if $HAS_WHIPTAIL; then
    pick_model_whiptail "$VRAM" "$RAM" || pick_model_plain "$VRAM" "$RAM"
else
    pick_model_plain "$VRAM" "$RAM"
fi

ok "Selected: $SELECTED_MODEL"

pull_model "$SELECTED_MODEL"

# Step 4 — Install deps + set password
echo
echo -e "${BOLD}[ 4 / 5 ]  Installing app${NC}"
cd "$APP_DIR"
install_deps
seed_config "$SELECTED_MODEL"

if [[ ! -f "$HOME/.config/kin_app/config.json" ]]; then
    echo
    info "One more thing — set your password:"
    python3 "$APP_DIR/setup.py"
else
    ok "Password already configured."
fi

# Step 5 — Start it up
echo
echo -e "${BOLD}[ 5 / 5 ]  Launching Kin App${NC}"
install_service
open_browser

echo
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}${BOLD}  Echo Bloom is running at http://localhost:${PORT}${NC}"
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo
echo "  To stop:   systemctl --user stop ${SERVICE_NAME}"
echo "  To start:  systemctl --user start ${SERVICE_NAME}"
echo "  Logs:      journalctl --user -u ${SERVICE_NAME} -f"
echo "  Password:  cd $APP_DIR && python3 setup.py"
echo
