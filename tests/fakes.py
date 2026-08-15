"""Minimal fakes for the Google Calendar discovery service.

The real client is `service.events().list(...).execute()` all the way down, so the
fakes mirror that shape rather than being mocked per-call.
"""

from __future__ import annotations

from typing import Any


class _Request:
    def __init__(self, result: Any):
        self._result = result

    def execute(self) -> Any:
        return self._result


class FakeEventsResource:
    """Records writes and serves paginated list responses."""

    def __init__(self, pages: list[dict] | None = None):
        self.pages = pages or [{"items": []}]
        self.list_calls: list[dict] = []
        self.inserted: list[dict] = []
        self.updated: list[tuple[str, dict]] = []
        self.deleted: list[str] = []

    def list(self, **kwargs) -> _Request:
        self.list_calls.append(kwargs)
        token = kwargs.get("pageToken")
        index = 0 if token is None else int(token)
        return _Request(self.pages[index])

    def insert(self, calendarId: str, body: dict) -> _Request:  # noqa: N803 (Google's casing)
        self.inserted.append(body)
        return _Request({"id": f"new-{len(self.inserted)}", **body})

    def update(self, calendarId: str, eventId: str, body: dict) -> _Request:  # noqa: N803
        self.updated.append((eventId, body))
        return _Request({"id": eventId, **body})

    def delete(self, calendarId: str, eventId: str) -> _Request:  # noqa: N803
        self.deleted.append(eventId)
        return _Request(None)


class FakeCalendarsResource:
    def __init__(self, existing: dict[str, dict] | None = None, get_status: int | None = None):
        self.existing = existing or {}
        self.get_status = get_status
        self.inserted: list[dict] = []

    def get(self, calendarId: str) -> _Request:  # noqa: N803
        if self.get_status:
            raise _http_error(self.get_status)
        if calendarId not in self.existing:
            raise _http_error(404)
        return _Request(self.existing[calendarId])

    def insert(self, body: dict) -> _Request:
        self.inserted.append(body)
        new_id = f"cal-{len(self.inserted)}"
        self.existing[new_id] = {"id": new_id, **body}
        return _Request(self.existing[new_id])


class FakeCalendarListResource:
    def __init__(self, items: list[dict] | None = None, raise_status: int | None = None):
        self.items = items or []
        self.raise_status = raise_status

    def list(self, pageToken=None) -> _Request:  # noqa: N803
        if self.raise_status:
            raise _http_error(self.raise_status)
        return _Request({"items": self.items})


class FakeService:
    """Stands in for the googleapiclient discovery service."""

    def __init__(
        self,
        events: FakeEventsResource | None = None,
        calendars: FakeCalendarsResource | None = None,
        calendar_list: FakeCalendarListResource | None = None,
    ):
        self._events = events or FakeEventsResource()
        self._calendars = calendars or FakeCalendarsResource()
        self._calendar_list = calendar_list or FakeCalendarListResource()

    def events(self) -> FakeEventsResource:
        return self._events

    def calendars(self) -> FakeCalendarsResource:
        return self._calendars

    def calendarList(self) -> FakeCalendarListResource:  # noqa: N802 (Google's casing)
        return self._calendar_list


def _http_error(status: int):
    """Build a googleapiclient HttpError with the given status."""
    from googleapiclient.errors import HttpError

    class _Resp:
        def __init__(self, status: int):
            self.status = status
            self.reason = "fake"

    return HttpError(_Resp(status), b"{}")
