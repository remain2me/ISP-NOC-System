import time
import json
import requests
import os

API_BASE = "http://127.0.0.1:5000"
DEVICE_LIST_URL = f"{API_BASE}/api/device_list"
BULK_REPORT_URL = f"{API_BASE}/api/report_fault_bulk"
STATE_FILE = "network_state.json"


def fetch_device_list(max_retries=12, retry_delay=5):
    """Pull the real device list from the Flask app instead of hardcoding it
    here. This is the fix for the earlier Splitter_01/Splitter_O1 mismatch --
    the database (via init_db.py) is now the ONLY source of truth for device
    names, so this file can never drift out of sync with it again, no matter
    how large the network gets."""
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(DEVICE_LIST_URL, timeout=5)
            if response.status_code == 200:
                devices = response.json().get("devices", [])
                if devices:
                    print(f"Loaded {len(devices)} devices from {DEVICE_LIST_URL}")
                    return devices
            print(f"Unexpected response from device list endpoint: {response.status_code}")
        except Exception as e:
            print(f"Waiting for Flask server... ({attempt}/{max_retries}) {e}")
        time.sleep(retry_delay)

    raise RuntimeError(
        f"Could not fetch device list from {DEVICE_LIST_URL} after {max_retries} attempts. "
        "Make sure app.py is running first."
    )


def init_state_file(target_devices):
    if not os.path.exists(STATE_FILE):
        states = {device["name"]: "ONLINE" for device in target_devices}
        with open(STATE_FILE, "w") as f:
            json.dump(states, f, indent=4)
        print(f"Created {STATE_FILE} with {len(states)} devices - modify entries to trigger alerts!")


def check_devices(target_devices):
    try:
        with open(STATE_FILE, "r") as f:
            simulated_states = json.load(f)
    except Exception as e:
        print(f"Error reading {STATE_FILE}: {e}")
        return

    # Build the whole cycle's worth of updates, then send it as ONE request.
    # At city scale (thousands of devices), doing this one-at-a-time would take
    # far longer than the 5-second cycle itself and permanently fall behind.
    updates = []
    for device in target_devices:
        name = device["name"]

        # ---------------------------------------------------------
        # Passive splitters are never pinged directly -- they always
        # report PASSIVE, regardless of what's in network_state.json.
        # ---------------------------------------------------------
        if "Splitter" in name:
            current_status = "PASSIVE"
            description = "Passive Optical Device (No Ping Required)"
        else:
            current_status = simulated_states.get(name, "ONLINE")
            description = "Simulated state change" if current_status == "OFFLINE" else "Link restored"
        # ---------------------------------------------------------

        updates.append({
            "device_name": name,
            "status": current_status,
            "description": description
        })

    try:
        response = requests.post(BULK_REPORT_URL, json={"updates": updates}, timeout=15)
        if response.status_code != 200:
            print(f"Server rejected bulk update with status {response.status_code}: {response.text}")
        else:
            print(f"Bulk update applied: {response.json().get('message', 'ok')}")
    except Exception as e:
        print(f"API unreachable: {e}")


if __name__ == "__main__":
    print("Starting NOC Simulation Monitor...")
    target_devices = fetch_device_list()
    init_state_file(target_devices)
    while True:
        check_devices(target_devices)
        time.sleep(5)