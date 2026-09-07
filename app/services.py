"""Business logic: link/sub CRUD, default link, bot settings, host resolution.

Used by both the REST API and the Telegram bot so behaviour stays identical
across surfaces.
"""
from __future__ import annotations

import asyncio
import base64
import json
import secrets
from datetime import datetime, timedelta
from urllib.parse import quote

from . import config, database, registry
from .registry import IRAN_TZ, log_activity
from .security import hash_password


# ── Host resolution (mirrors the old get_host but caches across requests) ──
def resolve_host(request=None, headers=None) -> str:
    """Pick the best public host for building share-links.

    Prefers the request's Host / X-Forwarded-Host so the generated links always
    point at the domain the client actually used; falls back to the configured
    RAILWAY_PUBLIC_DOMAIN / localhost for contexts without a request (bot).
    """
    if request is not None:
        h = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if h:
            host = h.split(":")[0]
            registry.PUBLIC_HOST = host
            return host
    if headers is not None:
        h = headers.get("x-forwarded-host") or headers.get("host")
        if h:
            host = h.split(":")[0]
            registry.PUBLIC_HOST = host
            return host
    return registry.PUBLIC_HOST or config.HOST_FALLBACK


# ── Link config generation ─────────────────────────────────────────────────
def generate_uuid() -> str:
    h = secrets.token_hex(16)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def generate_vless_link(
    uuid: str,
    host: str,
    remark: str = "NexTunnel",
    protocol: str = config.DEFAULT_PROTOCOL,
    fingerprint: str | None = None,
    alpn: str | None = None,
    port: int | None = None,
) -> str:
    fp = (fingerprint or config.DEFAULT_FINGERPRINT).strip() or config.DEFAULT_FINGERPRINT
    if fp not in config.FINGERPRINTS:
        fp = config.DEFAULT_FINGERPRINT
    alpn_val = (alpn or "").strip() or config.DEFAULT_ALPN_BY_PROTOCOL.get(protocol, "http/1.1")
    port_val = port or config.DEFAULT_PORT
    if not (config.MIN_PORT <= port_val <= config.MAX_PORT):
        port_val = config.DEFAULT_PORT

    if protocol == "vless-ws":
        path = f"/ws/{uuid}"
        params = {
            "encryption": "none",
            "security": "tls",
            "type": "ws",
            "host": host,
            "path": path,
            "sni": host,
            "fp": fp,
            "alpn": alpn_val,
        }
    else:
        mode = protocol.replace("xhttp-", "")  # packet-up | stream-up
        path = f"/xhttp-siz10/{mode}/{uuid}"
        params = {
            "encryption": "none",
            "security": "tls",
            "type": "xhttp",
            "mode": mode,
            "host": host,
            "path": path,
            "sni": host,
            "fp": fp,
            "alpn": alpn_val,
        }
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    return f"vless://{uuid}@{host}:{port_val}?{query}#{quote(remark)}"


def vless_link_for_link(link: dict, uid: str, host: str) -> str:
    return generate_vless_link(
        uid, host,
        remark=f"Nex-{link.get('label', '')}",
        protocol=link.get("protocol", config.DEFAULT_PROTOCOL),
        fingerprint=link.get("fingerprint"),
        alpn=link.get("alpn"),
        port=link.get("port"),
    )


# ── Unit parsing helpers (shared by API + bot) ─────────────────────────────
def parse_size_to_bytes(value: float, unit: str) -> int:
    unit = unit.upper()
    if unit == "GB":
        return int(value * 1024 ** 3)
    if unit == "MB":
        return int(value * 1024 ** 2)
    if unit == "KB":
        return int(value * 1024)
    return int(value)


def parse_speed_to_bytes(value: float, unit: str) -> int:
    if value <= 0:
        return 0
    unit = (unit or "MBIT").upper()
    if unit == "MBIT":
        return int(value * 1024 * 1024 / 8)
    if unit == "KB":
        return int(value * 1024)
    if unit == "MB":
        return int(value * 1024 * 1024)
    return int(value)


