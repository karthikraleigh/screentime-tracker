"""
Screen Time Tracker for Windows
--------------------------------
Runs a background thread that records which application is in the
foreground every few seconds (skipping time when the PC is idle), stores
the totals in a local SQLite database, and serves a small dashboard on
http://127.0.0.1:<port> (localhost only) so a parent can review usage.

Requires only the Python standard library. Windows only.
"""

import ctypes
from ctypes import wintypes
import sqlite3
import threading
import time
import datetime
import json
import http.server
import os
import sys
import shutil
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "screentime.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
FF_DB_COPY = os.path.join(CACHE_DIR, "places_copy.sqlite")

DEFAULT_CONFIG = {
    "poll_interval_seconds": 5,
    "idle_threshold_seconds": 60,
    "daily_limit_minutes": 120,
    "firefox_history_scan_seconds": 20,
    "port": 5757,
}


def load_config():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            merged.update(cfg)
            return merged
        except Exception:
            pass
    with open(CONFIG_PATH, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    return DEFAULT_CONFIG.copy()


CONFIG = load_config()
DB_LOCK = threading.Lock()

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_log (
            date TEXT NOT NULL,
            process_name TEXT NOT NULL,
            seconds INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (date, process_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS site_visits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            domain TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_date ON site_visits(date)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_meta(key, default=None):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return row[0]


def set_meta(key, value):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )
        conn.commit()
        conn.close()


def add_site_visit(date_str, time_str, domain, url, title):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO site_visits (date, time, domain, url, title) VALUES (?, ?, ?, ?, ?)",
            (date_str, time_str, domain, url, title),
        )
        conn.commit()
        conn.close()


def get_site_visits(date_str, limit=200):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT time, domain, url, title FROM site_visits
        WHERE date = ? ORDER BY time DESC LIMIT ?
        """,
        (date_str, limit),
    ).fetchall()
    conn.close()
    return rows


def get_top_domains(date_str, limit=25):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT domain, COUNT(*) as visits, MAX(time) as last_seen
        FROM site_visits WHERE date = ?
        GROUP BY domain ORDER BY visits DESC LIMIT ?
        """,
        (date_str, limit),
    ).fetchall()
    conn.close()
    return rows


def add_seconds(date_str, process_name, seconds):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """
            INSERT INTO usage_log (date, process_name, seconds)
            VALUES (?, ?, ?)
            ON CONFLICT(date, process_name)
            DO UPDATE SET seconds = seconds + excluded.seconds
            """,
            (date_str, process_name, seconds),
        )
        conn.commit()
        conn.close()


def get_day_totals(date_str):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT process_name, seconds FROM usage_log WHERE date = ? ORDER BY seconds DESC",
        (date_str,),
    ).fetchall()
    conn.close()
    return rows


