"""Mirroring TimeTree public calendars (公開行事曆).

The payload shapes below follow `GET /api/v2/public_calendars/<alias>` and its
`/public_events` listing as observed on 2026-10-03, with free text replaced.
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeService
from tt2gcal.colors import ColorMapper
from tt2gcal.google import GoogleCalendarClient
from tt2gcal.mapper import MapperOptions, map_events
from tt2gcal.state import Store
from tt2gcal.timetree import (
    PUBLIC_ID_PREFIX,
    TimeTreeCalendarInfo,
    TimeTreeClient,
    public_event_to_event,
)

CALENDAR = {
    "id": 1000000001,
    "alias_code": "example",
    "name": "Example 行事曆",
    "created_at": 1791026289813,
    "public_calendar_labels": [
        {"label_id": 7, "public_calendar_id": 1000000001, "name": "", "color": 15949708},
    ],
}


def public_event(event_id: str = "3069042614528544650", **overrides) -> dict:
    event = {
        "id": event_id,
        "public_calendar_id": 1000000001,
        "title": "活動日",
        "all_day": True,
        "start_at": 1793404800000,  # 2026-10-31 00:00 UTC
        "end_at": 1793404800000,
        "start_timezone": "UTC",
        "end_timezone": "UTC",
        "region_timezone": "Asia/Taipei",
        "public_calendar_label": {"label_id": 7, "color": 15949708},
        "note": "",
        "overview": "",
        "location_name": "",
        "link_url": "",
        "url": "https://timetr.ee/p/example/30000000001",
        "recurrences": None,
        "parent_id": "",
        "status": 1,
    }
    return {**event, **overrides}


class FakeTimeTree(TimeTreeClient):
    """Serves canned JSON by URL instead of talking to TimeTree."""

    def __init__(self, responses: dict[str, list[dict]]):
        super().__init__("user@example.com", "pw", store=None)
        self.responses = responses
        self.requests: list[tuple[str, dict | None]] = []

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        self.requests.append((url, params))
        return self.responses[url].pop(0)


def test_public_event_is_reshaped_for_the_mapper():
    event = public_event_to_event(public_event(note="說明", link_url="https://example.com"))

    assert event["uuid"] == "3069042614528544650"
    assert event["label_id"] == 7
    assert event["note"] == "說明"
    # `url` is the event's own share link; only the user-entered link is carried.
    assert event["url"] == "https://example.com"
    assert event["location"] is None
    assert event["recurrences"] == []
    assert event["parent_id"] is None


def test_public_event_maps_to_an_all_day_google_event_with_its_label_colour():
    calendar = TimeTreeCalendarInfo(id="public:1", name="x", raw=CALENDAR, is_public=True)
    desired, skipped = map_events(
        [public_event_to_event(public_event())],
        calendar_id=calendar.id,
        labels=calendar.labels,
        color_mapper=ColorMapper(palette={"4": "#f35f8c"}, cache={}),
        options=MapperOptions(),
    )

    assert skipped == {}
    (body,) = desired.values()
    assert body["start"] == {"date": "2026-10-31"}
    assert body["end"] == {"date": "2026-11-01"}
    assert body["colorId"] == "4"
    assert body["extendedProperties"]["private"]["ttCal"] == "public:1"


def test_public_calendar_labels_are_read_from_their_own_key():
    calendar = TimeTreeCalendarInfo(id="public:1", name="x", raw=CALENDAR, is_public=True)
    assert calendar.labels == {7: {"name": "", "color": "#f35f8c"}}


def test_public_calendars_are_looked_up_by_alias_with_a_prefixed_id():
    client = FakeTimeTree({
        "https://timetreeapp.com/api/v2/public_calendars/example": [{"public_calendar": CALENDAR}],
    })

    (calendar,) = client.public_calendars(("example",))

    assert calendar.id == f"{PUBLIC_ID_PREFIX}1000000001"
    assert calendar.name == "Example 行事曆"
    assert calendar.is_public


def test_public_events_page_through_the_cursor_from_the_calendar_creation_date():
    url = "https://timetreeapp.com/api/v2/public_calendars/example/public_events"
    client = FakeTimeTree({url: [
        {"public_events": [public_event("a")], "paging": {"next": True, "next_cursor": "c1"}},
        {"public_events": [public_event("b")], "paging": {"next": False, "next_cursor": None}},
    ]})
    calendar = TimeTreeCalendarInfo(id="public:1", name="x", raw=CALENDAR, is_public=True)

    events = client.events(calendar)

    assert [e["uuid"] for e in events] == ["a", "b"]
    (_, first), (_, second) = client.requests
    assert first["from"] == CALENDAR["created_at"]
    assert first["to"] > first["from"]
    assert "cursor" not in first
    assert second["cursor"] == "c1"


@pytest.mark.parametrize(("prefix", "expected"), [
    (None, "TimeTree · Example"),
    ("", "Example"),
])
def test_calendar_name_prefix_can_be_overridden_per_calendar(tmp_path, prefix, expected):
    service = FakeService()
    google = GoogleCalendarClient(service, Store(tmp_path), "TimeTree · ",
                                  allow_unverified_create=True)

    google.resolve_calendar("public:1", "Example", prefix=prefix)

    (created,) = service.calendars().inserted
    assert created["summary"] == expected
