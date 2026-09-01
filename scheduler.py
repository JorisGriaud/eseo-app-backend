"""
Background scheduler for automatic EDT synchronization
Runs every hour from 7 AM to 7 PM to check for schedule changes
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timezone, timedelta, date
from typing import Dict, List, Optional
import time
import asyncio
import os

# Firebase Admin SDK
import firebase_admin
from firebase_admin import credentials, messaging

import sync_coordination
from database import SessionLocal, User, Event, ScheduleChange
from scraper import ESEOScraper
from utils import PARIS_TZ, calculate_schedule_hash_for_range

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
            print(f"[OK] Firebase Admin SDK initialized with credentials from: {cred_path}")
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
        print(f"[OK] Notification sent successfully to {device_token[:20]}... (Message ID: {response})")
        return response

    except firebase_admin.exceptions.FirebaseError as e:
        print(f"[ERROR] Firebase error sending notification: {e}")
        return None
    except Exception as e:
        print(f"[ERROR] Error sending notification: {e}")
        return None


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


def _to_paris(dt_utc: datetime) -> datetime:
    """Convert a datetime (possibly naive from SQLite) to Paris timezone"""
    if not dt_utc:
        return None
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(PARIS_TZ)


def _format_time(dt_utc: datetime) -> str:
    """Format datetime to short time string in Paris timezone (e.g. '9h', '14h30')"""
    dt_paris = _to_paris(dt_utc)
    if not dt_paris:
        return ""
    if dt_paris.minute == 0:
        return f"{dt_paris.hour}h"
    return f"{dt_paris.hour}h{dt_paris.minute:02d}"


def _format_date(dt_utc: datetime) -> str:
    """Format datetime to short date string in Paris timezone (e.g. '09/02', '15/04')"""
    dt_paris = _to_paris(dt_utc)
    if not dt_paris:
        return ""
    return f"{dt_paris.day:02d}/{dt_paris.month:02d}"


def _capture_pre_sync_state(db, eseo_id: int, start_datetime_utc: datetime, end_datetime_utc: datetime):
    """
    Snapshot a user's current DB state for a range, before any writes -
    needed both to decide if anything changed (hash) and to build individual
    per-event notifications afterwards (see _diff_events). Used for the user
    whose own fetch triggered a sync, and (by propagate_group_events) for
    every groupmate affected by a fan-out write.

    Keyed by `debut` alone (not (debut, salle) as before a room-only change
    used to look like a cancel+add pair instead of a single "replace").
    """
    old_hash = calculate_schedule_hash_for_range(db, eseo_id, start_datetime_utc, end_datetime_utc)
    old_events = db.query(Event).filter(
        Event.eseo_id == eseo_id,
        Event.debut >= start_datetime_utc,
        Event.debut <= end_datetime_utc
    ).all()
    old_events_map = {
        event.debut: {
            "titre": event.titre,
            "salle": event.salle,
            "professeur": event.professeur,
            "fin": event.fin,
        }
        for event in old_events
    }
    return old_hash, old_events_map


def _diff_events(old_events_map: dict, current_events_map: dict) -> List[dict]:
    """
    Diff two {debut: {titre, salle, professeur, fin}} snapshots into a list
    of individual changes - one per added/cancelled/replaced timeslot -
    instead of a single aggregated message. Each user now gets one push
    notification per change (see _persist_and_notify_changes) rather than
    one bundled text blob for the whole sync.
    """
    changes = []
    for debut in set(old_events_map) | set(current_events_map):
        old = old_events_map.get(debut)
        new = current_events_map.get(debut)

        if old and new:
            if old != new:
                changes.append({"type": "replace", "debut": debut, "old": old, "new": new})
        elif new and not old:
            changes.append({"type": "add", "debut": debut, "old": None, "new": new})
        elif old and not new:
            changes.append({"type": "cancel", "debut": debut, "old": old, "new": None})

    changes.sort(key=lambda c: c["debut"])
    return changes


def _format_notification(
    change_type: str,
    debut: datetime,
    fin: datetime,
    old: Optional[dict],
    new: Optional[dict],
) -> tuple:
    """Builds (title, body) for a single change's push notification."""
    date_str = _format_date(debut)
    time_range = f"{_format_time(debut)}-{_format_time(fin)}"

    if change_type == "add":
        titre = _clean_course_name(new["titre"])
        return ("Nouveau cours ajouté", f"{titre} le {date_str} {time_range}")

    if change_type == "cancel":
        titre = _clean_course_name(old["titre"])
        return ("Cours annulé", f"{titre} le {date_str} {time_range}")

    # replace: distinguish a pure room/professor change from a genuinely
    # different course, for a more precise message.
    old_titre_clean = _clean_course_name(old["titre"])
    new_titre_clean = _clean_course_name(new["titre"])

    if old_titre_clean != new_titre_clean:
        return ("Emploi du temps modifié", f"{old_titre_clean} remplacé par {new_titre_clean} ({date_str} {time_range})")

    if old["salle"] != new["salle"]:
        return ("Salle changée", f"{new_titre_clean} : {old['salle'] or '?'} → {new['salle'] or '?'} ({date_str} {time_range})")

    if old["professeur"] != new["professeur"]:
        return ("Professeur changé", f"{new_titre_clean} : {old['professeur'] or '?'} → {new['professeur'] or '?'}")

    return ("Emploi du temps modifié", f"{new_titre_clean} a été modifié ({date_str} {time_range})")


