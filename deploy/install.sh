#!/bin/bash
# Istota deployment bootstrap
# Ensures Ansible is available, then delegates to the Ansible role.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh -o install.sh
#   sudo bash install.sh
#
#   install.sh [OPTIONS]
#     --headless        Skip the setup wizard. Requires an existing settings file
#                       (or --settings PATH). The default is to run the wizard.
#     --update          Update only (pull code + config + restart, skip full system setup)
#     --dry-run         Run wizard, generate Ansible vars, show what would change
#     --settings PATH   Settings file path (default: /etc/istota/settings.toml)
#     --help            Show this help

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}" 2>/dev/null)" && pwd 2>/dev/null || echo "/tmp")"

# Defaults
SETTINGS_FILE="${ISTOTA_SETTINGS_FILE:-/etc/istota/settings.toml}"
VARS_FILE="${ISTOTA_VARS_FILE:-/etc/istota/vars.yml}"
REPO_URL="${ISTOTA_REPO_URL:-https://github.com/istota-project/istota.git}"
REPO_BRANCH="${ISTOTA_REPO_BRANCH:-main}"
INSTALL_DIR=""  # Set after repo is available
UPDATE_ONLY=false
HEADLESS=false
DRY_RUN=false
RUN_WIZARD=false  # Resolved in main() based on flags + settings file + TTY

# Output helpers
_BOLD="\033[1m"
_BLUE="\033[1;34m"
_GREEN="\033[1;32m"
_YELLOW="\033[1;33m"
_RED="\033[1;31m"
_DIM="\033[2m"
_RESET="\033[0m"

info()    { echo -e "${_BLUE}==>${_RESET} $*"; }
ok()      { echo -e "${_GREEN}  ✓${_RESET} $*"; }
warn()    { echo -e "${_YELLOW}  !${_RESET} $*"; }
error()   { echo -e "${_RED}ERROR:${_RESET} $*" >&2; }
die()     { error "$@"; exit 1; }
section() { echo; echo -e "${_BOLD}━━━ $* ━━━${_RESET}"; echo; }

command_exists() { command -v "$1" &>/dev/null; }

# ============================================================
# Parse arguments
# ============================================================

while [ $# -gt 0 ]; do
    case "$1" in
        --headless)     HEADLESS=true; shift ;;
        --update)       UPDATE_ONLY=true; shift ;;
        --dry-run)      DRY_RUN=true; shift ;;
        --settings)     SETTINGS_FILE="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,/^$/s/^# \?//p' "$0"
            exit 0 ;;
        *) die "Unknown option: $1. Use --help for usage." ;;
    esac
done

# ============================================================
# Pre-flight
# ============================================================

preflight() {
    section "Pre-flight Checks"

    if [ "$(id -u)" -ne 0 ]; then
        die "This script must be run as root (or with sudo)"
    fi

    # OS detection
    if [ -f /etc/os-release ]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        case "$ID" in
            debian)
                if [ "${VERSION_ID:-0}" -lt 13 ] 2>/dev/null; then
                    warn "Debian $VERSION_ID detected. Debian 13+ recommended."
                else
                    ok "OS: $PRETTY_NAME"
                fi
                ;;
            ubuntu) ok "OS: $PRETTY_NAME" ;;
            *) warn "Untested OS: $PRETTY_NAME. Debian/Ubuntu recommended." ;;
        esac
    else
        warn "Could not detect OS. Debian/Ubuntu recommended."
    fi

    # Internet
    if curl -sf --max-time 5 https://github.com > /dev/null 2>&1; then
        ok "Internet connectivity"
    else
        die "No internet connectivity. Cannot reach github.com."
    fi

    # Python 3.11+
    if command_exists python3; then
        local pyver pymajor pyminor
        pyver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        pymajor=$(echo "$pyver" | cut -d. -f1)
        pyminor=$(echo "$pyver" | cut -d. -f2)
        if [ "$pymajor" -ge 3 ] && [ "$pyminor" -ge 11 ]; then
            ok "Python $pyver"
        else
            warn "Python $pyver found. 3.11+ required (will be installed)."
        fi
    else
        warn "Python not found (will be installed)"
    fi

    # Existing settings — the wizard-or-not decision is resolved in main()
    if [ -f "$SETTINGS_FILE" ]; then
        ok "Existing settings found at $SETTINGS_FILE"
    fi
}

