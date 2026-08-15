"""Reconnaissance over the TimeTree private API.

Answers the three questions that decide how the mapper must be built, from real
account data rather than guesswork:

  A. How does a deleted event appear in a full sync?
     -> if there is a tombstone field, the mapper must filter it out or deleted
        events get resurrected on every run.
  B. How is a modified/deleted single occurrence of a recurring event represented?
     -> if TimeTree returns both the master (with `recurrences`) and a child (with
        `parent_id`/`recurring_uuid`), inserting both double-books that date.
  C. What do the all-day and timezone fields actually contain?

Raw payloads are written to a directory (default `raw-timetree/`) which is
gitignored: they contain personal data.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from timetree_exporter.config import configure_developer_mode

from tt2gcal.timetree import TimeTreeClient

logger = logging.getLogger(__name__)

# Fields that would indicate a soft-deleted / tombstoned event.
TOMBSTONE_HINTS = ("deactivated_at", "deleted_at", "removed_at", "discarded_at", "status", "state")


def run_recon(client: TimeTreeClient, output_dir: Path) -> dict[str, Any]:
    """Fetch every calendar's events, dump raw JSON, and return a findings report."""
    _clear_previous_dump(output_dir)
    configure_developer_mode(enabled=True, raw_output_dir=str(output_dir))
    output_dir.mkdir(parents=True, exist_ok=True)

    calendars = client.calendars()
    report: dict[str, Any] = {"calendars": []}

    for cal in calendars:
        events = client.events(cal)
        report["calendars"].append(analyse_calendar(cal.name, cal.id, events, cal.labels))

    summary_path = output_dir / "recon-report.json"
    summary_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote recon report to %s", summary_path)
    return report


BIRTHDAY_TYPE = 1
MEMO_CATEGORY = 2


def is_syncable(event: dict) -> bool:
    """True for events we would actually mirror (not birthdays, not memos).

    Birthdays are auto-generated yearly-recurring entries with empty titles, so
    leaving them in skews every recurrence statistic in this report.
    """
    return event.get("type") != BIRTHDAY_TYPE and event.get("category") != MEMO_CATEGORY


def _clear_previous_dump(output_dir: Path) -> None:
    """Remove JSON left by earlier runs.

    The dump numbers files from 01 each run, so without this a shorter run leaves
    higher-numbered files behind and any analysis over the directory silently
    double-counts events across runs.
    """
    if not output_dir.is_dir():
        return
    for path in sorted(output_dir.rglob("*.json"), reverse=True):
        path.unlink()
    for path in sorted(output_dir.rglob("*"), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def analyse_calendar(name: str, cal_id: str, events: list[dict], labels: dict) -> dict[str, Any]:
    """Summarise one calendar's raw events into the facts the mapper design needs."""
    syncable = [e for e in events if is_syncable(e)]
    return {
        "name": name,
        "id": cal_id,
        "event_count": len(events),
        "syncable_count": len(syncable),
        "label_count": len(labels),
        "field_coverage": field_coverage(events),
        "schema_keys": sorted({k for e in events for k in e}),
        "tombstones": tombstone_findings(events),
        "recurrence": recurrence_findings(syncable),
        "recurrence_including_birthdays": recurrence_findings(events),
        "time_fields": time_field_findings(events),
        "type_category": {
            "type": dict(Counter(e.get("type") for e in events)),
            "category": dict(Counter(e.get("category") for e in events)),
        },
        # Recorded so a later run can be diffed to see how a deletion actually shows up.
        "event_uuids": sorted(e["uuid"] for e in events if e.get("uuid")),
    }


def field_coverage(events: list[dict]) -> dict[str, int]:
    """Return how many events carry each key — reveals fields we never anticipated."""
    counter: Counter[str] = Counter()
    for event in events:
        counter.update(k for k, v in event.items() if v not in (None, "", [], {}))
    return dict(sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])))


