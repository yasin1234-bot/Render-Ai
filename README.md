# Ping AI — Production Keep-Alive & Self-Ping Service for Render.com

![Ping AI Banner](https://raw.githubusercontent.com/yeasingahmmed0011/ping-ai/main/banner.png)

A lightweight, high-performance web application built with **Python Flask, APScheduler, SQLite, HTML5, CSS3, and JavaScript** designed specifically to keep **Render.com free tier web services and external apps awake 24/7 without sleeping**.

---

## ⚡ Why Ping AI?

Render.com puts Free Tier Web Services to sleep after **15 minutes of inactivity**, resulting in:
- **50+ second cold start delays** for users, webhooks, or API requests.
- Broken background workers, bots (Telegram/Discord bots), and cron jobs.

**Ping AI fixes this permanently by:**
1. **Fully Automated Self-Keep-Alive (Zero Config)**: Automatically detects its own Render URL on startup and pings itself every 5 minutes non-stop. No manual link pasting required.
2. **Multi-Target URL Keep-Alive Manager**: Allows you to add all your other external Render, Koyeb, Railway, or HuggingFace web service links with customizable intervals (2m, 5m, 7m, 10m).
3. **Zero Data Loss**: Persistent SQLite database keeps all your monitored targets and logs across server restarts.

---

## 🚀 Quick Deploy to Render.com (Step-by-Step)

### Option 1: Automatic Blueprint Deploy (1-Click)
1. Push this repository to your **GitHub** account.
2. Go to your [Render Dashboard](https://dashboard.render.com).
3. Click **New +** -> **Blueprint**.
4. Connect your repository — Render will automatically read `render.yaml` and configure Python 3.11, `requirements.txt`, and `gunicorn app:app`.
5. Click **Apply**!

### Option 2: Manual Web Service Setup on Render
1. Create a **New Web Service** on Render.
2. Connect your GitHub repository.
3. Configure the settings:
   - **Name**: `ping-ai`
   - **Environment**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app --workers 1 --threads 4 --timeout 120`
   - **Plan**: `Free`
4. Optional Environment Variables (in the Render Environment tab):
   - `SELF_PING_INTERVAL_MINUTES`: `5` (Default is 5 minutes)
   - `RENDER_EXTERNAL_URL`: Automatically provided by Render.
5. Click **Create Web Service**.

---

## 📁 Repository Structure

```
├── app.py                  # Core Flask backend + APScheduler background keepalive worker + SQLite
├── requirements.txt        # Python dependencies (Flask, requests, APScheduler, gunicorn)
├── Procfile                # Render/Heroku process runner configuration
├── render.yaml             # Render Blueprint infrastructure as code
├── ping_ai.db              # Auto-created SQLite persistent storage
├── templates/
│   └── index.html          # Mobile-first dark glassmorphism dashboard layout
├── static/
│   ├── css/
│   │   └── style.css       # Clean, modern CSS with mobile touch optimization
│   └── js/
│       └── app.js          # Live dashboard controller, instant ping, and log streamer
├── .env.example            # Environment variables example
└── README.md               # Documentation & deployment instructions
```

---

## 💻 Local Development Setup

```bash
# 1. Clone the repository
git clone https://github.com/your-username/ping-ai.git
cd ping-ai

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the app
python app.py
```
Open your browser at `http://localhost:5000`.

---

## 🛠️ API Reference

- `GET /healthz` or `GET /ping` — Returns `200 OK` (used by self-ping worker).
- `GET /api/status` — Returns system uptime, active monitors, and recent activity.
- `POST /api/targets` — Register a new URL to monitor (`{ name, url, interval_minutes }`).
- `POST /api/targets/<id>/toggle` — Pause/resume keep-alive for a target.
- `POST /api/targets/<id>/ping-now` — Trigger an instant test ping.
- `DELETE /api/targets/<id>` — Remove a monitored target.
- `POST /api/settings` — Update self-ping URL and configuration.

---

## 🛡️ License
MIT License. Built for developers hosting on Render.com free tier.
