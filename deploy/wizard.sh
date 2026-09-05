#!/bin/bash
# Istota interactive setup wizard
# Writes a settings TOML file for use with install.sh and Ansible.
#
# Usage:
#   bash wizard.sh [--settings /path/to/settings.toml]
#
# This script is called by install.sh by default (skipped under --headless),
# but can also be run standalone to (re)generate a settings file.

set -euo pipefail

# Defaults
SETTINGS_FILE="${ISTOTA_SETTINGS_FILE:-/etc/istota/settings.toml}"
ISTOTA_HOME="${ISTOTA_HOME:-/srv/app/istota}"
ISTOTA_NAMESPACE="${ISTOTA_NAMESPACE:-istota}"
REPO_URL="${ISTOTA_REPO_URL:-https://github.com/istota-project/istota.git}"
REPO_BRANCH="${ISTOTA_REPO_BRANCH:-main}"

# Wizard state
_WIZ_NC_URL=""
_WIZ_NC_USERNAME=""
_WIZ_NC_APP_PASSWORD=""
_WIZ_USE_MOUNT=true
_WIZ_MOUNT_PATH="/srv/mount/nextcloud/content"
_WIZ_RCLONE_PASS_OBSCURED=""
_WIZ_BOT_NAME=""
_WIZ_EMAIL_ENABLED=false
_WIZ_BROWSER_ENABLED=false
_WIZ_BROWSER_VNC_PASSWORD=""
_WIZ_BROWSER_VNC_BIND_ADDRESS="127.0.0.1"
_WIZ_MEMORY_SEARCH_ENABLED=true
_WIZ_SLEEP_CYCLE_ENABLED=true
_WIZ_CHANNEL_SLEEP_ENABLED=true
_WIZ_WHISPER_ENABLED=true
_WIZ_WHISPER_MODEL="small"
_WIZ_LOCATION_ENABLED=false
_WIZ_WEBHOOKS_PORT=8765
_WIZ_BACKUP_ENABLED=true
_WIZ_USERS_BLOCK=""
_WIZ_ADMIN_BLOCK="admin_users = []"
_WIZ_EMAIL_IMAP_HOST=""
_WIZ_EMAIL_IMAP_USER=""
_WIZ_EMAIL_IMAP_PASSWORD=""
_WIZ_EMAIL_SMTP_HOST=""
_WIZ_EMAIL_BOT_ADDRESS=""
_WIZ_CLAUDE_TOKEN=""
_WIZ_USER_IDS=()
_WIZ_HOSTNAME=""
_WIZ_WEB_ENABLED=true
_WIZ_WEB_OAUTH2_CLIENT_ID=""
_WIZ_WEB_OAUTH2_CLIENT_SECRET=""
_WIZ_WEB_SECRET_KEY=""
_WIZ_SECRET_KEY=""
_WIZ_BRAIN_KIND="claude_code"
_WIZ_BRAIN_NATIVE_PROVIDER="openai_compat"
_WIZ_BRAIN_NATIVE_MODEL="claude-sonnet-4-6"
_WIZ_BRAIN_NATIVE_BASE_URL="https://api.anthropic.com/v1"
_WIZ_BRAIN_NATIVE_API_KEY=""
_WIZ_BRAIN_NATIVE_PROMPT_CACHING=true
_WIZ_BRAIN_ROLE_FAST=""
_WIZ_BRAIN_ROLE_GENERAL=""
_WIZ_BRAIN_ROLE_SMART=""
_WIZ_BRAIN_ROOM_SELECTABLE=""
# "derive" rather than "" — the two are different instructions. The Ansible
# default for istota_brain_fallback is an expression, not a literal: it works
# out `claude_code` for a tmux_claude deployment and "" for every other, and a
# settings key of any value at all outranks it. So an unanswered prompt must
# emit no key, and only an explicit answer writes one.
_WIZ_BRAIN_FALLBACK="derive"
_WIZ_TALK_SIGNALING_ENABLED=false
_WIZ_TALK_SIGNALING_URL=""
_WIZ_DEVELOPER_ENABLED=false
_WIZ_DEVELOPER_REPOS_DIR=""
_WIZ_DEVELOPER_GITLAB_URL="https://gitlab.com"
_WIZ_DEVELOPER_GITLAB_USERNAME=""
_WIZ_DEVELOPER_GITLAB_TOKEN=""
_WIZ_DEVELOPER_GITHUB_USERNAME=""
_WIZ_DEVELOPER_GITHUB_TOKEN=""
_WIZ_WEB_MAP_PROVIDER="openfreemap"
_WIZ_WEB_MAP_API_KEY=""
_WIZ_WEB_MAP_DARK_STYLE=""
_WIZ_WEB_MAP_LIGHT_STYLE=""
_WIZ_WEB_MAP_ATTRIBUTION=""

# ============================================================
# Output helpers
# ============================================================

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
dim()     { echo -e "${_DIM}  $*${_RESET}"; }

command_exists() {
    command -v "$1" &>/dev/null
}

# Generate a 64-char hex secret (32 random bytes). Falls back to /dev/urandom
# if Python is unavailable for some reason.
generate_hex_secret() {
    if command_exists python3; then
        python3 -c 'import secrets; print(secrets.token_hex(32))'
    else
        head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
        echo
    fi
}

# Escape a value for interpolation into a TOML basic string.
#
# Every operator-typed value in this file lands between `"` in a heredoc, which
# does no escaping of its own. A `"` in the value closes the string early and a
# trailing `\` escapes the closing quote; either corrupts settings.toml while
# the wizard still exits 0 and reports success. One variant is worse than a
# parse error because it is silent: `https://host" # rest` parses cleanly as a
# *truncated* value, so the deployment comes up pointed somewhere else with
# nothing to read.
#
# `docker/istota/render-config.sh:104` is the same helper for the Docker shape,
# and the reason this one is applied to the pre-existing values too rather than
# only to the settings added alongside it: a helper some of its callers do not
# use is a fix that has not landed.
#
# Backslash first — escaping it after the quote would double the backslash the
# quote-escape just introduced.
toml_escape() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '%s' "$value"
}

# Render a comma-separated operator value as a TOML array of strings.
# Prints nothing when every element is empty, so a caller's own `-n` test is
# not the only thing deciding whether the key gets written.
toml_string_list() {
    local rest="$1" item out=""
    while [ -n "$rest" ]; do
        case "$rest" in
            *,*) item="${rest%%,*}"; rest="${rest#*,}" ;;
            *)   item="$rest"; rest="" ;;
        esac
        item="${item#"${item%%[![:space:]]*}"}"
        item="${item%"${item##*[![:space:]]}"}"
        [ -n "$item" ] || continue
        [ -n "$out" ] && out="$out, "
        out="$out\"$(toml_escape "$item")\""
    done
    # `if` rather than a trailing `&&`: this is the function's last command, so
    # under `set -e` an empty list would make `x="$(...)"` abort the wizard
    # rather than assign the empty string.
    if [ -n "$out" ]; then
        printf '%s' "$out"
    fi
}

