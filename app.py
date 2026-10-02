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
# APScheduler CONFIGURATION (FIXED FOR RENDER FREE TIER)
# ==============================================================================
# Use ThreadPoolExecutor with enough workers so pings don't block each other
# Use MemoryJobStore (not persistent) to avoid stale job conflicts on restart
jobstores = {
    'default': MemoryJobStore()
}
executors = {
    'default': ThreadPoolExecutor(max_workers=10)
}
job_defaults = {
    'coalesce': True,           # If multiple runs missed, only run once
    'max_instances': 1,         # Never run same job concurrently
    'misfire_grace_time': 120,  # 2 min grace if scheduler was busy
}

scheduler = BackgroundScheduler(
    jobstores=jobstores,
    executors=executors,
    job_defaults=job_defaults,
    daemon=True
)


# ==============================================================================
# DATABASE MANAGEMENT (SQLite Persistent Store)
# ==============================================================================

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
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
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row['value'] if row else default
    except Exception:
        return default


def set_setting(key: str, value: str):
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value)
    )
    conn.commit()
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
        'User-Agent': 'PingAI-KeepAlive/2.0 (+https://render.com)',
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

    # FIXED: Use try/finally to guarantee connection close
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


def self_keep_alive_job():
    self_enabled = get_setting("self_ping_enabled", "true") == "true"
    if not self_enabled:
        logger.info("Self keep-alive is currently paused by user toggle.")
        return

    self_url = get_automated_self_url()
    ping_url = self_url.rstrip('/') + '/healthz'
    logger.info(f"[Auto Self-Ping] Executing automated keep-alive ping on: {ping_url}")
    execute_ping(ping_url, target_id="self", target_name="Ping AI (Automated Self-Ping)")


def ping_target_job(target_id: str):
    """
    Worker job executed by APScheduler for a specific registered target.
    FIXED: Guaranteed connection close + clearer logging.
    """
    try:
        conn = get_db_connection()
        try:
            row = conn.execute("SELECT * FROM targets WHERE id = ?", (target_id,)).fetchone()
        finally:
            conn.close()
        
        if not row:
            logger.warning(f"Target {target_id} not found in DB. Removing job.")
            job = scheduler.get_job(f"target_{target_id}")
            if job:
                scheduler.remove_job(f"target_{target_id}")
            return
            
        if not row['is_active']:
            logger.info(f"Target {target_id} is paused. Skipping ping.")
            return

        logger.info(f"Executing scheduled ping for target: {row['name']} ({row['url']})")
        execute_ping(row['url'], target_id=row['id'], target_name=row['name'])
    except Exception as e:
        logger.error(f"Error in ping_target_job for {target_id}: {e}", exc_info=True)


def sync_scheduler_jobs():
    """
    Synchronizes APScheduler jobs with current active targets in SQLite.
    FIXED: Removes old jobs before re-adding to prevent overwrite bugs.
    """
    # 1. Sync Automated Self Keep-Alive
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
            name="Automated Self Keep-Alive Worker"
        )
        logger.info(f"Self Keep-Alive worker active. Interval: every {self_interval} minutes.")

    # 2. Sync Target URLs
    try:
        conn = get_db_connection()
        try:
            targets = conn.execute("SELECT * FROM targets").fetchall()
        finally:
            conn.close()

        active_ids = {f"target_{t['id']}" for t in targets}
        for job in scheduler.get_jobs():
            if job.id.startswith("target_") and job.id not in active_ids:
                scheduler.remove_job(job.id)

        for target in targets:
            job_id = f"target_{target['id']}"
            if target['is_active']:
                interval = max(1, int(target['interval_minutes']))
                # Remove old job first to prevent stale triggers
                existing = scheduler.get_job(job_id)
                if existing:
                    scheduler.remove_job(job_id)
                scheduler.add_job(
                    ping_target_job,
                    args=[target['id']],
                    trigger=IntervalTrigger(minutes=interval),
                    id=job_id,
                    replace_existing=True,
                    name=f"Keep-Alive for {target['name']}"
                )
                logger.info(f"Scheduled job for '{target['name']}' every {interval} min.")
            else:
                existing = scheduler.get_job(job_id)
                if existing:
                    scheduler.remove_job(job_id)
                    
        logger.info(f"Scheduler synchronized with {len(targets)} targets.")
    except Exception as e:
        logger.error(f"Failed to sync scheduler jobs: {e}", exc_info=True)


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
            logger.info(f"Programmatically captured public host: {detected}")


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

    # Register recurring job immediately
    job_id = f"target_{target_id}"
    existing = scheduler.get_job(job_id)
    if existing:
        scheduler.remove_job(job_id)
    scheduler.add_job(
        ping_target_job,
        args=[target_id],
        trigger=IntervalTrigger(minutes=interval),
        id=job_id,
        replace_existing=True,
        name=f"Keep-Alive for {name}"
    )
    logger.info(f"Registered new recurring job '{job_id}' every {interval} min for {name}.")

    # Immediate initial ping in background
    def run_initial_ping():
        time.sleep(2)
        try:
            execute_ping(url, target_id=target_id, target_name=name)
        except Exception as e:
            logger.error(f"Initial ping failed for {name}: {e}")

    threading.Thread(target=run_initial_ping, daemon=True).start()

    return jsonify({
        "success": True,
        "message": f"Target '{name}' added successfully and keep-alive scheduled every {interval} min.",
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
        scheduler.remove_job(job_id)

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
        interval_row = conn.execute("SELECT interval_minutes FROM targets WHERE id = ?", (target_id,)).fetchone()
    finally:
        conn.close()

    job_id = f"target_{target_id}"
    existing = scheduler.get_job(job_id)
    if existing:
        scheduler.remove_job(job_id)

    if new_state and interval_row:
        interval = max(1, int(interval_row['interval_minutes']))
        scheduler.add_job(
            ping_target_job,
            args=[target_id],
            trigger=IntervalTrigger(minutes=interval),
            id=job_id,
            replace_existing=True,
            name=f"Keep-Alive for {target_name}"
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
# BOOTSTRAP BACKGROUND SCHEDULER & IMMEDIATE STARTUP SELF-PING
# ==============================================================================

init_db()

if not scheduler.running:
    scheduler.start()
    sync_scheduler_jobs()
    logger.info("APScheduler worker running in background.")

    def delayed_initial_ping():
        time.sleep(5)
        self_keep_alive_job()

    threading.Thread(target=delayed_initial_ping, daemon=True).start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', DEFAULT_PORT))
    logger.info(f"Starting Ping AI Flask server on port {port}...")
    app.run(host='0.0.0.0', port=port, debug=False)