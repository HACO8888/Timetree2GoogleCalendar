"""Calendar-mapping persistence and its recovery path."""

from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError

from tests.fakes import FakeCalendarListResource, FakeCalendarsResource, FakeService
from tt2gcal.google import CalendarRecoveryError, GoogleCalendarClient
from tt2gcal.state import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state")


def make_client(
    store,
    *,
    existing_calendars=None,
    calendar_list=None,
    calendar_list_status=None,
    allow_unverified_create=False,
):
    service = FakeService(
        calendars=FakeCalendarsResource(existing_calendars or {}),
        calendar_list=FakeCalendarListResource(calendar_list or [], calendar_list_status),
    )
    client = GoogleCalendarClient(
        service, store, "TimeTree · ",
        calendar_timezone="Asia/Taipei",
        allow_unverified_create=allow_unverified_create,
    )
    return client, service


def test_first_run_creates_calendar_and_persists_the_mapping(store):
    client, service = make_client(store)

    calendar_id = client.resolve_calendar("tt-1", "家庭")

    assert service.calendars().inserted == [
        {
            "summary": "TimeTree · 家庭",
            "description": "Mirrored from TimeTree by tt2gcal.",
            "timeZone": "Asia/Taipei",
        }
    ]
    assert store.get_calendar_map() == {"tt-1": calendar_id}


def test_second_run_reuses_the_mapping_instead_of_creating_again(store):
    client, service = make_client(store)
    first = client.resolve_calendar("tt-1", "家庭")

    second = client.resolve_calendar("tt-1", "家庭")

    assert second == first
    assert len(service.calendars().inserted) == 1


def test_lost_mapping_recovers_by_summary_rather_than_creating_a_duplicate(store):
    """Losing state/calendars.json must not spawn a second 'TimeTree · 家庭'."""
    client, service = make_client(
        store,
        existing_calendars={"cal-existing": {"id": "cal-existing"}},
        calendar_list=[{"id": "cal-existing", "summary": "TimeTree · 家庭"}],
    )

    calendar_id = client.resolve_calendar("tt-1", "家庭")

    assert calendar_id == "cal-existing"
    assert service.calendars().inserted == []
    assert store.get_calendar_map() == {"tt-1": "cal-existing"}


def test_stale_mapping_to_a_deleted_calendar_is_replaced(store):
    store.set_calendar_map({"tt-1": "cal-gone"})
    client, service = make_client(store)

    calendar_id = client.resolve_calendar("tt-1", "家庭")

    assert calendar_id != "cal-gone"
    assert len(service.calendars().inserted) == 1


def test_unavailable_calendar_lookup_refuses_to_create_rather_than_risk_a_duplicate(store):
    """"Could not check" and "does not exist" must not collapse into the same answer.

    If calendarList.list is not granted under the chosen scope, silently falling
    through to create would add another calendar on every single run.
    """
    client, service = make_client(store, calendar_list_status=403)

    with pytest.raises(CalendarRecoveryError):
        client.resolve_calendar("tt-1", "家庭")

    assert service.calendars().inserted == []
    assert store.get_calendar_map() == {}


def test_unverified_create_is_possible_only_behind_an_explicit_opt_in(store):
    client, service = make_client(store, calendar_list_status=403, allow_unverified_create=True)

    calendar_id = client.resolve_calendar("tt-1", "家庭")

    assert len(service.calendars().inserted) == 1
    assert store.get_calendar_map() == {"tt-1": calendar_id}


def test_permission_error_on_the_mapped_calendar_is_not_read_as_missing(store):
    """A 403 means we cannot touch it, not that it is gone — creating a twin is wrong."""
    store.set_calendar_map({"tt-1": "cal-forbidden"})
    service = FakeService(
        calendars=FakeCalendarsResource({}, get_status=403),
        calendar_list=FakeCalendarListResource([]),
    )
    client = GoogleCalendarClient(service, store, "TimeTree · ")

    with pytest.raises(HttpError):
        client.resolve_calendar("tt-1", "家庭")

    assert service.calendars().inserted == []


def test_state_files_are_written_with_owner_only_permissions(store, tmp_path):
    path = store.write("probe.json", {"secret": "value"})

    assert path.stat().st_mode & 0o777 == 0o600
    assert store.dir.stat().st_mode & 0o777 == 0o700


def test_corrupt_state_file_reads_as_absent(store):
    store.write("probe.json", {"ok": True})
    (store.dir / "probe.json").write_text("{not json", encoding="utf-8")

    assert store.read("probe.json") is None
