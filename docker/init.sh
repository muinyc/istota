#!/bin/bash
# Istota Docker — first-run setup wizard
#
# Writes a .env next to docker-compose.yml so the user can run
# `docker compose up -d` straight after. Auto-generates passwords for
# Nextcloud / Postgres / bot / human-user accounts and walks through
# the same optional-feature prompts the bare-metal wizard asks (email,
# ntfy, GPS location, developer credentials), so the resulting Docker
# stack lights up the same surface area as a "real" install.
#
# Usage:
#   bash docker/init.sh             # full wizard, then asks before bringing up the stack
#   bash docker/init.sh --minimal   # skip optional sections (passwords + Claude + user only)
#   bash docker/init.sh --force     # overwrite an existing .env without asking
#   bash docker/init.sh --start     # bring the stack up unconditionally (skip the prompt)
#   bash docker/init.sh --no-start  # only write .env; never run docker compose up

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
EXAMPLE_FILE="$SCRIPT_DIR/.env.example"
FORCE=false
MINIMAL=false
START_PROMPT="ask"   # ask | yes | no

while [ $# -gt 0 ]; do
    case "$1" in
        --force|-f)   FORCE=true; shift ;;
        --minimal|-m) MINIMAL=true; shift ;;
        --start)      START_PROMPT="yes"; shift ;;
        --no-start)   START_PROMPT="no";  shift ;;
        --help|-h)
            sed -n '2,/^$/s/^# \?//p' "$0"
            exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# --- output helpers ---
_BOLD="\033[1m"; _BLUE="\033[1;34m"; _GREEN="\033[1;32m"
_YELLOW="\033[1;33m"; _RED="\033[1;31m"; _DIM="\033[2m"; _RESET="\033[0m"
info()    { echo -e "${_BLUE}==>${_RESET} $*"; }
ok()      { echo -e "${_GREEN}  ✓${_RESET} $*"; }
warn()    { echo -e "${_YELLOW}  !${_RESET} $*"; }
die()     { echo -e "${_RED}ERROR:${_RESET} $*" >&2; exit 1; }
section() { echo; echo -e "${_BOLD}━━━ $* ━━━${_RESET}"; echo; }
dim()     { echo -e "${_DIM}  $*${_RESET}"; }

# --- input helpers (match deploy/wizard.sh) ---
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

# --- splash ---
# ANSI Shadow figlet rendering of "ISTOTA". Hardcoded so a fresh box without
# `toilet` / `figlet` installed still gets the welcome screen.
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
dim "first-run setup wizard"
echo

# --- preflight ---
[ -f "$EXAMPLE_FILE" ] || die ".env.example not found at $EXAMPLE_FILE"
command -v openssl >/dev/null 2>&1 || die "openssl is required (used to generate passwords)"

DOCKER_MISSING=false
COMPOSE_MISSING=false
command -v docker >/dev/null 2>&1 || DOCKER_MISSING=true
if ! docker compose version >/dev/null 2>&1; then
    COMPOSE_MISSING=true
fi
if [ "$DOCKER_MISSING" = true ] || [ "$COMPOSE_MISSING" = true ]; then
    warn "Docker prerequisites are not in PATH on this machine."
    if [ "$DOCKER_MISSING" = true ]; then
        echo "    docker:          missing  →  https://docs.docker.com/engine/install/"
    else
        echo "    docker:          ok"
    fi
    if [ "$COMPOSE_MISSING" = true ]; then
        echo "    docker compose:  missing  →  https://docs.docker.com/compose/install/"
    else
        echo "    docker compose:  ok"
    fi
    echo "  Install Docker on the host where you intend to run the stack before"
    echo "  running 'docker compose up -d'. This script will still produce .env."
    echo
fi

if [ -f "$ENV_FILE" ] && [ "$FORCE" = false ]; then
    warn "$ENV_FILE already exists."
    read -rp "  Overwrite? [y/N]: " ans
    case "$ans" in
        [yY]*) : ;;
        *) echo "  Aborted. Use --force to skip this prompt."; exit 0 ;;
    esac
fi

# --- password generator ---
# url-safe, ~24 chars, no shell-special characters
gen_pw() { openssl rand -base64 18 | tr -d '/+=\n' | head -c 24; }

# Tracks which keys the wizard actively set. Anything not in this list
# flows through unchanged from .env.example, so --minimal preserves the
# example's defaults rather than zeroing out optional features.
ACTIVE_KEYS=()
mark() { ACTIVE_KEYS+=("$1"); }

# Record a module the operator said no to. Modules are default-on, so an
# answer of "no" exists nowhere unless it is written down.
module_off() {
    if [ -n "$USER_DISABLED_MODULES" ]; then
        USER_DISABLED_MODULES="$USER_DISABLED_MODULES,$1"
    else
        USER_DISABLED_MODULES="$1"
    fi
}

