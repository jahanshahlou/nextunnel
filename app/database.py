"""SQLite persistence layer for NexTunnel (via aiosqlite).

The relay hot path only mutates the in-memory mirrors in :mod:`app.registry`;
this module periodically flushes those counters to disk and loads them at
startup. Configuration (admin password hash, bot token/admins) lives in a
simple key/value table so the web panel can read/write it at runtime.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiosqlite

from . import registry, config

logger = logging.getLogger("NexTunnel")

_conn: aiosqlite.Connection | None = None


async def init() -> None:
    global _conn
    Path(config.DATA_DIR).mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(config.DB_PATH)
    _conn.row_factory = aiosqlite.Row
    await _conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS links (
            uuid             TEXT PRIMARY KEY,
            label            TEXT NOT NULL,
            protocol         TEXT NOT NULL DEFAULT 'vless-ws',
            fingerprint      TEXT NOT NULL DEFAULT 'chrome',
            alpn             TEXT NOT NULL DEFAULT '',
            port             INTEGER NOT NULL DEFAULT 443,
            limit_bytes      INTEGER NOT NULL DEFAULT 0,
            used_bytes       INTEGER NOT NULL DEFAULT 0,
            speed_limit_bytes INTEGER NOT NULL DEFAULT 0,
            ip_limit         INTEGER NOT NULL DEFAULT 0,
            active           INTEGER NOT NULL DEFAULT 1,
            expires_at       TEXT,
            sub_id           TEXT,
            note             TEXT NOT NULL DEFAULT '',
            is_default       INTEGER NOT NULL DEFAULT 0,
            created_at       TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subs (
            sub_id       TEXT PRIMARY KEY,
            name         TEXT NOT NULL,
            desc         TEXT NOT NULL DEFAULT '',
            password_hash TEXT,
            uuid_key     TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    await _conn.commit()
    logger.info("SQLite ready: %s", config.DB_PATH)


async def close() -> None:
    global _conn
    if _conn:
        await _conn.close()
        _conn = None


# ── Load ───────────────────────────────────────────────────────────────────
async def load_links() -> None:
    async with _lock():
        cur = await _conn.execute("SELECT * FROM links")
        rows = await cur.fetchall()
    registry.LINKS = {r["uuid"]: dict(r) for r in rows}
    # normalize types read back from sqlite
    for link in registry.LINKS.values():
        link["active"] = bool(link["active"])
        link["is_default"] = bool(link["is_default"])
        link["limit_bytes"] = int(link["limit_bytes"])
        link["used_bytes"] = int(link["used_bytes"])
        link["speed_limit_bytes"] = int(link["speed_limit_bytes"])
        link["ip_limit"] = int(link["ip_limit"])
        link["port"] = int(link["port"])
    logger.info("Loaded %d links", len(registry.LINKS))


async def load_subs() -> None:
    async with _lock():
        cur = await _conn.execute("SELECT * FROM subs")
        rows = await cur.fetchall()
    registry.SUBS = {r["sub_id"]: dict(r) for r in rows}
    logger.info("Loaded %d sub groups", len(registry.SUBS))


async def load_settings() -> dict:
    async with _lock():
        cur = await _conn.execute("SELECT key, value FROM settings")
        rows = await cur.fetchall()
    return {r["key"]: r["value"] for r in rows}


async def get_setting(key: str, default=None):
    s = await load_settings()
    return s.get(key, default)


# ── Persist ────────────────────────────────────────────────────────────────
async def save_link_row(uid: str, link: dict) -> None:
    async with _lock():
        await _conn.execute(
            """INSERT INTO links (uuid,label,protocol,fingerprint,alpn,port,limit_bytes,
               used_bytes,speed_limit_bytes,ip_limit,active,expires_at,sub_id,note,
               is_default,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(uuid) DO UPDATE SET
                 label=excluded.label, protocol=excluded.protocol,
                 fingerprint=excluded.fingerprint, alpn=excluded.alpn,
                 port=excluded.port, limit_bytes=excluded.limit_bytes,
                 used_bytes=excluded.used_bytes,
                 speed_limit_bytes=excluded.speed_limit_bytes,
                 ip_limit=excluded.ip_limit, active=excluded.active,
                 expires_at=excluded.expires_at, sub_id=excluded.sub_id,
                 note=excluded.note, is_default=excluded.is_default,
                 created_at=excluded.created_at""",
            (
                uid, link.get("label", ""), link.get("protocol"), link.get("fingerprint"),
                link.get("alpn"), link.get("port"), link.get("limit_bytes", 0),
                link.get("used_bytes", 0), link.get("speed_limit_bytes", 0),
                link.get("ip_limit", 0), int(link.get("active", True)),
                link.get("expires_at"), link.get("sub_id"), link.get("note"),
                int(link.get("is_default", False)), link.get("created_at"),
            ),
        )
        await _conn.commit()


async def delete_link_row(uid: str) -> None:
    async with _lock():
        await _conn.execute("DELETE FROM links WHERE uuid=?", (uid,))
        await _conn.commit()


async def save_sub_row(sub_id: str, sub: dict) -> None:
    async with _lock():
        await _conn.execute(
            """INSERT INTO subs (sub_id,name,desc,password_hash,uuid_key,created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(sub_id) DO UPDATE SET
                 name=excluded.name, desc=excluded.desc,
                 password_hash=excluded.password_hash,
                 uuid_key=excluded.uuid_key, created_at=excluded.created_at""",
            (sub_id, sub.get("name", ""), sub.get("desc", ""),
             sub.get("password_hash"), sub.get("uuid_key"), sub.get("created_at")),
        )
        await _conn.commit()


async def delete_sub_row(sub_id: str) -> None:
    async with _lock():
        await _conn.execute("DELETE FROM subs WHERE sub_id=?", (sub_id,))
        await _conn.commit()


async def set_setting(key: str, value: str) -> None:
    async with _lock():
        await _conn.execute(
            "INSERT INTO settings (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await _conn.commit()


async def persist_stats() -> None:
    async with _lock():
        await _conn.execute(
            "INSERT INTO settings (key,value) VALUES ('stats',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                json.dumps({
                    "total_bytes": registry.STATS["total_bytes"],
                    "total_requests": registry.STATS["total_requests"],
                    "total_errors": registry.STATS["total_errors"],
                    "hourly_traffic": dict(registry.HOURLY_TRAFFIC),
                }),
            ),
        )
        await _conn.commit()


async def load_stats() -> None:
    s = await get_setting("stats")
    if not s:
        return
    try:
        data = json.loads(s)
        registry.STATS["total_bytes"] = int(data.get("total_bytes", 0))
        registry.STATS["total_requests"] = int(data.get("total_requests", 0))
        registry.STATS["total_errors"] = int(data.get("total_errors", 0))
        registry.HOURLY_TRAFFIC.clear()
        registry.HOURLY_TRAFFIC.update({k: int(v) for k, v in data.get("hourly_traffic", {}).items()})
    except (ValueError, TypeError):
        pass


def _lock():
    """Helper: serialize all SQLite writes through the persist lock.

    aiosqlite itself queues on a single connection so this is belt-and-braces,
    but it keeps multi-statement operations from interleaving with flushes.
    Call as `async with _lock():`.
    """
    return registry.PERSIST_LOCK


# ── Full flush used by the background sync task ────────────────────────────
async def flush_all_links() -> None:
    """Persist every link row (used to sync traffic counters)."""
    async with registry.LINKS_LOCK:
        snapshot = dict(registry.LINKS)
    for uid, link in snapshot.items():
        await save_link_row(uid, link)
    await persist_stats()