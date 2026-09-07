"""Authentication: PBKDF2 password hashing, session management, rate limiting,
and trusted-proxy aware client-IP resolution.

Replaces the old scheme (single SHA-256 with a shared secret) which was fast
to brute-force and reset on every deployment when SECRET_KEY was unset.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import secrets
import time

from . import registry, config

_PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 with a random per-password salt."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"{_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        iterations, salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ── Sessions ───────────────────────────────────────────────────────────────
def create_session() -> str:
    token = secrets.token_urlsafe(32)
    async def _store():
        async with registry.SESSIONS_LOCK:
            registry.SESSIONS[token] = time.time() + config.SESSION_TTL
    asyncio.create_task(_store())
    return token


async def is_valid_session(token: str | None) -> bool:
    if not token:
        return False
    async with registry.SESSIONS_LOCK:
        exp = registry.SESSIONS.get(token)
        if exp is None:
            return False
        if exp < time.time():
            registry.SESSIONS.pop(token, None)
            return False
        return True


async def destroy_session(token: str | None) -> None:
    if not token:
        return
    async with registry.SESSIONS_LOCK:
        registry.SESSIONS.pop(token, None)


async def purge_expired_sessions() -> None:
    now = time.time()
    async with registry.SESSIONS_LOCK:
        expired = [t for t, exp in registry.SESSIONS.items() if exp < now]
        for t in expired:
            registry.SESSIONS.pop(t, None)


# ── Login rate limiting ────────────────────────────────────────────────────
_attempts: dict[str, list[float]] = {}


def login_allowed(ip: str) -> bool:
    now = time.time()
    bucket = _attempts.get(ip, [])
    bucket = [t for t in bucket if now - t < config.LOGIN_RATE_WINDOW]
    if len(bucket) >= config.LOGIN_RATE_MAX:
        _attempts[ip] = bucket
        return False
    bucket.append(now)
    _attempts[ip] = bucket
    return True


async def purge_rate_limits() -> None:
    now = time.time()
    for ip in [ip for ip, ts in _attempts.items() if not ts or now - max(ts) > config.LOGIN_RATE_WINDOW * 2]:
        _attempts.pop(ip, None)


# ── Client IP (behind trusted reverse proxies) ─────────────────────────────
def client_ip(headers) -> str:
    """Return the real client IP.

    Strategy: if TRUST_PROXY_HEADERS is on, use the LAST `X-Forwarded-For`
    entry. Railway/Cloudflare *append* the caller's IP to XFF, so the final
    element is the genuine client; any forged entries the user prepends land
    earlier in the chain and are ignored.
    """
    if config.TRUST_PROXY_HEADERS:
        xff = headers.get("x-forwarded-for")
        if xff:
            parts = [p.strip() for p in xff.split(",") if p.strip()]
            if parts:
                return parts[-1]
    real_ip = headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return headers.get("host", "unknown")  # never truly used; caller falls back


def request_ip(request) -> str:
    """Extract client IP from a Starlette Request/WebSocket."""
    h = getattr(request, "headers", None)
    if h is not None:
        ip = client_ip(h)
        if ip != "unknown":
            return ip
    client = getattr(request, "client", None)
    return client.host if client else "unknown"