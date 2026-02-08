"""
Utility functions for EDT application
Date calculations and helper functions
"""
from datetime import datetime, timedelta, timezone
from typing import Optional
import pytz
import hashlib
import json
from sqlalchemy import and_
from sqlalchemy.orm import Session


def get_date_range(weeks: int = 4) -> tuple[str, str]:
    """
    Calculate date range from Monday of current week to Sunday of week N

    Args:
        weeks: Number of weeks to include (default 4)

    Returns:
        Tuple of (start_date, end_date) in YYYYMMDDTHHmmss format

    Example:
        If today is Wednesday Feb 5, 2026 and weeks=2:
        - Start: Monday Feb 3, 2026 06:00 → "20260203T060000"
        - End: Sunday Feb 16, 2026 21:00 → "20260216T210000"
    """
    now = datetime.now()

    # Find Monday of current week (weekday 0 = Monday)
    days_since_monday = now.weekday()
    monday_of_week = now - timedelta(days=days_since_monday)

    # Set start time to Monday at 06:00
    start_date = monday_of_week.replace(hour=6, minute=0, second=0, microsecond=0)

    # Calculate Sunday of target week
    days_to_add = (weeks * 7) - 1  # -1 because we want Sunday
    end_date_day = monday_of_week + timedelta(days=days_to_add)

    # Set end time to Sunday at 21:00
    end_date = end_date_day.replace(hour=21, minute=0, second=0, microsecond=0)

    # Format as YYYYMMDDTHHmmss
    start_str = start_date.strftime("%Y%m%dT%H%M%S")
    end_str = end_date.strftime("%Y%m%dT%H%M%S")

    return start_str, end_str


def filter_event_fields(event: dict) -> dict:
    """
    Extract only required fields from API event data

    Args:
        event: Full event dictionary from API

    Returns:
        Filtered event with only: Code, Debut, Fin, Emplacement, Libelle, Professeur, Type, Groupe

    Note:
        Groupe is extracted from LesGroupes[0].LienAgenda.Groupe if available
        Example: "E2-Angers Gr1"
    """
    # Extract groupe from nested structure
    groupe = None
    if event.get("LesGroupes") and isinstance(event["LesGroupes"], list) and len(event["LesGroupes"]) > 0:
        lien_agenda = event["LesGroupes"][0].get("LienAgenda")
        if lien_agenda:
            groupe = lien_agenda.get("Groupe")

    return {
        "Code": event.get("Code"),
        "Debut": event.get("Debut"),
        "Fin": event.get("Fin"),
        "Emplacement": event.get("Emplacement"),
        "Libelle": event.get("Libelle"),
        "Professeur": event.get("Professeur"),
        "Type": event.get("Type"),
        "Groupe": groupe
    }


# Timezone management
PARIS_TZ = pytz.timezone('Europe/Paris')


def parse_eseo_datetime(eseo_datetime_str: str) -> Optional[datetime]:
    """
    Parse ESEO API datetime string to timezone-aware datetime in UTC

    Args:
        eseo_datetime_str: Format "2026-02-03T15:40:00" (ISO8601, Paris local time)
                          or "20260203T154000" (legacy format, Paris local time)

    Returns:
        datetime object in UTC timezone

    Example:
        "2026-02-03T15:40:00" (Paris) -> 2026-02-03 14:40:00+00:00 (UTC)
    """
    if not eseo_datetime_str:
        return None

    try:
        # Try ISO8601 format first (current API format)
        try:
            naive_dt = datetime.strptime(eseo_datetime_str, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            # Fallback to legacy format
            naive_dt = datetime.strptime(eseo_datetime_str, "%Y%m%dT%H%M%S")

        # Both formats are in Paris local time - convert to UTC
        paris_dt = PARIS_TZ.localize(naive_dt)
        return paris_dt.astimezone(pytz.utc)

    except (ValueError, AttributeError) as e:
        print(f"Error parsing datetime {eseo_datetime_str}: {e}")
        return None


def format_datetime_for_response(utc_datetime: datetime) -> Optional[str]:
    """
    Format UTC datetime to ISO8601 in Paris timezone for API responses

    Args:
        utc_datetime: Timezone-aware datetime in UTC (or naive datetime assumed to be UTC)

    Returns:
        ISO8601 string: "2026-02-03T08:00:00+01:00" (Paris time)

    Example:
        2026-02-03 07:00:00+00:00 (UTC) -> "2026-02-03T08:00:00+01:00" (Paris)

    Note:
        SQLite stores datetime as naive (loses timezone info). If naive, we assume it's UTC.
    """
    if not utc_datetime:
        return None

    # If naive datetime (from SQLite), treat as UTC
    if utc_datetime.tzinfo is None:
        utc_datetime = utc_datetime.replace(tzinfo=timezone.utc)

    # Convert to Paris timezone for display
    paris_dt = utc_datetime.astimezone(PARIS_TZ)
    return paris_dt.isoformat()


def calculate_schedule_hash_for_range(
    db: Session,
    eseo_id: int,
    start_date: datetime,
    end_date: datetime
) -> str:
    """
    Calculate MD5 hash of all events in date range
    Used for change detection in scheduler

    Args:
        db: SQLAlchemy session
        eseo_id: User's ESEO ID
        start_date: Start datetime (UTC timezone-aware)
        end_date: End datetime (UTC timezone-aware)

    Returns:
        MD5 hash string (32 chars)

    Note:
        Events are serialized consistently (sorted, deterministic JSON) to ensure
        identical schedules produce identical hashes
    """
    # Import here to avoid circular imports
    from database import Event

    events = db.query(Event).filter(
        and_(
            Event.eseo_id == eseo_id,
            Event.debut >= start_date,
            Event.debut <= end_date
        )
    ).order_by(Event.debut).all()

    # Serialize events to consistent string
    events_str = json.dumps([
        {
            "titre": e.titre,
            "debut": e.debut.isoformat(),
            "fin": e.fin.isoformat(),
            "salle": e.salle,
            "professeur": e.professeur
        }
        for e in events
    ], sort_keys=True)

    return hashlib.md5(events_str.encode()).hexdigest()
