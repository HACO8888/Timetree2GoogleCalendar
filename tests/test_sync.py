"""Diff engine and Google-side indexing."""

from __future__ import annotations

import pytest

from tests.fakes import FakeEventsResource, FakeService
from tt2gcal.google import GoogleCalendarClient
from tt2gcal.state import Store
from tt2gcal.sync import Action, apply_plan, build_plan


def body(uid: str, summary: str, digest: str) -> dict:
    return {
        "summary": summary,
        "extendedProperties": {"private": {"ttSource": "timetree", "ttUid": uid,
                                           "ttHash": digest}},
    }


def google_event(event_id: str, uid: str, summary: str, digest: str) -> dict:
    return {"id": event_id, **body(uid, summary, digest)}


def test_new_event_is_inserted():
    plan = build_plan("Cal", "gcal", {"a": body("a", "A", "h1")}, {})
    assert [c.action for c in plan.changes] == [Action.INSERT]


def test_changed_hash_updates_in_place_keeping_event_id():
    plan = build_plan(
        "Cal", "gcal",
        {"a": body("a", "A renamed", "h2")},
        {"a": google_event("g1", "a", "A", "h1")},
    )
    (change,) = plan.changes
    assert change.action is Action.UPDATE
    assert change.event_id == "g1"


def test_unchanged_hash_produces_no_change():
    plan = build_plan(
        "Cal", "gcal",
        {"a": body("a", "A", "h1")},
        {"a": google_event("g1", "a", "A", "h1")},
    )
    assert plan.is_empty


def test_missing_from_timetree_is_deleted():
    plan = build_plan("Cal", "gcal", {}, {"a": google_event("g1", "a", "A", "h1")})
    (change,) = plan.changes
    assert change.action is Action.DELETE
    assert change.event_id == "g1"


def test_apply_plan_makes_no_calls_when_nothing_changed(tmp_path):
    events = FakeEventsResource()
    client = GoogleCalendarClient(FakeService(events=events), Store(tmp_path), "TimeTree · ")
    plan = build_plan("Cal", "gcal", {"a": body("a", "A", "h1")},
                      {"a": google_event("g1", "a", "A", "h1")})

    apply_plan(client, plan)

    assert (events.inserted, events.updated, events.deleted) == ([], [], [])


def test_apply_plan_dry_run_writes_nothing(tmp_path):
    events = FakeEventsResource()
    client = GoogleCalendarClient(FakeService(events=events), Store(tmp_path), "TimeTree · ")
    plan = build_plan("Cal", "gcal", {"a": body("a", "A", "h1")},
                      {"b": google_event("g2", "b", "B", "h2")})

    counts = apply_plan(client, plan, dry_run=True)

    assert counts == {"insert": 1, "update": 0, "delete": 1}
    assert (events.inserted, events.updated, events.deleted) == ([], [], [])


@pytest.mark.parametrize("page_sizes", [(1, 1), (3, 2), (5, 5, 1)])
def test_event_index_follows_every_page(page_sizes, tmp_path):
    """A truncated index would make page-2 events look absent and be re-inserted forever.

    maxResults is a cap, not a guarantee, so this is the only check that catches a
    missing nextPageToken loop while the calendar is still small.
    """
    pages, uid = [], 0
    for page_number, size in enumerate(page_sizes):
        items = []
        for _ in range(size):
            uid += 1
            items.append(google_event(f"g{uid}", f"u{uid}", f"E{uid}", "h"))
        page = {"items": items}
        if page_number + 1 < len(page_sizes):
            page["nextPageToken"] = str(page_number + 1)
        pages.append(page)

    events = FakeEventsResource(pages=pages)
    client = GoogleCalendarClient(FakeService(events=events), Store(tmp_path), "TimeTree · ")

    index = client.index_synced_events("gcal")

    assert len(index) == sum(page_sizes)
    assert len(events.list_calls) == len(page_sizes)


def test_duplicate_uids_are_deleted_not_silently_collapsed(tmp_path):
    """A dropped duplicate becomes invisible to the diff forever — never updated,
    never deleted. Two writers racing is enough to create one."""
    events = FakeEventsResource(pages=[{"items": [
        {**google_event("g-old", "u1", "E", "h"), "created": "2026-01-01T00:00:00Z"},
        {**google_event("g-new", "u1", "E", "h"), "created": "2026-02-01T00:00:00Z"},
    ]}])
    client = GoogleCalendarClient(FakeService(events=events), Store(tmp_path), "TimeTree · ")

    index = client.index_synced_events("gcal")

    assert index["u1"]["id"] == "g-old"
    assert events.deleted == ["g-new"]


def test_event_index_filters_to_our_own_events(tmp_path):
    events = FakeEventsResource(pages=[{"items": [
        google_event("g1", "u1", "Ours", "h"),
        {"id": "g2", "summary": "Someone else's"},
    ]}])
    client = GoogleCalendarClient(FakeService(events=events), Store(tmp_path), "TimeTree · ")

    assert list(client.index_synced_events("gcal")) == ["u1"]
    assert events.list_calls[0]["privateExtendedProperty"] == "ttSource=timetree"
