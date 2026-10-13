from flask import Flask, request, jsonify, render_template, session, redirect, url_for
import sqlite3
from functools import wraps
from datetime import datetime, timedelta

app = Flask(__name__)
DB_PATH = r"database\network_noc.db"

# Session cookies need a secret key to be signed with. This is a fixed string
# (not os.urandom()) specifically so restarting the Flask dev server's
# auto-reloader during development doesn't silently invalidate every open
# session and log everyone out. For a real deployment with real accounts,
# this should come from an environment variable instead of being checked in.
app.secret_key = 'noc-dashboard-dev-secret-change-if-deployed'

# Hardcoded single admin account. This is only appropriate because this app
# is a local simulation/demo tool with no real customer data behind it --
# it is NOT how you'd gate anything with real credentials or real data
# (no password hashing, no per-user accounts, no rate limiting on attempts).
ADMIN_USERNAME = 'admin'
ADMIN_PASSWORD = 'admin'

def login_required(f):
    """For routes that render a full HTML page (like '/'). Redirects to the
    login page itself, since that's what a browser navigating there expects."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def api_login_required(f):
    """For JSON API routes the page's own JavaScript calls in the background
    (dashboard polling, saving a ticket). A redirect would just hand back the
    login page's HTML where JSON was expected, breaking JSON.parse() -- a
    plain 401 lets the frontend notice and send the browser to /login itself."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return jsonify({"error": "Not authenticated"}), 401
        return f(*args, **kwargs)
    return decorated

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    # Read columns by NAME instead of position -- schema changes never require
    # re-counting indices in this file again.
    conn.row_factory = sqlite3.Row
    return conn

def safe_get(row, key, default=None):
    """Read a column by name if it exists on this row, otherwise return default."""
    try:
        value = row[key]
        return value if value is not None else default
    except (IndexError, KeyError):
        return default

# --- GPON topology helpers ---------------------------------------------------
# Your devices table already encodes the real fiber hierarchy via "type" and
# "parent_device" (Core -> OLT -> Splitter -> ONU). Instead of storing a
# separate "users_affected" or "pon" value that can drift out of sync, both
# are computed live from this tree every time the dashboard loads.

def build_topology(devices):
    """Return (by_name, children) maps built from the flat devices list."""
    by_name = {d["name"]: d for d in devices if d.get("name")}
    children = {}
    for d in devices:
        parent = d.get("parent_device")
        if parent and parent != "None":
            children.setdefault(parent, []).append(d["name"])
    return by_name, children

def collect_affected_onus(device_name, by_name, children, _seen=None):
    """Return the SET of customer ONU device names downstream of device_name.
    Using a set (rather than just a count) lets callers safely union results
    from multiple simultaneously-down devices without double-counting an ONU
    that happens to sit under two of them at once."""
    if _seen is None:
        _seen = set()
    if device_name in _seen:
        return set()  # guards against any accidental cyclic parent_device data
    _seen.add(device_name)

    device = by_name.get(device_name)
    if not device:
        return set()
    if device["type"] == "ONU":
        return {device_name}

    result = set()
    for child_name in children.get(device_name, []):
        child = by_name.get(child_name)
        if not child:
            continue
        if child["type"] == "ONU":
            result.add(child_name)
        else:
            result |= collect_affected_onus(child_name, by_name, children, _seen)
    return result

def count_affected_users(device_name, by_name, children):
    """Count customer ONUs downstream of device_name. Each ONU = 1 subscriber."""
    if not by_name.get(device_name):
        return None
    return len(collect_affected_onus(device_name, by_name, children))

def get_pon_path(device_name, by_name):
    """Describe which OLT / splitter branch (PON) this device sits on, e.g.
    'OLT_Zone_Okhla -> Splitter_O1'. Core devices aren't on a PON segment."""
    device = by_name.get(device_name)
    if not device:
        return None
    if device["type"] == "Core":
        return "N/A (Core network)"
    if device["type"] == "OLT":
        return device_name

    chain = []
    current = device
    guard = 0
    while current and guard < 20:
        guard += 1
        parent_name = current.get("parent_device")
        if not parent_name or parent_name == "None":
            break
        parent = by_name.get(parent_name)
        if not parent:
            break
        chain.append(parent_name)
        if parent["type"] == "OLT":
            break
        current = parent
    chain.reverse()
    return " -> ".join(chain) if chain else "N/A"