# ── Link CRUD ──────────────────────────────────────────────────────────────
async def create_link(
    label: str = "لینک جدید",
    limit_bytes: int = 0,
    expires_at: str | None = None,
    note: str = "",
    sub_id: str | None = None,
    protocol: str = config.DEFAULT_PROTOCOL,
    fingerprint: str = config.DEFAULT_FINGERPRINT,
    alpn: str = "",
    port: int = config.DEFAULT_PORT,
    ip_limit: int = 0,
    speed_limit_bytes: int = 0,
) -> dict:
    if protocol not in config.PROTOCOLS:
        protocol = config.DEFAULT_PROTOCOL
    fingerprint = (fingerprint or config.DEFAULT_FINGERPRINT).strip().lower()
    if fingerprint not in config.FINGERPRINTS:
        fingerprint = config.DEFAULT_FINGERPRINT
    if not (config.MIN_PORT <= port <= config.MAX_PORT):
        port = config.DEFAULT_PORT

    uid = generate_uuid()
    link = {
        "uuid": uid,
        "label": (label or "لینک جدید").strip()[:60] or "لینک جدید",
        "limit_bytes": max(0, limit_bytes),
        "used_bytes": 0,
        "created_at": datetime.now().isoformat(),
        "active": True,
        "expires_at": expires_at,
        "note": (note or "").strip()[:200],
        "is_default": False,
        "sub_id": sub_id,
        "protocol": protocol,
        "fingerprint": fingerprint,
        "alpn": (alpn or "").strip()[:100],
        "port": port,
        "ip_limit": max(0, ip_limit),
        "speed_limit_bytes": max(0, speed_limit_bytes),
    }
    async with registry.LINKS_LOCK:
        registry.LINKS[uid] = link
    await database.save_link_row(uid, link)
    log_activity("link", f"کانفیگ «{link['label']}» ساخته شد", "ok")
    return link


async def update_link(uid: str, **fields) -> dict | None:
    """Patch a link. Accepted keys match the API body.
    `query`: {"label","note","active","reset_usage","limit_value","limit_unit",
              "expires_days","fingerprint","alpn","port","ip_limit",
              "speed_limit_value","speed_limit_unit","sub_id"}
    """
    async with registry.LINKS_LOCK:
        link = registry.LINKS.get(uid)
        if link is None:
            return None

        def log_edit():
            log_activity("link", f"کانفیگ «{link['label']}» ویرایش شد", "info")

        changed = False
        if "active" in fields:
            link["active"] = bool(fields["active"])
            log_activity("link", f"کانفیگ «{link['label']}» {'فعال' if link['active'] else 'غیرفعال'} شد",
                         "ok" if link["active"] else "warn")
            changed = True
        if "label" in fields:
            link["label"] = str(fields["label"])[:60]
            changed = True
        if "note" in fields:
            link["note"] = str(fields["note"])[:200]
            changed = True
        if fields.get("reset_usage"):
            link["used_bytes"] = 0
            log_activity("link", f"مصرف کانفیگ «{link['label']}» ریست شد", "info")
            changed = True

        if "limit_value" in fields:
            lv = float(fields.get("limit_value") or 0)
            lu = fields.get("limit_unit") or "GB"
            link["limit_bytes"] = 0 if lv <= 0 else parse_size_to_bytes(lv, lu)
            changed = True
        if "expires_days" in fields:
            ed = int(fields.get("expires_days") or 0)
            link["expires_at"] = (datetime.now() + timedelta(days=ed)).isoformat() if ed > 0 else None
            changed = True
        if "fingerprint" in fields:
            fp = str(fields.get("fingerprint") or config.DEFAULT_FINGERPRINT).strip().lower()
            link["fingerprint"] = fp if fp in config.FINGERPRINTS else config.DEFAULT_FINGERPRINT
            changed = True
        if "alpn" in fields:
            link["alpn"] = str(fields.get("alpn") or "").strip()[:100]
            changed = True
        if "port" in fields:
            try:
                p = int(fields.get("port") or config.DEFAULT_PORT)
            except (TypeError, ValueError):
                p = config.DEFAULT_PORT
            link["port"] = p if (config.MIN_PORT <= p <= config.MAX_PORT) else config.DEFAULT_PORT
            changed = True
        if "ip_limit" in fields:
            try:
                il = int(fields.get("ip_limit") or 0)
            except (TypeError, ValueError):
                il = 0
            link["ip_limit"] = max(0, il)
            changed = True
        if "speed_limit_value" in fields:
            sv = float(fields.get("speed_limit_value") or 0)
            su = fields.get("speed_limit_unit") or "MBIT"
            link["speed_limit_bytes"] = 0 if sv <= 0 else parse_speed_to_bytes(sv, su)
            from .transports.throttle import reset_bucket
            reset_bucket(uid)
            changed = True

        if "sub_id" in fields:
            changed = True
            link["sub_id"] = fields["sub_id"] or None

        if changed:
            log_edit()
            await database.save_link_row(uid, link)  # flush even inside lock (fast)
        return link