def get_last_n_days(n):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT date, SUM(seconds) FROM usage_log
        GROUP BY date ORDER BY date DESC LIMIT ?
        """,
        (n,),
    ).fetchall()
    conn.close()
    return list(reversed(rows))


# --------------------------------------------------------------------------
# Windows API helpers (no third-party deps)
# --------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def get_idle_seconds():
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    user32.GetLastInputInfo(ctypes.byref(lii))
    millis = kernel32.GetTickCount() - lii.dwTime
    return millis / 1000.0


def get_foreground_process_name():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h_process:
        return None
    try:
        buf_len = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(260)
        ok = kernel32.QueryFullProcessImageNameW(h_process, 0, buf, ctypes.byref(buf_len))
        if ok:
            return os.path.basename(buf.value)
        return None
    finally:
        kernel32.CloseHandle(h_process)


# --------------------------------------------------------------------------
# Firefox browsing history
# --------------------------------------------------------------------------
# Firefox keeps its history in a SQLite file ("places.sqlite") inside the
# user's profile folder. Firefox holds that file open (and often in WAL
# mode) while running, so rather than reading it directly we copy it (plus
# its -wal/-shm sidecar files, if present) to a scratch location and read
# the copy. This is a common, non-invasive way to read history without a
# browser extension or debugging protocol.

EXCLUDED_URL_PREFIXES = ("about:", "moz-extension:", "place:", "chrome:", "resource:", "data:", "file:")


def find_firefox_profile_dir():
    # A Firefox install can have several profile folders (an old legacy
    # default, per-install defaults, etc.) and profiles.ini's notion of
    # "Default" is not always the one actually in use. The reliable signal
    # is simply: whichever profile's places.sqlite was written to most
    # recently is the one currently being browsed with.
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    profiles_root = os.path.join(appdata, "Mozilla", "Firefox", "Profiles")
    if not os.path.isdir(profiles_root):
        return None

    best_dir, best_mtime = None, -1
    for name in os.listdir(profiles_root):
        candidate = os.path.join(profiles_root, name)
        db = os.path.join(candidate, "places.sqlite")
        if os.path.isfile(db):
            mtime = os.path.getmtime(db)
            if mtime > best_mtime:
                best_dir, best_mtime = candidate, mtime
    return best_dir


def copy_places_db(profile_dir):
    src = os.path.join(profile_dir, "places.sqlite")
    if not os.path.isfile(src):
        return None
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        shutil.copy2(src, FF_DB_COPY)
        for ext in ("-wal", "-shm"):
            wal_src = src + ext
            wal_dst = FF_DB_COPY + ext
            if os.path.isfile(wal_src):
                shutil.copy2(wal_src, wal_dst)
            elif os.path.isfile(wal_dst):
                os.remove(wal_dst)  # avoid replaying a stale WAL against the new copy
        return FF_DB_COPY
    except OSError:
        return None


def domain_from_url(url):
    try:
        netloc = urlparse(url).netloc
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc or url
    except Exception:
        return url


def start_of_today_micros():
    today = datetime.date.today()
    start_dt = datetime.datetime(today.year, today.month, today.day)
    return int(start_dt.timestamp() * 1_000_000)


def scan_firefox_history():
    profile_dir = find_firefox_profile_dir()
    if not profile_dir:
        return
    db_path = copy_places_db(profile_dir)
    if not db_path:
        return

    last_id = get_meta("last_ff_visit_id", 0)
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            """
            SELECT v.id, p.url, p.title, v.visit_date
            FROM moz_historyvisits v
            JOIN moz_places p ON v.place_id = p.id
            WHERE v.id > ? AND v.visit_date >= ?
            ORDER BY v.id ASC
            LIMIT 500
            """,
            (last_id, start_of_today_micros()),
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        print("firefox history read error:", exc)
        return

    if not rows:
        return

    max_id = last_id
    for visit_id, url, title, visit_date_us in rows:
        max_id = max(max_id, visit_id)
        if not url or url.startswith(EXCLUDED_URL_PREFIXES):
            continue
        dt = datetime.datetime.fromtimestamp(visit_date_us / 1_000_000)
        add_site_visit(dt.date().isoformat(), dt.strftime("%H:%M:%S"), domain_from_url(url), url, title or "")

    set_meta("last_ff_visit_id", max_id)


def firefox_history_loop():
    interval = CONFIG["firefox_history_scan_seconds"]
    while True:
        time.sleep(interval)
        try:
            scan_firefox_history()
        except Exception as exc:
            print("firefox scan error:", exc)


# --------------------------------------------------------------------------
# Background tracker loop
# --------------------------------------------------------------------------

def tracker_loop():
    interval = CONFIG["poll_interval_seconds"]
    idle_threshold = CONFIG["idle_threshold_seconds"]
    while True:
        time.sleep(interval)
        try:
            if get_idle_seconds() >= idle_threshold:
                continue
            proc = get_foreground_process_name()
            if not proc:
                continue
            today = datetime.date.today().isoformat()
            add_seconds(today, proc, interval)
        except Exception as exc:
            print("tracker error:", exc)


# --------------------------------------------------------------------------
# Web dashboard
# --------------------------------------------------------------------------

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Screen Time</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { color-scheme: light dark; }
  body {
    font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    max-width: 760px; margin: 0 auto; padding: 24px 16px 60px;
    background: #f5f6f8; color: #1c1e21;
  }
  h1 { font-size: 22px; margin-bottom: 4px; }
  .subtitle { color: #667; margin-bottom: 24px; font-size: 14px; }
  .card {
    background: #fff; border-radius: 12px; padding: 20px;
    margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  }
  .total { font-size: 40px; font-weight: 700; }
  .total-label { color: #667; font-size: 13px; text-transform: uppercase; letter-spacing: .04em; }
  .bar-track { background: #e7e9ec; border-radius: 8px; height: 14px; overflow: hidden; margin-top: 12px; }
  .bar-fill { height: 100%; border-radius: 8px; transition: width .3s; }
  .ok { background: #34a853; } .warn { background: #f9ab00; } .over { background: #ea4335; }
  table { width: 100%; border-collapse: collapse; }
  th, td { text-align: left; padding: 8px 4px; border-bottom: 1px solid #eee; font-size: 14px; }
  th { color: #667; font-weight: 600; font-size: 12px; text-transform: uppercase; }
  .history { display: flex; align-items: flex-end; gap: 6px; height: 120px; margin-top: 12px; }
  .day-col { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }
  .day-bar { width: 100%; background: #4285f4; border-radius: 4px 4px 0 0; min-height: 2px; }
  .day-label { font-size: 10px; color: #889; margin-top: 4px; }
  .empty { color: #889; font-size: 14px; padding: 12px 0; }
  .scroll-table { max-height: 260px; overflow-y: auto; margin-top: 4px; }
  .domain-cell { font-weight: 600; }
  .title-cell { color: #667; font-size: 12px; }
  .time-cell { color: #889; font-size: 12px; white-space: nowrap; }
</style>
</head>
<body>
  <h1>Screen Time Dashboard</h1>
  <div class="subtitle" id="dateLabel">Loading…</div>

  <div class="card">
    <div class="total-label">Today's total</div>
    <div class="total" id="totalToday">--</div>
    <div class="bar-track"><div class="bar-fill ok" id="limitBar" style="width:0%"></div></div>
    <div class="subtitle" id="limitLabel" style="margin-top:6px; margin-bottom:0;"></div>
  </div>

  <div class="card">
    <div class="total-label" style="margin-bottom:8px;">By app (today)</div>
    <table id="appsTable"><tbody></tbody></table>
    <div class="empty" id="appsEmpty" style="display:none;">No activity recorded yet today.</div>
  </div>

  <div class="card">
    <div class="total-label">Last 14 days</div>
    <div class="history" id="historyChart"></div>
  </div>

  <div class="card">
    <div class="total-label" style="margin-bottom:8px;">Websites visited today (Firefox)</div>
    <table id="domainsTable"><tbody></tbody></table>
    <div class="empty" id="domainsEmpty" style="display:none;">
      No Firefox browsing history found yet today.
    </div>
  </div>

  <div class="card">
    <div class="total-label" style="margin-bottom:8px;">Recent page visits</div>
    <div class="scroll-table">
      <table id="visitsTable"><tbody></tbody></table>
    </div>
    <div class="empty" id="visitsEmpty" style="display:none;">Nothing recorded yet.</div>
  </div>

<script>
function esc(s) {
  const div = document.createElement("div");
  div.textContent = s == null ? "" : String(s);
  return div.innerHTML;
}

function fmt(seconds) {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return h + "h " + m + "m";
  return m + "m";
}

async function refresh() {
  const today = await (await fetch("/api/today")).json();
  document.getElementById("dateLabel").textContent = today.date;
  document.getElementById("totalToday").textContent = fmt(today.total_seconds);

  const limitSeconds = today.daily_limit_minutes * 60;
  const pct = limitSeconds > 0 ? Math.min(100, (today.total_seconds / limitSeconds) * 100) : 0;
  const bar = document.getElementById("limitBar");
  bar.style.width = pct + "%";
  bar.className = "bar-fill " + (pct < 75 ? "ok" : pct < 100 ? "warn" : "over");
  document.getElementById("limitLabel").textContent =
    fmt(today.total_seconds) + " of " + fmt(limitSeconds) + " daily limit";

  const tbody = document.querySelector("#appsTable tbody");
  tbody.innerHTML = "";
  document.getElementById("appsEmpty").style.display = today.apps.length ? "none" : "block";
  today.apps.forEach(app => {
    const tr = document.createElement("tr");
    tr.innerHTML = "<td>" + app.name + "</td><td style='text-align:right'>" + fmt(app.seconds) + "</td>";
    tbody.appendChild(tr);
  });

  const hist = await (await fetch("/api/history")).json();
  const maxSeconds = Math.max(1, ...hist.days.map(d => d.seconds));
  const chart = document.getElementById("historyChart");
  chart.innerHTML = "";
  hist.days.forEach(d => {
    const col = document.createElement("div");
    col.className = "day-col";
    const barHeight = Math.max(2, (d.seconds / maxSeconds) * 100);
    const label = d.date.slice(5); // MM-DD
    col.innerHTML =
      "<div class='day-bar' style='height:" + barHeight + "%' title='" + fmt(d.seconds) + "'></div>" +
      "<div class='day-label'>" + label + "</div>";
    chart.appendChild(col);
  });

  const sites = await (await fetch("/api/websites")).json();

  const domainsBody = document.querySelector("#domainsTable tbody");
  domainsBody.innerHTML = "";
  document.getElementById("domainsEmpty").style.display = sites.top_domains.length ? "none" : "block";
  sites.top_domains.forEach(d => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      "<td class='domain-cell'>" + esc(d.domain) + "</td>" +
      "<td style='text-align:right'>" + d.visits + " visit" + (d.visits === 1 ? "" : "s") + "</td>" +
      "<td class='time-cell' style='text-align:right'>" + esc(d.last_seen) + "</td>";
    domainsBody.appendChild(tr);
  });

  const visitsBody = document.querySelector("#visitsTable tbody");
  visitsBody.innerHTML = "";
  document.getElementById("visitsEmpty").style.display = sites.recent_visits.length ? "none" : "block";
  sites.recent_visits.forEach(v => {
    const tr = document.createElement("tr");
    const titleText = v.title ? v.title : v.url;
    tr.innerHTML =
      "<td class='time-cell'>" + esc(v.time) + "</td>" +
      "<td><div class='domain-cell'>" + esc(v.domain) + "</div><div class='title-cell'>" + esc(titleText) + "</div></td>";
    visitsBody.appendChild(tr);
  });
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_json(self, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            data = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/api/today"):
            today = datetime.date.today().isoformat()
            rows = get_day_totals(today)
            total = sum(s for _, s in rows)
            self._send_json(
                {
                    "date": today,
                    "total_seconds": total,
                    "daily_limit_minutes": CONFIG["daily_limit_minutes"],
                    "apps": [{"name": n, "seconds": s} for n, s in rows],
                }
            )
        elif self.path.startswith("/api/history"):
            rows = get_last_n_days(14)
            self._send_json({"days": [{"date": d, "seconds": s or 0} for d, s in rows]})
        elif self.path.startswith("/api/websites"):
            today = datetime.date.today().isoformat()
            domains = get_top_domains(today)
            visits = get_site_visits(today, limit=100)
            self._send_json(
                {
                    "date": today,
                    "top_domains": [{"domain": d, "visits": v, "last_seen": t} for d, v, t in domains],
                    "recent_visits": [{"time": t, "domain": d, "url": u, "title": ti} for t, d, u, ti in visits],
                }
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # keep console/log file quiet


def main():
    if os.name != "nt":
        print("This tool uses Windows-only APIs and must run on Windows.")
        sys.exit(1)

    init_db()
    threading.Thread(target=tracker_loop, daemon=True).start()
    threading.Thread(target=firefox_history_loop, daemon=True).start()

    port = CONFIG["port"]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Screen Time dashboard running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