# Inert defaults — only written to .env if `mark` adds the key.
DOMAIN=""
ISTOTA_BOT_NAME="Istota"
USER_EMAIL=""
ISTOTA_EMAIL_ENABLED="false"
ISTOTA_EMAIL_IMAP_HOST=""
ISTOTA_EMAIL_IMAP_USER=""
ISTOTA_EMAIL_IMAP_PASSWORD=""
ISTOTA_EMAIL_SMTP_HOST=""
ISTOTA_EMAIL_BOT_ADDRESS=""
ISTOTA_DEVELOPER_GITLAB_TOKEN=""
ISTOTA_DEVELOPER_GITLAB_USERNAME=""
ISTOTA_DEVELOPER_GITHUB_TOKEN=""
ISTOTA_DEVELOPER_GITHUB_USERNAME=""
BROWSER_MEMORY_LIMIT=""
BROWSER_SHM_SIZE=""
ISTOTA_BROWSER_ENABLED="true"
LOCATION_ENABLED=false  # internal flag — adds "location" to COMPOSE_PROFILES
SIGNALING_ENABLED=false # internal flag — adds "signaling" to COMPOSE_PROFILES
USER_DISABLED_MODULES=""   # assembled by module_off, written as-is
ISTOTA_TALK_SIGNALING_ENABLED="false"
ISTOTA_TALK_SIGNALING_SERVER=""
ISTOTA_TALK_SIGNALING_SECRET=""
ISTOTA_TALK_SIGNALING_URL=""
ISTOTA_LOCATION_ENABLED="true"
ISTOTA_DEVELOPER_ENABLED="true"
ISTOTA_FEEDS_ENABLED="true"
ISTOTA_MONEY_ENABLED="true"
ISTOTA_BRAIN_KIND="claude_code"
ISTOTA_BRAIN_CLAUDE_CODE_MODEL=""
ISTOTA_BRAIN_NATIVE_PROVIDER="openai_compat"
ISTOTA_BRAIN_NATIVE_MODEL=""
ISTOTA_BRAIN_NATIVE_BASE_URL="https://api.anthropic.com/v1"
ISTOTA_BRAIN_NATIVE_API_KEY=""
ISTOTA_BRAIN_NATIVE_PROMPT_CACHING="false"

# --- bot identity & public hostname ---
section "Bot identity"
dim "Choose carefully — this is the name your bot will go by, in Nextcloud,"
dim "in Talk, in emails, on the web. The Nextcloud login is derived from it"
dim "(lowercased, ASCII), and Nextcloud has no clean way to rename a user"
dim "after creation. You can't change it once the stack is provisioned."
dim "(You wouldn't rename your child or pet either.)"
echo
prompt_value ISTOTA_BOT_NAME "User-facing bot name" "Istota"
mark ISTOTA_BOT_NAME

# Derive the Nextcloud bot username from the bot name (same sanitizer as
# the istota entrypoint's bot_dir_name): lowercase ASCII, spaces→underscore,
# fall back to "istota" if the result is empty or hits a reserved NC name.
# This is set in stone at first provisioning — NC has no clean rename — so
# changing ISTOTA_BOT_NAME post-boot only updates display name, not username.
BOT_USER="$(ISTOTA_BOT_NAME="$ISTOTA_BOT_NAME" python3 -c '
import os, re
name = os.environ["ISTOTA_BOT_NAME"].lower().strip()
name = re.sub(r"\s+", "_", name)
name = re.sub(r"[^a-z0-9_\-]", "", name)
if not name or name in {"admin", "guest", "root", "nextcloud"}:
    name = "istota"
print(name)
')"
mark BOT_USER
if [ "$BOT_USER" != "istota" ]; then
    dim "Nextcloud bot login: ${BOT_USER}"
fi
echo
dim "Public hostname this stack will be reached at. Leave empty for"
dim "localhost-only evaluation; set it once and OAuth2 callback URL,"
dim "Nextcloud trusted domains and the SvelteKit site host all derive"
dim "from it. Examples: 'istota.example.com', 'home.example.com:8080'."
prompt_value DOMAIN "DOMAIN" ""
[ -n "$DOMAIN" ] && mark DOMAIN

# --- model backend (brain) ---
if [ "$MINIMAL" = false ]; then
    section "Model backend (brain)"
    dim "claude_code (default) shells out to the Claude CLI using the OAuth token"
    dim "or ANTHROPIC_API_KEY. native runs istota's own agent loop in-process"
    dim "against an OpenAI-compatible / Anthropic endpoint and bills per token."
    dim "Runbook: docs/configuration/native-brain.md"
    echo
    prompt_bool _use_native "Use the native brain instead of the Claude CLI?" "n"
    if [ "$_use_native" = "true" ]; then
        ISTOTA_BRAIN_KIND="native"
        echo
        prompt_value  ISTOTA_BRAIN_NATIVE_BASE_URL "Provider base URL" "$ISTOTA_BRAIN_NATIVE_BASE_URL"
        prompt_value  ISTOTA_BRAIN_NATIVE_MODEL    "Model id (explicit, e.g. claude-sonnet-4-6)" ""
        prompt_secret ISTOTA_BRAIN_NATIVE_API_KEY  "Provider API key"
        prompt_bool   ISTOTA_BRAIN_NATIVE_PROMPT_CACHING "Enable prompt caching (Anthropic/OpenRouter)?" "y"
        mark ISTOTA_BRAIN_KIND
        mark ISTOTA_BRAIN_NATIVE_PROVIDER
        mark ISTOTA_BRAIN_NATIVE_MODEL
        mark ISTOTA_BRAIN_NATIVE_BASE_URL
        mark ISTOTA_BRAIN_NATIVE_API_KEY
        mark ISTOTA_BRAIN_NATIVE_PROMPT_CACHING
    else
        # The claude_code brain has its own model key. The top-level
        # ISTOTA_MODEL this wizard used to fall through to is deprecated
        # (ISSUE-418) — it was applied to whatever brain ran and shadowed each
        # brain's own default — so set the per-brain one instead.
        echo
        dim "Model for the Claude CLI. Empty uses the CLI's own default."
        dim "A canonical id (claude-opus-5), a shortcut (opus), a role tier"
        dim "(smart), optionally with an effort suffix (opus:high)."
        prompt_value ISTOTA_BRAIN_CLAUDE_CODE_MODEL "Model (empty for the CLI default)" ""
        [ -n "$ISTOTA_BRAIN_CLAUDE_CODE_MODEL" ] && mark ISTOTA_BRAIN_CLAUDE_CODE_MODEL
    fi
