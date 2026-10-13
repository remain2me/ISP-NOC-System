import sqlite3
import os

# Dynamically resolve absolute path to ensure no duplicate/empty databases are created
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_FOLDER = os.path.join(BASE_DIR, 'database')
DB_PATH = os.path.join(DB_FOLDER, 'network_noc.db')

# ---------------------------------------------------------------------------
# NETWORK SCALE CONFIGURATION
# Tune these to grow/shrink the simulated city network. Defaults below build
# a real Delhi-wide GPON network across 5 regions / 25 zones:
#   1 core router + 5 regional POPs + 25 OLTs (one per zone)
#   + 25 * SPLITTERS_PER_OLT splitters + (splitters * ONUS_PER_SPLITTER) customers
# With the defaults (6 splitters/OLT, 24 customers/splitter) that's 3,600
# subscribers across ~3,781 total devices. Bump these up for an even bigger
# network -- just keep in mind the Live Devices page renders every device's
# row up front (filtered via the Type/Zone dropdowns client-side), so at
# extremely large scale (tens of thousands+) you'd eventually want the
# frontend to fetch rows on demand instead. For a few thousand, this is fine.
# ---------------------------------------------------------------------------
REGIONS = {
    "North_Delhi":   ["Rohini", "Pitampura", "Model_Town", "Civil_Lines", "Ashok_Vihar"],
    "South_Delhi":   ["Saket", "Vasant_Kunj", "Hauz_Khas", "Greater_Kailash", "Malviya_Nagar"],
    "East_Delhi":    ["Mayur_Vihar", "Preet_Vihar", "Laxmi_Nagar", "Shahdara", "Karkardooma"],
    "West_Delhi":    ["Janakpuri", "Dwarka", "Rajouri_Garden", "Tilak_Nagar", "Vikaspuri"],
    "Central_Delhi": ["Connaught_Place", "Karol_Bagh", "Paharganj", "Daryaganj", "Patel_Nagar"],
}
SPLITTERS_PER_OLT = 6
ONUS_PER_SPLITTER = 24

def generate_topology():
    """Programmatically build (ip, name, type, parent_device) tuples for a
    city-wide GPON network: Core -> Regional POP -> Zone OLT -> Splitter -> ONU.
    Fully systematic naming/addressing so it scales cleanly with the constants
    above, instead of hand-typed device tuples."""
    devices = []
    devices.append(("10.0.0.1", "ISP_Core_Router", "Core", "None"))

    for region_idx, (region_name, zones) in enumerate(REGIONS.items(), start=1):
        pop_name = f"POP_{region_name}"
        devices.append((f"10.{region_idx}.0.1", pop_name, "Core", "ISP_Core_Router"))

        for zone_idx, zone_name in enumerate(zones, start=1):
            olt_name = f"OLT_{zone_name}"
            devices.append((f"10.{region_idx}.{zone_idx}.1", olt_name, "OLT", pop_name))

            for splitter_i in range(1, SPLITTERS_PER_OLT + 1):
                splitter_name = f"Splitter_{zone_name}_{splitter_i:02d}"
                splitter_host = 10 + splitter_i
                devices.append((f"10.{region_idx}.{zone_idx}.{splitter_host}", splitter_name, "Splitter", olt_name))

                onu_base_host = 20 + (splitter_i - 1) * ONUS_PER_SPLITTER
                for onu_i in range(1, ONUS_PER_SPLITTER + 1):
                    onu_name = f"Customer_ONU_{zone_name}_{splitter_i:02d}_{onu_i:03d}"
                    onu_host = onu_base_host + onu_i
                    devices.append((f"10.{region_idx}.{zone_idx}.{onu_host}", onu_name, "ONU", splitter_name))

    return devices

