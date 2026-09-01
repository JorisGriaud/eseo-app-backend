"""
FastAPI main application
Handles authentication, EDT retrieval, and user management
"""
from fastapi import FastAPI, Depends, HTTPException, status, Header, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from datetime import datetime, timezone, timedelta, date
import asyncio
import json
import os

import sync_coordination
from database import get_db, init_db, User, Event, ScheduleChange
from security import create_access_token, verify_token, get_eseo_id_from_token, RateLimiter
from scraper import ESEOScraper, mfa_cleanup_loop
from scheduler import (
    start_scheduler,
    stop_scheduler,
    maybe_sync_and_notify,
    _capture_pre_sync_state,
    _notify_if_changed,
)
from utils import PARIS_TZ, parse_eseo_datetime, format_datetime_for_response, calculate_schedule_hash_for_range

# FastAPI app initialization
app = FastAPI(
    title="EDT ESEO Backend",
    description="Backend API for ESEO schedule management with secure authentication",
    version="1.0.0"
)

# CORS configuration
# The API is stateless and only ever authenticates via the "Authorization: Bearer <jwt>"
# header, never via cookies, so allow_credentials must stay False - that keeps a
# wildcard origin CORS-compliant. Set ALLOWED_ORIGINS (comma-separated) in production
# to restrict which origins may call this API from a browser.
_allowed_origins_env = os.getenv("ALLOWED_ORIGINS")
allowed_origins = (
    [origin.strip() for origin in _allowed_origins_env.split(",") if origin.strip()]
    if _allowed_origins_env
    else ["*"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiters to slow down credential stuffing / TOTP brute-forcing.
# Keyed by email/session_id, not by IP, since this API is called through a
# reverse proxy without guaranteed client-IP forwarding today.
login_rate_limiter = RateLimiter(max_attempts=5, window_seconds=300)  # 5 tries / 5 min per email
mfa_rate_limiter = RateLimiter(max_attempts=5, window_seconds=300)  # 5 tries / 5 min per session


# Pydantic models for request/response validation
class LoginRequest(BaseModel):
    """Login request with ESEO credentials"""
    email: str = Field(..., description="ESEO email address")
    password: str = Field(..., description="ESEO password")


class LoginResponse(BaseModel):
    """Login response - either JWT token (success) or MFA challenge"""
    access_token: Optional[str] = None
    token_type: str = "bearer"
    eseo_id: Optional[str] = None
    mfa_required: bool = False
    session_id: Optional[str] = None
    mfa_type: Optional[str] = None  # "totp" or "push"
    mfa_data: Optional[str] = None  # For push: the number to match


class MFAVerifyRequest(BaseModel):
    """MFA verification request"""
    session_id: str = Field(..., description="Session ID from login response")
    totp_code: Optional[str] = Field(None, description="6-digit TOTP code (required for TOTP, not for push)")


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


class ScheduleChangeResponse(BaseModel):
    """A single logged schedule change, for the app's Logs screen"""
    id: int
    change_type: str  # "add" | "cancel" | "replace"
    event_id: Optional[int] = None
    debut: str
    fin: Optional[str] = None
    old_titre: Optional[str] = None
    old_salle: Optional[str] = None
    old_professeur: Optional[str] = None
    new_titre: Optional[str] = None
    new_salle: Optional[str] = None
    new_professeur: Optional[str] = None
    created_at: str


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

def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """
    Normalize a datetime to timezone-aware UTC.

    SQLite/SQLAlchemy round-trips DateTime columns as naive datetimes (the
    tzinfo is dropped on storage), even though the values are always stored in
    UTC. Comparing a naive value against an aware one with `==`/`<=` in plain
    Python silently gives the wrong answer instead of raising, so any
    DB-sourced datetime must be re-tagged as UTC before comparison.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _range_is_synced(user: User, start_utc: datetime, end_utc: datetime) -> bool:
    """True if [start_utc, end_utc] is fully covered by the user's tracked synced range."""
    synced_start = _as_utc(user.synced_start)
    synced_end = _as_utc(user.synced_end)
    return synced_start is not None and synced_end is not None and synced_start <= start_utc and synced_end >= end_utc


def _previously_known_range(user: User) -> Optional[tuple]:
    """
    Returns (start_utc, end_utc) already synced *before* this request touches
    user.synced_start/synced_end, or None if nothing was synced yet.

    Used to tell a genuinely new course landing in an already-tracked week
    from the routine initial population of a date range being synced for the
    first time - the latter would otherwise fire one "add" push notification
    per course, potentially dozens at once (see
    scheduler._persist_and_notify_changes). Must be read before
    _extend_synced_range() runs for this same request.
    """
    start = _as_utc(user.synced_start)
    end = _as_utc(user.synced_end)
    if start is not None and end is not None:
        return (start, end)
    return None


def _extend_synced_range(user: User, start_utc: datetime, end_utc: datetime) -> None:
    """
    Record that [start_utc, end_utc] has just been fetched from the API.

    Only a single contiguous interval is tracked. If the newly fetched range
    overlaps or touches the existing tracked interval, it's merged (extended);
    otherwise the tracked interval is replaced by the new range rather than
    merged, since a two-column interval can't represent a gap - replacing
    keeps us from ever over-reporting coverage.
    """
    synced_start = _as_utc(user.synced_start)
    synced_end = _as_utc(user.synced_end)

    if synced_start is not None and synced_end is not None and start_utc <= synced_end and end_utc >= synced_start:
        user.synced_start = min(synced_start, start_utc)
        user.synced_end = max(synced_end, end_utc)
    else:
        user.synced_start = start_utc
        user.synced_end = end_utc


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


def _upsert_single_event(db: Session, eseo_id: int, raw_event: Dict) -> Optional[tuple[int, bool]]:
    """
    Insert or update a single raw event for one user.

    Shared by upsert_events (a user's own full fetch) and
    propagate_group_events (fanning a groupe-tagged event out to other
    users), so the identity/matching/update logic lives in exactly one place.

    Returns (event_id, changed) - changed is True if a new row was created or
    an existing row's mutable fields were updated - or None if the event
    couldn't be parsed, or a race-condition duplicate insert was skipped.
    """
    try:
        debut_utc = parse_eseo_datetime(raw_event.get("Debut"))
        fin_utc = parse_eseo_datetime(raw_event.get("Fin"))

        if not debut_utc or not fin_utc:
            print(f"Skipping event with invalid timestamps: {raw_event}")
            return None

        titre = raw_event.get("Libelle", "Sans titre")

        # Check if event already exists (same identity: eseo_id, titre, debut)
        existing = db.query(Event).filter(
            and_(
                Event.eseo_id == eseo_id,
                Event.titre == titre,
                Event.debut == debut_utc
            )
        ).first()

        if existing:
            # Update mutable fields if they changed (e.g. room/professor reassignment)
            new_values = {
                "fin": fin_utc,
                "salle": raw_event.get("Emplacement"),
                "professeur": raw_event.get("Professeur"),
                "categorie_code": raw_event.get("Code"),
                "groupe": extract_groupe(raw_event),
            }
            changed = False
            for field, new_value in new_values.items():
                current_value = getattr(existing, field)
                # `fin` is a datetime read back from SQLite as naive UTC; normalize
                # both sides before comparing or it always looks "changed" (see _as_utc).
                if field == "fin":
                    current_value = _as_utc(current_value)
                if current_value != new_value:
                    setattr(existing, field, new_value)
                    changed = True
            return existing.id, changed

        # Create new event
        new_event = Event(
            eseo_id=eseo_id,
            titre=titre,
            debut=debut_utc,
            fin=fin_utc,
            salle=raw_event.get("Emplacement"),
            professeur=raw_event.get("Professeur"),
            categorie_code=raw_event.get("Code"),
            groupe=extract_groupe(raw_event)
        )

        db.add(new_event)
        try:
            db.flush()  # assign new_event.id without committing yet
        except IntegrityError:
            # Race condition: duplicate inserted concurrently between check and insert
            db.rollback()
            print(f"Duplicate event skipped (race condition): {titre}")
            return None

        return new_event.id, True

    except Exception as e:
        print(f"Error upserting event: {e}")
        return None


async def upsert_events(
    db: Session,
    eseo_id: int,
    raw_events: List[Dict],
    range_start_utc: datetime,
    range_end_utc: datetime,
) -> int:
    """
    Sync events to database for a given date range: insert new events, update
    events whose mutable fields changed (room/professor/etc.), and delete
    events that previously existed in this range but are no longer present
    upstream (cancelled/removed classes).

    Args:
        db: SQLAlchemy session
        eseo_id: User's ESEO ID
        raw_events: List of raw event dictionaries from API, covering
            [range_start_utc, range_end_utc]
        range_start_utc: Start of the fetched range (UTC), used to scope deletion
        range_end_utc: End of the fetched range (UTC), used to scope deletion

    Returns:
        Number of events inserted or updated

    Note:
        Uses (eseo_id, titre, debut) as the event's identity. Only rows whose
        `debut` falls within [range_start_utc, range_end_utc] are considered
        for deletion, so events outside the fetched window are left untouched.
    """
    upserted_count = 0
    touched_ids = set()

    for raw_event in raw_events:
        result = _upsert_single_event(db, eseo_id, raw_event)
        if result is None:
            continue
        event_id, changed = result
        touched_ids.add(event_id)
        if changed:
            upserted_count += 1

    db.commit()

    # Delete events that used to exist in this range but are no longer present
    # upstream (e.g. a cancelled class). Scoped strictly to the fetched range so
    # events outside it are never touched.
    stale_query = db.query(Event).filter(
        and_(
            Event.eseo_id == eseo_id,
            Event.debut >= range_start_utc,
            Event.debut <= range_end_utc
        )
    )
    if touched_ids:
        stale_query = stale_query.filter(~Event.id.in_(touched_ids))

    deleted_count = stale_query.delete(synchronize_session=False)
    if deleted_count:
        db.commit()
        print(f"Removed {deleted_count} stale event(s) for user {eseo_id} (no longer present upstream)")

    return upserted_count


def propagate_group_events(
    db: Session,
    source_eseo_id: int,
    raw_events: List[Dict],
    range_start_utc: datetime,
    range_end_utc: datetime,
) -> Dict[int, tuple]:
    """
    Propagate group-tagged events from a just-fetched user's raw_events to
    every other user who shares that `groupe`.

    A shared class/TP slot (e.g. "E2-Angers Gr1") is the same for everyone in
    it - unlike a personal course choice - so it's safe to trust one member's
    fresh fetch for the whole group instead of every member needing their own
    ESEO call (which is always per-eseo_id; ESEO has no per-class endpoint).

    Only raw_events whose extract_groupe(...) is truthy are considered. For
    each affected target user, this only touches THEIR rows for that specific
    groupe within the range - their personal events and other groupes are
    never read or written.

    Returns {target_eseo_id: (old_hash, old_events_map)} for every target
    that had at least one real change, with both captured *before* any
    writes - so the caller can notify them with the same before/after diff
    logic used for the user whose own fetch triggered this (see
    scheduler._capture_pre_sync_state / _notify_if_changed).
    """
    events_by_groupe: Dict[str, List[Dict]] = {}
    for raw_event in raw_events:
        groupe = extract_groupe(raw_event)
        if groupe:
            events_by_groupe.setdefault(groupe, []).append(raw_event)

    if not events_by_groupe:
        return {}

    # Discover every target affected by ANY touched groupe up front, so a
    # target sharing more than one of the touched groupes gets a single
    # before-snapshot covering all of them (not one per groupe, which would
    # miss part of the diff once the first groupe's writes had landed).
    target_groupes: Dict[int, List[str]] = {}
    for groupe in events_by_groupe:
        target_ids = [
            row[0] for row in db.query(Event.eseo_id).filter(
                and_(Event.groupe == groupe, Event.eseo_id != source_eseo_id)
            ).distinct().all()
        ]
        for target_eseo_id in target_ids:
            target_groupes.setdefault(target_eseo_id, []).append(groupe)

    affected: Dict[int, tuple] = {}

    for target_eseo_id, groupes in target_groupes.items():
        old_hash = calculate_schedule_hash_for_range(db, target_eseo_id, range_start_utc, range_end_utc)
        old_events = db.query(Event).filter(
            and_(
                Event.eseo_id == target_eseo_id,
                Event.debut >= range_start_utc,
                Event.debut <= range_end_utc,
            )
        ).all()
        old_events_map = {
            e.debut: {"titre": e.titre, "salle": e.salle, "professeur": e.professeur, "fin": e.fin}
            for e in old_events
        }

        touched_ids_by_groupe: Dict[str, set] = {}
        for groupe in groupes:
            touched_ids = set()
            for raw_event in events_by_groupe[groupe]:
                result = _upsert_single_event(db, target_eseo_id, raw_event)
                if result is not None:
                    event_id, _ = result
                    touched_ids.add(event_id)
            touched_ids_by_groupe[groupe] = touched_ids

        db.commit()

        # Delete this target's events for these SPECIFIC groupes and this
        # range that are no longer present upstream - never touches their
        # other groupes or personal events.
        for groupe, touched_ids in touched_ids_by_groupe.items():
            stale_query = db.query(Event).filter(
                and_(
                    Event.eseo_id == target_eseo_id,
                    Event.groupe == groupe,
                    Event.debut >= range_start_utc,
                    Event.debut <= range_end_utc,
                )
            )
            if touched_ids:
                stale_query = stale_query.filter(~Event.id.in_(touched_ids))
            stale_query.delete(synchronize_session=False)

        db.commit()

        new_hash = calculate_schedule_hash_for_range(db, target_eseo_id, range_start_utc, range_end_utc)
        if new_hash != old_hash:
            affected[target_eseo_id] = (old_hash, old_events_map)

    return affected


# Routes
@app.get("/", response_model=HealthResponse)
async def health_check():
    """Health check endpoint"""
    return HealthResponse(
        status="healthy",
        timestamp=datetime.now(timezone.utc).isoformat(),
        scheduler_running=True  # Will be updated with actual scheduler status
    )


def _finalize_login(db: Session, eseo_id: str) -> LoginResponse:
    """Create or update user and return JWT login response"""
    user = db.query(User).filter(User.eseo_id == int(eseo_id)).first()

    if not user:
        user = User(eseo_id=int(eseo_id))
        db.add(user)
        db.commit()
        db.refresh(user)

    user.last_sync = datetime.now(timezone.utc)
    user.updated_at = datetime.now(timezone.utc)
    db.commit()

    access_token = create_access_token(data={"eseo_id": int(eseo_id)})

    return LoginResponse(
        access_token=access_token,
        eseo_id=eseo_id
    )


@app.post("/auth/login", response_model=LoginResponse)
async def login(credentials: LoginRequest, db: Session = Depends(get_db)):
    """
    Authenticate user with ESEO credentials.

    If MFA is enabled, returns mfa_required=true with a session_id.
    The client must then call /auth/mfa/verify with the session_id and TOTP code.

    If no MFA, returns JWT token directly.
    """
    if not login_rate_limiter.check(credentials.email.strip().lower()):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later."
        )

    result = await ESEOScraper.start_login(credentials.email, credentials.password)

    if "error" in result:
        print(f"Login error for {credentials.email}: {result['error']}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials or unable to reach ESEO login."
        )

    if result.get("mfa_required"):
        return LoginResponse(
            mfa_required=True,
            session_id=result["session_id"],
            mfa_type=result.get("mfa_type"),
            mfa_data=result.get("mfa_data")
        )

    # No MFA needed - direct login
    return _finalize_login(db, result["eseo_id"])


@app.post("/auth/mfa/verify", response_model=LoginResponse)
async def verify_mfa(request: MFAVerifyRequest, db: Session = Depends(get_db)):
    """
    Complete MFA verification and return JWT token.

    For TOTP: send session_id + totp_code (6-digit code from authenticator app)
    For push: send session_id only (backend waits for approval on phone)

    On wrong TOTP code, returns 401 - client can retry with the same session_id.
    """
    if not mfa_rate_limiter.check(request.session_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many verification attempts. Please try again later."
        )

    result = await ESEOScraper.complete_mfa(request.session_id, request.totp_code)

    if "error" in result:
        print(f"MFA verify error for session {request.session_id}: {result['error']}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired verification code."
        )

    return _finalize_login(db, result["eseo_id"])


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
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = BackgroundTasks(),
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
        3. If cache hit: Return immediately, but still schedule a debounced
           background check (see maybe_sync_and_notify) - this is what lets
           opening/resuming the app replace the old always-on hourly polling.
        4. If cache miss or force=true: Fetch from API synchronously, and
           propagate any groupe-tagged changes to other users sharing that
           groupe (see propagate_group_events)
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
    # A cache hit requires the *entire* requested range to already be covered by
    # a previous fetch (tracked via User.synced_start/synced_end) - not merely
    # that some events happen to exist inside it. Otherwise a wider/overlapping
    # follow-up request would silently return a partial result (see history).
    if not force and _range_is_synced(current_user, start_datetime_utc, end_datetime_utc):
        cached_events = db.query(Event).filter(
            and_(
                Event.eseo_id == current_user.eseo_id,
                Event.debut >= start_datetime_utc,
                Event.debut <= end_datetime_utc
            )
        ).order_by(Event.debut).all()

        # Opening/resuming the app is what now drives real checks (replacing
        # the old always-on hourly poll) - debounced per user/groupe so a
        # burst of app opens from the same class doesn't hammer ESEO. Runs
        # after this response is already sent, so it never adds latency here.
        background_tasks.add_task(
            maybe_sync_and_notify, current_user.eseo_id, start_date.isoformat(), end_date.isoformat()
        )

        return AgendaResponse(
            events=[event_to_dict(event) for event in cached_events],
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            source="cache",
            fetched_at=datetime.now(timezone.utc).isoformat()
        )

    # Step 4: Fetch from API
    old_hash, old_events_map = _capture_pre_sync_state(
        db, current_user.eseo_id, start_datetime_utc, end_datetime_utc
    )

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

    # Step 5: Upsert events to DB, and propagate groupe-tagged changes to
    # other users sharing that groupe (see propagate_group_events)
    upserted_count = await upsert_events(
        db, current_user.eseo_id, raw_events, start_datetime_utc, end_datetime_utc
    )
    affected_targets = propagate_group_events(
        db, current_user.eseo_id, raw_events, start_datetime_utc, end_datetime_utc
    )

    # Step 6: Query again from DB (to get consistent format)
    fresh_events = db.query(Event).filter(
        and_(
            Event.eseo_id == current_user.eseo_id,
            Event.debut >= start_datetime_utc,
            Event.debut <= end_datetime_utc
        )
    ).order_by(Event.debut).all()

    # Notify this user and every affected groupmate if anything changed.
    # previously_known_range must be read before _extend_synced_range() below.
    new_hash = _notify_if_changed(
        db, current_user, start_datetime_utc, end_datetime_utc, old_hash, old_events_map,
        previously_known_range=_previously_known_range(current_user),
    )
    current_user.current_schedule_hash = new_hash

    if affected_targets:
        target_users = db.query(User).filter(User.eseo_id.in_(affected_targets.keys())).all()
        for target_user in target_users:
            target_old_hash, target_old_map = affected_targets[target_user.eseo_id]
            target_new_hash = _notify_if_changed(
                db, target_user, start_datetime_utc, end_datetime_utc, target_old_hash, target_old_map,
                previously_known_range=_previously_known_range(target_user),
            )
            target_user.current_schedule_hash = target_new_hash

    # Mark this user's own key + their groupes as freshly checked, so the
    # debounced background task/scheduled safety net don't redo this work.
    updated_groupes = [
        row[0] for row in db.query(Event.groupe).filter(
            Event.eseo_id == current_user.eseo_id, Event.groupe.isnot(None)
        ).distinct().all()
    ]
    sync_coordination.mark_checked(sync_coordination.user_key(current_user.eseo_id), updated_groupes)

    # Update user's last sync and the tracked synced range
    _extend_synced_range(current_user, start_datetime_utc, end_datetime_utc)
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


@app.get("/schedule/changes", response_model=List[ScheduleChangeResponse])
async def get_schedule_changes(
    limit: int = Query(50, le=200, description="Max number of changes to return"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get this user's schedule change history (most recent first), for the
    app's Logs screen. Populated by scheduler._persist_and_notify_changes
    every time a sync detects an added/cancelled/replaced event.
    """
    changes = db.query(ScheduleChange).filter(
        ScheduleChange.eseo_id == current_user.eseo_id
    ).order_by(ScheduleChange.created_at.desc()).limit(limit).all()

    return [
        ScheduleChangeResponse(
            id=change.id,
            change_type=change.change_type,
            event_id=change.event_id,
            debut=format_datetime_for_response(change.debut),
            fin=format_datetime_for_response(change.fin),
            old_titre=change.old_titre,
            old_salle=change.old_salle,
            old_professeur=change.old_professeur,
            new_titre=change.new_titre,
            new_salle=change.new_salle,
            new_professeur=change.new_professeur,
            created_at=format_datetime_for_response(change.created_at),
        )
        for change in changes
    ]


# Application lifecycle events
_mfa_cleanup_task: Optional[asyncio.Task] = None


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

    global _mfa_cleanup_task
    _mfa_cleanup_task = asyncio.create_task(mfa_cleanup_loop())
    print("MFA session cleanup task started")


@app.on_event("shutdown")
async def shutdown_event():
    """Stop background scheduler and MFA cleanup task on shutdown"""
    print("Shutting down...")
    stop_scheduler()

    if _mfa_cleanup_task:
        _mfa_cleanup_task.cancel()

    print("Background scheduler stopped")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
