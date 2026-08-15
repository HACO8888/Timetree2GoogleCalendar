#!/usr/bin/env bash
# Runs LOCALLY. Ships the working tree to the server and runs install.sh there.
#
#   ./deploy/deploy.sh root@your-server
#
# Secrets travel separately and deliberately: .env and state/ are excluded from
# the code rsync and pushed only when --with-secrets is given, so a routine code
# deploy can never clobber the server's live token or calendar mapping.

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

TARGET="${1:-}"
WITH_SECRETS=0
[[ "${2:-}" == "--with-secrets" ]] && WITH_SECRETS=1

if [[ -z "$TARGET" ]]; then
  log_error "usage: $0 user@host [--with-secrets]"
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/tt2gcal

echo ""
print_divider "$_CLI_PURPLE" "deploy to $TARGET"
echo ""

log_info "Shipping code to ${_CLI_BLUE}${TARGET}:${APP_DIR}${_CLI_NC}"
ssh "$TARGET" "mkdir -p $APP_DIR"
rsync -az --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '.env' \
  --exclude 'state/' \
  --exclude 'raw-timetree/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '.ruff_cache/' \
  "$REPO_ROOT/" "$TARGET:$APP_DIR/"
log_success "Code synced"

if [[ $WITH_SECRETS -eq 1 ]]; then
  log_warn "Pushing secrets — this overwrites the server's .env and state/"
  ssh "$TARGET" "mkdir -p $APP_DIR/state && chmod 700 $APP_DIR/state"
  scp -q "$REPO_ROOT/.env" "$TARGET:$APP_DIR/.env"

  # The calendar map must travel with the token, or the server would create a
  # second set of Google calendars instead of adopting the existing ones.
  for f in google-token.json calendars.json color-map.json; do
    if [[ -f "$REPO_ROOT/state/$f" ]]; then
      scp -q "$REPO_ROOT/state/$f" "$TARGET:$APP_DIR/state/$f"
      log_success "Sent ${_CLI_BLUE}state/${f}${_CLI_NC}"
    fi
  done

  # Deliberately NOT sent: state/session.json. A TimeTree session is tied to the
  # machine that created it; the server signs in once and keeps its own.
  log_info "Skipped ${_CLI_BLUE}state/session.json${_CLI_NC} — the server signs in on its own"
else
  log_info "Secrets untouched (pass ${_CLI_BLUE}--with-secrets${_CLI_NC} on first deploy)"
fi

log_info "Running the installer on the server..."
echo ""
ssh "$TARGET" "bash $APP_DIR/deploy/install.sh"
