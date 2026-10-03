"""TimeTree client: session persistence on top of timetree_exporter's private API layer.

TimeTree has no official API (the public one was shut down on 2023-12-22), so this
talks to the web app's private endpoints via the reverse-engineered client in
`timetree_exporter`. That package is pinned exactly in pyproject.toml because we
depend on its internals, not just its CLI.

Session persistence is a hard requirement, not an optimisation: the sign-in endpoint
is rate limited (error code -495), and a 15-minute schedule would otherwise hammer it
~96 times a day.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from requests.exceptions import HTTPError, RequestException
from timetree_exporter.api.auth import AuthenticationError, login
from timetree_exporter.api.calendar import TimeTreeCalendar
from timetree_exporter.api.const import API_USER_AGENT

from tt2gcal.state import Store

logger = logging.getLogger(__name__)

PUBLIC_API = "https://timetreeapp.com/api/v2/public_calendars"
PUBLIC_PAGE_SIZE = 100
# The public events endpoint only answers for an explicit [from, to) window. It
# starts at the calendar's creation so old events never fall out of the window
# and get deleted from Google; the end just has to outlive any real plan.
PUBLIC_HORIZON_MS = 3 * 366 * 24 * 60 * 60 * 1000
# Prefixed so a public calendar id can never collide with a shared calendar id in
# state/calendars.json or in the ttCal event property.
PUBLIC_ID_PREFIX = "public:"


@dataclass(frozen=True)
class TimeTreeCalendarInfo:
    """One TimeTree calendar, as we care about it."""

    id: str
    name: str
    raw: dict
    is_public: bool = False

    @property
    def alias_code(self) -> str | None:
        """Return the calendar's alias code (the code shown in share links)."""
        return self.raw.get("alias_code")

    @property
    def is_active(self) -> bool:
        """False once the calendar has been left or removed."""
        return self.raw.get("deactivated_at") is None

    @property
    def labels(self) -> dict[int, dict]:
        """Return {label_id: {"name": str, "color": "#rrggbb"}} from the inline copy.

        `GET /calendars` already embeds `calendar_labels`, so the dedicated
        `/labels` endpoint would just be one extra request per calendar per run
        against a rate-limited private API.
        """
        result: dict[int, dict] = {}
        raw_labels = self.raw.get("public_calendar_labels" if self.is_public else "calendar_labels")
        for label in raw_labels or []:
            label_id = label.get("label_id" if self.is_public else "id")
            if label_id is None:
                continue
            color = label.get("color")
            result[label_id] = {
                "name": label.get("name") or "",
                "color": f"#{color:06x}" if isinstance(color, int) else color,
            }
        return result


