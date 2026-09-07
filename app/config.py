"""Environment / configuration handling for NexTunnel.

All configuration is done via environment variables so the app can be freely
re-deployed (Railway, Docker, bare server) without touching code.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Paths & data ──────────────────────────────────────────────────────────
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "nextunnel.db"

# Download / background-sync intervals (seconds)
TRAFFIC_SYNC_INTERVAL = 30          # flush in-memory traffic counters to DB
CONNECTION_REAP_INTERVAL = 15       # XHTTP stale-session reaper

# Relay / XHTTP tuning
RELAY_BUF = 256 * 1024                 # ws relay chunk buffer
XHTTP_BUF = 512 * 1024
XHTTP_QUEUE_MAX = 512
XHTTP_SESSION_IDLE = 45                # seconds (uplink/downlink activity)
TCP_CONNECT_TIMEOUT = 10.0
SOCK_BUF_SIZE = 2 * 1024 * 1024        # SO_SNDBUF / SO_RCVBUF

# Adaptive flow/quake engine bounds (XHTTP stream-up)
FLOW_MIN_HW = 256 * 1024
FLOW_MAX_HW = 16 * 1024 * 1024
FLOW_START_HW = 2 * 1024 * 1024
FLOW_FAST_DRAIN_MS = 2.0
FLOW_SLOW_DRAIN_MS = 25.0
QUOTA_MIN_BATCH = 32 * 1024
QUOTA_MAX_BATCH = 1 * 1024 * 1024
QUOTA_START_BATCH = 64 * 1024
QUOTA_CHECK_INTERVAL = 0.2
PACKET_UP_HIGH_WATER = 2 * 1024 * 1024

# ── Server ────────────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", "8000"))
HOST_FALLBACK = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip() or "localhost"

# ⚙️ Which protocol/transport configs we can generate & serve.
PROTOCOLS = ("vless-ws", "xhttp-packet-up", "xhttp-stream-up")
DEFAULT_PROTOCOL = "vless-ws"

FINGERPRINTS = ("chrome", "firefox", "safari", "ios", "android", "edge", "360", "qq", "random", "randomized")
DEFAULT_FINGERPRINT = "chrome"

DEFAULT_ALPN_BY_PROTOCOL = {
    "vless-ws": "http/1.1",
    "xhttp-packet-up": "h2,http/1.1",
    "xhttp-stream-up": "h2,http/1.1",
}
DEFAULT_PORT = 443
MIN_PORT, MAX_PORT = 1, 65535
DEFAULT_SPEED_LIMIT = 0

# ── Auth / sessions ───────────────────────────────────────────────────────
SESSION_COOKIE = "nt_session"
SESSION_TTL = 60 * 60 * 24 * 365          # 1 year (remember me)
LOGIN_RATE_WINDOW = 60                    # seconds
LOGIN_RATE_MAX = 8                        # attempts per window per IP

# Initial admin password. Stored (hashed) into the DB on first boot; changing
# the env var later does NOT overwrite a password the user already set.
DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "NexTunnel@2025")

# ── Telegram bot (initial values only; the web panel can override at runtime) ──
INIT_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
INIT_BOT_ADMINS = [int(x) for x in os.environ.get("TELEGRAM_ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()]

SUPPORT_CHANNEL = os.environ.get("SUPPORT_CHANNEL", "https://t.me/Farajian2004f")

# ── Security: proxy headers ──────────────────────────────────────────────
# When True, `X-Forwarded-For`'s LAST entry is treated as the real client IP.
# Behind Railway/Cloudflare the proxy *appends* the true client, so taking the
# last entry stops spoofing (a remote user can only prepend fake entries).
TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "true").lower() in ("1", "true", "yes")