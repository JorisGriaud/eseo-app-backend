"""
Coordinates when a schedule sync (upstream ESEO fetch + diff + notify + group
fan-out) should actually run, to avoid redundant work when:
- multiple users in the same `groupe` open the app around the same time
  (app-driven trigger, short debounce window)
- the periodic scheduled job would otherwise re-check something that was
  already freshly verified moments ago (longer debounce window)

State is in-memory (module-level), matching the existing pattern used by
security.RateLimiter and scraper._mfa_sessions - correct for this app's
single-worker deployment (required anyway for APScheduler, see Dockerfile).
A restart just means one extra redundant check next time, never a
correctness issue.
"""
from datetime import datetime, timezone
from typing import Iterable

_last_checked: dict[str, datetime] = {}


def user_key(eseo_id: int) -> str:
    """Debounce key for a user's own (non-groupe) portion of their schedule."""
    return f"user:{eseo_id}"


def notes_user_key(eseo_id: int) -> str:
    """
    Debounce key for a user's notes checks - a separate namespace from
    user_key() so a schedule check and a notes check don't reset each
    other's timers (they run on very different cadences).
    """
    return f"notes:{eseo_id}"


def _is_stale(key: str, within_seconds: float) -> bool:
    last = _last_checked.get(key)
    if last is None:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() >= within_seconds


def should_sync(user_key_: str, groupes: Iterable[str], within_seconds: float) -> bool:
    """
    True if a sync is due: the user's own key or at least one of their known
    `groupe` keys hasn't been checked within the window.

    A user with no known groupe (new user, or 100% personal courses) only has
    their own key, so this naturally degrades to a plain per-user debounce -
    no special-casing needed.
    """
    keys = [user_key_] + [g for g in groupes if g]
    return any(_is_stale(k, within_seconds) for k in keys)


def mark_checked(user_key_: str, groupes: Iterable[str]) -> None:
    """Marks the user's own key AND every given groupe key as checked now."""
    now = datetime.now(timezone.utc)
    _last_checked[user_key_] = now
    for g in groupes:
        if g:
            _last_checked[g] = now
