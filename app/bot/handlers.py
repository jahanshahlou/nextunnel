"""Telegram bot handlers — admin-only link management, ported from the working
original and re-homed onto the shared services layer.
"""
from __future__ import annotations

import re

from .. import registry, services


def _find_link(query: str):
    """Locate a config by uuid prefix or label (case-insensitive)."""
    q = query.strip().lower()
    for uid, link in list(registry.LINKS.items()):
        if uid.lower().startswith(q):
            return uid, link
    for uid, link in list(registry.LINKS.items()):
        if link.get("label", "").lower() == q:
            return uid, link
    for uid, link in list(registry.LINKS.items()):
        if q in link.get("label", "").lower():
            return uid, link
    return None, None


def _fmt_link(uid: str, link: dict) -> str:
    used = registry.fmt_bytes(link.get("used_bytes", 0))
    limit = registry.fmt_bytes(link.get("limit_bytes", 0)) if link.get("limit_bytes") else "∞"
    state = "✅" if registry.is_link_allowed(link) else "⛔️"
    exp = link.get("expires_at", "") or "بدون انقضا"
    return (
        f"{state} <b>{link.get('label','?')}</b>\n"
        f"🆔 <code>{uid[:8]}</code>\n"
        f"📊 مصرف: {used} / {limit}\n"
        f"⏰ انقضا: {exp}"
    )


async def services_handle_command(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/start", "/help"):
        return (
            "🤖 <b>NexTunnel Bot</b>\n"
            "مدیریت کانفیگ‌ها از تلگرام:\n\n"
            "/links — لیست کانفیگ‌ها\n"
            "/link نام — لینک اتصال\n"
            "/sub نام — لینک اشتراک گروه\n"
            "/add نام حجمGB — ساخت کانفیگ\n"
            "/on نام | /off نام — فعال/غیرفعال\n"
            "/del نام — حذف\n"
            "/usage — ترافیک کانفیگ‌ها\n"
            "/stats — وضعیت سرور"
        )

    if cmd == "/links":
        if not registry.LINKS:
            return "کانفیگی وجود ندارد."
        lines = ["📋 <b>کانفیگ‌ها</b>:\n"]
        for uid, link in list(registry.LINKS.items()):
            lines.append(_fmt_link(uid, link))
        return "\n\n".join(lines)

    if cmd == "/link":
        if not arg:
            return "شناسه یا نام کانفیگ را ارسال کنید."
        uid, link = _find_link(arg)
        if not link:
            return "کانفیگی پیدا نشد."
        vless = services.vless_link_for_link(link, uid, registry.PUBLIC_HOST or "localhost")
        return f"{_fmt_link(uid, link)}\n\n🔗 <code>{vless}</code>"

    if cmd == "/sub":
        if not arg:
            return "نام گروه را بنویسید."
        for sid, sub in list(registry.SUBS.items()):
            if arg.strip().lower() in (sub.get("name", "").lower(), sid.lower()):
                base = registry.PUBLIC_HOST or "localhost"
                return f"🔗 اشتراک «{sub.get('name','?')}»:\n<code>https://{base}/api/sub/{sid}</code>"
        return "گروهی پیدا نشد."

    if cmd in ("/on", "/off"):
        if not arg:
            return "شناسه یا نام کانفیگ."
        uid, link = _find_link(arg)
        if not link:
            return "کانفیگی پیدا نشد."
        await services.set_link_active(uid, cmd == "/on")
        return f"{'✅ فعال شد' if cmd == '/on' else '⛔️ غیرفعال شد'}: {link.get('label')}"

    if cmd == "/del":
        if not arg:
            return "شناسه یا نام کانفیگ."
        uid, link = _find_link(arg)
        if not link:
            return "کانفیگی پیدا نشد."
        await services.remove_link(uid)
        return f"🗑 حذف شد: {link.get('label')}"

    if cmd == "/add":
        name = (arg or "لینک جدید").strip()
        limit_value = 0.0
        m = re.match(r"(.+?)\s+([\d.]+)\s*gb$", name, flags=re.IGNORECASE)
        if m:
            name = m.group(1).strip() or "لینک جدید"
            limit_value = float(m.group(2))
        link = await services.create_link(
            label=name,
            limit_bytes=int(limit_value * 1024 ** 3) if limit_value else 0,
        )
        return f"✅ ساخته شد:\n{_fmt_link(link['uuid'], link)}"

    if cmd == "/usage":
        lines = ["📊 <b>مصرف کانفیگ‌ها</b>:\n"]
        total = 0
        for uid, link in sorted(registry.LINKS.items(), key=lambda x: x[1].get("label", "")):
            used = int(link.get("used_bytes", 0))
            total += used
            lines.append(f"{link.get('label','?')}: {registry.fmt_bytes(used)}")
        lines.append(f"\n<b>کل: {registry.fmt_bytes(total)}</b>")
        return "\n".join(lines)

    if cmd == "/stats":
        st = await services.collect_stats()
        return (
            "📈 <b>وضعیت سرور</b>:\n"
            f"🟢 اتصال فعال: {st['active_connections']}\n"
            f"📦 ترافیک کل: {st['total_traffic_mb']} MB\n"
            f"🔄 درخواست کل: {st['total_requests']}\n"
            f"⚠️ خطاها: {st['total_errors']}\n"
            f"🕐 آپ‌تایم: {st['uptime']}"
        )

    return "دستور نامفهوم است. /help"