def compute_realtime_impact(devices, by_name):
    """True live count, independent of ticket bookkeeping: a customer ONU is
    'affected right now' if it, or anything upstream of it (splitter, OLT,
    core), currently has status OFFLINE. Walking the actual live device
    statuses avoids double-counting when multiple devices on the same
    branch are down at once."""
    affected = 0
    total_customers = 0
    for d in devices:
        if d["type"] != "ONU":
            continue
        total_customers += 1

        node = d
        seen = set()
        is_down = False
        while node and node["name"] not in seen:
            seen.add(node["name"])
            if (node.get("status") or "").upper() == "OFFLINE":
                is_down = True
                break
            parent_name = node.get("parent_device")
            if not parent_name or parent_name == "None":
                break
            node = by_name.get(parent_name)
        if is_down:
            affected += 1

    return affected, total_customers

def get_zone(device_name, by_name):
    """Return the OLT (zone) name a device belongs to, for the Zone filter.
    The OLT itself returns its own name; Core/POP devices return None
    (they're network-wide, not scoped to a single zone)."""
    device = by_name.get(device_name)
    if not device:
        return None
    if device["type"] == "Core":
        return None
    if device["type"] == "OLT":
        return device_name

    current = device
    guard = 0
    while current and guard < 20:
        guard += 1
        parent_name = current.get("parent_device")
        if not parent_name or parent_name == "None":
            return None
        parent = by_name.get(parent_name)
        if not parent:
            return None
        if parent["type"] == "OLT":
            return parent_name
        current = parent
    return None

def get_maintenance_covered_devices(cursor, children):
    """Returns a set of every device name currently covered by an active
    maintenance window: the window's target device plus everything
    downstream of it (scheduling maintenance on an OLT also covers its
    splitters and customer ONUs -- you shouldn't get separate tickets for
    every device beneath planned work)."""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    rows = cursor.execute(
        "SELECT device_name FROM maintenance_windows WHERE cancelled_at IS NULL AND (ends_at IS NULL OR ends_at > ?)",
        (now_str,)
    ).fetchall()

    covered = set()
    def collect_descendants(name, seen):
        if name in seen:
            return
        seen.add(name)
        covered.add(name)
        for child_name in children.get(name, []):
            collect_descendants(child_name, seen)

    for r in rows:
        target = safe_get(r, "device_name")
        if target:
            collect_descendants(target, set())
    return covered

def compute_all_device_stats(devices, by_name, children):
    """For every device, compute (total, online, offline) customer counts
    across its full subtree in one O(N) bottom-up pass (memoized per name).
    Re-walking each device's subtree independently would be O(N^2) and far
    too slow once the network has thousands of ONUs -- this is what makes
    clicking on ANY device (not just ones with open tickets) show real,
    accurate numbers instead of a made-up formula."""
    stats = {}

    def visit(name):
        if name in stats:
            return stats[name]
        node = by_name.get(name)
        if not node:
            return (0, 0, 0)

        if node["type"] == "ONU":
            is_offline = (node.get("status") or "").upper() == "OFFLINE"
            result = (1, 0 if is_offline else 1, 1 if is_offline else 0)
            stats[name] = result
            return result

        total = online = offline = 0
        node_is_offline = (node.get("status") or "").upper() == "OFFLINE"
        for child_name in children.get(name, []):
            c_total, c_online, c_offline = visit(child_name)
            total += c_total
            if node_is_offline:
                # This device itself being down takes out everything beneath
                # it, regardless of what each child individually reports.
                offline += c_total
            else:
                online += c_online
                offline += c_offline

        result = (total, online, offline)
        stats[name] = result
        return result

    for d in devices:
        visit(d["name"])

    return stats

