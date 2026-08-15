"""The diff engine: turn (desired TimeTree state, current Google state) into actions.

Deliberately free of I/O so it can be tested against fixtures, and deliberately
ignorant of how the desired bodies were built.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from tt2gcal.google import HASH_KEY, UID_KEY

logger = logging.getLogger(__name__)


class Action(StrEnum):
    """What to do with one event."""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True)
class Change:
    """A single planned change against one Google calendar."""

    action: Action
    uid: str
    summary: str
    body: dict | None = None
    event_id: str | None = None


@dataclass
class Plan:
    """All changes for one calendar, plus the counts worth logging."""

    calendar_name: str
    google_calendar_id: str
    changes: list[Change] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """Return {action: count} across this plan."""
        result = {action.value: 0 for action in Action}
        for change in self.changes:
            result[change.action.value] += 1
        return result

    @property
    def is_empty(self) -> bool:
        """True when nothing needs to change."""
        return not self.changes


def _existing_hash(event: dict) -> str | None:
    return (event.get("extendedProperties", {}).get("private", {}) or {}).get(HASH_KEY)


def _desired_hash(body: dict) -> str | None:
    return (body.get("extendedProperties", {}).get("private", {}) or {}).get(HASH_KEY)


def build_plan(
    calendar_name: str,
    google_calendar_id: str,
    desired: dict[str, dict],
    existing: dict[str, dict],
) -> Plan:
    """Compare desired TimeTree-derived bodies against the current Google events.

    `desired` is {ttUid: event body}, `existing` is {ttUid: Google event resource}.
    Deletion is decided purely by absence from `desired`, which is why the TimeTree
    fetch must always be a full one.
    """
    plan = Plan(calendar_name=calendar_name, google_calendar_id=google_calendar_id)

    for uid, body in desired.items():
        current = existing.get(uid)
        summary = body.get("summary") or "(untitled)"
        if current is None:
            plan.changes.append(Change(Action.INSERT, uid, summary, body=body))
        elif _existing_hash(current) != _desired_hash(body):
            plan.changes.append(
                Change(Action.UPDATE, uid, summary, body=body, event_id=current["id"])
            )

    for uid, current in existing.items():
        if uid not in desired:
            plan.changes.append(
                Change(
                    Action.DELETE,
                    uid,
                    current.get("summary") or "(untitled)",
                    event_id=current["id"],
                )
            )

    return plan


def apply_plan(client, plan: Plan, *, dry_run: bool = False) -> dict[str, int]:
    """Execute a plan against Google Calendar. Returns the applied counts."""
    applied = {action.value: 0 for action in Action}

    for change in plan.changes:
        label = f"{change.action.value} {change.uid} {change.summary!r}"
        if dry_run:
            logger.info("[dry-run] %s", label)
            applied[change.action.value] += 1
            continue

        logger.info("%s", label)
        if change.action is Action.INSERT:
            client.insert_event(plan.google_calendar_id, change.body)
        elif change.action is Action.UPDATE:
            client.update_event(plan.google_calendar_id, change.event_id, change.body)
        else:
            client.delete_event(plan.google_calendar_id, change.event_id)
        applied[change.action.value] += 1

    return applied


def describe(plan: Plan) -> str:
    """One-line human summary of a plan."""
    counts = plan.counts()
    return (
        f"{plan.calendar_name}: "
        f"{counts['insert']} insert, {counts['update']} update, {counts['delete']} delete"
    )


__all__ = ["Action", "Change", "Plan", "apply_plan", "build_plan", "describe", "UID_KEY"]
