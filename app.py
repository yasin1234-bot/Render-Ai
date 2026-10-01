import os
import time
import json
import requests
import threading
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# ---------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------
TARGETS_FILE = "targets.json"

targets = []
targets_lock = threading.Lock()

# ✅ Hardcoded fallback — Render env var না থাকলেও কাজ করবে
RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL",
    "https://render-ai-zle2.onrender.com"
)

print(f"[INIT] Self-Ping URL = {RENDER_EXTERNAL_URL}")
print(f"[INIT] PORT = {os.environ.get('PORT', '5000')}")

# ---------------------------------------------------------------
# Persistent storage (targets.json)
# ---------------------------------------------------------------
def load_targets():
    """Load saved targets from JSON file."""
    global targets
    if os.path.exists(TARGETS_FILE):
        try:
            with open(TARGETS_FILE, "r") as f:
                loaded = json.load(f)
                # Ensure structure is correct
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
            print(f"[INIT] Loaded {len(targets)} target(s) from {TARGETS_FILE}")
        except Exception as e:
            print(f"[INIT ERROR] Could not load targets: {e}")
            targets = []
    else:
        print(f"[INIT] No {TARGETS_FILE} found — starting fresh")


def save_targets():
    """Save targets to JSON file."""
    try:
        with open(TARGETS_FILE, "w") as f:
            json.dump(targets, f, indent=2)
    except Exception as e:
        print(f"[SAVE ERROR] {e}")


# Load persisted targets at startup
load_targets()

# ---------------------------------------------------------------
# Background ping worker
# ---------------------------------------------------------------
def ping_worker():
    """Background thread — self-ping + user target pings."""
    last_self_ping = 0

    while True:
        current_time = time.time()

        # 1) Self-ping every 4 minutes to keep THIS app awake
        if RENDER_EXTERNAL_URL and (current_time - last_self_ping >= 240):
            try:
                res = requests.get(
                    RENDER_EXTERNAL_URL,
                    timeout=60,
                    headers={"User-Agent": "KeepAlive-Self-Ping/1.0"},
                    allow_redirects=True,
                )
                print(f"[Self-Ping] {RENDER_EXTERNAL_URL} -> {res.status_code}")
            except Exception as e:
                print(f"[Self-Ping Error] {e}")
            last_self_ping = current_time

        # 2) Ping each user target based on its interval
        with targets_lock:
            for target in targets:
                interval_sec = target["interval"] * 60

                # First ping happens immediately (last_ping_timestamp = 0)
                if current_time - target["last_ping_timestamp"] >= interval_sec:
                    try:
                        res = requests.get(
                            target["url"],
                            timeout=30,
                            headers={"User-Agent": "KeepAlive-Ping/1.0"},
                            allow_redirects=True,
                        )
                        if res.status_code == 200:
                            target["status"] = f"200 OK ({res.elapsed.total_seconds():.2f}s)"
                        else:
                            target["status"] = f"HTTP {res.status_code}"
                        print(f"[Ping] {target['url']} -> {target['status']}")
                    except requests.exceptions.Timeout:
                        target["status"] = "Timeout"
                        print(f"[Ping Timeout] {target['url']}")
                    except Exception as e:
                        target["status"] = "Failed / Offline"
                        print(f"[Ping Error] {target['url']} -> {e}")

                    target["last_ping_timestamp"] = current_time
                    target["last_ping"] = time.strftime("%H:%M:%S")

            # Save state periodically so data survives restart
            save_targets()

        time.sleep(10)


# Start background thread
ping_thread = threading.Thread(target=ping_worker, daemon=True)
ping_thread.start()
print("[INIT] Ping worker thread started")

# ---------------------------------------------------------------
# Routes
# ---------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/targets", methods=["GET"])
def get_targets():
    with targets_lock:
        data = [{
            "url": t["url"],
            "interval": t["interval"],
            "last_ping": t["last_ping"],
            "status": t["status"]
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
        return jsonify({"error": "Invalid URL. Include http:// or https://"}), 400

    with targets_lock:
        # Update if exists
        for t in targets:
            if t["url"] == url:
                t["interval"] = interval
                save_targets()
                return jsonify({"message": "Updated existing target"}), 200

        targets.append({
            "url": url,
            "interval": interval,
            "last_ping_timestamp": 0,   # ✅ triggers immediate first ping
            "last_ping": "Never",
            "status": "Pending"
        })
        save_targets()

    return jsonify({"message": "URL added successfully"}), 201


@app.route("/api/targets/delete", methods=["POST"])
def delete_target():
    data = request.json or {}
    url = data.get("url", "").strip()

    with targets_lock:
        global targets
        targets = [t for t in targets if t["url"] != url]
        save_targets()

    return jsonify({"message": "Target removed"}), 200


# ✅ Health check for Render
@app.route("/healthz")
def healthz():
    return "OK", 200


# ---------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)