fi

# --- Claude Code OAuth token ---
section "Claude Code OAuth token"
if [ "$ISTOTA_BRAIN_KIND" = "native" ]; then
    dim "Native brain selected — the Claude CLI isn't used, so a Claude token"
    dim "is optional. Press Enter to skip; the provider API key is used instead."
    echo
fi
cat <<'EOF'
  Istota needs a long-lived Claude Code OAuth token to talk to the model.

  On a machine that already has Claude Code installed and authenticated,
  run:

      claude setup-token

  That prints a token starting with "sk-ant-...". Copy it and paste it
  below. The token does not expire automatically; revoke it from the
  Anthropic console if you ever need to.

  If you don't have Claude Code yet:
      npm install -g @anthropic-ai/claude-code
      claude          # log in interactively, then run setup-token

  You can also leave this blank and set ANTHROPIC_API_KEY later in .env.

EOF
read -rp "  CLAUDE_CODE_OAUTH_TOKEN (paste, or empty to skip): " CLAUDE_CODE_OAUTH_TOKEN
mark CLAUDE_CODE_OAUTH_TOKEN
echo

# --- primary user ---
section "Primary user"
default_user="$(id -un 2>/dev/null || echo user)"
prompt_value USER_NAME       "USER_NAME (Nextcloud login id)" "$default_user"
mark USER_NAME
prompt_value USER_DISPLAY_NAME "USER_DISPLAY_NAME (e.g. Alice Example)" "$USER_NAME"
mark USER_DISPLAY_NAME

# Best-effort timezone detection
default_tz="UTC"
if [ -L /etc/localtime ]; then
    tz_link="$(readlink /etc/localtime 2>/dev/null || true)"
    case "$tz_link" in
        */zoneinfo/*) default_tz="${tz_link#*/zoneinfo/}" ;;
    esac
elif [ -r /etc/timezone ]; then
    default_tz="$(tr -d '\n' < /etc/timezone)"
fi
prompt_value USER_TIMEZONE "USER_TIMEZONE (IANA, e.g. Europe/Berlin)" "$default_tz"
mark USER_TIMEZONE

if [ "$MINIMAL" = false ]; then
    prompt_value USER_EMAIL "USER_EMAIL (optional, enables email-related features when matched against IMAP)" ""
    [ -n "$USER_EMAIL" ] && mark USER_EMAIL
fi