def _persist_and_notify_changes(
    db,
    user: User,
    changes: List[dict],
    previously_known_range: Optional[tuple] = None,
) -> None:
    """
    Persists each change as a ScheduleChange row (powers the app's Logs
    screen) and sends one dedicated push notification per change - so the
    app can open directly on the affected day and highlight that specific
    event, instead of one aggregated "something changed" message.

    "add" changes are only pushed if they land inside previously_known_range
    (the range already synced *before* this sync) - a genuinely new course
    appearing in an already-tracked week. Otherwise it's routine initial
    population of a date range being synced for the first time, which would
    otherwise fire one "add" notification per course (potentially dozens at
    once). Every change is still logged either way, just not always pushed.
    """
    for change in changes:
        change_type = change["type"]
        old = change["old"]
        new = change["new"]
        debut = change["debut"]
        fin = (new or old)["fin"]

        event_id = None
        if new:
            event_row = db.query(Event).filter(
                Event.eseo_id == user.eseo_id,
                Event.debut == debut,
            ).first()
            event_id = event_row.id if event_row else None

        db.add(ScheduleChange(
            eseo_id=user.eseo_id,
            change_type=change_type,
            event_id=event_id,
            debut=debut,
            fin=fin,
            old_titre=old["titre"] if old else None,
            old_salle=old["salle"] if old else None,
            old_professeur=old["professeur"] if old else None,
            new_titre=new["titre"] if new else None,
            new_salle=new["salle"] if new else None,
            new_professeur=new["professeur"] if new else None,
        ))

        should_notify = bool(user.device_token)

        if should_notify and change_type == "add":
            debut_aware = debut if debut.tzinfo is not None else debut.replace(tzinfo=timezone.utc)
            in_known_range = (
                previously_known_range is not None
                and previously_known_range[0] <= debut_aware <= previously_known_range[1]
            )
            if not in_known_range:
                should_notify = False

        if should_notify:
            title, body = _format_notification(change_type, debut, fin, old, new)
            send_firebase_notification(
                user.device_token,
                title,
                body,
                data={
                    "change_type": change_type,
                    "target_date": _to_paris(debut).date().isoformat(),
                    "event_id": str(event_id) if event_id else "",
                    "old_titre": old["titre"] if old else "",
                    "old_salle": (old["salle"] or "") if old else "",
                    "new_titre": new["titre"] if new else "",
                    "new_salle": (new["salle"] or "") if new else "",
                },
            )

    db.commit()


