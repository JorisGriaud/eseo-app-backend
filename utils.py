"""
Utility functions for EDT application
Date calculations and helper functions
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict
import pytz
import hashlib
import json
from sqlalchemy import and_
from sqlalchemy.orm import Session
from bs4 import BeautifulSoup


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


def calculate_notes_hash(db: Session, eseo_id: int, semester_code: Optional[str] = None) -> str:
    """
    Calculate MD5 hash of a user's notes snapshot for one semester.

    Unlike calculate_schedule_hash_for_range, there's no date range: notes
    aren't calendar-scoped like schedule events. But since a user's Note rows
    now span every semester ESEO has a record for (see database.Note's
    semester_code), this MUST be scoped to one semester - otherwise a hash
    covering the whole table would never stabilize (historical semesters get
    (re)upserted at every login) and would make an unrelated semester's data
    look like a change to the current one.
    """
    from database import Note

    notes = db.query(Note).filter(
        Note.eseo_id == eseo_id, Note.semester_code == semester_code
    ).order_by(Note.external_key).all()

    notes_str = json.dumps([
        {
            "external_key": n.external_key,
            "libelle": n.libelle,
            "valeur": n.valeur,
            "coefficient": n.coefficient,
        }
        for n in notes
    ], sort_keys=True)

    return hashlib.md5(notes_str.encode()).hexdigest()


def parse_bulletin_html(html: str) -> Dict:
    """
    Extracts structured data from the "Bulletin provisoire" HTML document
    returned by reverse-proxy.eseo.fr/API-SP/api/bulletin/getBulletinByEtu -
    a print-oriented page (external fonts, print CSS), not something to
    embed as-is in the app. This pulls out just what the UI needs to render
    a native summary: student name, UE-level averages/ECTS/grade (not
    available anywhere in the getUE notes API), and the overall synthesis.

    Structure observed live (see database.Note for the getUE side of things,
    which only has individual evaluations, not these UE/overall aggregates):
        <h1>{student name}</h1>
        <h3>Bulletin de notes provisoire de {semester label}</h3>
        <table class="tableUF">
          <tr class="ligneUE">
            <td class="matiere" colspan="3">{UE name}</td>
            <td class="moyenne" rowspan="N">{UE average, often empty}</td>
            <td class="ects" rowspan="N">/{ects total}</td>
            <td class="grade" rowspan="N">{letter grade, often empty}</td>
          </tr>
          <tr><td class="eval">{eval name}</td><td>{coef}</td><td>{value}</td></tr>
          ... (one per evaluation in that UE)
        <table class="tableSynthese">
          ... overall Moyenne / Classement / Total ECTS cells

        Returns a plain dict (not a Pydantic model - this is assembled
        fresh per request, never stored), always with every key present
        even on a parse failure (empty ues list, None elsewhere) so the
        caller never needs to special-case a malformed document.
    """
    result: Dict = {"student_name": None, "title": None, "ues": [], "synthese": None}

    try:
        soup = BeautifulSoup(html, "html.parser")

        h1 = soup.find("h1")
        if h1:
            result["student_name"] = h1.get_text(strip=True)

        h3 = soup.find("h3")
        if h3:
            result["title"] = h3.get_text(strip=True)

        table = soup.find("table", class_="tableUF")
        if table:
            current_ue: Optional[Dict] = None
            for tr in table.find_all("tr"):
                classes = tr.get("class") or []
                if "ligneUE" in classes:
                    matiere = tr.find("td", class_="matiere")
                    moyenne = tr.find("td", class_="moyenne")
                    ects = tr.find("td", class_="ects")
                    grade = tr.find("td", class_="grade")
                    current_ue = {
                        "nom": matiere.get_text(strip=True) if matiere else None,
                        "moyenne": (moyenne.get_text(strip=True) or None) if moyenne else None,
                        "ects": ects.get_text(strip=True) if ects else None,
                        "grade": (grade.get_text(strip=True) or None) if grade else None,
                        "evaluations": [],
                    }
                    result["ues"].append(current_ue)
                elif current_ue is not None and tr.find("td", class_="eval"):
                    cells = tr.find_all("td")
                    current_ue["evaluations"].append({
                        "libelle": cells[0].get_text(strip=True) if len(cells) > 0 else None,
                        "coefficient": (cells[1].get_text(strip=True) or None) if len(cells) > 1 else None,
                        "valeur": (cells[2].get_text(strip=True) or None) if len(cells) > 2 else None,
                    })

        synth_table = soup.find("table", class_="tableSynthese")
        if synth_table:
            rows = synth_table.find_all("tr")
            if len(rows) >= 2:
                cells = rows[1].find_all("td")
                if len(cells) >= 3:
                    result["synthese"] = {
                        "moyenne": cells[0].get_text(strip=True),
                        "classement": cells[1].get_text(strip=True),
                        "ects": cells[2].get_text(strip=True),
                    }

    except Exception as e:
        print(f"Error parsing bulletin HTML: {e}")

    return result
