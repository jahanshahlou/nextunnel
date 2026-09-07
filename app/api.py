"""REST/JSON API for the NexTunnel panel.

Everything behind the dashboard is gated by the session cookie set at
``POST /api/login`` (PBKDF2-verified, rate-limited, IP-aware). Client-facing
endpoints (subs, share-links, probe) are intentionally open.
"""
from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from . import config, registry, services
from .security import (
    destroy_session,
    create_session,
    is_valid_session,
    login_allowed,
    request_ip,
    verify_password,
)
from .transports.xhttp import _teardown
from .transports.relay import websocket_tunnel
from fastapi import WebSocket

router = APIRouter(prefix="/api")

COOKIE = config.SESSION_COOKIE
SESSION_AGE = config.SESSION_TTL


def _cookie(response: Response, token: str, *, clear: bool = False) -> None:
    if clear:
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax", secure=False)
        return
    response.set_cookie(
        COOKIE, token, max_age=SESSION_AGE, httponly=True,
        samesite="lax", secure=False, path="/",
    )


async def _require_auth(request: Request) -> None:
    if not await is_valid_session(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="not authenticated")


def _host_of(request: Request) -> str:
    return services.resolve_host(request=request)


# ── Auth ───────────────────────────────────────────────────────────────────
class LoginBody(BaseModel):
    password: str


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    ip = request_ip(request)
    if not login_allowed(ip):
        raise HTTPException(status_code=429, detail="too many attempts")
    stored = await services.get_admin_password_hash()
    if not verify_password(body.password, stored):
        registry.log_activity("login", f"ورود ناموفق از {ip}", "err")
        raise HTTPException(status_code=403, detail="wrong password")
    token = create_session()
    _cookie(response, token)
    registry.log_activity("login", f"ورود موفق از {ip}", "info")
    return {"ok": True}


@router.post("/logout")
async def logout(request: Request, response: Response):
    await destroy_session(request.cookies.get(COOKIE))
    _cookie(response, "", clear=True)
    return {"ok": True}


@router.get("/session")
async def session(request: Request):
    ok = await is_valid_session(request.cookies.get(COOKIE))
    return {"authenticated": ok}


# ── Dashboard data ─────────────────────────────────────────────────────────
@router.get("/stats")
async def stats(request: Request):
    await _require_auth(request)
    return await services.collect_stats()


@router.get("/links")
async def links(request: Request):
    await _require_auth(request)
    await services.ensure_default_link()
    return await services.all_links_with_host(_host_of(request))


@router.get("/subs")
async def subs(request: Request):
    await _require_auth(request)
    return await services.all_subs()


@router.get("/connections")
async def connections(request: Request):
    await _require_auth(request)
    rows = []
    for cid, c in registry.CONNECTIONS.items():
        link = registry.LINKS.get(c.get("uuid"), {})
        label = link.get("label", "?")
        rows.append({
            "id": cid,
            "uuid": c.get("uuid"),
            "label": label,
            "ip": c.get("ip"),
            "transport": c.get("transport"),
            "bytes": c.get("bytes", 0),
            "connected_at": c.get("connected_at"),
        })
    return {"connections": rows, "count": len(rows)}


@router.get("/activity")
async def activity(request: Request):
    await _require_auth(request)
    return {"activity": list(registry.ACTIVITY_LOGS)}


# ── Link management ────────────────────────────────────────────────────────
class LinkCreateBody(BaseModel):
    label: str = ""
    limit_value: float = 0
    limit_unit: str = "GB"
    expires_days: int = 0
    note: str = ""
    sub_id: str | None = None
    protocol: str = config.DEFAULT_PROTOCOL
    fingerprint: str = config.DEFAULT_FINGERPRINT
    alpn: str = ""
    port: int = config.DEFAULT_PORT
    ip_limit: int = 0
    speed_limit_value: float = 0
    speed_limit_unit: str = "MBIT"


@router.post("/links")
async def create_link(body: LinkCreateBody, request: Request):
    await _require_auth(request)
    link = await services.create_link(
        label=body.label,
        limit_bytes=(
            0 if body.limit_value <= 0
            else services.parse_size_to_bytes(body.limit_value, body.limit_unit)
        ),
        expires_at=(
            (registry.now_ir() + timedelta(days=body.expires_days)).isoformat()
            if body.expires_days > 0 else None
        ),
        note=body.note,
        sub_id=body.sub_id,
        protocol=body.protocol,
        fingerprint=body.fingerprint,
        alpn=body.alpn,
        port=body.port,
        ip_limit=body.ip_limit,
        speed_limit_bytes=(
            0 if body.speed_limit_value <= 0
            else services.parse_speed_to_bytes(body.speed_limit_value, body.speed_limit_unit)
        ),
    )
    link["vless"] = services.vless_link_for_link(link, link["uuid"], _host_of(request))
    return link


class LinkToggleBody(BaseModel):
    active: bool


@router.post("/links/{uid}/toggle")
async def toggle_link(uid: str, body: LinkToggleBody, request: Request):
    await _require_auth(request)
    link = await services.set_link_active(uid, body.active)
    if link is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "active": link["active"]}


class LinkUpdateBody(BaseModel):
    label: str | None = None
    note: str | None = None
    active: bool | None = None
    reset_usage: bool = False
    limit_value: float | None = None
    limit_unit: str | None = None
    expires_days: int | None = None
    fingerprint: str | None = None
    alpn: str | None = None
    port: int | None = None
    ip_limit: int | None = None
    speed_limit_value: float | None = None
    speed_limit_unit: str | None = None
    sub_id: str | None = None


