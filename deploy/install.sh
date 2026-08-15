#!/usr/bin/env bash
# Runs ON THE SERVER as root, from /opt/tt2gcal. Idempotent: safe to re-run
# after every deploy. It never touches .env or state/, which arrive separately.

set -euo pipefail

_CLI_RED='\033[0;31m'
_CLI_GREEN='\033[0;32m'
_CLI_YELLOW='\033[0;33m'
_CLI_BLUE='\033[0;34m'
_CLI_PURPLE='\033[0;35m'
_CLI_CYAN='\033[0;36m'
_CLI_NC='\033[0m'

log_info() { echo -e "${_CLI_CYAN}[i]${_CLI_NC} $1"; }
log_success() { echo -e "${_CLI_GREEN}[✓]${_CLI_NC} $1"; }
log_warn() { echo -e "${_CLI_YELLOW}[!]${_CLI_NC} $1"; }
log_error() { echo -e "${_CLI_RED}[✗]${_CLI_NC} $1"; }

print_divider() {
  local color="${1:-$_CLI_PURPLE}" label="${2:-}"
  local width="${COLUMNS:-$(tput cols 2>/dev/null || echo 80)}"
  local fill
  if [[ -n "$label" ]]; then
    printf -v fill '%*s' "$((width - ${#label} - 6))" ''
    printf '%b━━━━[%s]%s%b\n' "$color" "$label" "${fill// /━}" "$_CLI_NC"
  else
    printf -v fill '%*s' "$width" ''
    printf '%b%s%b\n' "$color" "${fill// /━}" "$_CLI_NC"
  fi
}

APP_DIR=/opt/tt2gcal
APP_USER=tt2gcal
UV_BIN=/usr/local/bin/uv

if [[ $EUID -ne 0 ]]; then
  log_error "must run as root"
  exit 1
fi

echo ""
print_divider "$_CLI_PURPLE" "tt2gcal install"
echo ""

# --- service account ------------------------------------------------------

if id "$APP_USER" &>/dev/null; then
  log_info "User ${_CLI_CYAN}${APP_USER}${_CLI_NC} already exists"
else
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  log_success "Created system user ${_CLI_CYAN}${APP_USER}${_CLI_NC} (no shell, no login)"
fi

# The failure alert quotes the journal to say *why* the run failed. Without this
# the alert still fires, but arrives with no diagnostic content at all.
if id -nG "$APP_USER" | tr ' ' '\n' | grep -qx systemd-journal; then
  log_info "User can already read the journal"
else
  usermod -aG systemd-journal "$APP_USER"
  log_success "Granted ${_CLI_CYAN}${APP_USER}${_CLI_NC} journal read access (for failure alerts)"
fi

# --- uv -------------------------------------------------------------------

if [[ -x "$UV_BIN" ]]; then
  log_info "uv present: ${_CLI_BLUE}$($UV_BIN --version)${_CLI_NC}"
else
  log_info "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null
  log_success "Installed ${_CLI_BLUE}$($UV_BIN --version)${_CLI_NC}"
fi

# --- dependencies ---------------------------------------------------------

log_info "Syncing dependencies from the lockfile..."
cd "$APP_DIR"
"$UV_BIN" sync --frozen --no-dev >/dev/null
log_success "Virtualenv ready at ${_CLI_BLUE}${APP_DIR}/.venv${_CLI_NC}"

# --- permissions ----------------------------------------------------------

chmod +x "$APP_DIR/deploy/alert.sh"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
mkdir -p "$APP_DIR/state"
chown "$APP_USER:$APP_USER" "$APP_DIR/state"
chmod 700 "$APP_DIR/state"
find "$APP_DIR/state" -type f -exec chmod 600 {} +

if [[ -f "$APP_DIR/.env" ]]; then
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  log_success "Secrets locked down (${_CLI_BLUE}.env${_CLI_NC} 600, ${_CLI_BLUE}state/${_CLI_NC} 700)"
else
  log_error "No .env found — copy it before enabling the timer"
  exit 1
fi

# --- systemd --------------------------------------------------------------

install -m 644 "$APP_DIR/deploy/tt2gcal.service" /etc/systemd/system/tt2gcal.service
install -m 644 "$APP_DIR/deploy/tt2gcal.timer" /etc/systemd/system/tt2gcal.timer
install -m 644 "$APP_DIR/deploy/tt2gcal-alert.service" /etc/systemd/system/tt2gcal-alert.service
systemctl daemon-reload
log_success "systemd units installed"

echo ""
print_divider "$_CLI_PURPLE"
echo ""
log_info "Next: verify before scheduling"
echo -e "    ${_CLI_BLUE}sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/tt2gcal doctor${_CLI_NC}"
echo -e "    ${_CLI_BLUE}sudo -u ${APP_USER} ${APP_DIR}/.venv/bin/tt2gcal sync --dry-run${_CLI_NC}"
log_info "Then enable it"
echo -e "    ${_CLI_BLUE}systemctl enable --now tt2gcal.timer${_CLI_NC}"
echo ""