def _notify_if_changed(
    db,
    user: User,
    start_datetime_utc: datetime,
    end_datetime_utc: datetime,
    old_hash: str,
    old_events_map: dict,
    previously_known_range: Optional[tuple] = None,
) -> str:
    """
    Compares the user's current DB state to a pre-sync snapshot (see
    _capture_pre_sync_state), and if it changed, diffs it into individual
    changes (see _diff_events) and persists+notifies each one (see
    _persist_and_notify_changes). Returns the new hash so the caller can
    persist it on User.current_schedule_hash.
    """
    new_hash = calculate_schedule_hash_for_range(db, user.eseo_id, start_datetime_utc, end_datetime_utc)

    if new_hash != old_hash:
        print(f"Schedule changed for user {user.eseo_id}")
        current_events = db.query(Event).filter(
            Event.eseo_id == user.eseo_id,
            Event.debut >= start_datetime_utc,
            Event.debut <= end_datetime_utc
        ).all()
        current_events_map = {
            event.debut: {
                "titre": event.titre,
                "salle": event.salle,
                "professeur": event.professeur,
                "fin": event.fin,
            }
            for event in current_events
        }
        changes = _diff_events(old_events_map, current_events_map)
        if changes:
            _persist_and_notify_changes(db, user, changes, previously_known_range)

    return new_hash