# ============================================================
# Input helpers
# ============================================================

prompt_value() {
    local varname="$1" prompt="$2" default="${3:-}"
    local value
    if [ -n "$default" ]; then
        read -rp "  $prompt [$default]: " value
        value="${value:-$default}"
    else
        read -rp "  $prompt: " value
    fi
    eval "$varname=\"\$value\""
}

prompt_bool() {
    local varname="$1" prompt="$2" default="${3:-n}"
    local value
    if [ "$default" = "y" ]; then
        read -rp "  $prompt [Y/n]: " value
        value="${value:-y}"
    else
        read -rp "  $prompt [y/N]: " value
        value="${value:-n}"
    fi
    case "$value" in
        [yY]*) eval "$varname=true" ;;
        *)     eval "$varname=false" ;;
    esac
}

prompt_secret() {
    local varname="$1" prompt="$2"
    local value
    read -rsp "  $prompt: " value
    echo
    eval "$varname=\"\$value\""
}

# ============================================================
# Parse arguments
# ============================================================

while [ $# -gt 0 ]; do
    case "$1" in
        --settings)     SETTINGS_FILE="$2"; shift 2 ;;
        --home)         ISTOTA_HOME="$2"; shift 2 ;;
        --namespace)    ISTOTA_NAMESPACE="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: wizard.sh [--settings PATH] [--home PATH] [--namespace NAME]"
            exit 0 ;;
        *) die "Unknown option: $1" ;;
    esac
done

# ============================================================
# Wizard sections
# ============================================================

# Pull keys we never want to rotate (master secret key, web session key)
# out of an existing settings file so re-running the wizard doesn't
# silently lock the operator out of encrypted secrets.
wiz_load_existing_secrets() {
    [ -f "$SETTINGS_FILE" ] || return 0
    if ! command_exists python3; then
        return 0
    fi
    local extracted
    extracted=$(python3 - "$SETTINGS_FILE" <<'PY' 2>/dev/null || true
import sys, tomllib
try:
    with open(sys.argv[1], "rb") as f:
        data = tomllib.load(f)
except Exception:
    sys.exit(0)
print((data.get("secret_key") or "").strip())
print(((data.get("web") or {}).get("secret_key") or "").strip())
PY
)
    local existing_master existing_web
    existing_master=$(echo "$extracted" | sed -n '1p')
    existing_web=$(echo "$extracted" | sed -n '2p')
    if [ -n "$existing_master" ]; then
        _WIZ_SECRET_KEY="$existing_master"
        ok "Preserving existing secrets master key"
    fi
    if [ -n "$existing_web" ]; then
        _WIZ_WEB_SECRET_KEY="$existing_web"
        ok "Preserving existing web session key"
    fi
}

wiz_basics() {
    section "1. Basics"

    dim "Choose carefully — this is the name your bot will go by, in Talk,"
    dim "in emails, on the web. It also defines workspace folder names"
    dim "(e.g. /Users/<you>/<bot_name>/), so changing it later would orphan"
    dim "memories, briefings and skill data already saved under the old name."
    dim "(You wouldn't rename your child or pet either.)"
    echo
    prompt_value _WIZ_BOT_NAME "Bot name (user-facing identity)" "Istota"
    prompt_value ISTOTA_HOME "Install directory" "$ISTOTA_HOME"

    echo
    dim "Advanced: namespace sets the system user, group, and service names."
    local customize_ns
    prompt_bool customize_ns "Customize namespace?" "n"
    if [ "$customize_ns" = "true" ]; then
        prompt_value ISTOTA_NAMESPACE "Namespace" "$ISTOTA_NAMESPACE"
    fi
}

