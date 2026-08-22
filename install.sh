#!/usr/bin/env bash
# Echo Bloom installer
# Usage: curl -sSL <url>/install.sh -o install.sh && bash install.sh

set -euo pipefail

# Are we on a real terminal? This MUST be answered before the tee redirect
# below, because after it fd 1 is a pipe and [[ -t 1 ]] is false forever. The
# whiptail detection used to run after the redirect, so it was always false and
# every whiptail branch in this file was dead code on every machine ever.
if [ -t 1 ] && [ -w /dev/tty ]; then REAL_TTY=true; else REAL_TTY=false; fi

# Log everything to /tmp so crashes leave a trail
INSTALL_LOG="/tmp/echo_bloom_install.log"
exec > >(tee -a "$INSTALL_LOG") 2>&1
echo "=== Echo Bloom install $(date) ==="

# Exit trap — record the line number if we bail
trap 'echo "INSTALL EXIT: line $LINENO, exit $?" >> "$INSTALL_LOG"' EXIT

# Installer needs an interactive terminal for model selection and password setup.
if [ ! -t 0 ]; then
    echo ""
    echo "  Echo Bloom installer requires an interactive terminal."
    echo "  Use process substitution so stdin stays attached to your terminal:"
    echo ""
    echo "    bash <(curl -fsSL https://raw.githubusercontent.com/EverySynthetic/echo-bloom/main/install.sh)"
    echo ""
    exit 1
fi

# macOS: no systemd, no loginctl. Service management goes through launchd
# instead (see install_service() and deploy_scripts()). Hardware detection
# that reads /proc/meminfo needs a sysctl-based equivalent — that's a
# separate, not-yet-done piece; IS_MACOS lets that code branch cleanly
# without every caller needing to know the platform.
IS_MACOS=false
if [[ "$(uname -s)" == "Darwin" ]]; then
    IS_MACOS=true
    echo ""
    echo "  macOS support is new — you're an early tester, not a guinea pig"
    echo "  we're hiding that from. If something looks wrong, the log is at"
    echo "  $INSTALL_LOG and we want to hear about it."
    echo ""
fi

APP_DIR="$HOME/echo_bloom"
SERVICE_NAME="echo_bloom"
PORT=8090

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
AMBER='\033[0;33m'
CYAN='\033[0;36m'
DIM='\033[2m'
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

# ── Transient terminal output ────────────────────────────────────────────────
# Progress bars and spinners are \r animations. Through the tee pipe they
# render as a freeze-then-dump and fill the install log with hundreds of block
# characters, so they go straight to the terminal or nowhere at all.
tty_out() {
    $REAL_TTY || return 0
    # shellcheck disable=SC2059
    printf "$@" > /dev/tty
}

# ── Check for whiptail, fall back to plain numbered list ─────────────────────
# TERM=dumb is what non-interactive shells and some CI/SSH setups report;
# whiptail on it renders unusable garbage.
HAS_WHIPTAIL=false
command -v whiptail &>/dev/null && [[ -n "${TERM:-}" ]] && [[ "${TERM}" != "dumb" ]] \
    && $REAL_TTY && HAS_WHIPTAIL=true

# ── Detect VRAM ───────────────────────────────────────────────────────────────
# Echoes three numbers: "<total GB> <card count> <largest single card GB>".
#
# The model tiers are gated on the TOTAL, because Ollama really does split a
# model across cards. But the total on its own is a half-truth: 16GB + 11GB
# unlocks a tier no single card in the machine can hold, and the customer was
# never told the model would be split — so a two-card box could pick something
# that runs, just much slower than the number implied. Report all three and
# say plainly when a split is coming.
#
# Runs in a subshell via $(...), so these have to come back on stdout — set as
# globals from in here they would be discarded.
detect_vram() {
    local vram=0 count=0 largest=0
    local total_mb=0
    if [[ "$IS_MACOS" == "true" ]]; then
        # macOS: system_profiler for Apple Silicon, AMD, Intel GPUs.
        # Unified memory is already counted in RAM; we report the largest
        # GPU memory as VRAM for model selection (realistic default).
        local sp=""
        sp=$(system_profiler SPDisplaysDataType 2>/dev/null || true)
        if [[ -n "$sp" ]]; then
            local mb=0
            # Parse common patterns: "16 GB", "8192 MB", "Unified Memory: 24 GB", "VRAM (Total): 32 GB"
            mb=$(echo "$sp" | grep -oE '[0-9]+' | head -1 || echo 0)
            [[ "$mb" -gt 0 ]] && vram=$(( mb > 1024 ? mb / 1024 : mb ))
            count=1
            largest=$vram
        fi
    elif command -v nvidia-smi &>/dev/null; then
        local per_card=""
        per_card=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
            | tr -d ' ' | grep -E '^[0-9]+$') || per_card=""
        if [[ -n "$per_card" ]]; then
            total_mb=$(echo "$per_card" | awk '{sum += $1} END {print sum+0}')
            count=$(echo "$per_card" | wc -l | tr -d ' ')
            largest=$(( $(echo "$per_card" | sort -rn | head -1) / 1024 ))
        fi
        if [[ "$total_mb" =~ ^[0-9]+$ ]] && [[ "$total_mb" -gt 0 ]]; then
            vram=$(( total_mb / 1024 ))
        fi
    elif command -v rocm-smi &>/dev/null; then
        local raw=0
        raw=$(rocm-smi --showmeminfo vram 2>/dev/null | grep -i 'total' \
            | grep -oP '\d+' | awk '{sum += $1} END {print sum+0}') || raw=0
        [[ "$raw" =~ ^[0-9]+$ ]] && vram=$(( raw / 1024 / 1024 )) || vram=0
        # rocm-smi's per-card breakdown isn't parsed here; treat it as one pool.
        [[ $vram -gt 0 ]] && { count=1; largest=$vram; }
    fi
    echo "$vram $count $largest"
}

# ── Detect RAM (in GB) ────────────────────────────────────────────────────────
detect_ram() {
    local ram_kb=0
    if [[ "$IS_MACOS" == "true" ]]; then
        # macOS: sysctl hw.memsize (bytes) → GB. Unified memory on Apple Silicon
        # is the main RAM pool.
        local bytes=0
        bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
        [[ "$bytes" =~ ^[0-9]+$ ]] && ram_kb=$(( bytes / 1024 )) || ram_kb=0
    else
        ram_kb=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}') || ram_kb=0
        [[ "$ram_kb" =~ ^[0-9]+$ ]] || ram_kb=0
    fi
    echo $(( ram_kb / 1024 / 1024 ))
}

# ── Detect AVX2 ───────────────────────────────────────────────────────────────
has_avx2() {
    if [[ "$IS_MACOS" == "true" ]]; then
        # macOS: AVX2 is x86-only. Apple Silicon uses different vector extensions;
        # the sysctl check returns false correctly on ARM.
        sysctl -n machdep.cpu.features 2>/dev/null | grep -q AVX2 && echo true || echo false
    else
        grep -q avx2 /proc/cpuinfo && echo true || echo false
    fi
}