@router.post("/links/{uid}/update")
async def update_link(uid: str, body: LinkUpdateBody, request: Request):
    await _require_auth(request)
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    link = await services.update_link(uid, **fields)
    if link is None:
        raise HTTPException(status_code=404, detail="not found")
    return link


@router.post("/links/{uid}/delete")
async def delete_link(uid: str, request: Request):
    await _require_auth(request)
    label = await services.remove_link(uid)
    if label is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "deleted": label}


# ── Groups ────────────────────────────────────────────────────────────────
class SubCreateBody(BaseModel):
    name: str = ""
    desc: str = ""
    password: str = ""


@router.post("/subs")
async def create_sub(body: SubCreateBody, request: Request):
    await _require_auth(request)
    sub = await services.create_sub_group(body.name, body.desc, body.password)
    return sub


class SubUpdateBody(BaseModel):
    name: str | None = None
    desc: str | None = None
    password: str | None = None


@router.post("/subs/{sid}/update")
async def update_sub(sid: str, body: SubUpdateBody, request: Request):
    await _require_auth(request)
    sub = await services.update_sub(sid, **body.model_dump(exclude_none=True))
    if sub is None:
        raise HTTPException(status_code=404, detail="not found")
    return sub


@router.post("/subs/{sid}/delete")
async def delete_sub(sid: str, request: Request):
    await _require_auth(request)
    name = await services.remove_sub_group(sid)
    if name is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True, "deleted": name}


class SubLinkBody(BaseModel):
    sub_id: str | None = None


@router.post("/links/{uid}/sub")
async def set_sub(uid: str, body: SubLinkBody, request: Request):
    await _require_auth(request)
    ok = await services.set_link_sub(uid, body.sub_id)
    if not ok:
        raise HTTPException(status_code=404, detail="link or sub not found")
    return {"ok": True}


# ── Connections ────────────────────────────────────────────────────────────
@router.post("/connections/{conn_id}/close")
async def close_connection(conn_id: str, request: Request):
    await _require_auth(request)
    removed = registry.CONNECTIONS.pop(conn_id, None)
    if removed is None:
        # an XHTTP session may own it; try teardown by session
        found = None
        for sid, s in registry.XHTTP_SESSIONS.items():
            if s.get("conn_id") == conn_id:
                found = (sid, s)
                break
        if found is None:
            raise HTTPException(status_code=404, detail="connection not found")
        await _teardown(found[0])
    registry.log_activity("connection", "اتصال به‌صورت دستی قطع شد", "warn")
    return {"ok": True}


# ── Bot config (editable from panel) ───────────────────────────────────────
class BotBody(BaseModel):
    token: str
    admins: list


@router.get("/bot")
async def bot(request: Request):
    await _require_auth(request)
    state = await _import_bot_manager()
    cfg = await services.get_bot_config()
    cfg["running"] = state.running
    return cfg


@router.post("/bot")
async def save_bot(body: BotBody, request: Request):
    await _require_auth(request)
    await services.save_bot_config(body.token, body.admins)
    manager = await _import_bot_manager()
    await manager.restart()
    registry.log_activity("bot", "تنظیمات ربات تلگرام ذخیره و اعمال شد", "info")
    return {"ok": True, "running": manager.running}


async def _import_bot_manager():
    from .bot.manager import manager as m
    return m


# ── Password ──────────────────────────────────────────────────────────────
class PasswordBody(BaseModel):
    current: str
    new: str


@router.post("/password")
async def change_password(body: PasswordBody, request: Request):
    await _require_auth(request)
    ok = await services.change_admin_password(body.current, body.new)
    if not ok:
        raise HTTPException(status_code=400, detail="invalid current password or too short")
    registry.log_activity("login", "رمز عبور پنل تغییر کرد", "info")
    return {"ok": True}


# ── Public: host info (helps clients build correct links) ─────────────────
@router.get("/host")
async def host(request: Request):
    return {"host": _host_of(request)}


# ── Public: subscription & single-link endpoints ──────────────────────────
@router.get("/sub/{sub_id}")
async def subscription(sub_id: str, request: Request):
    sub = registry.SUBS.get(sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="group not found")
    host = _host_of(request)
    async with registry.LINKS_LOCK:
        in_group = [l for l in registry.LINKS.values() if l.get("sub_id") == sub_id and registry.is_link_allowed(l)]
    lines = [services.vless_link_for_link(l, l["uuid"], host) for l in in_group]
    return Response(content="\n".join(lines).encode(), media_type="text/plain")


@router.get("/sub/{sub_id}/config")
async def sub_config(sub_id: str, request: Request):
    sub = registry.SUBS.get(sub_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="group not found")
    return {"sub_id": sub_id, "name": sub.get("name"), "desc": sub.get("desc")}


@router.get("/link/{uid}/config")
async def link_config(uid: str, request: Request):
    link = registry.LINKS.get(uid)
    if link is None or not registry.is_link_allowed(link):
        raise HTTPException(status_code=404, detail="link not found")
    host = _host_of(request)
    return {
        "url": services.vless_link_for_link(link, uid, host),
        "protocol": link.get("protocol"),
        "transport": "ws" if link.get("protocol") == "vless-ws" else "xhttp",
    }