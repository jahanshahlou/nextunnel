"""Shared in-memory runtime state for NexTunnel.

This module is imported by every other module (transports, API, bot, web).
Keeping the mutable state here — instead of on `main` — breaks the circular
imports that plagued the original project (main ↔ relay ↔ xhttp ↔ throttle).
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config  # noqa: F401
from .config import SUPPORT_CHANNEL
from . import __version__ as VERSION  # noqa: F401
from . import APP_NAME  # noqa: F401

IRAN_TZ = ZoneInfo("Asia/Tehran")

# ── Data dictionaries (SQLite is the durable source of truth; these
#    mirrors are the hot in-memory copies used by the relay hot path) ──────
LINKS: dict = {}
SUBS: dict = {}
CONNECTIONS: dict = {}
STATS: dict = {
    "total_bytes": 0,
    "total_requests": 0,
    "total_errors": 0,
    "start_time": time.time(),
}
HOURLY_TRAFFIC: dict = defaultdict(int)
ERROR_LOGS: deque = deque(maxlen=60)
ACTIVITY_LOGS: deque = deque(maxlen=250)

# ── Session store (token -> expiry timestamp) ─────────────────────────────
SESSIONS: dict = {}

# ── XHTTP sessions (session_id -> state dict) ─────────────────────────────
XHTTP_SESSIONS: dict = {}

# ── Async locks ───────────────────────────────────────────────────────────
LINKS_LOCK = asyncio.Lock()
SUBS_LOCK = asyncio.Lock()
SESSIONS_LOCK = asyncio.Lock()
PERSIST_LOCK = asyncio.Lock()
XHTTP_LOCK = asyncio.Lock()

# Current "public host" seen from a request (used to build share-links in
# contexts where we don't have a Request, e.g. the Telegram bot).
PUBLIC_HOST: str = config.HOST_FALLBACK


def now_ir() -> datetime:
    return datetime.now(IRAN_TZ)


def log_activity(kind: str, message: str, level: str = "info") -> None:
    ACTIVITY_LOGS.appendleft({
        "kind": kind,
        "level": level,
        "message": message,
        "time": datetime.now().isoformat(),
    })


def fmt_bytes(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024 ** 2:
        return f"{b / 1024:.1f} KB"
    if b < 1024 ** 3:
        return f"{b / 1024 ** 2:.2f} MB"
    return f"{b / 1024 ** 3:.2f} GB"


def uptime() -> str:
    secs = int(time.time() - STATS["start_time"])
    h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def is_link_expired(link: dict | None) -> bool:
    if not link:
        return False
    exp = link.get("expires_at")
    if not exp:
        return False
    try:
        expired = datetime.fromisoformat(exp)
        if expired.tzinfo is None:
            expired = expired.replace(tzinfo=IRAN_TZ)
        return now_ir() > expired
    except ValueError:
        return False


def is_link_allowed(link: dict | None) -> bool:
    if not link:
        return False
    if not link.get("active", True):
        return False
    if is_link_expired(link):
        return False
    lb = link.get("limit_bytes", 0)
    if lb > 0 and link.get("used_bytes", 0) >= lb:
        return False
    return True


def unique_ips_for_uuid(uuid: str) -> set:
    return {c.get("ip") for c in CONNECTIONS.values() if c.get("uuid") == uuid and c.get("ip")}


def is_ip_allowed(link: dict | None, uuid: str, ip: str) -> bool:
    """Per-link concurrent-IP limit.

    If the same IP already holds a session on this config it's always allowed
    (a device with many parallel sockets/streams must not block itself).
    """
    if not link:
        return False
    limit = int(link.get("ip_limit", 0) or 0)
    if limit <= 0:
        return True
    ips = unique_ips_for_uuid(uuid)
    if ip in ips:
        return True
    return len(ips) < limit


def check_and_use(uid: str, n: int) -> bool:
    """Atomically consume `n` bytes of a config's quota.

    Returns False when the config is missing, disabled, expired or out of quota.
    All quota/traffic accounting is in-memory; a background task flushes the
    counters to SQLite on a schedule (see services.py).
    """
    link = LINKS.get(uid)
    if not is_link_allowed(link):
        return False
    link["used_bytes"] += n
    STATS["total_bytes"] += n
    HOURLY_TRAFFIC[now_ir().strftime("%H:00")] += n
    return True