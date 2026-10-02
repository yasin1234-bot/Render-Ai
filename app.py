"""
Ping AI — Production Python Flask Keep-Alive & Self-Ping Engine for Render.com
Fully Automated Self-Ping: Detects own Render deployment URL on startup with zero manual input required.
License: MIT
"""

import os
import time
import sqlite3
import logging
import threading
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify, render_template
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger("PingAI")

# Flask Application initialization
app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'ping-ai-secret-key-render-2026')

DB_PATH = os.environ.get('DB_PATH', os.path.join(os.path.dirname(__file__), 'ping_ai.db'))
DEFAULT_PORT = int(os.environ.get('PORT', 5000))
SELF_PING_INTERVAL_MINUTES = int(os.environ.get('SELF_PING_INTERVAL_MINUTES', '5'))

# ==============================================================================
# APScheduler CONFIGURATION (BACKUP LAYER)
# ==============================================================================
jobstores = {'default': MemoryJobStore()}
executors = {'default': ThreadPoolExecutor(max_workers=10)}
job_defaults = {
    'coalesce': True,
    'max_instances': 1,
    'misfire_grace_time': 300,
}

scheduler = BackgroundScheduler(
    jobstores=jobstores,
    executors=executors,
    job_defaults=job_defaults,
    daemon=True
)

# Global lock to prevent concurrent pings of same target
_ping_locks: Dict[str, threading.Lock] = {}
_ping_locks_lock = threading.Lock()

def _get_ping_lock(target_id: str) -> threading.Lock:
    with _ping_locks_lock:
        if target_id not in _ping_locks:
            _ping_locks[target_id] = threading.Lock()
        return _ping_locks[target_id]