# --- optional features ---
if [ "$MINIMAL" = false ]; then

    # Email
    section "Email integration"
    dim "IMAP polling for incoming requests, SMTP for replies and outbound."
    dim "If your provider needs an app password (Gmail, iCloud, Fastmail), generate one first."
    prompt_bool email_enabled "Enable email integration?" "n"
    if [ "$email_enabled" = "true" ]; then
        ISTOTA_EMAIL_ENABLED="true"
        echo
        prompt_value  ISTOTA_EMAIL_IMAP_HOST     "IMAP host" ""
        prompt_value  ISTOTA_EMAIL_IMAP_USER     "IMAP username" ""
        prompt_secret ISTOTA_EMAIL_IMAP_PASSWORD "IMAP password"
        prompt_value  ISTOTA_EMAIL_SMTP_HOST     "SMTP host" "$ISTOTA_EMAIL_IMAP_HOST"
        prompt_value  ISTOTA_EMAIL_BOT_ADDRESS   "Bot email address" "$ISTOTA_EMAIL_IMAP_USER"
        mark ISTOTA_EMAIL_ENABLED
        mark ISTOTA_EMAIL_IMAP_HOST
        mark ISTOTA_EMAIL_IMAP_USER
        mark ISTOTA_EMAIL_IMAP_PASSWORD
        mark ISTOTA_EMAIL_SMTP_HOST
        mark ISTOTA_EMAIL_BOT_ADDRESS
    else
        ISTOTA_EMAIL_ENABLED="false"
        mark ISTOTA_EMAIL_ENABLED
    fi

    # ntfy push notifications: configured per-user via web settings
    # (no operator-shared block).

    # GPS location
    section "GPS location tracking"
    dim "Webhook receiver for the Overland app (iOS/Android). Adds the 'location'"
    dim "compose profile so the receiver container starts. The bearer token Overland"
    dim "sends with each ping is auto-generated on first boot and surfaced in the logs."
    prompt_bool location_enabled "Enable GPS location tracking?" "n"
    if [ "$location_enabled" = "true" ]; then
        LOCATION_ENABLED=true
        ISTOTA_LOCATION_ENABLED="true"
    else
        # Both halves, because they are different axes and "no" means both.
        # The example file ships ISTOTA_LOCATION_ENABLED=true, so without the
        # mark below an operator who answered "no" got the module on and its
        # tab present, with no receiver container behind it.
        ISTOTA_LOCATION_ENABLED="false"
        module_off location
    fi
    mark ISTOTA_LOCATION_ENABLED

    # Modules
    section "Modules"
    dim "Each of these is on by default and appears as a tab in the web UI."
    dim "Answering no records the module in USER_DISABLED_MODULES, which seeds"
    dim "the user's profile the first time the stack boots. After that the"
    dim "stored profile wins, so change it in the web settings rather than .env."
    prompt_bool feeds_enabled "Feeds (RSS / Atom / Tumblr / Are.na reader)?" "y"
    if [ "$feeds_enabled" = "true" ]; then
        ISTOTA_FEEDS_ENABLED="true"
    else
        # One answer sets both, so an operator cannot end up with the module
        # hidden and its resource block still rendered, or the reverse.
        ISTOTA_FEEDS_ENABLED="false"
        module_off feeds
    fi
    mark ISTOTA_FEEDS_ENABLED
    prompt_bool money_enabled "Money (accounts, portfolio, tax estimates)?" "y"
    if [ "$money_enabled" = "true" ]; then
        ISTOTA_MONEY_ENABLED="true"
    else
        ISTOTA_MONEY_ENABLED="false"
        module_off money
    fi
    mark ISTOTA_MONEY_ENABLED
    # No deployment-level toggle for these two — USER_DISABLED_MODULES is the
    # only switch either has.
    prompt_bool health_enabled "Health (body stats, bloodwork, documents)?" "y"
    [ "$health_enabled" = "true" ] || module_off health
    prompt_bool briefings_enabled "Briefings (scheduled digests)?" "y"
    [ "$briefings_enabled" = "true" ] || module_off briefings

    # Developer skill
    section "Developer (git, GitLab, GitHub)"
    dim "Lets the bot push commits, open MRs/PRs, and use 'gh'/GitLab APIs."
    dim "Tokens are optional and stored only in your local .env."
    # Two questions, because the skill and its forge tokens are different
    # things: git, worktrees and the local half of the skill need no token at
    # all, so one question reading "configure credentials?" cannot decide
    # ISTOTA_DEVELOPER_ENABLED — which gates the whole [developer] block in
    # render-config.sh, not just the token fields. Defaulting the first to "y"
    # keeps the shipped default of the example file.
    prompt_bool developer_enabled "Enable the developer skill?" "y"
    if [ "$developer_enabled" = "true" ]; then
        ISTOTA_DEVELOPER_ENABLED="true"
    else
        ISTOTA_DEVELOPER_ENABLED="false"
    fi
    mark ISTOTA_DEVELOPER_ENABLED
    dev_enabled=false
    if [ "$developer_enabled" = "true" ]; then
        prompt_bool dev_enabled "Configure forge credentials now? (optional)" "n"
    fi
    if [ "$dev_enabled" = "true" ]; then
        echo
        prompt_value  ISTOTA_DEVELOPER_GITLAB_USERNAME "GitLab username (empty to skip)" ""
        if [ -n "$ISTOTA_DEVELOPER_GITLAB_USERNAME" ]; then
            prompt_secret ISTOTA_DEVELOPER_GITLAB_TOKEN "GitLab personal access token"
            mark ISTOTA_DEVELOPER_GITLAB_USERNAME
            mark ISTOTA_DEVELOPER_GITLAB_TOKEN
        fi
        prompt_value  ISTOTA_DEVELOPER_GITHUB_USERNAME "GitHub username (empty to skip)" ""
        if [ -n "$ISTOTA_DEVELOPER_GITHUB_USERNAME" ]; then
            prompt_secret ISTOTA_DEVELOPER_GITHUB_TOKEN "GitHub personal access token (gh-style)"
            mark ISTOTA_DEVELOPER_GITHUB_USERNAME
            mark ISTOTA_DEVELOPER_GITHUB_TOKEN
        fi
    fi

    if [ -n "$USER_DISABLED_MODULES" ]; then
        mark USER_DISABLED_MODULES
        dim "Modules switched off: $USER_DISABLED_MODULES"
    fi

fi  # end optional features

# --- browser container default ---
# The browser profile bundles a Chromium + bot-detection countermeasures
# container that the `browse` skill talks to. Chrome has no ARM packages,
# so we enable it by default on x86_64 hosts and on Apple Silicon (where
# Docker Desktop's Rosetta lets the linux/amd64 image run, slowly).
section "Container profiles"
HOST_ARCH="$(uname -m 2>/dev/null || echo unknown)"
HOST_OS="$(uname -s 2>/dev/null || echo unknown)"
COMPOSE_PROFILES=""
case "$HOST_ARCH" in
    x86_64|amd64)
        COMPOSE_PROFILES="browser"
        ok "Browser container enabled (host arch: $HOST_ARCH)"
        ;;
    arm64|aarch64)
        if [ "$HOST_OS" = "Darwin" ]; then
            COMPOSE_PROFILES="browser"
            # Rosetta-emulated Chromium OOMs at the 3G default — bump it.
            BROWSER_MEMORY_LIMIT="5G"
            BROWSER_SHM_SIZE="4gb"
            mark BROWSER_MEMORY_LIMIT
            mark BROWSER_SHM_SIZE
            warn "Browser container enabled under Rosetta emulation (host: $HOST_OS/$HOST_ARCH). Expect slow page loads; suitable for previews only."
            dim "BROWSER_MEMORY_LIMIT=${BROWSER_MEMORY_LIMIT}, BROWSER_SHM_SIZE=${BROWSER_SHM_SIZE} (Rosetta Chromium OOMs at the 3G default)"
        else
            warn "Browser container disabled (host: $HOST_OS/$HOST_ARCH; Chrome has no ARM packages and qemu emulation is unreliable)."
        fi
        ;;
    *)
        warn "Browser container disabled (host arch: $HOST_ARCH; Chrome has no ARM packages)."
        ;;
esac
# The same rule as location and developer: the answer this arrived at has to be
# written down. .env.example ships ISTOTA_BROWSER_ENABLED=true, so a host that
# gets no browser container would otherwise still be handed [browser] enabled,
# pointed at a container name that resolves to nothing.
case ",$COMPOSE_PROFILES," in
    *,browser,*) ISTOTA_BROWSER_ENABLED="true" ;;
    *)           ISTOTA_BROWSER_ENABLED="false" ;;
