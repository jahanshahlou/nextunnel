"""HTTP forward proxy used by the client-facing side.

Fixes the original's issues:
  * full-body buffering unbounded -> streamed relay with a body cap (4 MB)
  * open SSRF                    -> destination must resolve to a public address;
                                    private/link-local/loopback/CGNAT/multicast
                                    ranges are rejected
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

from fastapi import APIRouter, Request
from fastapi.responses import Response

logger = logging.getLogger("NexTunnel.proxy")

router = APIRouter()

MAX_BODY = 4 * 1024 * 1024

_BLOCKED_NETS = [
    ipaddress.ip_network(r)
    for r in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",       # private
        "127.0.0.0/8",                                         # loopback
        "169.254.0.0/16",                                      # link-local
        "100.64.0.0/10",                                       # shared CGNAT
        "0.0.0.0/8", "192.0.0.0/24", "192.0.2.0/24",
        "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
        "224.0.0.0/4", "240.0.0.0/4",                          # multicast/reserved
        "::1/128", "fc00::/7", "fe80::/10",
    )
]


def _is_blocked(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return True
    for _fam, _type, _proto, _canon, sockaddr in infos:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            return True
        for net in _BLOCKED_NETS:
            if ip in net:
                return True
    return False


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "-"


@router.api_route("/proxy/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(request: Request, path: str):
    url = path
    if "://" not in url:
        url = "http://" + url
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        target_host = parts.hostname or ""
        target_port = parts.port or (443 if parts.scheme == "https" else 80)
        url_path = parts.path or "/"
        if parts.query:
            url_path += "?" + parts.query
    except ValueError:
        return Response("bad url", status_code=400)
    logger.info("PROXY request %s target=%s:%s", request.method, target_host, target_port)

    if not target_host or " " in target_host or parts.scheme not in ("http", "https"):
        return Response("unsupported target", status_code=400)
    if _is_blocked(target_host):
        logger.warning("PROXY blocked SSRF target %s", target_host)
        return Response("target not allowed", status_code=403)

    # Stream request body with a hard cap so memory stays bounded.
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            return Response("body too large", status_code=413)

    try:
        async with asyncio.timeout(30):
            reader, writer = await asyncio.open_connection(target_host, target_port)
    except Exception as exc:
        logger.error("PROXY connect failed %s: %s", target_host, exc)
        return Response("connect failed", status_code=502)

    req_head = (
        f"{request.method} {url_path} HTTP/1.1\r\n"
        f"Host: {target_host}\r\n"
    )
    for k, v in request.headers.items():
        if k.lower() in ("host", "content-length", "connection", "proxy-connection"):
            continue
        req_head += f"{k}: {v}\r\n"
    req_head += f"X-Forwarded-For: {_client_ip(request)}\r\nConnection: close\r\n"
    if body:
        req_head += f"Content-Length: {len(body)}\r\n"

    writer.write(req_head.encode("latin-1", errors="replace") + b"\r\n" + body)
    await writer.drain()

    # ── Read response: headers, then body (chunked-aware, capped) ─────────
    try:
        buf = bytearray()

        # Read until the CRLFCRLF header terminator is present.
        async with asyncio.timeout(30):
            while b"\r\n\r\n" not in buf:
                chunk = await reader.read(2048)
                if not chunk:
                    return Response("no response", status_code=502)
                buf += chunk

        head_raw, _, body_seed = bytes(buf).partition(b"\r\n\r\n")
        lines = head_raw.decode("latin-1", errors="replace").split("\r\n")
        status_code = 502
        if lines and " " in lines[0]:
            try:
                status_code = int(lines[0].split(" ", 2)[1])
            except ValueError:
                status_code = 502
        resp_headers = {}
        chunked = False
        content_length = None
        for line in lines[1:]:
            k, _, v = line.partition(":")
            kl = (k or "").strip().lower()
            if kl == "transfer-encoding":
                if "chunked" in v.lower():
                    chunked = True
                continue
            if kl in ("connection", "keep-alive"):
                continue
            if kl == "content-length":
                try:
                    content_length = int(v.strip())
                except ValueError:
                    pass
            resp_headers[k.strip()] = v.strip()

        # Accumulate the full response body (bounded) so we can de-chunk.
        full = bytearray(body_seed)
        async with asyncio.timeout(30):
            while True:
                if chunked and full.endswith(b"\r\n0\r\n\r\n"):
                    break
                if content_length is not None and len(full) >= content_length:
                    break
                if len(full) >= MAX_BODY:
                    break
                chunk = await reader.read(65536)
                if not chunk:
                    break
                full += chunk

        if chunked:
            # In-memory de-chunking: <hex>\r\n<data>\r\n … 0\r\n\r\n
            out = bytearray()
            pos = 0
            try:
                while True:
                    eol = full.find(b"\r\n", pos)
                    if eol == -1:
                        break
                    size = int(full[pos:eol].split(b";", 1)[0], 16)
                    pos = eol + 2
                    if size == 0:
                        break
                    out += full[pos:pos + size]
                    pos += size + 2
                full = out
            except (ValueError, IndexError):
                pass
        body_resp = bytes(full)
    except Exception as exc:
        logger.error("PROXY read failed: %s", exc)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        return Response("proxy error", status_code=502)

    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

    reason = lines[0].split(" ", 2)[2] if len(lines) > 2 and len(lines[0].split(" ", 2)) > 2 else ""
    logger.info("PROXY %s → %s (%d %s, %d bytes)", request.method, target_host, status_code, reason, len(body_resp))
    return Response(content=body_resp, status_code=status_code, headers=resp_headers)