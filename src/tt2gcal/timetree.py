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
from dataclasses import dataclass

from requests.exceptions import HTTPError, RequestException
from timetree_exporter.api.auth import AuthenticationError, login
from timetree_exporter.api.calendar import TimeTreeCalendar

from tt2gcal.state import Store

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimeTreeCalendarInfo:
    """One TimeTree calendar, as we care about it."""

    id: str
    name: str
    raw: dict

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
        for label in self.raw.get("calendar_labels") or []:
            label_id = label.get("id")
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

    def events(self, calendar: TimeTreeCalendarInfo) -> list[dict]:
        """Return every event of one calendar as raw TimeTree dicts.

        Always a full fetch, never an incremental `since` cursor: deletions are
        detected by absence against the Google-side index, which is immune to
        whatever tombstone/cursor semantics the private API happens to use.
        """
        return self._ensure_api().get_events(
            calendar.id,
            calendar.name,
            calendar.raw.get("calendar_users"),
            include_comments=False,
        )
