import os
import psycopg2
from urllib.parse import urlparse

def fix_database():
    # Get database URL from environment variables
    DATABASE_URL = os.environ.get('DATABASE_URL')
    
    if not DATABASE_URL:
        print("DATABASE_URL not found")
        return

    # Parse the connection string
    result = urlparse(DATABASE_URL)
    
    try:
        # Connect to database
        conn = psycopg2.connect(
            database=result.path[1:],
            user=result.username,
            password=result.password,
            host=result.hostname,
            port=result.port
        )
        
        cursor = conn.cursor()
        
        # Change user_id to BIGINT
        cursor.execute('ALTER TABLE users ALTER COLUMN user_id TYPE BIGINT')
        print("✓ Changed users.user_id to BIGINT")
        
        conn.commit()
        print("✓ Database fixed successfully!")
        
    except Exception as e:
        print(f"✗ Error: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    fix_database()