# ============================================================
# Ensure prerequisites: Python, pipx, Ansible
# ============================================================

ensure_python() {
    if command_exists python3; then
        local pyminor
        pyminor=$(python3 -c "import sys; print(sys.version_info.minor)")
        if [ "$pyminor" -ge 11 ]; then
            return
        fi
    fi
    info "Installing Python 3"
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv 2>&1 | tail -3
    ok "Python installed"
}

ensure_pipx() {
    if command_exists pipx; then
        ok "pipx available"
        return
    fi
    info "Installing pipx"
    apt-get install -y -qq pipx 2>&1 | tail -3 \
        || python3 -m pip install --user pipx 2>&1 | tail -3
    # Ensure pipx is on PATH for the rest of this script
    pipx ensurepath --force > /dev/null 2>&1 || true
    export PATH="$HOME/.local/bin:$PATH"
    ok "pipx installed"
}

ensure_ansible() {
    if command_exists ansible-playbook; then
        ok "Ansible available ($(ansible --version 2>/dev/null | head -1))"
        return
    fi
    info "Installing ansible-core via pipx"
    pipx install ansible-core 2>&1 | tail -3
    pipx ensurepath --force > /dev/null 2>&1 || true
    export PATH="$HOME/.local/bin:$PATH"
    if command_exists ansible-playbook; then
        ok "Ansible installed"
    else
        die "Failed to install ansible-core. Install manually: pipx install ansible-core"
    fi
}

ensure_collections() {
    info "Ensuring Ansible collections"
    ansible-galaxy collection install community.general ansible.posix --force-with-deps 2>&1 | tail -5
    ok "Ansible collections ready"
}

# ============================================================
# Get the repo (for the Ansible role + wizard)
# ============================================================

ensure_repo() {
    # If we're running from a cloned repo, use it directly
    if [ -f "$SCRIPT_DIR/local-playbook.yml" ] && [ -d "$SCRIPT_DIR/ansible" ]; then
        INSTALL_DIR="$SCRIPT_DIR"
        ok "Using local repo at $INSTALL_DIR"
        return
    fi

    # Otherwise, clone to a temp location
    local clone_dir="/tmp/istota-deploy"
    if [ -d "$clone_dir/.git" ]; then
        info "Updating deploy repo"
        git -C "$clone_dir" fetch origin --tags
        git -C "$clone_dir" reset --hard "origin/$REPO_BRANCH"
    else
        info "Cloning deploy repo"
        apt-get install -y -qq git 2>&1 | tail -1
        git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$clone_dir"
    fi
    INSTALL_DIR="$clone_dir/deploy"
    ok "Deploy repo ready at $INSTALL_DIR"
}

# ============================================================
# Settings → Ansible vars conversion
# ============================================================

convert_settings() {
    if [ ! -f "$SETTINGS_FILE" ]; then
        die "No settings file found at $SETTINGS_FILE. Re-run without --headless for guided setup, or pass --settings PATH."
    fi

    info "Converting settings to Ansible vars"
    python3 "$INSTALL_DIR/settings_to_vars.py" \
        --settings "$SETTINGS_FILE" \
        --output "$VARS_FILE"
    ok "Vars written to $VARS_FILE"
}

# ============================================================
# Run Ansible
# ============================================================