esac
mark ISTOTA_BROWSER_ENABLED
if [ "$LOCATION_ENABLED" = true ]; then
    if [ -n "$COMPOSE_PROFILES" ]; then
        COMPOSE_PROFILES="$COMPOSE_PROFILES,location"
    else
        COMPOSE_PROFILES="location"
    fi
    ok "Location webhook receiver enabled"
fi

# Talk signaling. Asked here rather than with the other optional features
# because it is a compose profile, and asked *at all* because its automatic
# path has one window: provision-nc.sh registers the server from Nextcloud's
# post-installation hook, which the image runs only on the boot that performs
# the install. Both variables therefore have to be in .env before the first
# `docker compose up`, or the registration has to be done by hand afterwards.
if [ "$MINIMAL" = false ]; then
    echo
    dim "Talk signaling server (optional). Turns inbound Talk from a poll into"
    dim "a push, which is a large drop in load on Nextcloud."
    echo
    dim "This is the one setting that has to be decided now: Nextcloud registers"
    dim "the server during its own installation and never revisits it. Later,"
    dim "the alternative is to register it by hand:"
    # One line, no continuation backslash: dim() appends ${_RESET}, and
    # `echo -e` reads a trailing backslash together with it as an escaped
    # backslash, printing the reset sequence as literal text.
    dim "  docker compose exec -u www-data nextcloud php occ talk:signaling:add <url> <secret> --verify"
    echo
    dim "It also changes call signaling for every Talk user on this Nextcloud,"
    dim "not only for the bot. With no MCU configured media stays peer-to-peer"
    dim "and calls keep working, but it is a change to a shared service."
    prompt_bool signaling_wanted "Enable the Talk signaling server?" "n"
    if [ "$signaling_wanted" = "true" ]; then
        echo
        # No default is offered, and that is the finding rather than caution.
        # Two different things use this one URL: a browser connects to it, and
        # Nextcloud's own PHP posts room and chat events to it — that second leg
        # is what makes inbound a push at all, and it runs inside the nextcloud
        # container. So http://localhost:8081 is wrong even for a localhost-only
        # evaluation: it is a host-side publish, and from PHP `localhost` is
        # nextcloud's own loopback. The stack offers no address satisfying both,
        # so there is nothing to derive and nothing is suggested.
        dim "Talk needs one URL for the server, and two different things use it:"
        dim "a browser connects to it, and Nextcloud's own PHP posts room and"
        dim "chat events to it. That second leg is what makes inbound a push, and"
        dim "it runs inside the nextcloud container — so the URL has to resolve"
        dim "from a browser and from in there."
        echo
        dim "This stack provides no such address on its own: the server is"
        dim "published on loopback only (ISTOTA_TALK_SIGNALING_PORT, 8081 by"
        dim "default) and nginx does not proxy it. Put a TLS front end in front"
        dim "of that port on a name both sides resolve, and give its URL here."
        dim "Leave empty to skip signaling; nothing else in the stack changes."
        prompt_value ISTOTA_TALK_SIGNALING_SERVER "Signaling URL (empty to skip)" ""
        if [ -n "$ISTOTA_TALK_SIGNALING_SERVER" ]; then
            ISTOTA_TALK_SIGNALING_ENABLED="true"
            ISTOTA_TALK_SIGNALING_SECRET="$(gen_pw)"
            # The daemon's own route, which on this stack always differs from
            # the one above: Talk advertises the browser URL while the daemon
            # sits on the container network beside the server.
            ISTOTA_TALK_SIGNALING_URL="http://signaling:8080"
            SIGNALING_ENABLED=true
            mark ISTOTA_TALK_SIGNALING_ENABLED
            mark ISTOTA_TALK_SIGNALING_SERVER
            mark ISTOTA_TALK_SIGNALING_SECRET
            mark ISTOTA_TALK_SIGNALING_URL
            ok "Talk signaling enabled (shared secret generated)"
        else
            warn "No URL given — leaving Talk signaling off. To add it later you"
            warn "  will need the manual 'occ talk:signaling:add' above."
        fi
    fi
fi
if [ "$SIGNALING_ENABLED" = true ]; then
    if [ -n "$COMPOSE_PROFILES" ]; then
        COMPOSE_PROFILES="$COMPOSE_PROFILES,signaling"
    else
        COMPOSE_PROFILES="signaling"
    fi
fi
if [ -n "$COMPOSE_PROFILES" ]; then
    dim "COMPOSE_PROFILES=$COMPOSE_PROFILES (edit .env to change)"
fi

# --- generate passwords ---
section "Generating passwords"
ADMIN_PASSWORD="$(gen_pw)";    mark ADMIN_PASSWORD;    ok "ADMIN_PASSWORD"
USER_PASSWORD="$(gen_pw)";     mark USER_PASSWORD;     ok "USER_PASSWORD"
BOT_PASSWORD="$(gen_pw)";      mark BOT_PASSWORD;      ok "BOT_PASSWORD"
POSTGRES_PASSWORD="$(gen_pw)"; mark POSTGRES_PASSWORD; ok "POSTGRES_PASSWORD"
mark COMPOSE_PROFILES
case ",$COMPOSE_PROFILES," in
    *,browser,*) VNC_PASSWORD="$(gen_pw)"; mark VNC_PASSWORD; ok "VNC_PASSWORD (browser noVNC)" ;;
    *)           VNC_PASSWORD="" ;;
esac
# Pin the Compose project name. Without this, Compose names the project after
# the parent directory (typically "docker") and clones in different paths with
# the same parent name silently merge into the same project — recreating each
# other's containers and (worst case) mixing up volumes.
COMPOSE_PROJECT_NAME="istota"
mark COMPOSE_PROJECT_NAME

