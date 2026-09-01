"""
Database models and configuration for EDT application
SQLAlchemy with SQLite backend
"""
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, UniqueConstraint, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime, timezone
import os

# Database configuration
DATABASE_URL = "sqlite:///./data/edt_app.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    """
    User model storing ESEO ID and device information
    No passwords stored - only authentication tokens
    """
    __tablename__ = "users"

    eseo_id = Column(Integer, primary_key=True, index=True)
    device_token = Column(String, nullable=True, index=True)  # Firebase device token
    sync_range = Column(Integer, default=4, nullable=False)  # Number of weeks to sync (default 4)
    last_sync = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # Last successful sync
    current_schedule_hash = Column(String(32), nullable=True)  # MD5 hash for change detection
    # Tracks the single contiguous UTC date range already fetched from the ESEO
    # API and stored in `events`, so /agenda can tell a true cache hit (the
    # requested range is fully covered) from a partial one. Only one interval
    # is tracked; a fetch that isn't contiguous with it replaces it rather than
    # merging, so we never over-report coverage.
    synced_start = Column(DateTime, nullable=True)
    synced_end = Column(DateTime, nullable=True)

    # Cached ESEO browser session (Playwright storage_state: cookies + the
    # MSAL token cache from localStorage), Fernet-encrypted at rest. Needed
    # because - unlike the schedule's public agenda API - the notes API
    # requires a live Microsoft SSO session (see scraper.py's notes fetching
    # section for why plain cookie replay doesn't work). Cleared whenever a
    # background fetch finds the underlying session has expired; repopulated
    # on the user's next real login. NULL means "notes unavailable until
    # next login" - never an error state.
    eseo_session_state_encrypted = Column(Text, nullable=True)
    notes_session_updated_at = Column(DateTime, nullable=True)
    current_notes_hash = Column(String(32), nullable=True)  # MD5 hash for change detection, mirrors current_schedule_hash
    notes_last_sync = Column(DateTime, nullable=True)
    # The {code} for the user's current/active semester (e.g. "97568") - the
    # only semester scheduler.sync_user_notes diffs/notifies on. Learned from
    # the notes API response, not derivable any other way (see scraper.py's
    # notes-fetching section).
    current_semester_code = Column(String(50), nullable=True)
    # JSON list of every semester/year discovered at last login, e.g.
    # [{"code": "88973", "label": "E2 Angers - Semestre 4 - 2025-2026"}, ...]
    # - powers the app's semester picker. Refreshed at each login only
    # (historical semesters' grades don't change, no need to re-crawl them
    # on the periodic safety-net job).
    available_semesters_json = Column(Text, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class Event(Base):
    """
    Event model - one row per course/event
    Normalized storage with individual columns for efficient querying
    """
    __tablename__ = "events"

    # Primary key
    id = Column(Integer, primary_key=True, index=True)

    # User relation
    eseo_id = Column(Integer, index=True, nullable=False)

    # Event data (mapped from API fields)
    titre = Column(String(500), nullable=False)  # Libelle
    debut = Column(DateTime, nullable=False, index=True)  # Debut (converted to UTC)
    fin = Column(DateTime, nullable=False)  # Fin (converted to UTC)
    salle = Column(String(100), nullable=True)  # Emplacement
    professeur = Column(String(200), nullable=True)  # Professeur
    categorie_code = Column(String(50), nullable=True)  # Code
    groupe = Column(String(100), nullable=True)  # Groupe (extracted from LesGroupes)

    # Metadata
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    # Constraints
    __table_args__ = (
        UniqueConstraint('eseo_id', 'titre', 'debut', name='uix_event_unique'),
        Index('idx_eseo_debut', 'eseo_id', 'debut'),  # Composite index for queries
    )


class ScheduleChange(Base):
    """
    Log of individual schedule changes (one row per changed event), used both
    to build per-event push notifications and to power the app's "Logs"
    history screen. Populated by scheduler._persist_and_notify_changes.
    """
    __tablename__ = "schedule_changes"

    id = Column(Integer, primary_key=True, index=True)
    eseo_id = Column(Integer, index=True, nullable=False)
    change_type = Column(String(20), nullable=False)  # "add" | "cancel" | "replace"

    # The new Event.id when applicable (add/replace); null for a cancellation,
    # since the event row no longer exists.
    event_id = Column(Integer, nullable=True)

    debut = Column(DateTime, nullable=False, index=True)
    fin = Column(DateTime, nullable=True)

    old_titre = Column(String(500), nullable=True)
    old_salle = Column(String(100), nullable=True)
    old_professeur = Column(String(200), nullable=True)

    new_titre = Column(String(500), nullable=True)
    new_salle = Column(String(100), nullable=True)
    new_professeur = Column(String(200), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        Index('idx_schedule_changes_eseo_created', 'eseo_id', 'created_at'),
    )


class Note(Base):
    """
    One row per grade/evaluation entry, fetched from the authenticated ESEO
    notes API (reverse-proxy.eseo.fr/API-SP/api/notes/getUE/{code}/{eseo_id}).
    That API returns a list of UE ("Unite d'Enseignement") objects, each with
    a nested `Contenu` list of individual evaluations - this table stores one
    row per evaluation (the UE's own name/code are denormalized onto each row
    for display grouping, since there's no separate UE table).

    Never purged (a grade is a permanent academic record, unlike a calendar
    Event) - only the NoteChange log below is purged like ScheduleChange.
    """
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, index=True)
    eseo_id = Column(Integer, index=True, nullable=False)

    # ESEO's own "intIdEvaluation" - a stable id per evaluation, used as the
    # diff key in scheduler._diff_notes (unlike Event, which has no natural
    # id from the API and relies on (eseo_id, titre, debut)). NOT globally
    # unique across semesters, confirmed live: an aggregate "Annee scolaire"
    # view reuses the SAME evaluation ids as the individual-semester views it
    # summarizes (e.g. "S5"+"S6" -> "Annee scolaire"). The unique key is
    # therefore (eseo_id, semester_code, external_key), not external_key alone.
    external_key = Column(String(50), nullable=False, index=True)

    # Which semester/year this evaluation belongs to (see User.current_semester_code
    # / available_semesters_json) - part of this row's identity (see
    # external_key's docstring above), never NULL for a row written by the
    # current upsert code. Only semester_code == User.current_semester_code
    # is ever diffed/notified on (see scheduler._capture_pre_sync_notes_state);
    # others are read-only historical data for the semester picker.
    semester_code = Column(String(50), nullable=True, index=True)
    semester_label = Column(String(300), nullable=True)

    ue_nom = Column(String(300), nullable=True)     # UE's strNom, e.g. "Developpement logiciel"
    ue_code = Column(String(100), nullable=True)     # UE's strCode, e.g. "E3e-S06-ANG-DEVLO"
    libelle = Column(String(300), nullable=False)    # evaluation's strTitre
    # Kept as String, not a numeric type: ESEO returns "" for a not-yet-graded
    # evaluation, and may use non-numeric placeholders elsewhere (e.g. "Abs").
    valeur = Column(String(20), nullable=True)
    coefficient = Column(String(20), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint('eseo_id', 'semester_code', 'external_key', name='uix_note_unique'),
    )


class NoteChange(Base):
    """
    Log of individual grade changes (one row per new/updated/removed
    evaluation), mirrors ScheduleChange. Populated by
    scheduler._persist_and_notify_note_changes; purged after 90 days like
    ScheduleChange (see purge_old_events) - the Note rows themselves are not.
    """
    __tablename__ = "note_changes"

    id = Column(Integer, primary_key=True, index=True)
    eseo_id = Column(Integer, index=True, nullable=False)
    change_type = Column(String(20), nullable=False)  # "add" | "update" | "remove"

    # Null once the note itself is gone (a "remove" - the evaluation
    # disappeared from a later ESEO fetch, e.g. removed by an admin).
    note_id = Column(Integer, nullable=True)
    external_key = Column(String(50), nullable=False)
    libelle = Column(String(300), nullable=True)

    old_valeur = Column(String(20), nullable=True)
    new_valeur = Column(String(20), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    __table_args__ = (
        Index('idx_note_changes_eseo_created', 'eseo_id', 'created_at'),
    )


def get_db():
    """
    Dependency for FastAPI routes to get database session
    Usage: db: Session = Depends(get_db)
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    Initialize database - create all tables
    Call this at application startup
    """
    # Create data directory if it doesn't exist
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _add_missing_columns()


def _add_missing_columns():
    """
    Add columns introduced after the initial table creation to existing databases.

    Base.metadata.create_all() only creates tables that don't exist yet - it never
    alters an existing table's schema. Nullable columns added to a model later
    (e.g. User.synced_start/synced_end) need an explicit ALTER TABLE here, or a
    deployment with an existing database file would crash on the first query
    referencing the new column.
    """
    expected_columns = {
        "users": {
            "synced_start": "DATETIME",
            "synced_end": "DATETIME",
            "eseo_session_state_encrypted": "TEXT",
            "notes_session_updated_at": "DATETIME",
            "current_notes_hash": "VARCHAR(32)",
            "notes_last_sync": "DATETIME",
            "current_semester_code": "VARCHAR(50)",
            "available_semesters_json": "TEXT",
        },
        "notes": {
            "semester_code": "VARCHAR(50)",
            "semester_label": "VARCHAR(300)",
        },
    }

    with engine.connect() as conn:
        for table, columns in expected_columns.items():
            existing = {
                row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            }
            for column, ddl_type in columns.items():
                if column not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
        conn.commit()