run_ansible() {
    local playbook="$INSTALL_DIR/local-playbook.yml"

    if [ ! -f "$playbook" ]; then
        die "Playbook not found at $playbook"
    fi

    local extra_args=()
    extra_args+=(--extra-vars "@$VARS_FILE")

    if [ "$UPDATE_ONLY" = true ]; then
        extra_args+=(--extra-vars "istota_update_only=true")
    fi

    if [ "$DRY_RUN" = true ]; then
        extra_args+=(--check --diff)
    fi

    section "Running Ansible"
    echo -e "  ${_BOLD}Playbook:${_RESET}  $playbook"
    echo -e "  ${_BOLD}Vars:${_RESET}      $VARS_FILE"
    echo -e "  ${_BOLD}Mode:${_RESET}      $([ "$DRY_RUN" = true ] && echo "dry-run" || ([ "$UPDATE_ONLY" = true ] && echo "update" || echo "full install"))"
    echo

    export ANSIBLE_STDOUT_CALLBACK=default
    export ANSIBLE_CALLBACK_RESULT_FORMAT=yaml

    ansible-playbook "$playbook" \
        --connection local \
        --inventory localhost, \
        "${extra_args[@]}"
}

# ============================================================
# Main
# ============================================================

main() {
    if [ "$DRY_RUN" = true ]; then
        # Dry-run only needs Python for the wizard and converter
        if [ "$(id -u)" -ne 0 ]; then
            die "This script must be run as root (or with sudo)"
        fi
        ensure_python
        ensure_repo

        # Run wizard to temp file
        local tmpdir
        tmpdir=$(mktemp -d "${TMPDIR:-/tmp}/istota-dry-run.XXXXXX")
        local orig_settings="$SETTINGS_FILE"
        SETTINGS_FILE="$tmpdir/settings.toml"
        VARS_FILE="$tmpdir/vars.yml"

        bash "$INSTALL_DIR/wizard.sh" --settings "$SETTINGS_FILE"
        python3 "$INSTALL_DIR/settings_to_vars.py" \
            --settings "$SETTINGS_FILE" \
            --output "$VARS_FILE"

        section "Dry-run Output"
        echo -e "  ${_BOLD}Settings:${_RESET}  $SETTINGS_FILE"
        echo -e "  ${_BOLD}Vars:${_RESET}      $VARS_FILE"
        echo
        echo -e "  ${_DIM}Inspect:${_RESET}"
        echo "    cat $SETTINGS_FILE"
        echo "    cat $VARS_FILE"
        echo
        echo -e "  ${_DIM}Clean up when done:${_RESET}"
        echo "    rm -rf $tmpdir"
        echo

        # If Ansible is available, also show check mode
        if command_exists ansible-playbook; then
            ensure_repo
            section "Ansible Check Mode"
            ansible-playbook "$INSTALL_DIR/local-playbook.yml" \
                --connection local \
                --inventory localhost, \
                --extra-vars "@$VARS_FILE" \
                --check --diff || true
        fi
        return
    fi

    preflight

    # Decide whether to run the wizard. Default is interactive (run it);
    # --headless or --update opts out. If we'd need to prompt but stdin
    # isn't a TTY (and no settings yet), bail with a clear message instead
    # of silently producing nothing.
    if [ "$UPDATE_ONLY" = true ]; then
        RUN_WIZARD=false
    elif [ "$HEADLESS" = true ]; then
        if [ ! -f "$SETTINGS_FILE" ]; then
            die "--headless requires existing settings at $SETTINGS_FILE.
  Run interactively first, or pass --settings /path/to/settings.toml."
        fi
        RUN_WIZARD=false
    elif [ ! -f "$SETTINGS_FILE" ]; then
        if [ -t 0 ]; then
            RUN_WIZARD=true
        else
            die "No settings file found and stdin is not a terminal.
  Re-run from a terminal, or pass --headless with --settings /path/to/settings.toml."
        fi
    elif [ -t 0 ]; then
        # Settings exist + interactive + TTY available → ask whether to overwrite
        echo
        read -rp "  Overwrite existing settings with new wizard? [y/N]: " overwrite
        case "$overwrite" in
            [yY]*) RUN_WIZARD=true ;;
            *)     RUN_WIZARD=false; info "Skipping wizard, using existing settings" ;;
        esac
    else
        # Settings exist + no TTY → just proceed with what we have
        RUN_WIZARD=false
    fi

    # Step 1: Ensure Python, pipx, Ansible
    section "Bootstrap"
    ensure_python
    ensure_pipx
    ensure_ansible
    ensure_collections

    # Step 2: Get the repo
    ensure_repo

    # Step 3: Run wizard if needed
    if [ "$RUN_WIZARD" = true ]; then
        bash "$INSTALL_DIR/wizard.sh" --settings "$SETTINGS_FILE"
    fi

    # Step 4: Convert settings to Ansible vars
    convert_settings

    # Step 5: Run Ansible
    run_ansible

    section "Done"
    if [ "$UPDATE_ONLY" = true ]; then
        ok "Update complete"
    else
        ok "Installation complete"
    fi
    echo
    echo -e "  ${_BOLD}Settings:${_RESET}  $SETTINGS_FILE"
    echo -e "  ${_BOLD}Service:${_RESET}   journalctl -u istota-scheduler -f"
    echo

    print_next_steps

    echo -e "  To update: ${_DIM}sudo bash install.sh --update${_RESET}"
    echo -e "  To reconfigure: ${_DIM}sudo bash install.sh${_RESET}"
    echo
}

