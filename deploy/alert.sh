#!/usr/bin/env bash
# Called by systemd's OnFailure when a sync run exits non-zero.
#
# TimeTree's private API can change or start blocking at any time, and a silent
# failure looks exactly like "nothing to sync". So this always writes to the
# journal, and additionally pushes to TT2GCAL_ALERT_URL when one is configured.
#
# Two wire formats are supported, chosen from the URL:
#   - Discord webhooks want JSON {"content": ...} and cap at 2000 characters
#   - anything else (ntfy.sh, custom endpoints) gets the plain text body
#
# TT2GCAL_ALERT_URL is a credential: anyone holding it can post to that channel.
# It lives in .env, which is gitignored and mode 600.
#
# TT2GCAL_ALERT_NAME and TT2GCAL_ALERT_AVATAR override the Discord webhook's
# display name and icon per message, so alerts are recognisable in a busy
# channel. Leave the avatar unset to keep whatever the webhook is configured
# with in Discord's own settings.

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

if [[ -z "${TT2GCAL_ALERT_URL:-}" ]]; then
  log_info "set TT2GCAL_ALERT_URL in .env to also receive a push notification"
  exit 0
fi

if [[ "$TT2GCAL_ALERT_URL" == *"discord.com/api/webhooks"* ]]; then
  payload="$(
    HOST="$host" LINES="$lines" \
    NAME="${TT2GCAL_ALERT_NAME:-TimeTree Sync}" AVATAR="${TT2GCAL_ALERT_AVATAR:-}" \
    python3 - <<'PY'
import json, os

host = os.environ["HOST"]
lines = os.environ.get("LINES", "").strip()

# The webhook username already says which service this is, so the line itself
# only has to say what broke and where.
header = f":rotating_light: **Sync failed** on `{host}`"
if lines:
    # Discord caps content at 2000 characters; keep the tail, which holds the error.
    budget = 2000 - len(header) - len("\n```\n\n```") - 16
    if len(lines) > budget:
        lines = "...(truncated)\n" + lines[-budget:]
    body = f"{header}\n```\n{lines}\n```"
else:
    body = f"{header}\n(no journal output available)"

payload = {"content": body, "username": os.environ.get("NAME") or "TimeTree Sync"}
if os.environ.get("AVATAR"):
    payload["avatar_url"] = os.environ["AVATAR"]

print(json.dumps(payload))
PY
  )"
  if curl --silent --show-error --fail --max-time 20 \
       --header "Content-Type: application/json" \
       --data "$payload" \
       "$TT2GCAL_ALERT_URL" >/dev/null; then
    log_info "alert pushed to Discord"
  else
    log_error "could not push the alert to Discord"
  fi
else
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
fi
