#!/usr/bin/env bash
# Called by systemd's OnFailure when a sync run exits non-zero.
#
# TimeTree's private API can change or start blocking at any time. A silent
# failure looks exactly like "nothing to sync", so this always writes to the
# journal, and additionally pushes to TT2GCAL_ALERT_URL when one is configured
# (an ntfy.sh topic URL, or any endpoint that accepts a POST body).

set -uo pipefail

_CLI_RED='\033[0;31m'
_CLI_CYAN='\033[0;36m'
_CLI_NC='\033[0m'

log_error() { echo -e "${_CLI_RED}[✗]${_CLI_NC} $1"; }
log_info() { echo -e "${_CLI_CYAN}[i]${_CLI_NC} $1"; }

host="$(hostname)"
lines="$(journalctl -u tt2gcal.service -n 20 --no-pager --output=cat 2>/dev/null || true)"

log_error "tt2gcal sync failed on ${host}"
if [[ -n "$lines" ]]; then
  log_info "last 20 journal lines follow"
  printf '%s\n' "$lines"
fi

if [[ -n "${TT2GCAL_ALERT_URL:-}" ]]; then
  body="tt2gcal sync failed on ${host}"$'\n\n'"${lines}"
  if curl --silent --show-error --fail --max-time 20 \
       --header "Title: tt2gcal sync failed" \
       --header "Priority: high" \
       --data "$body" \
       "$TT2GCAL_ALERT_URL" >/dev/null; then
    log_info "alert pushed to TT2GCAL_ALERT_URL"
  else
    log_error "could not push to TT2GCAL_ALERT_URL"
  fi
else
  log_info "set TT2GCAL_ALERT_URL in .env to also receive a push notification"
fi