async def sync_user_schedule(
    eseo_id: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    debounce_seconds: float = 0,
    force: bool = False,
) -> bool:
    """
    Core sync for one user: debounce check -> fetch from ESEO -> upsert this
    user's own events -> propagate groupe-tagged events to every other user
    sharing that groupe -> diff & notify everyone affected -> mark checked ->
    update this user's last_sync/synced_range.

    Args:
        eseo_id: User's ESEO ID
        start_date/end_date: Range to sync. Defaults to today .. today +
            this user's own sync_range weeks (the scheduled job's usual range).
        debounce_seconds: Skip the sync if this user's own key AND every
            groupe they belong to were already checked within this window
            (see sync_coordination). Ignored if force=True.
        force: Bypass the debounce check entirely (e.g. cache-miss on /agenda,
            where a fetch must happen regardless of recent activity).

    Returns:
        True if a sync actually ran, False if skipped (debounce) or failed.
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.eseo_id == eseo_id).first()
        if not user:
            return False

        if start_date is None or end_date is None:
            start_date = datetime.now(PARIS_TZ).date()
            end_date = start_date + timedelta(weeks=user.sync_range)

        start_datetime_utc = PARIS_TZ.localize(
            datetime.combine(start_date, datetime.min.time())
        ).astimezone(timezone.utc)
        end_datetime_utc = PARIS_TZ.localize(
            datetime.combine(end_date, datetime.max.time())
        ).astimezone(timezone.utc)

        known_groupes = [
            row[0] for row in db.query(Event.groupe).filter(
                Event.eseo_id == eseo_id, Event.groupe.isnot(None)
            ).distinct().all()
        ]
        user_key = sync_coordination.user_key(eseo_id)

        if not force and not sync_coordination.should_sync(user_key, known_groupes, debounce_seconds):
            return False

        old_hash, old_events_map = _capture_pre_sync_state(db, eseo_id, start_datetime_utc, end_datetime_utc)

        raw_events = await ESEOScraper.fetch_schedule_async(
            str(eseo_id), start_date.isoformat(), end_date.isoformat()
        )

        if raw_events is None:
            print(f"Failed to fetch schedule for user {eseo_id}")
            return False

        # Lazy import: main.py imports start_scheduler/stop_scheduler/sync_user_schedule
        # from this module at load time, so importing main.py back at module
        # level here would be circular.
        from main import upsert_events, propagate_group_events, _extend_synced_range, _previously_known_range

        # Must be read before _extend_synced_range() below updates it.
        source_known_range = _previously_known_range(user)

        await upsert_events(db, eseo_id, raw_events, start_datetime_utc, end_datetime_utc)
        affected_targets = propagate_group_events(db, eseo_id, raw_events, start_datetime_utc, end_datetime_utc)

        new_hash = _notify_if_changed(
            db, user, start_datetime_utc, end_datetime_utc, old_hash, old_events_map,
            previously_known_range=source_known_range,
        )
        user.current_schedule_hash = new_hash

        if affected_targets:
            target_users = db.query(User).filter(User.eseo_id.in_(affected_targets.keys())).all()
            for target_user in target_users:
                target_old_hash, target_old_map = affected_targets[target_user.eseo_id]
                target_new_hash = _notify_if_changed(
                    db, target_user, start_datetime_utc, end_datetime_utc,
                    target_old_hash, target_old_map,
                    previously_known_range=_previously_known_range(target_user),
                )
                target_user.current_schedule_hash = target_new_hash

        # Re-read groupes: the fetch may have introduced/removed some.
        updated_groupes = [
            row[0] for row in db.query(Event.groupe).filter(
                Event.eseo_id == eseo_id, Event.groupe.isnot(None)
            ).distinct().all()
        ]
        sync_coordination.mark_checked(user_key, updated_groupes)

        _extend_synced_range(user, start_datetime_utc, end_datetime_utc)
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


async def maybe_sync_and_notify(eseo_id: int, start_date_iso: str, end_date_iso: str) -> None:
    """
    App-driven sync trigger: called as a FastAPI BackgroundTask from /agenda
    on every call (cache hit or not), so it never blocks the response. Debounced
    to 5 minutes per user/groupe (see sync_coordination) so a burst of app
    opens from the same class doesn't hammer the ESEO API.

    Must never raise - it runs after the HTTP response has already been sent.
    """
    try:
        start_date = date.fromisoformat(start_date_iso)
        end_date = date.fromisoformat(end_date_iso)
        await sync_user_schedule(eseo_id, start_date, end_date, debounce_seconds=300)
    except Exception as e:
        print(f"Error in background sync for user {eseo_id}: {e}")


def sync_all_users():
    """
    Safety-net sync for all users, called every 3 hours (7 AM - 7 PM).

    Most real syncing now happens on-demand when the app opens/resumes
    (see maybe_sync_and_notify), debounced per user/groupe. This job just
    guarantees eventual detection even if nobody opens the app for a while -
    it skips any user/groupe already checked within the last 30 minutes.
    """
    print(f"[{datetime.now().isoformat()}] Starting schedule sync for all users")

    db = SessionLocal()
    try:
        users = db.query(User).all()
        total_users = len(users)

        if total_users == 0:
            print("No users to sync")
            return

        print(f"Syncing schedules for {total_users} users")

        synced_count = 0
        skipped_count = 0

        for index, user in enumerate(users, 1):
            print(f"Processing user {index}/{total_users}: {user.eseo_id} (sync_range={user.sync_range} weeks)")

            did_sync = asyncio.run(sync_user_schedule(user.eseo_id, debounce_seconds=1800))

            if did_sync:
                synced_count += 1
            else:
                skipped_count += 1

            # Rate limiting: 2 seconds between requests
            if index < total_users:
                time.sleep(2)

        print(f"Sync completed: {synced_count} synced, {skipped_count} skipped (debounced or failed)")

    except Exception as e:
        print(f"Error in sync_all_users: {e}")
    finally:
        db.close()


def purge_old_events():
    """
    Purge events older than 6 months, and logged schedule changes older than
    90 days, to prevent DB bloat. Runs once per day at 2 AM.
    """
    print(f"[{datetime.now().isoformat()}] Starting old events purge...")

    db = SessionLocal()
    try:
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=180)  # 6 months
        deleted = db.query(Event).filter(Event.fin < cutoff_date).delete()

        changes_cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        deleted_changes = db.query(ScheduleChange).filter(ScheduleChange.created_at < changes_cutoff).delete()

        db.commit()

        print(f"Purged {deleted} events older than {cutoff_date.date()} "
              f"and {deleted_changes} logged changes older than {changes_cutoff.date()}")

    except Exception as e:
        print(f"Error purging old events: {e}")
        db.rollback()
    finally:
        db.close()


def start_scheduler():
    """
    Start the background scheduler.

    sync_all_users is now just a safety net (see its docstring) - most real
    syncing happens on-demand via maybe_sync_and_notify when the app
    opens/resumes, so this only needs to run every 3 hours instead of hourly.
    Runs purge_old_events daily at 2 AM.
    """
    if scheduler.running:
        print("Scheduler already running")
        return

    # Safety-net sync every 3 hours between 7 AM and 7 PM (7h, 10h, 13h, 16h, 19h)
    scheduler.add_job(
        func=sync_all_users,
        trigger=CronTrigger(hour="7-19/3", minute=0),
        id="schedule_sync",
        name="Synchronize all user schedules (safety net)",
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
    print("Scheduler started: Sync every 3h 7AM-7PM (safety net), Purge daily 2AM")


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
