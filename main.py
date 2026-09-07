"""NexTunnel — multi-protocol tunnel gateway (FastAPI).

Entry point wiring: API routes, transports, proxy, web UI, background tasks
(traffic sync, session cleanup, XHTTP reaper) and the Telegram bot lifecycle.
Run:  `python main.py`  (or `uvicorn main:app --host 0.0.0.0 --port $PORT`)
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI
from starlette.websockets import WebSocket
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import APP_NAME, __version__ as VERSION
from app import config, database, registry, services
from app.api import router as api_router
from app.proxy import router as proxy_router
from app.security import purge_expired_sessions, purge_rate_limits
from app.transports import xhttp
from app.transports.relay import websocket_tunnel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("NexTunnel")

app = FastAPI(title=APP_NAME, version=VERSION, docs_url=None, redoc_url=None)

APP_DIR = Path(__file__).resolve().parent / "app"
TEMPLATES = APP_DIR / "web" / "templates"
STATIC = APP_DIR / "web" / "static"

# ── Background tasks ──────────────────────────────────────────────────────
async def _traffic_sync():
    while True:
        await asyncio.sleep(config.TRAFFIC_SYNC_INTERVAL)
        try:
            await database.flush_all_links()
        except Exception as exc:
            logger.error("traffic sync failed: %s", exc)


async def _session_cleanup():
    while True:
        await asyncio.sleep(300)
        await purge_expired_sessions()
        await purge_rate_limits()


# ── Lifecycle ─────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    await database.init()
    await database.load_links()
    await database.load_subs()
    await database.load_stats()
    await services.ensure_default_link()
    registry.log_activity("system", f"NexTunnel {VERSION} راه‌اندازی شد", "info")

    asyncio.create_task(_traffic_sync())
    asyncio.create_task(_session_cleanup())
    xhttp.ensure_reaper()

    from app.bot.manager import manager as bot_manager
    try:
        await bot_manager.start()
    except Exception as exc:
        logger.error("bot failed to start: %s", exc)


@app.on_event("shutdown")
async def shutdown():
    from app.bot.manager import manager as bot_manager
    await bot_manager.stop()
    try:
        await database.flush_all_links()
    except Exception as exc:
        logger.error("final flush failed: %s", exc)
    await database.close()


# ── Routers ───────────────────────────────────────────────────────────────
app.include_router(api_router)
app.include_router(proxy_router)
app.include_router(xhttp.router)

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def _template(name: str) -> HTMLResponse:
    return HTMLResponse((TEMPLATES / name).read_text(encoding="utf-8"))


@app.get("/", response_class=HTMLResponse)
async def index():
    return _template("dashboard.html")


@app.get("/login", response_class=HTMLResponse)
async def login():
    return _template("login.html")


@app.get("/public", response_class=HTMLResponse)
async def public_page():
    html = (TEMPLATES / "public.html").read_text(encoding="utf-8")
    html = (html
            .replace("${host}", registry.PUBLIC_HOST or config.HOST_FALLBACK)
            .replace("${port}", str(config.PORT)))
    return HTMLResponse(html)


# ── Transports: raw WebSocket tunnel endpoint ────────────────────────────
@app.websocket("/ws/{uuid}")
async def ws_tunnel(ws: WebSocket, uuid: str):
    await websocket_tunnel(ws, uuid)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)