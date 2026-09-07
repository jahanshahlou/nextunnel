"""VLESS over WebSocket relay — the battle-tested hot path from the original,
re-homed onto the registry so there are no circular imports.

Security fixes applied:
  * real client IP uses the trusted-proxy logic (last XFF entry)
  * flow control, quota/IP checks, and socket tuning are preserved unchanged
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import socket
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

from .. import registry
from ..config import DEFAULT_PROTOCOL, RELAY_BUF
from ..security import client_ip as security_client_ip
from .protocol import parse_vless_header
from .throttle import throttle

logger = logging.getLogger("NexTunnel.relay")


def _ws_client_ip(ws: WebSocket) -> str:
    """Real client IP behind reverse proxy (last XFF entry, not the first)."""
    ip = security_client_ip(ws.headers)
    if ip == "unknown":
        return ws.client.host if ws.client else "نامشخص"
    return ip


async def relay_ws_to_tcp(ws: WebSocket, writer: asyncio.StreamWriter, conn_id: str, uid: str):
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            if not data:
                continue
            if not registry.check_and_use(uid, len(data)):
                await ws.close(code=1008, reason="quota/disabled/unknown")
                break
            await throttle(uid, len(data))
            registry.STATS["total_requests"] += 1
            conn = registry.CONNECTIONS.get(conn_id)
            if conn:
                conn["bytes"] += len(data)
            writer.write(data)
            if writer.transport.get_write_buffer_size() > RELAY_BUF:
                await writer.drain()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        try:
            writer.write_eof()
        except Exception:
            pass


async def relay_tcp_to_ws(ws: WebSocket, reader: asyncio.StreamReader, conn_id: str, uid: str):
    first = True
    try:
        while True:
            data = await reader.read(RELAY_BUF)
            if not data:
                break
            if not registry.check_and_use(uid, len(data)):
                await ws.close(code=1008, reason="quota/disabled/unknown")
                break
            await throttle(uid, len(data))
            conn = registry.CONNECTIONS.get(conn_id)
            if conn:
                conn["bytes"] += len(data)
            payload = (b"\x00\x00" + data) if first else data
            first = False
            await ws.send_bytes(payload)
    except Exception:
        pass


async def websocket_tunnel(ws: WebSocket, uuid: str):
    await ws.accept()

    link = registry.LINKS.get(uuid)
    if not registry.is_link_allowed(link):
        logger.warning(f"WS rejected uuid={uuid[:8]}… (not allowed)")
        await ws.close(code=1008, reason="not authorized")
        return

    ip = _ws_client_ip(ws)

    if not registry.is_ip_allowed(link, uuid, ip):
        logger.warning(f"WS rejected uuid={uuid[:8]}… ip={ip} (ip limit reached)")
        registry.log_activity(
            "connection",
            f"اتصال {ip} به کانفیگ «{link.get('label','?')}» رد شد (محدودیت تعداد آی‌پی)",
            "warn",
        )
        await ws.close(code=1008, reason="ip limit reached")
        return

    conn_id = secrets.token_urlsafe(6)
    registry.CONNECTIONS[conn_id] = {
        "uuid": uuid,
        "ip": ip,
        "transport": "vless-ws",
        "connected_at": datetime.now().isoformat(),
        "bytes": 0,
    }
    logger.info(f"WS [{conn_id}] uuid={uuid[:8]}… ip={ip} total={len(registry.CONNECTIONS)}")
    registry.log_activity("connection", f"اتصال جدید از {ip} (کانفیگ {link.get('label','?')})", "info")
    writer = None

    try:
        first_msg = await asyncio.wait_for(ws.receive(), timeout=15.0)
        if first_msg["type"] == "websocket.disconnect":
            return
        first_chunk = first_msg.get("bytes") or (first_msg.get("text") or "").encode()
        if not first_chunk:
            return

        command, address, port, payload = await parse_vless_header(first_chunk)

        if not registry.check_and_use(uuid, len(first_chunk)):
            await ws.close(code=1008, reason="quota/disabled")
            return

        registry.STATS["total_requests"] += 1
        conn = registry.CONNECTIONS.get(conn_id)
        if conn:
            conn["bytes"] += len(first_chunk)
        logger.info(f"➡️  [{conn_id}] → {address}:{port}")

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port),
            timeout=10.0,
        )
        sock = writer.transport.get_extra_info("socket")
        if sock:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        if payload:
            writer.write(payload)
            await writer.drain()

        done, pending = await asyncio.wait(
            {
                asyncio.create_task(relay_ws_to_tcp(ws, writer, conn_id, uuid)),
                asyncio.create_task(relay_tcp_to_ws(ws, reader, conn_id, uuid)),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        registry.STATS["total_errors"] += 1
        registry.ERROR_LOGS.appendleft({"error": "connection timeout", "time": datetime.now().isoformat()})
    except Exception as exc:
        registry.STATS["total_errors"] += 1
        registry.ERROR_LOGS.appendleft({"error": str(exc), "time": datetime.now().isoformat()})
        logger.error(f"WS error [{conn_id}]: {exc}")
    finally:
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        registry.CONNECTIONS.pop(conn_id, None)
        logger.info(f"WS closed [{conn_id}] total={len(registry.CONNECTIONS)}")