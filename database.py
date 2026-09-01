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