class TimeTreeClient:
    """Fetches calendars, labels and events, re-authenticating when the session dies."""

    def __init__(self, email: str, password: str, store: Store):
        self._email = email
        self._password = password
        self._store = store
        self._api: TimeTreeCalendar | None = None

    # --- authentication --------------------------------------------------

    def _build_api(self, session_id: str) -> TimeTreeCalendar:
        return TimeTreeCalendar(session_id)

    def _login(self) -> TimeTreeCalendar:
        """Sign in with email + password and persist the new session cookie."""
        logger.info("Signing in to TimeTree as %s", self._email)
        session_id = login(self._email, self._password)
        if not session_id:
            raise AuthenticationError("Sign-in succeeded but no _session_id cookie was returned")
        self._store.set_session_id(session_id)
        return self._build_api(session_id)

    def _ensure_api(self) -> TimeTreeCalendar:
        if self._api is None:
            session_id = self._store.get_session_id()
            self._api = self._build_api(session_id) if session_id else self._login()
        return self._api

    def _reauthenticate(self) -> TimeTreeCalendar:
        """Discard the stored session and sign in again."""
        self._store.clear_session()
        self._api = self._login()
        return self._api

    # --- data ------------------------------------------------------------

    def calendars(self) -> list[TimeTreeCalendarInfo]:
        """Return every calendar on the account, refreshing the session if needed.

        This is the first call of every run, so it doubles as the session probe:
        the private API gives us no reliable way to tell "expired session" from
        other failures, so any failure here triggers exactly one re-login + retry.
        """
        api = self._ensure_api()
        try:
            raw = api.get_metadata()
        except (HTTPError, RequestException, KeyError) as exc:
            logger.info("Calendar listing failed (%s); re-authenticating once", exc)
            api = self._reauthenticate()
            raw = api.get_metadata()

        return [
            TimeTreeCalendarInfo(id=str(cal["id"]), name=cal.get("name") or str(cal["id"]), raw=cal)
            for cal in raw
        ]

    def public_calendars(self, aliases: tuple[str, ...]) -> list[TimeTreeCalendarInfo]:
        """Return the public calendars (公開行事曆) named by their alias codes.

        Public calendars never appear in `/calendars`, so they are opt-in by alias.
        A missing alias raises: silently skipping it would delete its whole Google
        mirror on the next run.
        """
        result = []
        for alias in aliases:
            raw = self._get_json(f"{PUBLIC_API}/{alias}")["public_calendar"]
            result.append(TimeTreeCalendarInfo(
                id=f"{PUBLIC_ID_PREFIX}{raw['id']}", name=raw.get("name") or alias,
                raw=raw, is_public=True,
            ))
        return result

    def events(self, calendar: TimeTreeCalendarInfo) -> list[dict]:
        """Return every event of one calendar as raw TimeTree dicts.

        Always a full fetch, never an incremental `since` cursor: deletions are
        detected by absence against the Google-side index, which is immune to
        whatever tombstone/cursor semantics the private API happens to use.
        """
        if calendar.is_public:
            return self._public_events(calendar)
        return self._ensure_api().get_events(
            calendar.id,
            calendar.name,
            calendar.raw.get("calendar_users"),
            include_comments=False,
        )

    def _public_events(self, calendar: TimeTreeCalendarInfo) -> list[dict]:
        """Fetch every published event of a public calendar, in the shared-event shape."""
        params = {
            "from": calendar.raw.get("created_at") or 0,
            "to": int(time.time() * 1000) + PUBLIC_HORIZON_MS,
            "utc_offset": 0,
            "limit": PUBLIC_PAGE_SIZE,
        }
        url = f"{PUBLIC_API}/{calendar.raw['alias_code']}/public_events"
        events: list[dict] = []
        while True:
            page = self._get_json(url, params)
            events += [public_event_to_event(e) for e in page.get("public_events") or []]
            paging = page.get("paging") or {}
            if not (paging.get("next") and paging.get("next_cursor")):
                return events
            params = {**params, "cursor": paging["next_cursor"]}

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        response = self._ensure_api().session.get(
            url, params=params, headers={"X-Timetreea": API_USER_AGENT}
        )
        response.raise_for_status()
        return response.json()


def public_event_to_event(event: dict) -> dict:
    """Reshape a public calendar event into the shared-calendar event the mapper reads.

    Only fields that exist on both sides are carried. `url` on a public event is
    its own timetr.ee share link, not a user-entered URL, so `link_url` is used.
    No `type`/`category`: public calendars have no birthdays or memos.
    """
    label = event.get("public_calendar_label") or {}
    return {
        "uuid": str(event["id"]),
        "title": event.get("title"),
        "all_day": event.get("all_day"),
        "start_at": event.get("start_at"),
        "end_at": event.get("end_at"),
        "start_timezone": event.get("start_timezone"),
        "end_timezone": event.get("end_timezone"),
        "label_id": label.get("label_id"),
        "note": event.get("note") or event.get("overview") or None,
        "location": event.get("location_name") or None,
        "url": event.get("link_url") or None,
        "recurrences": event.get("recurrences") or [],
        "parent_id": event.get("parent_id") or None,
        "alerts": [],
    }