# --- write .env ---
# Start from .env.example and patch the values we manage; this preserves
# every comment and optional knob the example file documents.
TMP_ENV="$(mktemp "${TMPDIR:-/tmp}/istota-env.XXXXXX")"
trap 'rm -f "$TMP_ENV"' EXIT

# Pass values via the environment so the heredoc can stay single-quoted —
# avoids any shell expansion of the rendered passwords/tokens.
ADMIN_PASSWORD="$ADMIN_PASSWORD" \
POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
BOT_USER="$BOT_USER" \
BOT_PASSWORD="$BOT_PASSWORD" \
USER_NAME="$USER_NAME" \
USER_PASSWORD="$USER_PASSWORD" \
USER_DISPLAY_NAME="$USER_DISPLAY_NAME" \
USER_TIMEZONE="$USER_TIMEZONE" \
USER_EMAIL="$USER_EMAIL" \
USER_DISABLED_MODULES="$USER_DISABLED_MODULES" \
CLAUDE_CODE_OAUTH_TOKEN="$CLAUDE_CODE_OAUTH_TOKEN" \
VNC_PASSWORD="$VNC_PASSWORD" \
COMPOSE_PROFILES="$COMPOSE_PROFILES" \
COMPOSE_PROJECT_NAME="$COMPOSE_PROJECT_NAME" \
DOMAIN="$DOMAIN" \
ISTOTA_BOT_NAME="$ISTOTA_BOT_NAME" \
ISTOTA_EMAIL_ENABLED="$ISTOTA_EMAIL_ENABLED" \
ISTOTA_EMAIL_IMAP_HOST="$ISTOTA_EMAIL_IMAP_HOST" \
ISTOTA_EMAIL_IMAP_USER="$ISTOTA_EMAIL_IMAP_USER" \
ISTOTA_EMAIL_IMAP_PASSWORD="$ISTOTA_EMAIL_IMAP_PASSWORD" \
ISTOTA_EMAIL_SMTP_HOST="$ISTOTA_EMAIL_SMTP_HOST" \
ISTOTA_EMAIL_BOT_ADDRESS="$ISTOTA_EMAIL_BOT_ADDRESS" \
ISTOTA_DEVELOPER_GITLAB_TOKEN="$ISTOTA_DEVELOPER_GITLAB_TOKEN" \
ISTOTA_DEVELOPER_GITLAB_USERNAME="$ISTOTA_DEVELOPER_GITLAB_USERNAME" \
ISTOTA_DEVELOPER_GITHUB_TOKEN="$ISTOTA_DEVELOPER_GITHUB_TOKEN" \
ISTOTA_DEVELOPER_GITHUB_USERNAME="$ISTOTA_DEVELOPER_GITHUB_USERNAME" \
BROWSER_MEMORY_LIMIT="$BROWSER_MEMORY_LIMIT" \
BROWSER_SHM_SIZE="$BROWSER_SHM_SIZE" \
ISTOTA_BROWSER_ENABLED="$ISTOTA_BROWSER_ENABLED" \
ISTOTA_DEVELOPER_ENABLED="$ISTOTA_DEVELOPER_ENABLED" \
ISTOTA_LOCATION_ENABLED="$ISTOTA_LOCATION_ENABLED" \
ISTOTA_FEEDS_ENABLED="$ISTOTA_FEEDS_ENABLED" \
ISTOTA_MONEY_ENABLED="$ISTOTA_MONEY_ENABLED" \
ISTOTA_TALK_SIGNALING_ENABLED="$ISTOTA_TALK_SIGNALING_ENABLED" \
ISTOTA_TALK_SIGNALING_SERVER="$ISTOTA_TALK_SIGNALING_SERVER" \
ISTOTA_TALK_SIGNALING_SECRET="$ISTOTA_TALK_SIGNALING_SECRET" \
ISTOTA_TALK_SIGNALING_URL="$ISTOTA_TALK_SIGNALING_URL" \
ISTOTA_BRAIN_KIND="$ISTOTA_BRAIN_KIND" \
ISTOTA_BRAIN_CLAUDE_CODE_MODEL="$ISTOTA_BRAIN_CLAUDE_CODE_MODEL" \
ISTOTA_BRAIN_NATIVE_PROVIDER="$ISTOTA_BRAIN_NATIVE_PROVIDER" \
ISTOTA_BRAIN_NATIVE_MODEL="$ISTOTA_BRAIN_NATIVE_MODEL" \
ISTOTA_BRAIN_NATIVE_BASE_URL="$ISTOTA_BRAIN_NATIVE_BASE_URL" \
ISTOTA_BRAIN_NATIVE_API_KEY="$ISTOTA_BRAIN_NATIVE_API_KEY" \
ISTOTA_BRAIN_NATIVE_PROMPT_CACHING="$ISTOTA_BRAIN_NATIVE_PROMPT_CACHING" \
ACTIVE_KEYS="$(IFS=,; echo "${ACTIVE_KEYS[*]}")" \
python3 - "$EXAMPLE_FILE" "$TMP_ENV" <<'PYEOF'
import os, sys, re
src, dst = sys.argv[1], sys.argv[2]
active = [k for k in os.environ.get("ACTIVE_KEYS", "").split(",") if k]
overrides = {k: os.environ.get(k, "") for k in active}
seen = set()
out = []
key_re = re.compile(r"^([A-Z_][A-Z0-9_]*)=")
with open(src) as f:
    for line in f:
        m = key_re.match(line)
        if m and m.group(1) in overrides:
            k = m.group(1)
            seen.add(k)
            out.append(f"{k}={overrides[k]}\n")
        else:
            out.append(line)