# ==============================================================================
# DATABASE MANAGEMENT (SQLite Persistent Store)
# ==============================================================================

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS targets (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            interval_minutes INTEGER NOT NULL DEFAULT 5,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_ping_time TEXT,
            last_status_code INTEGER,
            last_latency_ms INTEGER,
            last_status_message TEXT,
            consecutive_successes INTEGER NOT NULL DEFAULT 0,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            total_pings INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ping_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id TEXT NOT NULL,
            target_name TEXT NOT NULL,
            url TEXT NOT NULL,
            status_code INTEGER,
            latency_ms INTEGER,
            status_message TEXT NOT NULL,
            is_success INTEGER NOT NULL,
            timestamp TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    ''')

    conn.commit()
    conn.close()
    logger.info("SQLite database initialized successfully.")


def get_setting(key: str, default: str = "") -> str:
    try:
        conn = get_db_connection()
        try:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        finally:
            conn.close()
        return row['value'] if row else default
    except Exception:
        return default


def set_setting(key: str, value: str):
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value)
        )
        conn.commit()
    finally:
        conn.close()


# ==============================================================================
# AUTOMATIC SELF-URL RESOLUTION (ZERO CONFIGURATION REQUIRED)
# ==============================================================================

def get_automated_self_url() -> str:
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if render_url and render_url.strip():
        return render_url.strip().rstrip('/')

    render_hostname = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
    if render_hostname and render_hostname.strip():
        return f"https://{render_hostname.strip()}".rstrip('/')

    custom_url = os.environ.get('SELF_URL')
    if custom_url and custom_url.strip():
        return custom_url.strip().rstrip('/')

    saved_url = get_setting("self_url", "")
    if saved_url and not saved_url.startswith("http://127.0.0.1") and not saved_url.startswith("http://localhost"):
        return saved_url.rstrip('/')

    port = int(os.environ.get('PORT', DEFAULT_PORT))
    return f"http://127.0.0.1:{port}"


# ==============================================================================
# PING WORKER EXECUTION ENGINE
# ==============================================================================

def execute_ping(url: str, target_id: str = "self", target_name: str = "Ping AI (Self)") -> Dict[str, Any]:
    start_time = time.perf_counter()
    headers = {
        'User-Agent': 'PingAI-KeepAlive/3.0 (+https://render.com)',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache'
    }
    
    status_code = None
    latency_ms = 0
    status_message = ""
    is_success = False
    
    try:
        response = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        status_code = response.status_code
        
        if 200 <= status_code < 400:
            status_message = f"HTTP {status_code} OK"
            is_success = True
        else:
            status_message = f"HTTP {status_code} {response.reason}"
            is_success = False
    except requests.exceptions.Timeout:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        status_code = 408
        status_message = "Request Timeout (>25s)"
        is_success = False
    except requests.exceptions.ConnectionError:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        status_code = 502
        status_message = "Connection Refused / Offline"
        is_success = False
    except Exception as e:
        latency_ms = int((time.perf_counter() - start_time) * 1000)
        status_code = 500
        status_message = f"Error: {str(e)[:50]}"
        is_success = False

    iso_now = datetime.now(timezone.utc).isoformat()

    try:
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT INTO ping_logs (target_id, target_name, url, status_code, latency_ms, status_message, is_success, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (target_id, target_name, url, status_code, latency_ms, status_message, 1 if is_success else 0, iso_now))

            if target_id != "self":
                if is_success:
                    conn.execute('''
                        UPDATE targets SET
                            last_ping_time = ?,
                            last_status_code = ?,
                            last_latency_ms = ?,
                            last_status_message = ?,
                            consecutive_successes = consecutive_successes + 1,
                            consecutive_failures = 0,
                            total_pings = total_pings + 1
                        WHERE id = ?
                    ''', (iso_now, status_code, latency_ms, status_message, target_id))
                else:
                    conn.execute('''
                        UPDATE targets SET
                            last_ping_time = ?,
                            last_status_code = ?,
                            last_latency_ms = ?,
                            last_status_message = ?,
                            consecutive_successes = 0,
                            consecutive_failures = consecutive_failures + 1,
                            total_pings = total_pings + 1
                        WHERE id = ?
                    ''', (iso_now, status_code, latency_ms, status_message, target_id))
            
            conn.execute("DELETE FROM ping_logs WHERE id NOT IN (SELECT id FROM ping_logs ORDER BY id DESC LIMIT 400)")
            conn.commit()
        finally:
            conn.close()
    except Exception as db_err:
        logger.error(f"Failed to record ping result in database: {db_err}")

    logger.info(f"Pinged [{target_name}] -> {status_message} ({latency_ms}ms)")
    
    return {
        "target_id": target_id,
        "target_name": target_name,
        "url": url,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "status_message": status_message,
        "is_success": is_success,
        "timestamp": iso_now
    }


# ==============================================================================
# MANUAL THREAD WORKERS (PRIMARY LAYER — GUARANTEED TO RUN)
# ==============================================================================

def self_keep_alive_worker():
    """
    Manual thread loop for self-ping — same reliable pattern as external targets.
    Runs forever, checking every 30 seconds whether it's time to ping.
    """
    logger.info("[Self Worker] Started.")
    last_run = 0
    while True:
        try:
            time.sleep(15)  # check every 15s
            self_enabled = get_setting("self_ping_enabled", "true") == "true"
            if not self_enabled:
                continue
            interval_sec = max(60, int(get_setting("self_ping_interval", str(SELF_PING_INTERVAL_MINUTES))) * 60)
            now = time.time()
            if now - last_run >= interval_sec:
                self_url = get_automated_self_url()
                ping_url = self_url.rstrip('/') + '/healthz'
                logger.info(f"[Self Worker] Pinging self: {ping_url}")
                execute_ping(ping_url, target_id="self", target_name="Ping AI (Self-Ping Worker)")
                last_run = now
        except Exception as e:
            logger.error(f"[Self Worker] Error: {e}", exc_info=True)
            time.sleep(10)


def target_ping_worker():
    """
    Manual thread loop for ALL external targets.
    This is the same logic as self_keep_alive_worker but iterates over every
    active target in the DB. Runs forever, checking every 20 seconds.
    """
    logger.info("[Target Worker] Started.")
    last_run_map: Dict[str, float] = {}
    while True:
        try:
            time.sleep(20)  # check every 20s
            conn = get_db_connection()
            try:
                targets = conn.execute("SELECT * FROM targets WHERE is_active = 1").fetchall()
            finally:
                conn.close()

            now = time.time()
            for t in targets:
                tid = t['id']
                interval_sec = max(60, int(t['interval_minutes']) * 60)
                last = last_run_map.get(tid, 0)
                if now - last >= interval_sec:
                    # Use a per-target lock so we don't double-ping
                    lock = _get_ping_lock(tid)
                    if not lock.acquire(blocking=False):
                        continue
                    try:
                        logger.info(f"[Target Worker] Pinging '{t['name']}' ({t['url']})")
                        execute_ping(t['url'], target_id=tid, target_name=t['name'])
                        last_run_map[tid] = now
                    finally:
                        lock.release()
        except Exception as e:
            logger.error(f"[Target Worker] Error: {e}", exc_info=True)
            time.sleep(10)


def start_manual_workers():
    """Start both manual worker threads (daemon = won't block Flask exit)."""
    t1 = threading.Thread(target=self_keep_alive_worker, daemon=True, name="SelfKeepAliveWorker")
    t1.start()
    t2 = threading.Thread(target=target_ping_worker, daemon=True, name="TargetPingWorker")
    t2.start()
    logger.info("Manual background workers started (self + targets).")


# ==============================================================================
# APScheduler JOBS (BACKUP LAYER)
# ==============================================================================

def self_keep_alive_job():
    self_enabled = get_setting("self_ping_enabled", "true") == "true"
    if not self_enabled:
        return
    self_url = get_automated_self_url()
    ping_url = self_url.rstrip('/') + '/healthz'
    logger.info(f"[APScheduler Self] Pinging: {ping_url}")
    execute_ping(ping_url, target_id="self", target_name="Ping AI (APScheduler Self)")


def ping_target_job(target_id: str):
    try:
        conn = get_db_connection()
        try:
            row = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        finally:
            conn.close()
        
        if not row:
            return
        if not row['is_active']:
            return

        lock = _get_ping_lock(target_id)
        if not lock.acquire(blocking=False):
            return
        try:
            logger.info(f"[APScheduler] Pinging '{row['name']}' ({row['url']})")
            execute_ping(row['url'], target_id=row['id'], target_name=row['name'])
        finally:
            lock.release()
    except Exception as e:
        logger.error(f"[APScheduler] Error for {target_id}: {e}", exc_info=True)


def sync_scheduler_jobs():
    """Sync APScheduler jobs as a backup to the manual workers."""
    try:
        self_enabled = get_setting("self_ping_enabled", "true") == "true"
        self_interval = int(get_setting("self_ping_interval", str(SELF_PING_INTERVAL_MINUTES)))
        
        existing_self = scheduler.get_job("self_keep_alive")
        if existing_self:
            scheduler.remove_job("self_keep_alive")
            
        if self_enabled:
            scheduler.add_job(
                self_keep_alive_job,
                trigger=IntervalTrigger(minutes=max(1, self_interval)),
                id="self_keep_alive",
                replace_existing=True,
                name="Self Keep-Alive (APScheduler Backup)"
            )

        conn = get_db_connection()
        try:
            targets = conn.execute("SELECT * FROM targets").fetchall()
        finally:
            conn.close()

        active_ids = {f"target_{t['id']}" for t in targets if t['is_active']}
        for job in scheduler.get_jobs():
            if job.id.startswith("target_") and job.id not in active_ids:
                try:
                    scheduler.remove_job(job.id)
                except Exception:
                    pass

        for target in targets:
            job_id = f"target_{target['id']}"
            existing = scheduler.get_job(job_id)
            if existing:
                try:
                    scheduler.remove_job(job_id)
                except Exception:
                    pass
            if target['is_active']:
                interval = max(1, int(target['interval_minutes']))
                scheduler.add_job(
                    ping_target_job,
                    args=[target['id']],
                    trigger=IntervalTrigger(minutes=interval),
                    id=job_id,
                    replace_existing=True,
                    name=f"APScheduler Backup: {target['name']}",
                    next_run_time=datetime.now(timezone.utc)
                )
    except Exception as e:
        logger.error(f"sync_scheduler_jobs error: {e}", exc_info=True)


# ==============================================================================
# HTTP ROUTES & REST APIS
# ==============================================================================

@app.before_request
def auto_detect_self_url():
    current_self = get_setting("self_url", "")
    if not current_self or "127.0.0.1" in current_self or "localhost" in current_self:
        host = request.headers.get('Host', '')
        proto = request.headers.get('X-Forwarded-Proto', 'https' if request.is_secure else 'http')
        if host and not host.startswith('127.0.0.1') and not host.startswith('localhost'):
            detected = f"{proto}://{host}"
            set_setting("self_url", detected)
            logger.info(f"Auto-detected public host: {detected}")


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/healthz')
@app.route('/ping')
def healthz():
    return jsonify({
        "status": "online",
        "app": "Ping AI",
        "time": datetime.now(timezone.utc).isoformat(),
        "message": "Render keep-alive service running 24/7."
    }), 200


@app.route('/api/status', methods=['GET'])
def get_system_status():
    conn = get_db_connection()
    try:
        targets = [dict(t) for t in conn.execute("SELECT * FROM targets ORDER BY created_at DESC").fetchall()]
        total_logs = conn.execute("SELECT COUNT(*) as cnt FROM ping_logs").fetchone()['cnt']
        success_logs = conn.execute("SELECT COUNT(*) as cnt FROM ping_logs WHERE is_success = 1").fetchone()['cnt']
        avg_latency = conn.execute("SELECT AVG(latency_ms) as avg_lat FROM ping_logs WHERE is_success = 1").fetchone()['avg_lat']
        recent_logs = [dict(l) for l in conn.execute("SELECT * FROM ping_logs ORDER BY id DESC LIMIT 50").fetchall()]
    finally:
        conn.close()

    self_url = get_automated_self_url()
    self_enabled = get_setting("self_ping_enabled", "true") == "true"
    self_interval = int(get_setting("self_ping_interval", str(SELF_PING_INTERVAL_MINUTES)))

    uptime_pct = round((success_logs / total_logs * 100), 1) if total_logs > 0 else 100.0

    return jsonify({
        "self_url": self_url,
        "self_ping_enabled": self_enabled,
        "self_ping_interval": self_interval,
        "is_automated": True,
        "targets": targets,
        "metrics": {
            "total_targets": len(targets),
            "active_targets": sum(1 for t in targets if t['is_active']),
            "total_pings": total_logs,
            "success_rate": uptime_pct,
            "avg_latency_ms": round(avg_latency, 1) if avg_latency else 0
        },
        "recent_logs": recent_logs,
        "server_time": datetime.now(timezone.utc).isoformat()
    })


@app.route('/api/targets', methods=['POST'])
def add_target():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    url = (data.get('url') or '').strip()
    interval = int(data.get('interval_minutes') or 5)

    if not url:
        return jsonify({"error": "Target URL is required."}), 400

    if not url.startswith('http://') and not url.startswith('https://'):
        url = 'https://' + url

    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            return jsonify({"error": "Invalid URL format."}), 400
    except Exception:
        return jsonify({"error": "Invalid URL format."}), 400

    if not name:
        name = parsed.netloc.replace('www.', '').split('.')[0].capitalize() or "External App"

    interval = max(1, min(interval, 60))
    target_id = f"tgt_{int(time.time() * 1000)}"
    iso_now = datetime.now(timezone.utc).isoformat()

    conn = get_db_connection()
    try:
        conn.execute('''
            INSERT INTO targets (id, name, url, interval_minutes, is_active, consecutive_successes, consecutive_failures, total_pings, created_at)
            VALUES (?, ?, ?, ?, 1, 0, 0, 0, ?)
        ''', (target_id, name, url, interval, iso_now))
        conn.commit()
    finally:
        conn.close()

    # APScheduler backup
    job_id = f"target_{target_id}"
    existing = scheduler.get_job(job_id)
    if existing:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass
    scheduler.add_job(
        ping_target_job,
        args=[target_id],
        trigger=IntervalTrigger(minutes=interval),
        id=job_id,
        replace_existing=True,
        name=f"APScheduler Backup: {name}",
        next_run_time=datetime.now(timezone.utc)
    )

    # Manual worker: force immediate first ping
    def run_initial_ping():
        time.sleep(1)
        try:
            execute_ping(url, target_id=target_id, target_name=name)
        except Exception as e:
            logger.error(f"Initial ping failed for {name}: {e}")

    threading.Thread(target=run_initial_ping, daemon=True).start()
    logger.info(f"Added target '{name}' (id={target_id}), interval={interval} min.")

    return jsonify({
        "success": True,
        "message": f"Target '{name}' added successfully. Manual worker will ping every {interval} min.",
        "target": {
            "id": target_id,
            "name": name,
            "url": url,
            "interval_minutes": interval,
            "is_active": 1,
            "created_at": iso_now
        }
    }), 201


@app.route('/api/targets/<target_id>', methods=['DELETE'])
def delete_target(target_id: str):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM targets WHERE id = ?", (target_id,))
        conn.execute("DELETE FROM ping_logs WHERE target_id = ?", (target_id,))
        conn.commit()
    finally:
        conn.close()

    job_id = f"target_{target_id}"
    if scheduler.get_job(job_id):
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass

    return jsonify({"success": True, "message": "Target removed."})


@app.route('/api/targets/<target_id>/toggle', methods=['POST'])
def toggle_target(target_id: str):
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT is_active, name FROM targets WHERE id = ?", (target_id,)).fetchone()
        if not row:
            return jsonify({"error": "Target not found."}), 404

        new_state = 0 if row['is_active'] else 1
        conn.execute("UPDATE targets SET is_active = ? WHERE id = ?", (new_state, target_id))
        conn.commit()
        target_name = row['name']
    finally:
        conn.close()

    job_id = f"target_{target_id}"
    existing = scheduler.get_job(job_id)
    if existing:
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass

    if new_state:
        conn = get_db_connection()
        try:
            trow = conn.execute("SELECT interval_minutes FROM targets WHERE id = ?", (target_id,)).fetchone()
        finally:
            conn.close()
        if trow:
            interval = max(1, int(trow['interval_minutes']))
            scheduler.add_job(
                ping_target_job,
                args=[target_id],
                trigger=IntervalTrigger(minutes=interval),
                id=job_id,
                replace_existing=True,
                name=f"APScheduler Backup: {target_name}",
                next_run_time=datetime.now(timezone.utc)
            )

    action = "resumed" if new_state else "paused"
    return jsonify({
        "success": True,
        "is_active": new_state,
        "message": f"Keep-alive for '{target_name}' {action}."
    })


@app.route('/api/targets/<target_id>/ping-now', methods=['POST'])
def ping_target_now(target_id: str):
    if target_id == "self":
        self_url = get_automated_self_url()
        result = execute_ping(self_url.rstrip('/') + '/healthz', target_id="self", target_name="Ping AI (Self)")
        return jsonify({"success": True, "result": result})

    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
    finally:
        conn.close()

    if not row:
        return jsonify({"error": "Target not found."}), 404

    result = execute_ping(row['url'], target_id=row['id'], target_name=row['name'])
    return jsonify({"success": True, "result": result})


@app.route('/api/settings', methods=['POST'])
def update_settings():
    data = request.get_json() or {}

    if 'self_ping_enabled' in data:
        set_setting('self_ping_enabled', 'true' if data['self_ping_enabled'] else 'false')

    if 'self_ping_interval' in data:
        interval = max(1, min(int(data['self_ping_interval']), 60))
        set_setting('self_ping_interval', str(interval))

    if 'self_url' in data:
        url = (data['self_url'] or '').strip()
        if url and not url.startswith('http://') and not url.startswith('https://'):
            url = 'https://' + url
        set_setting('self_url', url)

    sync_scheduler_jobs()
    return jsonify({"success": True, "message": "Settings updated."})


@app.route('/api/logs', methods=['GET'])
def get_logs():
    conn = get_db_connection()
    try:
        logs = conn.execute("SELECT * FROM ping_logs ORDER BY id DESC LIMIT 100").fetchall()
    finally:
        conn.close()
    return jsonify({"logs": [dict(l) for l in logs]})


@app.route('/api/logs', methods=['DELETE'])
def clear_logs():
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM ping_logs")
        conn.commit()
    finally:
        conn.close()
    return jsonify({"success": True, "message": "Logs cleared."})


# ==============================================================================
# BOOTSTRAP
# ==============================================================================

init_db()

# Start APScheduler (backup layer)
if not scheduler.running:
    scheduler.start()
    sync_scheduler_jobs()
    logger.info("APScheduler backup layer running.")

# Start manual threading workers (PRIMARY layer)
start_manual_workers()

# One-shot startup self-ping
def delayed_initial_ping():
    time.sleep(5)
    try:
        self_url = get_automated_self_url()
        execute_ping(self_url.rstrip('/') + '/healthz', target_id="self", target_name="Ping AI (Startup)")
    except Exception as e:
        logger.error(f"Startup self-ping failed: {e}")

threading.Thread(target=delayed_initial_ping, daemon=True).start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', DEFAULT_PORT))
    logger.info(f"Starting Ping AI Flask server on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=False)