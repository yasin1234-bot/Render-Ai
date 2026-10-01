import os
import time
import requests
import threading
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# Store ping targets: list of dicts -> {"url": str, "interval": int, "last_ping": str, "status": str}
targets = []
targets_lock = threading.Lock()

# Custom Self-Ping URL (Render environment variable dynamically fetches this, or auto-detects)
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

def ping_worker():
    """Background thread worker to periodically ping added URLs and self-ping."""
    last_self_ping = 0
    
    while True:
        current_time = time.time()
        
        # 1. Auto Self-Ping every 5 minutes (300 seconds) to keep THIS app awake
        if RENDER_EXTERNAL_URL and (current_time - last_self_ping >= 300):
            try:
                requests.get(RENDER_EXTERNAL_URL, timeout=10)
                print(f"[Self-Ping] Sent ping to self: {RENDER_EXTERNAL_URL}")
            except Exception as e:
                print(f"[Self-Ping Error] {e}")
            last_self_ping = current_time

        # 2. Ping user-added target URLs based on their individual intervals
        with targets_lock:
            for target in targets:
                interval_sec = target["interval"] * 60
                if current_time - target["last_ping_timestamp"] >= interval_sec:
                    try:
                        res = requests.get(target["url"], timeout=10)
                        target["status"] = f"200 OK ({res.elapsed.total_seconds():.2f}s)" if res.status_code == 200 else f"HTTP {res.status_code}"
                    except Exception as e:
                        target["status"] = "Failed / Offline"
                    
                    target["last_ping_timestamp"] = current_time
                    target["last_ping"] = time.strftime("%H:%M:%S")

        time.sleep(10)  # Check interval schedule every 10 seconds

# Start background thread
ping_thread = threading.Thread(target=ping_worker, daemon=True)
ping_thread.start()

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
    interval = int(data.get("interval", 5))

    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid URL standard. Include http:// or https://"}), 400

    with targets_lock:
        # Check if URL already exists
        for t in targets:
            if t["url"] == url:
                t["interval"] = interval
                return jsonify({"message": "Updated existing target"}), 200
        
        targets.append({
            "url": url,
            "interval": interval,
            "last_ping_timestamp": 0,
            "last_ping": "Never",
            "status": "Pending"
        })

    return jsonify({"message": "URL added successfully"}), 201

@app.route("/api/targets/delete", methods=["POST"])
def delete_target():
    data = request.json or {}
    url = data.get("url", "").strip()
    
    with targets_lock:
        global targets
        targets = [t for t in targets if t["url"] != url]

    return jsonify({"message": "Target removed"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)