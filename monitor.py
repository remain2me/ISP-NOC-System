import time
import subprocess
import sqlite3
import platform
from datetime import datetime

import os

# Dynamically calculates the absolute file path to your project root folder
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'database', 'network_noc.db')

import os
import json
import time
import sqlite3
from datetime import datetime

# Add this right below your imports
SIMULATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'simulation_state.json')

def ping_device(ip):
    """
    Simulates network hardware activity by checking the state of a local JSON file.
    This allows you to manually trigger 'cable cuts' during a live presentation.
    """
    # Auto-generate a 100% healthy network file if it doesn't exist yet
    if not os.path.exists(SIMULATION_FILE):
        default_state = {
            "192.168.100.1": "ONLINE", "192.168.100.2": "ONLINE",
            "192.168.1.10": "ONLINE",  "192.168.2.10": "ONLINE",
            "192.168.1.50": "ONLINE",  "192.168.1.51": "ONLINE", "192.168.2.50": "ONLINE",
            "192.168.1.101": "ONLINE", "192.168.1.102": "ONLINE", "192.168.2.103": "ONLINE"
        }
        with open(SIMULATION_FILE, 'w') as f:
            json.dump(default_state, f, indent=4)
            
    try:
        with open(SIMULATION_FILE, 'r') as f:
            states = json.load(f)
        
        status = states.get(ip, "OFFLINE")
        # Simulate a realistic fiber-optic latency (1-3ms)
        latency = 1.45 if status == "ONLINE" else 0.0
        return status, latency
    except Exception:
        return 'OFFLINE', 0.0

def get_upstream_failure(device_name, cursor):
    """
    RCA Engine: Recursively checks up the topology chain.
    If any parent device is already OFFLINE, returns that parent's name.
    """
    current_name = device_name
    while True:
        cursor.execute("SELECT parent_device FROM devices WHERE name = ?", (current_name,))
        row = cursor.fetchone()
        if not row or row[0] == 'None' or row[0] is None:
            break
            
        parent = row[0]
        cursor.execute("SELECT status FROM devices WHERE name = ?", (parent,))
        parent_row = cursor.fetchone()
        
        if parent_row and parent_row[0] == 'OFFLINE':
            return parent  # Found the root cause break upstream
        current_name = parent
        
    return None

def process_alerts_and_tickets():
    """
    Scans devices, performs pings, handles RCA suppression, 
    and updates statuses and active trouble tickets.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Fetch all registered architectural nodes
    cursor.execute("SELECT ip, name, type FROM devices")
    devices = cursor.fetchall()
    
    print(f"\n📡 Starting Live Network Scan Loop [{datetime.now().strftime('%H:%M:%S')}]")
    print("-" * 60)
    
    for ip, name, dev_type in devices:
        # 1. Run the live ping audit
        current_status, latency = ping_device(ip)
        
        # 2. Update the base metrics inside the device inventory table
        cursor.execute(
            "UPDATE devices SET status = ?, latency = ?, last_checked = ? WHERE name = ?",
            (current_status, latency, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), name)
        )
        
        if current_status == 'OFFLINE':
            # Check if this failure is just a cascade from an upstream blackout
            root_cause = get_upstream_failure(name, cursor)
            
            if root_cause:
                print(f"⚠️  {name} ({ip}) -> Down [SUPPRESSED] | Root Cause: Upstream node {root_cause} is down.")
                # Log to syslog but suppress opening an individual ticket
                log_msg = f"Device structural cascade failure. Outage bound to upstream root fault at {root_cause}."
                cursor.execute(
                    "INSERT INTO syslogs (timestamp, device_name, level, message) VALUES (?, ?, ?, ?)",
                    (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), name, 'WARNING', log_msg)
                )
            else:
                print(f"🚨 {name} ({ip}) -> OFFLINE | Generating Root Cause Incident Ticket!")
                
                # Check if an active ticket already exists for this root asset fault
                cursor.execute("SELECT id FROM tickets WHERE device_name = ? AND status = 'OPEN'", (name,))
                if not cursor.fetchone():
                    # Generate a clean system ticket with an operational SLA
                    sla_target = "4 Hours" if dev_type == 'Core' else "8 Hours"
                    summary = f"Critical link interruption detected on {name} ({dev_type} Node)."
                    
                    cursor.execute(
                        "INSERT INTO tickets (device_name, summary, status, sla_target, created_at) VALUES (?, ?, 'OPEN', ?, ?)",
                        (name, summary, sla_target, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    )
                    
                    # Cascade right into the central system log feed
                    cursor.execute(
                        "INSERT INTO syslogs (timestamp, device_name, level, message) VALUES (?, ?, ?, ?)",
                        (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), name, 'CRITICAL', f"Alarms raised. Ticket auto-provisioned due to direct fault code path.")
                    )
        else:
            print(f"🟢 {name} ({ip}) -> Online | Latency: {latency}ms")
            
            # If the device came back online, automatically close outstanding tickets
            cursor.execute("UPDATE tickets SET status = 'CLOSED' WHERE device_name = ? AND status = 'OPEN'", (name,))
            
    conn.commit()
    conn.close()

if __name__ == '__main__':
    print("🚀 Starting ISP Advanced Monitoring Engine Daemon...")
    print("Press Ctrl+C to terminate runtime execution loop safely.")
    
    while True:
        try:
            process_alerts_and_tickets()
            time.sleep(3)  # Audit cadence intervals
        except KeyboardInterrupt:
            print("\nShutting down Monitoring Daemon cleanly.")
            break
        except Exception as e:
            print(f"\nExecution anomaly encountered: {e}")
            time.sleep(5)