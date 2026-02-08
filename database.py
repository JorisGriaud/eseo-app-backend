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