wiz_nextcloud() {
    section "2. Nextcloud Connection"

    dim "Istota needs a Nextcloud user account to operate."
    dim "Create a dedicated user (e.g. 'istota') and generate an app password"
    dim "in Nextcloud > Settings > Security > Devices & sessions."
    echo

    while true; do
        prompt_value _WIZ_NC_URL "Nextcloud URL" ""
        # Normalize: strip trailing slash
        _WIZ_NC_URL="${_WIZ_NC_URL%/}"

        if [[ ! "$_WIZ_NC_URL" =~ ^https?:// ]]; then
            warn "URL should start with https://. Prepending..."
            _WIZ_NC_URL="https://$_WIZ_NC_URL"
        fi

        # Test connectivity
        echo -n "  Testing connection... "
        if curl -sf --max-time 10 "$_WIZ_NC_URL/status.php" > /dev/null 2>&1; then
            echo -e "${_GREEN}OK${_RESET}"
            break
        else
            echo -e "${_RED}FAILED${_RESET}"
            warn "Could not reach $_WIZ_NC_URL/status.php"
            local retry
            prompt_bool retry "Try again?" "y"
            [ "$retry" = "false" ] && break
        fi
    done

    prompt_value _WIZ_NC_USERNAME "Bot's Nextcloud username" "$ISTOTA_NAMESPACE"

    while true; do
        prompt_secret _WIZ_NC_APP_PASSWORD "App password"
        if [ -z "$_WIZ_NC_APP_PASSWORD" ]; then
            warn "App password is required"
            continue
        fi

        # Test authentication
        echo -n "  Verifying credentials... "
        local http_code
        http_code=$(curl -sf --max-time 10 -o /dev/null -w "%{http_code}" \
            -u "$_WIZ_NC_USERNAME:$_WIZ_NC_APP_PASSWORD" \
            -H "OCS-APIRequest: true" \
            "$_WIZ_NC_URL/ocs/v1.php/cloud/users/$_WIZ_NC_USERNAME?format=json" 2>/dev/null || echo "000")

        if [ "$http_code" = "200" ]; then
            echo -e "${_GREEN}OK${_RESET}"
            break
        elif [ "$http_code" = "401" ]; then
            echo -e "${_RED}FAILED${_RESET}"
            warn "Authentication failed. Check username and app password."
            local retry
            prompt_bool retry "Try again?" "y"
            [ "$retry" = "false" ] && break
        else
            echo -e "${_YELLOW}UNKNOWN (HTTP $http_code)${_RESET}"
            warn "Could not verify credentials (may still work). Continuing."
            break
        fi
    done
}

wiz_mount() {
    section "3. File Access (rclone Mount)"

    dim "Istota accesses Nextcloud files via a FUSE mount using rclone."
    dim "This is strongly recommended for full functionality."
    echo

    prompt_bool _WIZ_USE_MOUNT "Enable Nextcloud file mount?" "y"
    if [ "$_WIZ_USE_MOUNT" = "true" ]; then
        prompt_value _WIZ_MOUNT_PATH "Mount path" "/srv/mount/nextcloud/content"
        echo
        dim "The rclone obscured password will be generated automatically"
        dim "from the app password during installation."
    fi
}

wiz_users() {
    section "4. Users"

    dim "Define the Nextcloud users who will interact with istota."
    dim "Enter a blank user ID when finished."
    echo

    _WIZ_USERS_BLOCK=""
    _WIZ_USER_IDS=()
    local first_user=true

    while true; do
        local uid uname utz uemail
        if [ "$first_user" = true ]; then
            prompt_value uid "User ID (Nextcloud username, e.g. alice)" ""
        else
            prompt_value uid "Another user ID (blank to finish)" ""
        fi
        [ -z "$uid" ] && break

        prompt_value uname "Display name" "$uid"
        prompt_value utz "Timezone" "UTC"
        prompt_value uemail "Email address (optional)" ""

        # Escaped as the block is built, since wiz_write_settings escapes
        # scalars and this is already-assembled TOML by the time it gets there.
        # The user id is a bare key as well as a value; a `"` or a `.` in one
        # would forge a table header, so it goes through the same helper.
        _WIZ_USERS_BLOCK+="
[users.\"$(toml_escape "$uid")\"]
display_name = \"$(toml_escape "$uname")\"
timezone = \"$(toml_escape "$utz")\"
"
        if [ -n "$uemail" ]; then
            _WIZ_USERS_BLOCK+="email_addresses = [\"$(toml_escape "$uemail")\"]
"
        fi

        _WIZ_USER_IDS+=("$uid")
        first_user=false
        echo
    done

    if [ ${#_WIZ_USER_IDS[@]} -eq 0 ]; then
        warn "No users defined. You can add users later in the settings file."
    fi

    # Admin users
    echo
    if [ ${#_WIZ_USER_IDS[@]} -le 1 ]; then
        dim "With one user, they're automatically an admin."
        _WIZ_ADMIN_BLOCK="admin_users = []"
    else
        dim "Admin users get full system access (DB, all files, admin-only skills)."
        dim "Leave blank to make all users admins."
        local admin_line
        prompt_value admin_line "Admin user IDs (comma-separated)" ""
        # Through toml_string_list rather than the sed pipeline this replaces,
        # which quoted by substitution: a `"` in an id closed the string early,
        # and a trailing comma produced an empty element that reads as a user.
        _WIZ_ADMIN_BLOCK="admin_users = [$(toml_string_list "$admin_line")]"
    fi
}

wiz_features() {
    section "5. Optional Features"

    dim "Configure additional capabilities. All can be changed later."
    echo

    # Email
    prompt_bool _WIZ_EMAIL_ENABLED "Enable email integration?" "n"
    if [ "$_WIZ_EMAIL_ENABLED" = "true" ]; then
        echo
        prompt_value _WIZ_EMAIL_IMAP_HOST "IMAP host" ""
        prompt_value _WIZ_EMAIL_IMAP_USER "IMAP username" ""
        prompt_secret _WIZ_EMAIL_IMAP_PASSWORD "IMAP password"
        prompt_value _WIZ_EMAIL_SMTP_HOST "SMTP host" "$_WIZ_EMAIL_IMAP_HOST"
        prompt_value _WIZ_EMAIL_BOT_ADDRESS "Bot email address" "$_WIZ_EMAIL_IMAP_USER"
        echo
    fi

    # Memory search
    echo
    dim "Memory search enables semantic search over conversations and memories."
    dim "Requires ~2GB disk for PyTorch + sentence-transformers."
    prompt_bool _WIZ_MEMORY_SEARCH_ENABLED "Enable memory search?" "y"

    # Sleep cycle
    echo
    dim "Sleep cycle extracts daily memories from conversations overnight."
    prompt_bool _WIZ_SLEEP_CYCLE_ENABLED "Enable nightly memory extraction?" "y"

    # Channel sleep cycle
    if [ "$_WIZ_SLEEP_CYCLE_ENABLED" = "true" ]; then
        echo
        dim "Channel sleep cycle extracts shared context from group conversations."
        prompt_bool _WIZ_CHANNEL_SLEEP_ENABLED "Enable channel memory extraction?" "y"
    else
        _WIZ_CHANNEL_SLEEP_ENABLED=false
    fi

    # Whisper
    echo
    dim "Whisper provides audio-to-text transcription via faster-whisper."
    dim "Requires ~1-2GB disk depending on model size."
    prompt_bool _WIZ_WHISPER_ENABLED "Enable audio transcription?" "y"
    if [ "$_WIZ_WHISPER_ENABLED" = "true" ]; then
        echo
        dim "Model sizes: tiny (~75MB), base (~150MB), small (~500MB), medium (~1.5GB)"
        prompt_value _WIZ_WHISPER_MODEL "Whisper model" "small"
    fi

    # ntfy is now configured per-user via the web settings UI (or
    # `istota secret ensure -s ntfy ...`); the operator wizard skips it.

    # Location tracking
    echo
    dim "GPS location tracking via Overland app (webhook receiver)."
    prompt_bool _WIZ_LOCATION_ENABLED "Enable GPS location tracking?" "n"
    if [ "$_WIZ_LOCATION_ENABLED" = "true" ]; then
        echo
        prompt_value _WIZ_WEBHOOKS_PORT "Webhook receiver port" "8765"
    fi

    wiz_developer

    # Talk signaling
    echo
    dim "Talk signaling turns inbound Talk from a poll into a push, which is a"
    dim "large drop in load on Nextcloud. There is no credential here: istota"
    dim "authenticates as its own Nextcloud user and Talk mints the rest."
    echo
    dim "It needs a standalone signaling server already registered with your"
    dim "Nextcloud. This role installs no such server — check for one with:"
    dim "  occ talk:signaling:list"
    echo
    dim "Switching this on without a registered server, istota refuses to"
    dim "start rather than polling quietly while you believe push is live."
    prompt_bool _WIZ_TALK_SIGNALING_ENABLED "Receive Talk over a signaling server?" "n"
    if [ "$_WIZ_TALK_SIGNALING_ENABLED" = "true" ]; then
        echo
        dim "Leave the URL blank for the normal case — istota then reads the"
        dim "server address out of Talk's own settings. Set one only where this"
        dim "host must reach the server by a different route than browsers do."
        prompt_value _WIZ_TALK_SIGNALING_URL "Signaling URL override (blank = use Talk's)" ""
    fi

    # Backups
    echo
    dim "Automated backups of the database and Nextcloud files with rotation."
    prompt_bool _WIZ_BACKUP_ENABLED "Enable automated backups?" "y"

    # Browser
    echo
    dim "Browser container provides web browsing capability via Docker."
    prompt_bool _WIZ_BROWSER_ENABLED "Enable web browser container?" "n"
    if [ "$_WIZ_BROWSER_ENABLED" = "true" ]; then
        if ! command_exists docker; then
            warn "Docker not found. It will be installed during deployment."
        fi
        echo
        echo "  The browser viewer is an interactive view of the browser holding"
        echo "  your logged-in profile. It listens on 127.0.0.1 by default; give"
        echo "  it a VPN or management address only if you need to reach it from"
        echo "  another machine, and set a password when you do."
        prompt_value _WIZ_BROWSER_VNC_BIND_ADDRESS \
            "Address to publish the browser viewer on" "127.0.0.1"
        prompt_secret _WIZ_BROWSER_VNC_PASSWORD "VNC password for browser viewer"
        case "$_WIZ_BROWSER_VNC_BIND_ADDRESS" in
            127.*|::1|'[::1]'|localhost) ;;
            *)
                if [ -z "$_WIZ_BROWSER_VNC_PASSWORD" ]; then
                    warn "A reachable viewer address needs a password; the deploy will refuse without one."
                fi
                ;;
        esac
    fi
}

# The developer skill's own prompts, a function rather than inline in
# wiz_features so the give-up branch below can be driven by a test. That branch
# is the reason this section exists: tasks/main.yml asserts a forge token when
# the skill is on, so a settings file with `enabled = true` and no token is one
# the play refuses.
wiz_developer() {
    echo
    dim "The developer skill clones repositories and opens merge/pull requests"
    dim "on a forge. It needs a GitLab or GitHub token to do anything at all,"
    dim "and the deploy asserts one is set — so this asks for both together."
    dim "Use a dedicated bot account with the narrowest scopes that work."
    prompt_bool _WIZ_DEVELOPER_ENABLED "Enable the developer skill?" "n"
    if [ "$_WIZ_DEVELOPER_ENABLED" = "true" ]; then
        while true; do
            echo
            dim "Clones and worktrees live under this directory, one subtree per"
            dim "user. It should be on the same filesystem as the install."
            prompt_value _WIZ_DEVELOPER_REPOS_DIR "Repos directory" \
                "${_WIZ_DEVELOPER_REPOS_DIR:-$ISTOTA_HOME/repos}"
            echo
            dim "GitLab (blank username and token to skip this forge):"
            prompt_value _WIZ_DEVELOPER_GITLAB_URL "GitLab URL" "$_WIZ_DEVELOPER_GITLAB_URL"
            prompt_value _WIZ_DEVELOPER_GITLAB_USERNAME "GitLab username" "$_WIZ_DEVELOPER_GITLAB_USERNAME"
            prompt_secret _WIZ_DEVELOPER_GITLAB_TOKEN "GitLab token (api + write_repository)"
            echo
            dim "GitHub (blank username and token to skip this forge):"
            prompt_value _WIZ_DEVELOPER_GITHUB_USERNAME "GitHub username" "$_WIZ_DEVELOPER_GITHUB_USERNAME"
            prompt_secret _WIZ_DEVELOPER_GITHUB_TOKEN "GitHub token (repo scope)"

            # The role asserts at least one token when the skill is on, and
            # fails the play if it has neither. Settle that here rather than
            # writing settings that cannot deploy.
            if [ -n "$_WIZ_DEVELOPER_GITLAB_TOKEN" ] || [ -n "$_WIZ_DEVELOPER_GITHUB_TOKEN" ]; then
                break
            fi
            echo
            warn "No token given for either forge. The deploy refuses this"
            warn "  combination, so the skill has to be left off without one."
            local retry_token
            prompt_bool retry_token "Enter a token now?" "y"
            if [ "$retry_token" = "false" ]; then
                _WIZ_DEVELOPER_ENABLED=false
                warn "Developer skill left off. Add a token to $SETTINGS_FILE"
                warn "  and set developer.enabled = true to turn it on later."
                break
            fi
        done
    fi
}

wiz_hostname() {
    section "6. Public Hostname"

    dim "Several features need a public DNS name pointing at this server:"
    dim "  • web UI (Nextcloud OAuth2 redirect must be HTTPS)"
    dim "  • GPS location webhook (Overland posts here)"
    echo
    dim "The role installs nginx and renders /etc/nginx/conf.d/<hostname>.conf"
    dim "with a self-signed snakeoil cert. DNS, Let's Encrypt, and any extra"
    dim "reverse-proxy plumbing are out of scope — you handle them after install."
    echo
    dim "Enter a placeholder if you don't have DNS yet; you can edit"
    dim "$SETTINGS_FILE and re-run with --update later."
    echo

    local default_hostname="${ISTOTA_NAMESPACE}.example.com"
    prompt_value _WIZ_HOSTNAME "Public hostname" "$default_hostname"
    # Strip protocol if user pasted a URL by mistake
    _WIZ_HOSTNAME="${_WIZ_HOSTNAME#https://}"
    _WIZ_HOSTNAME="${_WIZ_HOSTNAME#http://}"
    _WIZ_HOSTNAME="${_WIZ_HOSTNAME%/}"
}

wiz_web_ui() {
    section "7. Web UI (Nextcloud OAuth2)"

    dim "The web UI authenticates users via your Nextcloud's built-in OAuth2"
    dim "provider. Disable it here if you only want the Talk/email/CLI surfaces."
    echo

    prompt_bool _WIZ_WEB_ENABLED "Enable web UI?" "y"
    if [ "$_WIZ_WEB_ENABLED" != "true" ]; then
        return
    fi

    echo
    dim "Before istota can complete the OAuth2 handshake, register a client in"
    dim "your Nextcloud (admin login required):"
    echo
    dim "  1. Open  $_WIZ_NC_URL/settings/admin/security"
    dim "  2. Under 'OAuth 2.0 clients', click 'Add client'"
    dim "  3. Name:          ${_WIZ_BOT_NAME:-Istota}"
    dim "     Redirection URI: https://$_WIZ_HOSTNAME/istota/callback"
    dim "  4. Copy the generated Client Identifier and Secret"
    echo
    dim "You can paste them now, or skip and fill them into $SETTINGS_FILE later"
    dim "(istota will install but log in won't work until they're set)."
    echo

    local have_oauth
    prompt_bool have_oauth "Paste OAuth2 client credentials now?" "y"
    if [ "$have_oauth" = "true" ]; then
        prompt_value _WIZ_WEB_OAUTH2_CLIENT_ID "Client ID" ""
        prompt_secret _WIZ_WEB_OAUTH2_CLIENT_SECRET "Client secret"
    fi

    # Auto-generate the session signing key — there's no reason to make the
    # operator type a 64-char hex string.
    _WIZ_WEB_SECRET_KEY="$(generate_hex_secret)"
    ok "Generated session signing key"

    echo
    dim "Map background tiles, used by the location views. The default needs no"
    dim "key and no account. 'carto' needs a free key — without one every tile"
    dim "comes back watermarked, with a 200 status, so nothing detects it for"
    dim "you. 'custom' takes your own MapLibre style URLs."
    echo
    while true; do
        prompt_value _WIZ_WEB_MAP_PROVIDER \
            "Basemap provider (openfreemap | carto | osm | custom)" "openfreemap"
        case "$_WIZ_WEB_MAP_PROVIDER" in
            openfreemap|carto|osm|custom) break ;;
            *) warn "Unknown provider '$_WIZ_WEB_MAP_PROVIDER'. Pick one of the four." ;;
        esac
    done
    if [ "$_WIZ_WEB_MAP_PROVIDER" = "carto" ]; then
        echo
        dim "Get one at https://carto.com/basemaps/apikey/"
        # Read in the clear on purpose. MapLibre puts this key in the tile URL,
        # so it ships to every browser that loads a map — it is public, not a
        # secret, and prompt_secret here would tell the operator otherwise.
        prompt_value _WIZ_WEB_MAP_API_KEY "CARTO API key" ""
        if [ -z "$_WIZ_WEB_MAP_API_KEY" ]; then
            warn "No key given — the map falls back to openfreemap at runtime."
        fi
    elif [ "$_WIZ_WEB_MAP_PROVIDER" = "custom" ]; then
        echo
        dim "MapLibre style URLs. Give at least one; the other reuses it."
        prompt_value _WIZ_WEB_MAP_DARK_STYLE "Dark style URL" ""
        prompt_value _WIZ_WEB_MAP_LIGHT_STYLE "Light style URL" ""
        prompt_value _WIZ_WEB_MAP_ATTRIBUTION "Attribution (HTML)" ""
        if [ -z "$_WIZ_WEB_MAP_DARK_STYLE" ] && [ -z "$_WIZ_WEB_MAP_LIGHT_STYLE" ]; then
            warn "No style URL given — the map falls back to openfreemap at runtime."
        fi
    fi
}

wiz_secrets_store() {
    # Master key for the encrypted secrets table. Always auto-generated;
    # the operator never sees it but it's persisted in the settings file
    # (mode 0600) so re-running the wizard or --update keeps the same key.
    if [ -z "$_WIZ_SECRET_KEY" ]; then
        _WIZ_SECRET_KEY="$(generate_hex_secret)"
    fi
}

wiz_brain() {
    section "8. Model Backend (Brain)"

    dim "Istota can drive the model two ways:"
    dim "  • claude_code (default) — shells out to the Claude CLI, using your"
    dim "    Claude.ai subscription or OAuth token. No API key required."
    dim "  • native — runs istota's own agent loop in-process against an"
    dim "    OpenAI-compatible / Anthropic API endpoint. Needs an API key and"
    dim "    bills per token."
    echo
    dim "Full runbook: docs/configuration/native-brain.md"
    echo

    local use_native
    prompt_bool use_native "Use the native brain instead of the Claude CLI?" "n"
    if [ "$use_native" != "true" ]; then
        _WIZ_BRAIN_KIND="claude_code"
        return
    fi

    _WIZ_BRAIN_KIND="native"
    echo
    dim "The native brain talks to any OpenAI-compatible endpoint. The default"
    dim "below targets Anthropic's API directly. The model id is explicit —"
    dim "the openai_compat provider does no alias resolution."
    prompt_value _WIZ_BRAIN_NATIVE_BASE_URL "Provider base URL" "$_WIZ_BRAIN_NATIVE_BASE_URL"
    prompt_value _WIZ_BRAIN_NATIVE_MODEL "Model id" "$_WIZ_BRAIN_NATIVE_MODEL"
    prompt_secret _WIZ_BRAIN_NATIVE_API_KEY "Provider API key"
    echo
    dim "Prompt caching cuts cost on Anthropic / OpenRouter endpoints; disable"
    dim "it for providers that don't support cache_control breakpoints."
    prompt_bool _WIZ_BRAIN_NATIVE_PROMPT_CACHING "Enable prompt caching?" "y"

    echo
    dim "Internal subsystems pick a model by role: fast (conversation selection,"
    dim "routing), general (sleep-cycle extraction, OCR), smart (heavy work,"
    dim "!model smart). By default all three use the model above."
    local diff_roles
    prompt_bool diff_roles "Use a different model for any role?" "n"
    if [ "$diff_roles" = "true" ]; then
        dim "Each must be served by the same endpoint. Blank = use the default."
        prompt_value _WIZ_BRAIN_ROLE_FAST    "fast model"    "$_WIZ_BRAIN_NATIVE_MODEL"
        prompt_value _WIZ_BRAIN_ROLE_GENERAL "general model" "$_WIZ_BRAIN_NATIVE_MODEL"
        prompt_value _WIZ_BRAIN_ROLE_SMART   "smart model"   "$_WIZ_BRAIN_NATIVE_MODEL"
    fi
}

# Still section 8, and a separate function because wiz_brain returns early on
# the claude_code path — these two questions apply whichever kind was picked.
wiz_brain_policy() {
    echo
    dim "A room can pin the brain it runs on, with !brain or the web room"
    dim "settings, and a scheduled job can pin one in CRON.md. Which kinds may"
    dim "be pinned is an allowlist, and it is empty by default: brain kind"
    dim "decides which credentials a task carries and which sandbox is built"
    dim "around it, so a kind is listed only where you mean to permit it."
    dim "Writing a pin is admin-only on top of this. Listing a kind also widens"
    dim "the doctor checks for it, whether or not anything pins it."
    echo
    local selectable_line
    prompt_value selectable_line \
        "Brain kinds a room or job may pin (comma-separated, blank for none)" ""
    # Nothing here judges whether a name is a real brain kind, which is the same
    # call render-config.sh makes for this same setting: `config` warns once at
    # load about a name `make_brain` cannot build, and a second opinion in a
    # shell script would only go out of date when a fourth kind ships. The first
    # draft of this did validate, and dropped anything it did not recognise —
    # so an operator would have asked for a permission and silently not got it.
    _WIZ_BRAIN_ROOM_SELECTABLE="$(toml_string_list "$selectable_line")"

    echo
    dim "Availability failover: when the brain above cannot run a task (usage"
    dim "limit, missing binary, tmux launch failure), the task runs on another"
    dim "kind instead. A pinned room or job never fails over — it runs what it"
    dim "names or fails saying why."
    echo
    dim "Leave this at 'derive' unless you have a reason. The role works the"
    dim "answer out from the brain kind, and any answer here overrides that."
    dim "'none' and 'derive' differ only when the brain above is tmux_claude,"
    dim "which is the one kind the derivation gives a failover to."
    # Same reasoning as the allowlist above: `derive` and `none` are this
    # wizard's own sentinels and are read here, but a brain kind is passed
    # through for config to validate rather than checked against a list that
    # goes stale.
    prompt_value _WIZ_BRAIN_FALLBACK \
        "Fallback brain (derive | none | claude_code | native | tmux_claude)" "derive"
}

wiz_claude_auth() {
    section "9. Claude Authentication"

    if [ "$_WIZ_BRAIN_KIND" = "native" ]; then
        dim "Native brain selected — the Claude CLI isn't used, so no Claude"
        dim "login is required. The provider API key you entered is used instead."
        return
    fi

    dim "Istota uses the Claude CLI which needs authentication."
    dim "You can either provide an OAuth token now, or authenticate"
    dim "interactively after installation."
    echo

    local has_token
    prompt_bool has_token "Do you have a Claude OAuth token?" "n"
    if [ "$has_token" = "true" ]; then
        prompt_secret _WIZ_CLAUDE_TOKEN "Claude OAuth token"
    else
        dim "You'll authenticate after installation with:"
        dim "  sudo -u $ISTOTA_NAMESPACE HOME=$ISTOTA_HOME claude login"
    fi
}

wiz_review() {
    section "10. Review Configuration"

    echo -e "  ${_BOLD}Bot name:${_RESET}          $_WIZ_BOT_NAME"
    echo -e "  ${_BOLD}Install dir:${_RESET}       $ISTOTA_HOME"
    echo -e "  ${_BOLD}Namespace:${_RESET}         $ISTOTA_NAMESPACE"
    echo
    echo -e "  ${_BOLD}Nextcloud URL:${_RESET}     $_WIZ_NC_URL"
    echo -e "  ${_BOLD}NC username:${_RESET}       $_WIZ_NC_USERNAME"
    echo -e "  ${_BOLD}NC app password:${_RESET}   ****"
    echo
    echo -e "  ${_BOLD}File mount:${_RESET}        $_WIZ_USE_MOUNT"
    if [ "$_WIZ_USE_MOUNT" = "true" ]; then
        echo -e "  ${_BOLD}Mount path:${_RESET}        $_WIZ_MOUNT_PATH"
    fi
    echo
    if [ ${#_WIZ_USER_IDS[@]} -gt 0 ]; then
        echo -e "  ${_BOLD}Users:${_RESET}             ${_WIZ_USER_IDS[*]}"
    else
        echo -e "  ${_BOLD}Users:${_RESET}             (none defined)"
    fi
    echo
    echo -e "  ${_BOLD}Email:${_RESET}             $_WIZ_EMAIL_ENABLED"
    echo -e "  ${_BOLD}Memory search:${_RESET}     $_WIZ_MEMORY_SEARCH_ENABLED"
    echo -e "  ${_BOLD}Sleep cycle:${_RESET}       $_WIZ_SLEEP_CYCLE_ENABLED"
    echo -e "  ${_BOLD}Channel sleep:${_RESET}     $_WIZ_CHANNEL_SLEEP_ENABLED"
    echo -e "  ${_BOLD}Whisper:${_RESET}           $_WIZ_WHISPER_ENABLED$([ "$_WIZ_WHISPER_ENABLED" = "true" ] && echo " (model: $_WIZ_WHISPER_MODEL)")"
    echo -e "  ${_BOLD}Location:${_RESET}          $_WIZ_LOCATION_ENABLED$([ "$_WIZ_LOCATION_ENABLED" = "true" ] && echo " (port: $_WIZ_WEBHOOKS_PORT)")"
    echo -e "  ${_BOLD}Backups:${_RESET}           $_WIZ_BACKUP_ENABLED"
    echo -e "  ${_BOLD}Browser:${_RESET}           $_WIZ_BROWSER_ENABLED"
    echo -e "  ${_BOLD}Developer skill:${_RESET}   $_WIZ_DEVELOPER_ENABLED"
    if [ "$_WIZ_DEVELOPER_ENABLED" = "true" ]; then
        local forges=""
        [ -n "$_WIZ_DEVELOPER_GITLAB_TOKEN" ] && forges="gitlab"
        [ -n "$_WIZ_DEVELOPER_GITHUB_TOKEN" ] && forges="${forges:+$forges, }github"
        echo -e "  ${_BOLD}Forge tokens:${_RESET}      $forges"
        echo -e "  ${_BOLD}Repos dir:${_RESET}         $_WIZ_DEVELOPER_REPOS_DIR"
    fi
    echo -e "  ${_BOLD}Talk signaling:${_RESET}    $_WIZ_TALK_SIGNALING_ENABLED"
    echo
    echo -e "  ${_BOLD}Hostname:${_RESET}          $_WIZ_HOSTNAME"
    echo -e "  ${_BOLD}Web UI:${_RESET}            $_WIZ_WEB_ENABLED"
    if [ "$_WIZ_WEB_ENABLED" = "true" ]; then
        local oauth_status
        if [ -n "$_WIZ_WEB_OAUTH2_CLIENT_ID" ] && [ -n "$_WIZ_WEB_OAUTH2_CLIENT_SECRET" ]; then
            oauth_status="configured"
        else
            oauth_status="${_YELLOW}set later in $SETTINGS_FILE${_RESET}"
        fi
        echo -e "  ${_BOLD}OAuth2:${_RESET}            $oauth_status"
        echo -e "  ${_BOLD}Basemap:${_RESET}           $_WIZ_WEB_MAP_PROVIDER$([ "$_WIZ_WEB_MAP_PROVIDER" = "carto" ] && { [ -n "$_WIZ_WEB_MAP_API_KEY" ] && echo " (key set)" || echo " ${_YELLOW}(no key — falls back to openfreemap)${_RESET}"; })"
    fi
    echo -e "  ${_BOLD}Secrets master key:${_RESET} auto-generated (stored in $SETTINGS_FILE)"
    echo -e "  ${_BOLD}Brain:${_RESET}             $_WIZ_BRAIN_KIND$([ "$_WIZ_BRAIN_KIND" = "native" ] && echo " (model: ${_WIZ_BRAIN_NATIVE_MODEL:-unset}, $_WIZ_BRAIN_NATIVE_BASE_URL)")"
    if [ "$_WIZ_BRAIN_KIND" = "native" ]; then
        echo -e "  ${_BOLD}Provider key:${_RESET}      $([ -n "$_WIZ_BRAIN_NATIVE_API_KEY" ] && echo "provided" || echo "${_YELLOW}set later in $SETTINGS_FILE${_RESET}")"
    else
        echo -e "  ${_BOLD}Claude token:${_RESET}      $([ -n "$_WIZ_CLAUDE_TOKEN" ] && echo "provided" || echo "authenticate later")"
    fi
    echo -e "  ${_BOLD}Room-pinnable:${_RESET}     ${_WIZ_BRAIN_ROOM_SELECTABLE:-(none)}"
    echo -e "  ${_BOLD}Brain fallback:${_RESET}    $_WIZ_BRAIN_FALLBACK$([ "$_WIZ_BRAIN_FALLBACK" = "derive" ] && echo " (worked out from the brain kind)")"
    echo

    # Repeated here because it is the one answer above that stops the daemon
    # starting rather than degrading, and the review screen is the last place
    # to change it.
    if [ "$_WIZ_TALK_SIGNALING_ENABLED" = "true" ]; then
        warn "Talk signaling is on. If no signaling server is registered with"
        warn "  your Nextcloud (occ talk:signaling:list), istota will refuse to"
        warn "  start. Register one, or answer no to that question."
        echo
    fi

    local confirm
    prompt_bool confirm "Proceed with installation?" "y"
    if [ "$confirm" = "false" ]; then
        die "Installation cancelled"
    fi
}

wiz_write_settings() {
    section "Writing Settings"

    local settings_dir
    settings_dir="$(dirname "$SETTINGS_FILE")"
    mkdir -p "$settings_dir"

    # Escape every operator-typed scalar before it reaches the heredoc below.
    # In place rather than into copies: wiz_write_settings is the last thing
    # main() runs, and nothing reads these afterwards. Values generated by this
    # script (the hex secrets) go through it too — they cannot contain either
    # character, and listing them costs nothing next to remembering which is
    # which. _WIZ_BRAIN_ROOM_SELECTABLE, _WIZ_USERS_BLOCK and _WIZ_ADMIN_BLOCK
    # are deliberately absent: each is already assembled TOML, escaped as it
    # was built, and escaping it again would quote its own delimiters.
    local _tv
    for _tv in \
        ISTOTA_HOME ISTOTA_NAMESPACE REPO_URL REPO_BRANCH \
        _WIZ_BOT_NAME _WIZ_NC_URL _WIZ_NC_USERNAME _WIZ_NC_APP_PASSWORD \
        _WIZ_MOUNT_PATH _WIZ_RCLONE_PASS_OBSCURED \
        _WIZ_CLAUDE_TOKEN _WIZ_SECRET_KEY _WIZ_HOSTNAME \
        _WIZ_WEB_OAUTH2_CLIENT_ID _WIZ_WEB_OAUTH2_CLIENT_SECRET _WIZ_WEB_SECRET_KEY \
        _WIZ_WEB_MAP_PROVIDER _WIZ_WEB_MAP_API_KEY _WIZ_WEB_MAP_DARK_STYLE \
        _WIZ_WEB_MAP_LIGHT_STYLE _WIZ_WEB_MAP_ATTRIBUTION \
        _WIZ_EMAIL_IMAP_HOST _WIZ_EMAIL_IMAP_USER _WIZ_EMAIL_IMAP_PASSWORD \
        _WIZ_EMAIL_SMTP_HOST _WIZ_EMAIL_BOT_ADDRESS \
        _WIZ_BROWSER_VNC_BIND_ADDRESS _WIZ_BROWSER_VNC_PASSWORD _WIZ_WHISPER_MODEL \
        _WIZ_DEVELOPER_REPOS_DIR _WIZ_DEVELOPER_GITLAB_URL \
        _WIZ_DEVELOPER_GITLAB_USERNAME _WIZ_DEVELOPER_GITLAB_TOKEN \
        _WIZ_DEVELOPER_GITHUB_USERNAME _WIZ_DEVELOPER_GITHUB_TOKEN \
        _WIZ_TALK_SIGNALING_URL \
        _WIZ_BRAIN_KIND _WIZ_BRAIN_NATIVE_PROVIDER _WIZ_BRAIN_NATIVE_MODEL \
        _WIZ_BRAIN_NATIVE_BASE_URL _WIZ_BRAIN_NATIVE_API_KEY _WIZ_BRAIN_FALLBACK \
        _WIZ_BRAIN_ROLE_FAST _WIZ_BRAIN_ROLE_GENERAL _WIZ_BRAIN_ROLE_SMART
    do
        printf -v "$_tv" '%s' "$(toml_escape "${!_tv}")"
    done

    # Brain block — always emit [brain].kind; the [brain.native] sub-block
    # only when native is selected (mirrors the Ansible config.toml template).
    local brain_block="
[brain]
kind = \"$_WIZ_BRAIN_KIND\"
room_selectable = [$_WIZ_BRAIN_ROOM_SELECTABLE]
"
    # Only an explicit answer writes the key. "derive" leaves it out so the
    # role's own expression decides — writing fallback = "" instead would look
    # like the same thing and is not: it pins "no fallback" onto a deployment
    # the expression would have given one.
    if [ "$_WIZ_BRAIN_FALLBACK" != "derive" ]; then
        if [ "$_WIZ_BRAIN_FALLBACK" = "none" ]; then
            brain_block+="fallback = \"\"
"
        else
            brain_block+="fallback = \"$_WIZ_BRAIN_FALLBACK\"
"
        fi
    fi
    if [ "$_WIZ_BRAIN_KIND" = "native" ]; then
        brain_block+="
[brain.native]
provider = \"$_WIZ_BRAIN_NATIVE_PROVIDER\"
model = \"$_WIZ_BRAIN_NATIVE_MODEL\"
base_url = \"$_WIZ_BRAIN_NATIVE_BASE_URL\"
prompt_caching = $_WIZ_BRAIN_NATIVE_PROMPT_CACHING
api_key = \"$_WIZ_BRAIN_NATIVE_API_KEY\"
"
        # Internal subsystems request models by role (fast/general/smart). The
        # native brain has no built-in role table, so map all three — each
        # defaulting to the configured model, individually overridable above.
        if [ -n "$_WIZ_BRAIN_NATIVE_MODEL" ]; then
            local _role_fast="${_WIZ_BRAIN_ROLE_FAST:-$_WIZ_BRAIN_NATIVE_MODEL}"
            local _role_general="${_WIZ_BRAIN_ROLE_GENERAL:-$_WIZ_BRAIN_NATIVE_MODEL}"
            local _role_smart="${_WIZ_BRAIN_ROLE_SMART:-$_WIZ_BRAIN_NATIVE_MODEL}"
            brain_block+="
[models.aliases]
fast = \"$_role_fast\"
general = \"$_role_general\"
smart = \"$_role_smart\"
"
        fi
    fi

    # Developer block. Only `enabled` when the skill is off: every other key
    # here would be written empty, and two of them (gitlab_url, github_url)
    # have non-empty Ansible defaults an empty settings value would blank.
    local developer_block="
[developer]
enabled = $_WIZ_DEVELOPER_ENABLED
"
    if [ "$_WIZ_DEVELOPER_ENABLED" = "true" ]; then
        developer_block+="repos_dir = \"$_WIZ_DEVELOPER_REPOS_DIR\"
gitlab_url = \"$_WIZ_DEVELOPER_GITLAB_URL\"
gitlab_username = \"$_WIZ_DEVELOPER_GITLAB_USERNAME\"
gitlab_token = \"$_WIZ_DEVELOPER_GITLAB_TOKEN\"
github_username = \"$_WIZ_DEVELOPER_GITHUB_USERNAME\"
github_token = \"$_WIZ_DEVELOPER_GITHUB_TOKEN\"
"
    fi

    # [talk.signaling] without a [talk] header above it: the wizard asks none
    # of [talk]'s own keys, and writing them empty would override the role's
    # defaults for enabled and bot_username.
    local talk_signaling_block="
[talk.signaling]
enabled = $_WIZ_TALK_SIGNALING_ENABLED
url = \"$_WIZ_TALK_SIGNALING_URL\"
"

    cat > "$SETTINGS_FILE" <<TOML
# Istota settings - generated by setup wizard
# Edit this file and re-run install.sh to apply changes.
# See deploy/ansible/defaults/main.yml for all available settings
# (use names without the istota_ prefix).

home = "$ISTOTA_HOME"
namespace = "$ISTOTA_NAMESPACE"
bot_name = "$_WIZ_BOT_NAME"
repo_url = "$REPO_URL"
repo_branch = "$REPO_BRANCH"
repo_tag = "latest"
use_environment_file = true

nextcloud_url = "$_WIZ_NC_URL"
nextcloud_username = "$_WIZ_NC_USERNAME"
nextcloud_app_password = "$_WIZ_NC_APP_PASSWORD"

use_nextcloud_mount = $_WIZ_USE_MOUNT
nextcloud_mount_path = "$_WIZ_MOUNT_PATH"
rclone_password_obscured = "$_WIZ_RCLONE_PASS_OBSCURED"

$_WIZ_ADMIN_BLOCK
claude_oauth_token = "$_WIZ_CLAUDE_TOKEN"
secret_key = "$_WIZ_SECRET_KEY"

[site]
# Public hostname for nginx (used by the web UI redirect and the location
# webhook). DNS + TLS are operator-managed.
hostname = "$_WIZ_HOSTNAME"

[web]
enabled = $_WIZ_WEB_ENABLED
oauth2_provider = "$_WIZ_NC_URL"
oauth2_client_id = "$_WIZ_WEB_OAUTH2_CLIENT_ID"
oauth2_client_secret = "$_WIZ_WEB_OAUTH2_CLIENT_SECRET"
secret_key = "$_WIZ_WEB_SECRET_KEY"

[web.map]
# Background tiles for the location views.
#   openfreemap (default) needs no key and no account
#   carto        needs api_key, else every tile is watermarked
#   osm          needs neither
#   custom       needs dark_style and/or light_style
# api_key is not a secret: MapLibre puts it in the tile URL, so it ships to
# every browser that loads a map. Each user can override it in their own
# location settings.
provider = "$_WIZ_WEB_MAP_PROVIDER"
api_key = "$_WIZ_WEB_MAP_API_KEY"
dark_style = "$_WIZ_WEB_MAP_DARK_STYLE"
light_style = "$_WIZ_WEB_MAP_LIGHT_STYLE"
attribution = "$_WIZ_WEB_MAP_ATTRIBUTION"

[security]
sandbox_enabled = true

[email]
enabled = $_WIZ_EMAIL_ENABLED
imap_host = "$_WIZ_EMAIL_IMAP_HOST"
imap_user = "$_WIZ_EMAIL_IMAP_USER"
imap_password = "$_WIZ_EMAIL_IMAP_PASSWORD"
smtp_host = "$_WIZ_EMAIL_SMTP_HOST"
bot_email = "$_WIZ_EMAIL_BOT_ADDRESS"

[browser]
enabled = $_WIZ_BROWSER_ENABLED
vnc_bind_address = "$_WIZ_BROWSER_VNC_BIND_ADDRESS"
vnc_password = "$_WIZ_BROWSER_VNC_PASSWORD"

[memory_search]
enabled = $_WIZ_MEMORY_SEARCH_ENABLED

[sleep_cycle]
enabled = $_WIZ_SLEEP_CYCLE_ENABLED

[channel_sleep_cycle]
enabled = $_WIZ_CHANNEL_SLEEP_ENABLED

[whisper]
enabled = $_WIZ_WHISPER_ENABLED
model = "$_WIZ_WHISPER_MODEL"

[location]
enabled = $_WIZ_LOCATION_ENABLED
webhooks_port = $_WIZ_WEBHOOKS_PORT

[backup]
enabled = $_WIZ_BACKUP_ENABLED
$developer_block$talk_signaling_block$brain_block
$_WIZ_USERS_BLOCK
TOML

    chmod 600 "$SETTINGS_FILE"
    ok "Settings written to $SETTINGS_FILE"
}

# ============================================================
# Main
# ============================================================

main() {
    # ANSI Shadow figlet rendering of "ISTOTA". Hardcoded so a fresh box
    # without `toilet` / `figlet` installed still gets the welcome screen.
    echo
    printf "${_BLUE}"
    cat <<'EOF'
  ██╗███████╗████████╗ ██████╗ ████████╗ █████╗
  ██║██╔════╝╚══██╔══╝██╔═══██╗╚══██╔══╝██╔══██╗
  ██║███████╗   ██║   ██║   ██║   ██║   ███████║
  ██║╚════██║   ██║   ██║   ██║   ██║   ██╔══██║
  ██║███████║   ██║   ╚██████╔╝   ██║   ██║  ██║
  ╚═╝╚══════╝   ╚═╝    ╚═════╝    ╚═╝   ╚═╝  ╚═╝
EOF
    printf "${_RESET}"
    echo
    dim "A CYNIUM Lamplight Release"
    dim "setup wizard"
    echo
    dim "This wizard will guide you through configuring istota."
    dim "Press Enter to accept defaults shown in [brackets]."
    echo

    wiz_load_existing_secrets
    wiz_basics
    wiz_nextcloud
    wiz_mount
    wiz_users
    wiz_features
    wiz_hostname
    wiz_web_ui
    wiz_secrets_store
    wiz_brain
    wiz_brain_policy
    wiz_claude_auth
    wiz_review
    wiz_write_settings
}

main "$@"
