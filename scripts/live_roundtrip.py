"""Live round-trip check for the update and delete paths.

The unit tests prove the diff engine decides correctly; this proves the decisions
actually land in Google Calendar. Run it after any change to the mapper or the
Google client, and once on the server after deploying.

Both phases are self-cleaning: whatever they disturb, the next sync restores,
because TimeTree is the source of truth and this script never touches TimeTree.

    uv run python scripts/live_roundtrip.py
"""

from __future__ import annotations

import logging
import sys

from tt2gcal.config import Config, load_dotenv
from tt2gcal.google import (
    HASH_KEY,
    SOURCE_KEY,
    SOURCE_VALUE,
    UID_KEY,
    GoogleCalendarClient,
    load_credentials,
)
from tt2gcal.runner import run_sync
from tt2gcal.state import Store
from tt2gcal.timetree import TimeTreeClient

ORPHAN_UID = "tt2gcal-live-roundtrip-orphan"

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def build_clients() -> tuple[Config, TimeTreeClient, GoogleCalendarClient]:
    load_dotenv()
    config = Config.from_env()
    store = Store(config.state_dir)
    google = GoogleCalendarClient.from_credentials(
        load_credentials(config.google_client_id, config.google_client_secret, store),
        store,
        config.calendar_prefix,
        calendar_timezone=config.calendar_timezone,
    )
    timetree = TimeTreeClient(config.timetree_email, config.timetree_password, store)
    return config, timetree, google


def check(label: str, passed: bool, detail: str = "") -> bool:
    print(f"  {'PASS' if passed else 'FAIL'}  {label}{f' — {detail}' if detail else ''}")
    return passed


def test_update(config, timetree, google, calendar_id: str) -> bool:
    """Corrupt one mirrored event, then prove sync repairs it in place."""
    print("UPDATE path")
    index = google.index_synced_events(calendar_id)
    if not index:
        return check("a mirrored event exists to corrupt", False)

    uid, event = next(iter(index.items()))
    original_summary, original_hash = event["summary"], event["extendedProperties"]["private"][
        HASH_KEY]

    google.service.events().patch(
        calendarId=calendar_id,
        eventId=event["id"],
        body={
            "summary": "CORRUPTED BY live_roundtrip",
            "extendedProperties": {"private": {HASH_KEY: "0000000000000000"}},
        },
    ).execute()

    result = run_sync(config, timetree, google)

    after = google.service.events().get(calendarId=calendar_id, eventId=event["id"]).execute()
    ok = check("event id is preserved (updated, not re-created)",
               after["id"] == event["id"])
    ok &= check("summary restored from TimeTree",
                after["summary"] == original_summary,
                f"{after['summary']!r}")
    ok &= check("ttHash restored",
                after["extendedProperties"]["private"][HASH_KEY] == original_hash)
    ok &= check("exactly one update, no inserts or deletes",
                result.applied.get("update") == 1
                and not result.applied.get("insert")
                and not result.applied.get("delete"),
                str(result.applied))
    return ok


def test_delete(config, timetree, google, calendar_id: str) -> bool:
    """Plant an event TimeTree does not have, then prove sync removes it."""
    print("DELETE path")
    planted = google.service.events().insert(
        calendarId=calendar_id,
        body={
            "summary": "PLANTED BY live_roundtrip",
            "start": {"date": "2030-01-01"},
            "end": {"date": "2030-01-02"},
            "extendedProperties": {
                "private": {
                    SOURCE_KEY: SOURCE_VALUE,
                    UID_KEY: ORPHAN_UID,
                    HASH_KEY: "planted",
                }
            },
        },
    ).execute()

    ok = check("planted event is visible to the index",
               ORPHAN_UID in google.index_synced_events(calendar_id))

    result = run_sync(config, timetree, google)

    ok &= check("exactly one delete, nothing else",
                result.applied.get("delete") == 1
                and not result.applied.get("insert")
                and not result.applied.get("update"),
                str(result.applied))
    ok &= check("planted event is gone",
                ORPHAN_UID not in google.index_synced_events(calendar_id))

    # Belt and braces: if the sync failed to remove it, do not leave litter behind.
    if ORPHAN_UID in google.index_synced_events(calendar_id):
        google.delete_event(calendar_id, planted["id"])
    return ok


def test_idempotent(config, timetree, google) -> bool:
    """A run with nothing to do must make no writes at all."""
    print("IDEMPOTENCE")
    result = run_sync(config, timetree, google)
    return check("second consecutive run is a no-op", not result.changed, str(result.applied))


def main() -> int:
    config, timetree, google = build_clients()
    mapping = google.store.get_calendar_map()
    if not mapping:
        print("No calendars mapped yet — run `tt2gcal sync` first.")
        return 2

    calendar_id = next(iter(mapping.values()))
    print(f"Using Google calendar {calendar_id}\n")

    ok = test_update(config, timetree, google, calendar_id)
    print()
    ok &= test_delete(config, timetree, google, calendar_id)
    print()
    ok &= test_idempotent(config, timetree, google)

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
