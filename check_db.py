import sqlite3

def test_view():
    # Using the exact path shown in your terminal logs
    db_path = r"database\network_noc.db"
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT name, status, latency FROM devices")
        rows = cursor.fetchall()
        
        print("\n=== CURRENT DATABASE STATE ===")
        if not rows:
            print("The devices table is completely empty!")
        for row in rows:
            print(f"Device: '{row[0]}' | Status: {row[1]} | Latency: {row[2]}ms")
        print("==============================\n")
        
        conn.close()
    except Exception as e:
        print(f"❌ Error reading database: {e}")

if __name__ == "__main__":
    test_view()