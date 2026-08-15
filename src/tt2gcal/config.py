"""Runtime configuration, read from environment variables (optionally via .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_STATE_DIR = "./state"
DEFAULT_CALENDAR_PREFIX = "TimeTree · "


def local_timezone() -> str:
    """Return the machine's IANA zone name, or UTC if it cannot be determined.

    Used only to stamp a sensible zone on newly created calendars. A server
    usually runs in UTC while the calendars it mirrors do not, so
    TT2GCAL_CALENDAR_TIMEZONE overrides this.
    """
    try:
        link = Path("/etc/localtime").resolve()
        if "zoneinfo" in link.parts:
            return "/".join(link.parts[link.parts.index("zoneinfo") + 1:])
    except OSError:
        pass
    return "UTC"


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ without overwriting."""
    env_path = path or Path.cwd() / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Config:
    """Everything the sync needs, resolved from the environment."""

    timetree_email: str
    timetree_password: str
    google_client_id: str
    google_client_secret: str
    state_dir: Path
    calendar_prefix: str
    calendar_timezone: str
    include_birthdays: bool
    include_memos: bool
    label_in_description: bool

    @classmethod
    def from_env(cls, *, require_timetree: bool = True, require_google: bool = True) -> Config:
        """Build a Config from environment variables, validating what the caller needs."""
        missing = []
        email = os.environ.get("TIMETREE_EMAIL", "")
        password = os.environ.get("TIMETREE_PASSWORD", "")
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")

        if require_timetree:
            missing += [n for n, v in (("TIMETREE_EMAIL", email), ("TIMETREE_PASSWORD", password))
                        if not v]
        if require_google:
            missing += [n for n, v in (("GOOGLE_CLIENT_ID", client_id),
                                       ("GOOGLE_CLIENT_SECRET", client_secret)) if not v]
        if missing:
            raise ConfigError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ". Copy .env.example to .env and fill it in."
            )

        return cls(
            timetree_email=email,
            timetree_password=password,
            google_client_id=client_id,
            google_client_secret=client_secret,
            state_dir=Path(os.environ.get("TT2GCAL_STATE_DIR", DEFAULT_STATE_DIR)).expanduser(),
            calendar_prefix=os.environ.get("TT2GCAL_CALENDAR_PREFIX", DEFAULT_CALENDAR_PREFIX),
            calendar_timezone=os.environ.get("TT2GCAL_CALENDAR_TIMEZONE") or local_timezone(),
            include_birthdays=_flag("TT2GCAL_INCLUDE_BIRTHDAYS"),
            include_memos=_flag("TT2GCAL_INCLUDE_MEMOS"),
            label_in_description=_flag("TT2GCAL_LABEL_IN_DESCRIPTION"),
        )
