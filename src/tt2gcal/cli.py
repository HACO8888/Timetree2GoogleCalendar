"""Command-line entry point for tt2gcal."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from tt2gcal import __version__
from tt2gcal.config import Config, ConfigError, load_dotenv
from tt2gcal.state import Store
from tt2gcal.timetree import TimeTreeClient

logger = logging.getLogger("tt2gcal")

DEFAULT_RAW_DIR = "raw-timetree"


def build_parser() -> argparse.ArgumentParser:
    """Build the tt2gcal argument parser."""
    parser = argparse.ArgumentParser(prog="tt2gcal", description=__doc__)
    parser.add_argument("--version", action="version", version=f"tt2gcal {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    recon = sub.add_parser(
        "recon",
        help="Dump raw TimeTree API payloads and report the facts the mapper depends on",
    )
    recon.add_argument(
        "--output-dir",
        default=DEFAULT_RAW_DIR,
        help=f"Where to write raw payloads (default: {DEFAULT_RAW_DIR}; gitignored)",
    )

    sub.add_parser("auth-google", help="Run the Google OAuth flow once and store the token")

    sub.add_parser("doctor", help="Check credentials, scopes and connectivity without writing")

    sync = sub.add_parser("sync", help="Mirror every TimeTree calendar into Google Calendar")
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the computed changes without writing anything",
    )
    sync.add_argument(
        "--allow-unverified-create",
        action="store_true",
        help=(
            "Create a Google calendar even when existing calendars cannot be listed. "
            "Only use this when you are certain no such calendar exists yet."
        ),
    )

    return parser


def configure_logging(verbose: bool) -> None:
    """Set up logging for CLI use."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _timetree(config: Config) -> TimeTreeClient:
    return TimeTreeClient(config.timetree_email, config.timetree_password, Store(config.state_dir))


def _google(config: Config, *, allow_unverified_create: bool = False):
    from tt2gcal.google import GoogleCalendarClient, load_credentials

    store = Store(config.state_dir)
    credentials = load_credentials(config.google_client_id, config.google_client_secret, store)
    return GoogleCalendarClient.from_credentials(
        credentials, store, config.calendar_prefix,
        calendar_timezone=config.calendar_timezone,
        allow_unverified_create=allow_unverified_create,
    )


def cmd_recon(args: argparse.Namespace) -> int:
    """Run reconnaissance against the TimeTree private API."""
    from tt2gcal.recon import run_recon

    config = Config.from_env(require_google=False)
    report = run_recon(_timetree(config), Path(args.output_dir))

    print(json.dumps(report, indent=2, ensure_ascii=False))
    print("\n--- verdicts ---", file=sys.stderr)
    for cal in report["calendars"]:
        print(
            f"[{cal['name']}] {cal['event_count']} events "
            f"({cal['syncable_count']} syncable after dropping birthdays/memos)",
            file=sys.stderr,
        )
        print(f"  A tombstones : {cal['tombstones']['verdict']}", file=sys.stderr)
        print(f"  B recurrence : {cal['recurrence']['verdict']}", file=sys.stderr)
        print(
            f"  C time       : {cal['time_fields']['all_day_count']} all-day, "
            f"{cal['time_fields']['timed_count']} timed, timezones="
            f"{list(cal['time_fields']['start_timezones'])}",
            file=sys.stderr,
        )
    return 0


def cmd_auth_google(args: argparse.Namespace) -> int:
    """Run the interactive Google OAuth flow."""
    from tt2gcal.google import authorize

    config = Config.from_env(require_timetree=False)
    store = Store(config.state_dir)
    authorize(config.google_client_id, config.google_client_secret, store)
    print(f"Google token stored in {store.dir / 'google-token.json'}")
    print(
        "Reminder: the OAuth consent screen must be 'In production' at "
        "console.cloud.google.com/auth/audience — in 'Testing' this refresh token "
        "expires in 7 days."
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check both sides without writing anything."""
    config = Config.from_env()
    ok = True

    calendars = _timetree(config).calendars()
    active = [c for c in calendars if c.is_active]
    print(f"TimeTree      : signed in, {len(calendars)} calendars ({len(active)} active)")
    for calendar in active:
        print(f"  - {calendar.name} ({len(calendar.labels)} labels)")

    google = _google(config)
    palette = google.event_palette()
    print(f"Google colours: {len(palette)} event colours available")

    try:
        google.service.calendarList().list().execute()
        print("Google list   : calendarList.list permitted (mapping can self-recover)")
    except Exception as exc:  # noqa: BLE001 — doctor reports, it does not raise
        ok = False
        print(f"Google list   : calendarList.list NOT permitted ({exc})")
        print("                Losing state/calendars.json would need manual repair.")

    mapping = Store(config.state_dir).get_calendar_map()
    print(f"Calendar map  : {len(mapping)} TimeTree calendars mapped")

    return 0 if ok else 1


def cmd_sync(args: argparse.Namespace) -> int:
    """Mirror TimeTree into Google Calendar."""
    from tt2gcal.runner import run_sync

    config = Config.from_env()
    google = _google(config, allow_unverified_create=args.allow_unverified_create)
    result = run_sync(config, _timetree(config), google, dry_run=args.dry_run)

    prefix = "[dry-run] " if args.dry_run else ""
    applied = result.applied
    print(
        f"{prefix}{result.calendars_seen} calendars: "
        f"{applied.get('insert', 0)} insert, {applied.get('update', 0)} update, "
        f"{applied.get('delete', 0)} delete"
    )
    if result.skipped:
        details = ", ".join(f"{count} {reason}" for reason, count in sorted(result.skipped.items()))
        print(f"{prefix}skipped: {details}")
    if not result.changed:
        print(f"{prefix}no changes")
    return 0


COMMANDS = {
    "recon": cmd_recon,
    "auth-google": cmd_auth_google,
    "doctor": cmd_doctor,
    "sync": cmd_sync,
}


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns a process exit code."""
    load_dotenv()
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    try:
        return COMMANDS[args.command](args)
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2
    except Exception as exc:  # noqa: BLE001
        # A non-zero exit is what makes systemd's OnFailure fire. The private API
        # changing under us must never look like a quiet successful run.
        logger.error("%s: %s", type(exc).__name__, exc, exc_info=args.verbose)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
