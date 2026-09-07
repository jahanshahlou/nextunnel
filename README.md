# 🚀 NexTunnel

پروکسی / تونل چندپروتکلی بدون‌دیتابیسِ سنگین، با پنل مدیریت ریسپانسیو (موبایل‌فرست) و ربات تلگرام که مستقیم از خود پنل قابل اتصال است.

| پروتکل | توضیح |
| --- | --- |
| `VLESS + WebSocket` | کلاسیک و پایدار |
| `XHTTP (Packet-Up)` | برای شبکه‌های محدودکننده — درخواست‌های POST جداگانه |
| `XHTTP (Stream-Up)` | حالت استریم‌پیوسته با کنترل‌فلو تطبیقی |
| `HTTP Proxy` | پروکسی ساده برای کلاینت‌ها (با محافظت SSRF) |

ویژگی‌ها:

- پنل مدیریت API+JS با طراحی **mobile-first** و RTL فارسی
- مدیریت کانفیگ، گروه/اشتراک، محدودیت حجم، انقضا، محدودیت آی‌پی همزمان، محدودیت سرعت (token bucket)
- **ربات تلگرام قابل اتصال از پنل** — توکن و ادمین‌ها در دیتابیس ذخیره و بدون ری‌استارت سرور اعمال می‌شوند
- آمار زنده، اتصالات فعال، لاگ فعالیت/خطا، نمودار ترافیک ساعتی
- ذخیره‌سازی SQLite (aiosqlite) + فلش دوره‌ای؛ هیچ داده‌ای با ری‌استارت از بین نمی‌رود
- امنیت: PBKDF2-SHA256 (نمک تصادفی، ۲۶۰k دور)، کوکی HttpOnly با نسبت SameSite=Lax، Rate-Limiting لاگین، مقایسه‌ی ثابت‌زمانی، IP واقعی از آخرین مقدار `X-Forwarded-For`
- مبارزه با SSRF در پروکسی (رد شبکه‌های خصوصی/loopback/link-local)

## راه‌اندازی

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR=.data
python main.py
```

پنل: `http://localhost:8000` — رمز پیش‌فرض: `NexTunnel@2025` (حتماً بعد از اولین ورود عوض کنید).

## راه‌اندازی روی Railway

1. یک پروژه‌ی جدید Railway از همین ریپو بسازید (Procfile: `web: python main.py`).
2. یک **Volume** به مسیر `/data` اضافه کنید (برای ماندگاری دیتابیس).
3. `RAILWAY_PUBLIC_DOMAIN` به‌صورت خودکار ست می‌شود.
4. متغیرهای اختیاری: `ADMIN_PASSWORD`، `TELEGRAM_BOT_TOKEN`، `TELEGRAM_ADMIN_IDS`.

## اتصال ربات تلگرام

دو راه:

- (ساده) متغیرهای محیطی `TELEGRAM_BOT_TOKEN` و `TELEGRAM_ADMIN_IDS` قبل از اولین اجرا.
- (از پنل) بخش «ربات تلگرام» → توکن + شناسه‌های مدیر → «ذخیره و راه‌اندازی مجدد».

دستورهای ربات: `/links`, `/link نام`, `/add نام 10GB`, `/on نام`, `/off نام`, `/del نام`, `/sub نام`, `/usage`, `/stats`, `/help`

## ساختار

```
main.py                 # entrypoint: routerها, داشبورد, background tasks
app/
  config.py             # تمام تنظیمات محیطی
  registry.py           # state مشترک درون‌حافظه (بدون import دایره‌ای)
  database.py           # SQLite (aiosqlite) — منبع حقیقت دیتا
  security.py           # هش رمز، سشن، rate-limit، استخراج IP
  services.py           # لاجیک تجاری کانفیگ/گروه/ربات
  api.py                # REST API
  proxy.py              # HTTP Proxy با محافظت SSRF
  transports/
    relay.py            # VLESS + WebSocket
    xhttp.py            # XHTTP packet-up / stream-up
    throttle.py         # token bucket سرعت
    protocol.py         # پارسر هدر VLESS
  bot/
    manager.py          # چرخه‌ی حیات ربات (ستارت/استاپ/رستارت از پنل)
    handlers.py         # هندلرهای تلگرام
  web/
    templates/          # dashboard / login / public
    static/css,js,img   # UI موبایل‌فرست
```

## تغییرات نسبت به نسخه‌ی قبلی (X4G)

- حذف JSON-file ذخیره‌سازی → **SQLite** با فلش دوره‌ای و بازیابی در بوت
- رفع باگ X-Forwarded-For (الان از **آخرین** مقدار استفاده می‌شود؛ قبلش آی‌پی جعلی می‌توانست دور بزند)
- رفع **memory-leak سشن‌ها** (پاکسازی دوره‌ای) و ریپر درست برای سشن‌های XHTTP idle
- حذف پروتکل نیمه‌کاره‌ی `stream-one`
- `reset_bucket` هنگام حذف کانفیگ (رفع نشت سطل سرعت)
- پروکسی بدون SSRF قبلی → پروکسی امن با پخش استریم + هدر وزن
- هش رمز PBKDF2 + مقایسه‌ی ثابت‌زمانی + rate-limit لاگین
- ربات تلگرام قابل مدیریت از پنل (قبلاً فقط با env ست می‌شد)