# Anything we wanted to set but didn't see in the example — append.
missing = [k for k in overrides if k not in seen]
if missing:
    out.append("\n# --- added by init.sh ---\n")
    for k in missing:
        out.append(f"{k}={overrides[k]}\n")
with open(dst, "w") as f:
    f.writelines(out)
PYEOF

mv "$TMP_ENV" "$ENV_FILE"
chmod 600 "$ENV_FILE"
trap - EXIT

# --- summary ---
section "Configuration written"
ok "Wrote $ENV_FILE (mode 600)"
echo
echo -e "  ${_BOLD}Generated credentials${_RESET} (also saved in $ENV_FILE):"
echo "    Nextcloud admin   :  admin / $ADMIN_PASSWORD"
echo "    Primary user      :  $USER_NAME / $USER_PASSWORD"
echo "    Bot user          :  $BOT_USER / $BOT_PASSWORD"
echo "    Postgres          :  $POSTGRES_PASSWORD"
[ -n "$VNC_PASSWORD" ] && echo "    Browser noVNC     :  $VNC_PASSWORD"
echo
echo -e "  ${_BOLD}Configuration:${_RESET}"
echo "    Bot name          :  $ISTOTA_BOT_NAME"
echo "    Brain             :  $ISTOTA_BRAIN_KIND$([ "$ISTOTA_BRAIN_KIND" = "native" ] && echo " (model: ${ISTOTA_BRAIN_NATIVE_MODEL:-unset}, $ISTOTA_BRAIN_NATIVE_BASE_URL)")"
echo "    Public hostname   :  ${DOMAIN:-(localhost-only)}"
echo "    Compose profiles  :  ${COMPOSE_PROFILES:-(none — only the core stack)}"
echo "    Email             :  $ISTOTA_EMAIL_ENABLED"
echo "    Modules off       :  ${USER_DISABLED_MODULES:-(none)}"
echo "    Talk signaling    :  $ISTOTA_TALK_SIGNALING_ENABLED"
[ -n "$ISTOTA_DEVELOPER_GITLAB_USERNAME" ] && echo "    Developer GitLab  :  $ISTOTA_DEVELOPER_GITLAB_USERNAME"
[ -n "$ISTOTA_DEVELOPER_GITHUB_USERNAME" ] && echo "    Developer GitHub  :  $ISTOTA_DEVELOPER_GITHUB_USERNAME"
echo

# Repeated here because the window closes at the first `docker compose up` and
# the summary is the last thing anybody reads before running it.
if [ "$ISTOTA_TALK_SIGNALING_ENABLED" = "true" ]; then
    warn "Talk signaling is registered during Nextcloud's own installation, so"
    warn "  it has to be in place before the first 'docker compose up'. It is"
    warn "  in $ENV_FILE now, so starting the stack from here is enough."
    warn "  If you have already installed this Nextcloud, register it by hand:"
    warn "    docker compose exec -u www-data nextcloud php occ talk:signaling:add '$ISTOTA_TALK_SIGNALING_SERVER' '<ISTOTA_TALK_SIGNALING_SECRET from .env>' --verify"
    warn "  Until Talk has it registered the daemon refuses to start, and"
    warn "  'restart: unless-stopped' makes that a loop that takes web and"
    warn "  webhooks down with it. Set ISTOTA_TALK_SIGNALING_ENABLED=false in"
    warn "  $ENV_FILE to back out."
    warn "  It changes call signaling for every Talk user on this Nextcloud."
    echo
fi

if [ "$ISTOTA_BRAIN_KIND" = "native" ] && [ -z "$ISTOTA_BRAIN_NATIVE_API_KEY" ]; then
    warn "Native brain selected but no provider API key set. The stack will"
    warn "  start, but the bot can't call the model until you set"
    warn "  ISTOTA_BRAIN_NATIVE_API_KEY in $ENV_FILE and 'docker compose restart istota web'."
    echo
elif [ "$ISTOTA_BRAIN_KIND" != "native" ] && [ -z "$CLAUDE_CODE_OAUTH_TOKEN" ]; then
    warn "No Claude Code token set. The stack will start, but the bot can't"
    warn "  call the model until you set CLAUDE_CODE_OAUTH_TOKEN or"
    warn "  ANTHROPIC_API_KEY in $ENV_FILE and 'docker compose restart istota'."
    echo
fi

# --- decide whether to bring the stack up ---
should_start=false
if [ "$DOCKER_MISSING" = true ] || [ "$COMPOSE_MISSING" = true ]; then
    warn "Docker / docker compose not available on this host — skipping startup."
    warn "  Copy this directory to a host with Docker, then run 'docker compose up -d'."
else
    case "$START_PROMPT" in
        yes) should_start=true ;;
        no)  should_start=false ;;
        ask)
            echo
            prompt_bool _start_now "Bring the stack up now (docker compose up -d --build)?" "y"
            should_start="$_start_now"
            ;;
    esac
fi

# Build URLs from the .env we just wrote (NC_PORT may have come from the example).
nc_port_raw="$(grep -E '^NC_PORT=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
nc_port="${nc_port_raw:-8080}"
public_proto="$(grep -E '^ISTOTA_PUBLIC_PROTO=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)"
public_proto="${public_proto:-http}"

# Localhost is always reachable when running on this host. The NC_PORT bind in
# docker-compose.yml maps to nginx :80, which proxies both / (Nextcloud) and
# /istota/ (web UI), so they share the host:port.
local_base="http://localhost:${nc_port}"

