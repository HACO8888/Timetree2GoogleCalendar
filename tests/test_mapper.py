"""Mapping TimeTree events onto Google event bodies.

Fixtures in tests/fixtures/timetree_events.json are real payloads captured with
`tt2gcal recon`, with free text replaced. Synthetic fixtures would have hidden
exactly the details this mapper exists to get right.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tt2gcal.colors import ColorMapper
from tt2gcal.mapper import (
    MapperOptions,
    content_hash,
    map_events,
    should_skip,
    time_fields,
    to_google_event,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures" / "timetree_events.json").read_text(encoding="utf-8")
)
LABELS = {
    label["id"]: {"name": label["name"], "color": f"#{label['color']:06x}"}
    for label in FIXTURES["labels"]
}


def fixture(kind: str) -> dict:
    """Return a deep copy of the captured event of the given kind."""
    for event in FIXTURES["events"]:
        if event["_fixture"] == kind:
            return copy.deepcopy(event)
    raise AssertionError(f"no fixture of kind {kind!r}")


def render(event: dict, **overrides) -> dict:
    options = overrides.pop("options", MapperOptions())
    return to_google_event(
        event,
        calendar_id=str(event["calendar_id"]),
        labels=LABELS,
        color_mapper=ColorMapper(),
        options=options,
        **overrides,
    )


# --- skipping ------------------------------------------------------------


def test_tombstoned_events_are_skipped_so_deletions_do_not_resurrect():
    event = fixture("timed")
    event["deactivated_at"] = 1782651974427
    assert should_skip(event, MapperOptions()) == "deactivated"


def test_live_events_are_not_skipped():
    assert should_skip(fixture("timed"), MapperOptions()) is None


def test_birthdays_are_skipped_by_default_and_kept_on_request():
    event = fixture("birthday")
    assert should_skip(event, MapperOptions()) == "birthday"
    assert should_skip(event, MapperOptions(include_birthdays=True)) is None


def test_memos_are_skipped_by_default():
    event = fixture("timed")
    event["category"] = 2
    assert should_skip(event, MapperOptions()) == "memo"


def test_events_without_times_are_skipped_rather_than_crashing():
    event = fixture("timed")
    event["start_at"] = None
    assert should_skip(event, MapperOptions()) == "no start/end"


# --- times ---------------------------------------------------------------


def test_all_day_uses_dates_with_an_exclusive_end():
    """TimeTree sends start_at == end_at for a one-day event; Google's end is exclusive."""
    event = fixture("all_day")
    assert event["start_at"] == event["end_at"], "fixture assumption"

    start, end = time_fields(event)

    assert set(start) == {"date"} and set(end) == {"date"}
    assert _days_between(start, end) == 1


def test_multi_day_all_day_keeps_its_span_plus_the_exclusive_day():
    """TimeTree's end date is inclusive, so a 4-day span must reach Google as 5 dates."""
    event = fixture("multi_day")
    inclusive_days = (event["end_at"] - event["start_at"]) // 86_400_000

    start, end = time_fields(event)

    assert _days_between(start, end) == inclusive_days + 1


def test_timed_events_carry_their_own_zone():
    start, end = time_fields(fixture("timed"))

    assert start["timeZone"] == "Asia/Taipei"
    assert start["dateTime"].endswith("+08:00")
    assert end["dateTime"] > start["dateTime"]


def test_zero_length_timed_events_are_given_a_minimum_duration():
    """TimeTree allows a point in time; Google rejects a zero-length timed event."""
    event = fixture("zero_duration")
    assert event["start_at"] == event["end_at"], "fixture assumption"

    start, end = time_fields(event)

    assert _minutes_between(start, end) == 1


def test_an_end_before_its_start_is_repaired_rather_than_sent():
    event = fixture("timed")
    event["end_at"] = event["start_at"] - 3_600_000

    start, end = time_fields(event)

    assert end["dateTime"] > start["dateTime"]


def _days_between(start: dict, end: dict) -> int:
    from datetime import date

    return (date.fromisoformat(end["date"]) - date.fromisoformat(start["date"])).days


def _minutes_between(start: dict, end: dict) -> float:
    from datetime import datetime

    delta = datetime.fromisoformat(end["dateTime"]) - datetime.fromisoformat(start["dateTime"])
    return delta.total_seconds() / 60


def test_unknown_timezone_falls_back_to_utc_instead_of_failing():
    event = fixture("timed")
    event["start_timezone"] = event["end_timezone"] = "Mars/Olympus_Mons"

    start, _ = time_fields(event)

    assert start["dateTime"].endswith("+00:00")


# --- body ----------------------------------------------------------------


def test_reminders_map_one_to_one_from_alerts():
    body = render(fixture("timed"))
    assert body["reminders"] == {
        "useDefault": False,
        "overrides": [{"method": "popup", "minutes": 60}],
    }


def test_all_day_reminder_minutes_pass_through_unchanged():
    """900 is Google's own encoding of '1 day before at 09:00'; rewriting it would be wrong."""
    body = render(fixture("all_day"))
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 900}]


def test_no_alerts_means_no_reminders_rather_than_google_defaults():
    body = render(fixture("no_alerts"))
    assert body["reminders"] == {"useDefault": False}