# Pull a few values out of settings.toml so we can tailor the next-steps
# message (web UI enabled? oauth2 client filled in? hostname placeholder?).
# Best-effort — if Python or the file is unavailable we just print the
# generic version.
print_next_steps() {
    [ -f "$SETTINGS_FILE" ] || return 0
    command_exists python3 || return 0

    local meta
    meta=$(python3 - "$SETTINGS_FILE" <<'PY' 2>/dev/null || true
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as f:
        data = tomllib.load(f)
except Exception:
    sys.exit(0)
web = data.get("web") or {}
site = data.get("site") or {}
location = data.get("location") or {}
brain = data.get("brain") or {}
native = brain.get("native") or {}
print(data.get("nextcloud_url", "").rstrip("/"))
print(site.get("hostname", ""))
print("1" if web.get("enabled", True) else "0")
print("1" if (web.get("oauth2_client_id") and web.get("oauth2_client_secret")) else "0")
print("1" if data.get("claude_oauth_token") else "0")
print("1" if location.get("enabled") else "0")
print("1" if (data.get("secret_key") or "") else "0")
print(brain.get("kind", "claude_code"))
print("1" if native.get("api_key") else "0")
PY
)
    [ -z "$meta" ] && return 0

    local nc_url hostname web_on oauth_set claude_set location_on master_set brain_kind native_key_set
    nc_url=$(echo "$meta"     | sed -n '1p')
    hostname=$(echo "$meta"   | sed -n '2p')
    web_on=$(echo "$meta"     | sed -n '3p')
    oauth_set=$(echo "$meta"  | sed -n '4p')
    claude_set=$(echo "$meta" | sed -n '5p')
    location_on=$(echo "$meta"| sed -n '6p')
    master_set=$(echo "$meta" | sed -n '7p')
    brain_kind=$(echo "$meta" | sed -n '8p')
    native_key_set=$(echo "$meta" | sed -n '9p')

    section "Next Steps"

    local n=1

    # 1. DNS / TLS — always relevant when nginx config was rendered
    if [ "$web_on" = "1" ] || [ "$location_on" = "1" ]; then
        echo -e "  ${_BOLD}$n.${_RESET} Point DNS at this server"
        echo "     Make sure ${hostname:-<your hostname>} resolves here."
        echo
        n=$((n+1))

        echo -e "  ${_BOLD}$n.${_RESET} Obtain a TLS certificate (Let's Encrypt etc.)"
        echo "     The role rendered /etc/nginx/conf.d/${hostname:-<hostname>}.conf with"
        echo "     a self-signed cert. After running certbot, uncomment the"
        echo "     three ssl_certificate* lines in that file and remove the"
        echo "     'include /etc/nginx/snippets/snakeoil.conf;' line, then"
        echo "     'sudo systemctl reload nginx'."
        echo
        echo -e "     ${_DIM}Reverse-proxying, DNS, and certbot are out of scope for this installer.${_RESET}"
        echo
        n=$((n+1))
    fi

    # 2. OAuth2 client registration
    if [ "$web_on" = "1" ] && [ "$oauth_set" != "1" ]; then
        echo -e "  ${_BOLD}$n.${_RESET} Register a Nextcloud OAuth2 client (web UI is dark until this is done)"
        echo "     Visit ${nc_url:-<your-nextcloud>}/settings/admin/security"
        echo "     Under 'OAuth 2.0 clients' add one with:"
        echo "       Redirection URI: https://${hostname:-<hostname>}/istota/callback"
        echo "     Then put the Client ID and Secret into:"
        echo "       $SETTINGS_FILE  (oauth2_client_id, oauth2_client_secret under [web])"
        echo "     and re-run: ${_DIM}sudo bash install.sh --update${_RESET}"
        echo
        n=$((n+1))
    fi

    # 3. Model backend credential
    if [ "$brain_kind" = "native" ]; then
        if [ "$native_key_set" != "1" ]; then
            echo -e "  ${_BOLD}$n.${_RESET} Set the native brain's provider API key"
            echo "     The scheduler can't run tasks until the provider has a credential."
            echo "     Add it to $SETTINGS_FILE under [brain.native] as api_key and re-run"
            echo "     --update (the Claude CLI is not used with the native brain)."
            echo
            n=$((n+1))
        fi
    elif [ "$claude_set" != "1" ]; then
        echo -e "  ${_BOLD}$n.${_RESET} Authenticate the Claude CLI"
        echo "     The scheduler can't run tasks until Claude has a credential."
        echo "     Generate a token: ${_DIM}claude setup-token${_RESET}"
        echo "     Then add it to $SETTINGS_FILE as claude_oauth_token and re-run --update,"
        echo "     or log in directly on the server with:"
        echo "       ${_DIM}sudo -u istota HOME=/srv/app/istota claude login${_RESET}"
        echo
        n=$((n+1))
    fi

    # 4. Master secret key sanity check
    if [ "$master_set" != "1" ]; then
        echo -e "  ${_BOLD}$n.${_RESET} ${_YELLOW}!${_RESET} secret_key is unset — the encrypted secrets table is disabled"
        echo "     Connected services (karakeep, google_workspace, monarch tokens, etc.)"
        echo "     won't work until you generate one and put it in $SETTINGS_FILE:"
        echo "       ${_DIM}python3 -c 'import secrets; print(secrets.token_hex(32))'${_RESET}"
        echo
        n=$((n+1))
    fi

    # 5. Smoke test
    echo -e "  ${_BOLD}$n.${_RESET} Smoke test"
    if [ "$web_on" = "1" ] && [ "$oauth_set" = "1" ]; then
        echo "     • Open https://${hostname:-<hostname>}/istota and sign in."
    fi
    echo "     • Send a Talk DM to the bot user — it should reply within ~10s."
    echo "     • Tail the log: ${_DIM}journalctl -u istota-scheduler -f${_RESET}"
    echo
}

# Error trap
_on_error() {
    local exit_code=$? line_no="${BASH_LINENO[0]}"
    echo
    error "Failed at line $line_no (exit code $exit_code)"
    echo
    echo "  After fixing the issue, re-run:"
    echo "    sudo bash install.sh --update"
    echo
    exit "$exit_code"
}
trap _on_error ERR

main "$@"