async def remove_link(uid: str) -> str | None:
    async with registry.LINKS_LOCK:
        link = registry.LINKS.get(uid)
        if link is None:
            return None
        label = link.get("label", uid)
        del registry.LINKS[uid]
    await database.delete_link_row(uid)
    from .transports.throttle import reset_bucket
    reset_bucket(uid)
    log_activity("link", f"کانفیگ «{label}» حذف شد", "err")
    return label


async def set_link_active(uid: str, active: bool) -> dict | None:
    async with registry.LINKS_LOCK:
        link = registry.LINKS.get(uid)
        if link is None:
            return None
        link["active"] = bool(active)
    await database.save_link_row(uid, link)
    return link


# ── Default link (guarantees the panel always has something to copy) ───────
_default_created = False


async def ensure_default_link() -> None:
    global _default_created
    if _default_created:
        return
    async with registry.LINKS_LOCK:
        if any(l.get("is_default") for l in registry.LINKS.values()):
            _default_created = True
            return
        # derivation is deterministic for the admin password so the panel's
        # default UUID is stable across restarts.
        uid_seed = secrets.token_urlsafe(8)
        link = {
            "uuid": generate_uuid(),
            "label": "لینک پیش‌فرض",
            "limit_bytes": 0,
            "used_bytes": 0,
            "created_at": datetime.now().isoformat(),
            "active": True,
            "expires_at": None,
            "note": "",
            "is_default": True,
            "sub_id": None,
            "protocol": config.DEFAULT_PROTOCOL,
            "fingerprint": config.DEFAULT_FINGERPRINT,
            "alpn": "",
            "port": config.DEFAULT_PORT,
            "ip_limit": 0,
            "speed_limit_bytes": 0,
        }
        registry.LINKS[link["uuid"]] = link
        await database.save_link_row(link["uuid"], link)
        _default_created = True


# ── Sub groups ─────────────────────────────────────────────────────────────
async def create_sub_group(name: str = "گروه جدید", desc: str = "", password: str = "") -> dict:
    sub_id = generate_uuid()
    uuid_key = secrets.token_urlsafe(16)
    sub = {
        "sub_id": sub_id,
        "name": (name or "گروه جدید").strip()[:60],
        "desc": (desc or "").strip()[:200],
        "password_hash": hash_password(password) if password else None,
        "uuid_key": uuid_key,
        "created_at": datetime.now().isoformat(),
    }
    async with registry.SUBS_LOCK:
        registry.SUBS[sub_id] = sub
    await database.save_sub_row(sub_id, sub)
    log_activity("sub", f"گروه «{sub['name']}» ساخته شد", "ok")
    return sub


async def update_sub(sub_id: str, **fields) -> dict | None:
    async with registry.SUBS_LOCK:
        sub = registry.SUBS.get(sub_id)
        if sub is None:
            return None
        if "name" in fields:
            sub["name"] = str(fields["name"])[:60]
        if "desc" in fields:
            sub["desc"] = str(fields["desc"])[:200]
        if "password" in fields:
            pw = str(fields["password"]).strip()
            sub["password_hash"] = hash_password(pw) if pw else None
    await database.save_sub_row(sub_id, sub)
    return sub


async def remove_sub_group(sub_id: str) -> str | None:
    async with registry.SUBS_LOCK:
        sub = registry.SUBS.get(sub_id)
        if sub is None:
            return None
        name = sub.get("name", sub_id)
        del registry.SUBS[sub_id]
    await database.delete_sub_row(sub_id)
    # Detach links — a link outside any group is still usable via its own URL.
    async with registry.LINKS_LOCK:
        for link in registry.LINKS.values():
            if link.get("sub_id") == sub_id:
                link["sub_id"] = None
                await database.save_link_row(link["uuid"], link)
    log_activity("sub", f"گروه «{name}» حذف شد", "warn")
    return name


