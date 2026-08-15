"""Persisted state: TimeTree session, Google token, calendar map, colour map.

Everything here lives outside git (see .gitignore) and is written with 0600
permissions, because it holds credentials.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SESSION_FILE = "session.json"
GOOGLE_TOKEN_FILE = "google-token.json"
CALENDARS_FILE = "calendars.json"
COLOR_MAP_FILE = "color-map.json"


class Store:
    """Small JSON-file store rooted at the configured state directory."""

    def __init__(self, state_dir: Path):
        self.dir = state_dir

    def _path(self, name: str) -> Path:
        return self.dir / name

    def read(self, name: str) -> dict[str, Any] | None:
        """Return parsed JSON for a state file, or None if absent or corrupt."""
        path = self._path(name)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def write(self, name: str, data: dict[str, Any]) -> Path:
        """Write JSON to a state file with 0600 permissions.

        The directory mode is set only when we create it, so an operator who has
        deliberately widened access on a server keeps their choice.
        """
        if not self.dir.exists():
            self.dir.mkdir(parents=True, mode=0o700)
        path = self._path(name)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    # --- TimeTree session ------------------------------------------------

    def get_session_id(self) -> str | None:
        """Return the persisted TimeTree _session_id cookie, if any."""
        data = self.read(SESSION_FILE)
        return data.get("session_id") if data else None

    def set_session_id(self, session_id: str) -> None:
        """Persist the TimeTree _session_id cookie."""
        self.write(SESSION_FILE, {"session_id": session_id})

    def clear_session(self) -> None:
        """Drop the persisted TimeTree session so the next run logs in again."""
        path = self._path(SESSION_FILE)
        path.unlink(missing_ok=True)

    # --- TimeTree calendar id -> Google calendar id ----------------------

    def get_calendar_map(self) -> dict[str, str]:
        """Return the persisted TimeTree-calendar-id to Google-calendar-id map."""
        return self.read(CALENDARS_FILE) or {}

    def set_calendar_map(self, mapping: dict[str, str]) -> None:
        """Persist the TimeTree-calendar-id to Google-calendar-id map."""
        self.write(CALENDARS_FILE, mapping)

    # --- TimeTree label colour -> Google colorId -------------------------

    def get_color_map(self) -> dict[str, str]:
        """Return the cached hex-colour to Google colorId map."""
        return self.read(COLOR_MAP_FILE) or {}

    def set_color_map(self, mapping: dict[str, str]) -> None:
        """Persist the hex-colour to Google colorId map."""
        self.write(COLOR_MAP_FILE, mapping)
