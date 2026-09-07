"""NexTunnel - Advanced Multi-Protocol Tunnel Gateway

A professional re-architecture of the X4G VLESS gateway:
  * Modular package layout (no circular imports)
  * SQLite persistence (aiosqlite) instead of a fragile JSON file
  * PBKDF2 password hashing, secure sessions, login rate-limiting
  * Trusted-proxy aware client IP resolution (fixes IP-limit bypass)
  * Telegram bot fully configurable from the web panel
  * Mobile-first, fully responsive web UI
"""
from __future__ import annotations

__version__ = "1.0.0"
APP_NAME = "NexTunnel"