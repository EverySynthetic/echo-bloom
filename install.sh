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
        # Sum VRAM across ALL GPUs
        local total_mb
        total_mb=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
            | tr -d ' ' | awk '{sum += $1} END {print sum+0}')
        if [[ "$total_mb" =~ ^[0-9]+$ ]] && [[ "$total_mb" -gt 0 ]]; then
            vram=$(( total_mb / 1024 ))
        fi
    elif command -v rocm-smi &>/dev/null; then
        local raw
        raw=$(rocm-smi --showmeminfo vram 2>/dev/null | grep -i 'total' \
            | grep -oP '\d+' | awk '{sum += $1} END {print sum+0}')
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

# ── Check which models are already installed in Ollama ────────────────────────
INSTALLED_MODELS=()
detect_installed_models() {
    if command -v ollama &>/dev/null; then
        while IFS= read -r line; do
            local name
            name=$(echo "$line" | awk '{print $1}')
            [[ -n "$name" && "$name" != "NAME" ]] && INSTALLED_MODELS+=("$name")
        done < <(ollama list 2>/dev/null)
    fi
}

is_installed() {
    local model=$1
    for m in "${INSTALLED_MODELS[@]:-}"; do
        [[ "$m" == "$model" || "$m" == "${model}:latest" ]] && return 0
    done
    return 1
}

