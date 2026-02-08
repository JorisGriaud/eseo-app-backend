"""
Database migration script
Adds sync_range column to users table
"""
import sqlite3
import os

DB_PATH = "data/edt_app.db"

def migrate():
    """Add sync_range column to users table"""
    if not os.path.exists(DB_PATH):
        print(f"❌ Database not found at {DB_PATH}")
        return False

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Check if column already exists
        cursor.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]

        if 'sync_range' in columns:
            print("✅ Column sync_range already exists in users table")
            conn.close()
            return True

        # Add sync_range column
        print("Adding sync_range column to users table...")
        cursor.execute("ALTER TABLE users ADD COLUMN sync_range INTEGER NOT NULL DEFAULT 4")
        conn.commit()

        # Verify the column was added
        cursor.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]

        if 'sync_range' in columns:
            print("✅ Migration successful! sync_range column added with default value 4")

            # Show current users and their sync_range
            cursor.execute("SELECT eseo_id, sync_range FROM users")
            users = cursor.fetchall()
            if users:
                print(f"\n📊 Current users ({len(users)} total):")
                for eseo_id, sync_range in users[:5]:  # Show first 5
                    print(f"   - User {eseo_id}: sync_range={sync_range}")
                if len(users) > 5:
                    print(f"   ... and {len(users) - 5} more users")
            else:
                print("\n📊 No users in database yet")

            conn.close()
            return True
        else:
            print("❌ Migration failed - column not found after ALTER TABLE")
            conn.close()
            return False

    except sqlite3.OperationalError as e:
        print(f"❌ SQLite error: {e}")
        return False
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return False

if __name__ == "__main__":
    print("🔧 Starting database migration...")
    print(f"📁 Database: {DB_PATH}\n")

    success = migrate()

    if success:
        print("\n✅ Migration complete! You can now restart the application.")
    else:
        print("\n❌ Migration failed. Please check the error messages above.")