def init_db():
    # Ensure the database directory exists
    os.makedirs(DB_FOLDER, exist_ok=True)

    print("Initializing SQLite Database tables...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Drop existing tables for a clean slate rebuild
    cursor.execute("DROP TABLE IF EXISTS devices")
    cursor.execute("DROP TABLE IF EXISTS tickets")
    cursor.execute("DROP TABLE IF EXISTS syslogs")
    cursor.execute("DROP TABLE IF EXISTS metrics_history")
    cursor.execute("DROP TABLE IF EXISTS maintenance_windows")

    # 1. Create Devices Table (Inventory & Status)
    cursor.execute('''
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT UNIQUE NOT NULL,
            name TEXT UNIQUE NOT NULL COLLATE NOCASE,
            type TEXT NOT NULL,
            parent_device TEXT NOT NULL,
            status TEXT DEFAULT 'UNKNOWN',
            latency REAL DEFAULT 0.0,
            last_checked TEXT
        )
    ''')
    # Indexes matter once this table has thousands of rows -- these back the
    # bulk update endpoint and the type/zone filters.
    cursor.execute("CREATE INDEX idx_devices_type ON devices(type)")
    cursor.execute("CREATE INDEX idx_devices_parent ON devices(parent_device)")

    # 2. Create Tickets Table (Incident Management)
    cursor.execute('''
        CREATE TABLE tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_name TEXT NOT NULL COLLATE NOCASE,
            summary TEXT NOT NULL,
            status TEXT DEFAULT 'OPEN',
            sla_target TEXT NOT NULL,
            created_at TEXT NOT NULL,
            etr TEXT DEFAULT '',
            note_partner TEXT DEFAULT '',
            note_company_us TEXT DEFAULT '',
            note_public TEXT DEFAULT '',
            severity TEXT DEFAULT 'WARNING',
            root_cause_ticket_id INTEGER DEFAULT NULL,
            acknowledged INTEGER DEFAULT 0,
            acknowledged_by TEXT DEFAULT '',
            acknowledged_at TEXT DEFAULT ''
        )
    ''')
    cursor.execute("CREATE INDEX idx_tickets_device_status ON tickets(device_name, status)")
    cursor.execute("CREATE INDEX idx_tickets_root_cause ON tickets(root_cause_ticket_id)")

    # 2b. Maintenance Windows -- suppresses ticket/alert creation for a device
    # (and everything downstream of it) while planned work is in progress.
    cursor.execute('''
        CREATE TABLE maintenance_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_name TEXT NOT NULL COLLATE NOCASE,
            reason TEXT NOT NULL,
            created_by TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            ends_at TEXT,        -- NULL = indefinite, until manually ended
            cancelled_at TEXT    -- NULL = still active (subject to ends_at)
        )
    ''')
    cursor.execute("CREATE INDEX idx_maintenance_device ON maintenance_windows(device_name)")

    # 3. Create Syslogs Table (Active Event Stream)
    cursor.execute('''
        CREATE TABLE syslogs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            device_name TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL
        )
    ''')

    # 4. Create Metrics History Table (powers the Analytics line/donut charts)
    cursor.execute('''
        CREATE TABLE metrics_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            users_affected INTEGER NOT NULL,
            avg_latency REAL DEFAULT 0.0
        )
    ''')

    seed_devices = generate_topology()
    cursor.executemany('''
        INSERT INTO devices (ip, name, type, parent_device)
        VALUES (?, ?, ?, ?)
    ''', seed_devices)

    conn.commit()
    conn.close()

    total = len(seed_devices)
    onu_count = sum(1 for d in seed_devices if d[2] == "ONU")
    splitter_count = sum(1 for d in seed_devices if d[2] == "Splitter")
    olt_count = sum(1 for d in seed_devices if d[2] == "OLT")
    print(f"Database initialized: {total} devices total")
    print(f"  {olt_count} OLTs (zones), {splitter_count} splitters, {onu_count} customer ONUs")
    print(f"Database location: {DB_PATH}")

if __name__ == '__main__':
    init_db()