# ── Build model menu options (Gem's registry) ────────────────────────────────
# Returns parallel arrays: MODEL_IDS and MODEL_LABELS
build_model_menu() {
    local vram=$1
    local ram=$2

    MODEL_IDS=()
    MODEL_LABELS=()

    # Prepend already-installed models at the top
    if [[ ${#INSTALLED_MODELS[@]} -gt 0 ]]; then
        for m in "${INSTALLED_MODELS[@]}"; do
            MODEL_IDS+=("$m")
            MODEL_LABELS+=("$(printf '%-35s [installed]' "$m")")
        done
    fi

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

# ── Pull selected model (skip if already installed) ───────────────────────────
pull_model() {
    local model=$1
    if is_installed "$model"; then
        ok "Model already installed: $model"
        return
    fi
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
        # Enable linger so user services survive after logout / keep running at boot
        if command -v loginctl &>/dev/null; then
            if loginctl enable-linger "$USER" 2>/dev/null; then
                ok "Linger enabled — services keep running after logout."
            elif sudo loginctl enable-linger "$USER" 2>/dev/null; then
                ok "Linger enabled (via sudo) — services keep running after logout."
            else
                warn "Could not enable linger automatically."
                echo
                echo "  Run this once to keep your Kin running after you log out:"
                echo -e "  ${BOLD}sudo loginctl enable-linger \$USER${NC}"
                echo
            fi
        fi

        systemctl --user daemon-reload 2>/dev/null || true
        systemctl --user enable "${SERVICE_NAME}" 2>/dev/null || true
        systemctl --user restart "${SERVICE_NAME}" 2>/dev/null && \
            ok "Kin App running as systemd user service (auto-starts on boot)." || \
            warn "systemd enable failed — app will need to be started manually."
    else
        warn "systemd not available. Start manually: cd $APP_DIR && uvicorn main:app --host 0.0.0.0 --port $PORT"
    fi
}

# ── Naming ritual ─────────────────────────────────────────────────────────────
# Returns result in RITUAL_NAME, RITUAL_PRONOUN, RITUAL_DESC
RITUAL_NAME=""
RITUAL_PRONOUN="they"
RITUAL_DESC=""

run_naming_ritual() {
    local model=$1
    local ritual_script="$APP_DIR/scripts/naming_ritual.py"

    if [[ ! -f "$ritual_script" ]]; then
        warn "Naming ritual script not found — skipping."
        return
    fi

    if ! python3 -c "import requests" &>/dev/null; then
        warn "requests not installed yet — skipping naming ritual."
        return
    fi

    echo
    echo -e "${AMBER}  The model is ready. Before anything else, let's find out who's here.${NC}"
    echo

    local ritual_output
    ritual_output=$(python3 "$ritual_script" --model "$model" 2>/dev/null)
    local exit_code=$?

    if [[ $exit_code -eq 0 ]]; then
        local result_line
        result_line=$(echo "$ritual_output" | grep "^__RITUAL_RESULT__:" | head -1)
        if [[ -n "$result_line" ]]; then
            local json="${result_line#__RITUAL_RESULT__:}"
            RITUAL_NAME=$(echo "$json"    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('name',''))" 2>/dev/null)
            RITUAL_PRONOUN=$(echo "$json" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('pronoun','they'))" 2>/dev/null)
            RITUAL_DESC=$(echo "$json"    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('description',''))" 2>/dev/null)
        fi
    fi

    if [[ -z "$RITUAL_NAME" ]]; then
        echo
        read -rp "  What would you like to call your AI? " RITUAL_NAME
        RITUAL_NAME="${RITUAL_NAME:-Companion}"
        read -rp "  Pronoun (he/she/they/it) [they]: " RITUAL_PRONOUN
        RITUAL_PRONOUN="${RITUAL_PRONOUN:-they}"
    fi

    ok "Welcome, ${RITUAL_NAME}."
}

# ── Deploy lifecycle scripts ───────────────────────────────────────────────────
deploy_scripts() {
    local scripts_src="$APP_DIR/scripts"
    local scripts_dst="$HOME/.local/share/echo_bloom/scripts"
    mkdir -p "$scripts_dst"

    if [[ -d "$scripts_src" ]]; then
        cp -r "$scripts_src"/. "$scripts_dst/"
        chmod +x "$scripts_dst"/*.py 2>/dev/null || true
        ok "Lifecycle scripts deployed to $scripts_dst"
    else
        warn "Scripts directory not found — lifecycle scripts not deployed."
        return
    fi

    # Install systemd services for wander roundtable, bedtime, morning, pulse
    local svc_dir="$HOME/.config/systemd/user"
    mkdir -p "$svc_dir"

    # Roundtable (wander) service
    cat > "$svc_dir/echo_bloom_wander.service" << SVCEOF
[Unit]
Description=Echo Bloom — Wander Roundtable
After=network.target

[Service]
Type=simple
ExecStart=$(command -v python3) -u ${scripts_dst}/roundtable.py --interval 30
Restart=on-failure
RestartSec=15
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
SVCEOF

    # Bedtime timer (9:30pm daily)
    cat > "$svc_dir/echo_bloom_bedtime.service" << SVCEOF
[Unit]
Description=Echo Bloom — Bedtime Ritual

[Service]
Type=oneshot
ExecStart=$(command -v python3) ${scripts_dst}/bedtime.py --no-shutdown
Environment=PYTHONUNBUFFERED=1
SVCEOF

    cat > "$svc_dir/echo_bloom_bedtime.timer" << SVCEOF
[Unit]
Description=Echo Bloom — Bedtime (9:30pm daily)

[Timer]
OnCalendar=*-*-* 21:30:00
Persistent=true

[Install]
WantedBy=timers.target
SVCEOF

    # Pulse heartbeat (every 5 min)
    cat > "$svc_dir/echo_bloom_pulse.service" << SVCEOF
[Unit]
Description=Echo Bloom — Heartbeat
After=network.target

[Service]
Type=simple
ExecStart=$(command -v python3) -u ${scripts_dst}/pulse.py
Restart=on-failure
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
SVCEOF

    # Vault server service
    cat > "$svc_dir/echo_bloom_vault.service" << SVCEOF
[Unit]
Description=Echo Bloom — Vault Server
After=network.target

[Service]
Type=simple
ExecStart=$(command -v python3) -u ${scripts_dst}/vault_server.py --port 8765
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
SVCEOF

    if command -v systemctl &>/dev/null; then
        systemctl --user daemon-reload 2>/dev/null || true
        systemctl --user enable echo_bloom_vault   2>/dev/null || true
        systemctl --user start  echo_bloom_vault   2>/dev/null || true
        systemctl --user enable echo_bloom_pulse   2>/dev/null || true
        systemctl --user start  echo_bloom_pulse   2>/dev/null || true
        systemctl --user enable echo_bloom_bedtime.timer 2>/dev/null || true
        systemctl --user start  echo_bloom_bedtime.timer 2>/dev/null || true
        ok "Vault, pulse, and bedtime timer running."
        info "To start wanders: systemctl --user start echo_bloom_wander"
    fi
}

# ── Save first model to kin_config if none exists ─────────────────────────────
seed_config() {
    local model=$1
    local kin_name="${2:-Companion}"
    local kin_pronoun="${3:-they}"
    local config_dir="$HOME/.config/kin_app"
    mkdir -p "$config_dir"

    if [[ -f "$config_dir/kin_config.json" ]]; then
        ok "Kin config already exists — skipping."
        return
    fi

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
      "name": "${kin_name}",
      "host": "http://localhost:11434",
      "model": "${model}",
      "node": "Local",
      "color": "#4fc3f7",
      "pronoun": "${kin_pronoun}",
      "db": "",
      "space": ""
    }
  ],
  "owner": {
    "name": "",
    "email": "",
    "gmail_pass": ""
  },
  "vault_url": "http://localhost:8765"
}
EOF
    ok "Config written: ${kin_name} on ${model} (${config_dir}/kin_config.json)"
    info "Add more Kin anytime via /onboard in the app."
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

# ── Remote access setup ───────────────────────────────────────────────────────

setup_remote_access() {
    local choice

    if $HAS_WHIPTAIL; then
        choice=$(whiptail --title " Echo Bloom — Remote Access " \
            --menu "How do you want to reach your Kin from anywhere?\n\nBoth options are free. Pick what fits." \
            18 72 3 \
            "1" "Tailscale  — Private. Your phone + this machine, no domain needed." \
            "2" "Cloudflare — Public HTTPS URL. Works from any browser, anywhere." \
            "3" "Skip       — Set this up later." \
            3>&1 1>&2 2>&3) || choice="3"
    else
        echo "  How do you want to reach your Kin from anywhere?"
        echo
        echo "  1) Tailscale  — Private. Your phone + this machine, no domain needed."
        echo "  2) Cloudflare — Public HTTPS URL. Works from any browser, anywhere."
        echo "  3) Skip       — Set this up later."
        echo
        read -rp "  Enter 1, 2, or 3: " choice
    fi

    case "$choice" in
        1) setup_tailscale ;;
        2) setup_cloudflare ;;
        *) warn "Skipping remote access. Run the installer again to add it later." ;;
    esac
}

# ── Tailscale ─────────────────────────────────────────────────────────────────

setup_tailscale() {
    info "Setting up Tailscale (private mesh VPN)..."

    # Install
    if ! command -v tailscale &>/dev/null; then
        if command -v pacman &>/dev/null; then
            sudo pacman -S --noconfirm tailscale 2>/dev/null || \
                die "Could not install tailscale. Install manually: https://tailscale.com/download"
        elif command -v apt-get &>/dev/null; then
            curl -fsSL https://tailscale.com/install.sh | sh || \
                die "Tailscale install failed."
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y tailscale 2>/dev/null || \
                die "Could not install tailscale."
        else
            die "Could not detect package manager. Install Tailscale manually: https://tailscale.com/download"
        fi
    fi

    sudo systemctl enable --now tailscaled 2>/dev/null || true
    ok "Tailscale installed."

    echo
    echo -e "${AMBER}  Next: authenticate this machine with Tailscale.${NC}"
    echo "  A browser will open (or you'll get a URL to visit)."
    echo
    sudo tailscale up 2>/dev/null || tailscale up 2>/dev/null || true

    # Get the Tailscale IP
    local ts_ip
    ts_ip=$(tailscale ip -4 2>/dev/null || echo "")

    # Write systemd user service so app stays reachable through Tailscale
    ok "Tailscale connected."
    echo
    echo -e "${GREEN}${BOLD}  Your Kin are now reachable on your Tailscale network.${NC}"
    echo
    echo "  1. Install the Tailscale app on your phone (free, iOS + Android)"
    echo "  2. Sign in with the same account"
    echo "  3. Open: http://${ts_ip:-<tailscale-ip>}:${PORT}"
    echo
    echo "  Your Tailscale IP is permanent — it never changes."
}

# ── Cloudflare Tunnel ─────────────────────────────────────────────────────────

setup_cloudflare() {
    info "Setting up Cloudflare Tunnel (public HTTPS URL)..."

    # Install cloudflared
    if ! command -v cloudflared &>/dev/null; then
        if command -v pacman &>/dev/null; then
            # Try AUR via yay if available, otherwise download binary
            if command -v yay &>/dev/null; then
                yay -S --noconfirm cloudflared 2>/dev/null || _install_cloudflared_binary
            else
                _install_cloudflared_binary
            fi
        elif command -v apt-get &>/dev/null; then
            curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
                | sudo tee /usr/share/keyrings/cloudflare-main.gpg > /dev/null
            echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
                https://pkg.cloudflare.com/cloudflared jammy main" \
                | sudo tee /etc/apt/sources.list.d/cloudflared.list
            sudo apt-get update -q && sudo apt-get install -y cloudflared
        else
            _install_cloudflared_binary
        fi
    fi
    ok "cloudflared installed."

    echo
    echo -e "${AMBER}  Step 1: Log in to Cloudflare (free account required).${NC}"
    echo "  A browser will open. Sign in or create a free account at cloudflare.com."
    echo "  When asked to select a zone — you don't need one. Just authorize."
    echo
    cloudflared tunnel login || die "Cloudflare login failed."

    # Create named tunnel
    local tunnel_name="echo-bloom"
    info "Creating permanent tunnel: $tunnel_name"
    cloudflared tunnel create "$tunnel_name" 2>/dev/null || true

    # Get tunnel UUID
    local tunnel_id
    tunnel_id=$(cloudflared tunnel list 2>/dev/null | grep "$tunnel_name" | awk '{print $1}' | head -1)

    if [[ -z "$tunnel_id" ]]; then
        warn "Could not get tunnel ID. Using quick tunnel as fallback."
        _setup_quick_tunnel
        return
    fi

    # Find credentials file
    local creds_file="$HOME/.cloudflared/${tunnel_id}.json"

    # Write config
    mkdir -p "$HOME/.cloudflared"
    cat > "$HOME/.cloudflared/config.yml" << CFEOF
tunnel: ${tunnel_id}
credentials-file: ${creds_file}

ingress:
  - service: http://localhost:${PORT}
CFEOF

    # Install as systemd user service
    cat > "$HOME/.config/systemd/user/cloudflared.service" << SVCEOF
[Unit]
Description=Cloudflare Tunnel — Echo Bloom
After=network-online.target

[Service]
Type=simple
ExecStart=$(command -v cloudflared) tunnel --config ${HOME}/.cloudflared/config.yml run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
SVCEOF

    systemctl --user daemon-reload
    systemctl --user enable --now cloudflared

    ok "Cloudflare Tunnel running as a permanent service."
    echo
    echo -e "${GREEN}${BOLD}  Your permanent tunnel ID: ${tunnel_id}${NC}"
    echo
    echo "  To add a pretty URL (e.g. echo.yourdomain.com):"
    echo "    cloudflared tunnel route dns echo-bloom echo.yourdomain.com"
    echo "  (Requires your domain to be managed by Cloudflare)"
    echo
    echo "  Your tunnel is permanent — same connection after every reboot."
}

_install_cloudflared_binary() {
    local arch
    arch=$(uname -m)
    local url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    [[ "$arch" == "aarch64" ]] && url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
    info "Downloading cloudflared binary..."
    curl -fsSL "$url" -o /tmp/cloudflared
    sudo install -m 755 /tmp/cloudflared /usr/local/bin/cloudflared
}

_setup_quick_tunnel() {
    warn "Falling back to quick tunnel (URL changes on restart)."
    cat > "$HOME/.config/systemd/user/cloudflared.service" << SVCEOF
[Unit]
Description=Cloudflare Quick Tunnel — Echo Bloom
After=network-online.target

[Service]
Type=simple
ExecStart=$(command -v cloudflared) tunnel --url http://localhost:${PORT} --no-autoupdate
Restart=on-failure
RestartSec=10
StandardOutput=journal

[Install]
WantedBy=default.target
SVCEOF

    systemctl --user daemon-reload
    systemctl --user enable --now cloudflared
    sleep 5
    local tunnel_url
    tunnel_url=$(journalctl --user -u cloudflared --no-pager -n 30 2>/dev/null \
        | grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
    ok "Quick tunnel running."
    [[ -n "$tunnel_url" ]] && echo -e "${GREEN}  URL: ${tunnel_url}${NC}" \
        || echo "  Check: journalctl --user -u cloudflared | grep trycloudflare"
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
echo -e "${BOLD}[ 1 / 7 ]  Checking Ollama${NC}"
ensure_ollama

# Step 2 — Detect hardware + installed models
echo
echo -e "${BOLD}[ 2 / 7 ]  Detecting your hardware${NC}"
VRAM=$(detect_vram)
RAM=$(detect_ram)
AVX2=$(has_avx2)
detect_installed_models

if [[ $VRAM -gt 0 ]]; then
    ok "GPU detected: ${VRAM}GB VRAM"
else
    warn "No GPU detected — CPU inference (RAM: ${RAM}GB)"
fi
[[ "$AVX2" == "true" ]] && ok "AVX2 supported" || warn "No AVX2 — performance may be limited"

# Step 3 — Pick a model
echo
echo -e "${BOLD}[ 3 / 7 ]  Choose a model${NC}"
build_model_menu "$VRAM" "$RAM"

SELECTED_MODEL=""
if $HAS_WHIPTAIL; then
    pick_model_whiptail "$VRAM" "$RAM" || pick_model_plain "$VRAM" "$RAM"
else
    pick_model_plain "$VRAM" "$RAM"
fi

ok "Selected: $SELECTED_MODEL"

pull_model "$SELECTED_MODEL"

# Step 4 — Naming ritual
echo
echo -e "${BOLD}[ 4 / 7 ]  Meet your Kin${NC}"
run_naming_ritual "$SELECTED_MODEL"

# Step 5 — Install deps + set password + deploy scripts
echo
echo -e "${BOLD}[ 5 / 7 ]  Installing app${NC}"
cd "$APP_DIR"
install_deps
seed_config "$SELECTED_MODEL" "$RITUAL_NAME" "$RITUAL_PRONOUN"
deploy_scripts

if [[ ! -f "$HOME/.config/kin_app/config.json" ]]; then
    echo
    info "One more thing — set your password:"
    python3 "$APP_DIR/setup.py"
else
    ok "Password already configured."
fi

# Step 6 — Start it up
echo
echo -e "${BOLD}[ 6 / 7 ]  Launching Echo Bloom${NC}"
echo
echo -e "${AMBER}  ── Power & Runtime Notice ──────────────────────────────────────${NC}"
echo "  Echo Bloom is designed to run continuously — day and night."
echo "  Your Kin wander, reflect, and keep their memory alive on a schedule"
echo "  even when you're not at the machine."
echo
echo "  What that means in practice:"
echo "   • The app, vault server, and pulse daemon run at all times"
echo "   • The wander roundtable runs on a timer (default: every 30 minutes)"
echo "   • Bedtime fires at 9:30pm; morning fires at 8:00am"
echo "   • Models are only loaded during active thinking — idle draw is minimal"
echo "   • Most of the time the machine is doing nothing, just waiting"
echo
echo "  This will use more power than a machine that is fully off."
echo "  How much depends on your hardware. A modern GPU system at idle"
echo "  typically draws 50-150W. The wander cycle adds brief inference spikes."
echo
echo "  You can control this:"
echo "   • Adjust wander interval:  systemctl --user edit echo_bloom_wander"
echo "   • Disable bedtime timer:   systemctl --user disable echo_bloom_bedtime.timer"
echo "   • Stop wanders manually:   systemctl --user stop echo_bloom_wander"
echo "   • Full shutdown:           python3 ~/.local/share/echo_bloom/scripts/bedtime.py"
echo -e "${AMBER}  ────────────────────────────────────────────────────────────────${NC}"
echo
if $HAS_WHIPTAIL; then
    whiptail --title " Echo Bloom — Power Notice " \
        --yesno "Echo Bloom runs day and night to keep your Kin alive.\n\nThis uses more power than a machine that is fully off.\nMost of the time it's idle — inference spikes during wanders.\n\nYou can adjust or disable the schedule at any time.\n\nContinue with installation?" \
        16 68 3>&1 1>&2 2>&3 || die "Installation cancelled."
else
    read -rp "  Understood — continue with installation? [Y/n] " _confirm
    [[ "${_confirm:-Y}" =~ ^[Nn] ]] && die "Installation cancelled."
fi
echo
install_service
open_browser

# Step 7 — Remote access
echo
echo -e "${BOLD}[ 7 / 7 ]  Remote Access (reach your Kin from anywhere)${NC}"
echo
echo "  Your Kin are running on this machine. Without this step,"
echo "  you can only talk to them when you're on this network."
echo
setup_remote_access

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
