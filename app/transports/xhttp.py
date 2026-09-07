"""XHTTP (Siz10a) transport — packet-up / stream-up modes with adaptive flow &
quota batching. Ported from the working original; bug fixes applied:

  * stale-session reaper now also expires idle *established* sessions
  * `connections[conn_id]` lookups are guarded (removed the KeyError race)
  * real client IP uses the trusted-proxy logic
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import time
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, WebSocket
from fastapi.responses import StreamingResponse

from .. import registry, config
from ..security import client_ip as security_client_ip
from .protocol import parse_vless_header
from .throttle import throttle

logger = logging.getLogger("NexTunnel.xhttp")

router = APIRouter()

FINGERPRINT_HEADERS = {
    "chrome": {
        "content-type": "application/grpc",
        "cache-control": "no-cache, no-store",
        "x-accel-buffering": "no",
        "server": "cloudflare",
    },
    "plain": {
        "content-type": "application/octet-stream",
        "cache-control": "no-store",
        "x-accel-buffering": "no",
    },
}


def _resp_headers(fp: str) -> dict:
    return dict(FINGERPRINT_HEADERS.get(fp, FINGERPRINT_HEADERS["chrome"]))


def _tune_socket(writer: asyncio.StreamWriter):
    sock = writer.transport.get_extra_info("socket")
    if not sock:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, config.SOCK_BUF_SIZE)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, config.SOCK_BUF_SIZE)
    except OSError:
        pass


class _QuotaGate:
    """Adaptive batch quota: measures real per-session rate (EWMA) and adjusts
    the batch size so fast sessions do fewer awaits and slow ones check quota
    more precisely. No data is ever buffered — only the *check* timing adapts.
    """
    __slots__ = ("uuid", "pending", "last_check", "ok", "batch_bytes", "rate_ewma")

    def __init__(self, uuid: str):
        self.uuid = uuid
        self.pending = 0
        self.last_check = time.monotonic()
        self.ok = True
        self.batch_bytes = config.QUOTA_START_BATCH
        self.rate_ewma = 0.0

    async def add(self, nbytes: int) -> bool:
        if not self.ok:
            return False
        self.pending += nbytes
        now = time.monotonic()
        elapsed = now - self.last_check
        if self.pending >= self.batch_bytes or elapsed >= config.QUOTA_CHECK_INTERVAL:
            flush, self.pending = self.pending, 0
            if elapsed > 0:
                inst_rate = flush / elapsed
                self.rate_ewma = inst_rate if self.rate_ewma == 0 else (0.7 * self.rate_ewma + 0.3 * inst_rate)
                target = int(self.rate_ewma * config.QUOTA_CHECK_INTERVAL)
                self.batch_bytes = max(config.QUOTA_MIN_BATCH, min(config.QUOTA_MAX_BATCH, target or config.QUOTA_MIN_BATCH))
            self.last_check = now
            self.ok = await asyncio.to_thread(registry.check_and_use, self.uuid, flush)
            return self.ok
        return True

    async def flush(self) -> bool:
        if self.pending:
            flush, self.pending = self.pending, 0
            self.ok = self.ok and await asyncio.to_thread(registry.check_and_use, self.uuid, flush)
        return self.ok


class _AdaptiveFlow:
    """AIMD-style high-water mark for writer.drain(). Fast drains increase the
    watermark (fewer syscalls), real backpressure halves it instantly.
    """
    __slots__ = ("high_water", "last_drain_ms")

    def __init__(self):
        self.high_water = config.FLOW_START_HW
        self.last_drain_ms = 0.0

    def should_drain(self, buf_size: int) -> bool:
        return buf_size > self.high_water

    async def drain(self, writer: asyncio.StreamWriter):
        t0 = time.monotonic()
        await writer.drain()
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.last_drain_ms = elapsed_ms
        if elapsed_ms < config.FLOW_FAST_DRAIN_MS:
            self.high_water = min(config.FLOW_MAX_HW, int(self.high_water * 1.5) + 65536)
        elif elapsed_ms > config.FLOW_SLOW_DRAIN_MS:
            self.high_water = max(config.FLOW_MIN_HW, self.high_water // 2)


def _req_ip(request: Request) -> str:
    ip = security_client_ip(request.headers)
    if ip == "unknown":
        return request.client.host if request.client else "نامشخص"
    return ip


async def _open_tcp_from_header(first_chunk: bytes):
    command, address, port, payload = await parse_vless_header(first_chunk)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(address, port), timeout=config.TCP_CONNECT_TIMEOUT
    )
    _tune_socket(writer)
    if payload:
        writer.write(payload)
        await writer.drain()
    return reader, writer, address, port


async def _check_link(uuid: str):
    if not registry.is_link_allowed(registry.LINKS.get(uuid)):
        raise HTTPException(status_code=403, detail="not authorized")


async def _get_or_create_session(uuid: str, mode: str, session_id: str, ip: str) -> dict:
    async with registry.XHTTP_LOCK:
        sess = registry.XHTTP_SESSIONS.get(session_id)
        if sess is not None:
            sess["last_seen"] = time.time()
            return sess

        link = registry.LINKS.get(uuid)
        if not registry.is_ip_allowed(link, uuid, ip):
            logger.warning(f"XHTTP[{mode}] rejected uuid={uuid[:8]} ip={ip} (ip limit reached)")
            raise HTTPException(status_code=403, detail="ip limit reached")

        conn_id = secrets.token_urlsafe(6)
        registry.CONNECTIONS[conn_id] = {
            "uuid": uuid,
            "ip": ip,
            "connected_at": datetime.now().isoformat(),
            "bytes": 0,
            "transport": f"xhttp-{mode}",
        }
        sess = {
            "uuid": uuid, "mode": mode, "writer": None,
            "downlink_task": None, "uplink_task": None,
            "down_q": asyncio.Queue(maxsize=config.XHTTP_QUEUE_MAX),
            "last_seen": time.time(),
            "conn_id": conn_id, "tcp_open": False, "closed": False,
            "seq_buf": {}, "next_seq": 0,
            "gate": None, "flow": None,
        }
        registry.XHTTP_SESSIONS[session_id] = sess
        logger.info(f"new XHTTP[{mode}] session [{session_id[:8]}] uuid={uuid[:8]} ip={ip}")
        return sess


async def _teardown(session_id: str):
    sess = registry.XHTTP_SESSIONS.pop(session_id, None)
    if not sess:
        return
    sess["closed"] = True
    for t in ("uplink_task", "downlink_task"):
        task = sess.get(t)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    writer = sess.get("writer")
    if writer:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
    registry.CONNECTIONS.pop(sess.get("conn_id"), None)
    dq = sess.get("down_q")
    if dq:
        try:
            dq.put_nowait(None)
        except Exception:
            pass
    logger.info(f"closed XHTTP[{sess.get('mode')}] [{session_id[:8]}] total={len(registry.XHTTP_SESSIONS)}")


async def _reaper():
    while True:
        await asyncio.sleep(config.CONNECTION_REAP_INTERVAL)
        now = time.time()
        stale = [sid for sid, s in registry.XHTTP_SESSIONS.items() if now - s["last_seen"] > config.XHTTP_SESSION_IDLE]
        for sid in stale:
            logger.info(f"reaping idle XHTTP session [{sid[:8]}]")
            await _teardown(sid)


_reaper_started = False


def ensure_reaper():
    global _reaper_started
    if not _reaper_started:
        asyncio.create_task(_reaper())
        _reaper_started = True


async def _pump_tcp_to_queue(session_id: str, uuid: str, reader: asyncio.StreamReader, down_q: asyncio.Queue):
    first = True
    gate = _QuotaGate(uuid)
    try:
        while True:
            data = await reader.read(config.XHTTP_BUF)
            if not data:
                break
            if not await gate.add(len(data)):
                break
            await throttle(uuid, len(data))
            sess = registry.XHTTP_SESSIONS.get(session_id)
            if sess:
                c = registry.CONNECTIONS.get(sess["conn_id"])
                if c:
                    c["bytes"] += len(data)
                sess["last_seen"] = time.time()
            payload = (b"\x00\x00" + data) if first else data
            first = False
            await down_q.put(payload)
    except (asyncio.CancelledError, Exception):
        pass
    finally:
        await gate.flush()
        await _teardown(session_id)


async def _open_tcp_for_session(session_id: str, uuid: str, sess: dict, first_chunk: bytes):
    reader, writer, address, port = await _open_tcp_from_header(first_chunk)
    logger.info(f"connect XHTTP[{sess['mode']}] [{session_id[:8]}] -> {address}:{port}")
    sess["writer"] = writer
    sess["tcp_open"] = True
    sess["downlink_task"] = asyncio.create_task(
        _pump_tcp_to_queue(session_id, uuid, reader, sess["down_q"])
    )


def _downstream_gen(sess: dict):
    async def gen():
        try:
            while True:
                chunk = await sess["down_q"].get()
                if chunk is None:
                    break
                sess["last_seen"] = time.time()
                yield chunk
        finally:
            pass
    return gen()


# ── Shared downlink GET ────────────────────────────────────────────────────
@router.get("/xhttp-siz10/{mode}/{uuid}/{session_id}")
async def xhttp_downlink(mode: str, uuid: str, session_id: str, request: Request):
    ensure_reaper()
    if mode not in ("packet-up", "stream-up"):
        raise HTTPException(status_code=404, detail="unknown mode")
    await _check_link(uuid)
    fp = request.query_params.get("fp", "chrome")
    sess = await _get_or_create_session(uuid, mode, session_id, _req_ip(request))
    if sess.get("closed"):
        raise HTTPException(status_code=404, detail="session closed")
    headers = _resp_headers(fp)
    return StreamingResponse(_downstream_gen(sess), headers=headers, media_type=headers["content-type"])


# ── PACKET-UP upload ───────────────────────────────────────────────────────
@router.post("/xhttp-siz10/packet-up/{uuid}/{session_id}/{seq}")
async def packet_up_upload(uuid: str, session_id: str, seq: int, request: Request):
    ensure_reaper()
    sess = await _get_or_create_session(uuid, "packet-up", session_id, _req_ip(request))
    if sess.get("closed"):
        raise HTTPException(status_code=404, detail="session closed")

    sess["last_seen"] = time.time()
    body = await request.body()
    if not body:
        return {"ok": True}

    if not await asyncio.to_thread(registry.check_and_use, uuid, len(body)):
        await _teardown(session_id)
        raise HTTPException(status_code=403, detail="quota/disabled/unknown")
    await throttle(uuid, len(body))

    registry.STATS["total_requests"] += 1
    c = registry.CONNECTIONS.get(sess["conn_id"])
    if c:
        c["bytes"] += len(body)

    try:
        if sess["writer"] is None:
            # First packet that carries the VLESS header; seq may miss 0 if
            # packets arrive out of order — buffer the premature ones.
            if seq != 0:
                sess["seq_buf"][seq] = body
                return {"ok": True, "buffered": True}
            await _open_tcp_for_session(session_id, uuid, sess, body)
            nxt = 1
            while nxt in sess["seq_buf"]:
                sess["writer"].write(sess["seq_buf"].pop(nxt))
                nxt += 1
            sess["next_seq"] = nxt
            return {"ok": True, "connected": True}

        if seq == sess["next_seq"]:
            sess["writer"].write(body)
            sess["next_seq"] += 1
            while sess["next_seq"] in sess["seq_buf"]:
                sess["writer"].write(sess["seq_buf"].pop(sess["next_seq"]))
                sess["next_seq"] += 1
        else:
            sess["seq_buf"][seq] = body

        if sess["writer"].transport.get_write_buffer_size() > config.PACKET_UP_HIGH_WATER:
            await sess["writer"].drain()
    except Exception as exc:
        registry.ERROR_LOGS.appendleft({"error": str(exc), "time": datetime.now().isoformat()})
        await _teardown(session_id)
        raise HTTPException(status_code=502, detail="write failed")

    return {"ok": True}


# ── STREAM-UP upload (single continuous POST) ─────────────────────────────
@router.post("/xhttp-siz10/stream-up/{uuid}/{session_id}")
async def stream_up_upload(uuid: str, session_id: str, request: Request):
    ensure_reaper()
    sess = await _get_or_create_session(uuid, "stream-up", session_id, _req_ip(request))
    if sess.get("closed"):
        raise HTTPException(status_code=404, detail="session closed")

    gate = sess.get("gate") or _QuotaGate(uuid)
    sess["gate"] = gate
    flow = sess.get("flow") or _AdaptiveFlow()
    sess["flow"] = flow

    conn = registry.CONNECTIONS.get(sess["conn_id"])
    writer = sess["writer"]

    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            sess["last_seen"] = time.time()

            if not await gate.add(len(chunk)):
                raise HTTPException(status_code=403, detail="quota/disabled/unknown")
            await throttle(uuid, len(chunk))

            registry.STATS["total_requests"] += 1
            conn = registry.CONNECTIONS.get(sess["conn_id"])
            if conn:
                conn["bytes"] += len(chunk)

            if writer is None:
                await _open_tcp_for_session(session_id, uuid, sess, chunk)
                writer = sess["writer"]
                continue

            writer.write(chunk)
            if flow.should_drain(writer.transport.get_write_buffer_size()):
                await flow.drain(writer)
    except HTTPException:
        await gate.flush()
        await _teardown(session_id)
        raise
    except Exception as exc:
        registry.ERROR_LOGS.appendleft({"error": str(exc), "time": datetime.now().isoformat()})
        await gate.flush()
        await _teardown(session_id)
        raise HTTPException(status_code=502, detail="stream error")

    await gate.flush()
    return {"ok": True}


# WebSocket heartbeat probe (used by the dashboard's connectivity tester)
@router.websocket("/xhttp-siz10/probe")
async def ws_probe(ws: WebSocket):
    await ws.accept()
    await ws.send_json({"ok": True})
    await ws.close()