# Public URL (only meaningful when DOMAIN is set). DOMAIN may already include
# :port; if not, assume the proxy in front terminates on the default port for
# the proto. We don't try to second-guess that.
public_base=""
if [ -n "$DOMAIN" ]; then
    case "$DOMAIN" in
        *:*) public_base="${public_proto}://${DOMAIN}" ;;
        *)   public_base="${public_proto}://${DOMAIN}" ;;
    esac
fi

print_urls() {
    echo
    echo -e "  ${_BOLD}URLs (localhost — always works on this host):${_RESET}"
    echo "    Nextcloud   :  ${local_base}/"
    echo "    Istota web  :  ${local_base}/istota/"
    if [ -n "$public_base" ]; then
        echo
        echo -e "  ${_BOLD}URLs (public — once DNS / your reverse proxy is in place):${_RESET}"
        echo "    Nextcloud   :  ${public_base}/"
        echo "    Istota web  :  ${public_base}/istota/"
    fi
}

if [ "$should_start" = true ]; then
    # Footgun guard: if there's already an "istota" project running from a
    # different path, `docker compose up` from here would merge into it —
    # recreating its containers with our config. Refuse to proceed unless the
    # operator explicitly takes the existing stack down or moves it aside.
    existing_path="$(docker compose ls --format json 2>/dev/null \
        | python3 -c '
import json, os, sys
try:
    rows = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for row in rows or []:
    if row.get("Name") == "istota":
        files = (row.get("ConfigFiles") or "").split(",")
        if files:
            print(os.path.dirname(files[0]))
        break
' 2>/dev/null || true)"
    if [ -n "$existing_path" ] && [ "$existing_path" != "$SCRIPT_DIR" ]; then
        warn "An 'istota' Compose project is already running from:"
        warn "    $existing_path"
        warn "Bringing this stack up here would recreate that one's containers."
        warn "Take it down first ('docker compose -f ${existing_path}/docker-compose.yml down')"
        warn "or set COMPOSE_PROJECT_NAME to a different value in $ENV_FILE."
        die "Refusing to start to protect the existing deployment."
    fi

    # Stale-volume guard: postgres only initializes the DB on first volume
    # create. If istota_postgres_data exists from a prior run, the new
    # POSTGRES_PASSWORD in .env won't take effect — Nextcloud's installer
    # will fail with "password authentication failed for user nextcloud".
    stale_volumes=()
    for v in postgres_data nextcloud_html nextcloud_data shared_files istota_data; do
        if docker volume inspect "${COMPOSE_PROJECT_NAME}_${v}" >/dev/null 2>&1; then
            stale_volumes+=("${COMPOSE_PROJECT_NAME}_${v}")
        fi
    done
    if [ ${#stale_volumes[@]} -gt 0 ]; then
        warn "Found existing Docker volumes from a previous run:"
        for v in "${stale_volumes[@]}"; do
            echo "    $v"
        done
        warn "Postgres won't pick up the new POSTGRES_PASSWORD from .env, so"
        warn "Nextcloud's first-boot installer will fail with an auth error."
        echo
        prompt_bool _wipe_volumes "Remove these volumes and start fresh?" "n"
        if [ "$_wipe_volumes" = true ]; then
            info "Running: docker compose down -v"
            (cd "$SCRIPT_DIR" && docker compose down -v) || \
                die "docker compose down -v failed. Inspect the output above."
        else
            warn "Keeping existing volumes. If startup fails with a postgres auth"
            warn "  error, re-run with --force after 'docker compose down -v'."
        fi
    fi

    section "Starting the stack"
    info "Running: docker compose up -d --build"
    if ! (cd "$SCRIPT_DIR" && docker compose up -d --build); then
        die "docker compose failed. Inspect the output above, then re-run 'docker compose up -d' from $SCRIPT_DIR."
    fi
    echo

    # Poll Nextcloud's status endpoint via the localhost bind. First boot can
    # take a minute or two while NC runs migrations and the istota entrypoint
    # provisions Talk rooms + the OAuth2 client. Cap at 5 minutes — beyond
    # that the user should look at the logs anyway.
    info "Waiting for Nextcloud to come up (first boot can take a minute or two)..."
    nc_status_url="${local_base}/status.php"
    waited=0
    nc_ready=false
    while [ "$waited" -lt 300 ]; do
        if curl -sf "$nc_status_url" 2>/dev/null | grep -q '"installed":true'; then
            nc_ready=true
            break
        fi
        sleep 5
        waited=$((waited + 5))
    done
    if [ "$nc_ready" = true ]; then
        ok "Nextcloud is up at ${local_base}/"
    else
        warn "Nextcloud didn't respond at ${local_base}/ within 5 minutes."
        warn "  Check logs with: docker compose -f $SCRIPT_DIR/docker-compose.yml logs nextcloud istota"
    fi

    section "Ready"
    print_urls
    echo
    echo -e "  ${_BOLD}Log in:${_RESET}"
    echo "    Open ${local_base}/ and sign in as ${USER_NAME} / ${USER_PASSWORD}"
    echo "    Then visit ${local_base}/istota/ — sign in there with the same"
    echo "    Nextcloud user (OAuth2 redirects through Nextcloud)."
    echo
    dim "Tail logs:    docker compose -f $SCRIPT_DIR/docker-compose.yml logs -f"
    dim "Stop stack:   docker compose -f $SCRIPT_DIR/docker-compose.yml down"
    echo
else
    section "Done"
    echo -e "  ${_BOLD}Next steps:${_RESET}"
    echo "    cd $SCRIPT_DIR"
    echo "    docker compose up -d --build"
    print_urls
    echo
    dim "Tip: re-run with --start to bring the stack up automatically, --no-start"
    dim "to skip the prompt entirely, or --minimal for a shorter wizard."
    echo
fi