def tombstone_findings(events: list[dict]) -> dict[str, Any]:
    """Question A: look for any field that could mark an event as deleted.

    A field being present in the schema but null everywhere is the interesting
    case, and the easy one to miss: it means the API *can* hand us tombstones even
    though this snapshot has none, so the mapper must filter on it regardless.
    """
    in_schema: dict[str, int] = {}
    populated: dict[str, Any] = {}

    for hint in TOMBSTONE_HINTS:
        schema_count = sum(1 for e in events if hint in e)
        if schema_count:
            in_schema[hint] = schema_count
        values = [e[hint] for e in events if e.get(hint) not in (None, "")]
        if values:
            populated[hint] = {
                "count": len(values),
                "sample_values": list({json.dumps(v, ensure_ascii=False) for v in values})[:5],
            }

    if populated:
        verdict = (
            f"tombstoned events are present right now via {sorted(populated)} — "
            "the mapper MUST filter them or deleted events get resurrected"
        )
    elif in_schema:
        verdict = (
            f"{sorted(in_schema)} exist in the schema but are null in this snapshot — "
            "filter on them anyway; absence of tombstones today is not a guarantee"
        )
    else:
        verdict = "no tombstone field at all; deleted events appear to be omitted entirely"

    return {
        "fields_in_schema": in_schema,
        "fields_populated": populated,
        "verdict": verdict,
    }


def recurrence_findings(events: list[dict]) -> dict[str, Any]:
    """Question B: do masters and per-occurrence children coexist in one response?"""
    masters = {e["uuid"] for e in events if e.get("recurrences")}
    children = [e for e in events if e.get("parent_id") or e.get("recurring_uuid")]
    orphan_children = [
        e for e in children
        if (e.get("recurring_uuid") or e.get("parent_id")) not in masters
    ]
    both = [
        e for e in children
        if (e.get("recurring_uuid") or e.get("parent_id")) in masters
    ]

    if masters and both:
        verdict = (
            "masters AND per-occurrence children coexist — do NOT insert both; "
            "map masters with RRULE and patch instances via recurringEventId"
        )
    elif masters and not children:
        verdict = (
            "only masters with RRULE, no exception children — map RRULE straight through. "
            "INCONCLUSIVE about exceptions until one exists in the account"
        )
    elif children and not masters:
        verdict = "only expanded children; treat every event as standalone (no RRULE)"
    else:
        verdict = "no recurring events in this sample — INCONCLUSIVE, re-run after creating one"

    return {
        "master_count": len(masters),
        "child_count": len(children),
        "children_whose_master_is_also_present": len(both),
        "orphan_children": len(orphan_children),
        "sample_master": _sample(events, lambda e: bool(e.get("recurrences"))),
        "sample_child": _sample(
            events, lambda e: bool(e.get("parent_id") or e.get("recurring_uuid"))
        ),
        "verdict": verdict,
    }


def time_field_findings(events: list[dict]) -> dict[str, Any]:
    """Question C: what all_day and timezone values actually show up."""
    all_day = [e for e in events if e.get("all_day")]
    timed = [e for e in events if not e.get("all_day")]
    return {
        "all_day_count": len(all_day),
        "timed_count": len(timed),
        "start_timezones": dict(Counter(e.get("start_timezone") for e in events)),
        "timezone_mismatch_count": sum(
            1 for e in events if e.get("start_timezone") != e.get("end_timezone")
        ),
        "sample_all_day": _sample(events, lambda e: bool(e.get("all_day"))),
        "sample_timed": _sample(events, lambda e: not e.get("all_day")),
    }


def _sample(events: list[dict], predicate) -> dict | None:
    """Return the first event matching predicate, with free-text fields redacted."""
    for event in events:
        if predicate(event):
            return redact(event)
    return None


def redact(event: dict) -> dict:
    """Replace personal free text with a length marker so samples are safe to share."""
    redacted = {}
    for key, value in event.items():
        if key in {"title", "note", "location", "url"} and value:
            redacted[key] = f"<{key} len={len(str(value))}>"
        else:
            redacted[key] = value
    return redacted
