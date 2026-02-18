"""
FastAPI main application
Handles authentication, EDT retrieval, and user management
"""
from fastapi import FastAPI, Depends, HTTPException, status, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import datetime, timezone, timedelta, date
import json

from database import get_db, init_db, User, Event
from security import create_access_token, verify_token, get_eseo_id_from_token
from scraper import ESEOScraper
from scheduler import start_scheduler, stop_scheduler
from utils import PARIS_TZ, parse_eseo_datetime, format_datetime_for_response

# FastAPI app initialization
app = FastAPI(
    title="EDT ESEO Backend",
    description="Backend API for ESEO schedule management with secure authentication",
    version="1.0.0"
)

# CORS configuration - adjust for production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Change to specific origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Pydantic models for request/response validation
class LoginRequest(BaseModel):
    """Login request with ESEO credentials"""
    email: str = Field(..., description="ESEO email address")
    password: str = Field(..., description="ESEO password")


class LoginResponse(BaseModel):
    """Login response with JWT token"""
    access_token: str
    token_type: str = "bearer"
    eseo_id: str


class RegisterDeviceRequest(BaseModel):
    """Device token registration for push notifications"""
    device_token: str = Field(..., description="Firebase Cloud Messaging token")


class AgendaResponse(BaseModel):
    """Agenda response with cache-first strategy"""
    events: List[dict]
    start_date: str
    end_date: str
    source: str  # "cache" or "api"
    fetched_at: str


class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    timestamp: str
    scheduler_running: bool


