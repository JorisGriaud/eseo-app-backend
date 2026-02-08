"""
Database migration script
Migrates from old Schedule model (JSON storage) to new Event model (normalized table)
"""
from database import Base, engine, SessionLocal
from sqlalchemy import text


def migrate():
    """
    Migrate database schema from schedules to events

    Steps:
        1. Drop old schedules table
        2. Create new events table with Event model
        3. Reset user schedule hashes

    Note: This is a one-time migration that starts fresh.
          Old schedule data is discarded (as per user decision).
    """
    print("[MIGRATION] Starting database migration from schedules to events...")
    print(f"[DATABASE] data/edt_app.db\n")

    db = SessionLocal()

    try:
        # Step 1: Drop old schedules table
        print("Step 1: Dropping old schedules table...")
        db.execute(text("DROP TABLE IF EXISTS schedules"))
        db.commit()
        print("[OK] Old schedules table dropped")

        # Step 2: Create new events table
        print("\nStep 2: Creating new events table...")
        Base.metadata.create_all(bind=engine)
        print("[OK] New events table created with indexes and constraints")

        # Step 3: Reset user schedule hashes
        print("\nStep 3: Resetting user schedule hashes...")
        result = db.execute(text("UPDATE users SET current_schedule_hash = NULL"))
        db.commit()
        print(f"[OK] Reset schedule hashes for all users")

        print("\n" + "="*60)
        print("[OK] Migration complete!")
        print("="*60)
        print("\nNext steps:")
        print("1. Restart the application")
        print("2. The scheduler will populate the events table on the next sync")
        print("3. Test the new /agenda endpoint")
        print("\nOld endpoints /schedule and /schedule/refresh should be removed from code.")

        return True

    except Exception as e:
        print(f"\n[ERROR] Migration failed: {e}")
        db.rollback()
        return False
    finally:
        db.close()


if __name__ == "__main__":
    success = migrate()

    if not success:
        print("\n[WARNING]  Migration failed. Please check the error messages above.")
        print("Database backup is recommended before re-attempting.")
        exit(1)