def test_at_most_five_reminder_overrides_are_sent():
    event = fixture("timed")
    event["alerts"] = [5, 10, 15, 20, 25, 30, 60]
    assert len(render(event)["reminders"]["overrides"]) == 5


def test_label_colour_becomes_a_google_color_id():
    body = render(fixture("timed"))
    assert body["colorId"] in {str(n) for n in range(1, 12)}


def test_label_name_is_carried_in_private_properties_not_the_title():
    event = fixture("timed")
    event["label_id"] = next(lid for lid, spec in LABELS.items() if spec["name"])

    body = render(event)

    assert body["summary"] == event["title"]
    assert body["extendedProperties"]["private"]["ttLabel"] == LABELS[event["label_id"]]["name"]


def test_label_reaches_the_description_only_when_asked():
    event = fixture("timed")
    event["label_id"] = next(lid for lid, spec in LABELS.items() if spec["name"])
    name = LABELS[event["label_id"]]["name"]

    assert name not in (render(event).get("description") or "")
    assert name in render(event, options=MapperOptions(label_in_description=True))["description"]


def test_untitled_events_get_a_placeholder_rather_than_an_empty_summary():
    event = fixture("timed")
    event["title"] = ""
    assert render(event)["summary"] == "(無標題)"


def test_identity_properties_are_attached():
    event = fixture("timed")
    private = render(event)["extendedProperties"]["private"]

    assert private["ttSource"] == "timetree"
    assert private["ttUid"] == event["uuid"]
    assert private["ttCal"] == str(event["calendar_id"])
    assert len(private["ttHash"]) == 16


# --- hashing -------------------------------------------------------------


def test_hash_is_stable_across_repeated_mapping():
    """An unstable hash would rewrite every event on every run."""
    event = fixture("timed")
    first = render(event)["extendedProperties"]["private"]["ttHash"]
    second = render(copy.deepcopy(event))["extendedProperties"]["private"]["ttHash"]
    assert first == second


@pytest.mark.parametrize(
    ("field", "value"),
    [("title", "Different"), ("start_at", 1788526800000 + 3600_000), ("note", "New note")],
)
def test_hash_changes_when_visible_content_changes(field, value):
    event = fixture("timed")
    before = render(event)["extendedProperties"]["private"]["ttHash"]
    event[field] = value
    assert render(event)["extendedProperties"]["private"]["ttHash"] != before


@pytest.mark.parametrize("field", ["updated_at", "like_count", "primary_id", "row_order"])
def test_hash_ignores_fields_that_do_not_reach_google(field):
    event = fixture("timed")
    before = render(event)["extendedProperties"]["private"]["ttHash"]
    event[field] = 999999
    assert render(event)["extendedProperties"]["private"]["ttHash"] == before


def test_hash_excludes_extended_properties_so_it_cannot_depend_on_itself():
    body = {"summary": "A", "extendedProperties": {"private": {"ttHash": "whatever"}}}
    assert content_hash(body) == content_hash({"summary": "A"})


# --- whole-calendar mapping ---------------------------------------------


def test_map_events_skips_and_counts():
    events = [fixture(k) for k in ("timed", "all_day", "birthday")]

    desired, skipped = map_events(
        events, calendar_id="c1", labels=LABELS, color_mapper=ColorMapper(),
        options=MapperOptions(),
    )

    assert len(desired) == 2
    assert skipped == {"birthday": 1}


def test_modified_occurrence_is_refused_rather_than_double_booked():
    """TimeTree's exception representation is unverified (notes question B).

    Inserting both a master and its modified occurrence would put two events on
    that date; skipping is visible in the logs, double-booking is not.
    """
    master = fixture("timed")
    master["recurrences"] = ["RRULE:FREQ=WEEKLY"]
    child = fixture("no_alerts")
    child["recurring_uuid"] = master["uuid"]

    desired, skipped = map_events(
        [master, child], calendar_id="c1", labels=LABELS, color_mapper=ColorMapper(),
        options=MapperOptions(),
    )

    assert list(desired) == [master["uuid"]]
    assert skipped == {"recurrence exception (unsupported)": 1}


def test_occurrence_is_refused_even_when_its_master_is_absent():
    """`recurring_uuid` may be a series id, not the master's uuid.

    A guard that first checked "is the master in this payload?" would never fire
    in that case, and the child would be inserted as a standalone event while the
    master's RRULE still covered that date.
    """
    child = fixture("timed")
    child["recurring_uuid"] = "some-series-id-with-no-matching-event"

    desired, skipped = map_events(
        [child], calendar_id="c1", labels=LABELS, color_mapper=ColorMapper(),
        options=MapperOptions(),
    )

    assert desired == {}
    assert skipped == {"recurrence exception (unsupported)": 1}


def test_recurrence_lines_pass_through_to_google():
    event = fixture("timed")
    event["recurrences"] = ["RRULE:FREQ=WEEKLY;BYDAY=MO"]
    assert render(event)["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]


def test_one_off_events_carry_no_recurrence_key():
    assert "recurrence" not in render(fixture("timed"))