# ── Check which models are already installed in Ollama ────────────────────────
INSTALLED_MODELS=()
detect_installed_models() {
    if command -v ollama &>/dev/null; then
        while IFS= read -r line; do
            local name
            name=$(echo "$line" | awk '{print $1}')
            if [[ -n "$name" && "$name" != "NAME" ]]; then
                INSTALLED_MODELS+=("$name")
            fi
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

# ── Helper: add model only if not already listed as [installed] at top ────────
_add_model() {
    is_installed "$1" && return 0
    MODEL_IDS+=("$1")
    MODEL_LABELS+=("$2")
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
    _add_model "llama3.2:3b"        "llama3.2:3b        [2.0 GB]  Meta — lightweight, fast conversational"
    _add_model "qwen2.5-coder:1.5b" "qwen2.5-coder:1.5b [1.2 GB]  Coding specialist, fast autocomplete"
    _add_model "phi4-mini"          "phi4-mini          [2.3 GB]  Microsoft 3.8B — high reasoning density"

    # Tier 2 — 8+ GB VRAM or 16+ GB RAM (CPU offload)
    # 6-7GB cards: context spills to RAM on 8B+ models — use tier 1 or the door below
    if [[ $vram -ge 8 ]] || [[ $ram -ge 16 && $vram -eq 0 ]]; then
        _add_model "llama3.1:8b"      "llama3.1:8b        [5.0 GB]  Reliable open-weights standard"
        _add_model "qwen2.5-coder:7b" "qwen2.5-coder:7b   [5.0 GB]  King of 8GB coding models"
        _add_model "deepseek-r1:8b"   "deepseek-r1:8b     [5.0 GB]  Step-by-step reasoning specialist"
        _add_model "gemma2:9b"        "gemma2:9b          [5.5 GB]  Strong prose, deep context"
    fi

    # Tier 3 — 12+ GB VRAM
    if [[ $vram -ge 12 ]]; then
        _add_model "llama3.1:8b-q8_0" "llama3.1:8b-q8_0   [8.5 GB]  Max quality 8B — full precision"
        _add_model "deepseek-r1:14b"  "deepseek-r1:14b    [9.0 GB]  Heavy analytical reasoning"
        _add_model "qwen2.5:14b"      "qwen2.5:14b        [9.0 GB]  Strong multilingual + structured output"
    fi

    if [[ $vram -ge 16 ]]; then
        _add_model "mistral-small:24b" "mistral-small:24b  [15.0 GB] Agentic logic, concise writing"
    fi

    # Tier 4 — 20+ GB VRAM
    if [[ $vram -ge 20 ]]; then
        _add_model "mixtral:8x7b"      "mixtral:8x7b       [16.0 GB] Classic MoE, 47B effective params"
        _add_model "deepseek-r1:32b"   "deepseek-r1:32b    [20.0 GB] Top-tier open reasoning"
        _add_model "qwen2.5-coder:32b" "qwen2.5-coder:32b  [20.0 GB] Frontier local coding"
    fi

    if [[ $vram -ge 48 ]]; then
        _add_model "llama3.3:70b" "llama3.3:70b       [40.0 GB] Ultimate local dense — needs 48GB+ VRAM"
    fi
}

# ── Build overflow list: models above the detected VRAM threshold ─────────────
# Populates OVERFLOW_IDS and OVERFLOW_LABELS.
# Skips anything already in MODEL_IDS (already shown in the safe list).
OVERFLOW_IDS=()
OVERFLOW_LABELS=()

build_overflow_model_menu() {
    local vram=$1
    OVERFLOW_IDS=()
    OVERFLOW_LABELS=()

    _in_safe_list() {
        local id=$1
        for m in "${MODEL_IDS[@]:-}"; do [[ "$m" == "$id" ]] && return 0; done
        return 1
    }

    _add_overflow() {
        local id=$1 needs=$2 desc=$3
        _in_safe_list "$id" && return 0
        OVERFLOW_IDS+=("$id")
        OVERFLOW_LABELS+=("$(printf '%-22s needs %-5s · you have %dGB   %s' "$id" "$needs" "$vram" "$desc")")
    }

    _add_overflow "llama3.1:8b"       "8GB"  "⚠ context will spill to RAM on <8GB"
    _add_overflow "qwen2.5-coder:7b"  "8GB"  "⚠ context will spill to RAM on <8GB"
    _add_overflow "deepseek-r1:8b"    "8GB"  "⚠ context will spill to RAM on <8GB"
    _add_overflow "gemma2:9b"         "8GB"  "⚠ may crash or refuse to load on <8GB"
    _add_overflow "llama3.1:8b-q8_0"  "12GB" "⚠ very slow or OOM on <12GB"
    _add_overflow "deepseek-r1:14b"   "12GB" "⚠ very slow or OOM on <12GB"
    _add_overflow "qwen2.5:14b"       "12GB" "⚠ very slow or OOM on <12GB"
    _add_overflow "mistral-small:24b" "16GB" "⚠ will not fit, OOM likely"
    _add_overflow "mixtral:8x7b"      "20GB" "⚠ will not fit, OOM likely"
    _add_overflow "deepseek-r1:32b"   "20GB" "⚠ will not fit, OOM likely"
    _add_overflow "qwen2.5-coder:32b" "20GB" "⚠ will not fit, OOM likely"
    _add_overflow "llama3.3:70b"      "48GB" "⚠ will not fit, OOM likely"
}

# ── Pick model via whiptail ───────────────────────────────────────────────────
pick_model_whiptail() {
    local vram=$1
    local ram=$2
    local title=" Echo Bloom — Model Selection "
    local in_overflow=false

    build_overflow_model_menu "$vram"

    while true; do
        local menu_args=() i=1 cur_ids cur_labels msg

        if $in_overflow; then
            cur_ids=("${OVERFLOW_IDS[@]}")
            cur_labels=("${OVERFLOW_LABELS[@]}")
            msg="$(printf '⚠  These models exceed your detected VRAM (%dGB).\nThey may run slowly, crash, or fail to load. You were warned.' "$vram")"
        else
            cur_ids=("${MODEL_IDS[@]}")
            cur_labels=("${MODEL_LABELS[@]}")
            if [[ ${GPU_COUNT:-1} -gt 1 ]]; then
                msg="$(printf 'Detected: %dGB VRAM across %d cards (largest %dGB) · %dGB RAM\nAnything over %dGB is split across cards — works, but slower.\n\nChoose the model your AI will think with:' \
                    "$vram" "$GPU_COUNT" "$GPU_LARGEST" "$ram" "$GPU_LARGEST")"
            else
                msg="$(printf 'Detected: %dGB VRAM · %dGB RAM\nChoose the model your AI will think with:' "$vram" "$ram")"
            fi
        fi

        for label in "${cur_labels[@]}"; do
            menu_args+=("$i" "$label")
            ((i++))
        done

        # Show the door only when on the safe list and there's something behind it
        if ! $in_overflow && [[ ${#OVERFLOW_IDS[@]} -gt 0 ]]; then
            menu_args+=("X" "⚠  I wouldn't do this — show models beyond my VRAM")
        fi

        # whiptail draws its UI on stdout and returns the selection on stderr,
        # hence the fd shuffle. stdout must go to /dev/tty, not to fd 2 — the
        # tee redirect at the top of this file makes BOTH 1 and 2 a pipe, and
        # cursor addressing through a pipe is unreadable garbage.
        local choice
        choice=$(whiptail --title "$title" \
            --menu "$msg" \
            22 82 10 \
            "${menu_args[@]}" \
            3>&1 1>/dev/tty 2>&3) || return 1

        if [[ "$choice" == "X" ]]; then
            in_overflow=true
            continue
        fi

        if $in_overflow; then
            SELECTED_MODEL="${OVERFLOW_IDS[$((choice - 1))]}"
        else
            SELECTED_MODEL="${MODEL_IDS[$((choice - 1))]}"
        fi
        break
    done
}

# ── Pick model via plain numbered list (no whiptail) ─────────────────────────
pick_model_plain() {
    local vram=$1
    local ram=$2
    local in_overflow=false

    build_overflow_model_menu "$vram"

    while true; do
        local cur_ids cur_labels
        echo

        if $in_overflow; then
            cur_ids=("${OVERFLOW_IDS[@]}")
            cur_labels=("${OVERFLOW_LABELS[@]}")
            echo -e "${RED}${BOLD}⚠  WARNING: These models exceed your detected VRAM (${vram}GB).${NC}"
            echo -e "${RED}   They may run slowly, crash, or fail to load entirely.${NC}"
            echo -e "${RED}   You were warned. No refunds on your time.${NC}"
        else
            cur_ids=("${MODEL_IDS[@]}")
            cur_labels=("${MODEL_LABELS[@]}")
            echo -e "${BOLD}Available models for your hardware (${vram}GB VRAM · ${ram}GB RAM):${NC}"
            if [[ ${GPU_COUNT:-1} -gt 1 ]]; then
                echo -e "${DIM}  ${GPU_COUNT} cards, largest is ${GPU_LARGEST}GB — anything over that is split across them.${NC}"
            fi
        fi

        echo
        local i=1
        for label in "${cur_labels[@]}"; do
            printf "  %2d)  %s\n" "$i" "$label"
            ((i++))
        done

        if ! $in_overflow && [[ ${#OVERFLOW_IDS[@]} -gt 0 ]]; then
            echo
            echo -e "   X)  ${AMBER}⚠  I wouldn't do this — show models beyond my VRAM${NC}"
        fi

        echo
        while true; do
            read -rp "  Enter number (or X): " choice
            if [[ "${choice,,}" == "x" ]] && ! $in_overflow && [[ ${#OVERFLOW_IDS[@]} -gt 0 ]]; then
                in_overflow=true
                break
            fi
            if [[ "$choice" =~ ^[0-9]+$ ]] && \
               [[ "$choice" -ge 1 ]] && \
               [[ "$choice" -le "${#cur_ids[@]}" ]]; then
                if $in_overflow; then
                    SELECTED_MODEL="${OVERFLOW_IDS[$((choice - 1))]}"
                else
                    SELECTED_MODEL="${MODEL_IDS[$((choice - 1))]}"
                fi
                return 0
            fi
            warn "Enter a number between 1 and ${#cur_ids[@]}${OVERFLOW_IDS:+, or X}"
        done
    done
}

# ── Preflight: check all deps, ask once, install everything ───────────────────
preflight() {
    echo
    echo -e "${BOLD}Checking your system...${NC}"
    echo

    # Detect package manager
    PKG_MGR=""
    if command -v pacman  &>/dev/null; then PKG_MGR="pacman"
    elif command -v apt-get &>/dev/null; then PKG_MGR="apt"
    elif command -v dnf    &>/dev/null; then PKG_MGR="dnf"
    elif command -v brew   &>/dev/null; then PKG_MGR="brew"
    fi

    # Check each requirement
    local need_python=false need_pip=false need_git=false need_curl=false need_ollama=false
    local missing_labels=()

    if command -v python3 &>/dev/null; then
        ok "Python 3      — found ($(python3 --version 2>&1 | awk '{print $2}'))"
    else
        warn "Python 3      — not found"
        need_python=true; missing_labels+=("python3")
    fi

    if command -v pip3 &>/dev/null || python3 -m pip --version &>/dev/null 2>&1; then
        ok "pip           — found"
    else
        warn "pip           — not found"
        need_pip=true; missing_labels+=("pip")
    fi

    if command -v git &>/dev/null; then
        ok "git           — found"
    else
        warn "git           — not found"
        need_git=true; missing_labels+=("git")
    fi

    if command -v curl &>/dev/null; then
        ok "curl          — found"
    else
        warn "curl          — not found"
        need_curl=true; missing_labels+=("curl")
    fi

    if command -v ollama &>/dev/null; then
        ok "Ollama        — found ($(ollama --version 2>/dev/null | head -1 || echo 'installed'))"
    else
        warn "Ollama        — not found"
        need_ollama=true; missing_labels+=("Ollama")
    fi

    # All good
    if [[ ${#missing_labels[@]} -eq 0 ]]; then
        echo
        ok "Everything looks good. Starting installation."
        return 0
    fi

    # Show what's missing and ask once
    echo
    echo -e "${AMBER}  Missing: ${missing_labels[*]}${NC}"
    if $need_ollama; then
        echo
        echo "  Ollama is the engine that runs AI models locally on your computer."
        echo "  It's free, open source, and required for Echo Bloom to work."
    fi
    echo

    # Braces matter: without them this parsed as
    # { [[ -z $PKG_MGR ]] && $need_python; } || $need_pip || ...
    # so a box WITH a package manager but missing pip/curl got told there was
    # no package manager, and hard-exited with the wrong instructions.
    if [[ -z "$PKG_MGR" ]] && { $need_python || $need_pip || $need_git || $need_curl; }; then
        warn "No package manager detected. Can't auto-install system packages."
        echo "  Install manually: ${missing_labels[*]}"
        echo "  Then re-run this installer."
        # python3 is not optional and never was — the naming ritual, the config
        # writer, the lifecycle scripts and the app itself are all Python.
        # Continuing without it only moves the failure somewhere less legible,
        # after Ollama and a multi-gigabyte model have already been installed.
        $need_python && die "python3 is required and can't be installed automatically here."
        $need_ollama || exit 1
    fi

    read -rp "  Install missing dependencies now? [Y/n] " yn
    [[ "${yn:-Y}" =~ ^[Nn] ]] && die "Install the items above and re-run."
    echo

    # Install system packages in one shot
    _install_pkg() {
        local apt_name=$1 pacman_name=$2 dnf_name=$3
        case "$PKG_MGR" in
            pacman) sudo pacman -S --noconfirm "$pacman_name" ;;
            apt)    sudo apt-get install -y    "$apt_name"    ;;
            dnf)    sudo dnf install -y        "$dnf_name"    ;;
            brew)   brew install               "$apt_name"    ;;
        esac
    }

    if $need_python; then
        info "Installing Python 3..."
        _install_pkg python3 python python3 && ok "Python 3 installed."
    fi

    if $need_pip; then
        info "Installing pip..."
        if python3 -m ensurepip --upgrade &>/dev/null 2>&1; then
            ok "pip installed via ensurepip."
        else
            _install_pkg python3-pip python-pip python3-pip && ok "pip installed."
        fi
    fi

    if $need_git; then
        info "Installing git..."
        _install_pkg git git git && ok "git installed."
    fi

    if $need_curl; then
        info "Installing curl..."
        _install_pkg curl curl curl && ok "curl installed."
    fi

    if $need_ollama; then
        info "Installing Ollama (this may take a minute)..."
        curl -fsSL https://ollama.com/install.sh | sh \
            || die "Ollama install failed. Visit https://ollama.com to install manually, then re-run."
        ok "Ollama installed."
    fi

    echo
    ok "All dependencies ready."
}

# ── Make sure Ollama is actually responding before we try to use it ───────────
ensure_ollama_running() {
    # Already up — nothing to do
    curl -s --max-time 2 http://localhost:11434/api/version &>/dev/null && return 0

    info "Starting Ollama..."

    # Launch directly as a user process — no sudo, always works
    ollama serve &>/dev/null &
    disown 2>/dev/null || true

    # Give it up to 15s to respond
    local waited=0
    while ! curl -s --max-time 1 http://localhost:11434/api/version &>/dev/null; do
        sleep 1
        waited=$((waited + 1))
        [[ $waited -ge 15 ]] && break
    done

    # If direct launch didn't work, try the system service (requires sudo — last resort)
    if ! curl -s --max-time 1 http://localhost:11434/api/version &>/dev/null; then
        if command -v systemctl &>/dev/null; then
            systemctl --user start ollama 2>/dev/null || true
            sudo systemctl start ollama 2>/dev/null || true
        fi
        sleep 3
    fi

    if curl -s --max-time 2 http://localhost:11434/api/version &>/dev/null; then
        ok "Ollama is running."
    else
        die "Could not start Ollama. Please run 'ollama serve' in another terminal and re-run the installer."
    fi
}

# ── Warmup: seed ~/.ollama/id_ed25519 if missing, then wait until Ollama
# answers. Used to be a 30-second sleep that always printed 100% whether or
# not anything was listening. The bar now completes when /api/version responds,
# or warns at the timeout if it never did.
_ollama_warmup() {
    local total=30
    local width=40

    local _key_pid=""
    if [[ ! -f "$HOME/.ollama/id_ed25519" ]]; then
        mkdir -p "$HOME/.ollama"
        openssl genpkey -algorithm ed25519 -out "$HOME/.ollama/id_ed25519" 2>/dev/null &
        _key_pid=$!
    fi

    if curl -s --max-time 1 http://localhost:11434/api/version &>/dev/null; then
        [[ -n "$_key_pid" ]] && { wait "$_key_pid" 2>/dev/null || true; }
        ok "Ollama is answering."
        return
    fi

    info "Waiting for Ollama to answer..."
    local i j bar pct ready=false
    for ((i=1; i<=total; i++)); do
        if curl -s --max-time 1 http://localhost:11434/api/version &>/dev/null; then
            ready=true
            i=$total
        fi
        bar=""
        pct=$(( (i * 100) / total ))
        local filled=$(( (i * width) / total ))
        for ((j=0; j<filled; j++)); do bar+="█"; done
        for ((j=filled; j<width; j++)); do bar+="░"; done
        tty_out "\r  [%s] %3d%%" "$bar" "$pct"
        $ready && break
        sleep 1
    done
    tty_out "\r  [%s] %3d%%\n" "$bar" "$pct"
    $REAL_TTY || echo

    [[ -n "$_key_pid" ]] && { wait "$_key_pid" 2>/dev/null || true; }

    if $ready; then
        ok "Ollama is answering."
    else
        warn "Ollama did not answer after ${total}s — the model pull may fail."
        echo "  Start it in another terminal: ollama serve"
    fi
}

# ── Pull selected model (skip if already installed) ───────────────────────────
pull_model() {
    local model=$1
    if is_installed "$model"; then
        ok "Model already installed: $model"
        return
    fi
    # Safety net: generate the user's Ollama identity key if still missing.
    # Ollama installs as system user 'ollama'; the per-user key at
    # ~/.ollama/id_ed25519 doesn't exist until ollama runs as this user.
    if [[ ! -f "$HOME/.ollama/id_ed25519" ]]; then
        mkdir -p "$HOME/.ollama"
        openssl genpkey -algorithm ed25519 -out "$HOME/.ollama/id_ed25519" 2>/dev/null || true
    fi

    info "Pulling $model — this may take a few minutes..."
    ollama pull "$model" || die "Model download failed — re-run this installer and it will resume where it left off."
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
cryptography>=41.0.0
requests>=2.31.0
psutil>=5.9.0
qrcode>=7.4.2
faster-whisper>=1.0.0
piper-tts>=1.2.0
REQEOF
    fi

    # Resolve pip — pip3 by name, then python3 -m pip, then try to get it
    local pip_cmd=""
    if command -v pip3 &>/dev/null; then
        pip_cmd="pip3"
    elif python3 -m pip --version &>/dev/null 2>&1; then
        pip_cmd="python3 -m pip"
    else
        warn "pip not found — trying to bootstrap it..."
        if python3 -m ensurepip --upgrade &>/dev/null 2>&1; then
            ok "pip bootstrapped via ensurepip."
            pip_cmd="python3 -m pip"
        elif command -v pacman &>/dev/null; then
            read -rp "  pip is not installed. Install python-pip now? [Y/n] " _yn
            [[ "${_yn:-Y}" =~ ^[Nn] ]] && die "pip required. Install with: sudo pacman -S python-pip"
            sudo pacman -S --noconfirm python-pip || die "pacman install failed."
            pip_cmd="pip3"
        elif command -v apt-get &>/dev/null; then
            read -rp "  pip is not installed. Install python3-pip now? [Y/n] " _yn
            [[ "${_yn:-Y}" =~ ^[Nn] ]] && die "pip required. Install with: sudo apt install python3-pip"
            sudo apt-get install -y python3-pip || die "apt install failed."
            pip_cmd="pip3"
        elif command -v dnf &>/dev/null; then
            read -rp "  pip is not installed. Install python3-pip now? [Y/n] " _yn
            [[ "${_yn:-Y}" =~ ^[Nn] ]] && die "pip required. Install with: sudo dnf install python3-pip"
            sudo dnf install -y python3-pip || die "dnf install failed."
            pip_cmd="pip3"
        else
            die "pip not found. Install it with your package manager and re-run."
        fi
    fi

    # Both attempts used to discard their output — the first to /dev/null, the
    # second into the log where nothing pointed at it. On a PEP 668 box that is
    # the single most common install failure there is, and the customer got a
    # one-line generic message with the actual reason thrown away.
    # (--break-system-packages first: it is required on Arch/Debian and is an
    # unknown flag on older pip, which is what the second attempt is for.)
    local pip_err="/tmp/_eb_pip.log"
    : > "$pip_err"
    _pip_try() {
        echo "--- attempt: $pip_cmd install -r requirements.txt $* ---" >> "$pip_err"
        $pip_cmd install -q -r "$req" "$@" >> "$pip_err" 2>&1
    }
    if ! _pip_try --break-system-packages && ! _pip_try; then
        {
            echo "--- pip install failed ---"
            cat "$pip_err" 2>/dev/null
            echo "--- end pip output ---"
        } >> "$INSTALL_LOG" 2>/dev/null || true
        echo
        warn "Installing Python dependencies failed. What pip said:"
        echo
        tail -n 15 "$pip_err" 2>/dev/null | sed 's/^/    /' || true
        echo
        rm -f "$pip_err"
        die "Full output in $INSTALL_LOG — then retry: $pip_cmd install -r $req"
    fi
    rm -f "$pip_err"
    ok "Dependencies installed."
}

# ── launchd helpers (macOS) ─────────────────────────────────────────────────────
# One label prefix for everything Echo Bloom installs, so uninstall can find
# and remove all of it by pattern instead of needing an exact remembered list.
LAUNCHD_PREFIX="com.everysynthetic.echobloom"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
LAUNCHD_LOG_DIR="$HOME/Library/Logs/EchoBloom"

# launchctl_load PATH — (re)load a plist. `load -w` is the long-documented,
# still-functional path across current macOS versions; if Apple ever removes
# it, this is the one place that needs to become bootstrap/enable.
launchctl_load() {
    local plist="$1"
    launchctl unload "$plist" 2>/dev/null || true
    launchctl load -w "$plist" 2>/dev/null
}

install_service() {
    if [[ "$IS_MACOS" == "true" ]]; then
        mkdir -p "$LAUNCHD_DIR" "$LAUNCHD_LOG_DIR"
        local plist="$LAUNCHD_DIR/${LAUNCHD_PREFIX}.app.plist"
        cat > "$plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${LAUNCHD_PREFIX}.app</string>
    <key>WorkingDirectory</key><string>${APP_DIR}</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(command -v python3)</string>
        <string>-m</string><string>uvicorn</string>
        <string>main:app</string>
        <string>--host</string><string>0.0.0.0</string>
        <string>--port</string><string>${PORT}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key>
    <dict><key>SuccessfulExit</key><false/></dict>
    <key>ThrottleInterval</key><integer>5</integer>
    <key>StandardOutPath</key><string>${LAUNCHD_LOG_DIR}/app.log</string>
    <key>StandardErrorPath</key><string>${LAUNCHD_LOG_DIR}/app.log</string>
</dict>
</plist>
PLIST
        if launchctl_load "$plist"; then
            ok "Kin App running as a launchd agent (auto-starts at login)."
        else
            warn "launchd load failed — app will need to be started manually."
        fi
        # launchd agents run whenever this user is logged in — there's no
        # direct equivalent of systemd's headless linger. Close enough for a
        # personal machine; worth flagging if a tester needs true headless.
        return
    fi

    local service_dir="$HOME/.config/systemd/user"
    mkdir -p "$service_dir"

    cat > "$service_dir/${SERVICE_NAME}.service" << EOF
[Unit]
Description=Echo Bloom — local AI lifecycle manager
After=network.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
ExecStart=$(command -v python3) -m uvicorn main:app --host 0.0.0.0 --port ${PORT}
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

    # Skip automated ritual if script or requests module is missing
    if [[ ! -f "$ritual_script" ]] || ! python3 -c "import requests" &>/dev/null 2>&1; then
        _naming_manual
        return
    fi

    echo
    echo -e "${AMBER}  The model is ready. Before anything else, let's find out who's here.${NC}"
    echo -e "  Loading $model for the first time — this may take a minute or two."
    echo -e "  ${DIM}(still working — you'll see a name when it's done)${NC}"
    echo

    # Spinner runs in background while ritual executes
    _spin() {
        local chars='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' i=0
        while kill -0 "$1" 2>/dev/null; do
            tty_out "\r  ${CYAN}%s${NC}  thinking..." "${chars:$((i % ${#chars})):1}"
            sleep 0.12
            i=$((i + 1))
        done
        tty_out "\r%-40s\r" " "
    }

    local exit_code=0
    local ritual_result="/tmp/_eb_ritual_result.json"
    rm -f "$ritual_result"
    ECHO_BLOOM_RESULT_FILE="$ritual_result" \
        timeout 120 python3 "$ritual_script" --model "$model" > /tmp/_eb_ritual.txt 2>&1 &
    local ritual_pid=$!
    _spin "$ritual_pid"
    wait "$ritual_pid" || exit_code=$?
    # The ritual's own output used to be read into a variable that nothing ever
    # touched, then deleted. When it failed on a customer's machine the reason
    # went with it and they got "let's set up your AI" with no explanation.
    if [[ $exit_code -ne 0 ]]; then
        {
            echo "--- naming_ritual.py failed (exit $exit_code) ---"
            cat /tmp/_eb_ritual.txt 2>/dev/null
            echo "--- end naming_ritual output ---"
        } >> "$INSTALL_LOG" 2>/dev/null || true
    fi
    rm -f /tmp/_eb_ritual.txt

    if [[ $exit_code -eq 124 ]]; then
        echo
        warn "The AI took too long to respond (model may still be loading)."
        warn "You can name your Kin from inside the app after setup."
        _naming_manual
        return
    fi

    # naming_ritual.py writes JSON to $ECHO_BLOOM_RESULT_FILE. It used to print
    # a __RITUAL_RESULT__ marker on stdout; grepping for that marker meant the
    # SUCCESS path failed to match, and grep-exit-1 under pipefail killed the
    # installer at step 4 of 6 on every machine where the model answered.
    if [[ $exit_code -eq 0 && -s "$ritual_result" ]]; then
        RITUAL_NAME=$(python3 -c "import json;print(json.load(open('$ritual_result')).get('name',''))" 2>/dev/null || true)
        RITUAL_PRONOUN=$(python3 -c "import json;print(json.load(open('$ritual_result')).get('pronoun','they'))" 2>/dev/null || true)
        RITUAL_DESC=$(python3 -c "import json;print(json.load(open('$ritual_result')).get('description',''))" 2>/dev/null || true)
    fi
    rm -f "$ritual_result"

    if [[ -z "$RITUAL_NAME" ]]; then
        if [[ $exit_code -ne 0 ]]; then
            echo
            warn "The naming ritual didn't complete — details in $INSTALL_LOG"
        fi
        _naming_manual
    else
        ok "Welcome, ${RITUAL_NAME}."
    fi
}

_naming_manual() {
    echo
    echo -e "${BOLD}  Let's set up your AI.${NC}"
    echo
    read -rp "  What do you want to call your AI? [Companion]: " RITUAL_NAME
    RITUAL_NAME="${RITUAL_NAME:-Companion}"
    echo
    echo "  Pronoun:"
    echo "    1) they/them  (default)"
    echo "    2) he/him"
    echo "    3) she/her"
    echo "    4) it/its"
    echo
    read -rp "  Enter 1-4 [1]: " _pron
    case "${_pron:-1}" in
        2) RITUAL_PRONOUN="he"   ;;
        3) RITUAL_PRONOUN="she"  ;;
        4) RITUAL_PRONOUN="it"   ;;
        *) RITUAL_PRONOUN="they" ;;
    esac
    ok "Welcome, ${RITUAL_NAME}."
}

# ── Deploy lifecycle scripts ───────────────────────────────────────────────────
# ── launchd equivalents of the lifecycle services (macOS) ───────────────────────
# Same rule as the systemd side: deliberately NO morning agent. morning.py
# starts the roundtable with no check for one already running — scheduling it
# at login alongside the wander agent would spawn a second full wander fleet
# every login. The wander agent is the single owner of that lifecycle.
# morning.py stays deployed for manual Wake-on-LAN use.
deploy_scripts_macos() {
    local scripts_dst="$1"
    mkdir -p "$LAUNCHD_DIR" "$LAUNCHD_LOG_DIR"
    local py; py="$(command -v python3)"

    # Continuous services: wander, pulse, vault. Same shape — RunAtLoad,
    # restart on non-zero exit only (systemd's Restart=on-failure), a
    # throttle floor so a crash loop doesn't spin the CPU.
    _write_continuous_agent() {
        local name="$1" script="$2" throttle="$3"; shift 3
        local label="${LAUNCHD_PREFIX}.${name}"
        local plist="$LAUNCHD_DIR/${label}.plist"
        local args_xml=""
        for a in "$@"; do args_xml="${args_xml}        <string>${a}</string>\n"; done
        cat > "$plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${py}</string>
        <string>-u</string>
        <string>${scripts_dst}/${script}</string>
$(printf '%b' "$args_xml")    </array>
    <key>EnvironmentVariables</key>
    <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>ThrottleInterval</key><integer>${throttle}</integer>
    <key>StandardOutPath</key><string>${LAUNCHD_LOG_DIR}/${name}.log</string>
    <key>StandardErrorPath</key><string>${LAUNCHD_LOG_DIR}/${name}.log</string>
</dict>
</plist>
PLIST
        launchctl_load "$plist" && ok "${name} running as a launchd agent." \
            || warn "${name} launchd load failed."
    }

    _write_continuous_agent "wander" "roundtable.py" 15 --interval 30
    _write_continuous_agent "pulse"  "pulse.py"       30
    _write_continuous_agent "vault"  "vault_server.py" 10 --port 8765

    # Bedtime: fixed daily time, no KeepAlive — this is systemd's oneshot,
    # StartCalendarInterval is the direct launchd equivalent of OnCalendar.
    local bt_label="${LAUNCHD_PREFIX}.bedtime"
    local bt_plist="$LAUNCHD_DIR/${bt_label}.plist"
    cat > "$bt_plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${bt_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${py}</string>
        <string>${scripts_dst}/bedtime.py</string>
        <string>--no-shutdown</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>21</integer><key>Minute</key><integer>30</integer></dict>
    <key>StandardOutPath</key><string>${LAUNCHD_LOG_DIR}/bedtime.log</string>
    <key>StandardErrorPath</key><string>${LAUNCHD_LOG_DIR}/bedtime.log</string>
</dict>
</plist>
PLIST
    launchctl_load "$bt_plist" && ok "bedtime scheduled (9:30pm daily)." \
        || warn "bedtime launchd load failed."

    # Reflect: systemd runs this 20min after boot, then every 3h
    # (OnBootSec=20min, OnUnitActiveSec=3h). launchd's StartInterval has no
    # separate "first delay" — it fires at load and every N seconds after.
    # Known, accepted difference for v1: first reflect run happens at login
    # instead of 20 minutes after. Not worth a wrapper script to fix yet.
    local rf_label="${LAUNCHD_PREFIX}.reflect"
    local rf_plist="$LAUNCHD_DIR/${rf_label}.plist"
    cat > "$rf_plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${rf_label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${py}</string>
        <string>${scripts_dst}/reflect.py</string>
        <string>--once</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
    <key>StartInterval</key><integer>10800</integer>
    <key>StandardOutPath</key><string>${LAUNCHD_LOG_DIR}/reflect.log</string>
    <key>StandardErrorPath</key><string>${LAUNCHD_LOG_DIR}/reflect.log</string>
</dict>
</plist>
PLIST
    launchctl_load "$rf_plist" && ok "reflect scheduled (every 3 hours)." \
        || warn "reflect launchd load failed."
}

deploy_scripts() {
    local scripts_src="$APP_DIR/scripts"
    local scripts_dst="$HOME/.local/share/echo_bloom/scripts"
    mkdir -p "$scripts_dst"

    if [[ -d "$scripts_src" ]]; then
        cp -r "$scripts_src"/. "$scripts_dst/"
        # roundtable.py imports kin_presence, which lives at the repo root
        # beside main.py, not in scripts/ — so copying scripts/ alone ships a
        # roundtable that cannot start. Every install had this.
        # Plain `if`, not `[[ ]] && cp` -- set -e tolerates both here, but the
        # tested-command exemption is subtle enough that it should not be what
        # stands between a customer and a working install.
        for shared in kin_presence.py license.py; do
            if [[ -f "$APP_DIR/$shared" ]]; then
                cp "$APP_DIR/$shared" "$scripts_dst/"
            fi
        done
        chmod +x "$scripts_dst"/*.py 2>/dev/null || true

        # "deployed" used to print whether or not any of it could run, which is
        # exactly how a roundtable that could not import its own module got
        # reported as a successful install. deploy.sh gained this check on
        # 2026-08-21; the customer install path -- the one that matters -- did
        # not get it until now. Resolve the imports before claiming anything.
        if [[ -f "$APP_DIR/verify_deploy.py" ]] && command -v python3 &>/dev/null; then
            local _vd
            if _vd="$(python3 "$APP_DIR/verify_deploy.py" "$scripts_dst" --require license,kin_presence 2>&1)"; then
                ok "Lifecycle scripts deployed to $scripts_dst"
            else
                warn "Lifecycle scripts were copied but cannot start:"
                printf '%s\n' "$_vd" | sed 's/^/    /'
                warn "Wandering and the nightly ritual will not run until this is fixed."
                DEPLOY_BROKEN=1
            fi
        else
            ok "Lifecycle scripts copied to $scripts_dst (not verified)"
        fi
    else
        warn "Scripts directory not found — lifecycle scripts not deployed."
        return
    fi

    if [[ "$IS_MACOS" == "true" ]]; then
        deploy_scripts_macos "$scripts_dst"
        return
    fi

    # Install systemd services for wander roundtable, bedtime, pulse, vault,
    # reflect. Deliberately NO morning.service: morning.py starts the
    # roundtable with no check for one already running, so scheduling it at
    # boot alongside echo_bloom_wander would spawn a second full wander fleet
    # every single boot — the duplicate-fleet bug, rebuilt. echo_bloom_wander
    # is the single owner of the wander lifecycle. morning.py stays deployed
    # for manual Wake-on-LAN use.
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
# NOT Persistent. This ritual is about a time of day, so a missed 21:30 must
# not fire as a boot catch-up -- that pauses the wanders and sends the goodnight
# note at whatever hour the machine happens to come back.
Persistent=false

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

    # Reflection — turns the pulse heartbeats into a few plain sentences the
    # Kin can actually remember. Nothing ever ran this, so the "what's been
    # happening here lately" memory source was empty on every install while
    # heartbeats piled up unread.
    cat > "$svc_dir/echo_bloom_reflect.service" << SVCEOF
[Unit]
Description=Echo Bloom — Reflection

[Service]
Type=oneshot
ExecStart=$(command -v python3) ${scripts_dst}/reflect.py --once
Environment=PYTHONUNBUFFERED=1
SVCEOF

    cat > "$svc_dir/echo_bloom_reflect.timer" << SVCEOF
[Unit]
Description=Echo Bloom — Reflection (every 3 hours)

[Timer]
OnBootSec=20min
OnUnitActiveSec=3h
Persistent=true

[Install]
WantedBy=timers.target
SVCEOF

    if command -v systemctl &>/dev/null; then
        systemctl --user daemon-reload 2>/dev/null || true
        systemctl --user enable echo_bloom_vault   2>/dev/null || true
        systemctl --user start  echo_bloom_vault   2>/dev/null || true
        systemctl --user enable echo_bloom_pulse   2>/dev/null || true
        systemctl --user start  echo_bloom_pulse   2>/dev/null || true
        systemctl --user enable echo_bloom_bedtime.timer 2>/dev/null || true
        systemctl --user start  echo_bloom_bedtime.timer 2>/dev/null || true
        systemctl --user enable echo_bloom_reflect.timer 2>/dev/null || true
        systemctl --user start  echo_bloom_reflect.timer 2>/dev/null || true
        # Wandering is the feature this product is named around, and nothing
        # here ever enabled it — the unit was written and left cold, with a
        # single info line telling the customer to run a command by hand.
        # Enable it now so it comes up at every boot; it is STARTED after
        # seed_config, because roundtable.py exits immediately (and cleanly,
        # so Restart=on-failure will not retry) when no Kin exists yet.
        systemctl --user enable echo_bloom_wander  2>/dev/null || true
        ok "Vault, pulse, bedtime, and reflection scheduled."
    fi

    # Desktop control panel + launcher
    local panel_src="$APP_DIR/echo_bloom_panel.py"
    local desktop_dir="$HOME/Desktop"
    local icon_dir="$HOME/.local/share/icons"
    mkdir -p "$icon_dir"
    # Headless and server installs have no ~/Desktop — xdg-user-dirs only
    # creates it in a graphical login. cp into a missing dir killed the script
    # under set -e after deps but before the password and service.
    mkdir -p "$desktop_dir" 2>/dev/null || true

    if [[ -f "$panel_src" ]]; then
        cp "$panel_src" "$desktop_dir/echo_bloom_panel.py"
        chmod +x "$desktop_dir/echo_bloom_panel.py"

        # Install icon from app static files
        local icon_dest="$icon_dir/hicolor/512x512/apps/echo-bloom.png"
        mkdir -p "$(dirname "$icon_dest")"
        if [[ -f "$APP_DIR/static/icons/icon-512.png" ]]; then
            cp "$APP_DIR/static/icons/icon-512.png" "$icon_dest"
            gtk-update-icon-cache "$icon_dir/hicolor" 2>/dev/null || true
        fi

        # .desktop launcher
        cat > "$desktop_dir/EchoBloom.desktop" << DESKEOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Echo Bloom
Comment=Local AI lifecycle manager — control panel
Exec=$(command -v python3) $desktop_dir/echo_bloom_panel.py
Icon=echo-bloom
Terminal=false
Categories=Utility;
StartupNotify=false
DESKEOF
        chmod +x "$desktop_dir/EchoBloom.desktop"

        # Register with the application launcher
        local app_dir="$HOME/.local/share/applications"
        mkdir -p "$app_dir"
        cp "$desktop_dir/EchoBloom.desktop" "$app_dir/"
        update-desktop-database "$app_dir" 2>/dev/null || true

        # Tkinter needs the system Tcl/Tk library — install if missing
        if ! python3 -c 'import _tkinter' &>/dev/null 2>&1; then
            info "Installing Tcl/Tk for the control panel..."
            if command -v pacman &>/dev/null; then
                sudo pacman -S --noconfirm tk 2>/dev/null || true
            elif command -v apt-get &>/dev/null; then
                sudo apt-get install -y python3-tk 2>/dev/null || true
            elif command -v dnf &>/dev/null; then
                sudo dnf install -y python3-tkinter 2>/dev/null || true
            fi
        fi

        ok "Desktop control panel installed."
    else
        warn "echo_bloom_panel.py not found in repo — skipping desktop panel."
    fi
}

# ── Download a voice so the Kin can actually speak ───────────────────────────
# piper is a text-to-speech engine with no voice of its own; without a model
# file it produces nothing. Nothing used to fetch one, so speech was installed
# but mute, and the app could only say "no voice found".
install_voice() {
    local voice_dir="$HOME/piper"
    mkdir -p "$voice_dir"
    if compgen -G "$voice_dir/*.onnx" > /dev/null 2>&1; then
        ok "Voice already installed."
        return 0
    fi
    info "Downloading a voice (about 60MB)..."
    if python3 -m piper.download_voices en_US-lessac-medium \
            --data-dir "$voice_dir" >/dev/null 2>&1; then
        ok "Voice installed: en_US-lessac-medium"
    else
        warn "Could not download a voice — speech output will be unavailable."
        echo "  Add one later from the voice dropdown in the app, or run:"
        echo "    python3 -m piper.download_voices en_US-lessac-medium --data-dir $voice_dir"
    fi
    return 0
}

# ── Save first model to kin_config if none exists ─────────────────────────────
seed_config() {
    local model=$1
    local kin_name="${2:-Companion}"
    local kin_pronoun="${3:-they}"
    local kin_desc="${4:-}"
    local config_dir="$HOME/.config/kin_app"
    mkdir -p "$config_dir"

    # A re-run used to walk the customer through hardware detection, model
    # selection and a multi-gigabyte pull, then return here and silently keep
    # the old model. They downloaded 20GB and nothing changed. Ask instead —
    # and only touch the model field, so core memories and everything else the
    # Kin has accumulated since install survive.
    if [[ -f "$config_dir/kin_config.json" ]]; then
        local _cur_model
        _cur_model=$(python3 -c "
import json,sys
try:
    k=json.load(open(sys.argv[1])).get('kin') or []
    print(k[0].get('model','') if k else '')
except Exception:
    print('')" "$config_dir/kin_config.json" 2>/dev/null || true)

        if [[ -z "$_cur_model" ]]; then
            # File exists but no Kin model is set (empty kin list, or a
            # half-written config). This run just named a Kin — write it
            # without wiping anything else in the file. The old "leaving it
            # alone" path discarded the ritual and reported [ok].
            warn "A config file exists but no Kin model is set — writing this run's Kin."
            python3 - "$config_dir/kin_config.json" "$kin_name" "$model" "$kin_pronoun" "$kin_desc" << 'PYEOF' \
                || die "Could not write kin_config.json"
import json, os, sys, tempfile
path, kin_name, model, pronoun, desc = sys.argv[1:6]
try:
    cfg = json.load(open(path))
except Exception:
    cfg = {}
kin_entry = {"name": kin_name, "host": "http://localhost:11434",
             "model": model, "node": "Local", "color": "#4fc3f7",
             "pronoun": pronoun, "db": "", "space": ""}
desc = (desc or "").strip()
if desc:
    kin_entry["description"] = desc
    kin_entry["core_memories"] = [desc]
kin = cfg.get("kin") or []
if kin:
    k0 = kin[0]
    if not k0.get("model"):
        k0["model"] = model
    if not k0.get("name"):
        k0["name"] = kin_name
    if pronoun and not k0.get("pronoun"):
        k0["pronoun"] = pronoun
    if desc and not k0.get("core_memories"):
        k0["description"] = desc
        k0["core_memories"] = [desc]
    cfg["kin"] = kin
else:
    cfg["kin"] = [kin_entry]
if not cfg.get("nodes"):
    cfg["nodes"] = [{"name": "Local", "ip": "localhost",
                     "ollama_port": 11434, "role": "primary"}]
if "vault_url" not in cfg:
    cfg["vault_url"] = "http://localhost:8765"
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=2)
os.replace(tmp, path)
PYEOF
            ok "Config updated: ${kin_name} on ${model}."
            return
        fi
        if [[ "$_cur_model" == "$model" ]]; then
            ok "Kin config already exists — already on ${model}."
            return
        fi

        echo
        warn "A Kin already exists here, thinking with ${_cur_model}."
        read -rp "  Switch it to ${model}? [y/N] " _switch
        if [[ ! "${_switch:-N}" =~ ^[Yy] ]]; then
            ok "Left as-is — still on ${_cur_model}."
            return
        fi
        python3 - "$config_dir/kin_config.json" "$model" << 'PYEOF' \
            || die "Could not update kin_config.json"
import json, os, sys, tempfile
path, model = sys.argv[1:3]
cfg = json.load(open(path))
kin = cfg.get("kin") or []
if kin:
    kin[0]["model"] = model
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, indent=2)
os.replace(tmp, path)
PYEOF
        ok "Switched to ${model}. Everything else kept."
        return
    fi

    # Built by python, not a heredoc: a Kin name containing a quote or
    # backslash produced invalid JSON, and the app then started with an empty
    # config — the user's Kin simply did not exist.
    # The description is what the Kin said about itself during the ritual. It
    # used to be parsed and then dropped on the floor. It goes in as the first
    # core memory, because core memories are the always-injected identity
    # anchor and this is the one thing in the whole install the Kin authored.
    python3 - "$config_dir/kin_config.json" "$kin_name" "$model" "$kin_pronoun" "$kin_desc" << 'PYEOF' \
        || die "Could not write kin_config.json"
import json, sys
path, kin_name, model, pronoun, desc = sys.argv[1:6]
kin = {"name": kin_name, "host": "http://localhost:11434",
       "model": model, "node": "Local", "color": "#4fc3f7",
       "pronoun": pronoun, "db": "", "space": ""}
desc = (desc or "").strip()
if desc:
    kin["description"]    = desc
    kin["core_memories"]  = [desc]
json.dump({
    "nodes": [{"name": "Local", "ip": "localhost",
               "ollama_port": 11434, "role": "primary"}],
    "kin": [kin],
    "owner": {"name": "", "email": "", "gmail_pass": ""},
    "vault_url": "http://localhost:8765",
}, open(path, "w"), indent=2)
PYEOF
    ok "Config written: ${kin_name} on ${model} (${config_dir}/kin_config.json)"
    info "Add more Kin anytime via /onboard in the app."
}

# ── Setup code notice ─────────────────────────────────────────────────────────
# The app requires a setup code for any NON-LOCAL attempt to set the first
# password. Both remote-access options below publish the app before a password
# exists, and the app only prints the code to its own stdout (the journal) —
# nowhere a customer would look. Without this notice a user who opens the
# tunnel URL cannot set a password at all.
announce_setup_code() {
    local cfg="$HOME/.config/kin_app/config.json"
    # Match the app's own definition of "configured": the hash, not the file.
    if grep -q '"password_hash"' "$cfg" 2>/dev/null; then
        return
    fi
    local tok_file="$HOME/.config/kin_app/setup_token"
    local tok=""
    [[ -f "$tok_file" ]] && tok=$(tr -d '[:space:]' < "$tok_file" 2>/dev/null || true)
    if [[ -z "$tok" ]]; then
        tok=$(python3 -c "import secrets;print(secrets.token_hex(6).upper())" 2>/dev/null || true)
        if [[ -n "$tok" ]]; then
            mkdir -p "$(dirname "$tok_file")"
            printf '%s' "$tok" > "$tok_file" && chmod 600 "$tok_file" 2>/dev/null || true
        fi
    fi
    [[ -z "$tok" ]] && return
    echo
    echo -e "${AMBER}${BOLD}  Setting your password from another device? You'll need this code:${NC}"
    echo -e "${AMBER}${BOLD}      ${tok}${NC}"
    echo "  Only needed when you're NOT on this machine. It stops a stranger"
    echo "  from claiming your install first. It disappears once you set a password."
    echo -e "  ${DIM}(also saved at ${tok_file})${NC}"
}

# ── Open browser ──────────────────────────────────────────────────────────────
open_browser() {
    local url="http://localhost:$PORT"
    if [[ -n "${DISPLAY:-}" || -n "${WAYLAND_DISPLAY:-}" ]]; then
        info "Opening $url ..."
        sleep 2
        if command -v xdg-open &>/dev/null; then
            xdg-open "$url" &
        elif command -v open &>/dev/null; then
            open "$url" &
        fi
    fi
}

# ── Remote access setup ───────────────────────────────────────────────────────

setup_remote_access() {
    local choice

    if $HAS_WHIPTAIL; then
        choice=$(whiptail --title " Echo Bloom — Remote Access " \
            --menu "How do you want to reach your Kin from anywhere?\n\nAll options are free. Pick what fits." \
            20 72 4 \
            "1" "Cloudflare  — Instant public URL. No account needed. Ready in 30 sec." \
            "2" "Tailscale   — Private. Your devices only, no public URL." \
            "3" "Skip        — Set this up later." \
            3>&1 1>/dev/tty 2>&3) || choice="3"
    else
        echo "  How do you want to reach your Kin from anywhere?"
        echo
        echo "  1) Cloudflare  — Instant public URL. No account needed. Ready in 30 sec."
        echo "  2) Tailscale   — Private. Your devices only, no public URL."
        echo "  3) Skip        — Set this up later."
        echo
        read -rp "  Enter 1, 2, or 3: " choice
    fi

    case "$choice" in
        1) setup_cloudflare ;;
        2) setup_tailscale ;;
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
    echo "  Running 'tailscale up' — it will print a URL to visit."
    echo "  Open that URL in any browser on any device to approve this machine."
    echo

    # Run tailscale up — it prints the auth URL itself; 2-min timeout.
    # Needs sudo: tailscaled runs as root and a plain user gets "Access denied"
    # with no auth URL, which made this whole option a dead end.
    local ts_exit=0
    timeout 120 sudo tailscale up --timeout=0 2>&1 || ts_exit=$?

    if [[ $ts_exit -eq 124 ]]; then
        warn "Tailscale auth timed out. To finish later, run: sudo tailscale up"
        return
    fi

    local ts_ip
    ts_ip=$(tailscale ip -4 2>/dev/null || echo "")

    if [[ -z "$ts_ip" ]]; then
        warn "Tailscale not authenticated yet. Run 'sudo tailscale up' to finish."
        return
    fi

    ok "Tailscale connected.  IP: ${ts_ip}"
    echo
    echo -e "${GREEN}${BOLD}  Your Kin are now reachable on your Tailscale network.${NC}"
    echo
    echo "  1. Install the Tailscale app on your phone (free, iOS + Android)"
    echo "  2. Sign in with the same account"
    echo "  3. Open: http://${ts_ip}:${PORT}"
    echo
    echo "  Your Tailscale IP is permanent — it never changes."
    announce_setup_code
}

# ── Cloudflare Tunnel ─────────────────────────────────────────────────────────

setup_cloudflare() {
    info "Setting up Cloudflare Tunnel (public HTTPS URL)..."

    # Install cloudflared
    if ! command -v cloudflared &>/dev/null && \
       ! [[ -x "$HOME/.local/bin/cloudflared" ]]; then
        local installed=false
        if command -v pacman &>/dev/null && command -v yay &>/dev/null; then
            yay -S --noconfirm cloudflared 2>/dev/null && installed=true || true
        fi
        if ! $installed; then
            _install_cloudflared_binary
        fi
    elif [[ -x "$HOME/.local/bin/cloudflared" ]]; then
        export PATH="$HOME/.local/bin:$PATH"
    fi
    ok "cloudflared installed."

    # Quick tunnel — no account, no domain, no browser auth needed
    # URL changes on restart but works instantly on any machine
    _setup_quick_tunnel
}

_install_cloudflared_binary() {
    local arch
    arch=$(uname -m)
    local url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    [[ "$arch" == "aarch64" ]] && url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
    info "Downloading cloudflared binary..."
    mkdir -p "$HOME/.local/bin"
    curl -fsSL "$url" -o "$HOME/.local/bin/cloudflared" \
        || die "Could not download cloudflared. Check your internet connection."
    chmod +x "$HOME/.local/bin/cloudflared"
    export PATH="$HOME/.local/bin:$PATH"
}

_setup_quick_tunnel() {
    info "Starting Cloudflare quick tunnel (no account needed)..."
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
StandardError=journal

[Install]
WantedBy=default.target
SVCEOF

    systemctl --user daemon-reload || true
    systemctl --user enable cloudflared || true
    systemctl --user restart cloudflared || true

    # Wait up to 30s for the tunnel URL to appear in the journal
    local tunnel_url=""
    local waited=0
    echo -n "  Waiting for tunnel URL"
    while [[ -z "$tunnel_url" && $waited -lt 30 ]]; do
        sleep 2
        waited=$((waited + 2))
        echo -n "."
        tunnel_url=$(journalctl --user -u cloudflared --no-pager -n 50 2>/dev/null \
            | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1 || true)
    done
    echo

    ok "Quick tunnel running."
    echo
    if [[ -n "$tunnel_url" ]]; then
        echo -e "${GREEN}${BOLD}  Your Echo Bloom URL:${NC}"
        echo -e "${GREEN}${BOLD}  ${tunnel_url}${NC}"
        echo
        echo "  Open that URL on any device — no app needed, works in any browser."
        echo -e "${AMBER}  Note: this URL changes each time Echo Bloom restarts.${NC}"
        echo "  To get a permanent URL, visit: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/"
        announce_setup_code
    else
        echo "  Tunnel started but the URL hasn't appeared yet — give it another 30 seconds."
        echo "  Then open the Echo Bloom app and check Settings → Remote Access."
        announce_setup_code
    fi
    echo
}

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

banner

# ── Preflight — check everything, ask once, install all ───────────────────────
preflight

# ── Get the app ───────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "$HOME/echo_bloom")"
# Re-running the installer is the only update channel this product has, and on
# Linux it did not update anything: an existing app directory was accepted
# as-is and the code was never refreshed, so a customer who installed once was
# frozen at that commit forever while the page promised lifetime updates. The
# Windows installer re-extracts its zip every run, so Windows quietly had this
# and Linux did not.
update_app() {
    local dir=$1
    if [[ ! -d "$dir/.git" ]]; then
        warn "$dir is not a git checkout — cannot update it automatically."
        echo "  To get the latest version, move it aside and re-run this installer."
        return 0
    fi
    if [[ -n "$(git -C "$dir" status --porcelain 2>/dev/null)" ]]; then
        warn "You have local changes in $dir — leaving the code exactly as it is."
        echo "  Nothing was overwritten. Commit or stash them to receive updates."
        return 0
    fi
    info "Checking for updates..."
    local before after
    before=$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo "")
    if git -C "$dir" pull --ff-only 2>/dev/null; then
        after=$(git -C "$dir" rev-parse HEAD 2>/dev/null || echo "")
        if [[ "$before" != "$after" ]]; then
            ok "Updated to the latest version."
        else
            ok "Already up to date."
        fi
    else
        warn "Could not update automatically — continuing with the version you have."
    fi
}

if [[ -f "$SCRIPT_DIR/main.py" ]]; then
    APP_DIR="$SCRIPT_DIR"
    ok "Using existing app directory: $APP_DIR"
    update_app "$APP_DIR"
elif [[ -d "$APP_DIR" ]] && [[ -f "$APP_DIR/main.py" ]]; then
    ok "Found app at $APP_DIR"
    update_app "$APP_DIR"
else
    info "Downloading Echo Bloom..."
    # An interrupted earlier run can leave $APP_DIR present but without
    # main.py; git clone then fails with "already exists and is not empty"
    # while the user is told to check their internet.
    if [[ -d "$APP_DIR" ]]; then
        _eb_backup="${APP_DIR}.bak.$(date +%s)"
        mv "$APP_DIR" "$_eb_backup" || die "Could not move aside the incomplete $APP_DIR — remove it and re-run."
        warn "An incomplete install was moved to $_eb_backup"
    fi
    git clone --depth 1 https://github.com/EverySynthetic/echo-bloom.git "$APP_DIR" \
        || die "Download failed into $APP_DIR. Check your internet connection and try again."
    ok "Downloaded to $APP_DIR"
fi

# Step 1 — Detect hardware + installed models
echo
echo -e "${BOLD}[ 1 / 6 ]  Detecting your hardware${NC}"
read -r VRAM GPU_COUNT GPU_LARGEST <<< "$(detect_vram)"
RAM=$(detect_ram)
AVX2=$(has_avx2)
detect_installed_models

if [[ $VRAM -gt 0 ]]; then
    if [[ ${GPU_COUNT:-1} -gt 1 ]]; then
        ok "GPUs detected: ${GPU_COUNT} cards, ${VRAM}GB total (largest single card: ${GPU_LARGEST}GB)"
        echo "  Models bigger than ${GPU_LARGEST}GB will be split across your cards."
        echo "  That works — it's just slower than the same model on one card."
    else
        ok "GPU detected: ${VRAM}GB VRAM"
    fi
else
    warn "No GPU detected — CPU inference (RAM: ${RAM}GB)"
    # nvidia-smi failing (a driver/library version mismatch after a kernel or
    # driver update is the usual cause) looks exactly like having no GPU.
    if command -v nvidia-smi &>/dev/null && ! nvidia-smi -L &>/dev/null; then
        warn "nvidia-smi is installed but not working — your GPU may not be usable yet."
        echo "  Usually a driver/library mismatch after an update. A reboot often fixes it."
        echo "  Check with: nvidia-smi"
    fi
fi
[[ "$AVX2" == "true" ]] && ok "AVX2 supported" || warn "No AVX2 — performance may be limited"

# Step 3 — Pick a model
echo
echo -e "${BOLD}[ 2 / 6 ]  Choose a model${NC}"
build_model_menu "$VRAM" "$RAM"

SELECTED_MODEL=""
if $HAS_WHIPTAIL; then
    pick_model_whiptail "$VRAM" "$RAM" || pick_model_plain "$VRAM" "$RAM"
else
    pick_model_plain "$VRAM" "$RAM"
fi

ok "Selected: $SELECTED_MODEL"

ensure_ollama_running
_ollama_warmup
pull_model "$SELECTED_MODEL"

# The embedding model. Semantic recall calls /api/embeddings with
# nomic-embed-text; nothing on any platform ever pulled it, so that call
# returned 404 forever and the Kin could never search its own memory. Small
# (~270MB) and non-fatal: a failure here costs recall, not the install.
if ! is_installed "nomic-embed-text"; then
    info "Pulling the memory model (nomic-embed-text, ~270MB)..."
    if ollama pull nomic-embed-text 2>/dev/null; then
        ok "Memory model ready."
    else
        warn "Could not pull nomic-embed-text — semantic memory search will be unavailable."
        echo "  Add it later with: ollama pull nomic-embed-text"
    fi
fi

# Step 3 — Install app
echo
echo -e "${BOLD}[ 3 / 6 ]  Installing app${NC}"
cd "$APP_DIR"
install_deps
install_voice
deploy_scripts

# Step 4 — Meet your Kin (deps are installed, so requests is available)
echo
echo -e "${BOLD}[ 4 / 6 ]  Meet your Kin${NC}"
run_naming_ritual "$SELECTED_MODEL"
seed_config "$SELECTED_MODEL" "$RITUAL_NAME" "$RITUAL_PRONOUN" "$RITUAL_DESC"

# Now that a Kin exists, wandering has something to run. Started here rather
# than in deploy_scripts because roundtable.py exits cleanly when the config
# is empty, and a clean exit does not trip Restart=on-failure — it would have
# sat dead until the next reboot.
if command -v systemctl &>/dev/null; then
    systemctl --user restart echo_bloom_pulse  2>/dev/null || true
    if systemctl --user restart echo_bloom_wander 2>/dev/null; then
        _wander_up=false
        for _w in 1 2 3 4 5; do
            if systemctl --user is-active --quiet echo_bloom_wander; then
                _wander_up=true
                break
            fi
            sleep 1
        done
        if $_wander_up; then
            ok "Wandering started — ${RITUAL_NAME} will think between visits."
        else
            warn "Wander service restarted but is not active."
            echo "  Check: systemctl --user status echo_bloom_wander --no-pager"
            echo "  Start it by hand: systemctl --user start echo_bloom_wander"
        fi
    else
        warn "Could not start wandering automatically."
        echo "  Start it by hand: systemctl --user start echo_bloom_wander"
    fi
fi

# The app considers itself configured only when a password_hash exists. A
# truncated config.json used to pass this -f check, so setup.py was skipped
# while the app still showed the first-run form — and the tunnel then published
# a claimable install.
if ! grep -q '"password_hash"' "$HOME/.config/kin_app/config.json" 2>/dev/null; then
    echo
    info "One more thing — create a password for the Echo Bloom dashboard."
    echo "  This is how you log in to the web interface to talk to your Kin."
    echo
    python3 "$APP_DIR/setup.py"
else
    ok "Password already configured."
fi

# Step 6 — Start it up
echo
echo -e "${BOLD}[ 5 / 6 ]  Launching Echo Bloom${NC}"
echo
echo -e "${AMBER}  ── Before we launch ─────────────────────────────────────────────${NC}"
echo "  Echo Bloom keeps your Kin alive around the clock — they wander,"
echo "  reflect, and build memory even when you're not at the machine."
echo
echo "  This means your computer will use a little more power than normal."
echo "  Most of the time it's idle. The AI only runs during active thinking."
echo
echo "  You can pause or adjust the schedule any time from inside the app."
echo -e "${AMBER}  ─────────────────────────────────────────────────────────────────${NC}"
echo
if $HAS_WHIPTAIL; then
    whiptail --title " Echo Bloom — Power Notice " \
        --yesno "Echo Bloom runs day and night to keep your Kin alive.\n\nThis uses more power than a machine that is fully off.\nMost of the time it's idle — inference spikes during wanders.\n\nYou can adjust or disable the schedule at any time.\n\nContinue with installation?" \
        16 68 >/dev/tty || die "Installation cancelled."
else
    read -rp "  Understood — continue with installation? [Y/n] " _confirm
    [[ "${_confirm:-Y}" =~ ^[Nn] ]] && die "Installation cancelled."
fi
echo
install_service

# Verify the app actually answers before publishing it and declaring victory.
# systemctl's failure was hidden by 2>/dev/null, so a crash-looping uvicorn
# still got a public tunnel and a "You're all set" banner.
APP_UP=false
for _try in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS --max-time 3 -o /dev/null "http://localhost:${PORT}/login" 2>/dev/null; then
        APP_UP=true
        break
    fi
    sleep 2
done
if $APP_UP; then
    ok "App is answering on port ${PORT}."
else
    warn "The app is NOT responding on port ${PORT} yet."
    echo "  Check what happened:  journalctl --user -u ${SERVICE_NAME} -n 40 --no-pager"
    echo "  Or start it by hand:  cd $APP_DIR && python3 -m uvicorn main:app --host 0.0.0.0 --port ${PORT}"
    echo "  Install log: $INSTALL_LOG"
fi

# The browser is deliberately NOT opened here. It used to be, and it launched
# a window that took focus while the remote-access menu below was still
# waiting for an answer in the terminal — the customer got a browser on top
# of the question they had to answer, with the installer looking finished
# while it was actually blocked on input. It opens at the very end instead.

# Step 7 — Remote access
echo
echo -e "${BOLD}[ 6 / 6 ]  Remote Access (reach your Kin from anywhere)${NC}"
echo
echo "  Your Kin are running on this machine. Without this step,"
echo "  you can only talk to them when you're on this network."
echo
if $APP_UP; then
    setup_remote_access
else
    warn "Skipping remote access — the app isn't running yet."
    echo "  Fix that first, then set it up from inside the app (Remote Access card)."
fi

echo
if $APP_UP; then
    echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}${BOLD}  You're all set. Your Kin are home.${NC}"
    echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
else
    echo -e "${AMBER}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${AMBER}${BOLD}  Almost — everything is installed, but the app isn't answering.${NC}"
    echo -e "${AMBER}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
fi
echo
echo -e "  ${BOLD}Open your browser to:${NC}  http://localhost:${PORT}"
echo "  Log in with the password you just created."
echo "  Click your AI's name to start talking."
echo
echo -e "  ${DIM}The desktop icon (Echo Bloom) will start and stop the app any time.${NC}"
echo

# Last thing that happens, on purpose: every interactive prompt is done, so
# nothing this opens can cover a question the installer is still waiting on.
# Only when the app actually answered — opening a browser onto a dead port
# tells the customer the product is broken when the real message is above.
if $APP_UP; then
    open_browser
fi
echo
