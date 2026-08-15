"""One sync run: fetch TimeTree, map, diff against Google, apply."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from tt2gcal.colors import ColorMapper
from tt2gcal.config import Config
from tt2gcal.google import GoogleCalendarClient
from tt2gcal.mapper import MapperOptions, map_events
from tt2gcal.sync import Plan, apply_plan, build_plan, describe
from tt2gcal.timetree import TimeTreeClient

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    """What one sync run did, across every calendar."""

    plans: list[Plan] = field(default_factory=list)
    applied: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    calendars_seen: int = 0

    @property
    def changed(self) -> bool:
        """True when at least one calendar needed a change."""
        return any(not plan.is_empty for plan in self.plans)


def run_sync(
    config: Config,
    timetree: TimeTreeClient,
    google: GoogleCalendarClient,
    *,
    dry_run: bool = False,
) -> RunResult:
    """Mirror every active TimeTree calendar into its Google counterpart."""
    options = MapperOptions(
        include_birthdays=config.include_birthdays,
        include_memos=config.include_memos,
        label_in_description=config.label_in_description,
    )
    color_mapper = ColorMapper(
        palette=google.event_palette(), cache=google.store.get_color_map()
    )
    result = RunResult()

    for calendar in timetree.calendars():
        if not calendar.is_active:
            logger.info("Skipping deactivated TimeTree calendar '%s'", calendar.name)
            continue
        result.calendars_seen += 1

        events = timetree.events(calendar)
        desired, skipped = map_events(
            events,
            calendar_id=calendar.id,
            labels=calendar.labels,
            color_mapper=color_mapper,
            options=options,
        )
        for reason, count in skipped.items():
            result.skipped[reason] = result.skipped.get(reason, 0) + count

        google_calendar_id = google.resolve_calendar(
            calendar.id, calendar.name, create=not dry_run
        )
        if google_calendar_id is None:
            # Dry run with no calendar yet: report what would happen without
            # creating anything. Everything is an insert against an empty calendar.
            logger.info(
                "Would create Google calendar '%s%s' and insert %d events",
                config.calendar_prefix, calendar.name, len(desired),
            )
            result.applied["insert"] = result.applied.get("insert", 0) + len(desired)
            result.plans.append(
                build_plan(calendar.name, "(would be created)", desired, {})
            )
            continue

        existing = google.index_synced_events(google_calendar_id)

        plan = build_plan(calendar.name, google_calendar_id, desired, existing)
        logger.info("%s (%d TimeTree events, %d mirrored)", describe(plan), len(events),
                    len(desired))
        result.plans.append(plan)

        counts = apply_plan(google, plan, dry_run=dry_run)
        for action, count in counts.items():
            result.applied[action] = result.applied.get(action, 0) + count

    # Colour lookups are pure, so caching them just avoids recomputation; never
    # write the cache during a dry run, which must leave no trace.
    if not dry_run:
        google.store.set_color_map(color_mapper.cache)

    return result
