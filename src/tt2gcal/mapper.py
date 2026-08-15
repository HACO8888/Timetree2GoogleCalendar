"""Turn raw TimeTree event dicts into Google Calendar event bodies.

Field shapes here are not guessed — they come from `tt2gcal recon` against a live
account and are written down in docs/timetree-api-notes.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tt2gcal.colors import ColorMapper
from tt2gcal.google import CAL_KEY, HASH_KEY, LABEL_KEY, SOURCE_KEY, SOURCE_VALUE, UID_KEY

logger = logging.getLogger(__name__)

BIRTHDAY_TYPE = 1
MEMO_CATEGORY = 2

# Google rejects an event with more than five reminder overrides.
MAX_REMINDERS = 5

# TimeTree allows a timed event with start_at == end_at (3 of 22 in the sampled
# account) — a point in time. Google rejects a zero-length timed event, so give it
# the smallest duration that is not zero. Inventing a minute is a smaller lie than
# inventing an hour, and far better than the event silently failing every run.
MIN_TIMED_DURATION = timedelta(minutes=1)


@dataclass(frozen=True)
class MapperOptions:
    """What the mapper is allowed to emit."""

    include_birthdays: bool = False
    include_memos: bool = False
    label_in_description: bool = False


class SkipReason(str):
    """Why an event was not mapped (used for logging only)."""


def should_skip(event: dict, options: MapperOptions) -> str | None:
    """Return a reason string if this event must not be mirrored, else None."""
    if event.get("deactivated_at") is not None:
        # A tombstone. Mirroring it would resurrect an event the user deleted.
        return "deactivated"
    if not event.get("uuid"):
        return "no uuid"
    if event.get("start_at") is None or event.get("end_at") is None:
        return "no start/end"
    if event.get("type") == BIRTHDAY_TYPE and not options.include_birthdays:
        return "birthday"
    if event.get("category") == MEMO_CATEGORY and not options.include_memos:
        return "memo"
    return None


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name else UTC
    except Exception:  # noqa: BLE001 — an unknown zone must not kill the whole sync
        logger.warning("Unknown time zone %r; falling back to UTC", name)
        return UTC


def _to_datetime(epoch_ms: int, tz_name: str | None) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=_zone(tz_name))


def time_fields(event: dict) -> tuple[dict, dict]:
    """Return (start, end) in Google's shape.

    All-day events arrive with start_at == end_at for a single day and a UTC zone;
    Google's end date is exclusive, so a one-day event ends on the following day.
    """
    start_tz = event.get("start_timezone")
    end_tz = event.get("end_timezone") or start_tz
    start_dt = _to_datetime(event["start_at"], start_tz)
    end_dt = _to_datetime(event["end_at"], end_tz)

    if event.get("all_day"):
        # TimeTree's end date is inclusive (a one-day event has start_at == end_at),
        # Google's is exclusive, so a multi-day span maps by adding a single day.
        return (
            {"date": start_dt.date().isoformat()},
            {"date": (max(end_dt.date(), start_dt.date()) + timedelta(days=1)).isoformat()},
        )

    if end_dt <= start_dt:
        logger.debug(
            "Event %s has no duration (%s); extending it to %s so Google accepts it",
            event.get("uuid"), start_dt.isoformat(), MIN_TIMED_DURATION,
        )
        end_dt = start_dt + MIN_TIMED_DURATION
        end_tz = start_tz

    return (
        {"dateTime": start_dt.isoformat(), "timeZone": start_tz or "UTC"},
        {"dateTime": end_dt.isoformat(), "timeZone": end_tz or "UTC"},
    )


def reminders(event: dict) -> dict:
    """Map TimeTree alerts (minutes before start) onto Google reminder overrides."""
    alerts = [a for a in (event.get("alerts") or []) if isinstance(a, int) and a >= 0]
    if not alerts:
        return {"useDefault": False}
    overrides = [{"method": "popup", "minutes": minutes} for minutes in sorted(set(alerts))]
    if len(overrides) > MAX_REMINDERS:
        logger.debug("Trimming %d reminders to %d for %s", len(overrides), MAX_REMINDERS,
                     event.get("uuid"))
        overrides = overrides[:MAX_REMINDERS]
    return {"useDefault": False, "overrides": overrides}


def description(event: dict, label_name: str | None, options: MapperOptions) -> str | None:
    """Build the description from the note, the event URL, and optionally the label."""
    parts = []
    if options.label_in_description and label_name:
        parts.append(f"[{label_name}]")
    if event.get("note"):
        parts.append(event["note"])
    if event.get("url"):
        parts.append(event["url"])
    return "\n\n".join(parts) or None


def recurrence(event: dict) -> list[str] | None:
    """Return Google `recurrence` content lines, or None for a one-off event."""
    lines = [line for line in (event.get("recurrences") or []) if isinstance(line, str) and line]
    return lines or None


def content_hash(body: dict) -> str:
    """Stable digest of everything that decides whether an update is needed.

    Deliberately excludes ttHash itself and anything that changes per run — if a
    volatile field leaked in, every event would be rewritten on every sync.
    """
    payload = {k: v for k, v in body.items() if k != "extendedProperties"}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def to_google_event(
    event: dict,
    *,
    calendar_id: str,
    labels: dict[int, dict],
    color_mapper: ColorMapper,
    options: MapperOptions,
) -> dict:
    """Build the Google event body for one TimeTree event."""
    label = labels.get(event.get("label_id")) or {}
    label_name = label.get("name") or None
    start, end = time_fields(event)

    body: dict = {
        "summary": event.get("title") or "(無標題)",
        "start": start,
        "end": end,
        "reminders": reminders(event),
    }

    if desc := description(event, label_name, options):
        body["description"] = desc
    if event.get("location"):
        body["location"] = event["location"]
    if rrule := recurrence(event):
        body["recurrence"] = rrule
    if color_id := color_mapper.color_id_for(label.get("color")):
        body["colorId"] = color_id

    private = {
        SOURCE_KEY: SOURCE_VALUE,
        UID_KEY: event["uuid"],
        CAL_KEY: str(calendar_id),
        HASH_KEY: content_hash(body),
    }
    if label_name:
        private[LABEL_KEY] = label_name[:1024]
    body["extendedProperties"] = {"private": private}

    return body


def map_events(
    events: list[dict],
    *,
    calendar_id: str,
    labels: dict[int, dict],
    color_mapper: ColorMapper,
    options: MapperOptions,
) -> tuple[dict[str, dict], dict[str, int]]:
    """Map a calendar's events to {ttUid: Google body}, plus a count of what was skipped.

    Per-occurrence exceptions of a recurring event are refused rather than guessed
    at: TimeTree's representation of them is unverified (see docs/timetree-api-notes.md,
    question B), and inserting both a master and its modified occurrence would
    double-book that date. Refusing is visible in the logs; double-booking is not.
    """
    desired: dict[str, dict] = {}
    skipped: dict[str, int] = {}

    def note_skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    for event in events:
        if reason := should_skip(event, options):
            note_skip(reason)
            continue

        # Skip on the child's own fields alone, without checking whether the master
        # is in this payload. `recurring_uuid` may be a series identifier rather
        # than the master's uuid, in which case a "is the master here?" test would
        # never fire and the child would sail through as a standalone event while
        # the master's RRULE still covers that date — the exact double-booking this
        # guard exists to prevent. Costs nothing today: no such events exist.
        parent = event.get("recurring_uuid") or event.get("parent_id")
        if parent:
            logger.warning(
                "Event %s belongs to recurring series %s. TimeTree's representation of "
                "per-occurrence exceptions has never been observed, so it is skipped rather "
                "than risk double-booking. Re-run `tt2gcal recon` and settle question B in "
                "docs/timetree-api-notes.md.",
                event["uuid"], parent,
            )
            note_skip("recurrence exception (unsupported)")
            continue

        desired[event["uuid"]] = to_google_event(
            event,
            calendar_id=calendar_id,
            labels=labels,
            color_mapper=color_mapper,
            options=options,
        )

    return desired, skipped