def _build_dashboard_context(cursor, conn):
    """Computes everything the dashboard needs (devices w/ real stats, open
    tickets, chart history, etc.) in one place. Used by BOTH the initial
    page render (/) and the AJAX refresh endpoint (/api/dashboard_data) --
    having one shared function means the periodic refresh can never drift
    out of sync with what a full page load would show."""
    # 1. Fetch devices
    raw_devices = cursor.execute("SELECT * FROM devices").fetchall()
    devices = []
    device_lookup = {}  # lowercased name -> device dict, for the IP join below
    for row in raw_devices:
        device = {
            "ip": safe_get(row, "ip"),
            "name": safe_get(row, "name"),
            "type": safe_get(row, "type"),
            "parent_device": safe_get(row, "parent_device"),
            "status": safe_get(row, "status"),
            "latency": safe_get(row, "latency")
        }
        devices.append(device)
        if device["name"]:
            device_lookup[device["name"].strip().lower()] = device

    # Topology maps, keyed by exact device name, for the PON/user-count helpers
    by_name, children = build_topology(devices)

    # Real per-device stats (total/online/offline customers under each device)
    # computed once for the whole tree, so clicking ANY device -- not just
    # ones with open tickets -- shows real numbers instead of a formula.
    device_stats = compute_all_device_stats(devices, by_name, children)
    maintenance_covered = get_maintenance_covered_devices(cursor, children)
    zones = set()
    for device in devices:
        total, online, offline = device_stats.get(device["name"], (0, 0, 0))
        device["total_users"] = total
        device["online_users"] = online
        device["offline_users"] = offline
        device["under_maintenance"] = device["name"] in maintenance_covered
        zone = get_zone(device["name"], by_name)
        device["zone"] = zone
        if zone:
            zones.add(zone)
    zone_list = sorted(zones)

    # Live counts for the Analytics graphs -- computed fresh from actual
    # device statuses every load, not hardcoded demo numbers.
    users_affected_now, total_customers = compute_realtime_impact(devices, by_name)

    # 2. Fetch tickets
    raw_tickets = cursor.execute("SELECT * FROM tickets WHERE status = 'OPEN'").fetchall()
    tickets = []
    for t in raw_tickets:
        device_name = safe_get(t, "device_name")
        matched_device = device_lookup.get((device_name or "").strip().lower(), {})
        real_name = matched_device.get("name")  # exact-case name, for topology lookups

        summary_text = safe_get(t, "summary")

        tickets.append({
            "id": t["id"],
            "device_name": device_name,
            # Schema has no "timestamp" column -- created_at is the real down-since time.
            "timestamp": safe_get(t, "created_at"),

            "ip": matched_device.get("ip"),
            "pon": get_pon_path(real_name, by_name) if real_name else None,
            "users_affected": count_affected_users(real_name, by_name, children) if real_name else None,

            # Set once, when the ticket was opened (see compute_severity()) --
            # not recomputed here, so it reflects the real impact at the time
            # of the outage even if the topology or device counts change later.
            "severity": safe_get(t, "severity", "WARNING"),

            # For the Type/Zone filters on the Alerts page and ticket sidebar.
            "device_type": matched_device.get("type"),
            "zone": get_zone(real_name, by_name) if real_name else None,

            "summary": summary_text,
            "sla_target": safe_get(t, "sla_target"),
            "created_at": safe_get(t, "created_at"),

            # Editable fields set by the NOC engineer from the ticket modal.
            "etr": safe_get(t, "etr", ""),
            "note_partner": safe_get(t, "note_partner", ""),
            "note_company_us": safe_get(t, "note_company_us", ""),
            "note_public": safe_get(t, "note_public", ""),

            # Acknowledgement -- distinct from resolved: "someone's on it" vs "it's fixed".
            "acknowledged": bool(safe_get(t, "acknowledged", 0)),
            "acknowledged_by": safe_get(t, "acknowledged_by", ""),
            "acknowledged_at": safe_get(t, "acknowledged_at", ""),

            # Feeds the "notes" line in the Alert Details modal on the frontend.
            "notes": summary_text,
        })

    # 3. Calculate alerts
    try:
        total_alerts = cursor.execute("SELECT COUNT(*) FROM syslogs WHERE level = 'CRITICAL'").fetchone()[0]
    except Exception:
        total_alerts = 0

    # 4. Real-time metrics for the Analytics page. Uses the same accurate,
    # non-double-counting live walk as the donut/line charts (rather than
    # summing each ticket's own users_affected, which could double-count
    # a customer sitting under two simultaneously-open tickets).
    total_users_affected = users_affected_now

    online_latencies = [
        d["latency"] for d in devices
        if (d.get("status") or "").upper() == "ONLINE" and d.get("latency") is not None
    ]
    avg_latency = round(sum(online_latencies) / len(online_latencies), 2) if online_latencies else 0.0

    current_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        cursor.execute(
            "INSERT INTO metrics_history (timestamp, users_affected, avg_latency) VALUES (?, ?, ?)",
            (current_time_str, total_users_affected, avg_latency)
        )
        # Keep the table small -- only the most recent 30 snapshots are ever charted.
        cursor.execute("""
            DELETE FROM metrics_history WHERE id NOT IN (
                SELECT id FROM metrics_history ORDER BY id DESC LIMIT 30
            )
        """)
        conn.commit()
    except Exception as e:
        print(f"Metrics snapshot error: {e}")

    try:
        raw_history = cursor.execute(
            "SELECT timestamp, users_affected, avg_latency FROM metrics_history ORDER BY id ASC"
        ).fetchall()
    except Exception:
        raw_history = []

    chart_data = {"labels": [], "users_online": [], "users_affected": [], "avg_latency": []}
    for row in raw_history:
        ts = safe_get(row, "timestamp", "")
        label = ts.split(" ")[-1] if ts else ""  # just the HH:MM:SS part
        affected = safe_get(row, "users_affected", 0) or 0
        chart_data["labels"].append(label)
        chart_data["users_affected"].append(affected)
        chart_data["users_online"].append(max(total_customers - affected, 0))
        chart_data["avg_latency"].append(safe_get(row, "avg_latency", 0.0) or 0.0)

    # Active maintenance windows, for the "Scheduled Maintenance" list in the UI.
    try:
        raw_windows = cursor.execute("""
            SELECT * FROM maintenance_windows
            WHERE cancelled_at IS NULL AND (ends_at IS NULL OR ends_at > ?)
            ORDER BY created_at DESC
        """, (current_time_str,)).fetchall()
        maintenance_windows = [{
            "id": safe_get(w, "id"),
            "device_name": safe_get(w, "device_name"),
            "reason": safe_get(w, "reason"),
            "created_by": safe_get(w, "created_by"),
            "created_at": safe_get(w, "created_at"),
            "ends_at": safe_get(w, "ends_at"),
        } for w in raw_windows]
    except Exception:
        maintenance_windows = []

    return {
        "devices": devices,
        "tickets": tickets,
        "total_alerts": total_alerts,
        "active_tickets": len(tickets),
        "total_users_affected": total_users_affected,
        "total_network_users": total_customers,
        "chart_data": chart_data,
        "zone_list": zone_list,
        "maintenance_windows": maintenance_windows,
    }

