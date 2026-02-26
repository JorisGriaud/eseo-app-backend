"""
Background scheduler for automatic EDT synchronization
Runs every hour from 7 AM to 7 PM to check for schedule changes
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timezone, timedelta, date
import time
import json
import asyncio
from typing import List
import os

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, messaging

from database import SessionLocal, User, Event
from scraper import ESEOScraper
from utils import PARIS_TZ, calculate_schedule_hash_for_range, parse_eseo_datetime

# Global scheduler instance
scheduler = BackgroundScheduler()

# Initialize Firebase Admin SDK (once at startup)
def initialize_firebase():
    """
    Initialize Firebase Admin SDK with service account credentials

    Note: This should be called once at application startup
    """
    if not firebase_admin._apps:
        try:
            cred_path = os.getenv("FIREBASE_CREDENTIALS_PATH", "./credentials/firebase-credentials.json")

            if not os.path.exists(cred_path):
                print(f"Warning: Firebase credentials not found at {cred_path}")
                print("Push notifications will be disabled")
                return False

            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
            print(f"✅ Firebase Admin SDK initialized with credentials from: {cred_path}")
            return True
        except Exception as e:
            print(f"Error initializing Firebase Admin SDK: {e}")
            return False
    return True


def send_firebase_notification(device_token: str, title: str, body: str, data: dict = None):
    """
    Send push notification via Firebase Admin SDK

    Args:
        device_token: FCM device token
        title: Notification title
        body: Notification body
        data: Additional data payload (optional)

    Returns:
        Message ID if successful, None otherwise

    Note: Requires Firebase Admin SDK to be initialized with credentials
    """
    # Ensure Firebase is initialized
    if not initialize_firebase():
        print("Firebase not initialized, skipping notification")
        return None

    try:
        # Construct the message
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            token=device_token,
            # Android-specific options
            android=messaging.AndroidConfig(
                priority='high',
                notification=messaging.AndroidNotification(
                    sound='default',
                    click_action='FLUTTER_NOTIFICATION_CLICK',
                ),
            ),
            # iOS-specific options
            apns=messaging.APNSConfig(
                payload=messaging.APNSPayload(
                    aps=messaging.Aps(
                        sound='default',
                    ),
                ),
            ),
        )

        # Add custom data if provided
        if data:
            message.data = data

        # Send the message
        response = messaging.send(message)
        print(f"✅ Notification sent successfully to {device_token[:20]}... (Message ID: {response})")
        return response

    except firebase_admin.exceptions.FirebaseError as e:
        print(f"❌ Firebase error sending notification: {e}")
        return None
    except Exception as e:
        print(f"❌ Error sending notification: {e}")
        return None


def sync_user_schedule(eseo_id: int, device_token: str = None, current_hash: str = None, sync_range: int = 4) -> bool:
    """
    Sync schedule for a single user and send notification if changed

    Args:
        eseo_id: User's ESEO ID
        device_token: Firebase device token (if notifications enabled)
        current_hash: Current schedule hash for comparison
        sync_range: Number of weeks to sync (from Users.sync_range)

    Returns:
        True if sync successful, False otherwise
    """
    db = SessionLocal()
    try:
        # Fetch latest schedule using user's personalized sync_range
        schedule_data = ESEOScraper.fetch_schedule(str(eseo_id), weeks=sync_range)

        if not schedule_data:
            print(f"Failed to fetch schedule for user {eseo_id}")
            return False

        new_hash = schedule_data["hash"]

        # Check if schedule changed
        has_changed = ESEOScraper.compare_schedules(current_hash, new_hash)

        if has_changed:
            print(f"Schedule changed for user {eseo_id}")

            # Save new schedule
            new_schedule = Schedule(
                eseo_id=eseo_id,
                schedule_data=json.dumps(schedule_data["schedule"]),
                schedule_hash=new_hash,
                fetched_at=datetime.now(timezone.utc)
            )
            db.add(new_schedule)

            # Update user hash
            user = db.query(User).filter(User.eseo_id == eseo_id).first()
            if user:
                user.current_schedule_hash = new_hash
                user.last_sync = datetime.now(timezone.utc)
                user.updated_at = datetime.now(timezone.utc)

            db.commit()

            # Send notification if device token available
            if device_token:
                send_firebase_notification(
                    device_token=device_token,
                    title="📅 Emploi du temps modifié",
                    body="Votre emploi du temps a été mis à jour. Consultez les changements.",
                    data={"type": "schedule_update", "eseo_id": str(eseo_id)}
                )
        else:
            # Update last sync time even if no changes
            user = db.query(User).filter(User.eseo_id == eseo_id).first()
            if user:
                user.last_sync = datetime.now(timezone.utc)
                db.commit()

        return True

    except Exception as e:
        print(f"Error syncing user {eseo_id}: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def sync_all_users():
    """
    Sync schedules for all users in database
    Called by scheduler every hour (7 AM - 7 PM)

    Each user syncs their personalized range from Users.sync_range

    Rate limiting: 2 seconds delay between users to avoid overwhelming ESEO servers
    For 1000 users: ~33 minutes total
    """
    print(f"[{datetime.now().isoformat()}] Starting schedule sync for all users")

    db = SessionLocal()
    try:
        # Get all users with their device tokens and sync_range
        users = db.query(User).all()
        total_users = len(users)

        if total_users == 0:
            print("No users to sync")
            return

        print(f"Syncing schedules for {total_users} users")

        success_count = 0
        fail_count = 0

        for index, user in enumerate(users, 1):
            print(f"Processing user {index}/{total_users}: {user.eseo_id} (sync_range={user.sync_range} weeks)")

            success = sync_user_schedule(
                eseo_id=user.eseo_id,
                device_token=user.device_token,
                current_hash=user.current_schedule_hash,
                sync_range=user.sync_range
            )

            if success:
                success_count += 1
            else:
                fail_count += 1

            # Rate limiting: 2 seconds between requests
            if index < total_users:
                time.sleep(2)

        print(f"Sync completed: {success_count} successful, {fail_count} failed")

    except Exception as e:
        print(f"Error in sync_all_users: {e}")
    finally:
        db.close()


def _clean_course_name(course_name: str) -> str:
    """
    Clean and shorten course name for notification

    Args:
        course_name: Full course name from API

    Returns:
        Cleaned course name

    Examples:
        "Mathématiques - Travaux dirigés Groupe 1 (E2-Angers Gr1)" -> "Mathématiques"
        "Projet d'électronique analogique - TP" -> "Projet d'électronique"
    """
    if not course_name:
        return ""

    # Remove everything after " - " (type de cours, groupe, etc.)
    if " - " in course_name:
        course_name = course_name.split(" - ")[0]

    # Remove everything in parentheses
    if "(" in course_name:
        course_name = course_name.split("(")[0]

    # Trim whitespace
    course_name = course_name.strip()

    # Limit length to 40 characters
    if len(course_name) > 40:
        course_name = course_name[:37] + "..."

    return course_name


def _format_time(dt_utc: datetime) -> str:
    """
    Format datetime to short time string in Paris timezone

    Args:
        dt_utc: Datetime in UTC

    Returns:
        Short time string (e.g., "9h", "14h30")

    Examples:
        datetime(2026, 2, 17, 8, 0) UTC -> "9h" (Paris)
        datetime(2026, 2, 17, 13, 30) UTC -> "14h30" (Paris)
    """
    if not dt_utc:
        return ""

    # If naive datetime (from SQLite), treat as UTC — same pattern as format_datetime_for_response()
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)

    # Convert to Paris timezone
    dt_paris = dt_utc.astimezone(PARIS_TZ)

    # Format as "9h" or "14h30"
    if dt_paris.minute == 0:
        return f"{dt_paris.hour}h"
    else:
        return f"{dt_paris.hour}h{dt_paris.minute:02d}"


def _create_change_notification_message(
    db,
    eseo_id: int,
    start_datetime_utc: datetime,
    end_datetime_utc: datetime,
    old_events_map: dict
) -> str:
    """
    Create a detailed notification message showing what changed with time ranges

    Args:
        db: Database session
        eseo_id: User's ESEO ID
        start_datetime_utc: Start of sync range
        end_datetime_utc: End of sync range
        old_events_map: Dict mapping (debut, salle) -> (titre, fin) of old events

    Returns:
        Formatted notification message with specific changes and time ranges

    Examples:
        "Mathématiques 9h-11h → Physique"
        "Ajouté: Anglais 14h-16h"
        "Annulé: Chimie 10h-12h"
        "Mathématiques 9h-11h → Physique, Ajouté: Anglais 14h-16h"
    """
    try:
        # Get current events in the range
        current_events = db.query(Event).filter(
            Event.eseo_id == eseo_id,
            Event.debut >= start_datetime_utc,
            Event.debut <= end_datetime_utc
        ).all()

        # Create map of current events: (debut, salle) -> (titre, fin)
        current_events_map = {
            (event.debut, event.salle): (event.titre, event.fin)
            for event in current_events
        }

        changes = []

        # Detect replacements (same time/room, different course)
        for key, old_data in old_events_map.items():
            debut_utc, salle = key
            old_titre, old_fin = old_data

            if key in current_events_map:
                new_titre, new_fin = current_events_map[key]
                if old_titre != new_titre:
                    old_clean = _clean_course_name(old_titre)
                    new_clean = _clean_course_name(new_titre)
                    if old_clean and new_clean and old_clean != new_clean:
                        time_range = f"{_format_time(debut_utc)}-{_format_time(old_fin)}"
                        changes.append(f"{old_clean} {time_range} → {new_clean}")

        # Detect additions (new time slots or rooms)
        for key, new_data in current_events_map.items():
            debut_utc, salle = key
            new_titre, new_fin = new_data

            if key not in old_events_map:
                new_clean = _clean_course_name(new_titre)
                if new_clean:
                    time_range = f"{_format_time(debut_utc)}-{_format_time(new_fin)}"
                    changes.append(f"Ajouté: {new_clean} {time_range}")

        # Detect deletions (old events no longer present)
        for key, old_data in old_events_map.items():
            debut_utc, salle = key
            old_titre, old_fin = old_data

            if key not in current_events_map:
                old_clean = _clean_course_name(old_titre)
                if old_clean:
                    time_range = f"{_format_time(debut_utc)}-{_format_time(old_fin)}"
                    changes.append(f"Annulé: {old_clean} {time_range}")

        if not changes:
            return "Emploi du temps mis à jour"

        # Limit to 3 changes to avoid too long notification
        if len(changes) <= 3:
            return ", ".join(changes)
        else:
            # Show first 2 changes + count
            return f"{changes[0]}, {changes[1]} et {len(changes) - 2} autre(s)"

    except Exception as e:
        print(f"Error creating notification message: {e}")
        return "Emploi du temps mis à jour"


async def sync_user_schedule_v2(
    eseo_id: int,
    device_token: str = None,
    sync_range: int = 4
) -> bool:
    """
    Sync user schedule using new Event model (async version)

    Args:
        eseo_id: User's ESEO ID
        device_token: Firebase device token (if notifications enabled)
        sync_range: Number of weeks to sync

    Returns:
        True if sync successful, False otherwise

    Process:
        1. Calculate date range
        2. Calculate old hash (before fetch)
        3. Fetch from API
        4. Upsert events
        5. Calculate new hash
        6. Send notification if changed
    """
    db = SessionLocal()
    try:
        # Calculate date range
        start_date = datetime.now(PARIS_TZ).date()
        end_date = start_date + timedelta(weeks=sync_range)

        # Convert to UTC
        start_datetime_utc = PARIS_TZ.localize(
            datetime.combine(start_date, datetime.min.time())
        ).astimezone(timezone.utc)

        end_datetime_utc = PARIS_TZ.localize(
            datetime.combine(end_date, datetime.max.time())
        ).astimezone(timezone.utc)

        # Calculate old hash (before fetch)
        old_hash = calculate_schedule_hash_for_range(
            db, eseo_id, start_datetime_utc, end_datetime_utc
        )

        # Capture old events BEFORE fetch (for change detection)
        old_events = db.query(Event).filter(
            Event.eseo_id == eseo_id,
            Event.debut >= start_datetime_utc,
            Event.debut <= end_datetime_utc
        ).all()

        # Create map of old events: (debut, salle) -> (titre, fin)
        old_events_map = {
            (event.debut, event.salle): (event.titre, event.fin)
            for event in old_events
        }

        # Fetch from API
        raw_events = await ESEOScraper.fetch_schedule_async(
            str(eseo_id),
            start_date.isoformat(),
            end_date.isoformat()
        )

        if raw_events is None:
            print(f"Failed to fetch schedule for user {eseo_id}")
            return False

        # Upsert events
        from main import upsert_events
        upserted_count = await upsert_events(db, eseo_id, raw_events)

        # Calculate new hash
        new_hash = calculate_schedule_hash_for_range(
            db, eseo_id, start_datetime_utc, end_datetime_utc
        )

        # Check if changed
        has_changed = (old_hash != new_hash)

        if has_changed:
            print(f"Schedule changed for user {eseo_id} ({upserted_count} events upserted)")

            # Send notification if device token available
            if device_token:
                # Create detailed message showing what changed
                notification_message = _create_change_notification_message(
                    db, eseo_id, start_datetime_utc, end_datetime_utc, old_events_map
                )

                send_firebase_notification(
                    device_token,
                    "Emploi du temps modifié",
                    notification_message
                )

        # Update user
        user = db.query(User).filter(User.eseo_id == eseo_id).first()
        if user:
            user.current_schedule_hash = new_hash
            user.last_sync = datetime.now(timezone.utc)
            user.updated_at = datetime.now(timezone.utc)
            db.commit()

        return True

    except Exception as e:
        print(f"Error syncing user {eseo_id}: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def sync_all_users_v2():
    """
    Sync all users with asyncio.run (new version for Event model)

    Called by scheduler every hour (7 AM - 7 PM)
    Uses async fetch_schedule_async for better performance
    """
    print(f"[{datetime.now().isoformat()}] Starting schedule sync for all users (v2)")

    db = SessionLocal()
    try:
        users = db.query(User).all()
        total_users = len(users)

        if total_users == 0:
            print("No users to sync")
            return

        print(f"Syncing schedules for {total_users} users")

        success_count = 0
        fail_count = 0

        for index, user in enumerate(users, 1):
            print(f"Processing user {index}/{total_users}: {user.eseo_id} (sync_range={user.sync_range} weeks)")

            # Run async in sync context
            success = asyncio.run(sync_user_schedule_v2(
                user.eseo_id,
                user.device_token,
                user.sync_range
            ))

            if success:
                success_count += 1
            else:
                fail_count += 1

            # Rate limiting: 2 seconds between requests
            if index < total_users:
                time.sleep(2)

        print(f"Sync completed: {success_count} successful, {fail_count} failed")

    except Exception as e:
        print(f"Error in sync_all_users_v2: {e}")
    finally:
        db.close()


def purge_old_events():
    """
    Purge events older than 6 months to prevent DB bloat
    Runs once per day at 2 AM
    """
    print(f"[{datetime.now().isoformat()}] Starting old events purge...")

    db = SessionLocal()
    try:
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=180)  # 6 months

        deleted = db.query(Event).filter(Event.fin < cutoff_date).delete()
        db.commit()

        print(f"Purged {deleted} events older than {cutoff_date.date()}")

    except Exception as e:
        print(f"Error purging old events: {e}")
        db.rollback()
    finally:
        db.close()


def start_scheduler():
    """
    Start the background scheduler
    Runs sync_all_users_v2 every hour from 7 AM to 7 PM (school hours)
    Runs purge_old_events daily at 2 AM
    """
    if scheduler.running:
        print("Scheduler already running")
        return

    # Schedule sync job to run every hour between 7 AM and 7 PM
    scheduler.add_job(
        func=sync_all_users_v2,  # Use new version
        trigger=CronTrigger(hour="7-19", minute=0),
        id="schedule_sync",
        name="Synchronize all user schedules",
        replace_existing=True
    )

    # Schedule purge job to run daily at 2 AM
    scheduler.add_job(
        func=purge_old_events,
        trigger=CronTrigger(hour=2, minute=0),
        id="purge_old_events",
        name="Purge events > 6 months",
        replace_existing=True
    )

    scheduler.start()
    print("Scheduler started: Sync hourly 7AM-7PM, Purge daily 2AM")


def stop_scheduler():
    """Stop the background scheduler"""
    if scheduler.running:
        scheduler.shutdown()
        print("Scheduler stopped")


def run_sync_now():
    """
    Manual trigger for testing
    Run sync immediately without waiting for scheduled time
    """
    print("Running manual sync...")
    sync_all_users()


# For testing purposes
if __name__ == "__main__":
    print("Testing scheduler...")
    print("Running immediate sync...")
    run_sync_now()