async def set_link_sub(uid: str, sub_id: str | None) -> bool:
    async with registry.LINKS_LOCK:
        link = registry.LINKS.get(uid)
        if link is None:
            return False
        if sub_id is not None:
            async with registry.SUBS_LOCK:
                if sub_id not in registry.SUBS:
                    return False
        link["sub_id"] = sub_id
        await database.save_link_row(uid, link)
    return True


# ── Settings (admin password / bot) ────────────────────────────────────────
_loader = None


async def get_admin_password_hash() -> str:
    stored = await database.get_setting("admin_password_hash")
    if stored:
        return stored
    stored = hash_password(config.DEFAULT_ADMIN_PASSWORD)
    await database.set_setting("admin_password_hash", stored)
    return stored


async def change_admin_password(current: str, new: str) -> bool:
    stored = await get_admin_password_hash()
    from .security import verify_password
    if not verify_password(current, stored):
        return False
    if len(new) < 4:
        return False
    # overwrite the DB immediately; sessions are invalidated by the API layer.
    new_hash = hash_password(new)
    await database.set_setting("admin_password_hash", new_hash)
    return True


async def get_bot_config() -> dict:
    token = await database.get_setting("bot_token", config.INIT_BOT_TOKEN or None)
    admins_raw = await database.get_setting("bot_admins")
    if admins_raw is None:
        admins = config.INIT_BOT_ADMINS
    else:
        try:
            admins = json.loads(admins_raw)
        except (ValueError, TypeError):
            admins = []
    return {"token": token or "", "admins": admins}


async def save_bot_config(token: str, admins: list[int]) -> None:
    await database.set_setting("bot_token", (token or "").strip())
    await database.set_setting("bot_admins", json.dumps([int(a) for a in admins]))


# ── Stats access helper ────────────────────────────────────────────────────
async def all_links_with_host(host: str) -> dict:
    async with registry.LINKS_LOCK:
        snapshot = dict(registry.LINKS)
    out = []
    for uid, link in snapshot.items():
        item = dict(link)
        item["vless"] = vless_link_for_link(link, uid, host)
        item["sub_name"] = registry.SUBS.get(link.get("sub_id"), {}).get("name")
        item["is_expired"] = registry.is_link_expired(link)
        item["usable"] = registry.is_link_allowed(link)
        item["remaining_bytes"] = max(0, (link.get("limit_bytes", 0) or 0) - (link.get("used_bytes", 0) or 0))
        out.append(item)
    out.sort(key=lambda x: (not x.get("is_default", False), x.get("created_at", "")))
    return {"links": out, "host": host}


async def all_subs() -> dict:
    async with registry.SUBS_LOCK:
        snapshot = dict(registry.SUBS)
    async with registry.LINKS_LOCK:
        links = dict(registry.LINKS)
    out = []
    for sid, sub in snapshot.items():
        item = dict(sub)
        item["links_count"] = sum(1 for l in links.values() if l.get("sub_id") == sid)
        item["has_password"] = bool(sub.get("password_hash"))
        item.pop("password_hash", None)
        out.append(item)
    return {"subs": out}


async def collect_stats() -> dict:
    async with registry.LINKS_LOCK:
        snap = dict(registry.LINKS)
    sub_count = len(registry.SUBS)
    return {
        "active_connections": len(registry.CONNECTIONS),
        "total_traffic_mb": round(registry.STATS["total_bytes"] / (1024 ** 2), 2),
        "total_requests": registry.STATS["total_requests"],
        "total_errors": registry.STATS["total_errors"],
        "uptime": registry.uptime(),
        "timestamp": datetime.now().isoformat(),
        "hourly": dict(registry.HOURLY_TRAFFIC),
        "recent_errors": list(registry.ERROR_LOGS)[-10:],
        "links_count": len(snap),
        "active_links": sum(1 for l in snap.values() if registry.is_link_allowed(l)),
        "expired_links": sum(1 for l in snap.values() if registry.is_link_expired(l)),
        "subs_count": sub_count,
    }