_EMPTY_DASHBOARD_CONTEXT = {
    "devices": [], "tickets": [], "total_alerts": 0, "active_tickets": 0,
    "total_users_affected": 0, "total_network_users": 0,
    "chart_data": {"labels": [], "users_online": [], "users_affected": [], "avg_latency": []},
    "zone_list": [], "maintenance_windows": [],
}

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('index'))
        error = 'Incorrect username or password.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        context = _build_dashboard_context(cursor, conn)
        return render_template('index.html', **context)

    except Exception as e:
        print(f"Route '/' Error: {e}")
        return render_template('index.html', **_EMPTY_DASHBOARD_CONTEXT)
    finally:
        if conn:
            conn.close()

@app.route('/api/dashboard_data', methods=['GET'])
@api_login_required
def get_dashboard_data():
    """Returns the same data the initial page render uses, as JSON. The
    frontend polls this every few seconds and patches the page in place --
    no full reload, no lost scroll position/filters/open modals."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        context = _build_dashboard_context(cursor, conn)
        return jsonify({"status": "success", **context})
    except Exception as e:
        print(f"Database Error: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/update_ticket', methods=['POST'])
@api_login_required
def update_ticket():
    data = request.get_json() or {}
    ticket_id = data.get('ticket_id')

    if not ticket_id:
        return jsonify({"error": "Missing ticket_id parameter"}), 400

    etr = data.get('etr', '')
    note_partner = data.get('note_partner', '')
    note_company_us = data.get('note_company_us', '')
    note_public = data.get('note_public', '')

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE tickets
               SET etr = ?, note_partner = ?, note_company_us = ?, note_public = ?
               WHERE id = ?""",
            (etr, note_partner, note_company_us, note_public, ticket_id)
        )
        conn.commit()
        return jsonify({"status": "success", "message": "Ticket updated."})
    except Exception as e:
        print(f"Database Error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/acknowledge_ticket', methods=['POST'])
@api_login_required
def acknowledge_ticket():
    """Toggles a ticket's acknowledged state. Acknowledged is deliberately
    separate from resolved -- it means "someone's actively on this", not
    "it's fixed". Toggling lets an engineer un-ack by mistake without losing
    the ticket, and re-ack later if needed."""
    data = request.get_json() or {}
    ticket_id = data.get('ticket_id')
    if not ticket_id:
        return jsonify({"error": "Missing ticket_id parameter"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        row = cursor.execute("SELECT acknowledged FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not row:
            return jsonify({"error": "Ticket not found"}), 404

        currently_acked = bool(safe_get(row, "acknowledged", 0))
        if currently_acked:
            cursor.execute(
                "UPDATE tickets SET acknowledged = 0, acknowledged_by = '', acknowledged_at = '' WHERE id = ?",
                (ticket_id,)
            )
            new_state = False
        else:
            username = session.get('username', 'admin')
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute(
                "UPDATE tickets SET acknowledged = 1, acknowledged_by = ?, acknowledged_at = ? WHERE id = ?",
                (username, now_str, ticket_id)
            )
            new_state = True

        conn.commit()
        return jsonify({"status": "success", "acknowledged": new_state})
    except Exception as e:
        print(f"Database Error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/maintenance/start', methods=['POST'])
@api_login_required
def start_maintenance():
    """Schedules a maintenance window for a device. While active, this device
    (and everything downstream of it) won't generate tickets/alerts if it
    goes offline -- see get_maintenance_covered_devices() and
    _apply_device_fault() above."""
    data = request.get_json() or {}
    device_name = (data.get('device_name') or '').strip()
    reason = (data.get('reason') or 'Scheduled maintenance').strip()
    duration_minutes = data.get('duration_minutes')  # None/omitted = indefinite

    if not device_name:
        return jsonify({"error": "Missing device_name parameter"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        exists = cursor.execute("SELECT 1 FROM devices WHERE name = ?", (device_name,)).fetchone()
        if not exists:
            return jsonify({"error": f"No such device: {device_name}"}), 404

        now = datetime.now()
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
        ends_at = None
        if duration_minutes:
            try:
                ends_at = (now + timedelta(minutes=float(duration_minutes))).strftime('%Y-%m-%d %H:%M:%S')
            except (TypeError, ValueError):
                ends_at = None

        username = session.get('username', 'admin')
        cursor.execute(
            """INSERT INTO maintenance_windows (device_name, reason, created_by, created_at, ends_at)
               VALUES (?, ?, ?, ?, ?)""",
            (device_name, reason, username, now_str, ends_at)
        )
        cursor.execute(
            "INSERT INTO syslogs (timestamp, level, message, device_name) VALUES (?, 'INFO', ?, ?)",
            (now_str, f"MAINTENANCE SCHEDULED: {device_name} by {username} -- {reason}", device_name)
        )
        conn.commit()
        return jsonify({"status": "success", "message": "Maintenance window started."})
    except Exception as e:
        print(f"Database Error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/maintenance/end', methods=['POST'])
@api_login_required
def end_maintenance():
    """Ends a maintenance window early (or on schedule, manually)."""
    data = request.get_json() or {}
    window_id = data.get('window_id')
    if not window_id:
        return jsonify({"error": "Missing window_id parameter"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(
            "UPDATE maintenance_windows SET cancelled_at = ? WHERE id = ? AND cancelled_at IS NULL",
            (now_str, window_id)
        )
        conn.commit()
        return jsonify({"status": "success", "message": "Maintenance window ended."})
    except Exception as e:
        print(f"Database Error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

# Severity is driven by real blast-radius impact (actual subscribers affected,
# from walking the topology), not by device type -- an OLT with only a couple
# customers on it isn't automatically "critical" just because it's an OLT, and
# a splitter serving 50 people genuinely is worse than a quiet one serving 2.
# Each tier also maps to a real SLA target, so severity isn't just a color.
SEVERITY_THRESHOLDS = [
    (100, 'CRITICAL', '1 Hour'),
    (20, 'MAJOR', '4 Hours'),
    (2, 'MINOR', '8 Hours'),
    (0, 'WARNING', '24 Hours'),
]

def compute_severity(users_affected):
    """Returns (severity, sla_target) for a given real subscriber impact count."""
    if users_affected is None:
        users_affected = 0
    for threshold, severity, sla in SEVERITY_THRESHOLDS:
        if users_affected >= threshold:
            return severity, sla
    return 'WARNING', '24 Hours'

def _apply_device_fault(cursor, device_name, status, description, current_time_str, maintenance_covered=None, by_name=None, children=None):
    """Apply one device's status change: update its row, and open/resolve its
    ticket + syslog entries as needed. Does NOT commit -- callers batch many
    of these into a single transaction (see report_fault and
    report_fault_bulk below), which is what makes bulk updates fast even
    with thousands of devices.

    maintenance_covered (optional set of device names) suppresses ticket/alert
    creation for devices under an active, planned maintenance window -- the
    device's status still updates normally, so Live Devices reflects reality,
    it just doesn't page anyone about work that was scheduled on purpose.

    by_name/children (optional topology maps, see build_topology()) let this
    compute the real blast-radius impact for severity/SLA when opening a
    ticket. Without them, new tickets fall back to WARNING/24h rather than
    guessing -- a real number is always preferred over an estimate."""
    status = (status or 'OFFLINE').upper().strip()
    description = description or 'Link state change detected.'
    device_name = (device_name or '').strip()
    maintenance_covered = maintenance_covered or set()

    if status == 'OFFLINE':
        latency_val = 0.0
    elif status == 'PASSIVE':
        latency_val = 0.0
    else:
        latency_val = 1.45

    # Plain "=" here (not LOWER(TRIM(name))=LOWER(TRIM(?))) so this can actually
    # use the index -- the name/device_name columns are COLLATE NOCASE, so
    # case-insensitive matching still works without wrapping the column in a
    # function and forcing a full table scan on every single update.
    cursor.execute(
        "UPDATE devices SET status = ?, latency = ? WHERE name = ?",
        (status, latency_val, device_name)
    )

    if status not in ('OFFLINE', 'ONLINE'):
        return

    cursor.execute(
        "SELECT id FROM tickets WHERE device_name = ? AND status = 'OPEN'",
        (device_name,)
    )
    existing = cursor.fetchone()

    if status == 'OFFLINE' and not existing:
        if device_name in maintenance_covered:
            # Expected outage -- log it quietly (INFO, not CRITICAL) so there's
            # still a record, but no ticket and no alert noise.
            cursor.execute(
                "INSERT INTO syslogs (timestamp, level, message, device_name) VALUES (?, 'INFO', ?, ?)",
                (current_time_str, f"MAINTENANCE: {device_name} down as part of a scheduled maintenance window.", device_name)
            )
            return

        users_affected = count_affected_users(device_name, by_name, children) if by_name and children else None
        severity, sla_target = compute_severity(users_affected)

        ticket_summary = f"Link Down: {device_name} -- {description}"
        cursor.execute(
            "INSERT INTO tickets (device_name, status, summary, sla_target, created_at, severity) VALUES (?, 'OPEN', ?, ?, ?, ?)",
            (device_name, ticket_summary, sla_target, current_time_str, severity)
        )
        cursor.execute(
            "INSERT INTO syslogs (timestamp, level, message, device_name) VALUES (?, 'CRITICAL', ?, ?)",
            (current_time_str, f"LINK CRITICAL: Interface down on asset {device_name}!", device_name)
        )
    elif status == 'ONLINE' and existing:
        cursor.execute(
            "UPDATE tickets SET status = 'RESOLVED' WHERE device_name = ? AND status = 'OPEN'",
            (device_name,)
        )
        cursor.execute(
            "INSERT INTO syslogs (timestamp, level, message, device_name) VALUES (?, 'INFO', ?, ?)",
            (current_time_str, f"LINK CLEAR: Interface restored on asset {device_name}.", device_name)
        )

# --- Monitor-facing endpoints ---
# These three routes (/api/device_list, /api/report_fault, /api/report_fault_bulk)
# are deliberately NOT behind login_required/api_login_required. They're called
# by pkt_monitor.py, a script running locally alongside app.py -- not by a
# browser user -- so there's no "user" to log in as here. If this ever needs
# to run somewhere pkt_monitor.py isn't trusted to reach the API directly
# (e.g. exposed beyond localhost), these would need their own service-level
# auth (e.g. a shared API key), separate from the admin/admin session login.

@app.route('/api/device_list', methods=['GET'])
def get_device_list():
    """Returns every device's name/ip/type. pkt_monitor.py fetches this at
    startup instead of hardcoding its own device list -- this is what
    prevents a repeat of the earlier Splitter_01 vs Splitter_O1 naming bug:
    the database is the single source of truth, always."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        rows = cursor.execute("SELECT name, ip, type FROM devices").fetchall()
        devices = [{"name": safe_get(r, "name"), "ip": safe_get(r, "ip"), "type": safe_get(r, "type")} for r in rows]
        return jsonify({"status": "success", "devices": devices})
    except Exception as e:
        print(f"Database Error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/report_fault', methods=['POST'])
def report_fault():
    data = request.get_json() or {}
    device_name = data.get('device_name')
    status = data.get('status', 'OFFLINE')
    description = data.get('description', 'Link state change detected.')

    if not device_name:
        return jsonify({"error": "Missing device_name parameter"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        current_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Build topology just enough to know which devices are under active
        # maintenance (a window on a parent covers everything beneath it too)
        # and to compute real severity/blast-radius if a new ticket opens.
        raw_devices = cursor.execute("SELECT name, parent_device, type FROM devices").fetchall()
        devices_min = [{"name": safe_get(r, "name"), "parent_device": safe_get(r, "parent_device"), "type": safe_get(r, "type")} for r in raw_devices]
        by_name, children = build_topology(devices_min)
        maintenance_covered = get_maintenance_covered_devices(cursor, children)

        _apply_device_fault(cursor, device_name, status, description, current_time_str, maintenance_covered, by_name, children)
        conn.commit()
        return jsonify({"status": "success", "message": "Telemetry updated cleanly."})

    except Exception as e:
        print(f"Database Error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

@app.route('/api/report_fault_bulk', methods=['POST'])
def report_fault_bulk():
    """Same as /api/report_fault, but applies a whole list of device updates
    in ONE database transaction. Essential at city scale: thousands of
    individual HTTP calls (one per device) every polling cycle would take
    far longer than the cycle itself and fall permanently behind. This
    endpoint lets pkt_monitor.py send everything in a single request."""
    data = request.get_json() or {}
    updates = data.get('updates', [])

    if not isinstance(updates, list) or not updates:
        return jsonify({"error": "Missing or empty 'updates' list"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        current_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Computed ONCE for the whole batch, not per device -- same reasoning
        # as everything else in this endpoint: avoid O(devices) extra queries.
        raw_devices = cursor.execute("SELECT name, parent_device, type FROM devices").fetchall()
        devices_min = [{"name": safe_get(r, "name"), "parent_device": safe_get(r, "parent_device"), "type": safe_get(r, "type")} for r in raw_devices]
        by_name, children = build_topology(devices_min)
        maintenance_covered = get_maintenance_covered_devices(cursor, children)

        applied = 0
        for item in updates:
            device_name = item.get('device_name')
            if not device_name:
                continue
            _apply_device_fault(
                cursor,
                device_name,
                item.get('status', 'OFFLINE'),
                item.get('description', 'Link state change detected.'),
                current_time_str,
                maintenance_covered,
                by_name,
                children
            )
            applied += 1

        conn.commit()
        return jsonify({"status": "success", "message": f"Telemetry updated for {applied} devices."})

    except Exception as e:
        print(f"Database Error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    app.run(debug=True, port=5000)