# Dependency to extract and verify JWT from Authorization header
async def get_current_user(authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> User:
    """
    Verify JWT token and return associated user
    Raises HTTPException if token is invalid or user not found
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header missing",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Extract token from "Bearer <token>" format
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication scheme",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify token and extract eseo_id
    eseo_id = get_eseo_id_from_token(token)
    if not eseo_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Get user from database
    user = db.query(User).filter(User.eseo_id == eseo_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    return user


# Helper functions for new /agenda endpoint

def extract_groupe(event: dict) -> Optional[str]:
    """
    Extract Groupe from nested LesGroupes structure

    Args:
        event: Raw event dictionary from API

    Returns:
        Groupe string (e.g. "E2-Angers Gr1") or None
    """
    if event.get("LesGroupes") and isinstance(event["LesGroupes"], list) and len(event["LesGroupes"]) > 0:
        lien_agenda = event["LesGroupes"][0].get("LienAgenda")
        if lien_agenda:
            return lien_agenda.get("Groupe")
    return None


def event_to_dict(event: Event) -> dict:
    """
    Convert Event model to dictionary for API response

    Args:
        event: Event model instance

    Returns:
        Dictionary with formatted event data including creation/update timestamps
    """
    return {
        "id": event.id,
        "titre": event.titre,
        "debut": format_datetime_for_response(event.debut),
        "fin": format_datetime_for_response(event.fin),
        "salle": event.salle,
        "professeur": event.professeur,
        "categorie_code": event.categorie_code,
        "groupe": event.groupe,
        "created_at": format_datetime_for_response(event.created_at),
        "updated_at": format_datetime_for_response(event.updated_at)
    }


async def upsert_events(db: Session, eseo_id: int, raw_events: List[Dict]) -> int:
    """
    Upsert events to database (insert new, skip duplicates)

    Args:
        db: SQLAlchemy session
        eseo_id: User's ESEO ID
        raw_events: List of raw event dictionaries from API

    Returns:
        Number of events successfully upserted

    Note:
        Uses UniqueConstraint (eseo_id, titre, debut) to detect duplicates
    """
    upserted_count = 0

    for raw_event in raw_events:
        try:
            # Parse timestamps
            debut_utc = parse_eseo_datetime(raw_event.get("Debut"))
            fin_utc = parse_eseo_datetime(raw_event.get("Fin"))

            if not debut_utc or not fin_utc:
                print(f"Skipping event with invalid timestamps: {raw_event}")
                continue

            # Check if event already exists (UniqueConstraint)
            existing = db.query(Event).filter(
                and_(
                    Event.eseo_id == eseo_id,
                    Event.titre == raw_event.get("Libelle"),
                    Event.debut == debut_utc
                )
            ).first()

            if existing:
                # Event already exists, skip
                continue

            # Create new event
            new_event = Event(
                eseo_id=eseo_id,
                titre=raw_event.get("Libelle", "Sans titre"),
                debut=debut_utc,
                fin=fin_utc,
                salle=raw_event.get("Emplacement"),
                professeur=raw_event.get("Professeur"),
                categorie_code=raw_event.get("Code"),
                groupe=extract_groupe(raw_event)
            )

            db.add(new_event)
            upserted_count += 1

        except Exception as e:
            print(f"Error upserting event: {e}")
            continue

    # Commit all at once
    try:
        db.commit()
    except IntegrityError as e:
        # Handle race condition (duplicate inserted between check and insert)
        db.rollback()
        print(f"Integrity error during upsert: {e}")

    return upserted_count


# Routes
@app.get("/", response_model=HealthResponse)
async def health_check():
    """Health check endpoint"""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc).isoformat(),
        scheduler_running=True  # Will be updated with actual scheduler status
    )


@app.post("/auth/login", response_model=LoginResponse)
async def login(credentials: LoginRequest, db: Session = Depends(get_db)):
    """
    Authenticate user with ESEO credentials and return JWT token

    Process:
    1. Use Playwright to authenticate with Microsoft
    2. Extract eseo_id from SharePoint
    3. Create or update user in database
    4. Return JWT token containing eseo_id

    Note: Credentials are NOT stored - only used for authentication
    """
    # Extract ESEO ID using Playwright
    eseo_id = await ESEOScraper.extract_eseo_id(credentials.email, credentials.password)

    if not eseo_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials or unable to extract ESEO ID"
        )

    # Check if user exists
    user = db.query(User).filter(User.eseo_id == int(eseo_id)).first()

    if not user:
        # Create new user
        user = User(eseo_id=int(eseo_id))
        db.add(user)
        db.commit()
        db.refresh(user)

    # Update last sync time
    user.last_sync = datetime.now(timezone.utc)
    user.updated_at = datetime.now(timezone.utc)
    db.commit()

    # Create JWT token
    access_token = create_access_token(data={"eseo_id": int(eseo_id)})

    return LoginResponse(
        access_token=access_token,
        eseo_id=eseo_id
    )


@app.post("/auth/register-device")
async def register_device(
    request: RegisterDeviceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Register device token for push notifications

    The Flutter app should call this after login to enable notifications
    """
    current_user.device_token = request.device_token
    current_user.updated_at = datetime.now(timezone.utc)
    db.commit()

    return {"message": "Device token registered successfully"}


@app.delete("/auth/logout")
async def logout(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Logout user (removes device token to stop notifications)

    Note: JWT token will remain valid until expiration
    Client should delete token from local storage
    """
    current_user.device_token = None
    current_user.updated_at = datetime.now(timezone.utc)
    db.commit()

    return {"message": "Logged out successfully"}


@app.get("/agenda", response_model=AgendaResponse)
async def get_agenda(
    start: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    end: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    force: bool = Query(False, description="Force refresh from API"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get user's agenda with cache-first strategy

    Query params:
        - start: Optional start date (default: today)
        - end: Optional end date (default: start + 4 weeks)
        - force: Force refresh from API (default: false)

    Logic:
        1. Parse and validate dates
        2. If force=false: Query DB for [start, end] range
        3. If cache hit: Return immediately
        4. If cache miss or force=true: Fetch from API
        5. Upsert events to DB
        6. Return events

    Example:
        GET /agenda  (today + 4 weeks)
        GET /agenda?start=2026-02-10&end=2026-02-17
        GET /agenda?force=true
    """
    # Step 1: Parse dates with defaults
    try:
        if start:
            start_date = datetime.strptime(start, "%Y-%m-%d").date()
        else:
            start_date = datetime.now(PARIS_TZ).date()

        if end:
            end_date = datetime.strptime(end, "%Y-%m-%d").date()
        else:
            end_date = start_date + timedelta(weeks=4)

        # Validate range
        if end_date < start_date:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="end_date must be after start_date"
            )
        if (end_date - start_date).days > 365:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Date range cannot exceed 1 year"
            )

    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid date format. Use YYYY-MM-DD"
        )

    # Step 2: Convert to UTC for DB query
    start_datetime_utc = PARIS_TZ.localize(
        datetime.combine(start_date, datetime.min.time())
    ).astimezone(timezone.utc)

    end_datetime_utc = PARIS_TZ.localize(
        datetime.combine(end_date, datetime.max.time())
    ).astimezone(timezone.utc)

    # Step 3: Cache-first logic (skip if force=true)
    if not force:
        cached_events = db.query(Event).filter(
            and_(
                Event.eseo_id == current_user.eseo_id,
                Event.debut >= start_datetime_utc,
                Event.debut <= end_datetime_utc
            )
        ).order_by(Event.debut).all()

        if cached_events:
            return AgendaResponse(
                events=[event_to_dict(event) for event in cached_events],
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                source="cache",
                fetched_at=datetime.now(timezone.utc).isoformat()
            )

    # Step 4: Fetch from API
    raw_events = await ESEOScraper.fetch_schedule_async(
        eseo_id=str(current_user.eseo_id),
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat()
    )

    if raw_events is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to fetch schedule from ESEO API"
        )

    # Step 5: Upsert events to DB
    upserted_count = await upsert_events(db, current_user.eseo_id, raw_events)

    # Step 6: Query again from DB (to get consistent format)
    fresh_events = db.query(Event).filter(
        and_(
            Event.eseo_id == current_user.eseo_id,
            Event.debut >= start_datetime_utc,
            Event.debut <= end_datetime_utc
        )
    ).order_by(Event.debut).all()

    # Update user's last sync
    current_user.last_sync = datetime.now(timezone.utc)
    current_user.updated_at = datetime.now(timezone.utc)
    db.commit()

    return AgendaResponse(
        events=[event_to_dict(event) for event in fresh_events],
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        source="api",
        fetched_at=datetime.now(timezone.utc).isoformat()
    )


# Application lifecycle events
@app.on_event("startup")
async def startup_event():
    """Initialize database and start background scheduler"""
    print("Starting EDT ESEO Backend...")
    init_db()
    print("Database initialized")

    # Initialize Firebase Admin SDK for push notifications
    from scheduler import initialize_firebase
    initialize_firebase()

    start_scheduler()
    print("Background scheduler started")


@app.on_event("shutdown")
async def shutdown_event():
    """Stop background scheduler on shutdown"""
    print("Shutting down...")
    stop_scheduler()
    print("Background scheduler stopped")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
