"""Offline mode: run the PRO engine with an Offline Key file, with no licence calls at all.

A Platform Licence holder downloads an Offline Key (a JSON file from the Licenses page, or
GET /api/v1/offline/key). Its ``token`` is a PRO run token that the engine verifies locally (signature,
validity window and a rollback guard). With ``offline_key=`` (or CLEARCOTE_OFFLINE_KEY) set, a launch:

- makes no lease checkout, heartbeat or check-in, and no download: the binary must be given
  (``executable_path=`` or CLEARCOTE_BINARY), normally the PRO binary from the offline bundle;
- passes the key's token to the engine as CLEARCOTE_RUN_TOKEN.

The key is checked here before launching, so an expired or malformed file fails with a clear message
instead of an engine refusal. The engine stays the authority.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

OFFLINE_KEY_ENV = "CLEARCOTE_OFFLINE_KEY"
OFFLINE_KEY_FORMAT = "clearcote-offline-key"
WARN_DAYS = 7


class OfflineKeyError(RuntimeError):
    """The Offline Key file is missing, malformed or expired, or offline mode lacks a binary."""


def offline_key_path(explicit: str | os.PathLike | None = None) -> str | None:
    """explicit ``offline_key=`` > CLEARCOTE_OFFLINE_KEY. None means online mode."""
    if explicit:
        return os.fspath(explicit)
    env = os.environ.get(OFFLINE_KEY_ENV, "").strip()
    return env or None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_offline_key(path: str, now: datetime | None = None) -> dict:
    """Read and check an Offline Key file. Returns the parsed file; raises OfflineKeyError."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise OfflineKeyError(f"Offline key file not found: {path}") from None
    except (OSError, ValueError) as exc:
        raise OfflineKeyError(f"Offline key file can't be read as JSON ({path}): {exc}") from None
    if not isinstance(data, dict) or data.get("format") != OFFLINE_KEY_FORMAT:
        raise OfflineKeyError(f"{path} is not a Clearcote offline key file.")
    token = data.get("token")
    if not isinstance(token, str) or token.count(".") != 1:
        raise OfflineKeyError(f"{path} has no valid token.")
    try:
        expires = _parse_time(str(data.get("expires_at")))
    except ValueError:
        raise OfflineKeyError(f"{path} has no valid expires_at.") from None
    now = now or datetime.now(timezone.utc)
    if expires <= now:
        raise OfflineKeyError(
            f"This offline key expired on {expires.date().isoformat()}. Download the newest key from the "
            "Offline card on your Licenses page (or GET /api/v1/offline/key) and install it."
        )
    data["_expires"] = expires
    return data


class OfflineLease:
    """Stands in for an online lease handle: same surface, no network."""

    refresh_token = None  # nothing to refresh offline: a newer key is installed by hand

    def __init__(self, token: str, expires: datetime):
        self.token = token
        self.expires = expires

    def bind_launch(self):
        return (None, lambda: None)

    def stop(self) -> None:
        pass


def offline_lease(explicit=None, quiet: bool = False, now: datetime | None = None) -> OfflineLease | None:
    """The offline lease for this launch, or None for online mode."""
    path = offline_key_path(explicit)
    if not path:
        return None
    data = load_offline_key(path, now=now)
    expires = data["_expires"]
    left = (expires - (now or datetime.now(timezone.utc))).days
    if left < WARN_DAYS and not quiet:
        sys.stderr.write(
            f"[clearcote] [offline] the offline key expires in {left} day(s) ({expires.date().isoformat()}); "
            "install the newer key from your renewal before then.\n"
        )
    return OfflineLease(data["token"], expires)


def require_offline_binary(kwargs: dict) -> None:
    """Offline mode never downloads: the binary must come from the offline bundle."""
    if not (kwargs.get("executable_path") or os.environ.get("CLEARCOTE_BINARY")):
        raise OfflineKeyError(
            "Offline mode runs the PRO binary from your offline bundle: pass executable_path=... "
            "or set CLEARCOTE_BINARY."
        )
