import os
import time
import json
import atexit
import requests
import threading
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# ---------------------------------------------------------------
# Config
# ---------------------------------------------------------------
TARGETS_FILE = "/tmp/targets.json"   # Render-এ writable

targets = []
targets_lock = threading.Lock()
worker_lock = threading.Lock()
worker_started = False

RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL",
    "https://render-ai-zle2.onrender.com"
)

print(f"[BOOT] Self-Ping URL = {RENDER_EXTERNAL_URL}", flush=True)
print(f"[BOOT] PID = {os.getpid()}", flush=True)


# ---------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------
def load_targets():
    global targets
    if os.path.exists(TARGETS_FILE):
        try:
            with open(TARGETS_FILE, "r") as f:
                loaded = json.load(f)
            targets = [
                {
                    "url": t["url"],
                    "interval": int(t.get("interval", 5)),
                    "last_ping_timestamp": 0,
                    "last_ping": t.get("last_ping", "Never"),
                    "status": t.get("status", "Pending"),
                }
                for t in loaded if "url" in t
            ]
            print(f"[BOOT] Loaded {len(targets)} targets", flush=True)
        except Exception as e:
            print(f"[BOOT ERROR] load_targets: {e}", flush=True)
            targets = []


def save_targets():
    try:
        with targets_lock:
            snapshot = list(targets)
        with open(TARGETS_FILE, "w") as f:
            json.dump(snapshot, f, indent=2)
    except Exception as e:
        print(f"[SAVE ERROR] {e}", flush=True)


load_targets()


# ---------------------------------------------------------------
# Ping helper
# ---------------------------------------------------------------
def do_ping(url, ua="KeepAlive-Ping/1.0", timeout=25):
    try:
        res = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": ua},
            allow_redirects=True,
        )
        if res.status_code == 200:
            return f"200 OK ({res.elapsed.total_seconds():.2f}s)"
        return f"HTTP {res.status_code}"
    except requests.exceptions.Timeout:
        return "Timeout"
    except Exception as e:
        return f"Error: {str(e)[:40]}"


# ---------------------------------------------------------------
# Worker
# ---------------------------------------------------------------
def ping_worker():
    print(f"[WORKER] Started in PID={os.getpid()}", flush=True)
    last_self_ping = 0
    cycle = 0

    while True:
        try:
            cycle += 1
            now = time.time()
            print(f"[WORKER] Cycle {cycle} @ {time.strftime('%H:%M:%S')}", flush=True)

            # Self ping every 4 min
            if RENDER_EXTERNAL_URL and (now - last_self_ping >= 240):
                status = do_ping(RENDER_EXTERNAL_URL, ua="KeepAlive-Self/1.0", timeout=45)
                print(f"[Self-Ping] -> {status}", flush=True)
                last_self_ping = now

            # Target pings
            with targets_lock:
                snapshot = list(targets)

            for target in snapshot:
                interval_sec = target["interval"] * 60
                if now - target["last_ping_timestamp"] >= interval_sec:
                    print(f"[PING-START] {target['url']}", flush=True)
                    status = do_ping(target["url"], timeout=25)
                    print(f"[PING-DONE] {target['url']} -> {status}", flush=True)

                    with targets_lock:
                        for t in targets:
                            if t["url"] == target["url"]:
                                t["status"] = status
                                t["last_ping_timestamp"] = time.time()
                                t["last_ping"] = time.strftime("%H:%M:%S")
                                break

            save_targets()

        except Exception as e:
            print(f"[WORKER ERROR] {e}", flush=True)

        time.sleep(10)


def ensure_worker_running():
    """Start worker exactly once, safely across Gunicorn forks."""
    global worker_started
    with worker_lock:
        alive = any(
            t.name == "ping-worker" and t.is_alive()
            for t in threading.enumerate()
        )
        if not alive:
            t = threading.Thread(
                target=ping_worker,
                daemon=True,
                name="ping-worker",
            )
            t.start()
            worker_started = True
            print(f"[BOOT] Worker thread launched (PID={os.getpid()})", flush=True)
        else:
            print(f"[BOOT] Worker already running (PID={os.getpid()})", flush=True)


# ✅ Start immediately at import time
ensure_worker_running()


# ✅ ALSO start on the very first HTTP request — this guarantees the thread
#    survives even if Gunicorn re-forks after import.
@app.before_request
def _ensure_worker_on_first_request():
    ensure_worker_running()


# ✅ Extra safety — re-start on any process exit signal if somehow dead
@atexit.register
def _shutdown():
    print("[BOOT] Process exiting", flush=True)


# ---------------------------------------------------------------
# Routes
# ---------------------------------------------------------------
@app.route("/")
def index():
    ensure_worker_running()   # ✅ double-check every page load
    return render_template("index.html")


@app.route("/api/targets", methods=["GET"])
def get_targets():
    with targets_lock:
        data = [{
            "url": t["url"],
            "interval": t["interval"],
            "last_ping": t["last_ping"],
            "status": t["status"],
        } for t in targets]
    return jsonify(data)


@app.route("/api/targets", methods=["POST"])
def add_target():
    data = request.json or {}
    url = data.get("url", "").strip()
    try:
        interval = int(data.get("interval", 5))
    except (TypeError, ValueError):
        interval = 5

    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid URL"}), 400

    with targets_lock:
        for t in targets:
            if t["url"] == url:
                t["interval"] = interval
                save_targets()
                return jsonify({"message": "Updated"}), 200

        targets.append({
            "url": url,
            "interval": interval,
            "last_ping_timestamp": 0,   # immediate first ping
            "last_ping": "Never",
            "status": "Pending",
        })

    save_targets()
    ensure_worker_running()   # ✅ safety
    print(f"[API] Added target: {url}", flush=True)
    return jsonify({"message": "Added"}), 201


@app.route("/api/targets/delete", methods=["POST"])
def delete_target():
    data = request.json or {}
    url = data.get("url", "").strip()

    with targets_lock:
        global targets
        targets = [t for t in targets if t["url"] != url]

    save_targets()
    return jsonify({"message": "Removed"}), 200


@app.route("/healthz")
def healthz():
    ensure_worker_running()   # ✅ ping this → keeps worker alive
    return "OK", 200


@app.route("/api/debug")
def debug():
    return jsonify({
        "pid": os.getpid(),
        "worker_threads": [t.name for t in threading.enumerate()],
        "worker_alive": any(
            t.name == "ping-worker" and t.is_alive()
            for t in threading.enumerate()
        ),
        "targets_count": len(targets),
        "targets": [
            {"url": t["url"], "status": t["status"], "last": t["last_ping"]}
            for t in targets
        ],
        "render_url": RENDER_EXTERNAL_URL,
        "time": time.strftime("%H:%M:%S"),
    })


# ---------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)