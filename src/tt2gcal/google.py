"""Google Calendar client: OAuth, calendar lifecycle, and paginated event indexing.

Scope is `calendar.app.created` — "make secondary Google calendars, and see, create,
change and delete events on them". The app can only touch calendars it created
itself, so the user's primary calendar is structurally out of reach.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from tt2gcal.colors import DEFAULT_EVENT_COLORS
from tt2gcal.state import GOOGLE_TOKEN_FILE, Store

logger = logging.getLogger(__name__)

SCOPES = [
    # Create secondary calendars and fully manage events on the ones we created.
    # The user's primary calendar is structurally out of reach.
    "https://www.googleapis.com/auth/calendar.app.created",
    # Read-only listing, purely so a lost state/calendars.json can be repaired by
    # matching calendar names instead of creating a duplicate set. Verified
    # necessary: calendarList.list returns 403 under calendar.app.created alone.
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]

# Marks every event this tool owns, so events.list can filter to exactly our events.
SOURCE_KEY = "ttSource"
SOURCE_VALUE = "timetree"
UID_KEY = "ttUid"
CAL_KEY = "ttCal"
HASH_KEY = "ttHash"
LABEL_KEY = "ttLabel"

# events.list caps at 2500, but the cap is a maximum, not a guarantee — always page.
PAGE_SIZE = 2500


class GoogleAuthError(RuntimeError):
    """Raised when Google credentials are missing or unusable."""


class CalendarRecoveryError(RuntimeError):
    """Raised when we cannot prove a calendar does not already exist.

    Creating one anyway is the failure mode this whole mapping exists to prevent:
    if the lookup is permanently unavailable and the mapping keeps getting lost,
    every run would add another duplicate calendar.
    """


def authorize(config_client_id: str, config_client_secret: str, store: Store) -> Credentials:
    """Run the interactive OAuth flow once and persist the refresh token."""
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": config_client_id,
                "client_secret": config_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=SCOPES,
    )
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    store.write(GOOGLE_TOKEN_FILE, _creds_to_dict(creds))
    return creds


def load_credentials(client_id: str, client_secret: str, store: Store) -> Credentials:
    """Load persisted credentials, refreshing the access token if needed."""
    data = store.read(GOOGLE_TOKEN_FILE)
    if not data or not data.get("refresh_token"):
        raise GoogleAuthError(
            "No Google refresh token found. Run `tt2gcal auth-google` first.\n"
            "Note: the OAuth consent screen must be set to 'In production' "
            "(console.cloud.google.com/auth/audience) — in 'Testing' the refresh "
            "token silently expires after 7 days and the schedule breaks weekly."
        )

    granted = set(data.get("scopes") or [])
    if missing := [scope for scope in SCOPES if scope not in granted]:
        raise GoogleAuthError(
            "The stored Google token is missing "
            + ", ".join(missing)
            + ".\nRun `tt2gcal auth-google` again to re-consent with the current scopes."
        )

    creds = Credentials(
        token=data.get("token"),
        refresh_token=data["refresh_token"],
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=client_id,
        client_secret=client_secret,
        scopes=data.get("scopes", SCOPES),
    )
    if not creds.valid:
        creds.refresh(Request())
        store.write(GOOGLE_TOKEN_FILE, _creds_to_dict(creds))
    return creds


def _creds_to_dict(creds: Credentials) -> dict[str, Any]:
    return {
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
        "scopes": list(creds.scopes or SCOPES),
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }


class GoogleCalendarClient:
    """Thin wrapper over the Calendar v3 API with the behaviours this sync needs."""

    def __init__(
        self,
        service: Any,
        store: Store,
        calendar_prefix: str,
        *,
        calendar_timezone: str = "UTC",
        allow_unverified_create: bool = False,
    ):
        self.service = service
        self.store = store
        self.prefix = calendar_prefix
        self.calendar_timezone = calendar_timezone
        self.allow_unverified_create = allow_unverified_create

    @classmethod
    def from_credentials(
        cls,
        credentials: Credentials,
        store: Store,
        calendar_prefix: str,
        *,
        calendar_timezone: str = "UTC",
        allow_unverified_create: bool = False,
    ) -> GoogleCalendarClient:
        """Build a client from OAuth credentials."""
        service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        return cls(service, store, calendar_prefix,
                   calendar_timezone=calendar_timezone,
                   allow_unverified_create=allow_unverified_create)

    # --- palette ---------------------------------------------------------

    def event_palette(self) -> dict[str, str]:
        """Return {colorId: '#rrggbb'} for events, falling back to the known defaults.

        colors.get may not be granted under calendar.app.created; the defaults are
        stable enough that failing the whole sync over a palette lookup is wrong.
        """
        try:
            colors = self.service.colors().get().execute()
        except HttpError as exc:
            logger.warning("colors.get unavailable (%s); using the default palette", exc)
            return dict(DEFAULT_EVENT_COLORS)
        event_colors = colors.get("event") or {}
        palette = {cid: spec["background"] for cid, spec in event_colors.items()
                   if spec.get("background")}
        return palette or dict(DEFAULT_EVENT_COLORS)

    # --- calendar lifecycle ---------------------------------------------

    def resolve_calendar(
        self, tt_cal_id: str, tt_cal_name: str, *, create: bool = True
    ) -> str | None:
        """Return the Google calendar id for a TimeTree calendar, creating it if needed.

        Order matters: the persisted map first, then a summary match against the
        calendars we can see, and only then creation. Skipping the middle step would
        create a duplicate calendar every run whenever state/calendars.json is lost.

        With create=False nothing is written and None is returned when no calendar
        is known yet — a dry run must leave no trace, including no new calendars.
        """
        mapping = self.store.get_calendar_map()
        existing = mapping.get(tt_cal_id)
        if existing and self._calendar_exists(existing):
            return existing

        summary = f"{self.prefix}{tt_cal_name}"
        if not create:
            return None
        recovered = self._find_calendar_by_summary(summary)
        if recovered:
            logger.info("Recovered calendar mapping for '%s' -> %s", summary, recovered)
        else:
            recovered = self._create_calendar(summary)
            logger.info("Created Google calendar '%s' -> %s", summary, recovered)

        mapping[tt_cal_id] = recovered
        self.store.set_calendar_map(mapping)
        return recovered

    def _calendar_exists(self, calendar_id: str) -> bool:
        try:
            self.service.calendars().get(calendarId=calendar_id).execute()
            return True
        except HttpError as exc:
            if exc.resp.status in (404, 410):
                return False
            # 403 here means a permission problem, not a missing calendar. Treating it
            # as missing would send us down the create path against a calendar that
            # very much exists.
            raise

    def _find_calendar_by_summary(self, summary: str) -> str | None:
        """Return the id of a calendar with this exact summary, or None if there is none.

        Raises CalendarRecoveryError when the lookup itself is unavailable, because
        "we could not check" and "there is no such calendar" must not collapse into
        the same answer — the second one leads to creating a duplicate.
        """
        try:
            page_token = None
            while True:
                response = self.service.calendarList().list(pageToken=page_token).execute()
                for item in response.get("items", []):
                    if item.get("summary") == summary:
                        return item["id"]
                page_token = response.get("nextPageToken")
                if not page_token:
                    return None
        except HttpError as exc:
            if self.allow_unverified_create:
                logger.warning(
                    "calendarList.list unavailable (%s); creating '%s' unverified because "
                    "allow_unverified_create is set",
                    exc, summary,
                )
                return None
            raise CalendarRecoveryError(
                f"Cannot look up existing calendars ({exc}), so '{summary}' cannot be "
                "created safely — a duplicate would be added on every run.\n"
                "Fix one of:\n"
                "  1. Grant a scope that permits calendarList.list and re-run "
                "`tt2gcal auth-google`; or\n"
                "  2. Put the Google calendar id into state/calendars.json manually; or\n"
                "  3. Re-run with --allow-unverified-create if you are certain no such "
                "calendar exists yet."
            ) from exc

    def _create_calendar(self, summary: str) -> str:
        created = self.service.calendars().insert(
            body={
                "summary": summary,
                "description": "Mirrored from TimeTree by tt2gcal.",
                "timeZone": self.calendar_timezone,
            }
        ).execute()
        return created["id"]

    # --- events ----------------------------------------------------------

    def iter_synced_events(self, calendar_id: str) -> Iterator[dict]:
        """Yield every event this tool owns on a calendar, following every page.

        Paging is mandatory. If the index were ever truncated, the missing events
        would look absent to the diff and be re-inserted on every single run.
        """
        page_token = None
        while True:
            response = self.service.events().list(
                calendarId=calendar_id,
                privateExtendedProperty=f"{SOURCE_KEY}={SOURCE_VALUE}",
                singleEvents=False,
                showDeleted=False,
                maxResults=PAGE_SIZE,
                pageToken=page_token,
            ).execute()
            yield from response.get("items", [])
            page_token = response.get("nextPageToken")
            if not page_token:
                return

    def index_synced_events(self, calendar_id: str) -> dict[str, dict]:
        """Return {ttUid: google event} for every event this tool owns.

        Duplicate ttUids are removed rather than silently collapsed. A plain dict
        would keep one copy and drop the other, and the dropped one becomes
        invisible to the diff forever — never updated, never deleted. Two writers
        racing (a manual local run overlapping the server's timer) is enough to
        create one, so the index repairs it instead of hiding it.
        """
        index: dict[str, dict] = {}
        for event in self.iter_synced_events(calendar_id):
            uid = (event.get("extendedProperties", {}).get("private", {}) or {}).get(UID_KEY)
            if not uid:
                continue
            previous = index.get(uid)
            if previous is None:
                index[uid] = event
                continue

            # Keep the older copy: it is the one whose id other state may reference.
            keep, drop = sorted((previous, event), key=lambda e: e.get("created", ""))
            logger.warning(
                "Duplicate ttUid %s on calendar %s (events %s and %s); deleting %s",
                uid, calendar_id, previous["id"], event["id"], drop["id"],
            )
            self.delete_event(calendar_id, drop["id"])
            index[uid] = keep
        return index

    def insert_event(self, calendar_id: str, body: dict) -> dict:
        """Create one event."""
        return self.service.events().insert(calendarId=calendar_id, body=body).execute()

    def update_event(self, calendar_id: str, event_id: str, body: dict) -> dict:
        """Replace one event in place, keeping its Google event id."""
        return self.service.events().update(
            calendarId=calendar_id, eventId=event_id, body=body
        ).execute()

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Delete one event, tolerating an already-deleted target."""
        try:
            self.service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        except HttpError as exc:
            if exc.resp.status in (404, 410):
                logger.debug("Event %s already gone", event_id)
                return
            raise
