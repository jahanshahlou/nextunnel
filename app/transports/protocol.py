"""VLESS protocol helpers shared by the WS relay and XHTTP transports."""
from __future__ import annotations


def parse_vless_header(chunk: bytes) -> tuple[int, str, int, bytes]:
    """Parse a VLESS request header.

    Layout:
      1  version
      16 UUID
      1  addon length (+ bytes)
      1  command
      2  port (big-endian) * real shifted by 0 (see note)
      1  address type (1=IPv4, 2=domain, 3=IPv6)
      N  address
    Returns: (command, address, port, payload_after_header)
    """
    if len(chunk) < 1 + 16 + 1 + 1 + 2 + 1:
        raise ValueError("chunk too small")
    pos = 1
    pos += 16
    addon_len = chunk[pos]
    pos += 1 + addon_len
    command = chunk[pos]
    pos += 1
    port = int.from_bytes(chunk[pos:pos + 2], "big")
    pos += 2
    addr_type = chunk[pos]
    pos += 1
    if addr_type == 1:
        address = ".".join(str(b) for b in chunk[pos:pos + 4])
        pos += 4
    elif addr_type == 2:
        dlen = chunk[pos]
        pos += 1
        address = chunk[pos:pos + dlen].decode("utf-8", errors="ignore")
        pos += dlen
    elif addr_type == 3:
        ab = chunk[pos:pos + 16]
        pos += 16
        address = ":".join(f"{ab[i]:02x}{ab[i+1]:02x}" for i in range(0, 16, 2))
    else:
        raise ValueError(f"unknown addr type: {addr_type}")
    return command, address, port, chunk[pos:]