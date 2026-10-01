import os
import time
import requests
import threading
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

targets = []
targets_lock = threading.Lock()

# ✅ Fallback সহ Render URL
RENDER_EXTERNAL_URL = os.environ.get(
    "RENDER_EXTERNAL_URL",
    "https://your-app-name.onrender.com"  # আপনার আসল URL বসান
)

def ping_worker():
    """Background thread worker to periodically ping added URLs and self-ping."""
    last_self_ping = 0
    
    while True:
        current_time = time.time()
        
        # ✅ Self-ping প্রতি ৪ মিনিটে (২৪০ সেকেন্ড), timeout ৬০s
        if RENDER_EXTERNAL_URL and (current_time - last_self_ping >= 240):
            try:
                res = requests.get(
                    RENDER_EXTERNAL_URL,
                    timeout=60,  # ✅ cold start-এর জন্য ৬০s
                    headers={"User-Agent": "KeepAlive-Self-Ping/1.0"}
                )
                print(f"[Self-Ping] {RENDER_EXTERNAL_URL} -> {res.status_code}")
            except Exception as e:
                print(f"[Self-Ping Error] {e}")
            last_self_ping = current_time

        # Target URLs
        with targets_lock:
            for target in targets:
                interval_sec = target["interval"] * 60
                if current_time - target["last_ping_timestamp"] >= interval_sec:
                    try:
                        res = requests.get(target["url"], timeout=30)
                        target["status"] = (
                            f"200 OK ({res.elapsed.total_seconds():.2f}s)"
                            if res.status_code == 200
                            else f"HTTP {res.status_code}"
                        )
                    except Exception:
                        target["status"] = "Failed / Offline"
                    
                    target["last_ping_timestamp"] = current_time
                    target["last_ping"] = time.strftime("%H:%M:%S")

        time.sleep(10)

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
    try:
        interval = int(data.get("interval", 5))
    except (TypeError, ValueError):
        interval = 5

    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid URL. Include http:// or https://"}), 400

    with targets_lock:
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

# ✅ Health check endpoint (Render এর জন্য গুরুত্বপূর্ণ)
@app.route("/healthz")
def healthz():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)