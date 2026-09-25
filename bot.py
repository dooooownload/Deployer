import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

API = "https://api.cloudflare.com/client/v4"
SCRIPT_NAME = "husterix"
D1_NAME = "zeus-db-eu7n21"
EXPECTED_SUBDOMAIN = "freebirds22"
# Paste a fresh BotFather token here. Never reuse a token shared in chat.
# Environment variable takes precedence if set.
TELEGRAM_BOT_TOKEN = "8926130639:AAH0qfCiDiQDijulW8n4mnhUhwoRRm8_Vsg"
# Optional D1 username for a ready-to-click subscription link.
# Leave blank to show the URL pattern without assuming a username.
SUBSCRIPTION_USERNAME = ""
WORKER_FILE = Path(__file__).with_name("worker.js")
MAX_RESTORE_FILE_BYTES = 20 * 1024 * 1024  # Telegram bot API document limit

# Secrets are intentionally kept in process memory only and are lost on restart.
TOKENS: dict[int, str] = {}
WAITING_FOR_TOKEN: set[int] = set()
PENDING: dict[int, dict[str, Any]] = {}
WAITING_FOR_RESTORE: dict[int, dict[str, Any]] = {}
PENDING_RESTORE: dict[int, dict[str, Any]] = {}
# Environment variable takes precedence if set; otherwise this default is used.
ADMIN_ID = int(os.environ.get("TELEGRAM_ADMIN_ID", "7727625618"))


class CloudflareError(Exception):
    pass


class Cloudflare:
    def __init__(self, token: str):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=90),
        )

    async def close(self):
        await self.session.close()

    async def request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        async with self.session.request(method, API + path, **kwargs) as r:
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {}
            if not r.ok or not data.get("success", False):
                errs = data.get("errors") or []
                detail = "; ".join(str(e.get("message", "")) for e in errs if e.get("message"))
                raise CloudflareError(detail or f"Cloudflare returned HTTP {r.status}")
            return data

    async def accounts(self) -> list[dict[str, Any]]:
        return (await self.request("GET", "/accounts?per_page=100")).get("result", [])

    async def databases(self, account_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self.request(
                "GET", f"/accounts/{account_id}/d1/database",
                params={"per_page": 100, "page": page},
            )
            result.extend(data.get("result", []))
            info = data.get("result_info") or {}
            pages = int(info.get("total_pages", page))
            if page >= pages or not data.get("result"):
                return result
            page += 1

    async def subdomain(self, account_id: str) -> str:
        data = await self.request("GET", f"/accounts/{account_id}/workers/subdomain")
        return str((data.get("result") or {}).get("subdomain") or "")

    async def script_exists(self, account_id: str) -> bool:
        url = f"{API}/accounts/{account_id}/workers/scripts/{SCRIPT_NAME}"
        async with self.session.get(url) as r:
            if r.status == 404:
                return False
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {}
            if not r.ok or not data.get("success", False):
                errs = data.get("errors") or []
                detail = "; ".join(str(e.get("message", "")) for e in errs if e.get("message"))
                raise CloudflareError(detail or f"Could not check Worker status (HTTP {r.status})")
            return True

    async def delete_script(self, account_id: str) -> None:
        await self.request("DELETE", f"/accounts/{account_id}/workers/scripts/{SCRIPT_NAME}")

    async def deploy(self, account_id: str, database_id: str) -> None:
        if not WORKER_FILE.is_file():
            raise CloudflareError("worker.js is missing next to bot.py")
        contents = WORKER_FILE.read_bytes()
        metadata = {
            "main_module": "worker.js",
            "compatibility_date": "2026-07-10",
            "compatibility_flags": ["nodejs_compat"],
            "bindings": [{"type": "d1", "name": "DB", "database_id": database_id}],
        }
        form = aiohttp.FormData()
        form.add_field("metadata", json.dumps(metadata), content_type="application/json")
        form.add_field(
            "worker.js", contents, filename="worker.js",
            content_type="application/javascript+module",
        )
        await self.request(
            "PUT", f"/accounts/{account_id}/workers/scripts/{SCRIPT_NAME}", data=form
        )
        # Explicitly enable the workers.dev route, without enabling preview URLs.
        await self.request(
            "POST", f"/accounts/{account_id}/workers/scripts/{SCRIPT_NAME}/subdomain",
            json={"enabled": True, "previews_enabled": False},
        )

    async def export_database(self, account_id: str, database_id: str) -> tuple[str, bytes]:
        """Kick off a D1 export, poll until a signed URL is ready, then download it."""
        path = f"/accounts/{account_id}/d1/database/{database_id}/export"
        data = await self.request("POST", path, json={"output_format": "polling"})
        result = data.get("result") or {}
        bookmark = result.get("at_bookmark")
        signed_url = result.get("signed_url")
        filename = result.get("filename") or "backup.sql"
        attempts = 0
        while not signed_url:
            attempts += 1
            if attempts > 60:
                raise CloudflareError("گرفتن بکاپ خیلی طول کشید؛ بعداً دوباره تلاش کن.")
            await asyncio.sleep(2)
            data = await self.request(
                "POST", path,
                json={"output_format": "polling", "current_bookmark": bookmark},
            )
            result = data.get("result") or {}
            bookmark = result.get("at_bookmark") or bookmark
            signed_url = result.get("signed_url")
            filename = result.get("filename") or filename
        # The signed URL is a pre-authorized R2 link; fetch it without the CF API token.
        async with aiohttp.ClientSession() as raw:
            async with raw.get(signed_url) as r:
                if not r.ok:
                    raise CloudflareError(f"دانلود فایل بکاپ ناموفق بود (HTTP {r.status})")
                content = await r.read()
        return filename, content

    async def import_database(self, account_id: str, database_id: str, sql_bytes: bytes) -> dict[str, Any]:
        """Upload a .sql file and have D1 ingest it, polling until done."""
        path = f"/accounts/{account_id}/d1/database/{database_id}/import"
        etag = hashlib.md5(sql_bytes).hexdigest()
        init = await self.request("POST", path, json={"action": "init", "etag": etag})
        result = init.get("result") or {}
        upload_url = result.get("upload_url")
        filename = result.get("filename")
        if not upload_url or not filename:
            raise CloudflareError("Cloudflare لینک آپلود برنگرداند.")
        # Upload to the presigned R2 URL directly, without the CF API token.
        async with aiohttp.ClientSession() as raw:
            async with raw.put(upload_url, data=sql_bytes) as r:
                if not r.ok:
                    raise CloudflareError(f"آپلود فایل به Cloudflare ناموفق بود (HTTP {r.status})")
        ingest = await self.request(
            "POST", path, json={"action": "ingest", "etag": etag, "filename": filename}
        )
        result = ingest.get("result") or {}
        bookmark = result.get("at_bookmark")
        status = result.get("status")
        attempts = 0
        while status not in ("complete", "error"):
            attempts += 1
            if attempts > 90:
                raise CloudflareError("بارگذاری خیلی طول کشید؛ وضعیت را در داشبورد D1 بررسی کن.")
            await asyncio.sleep(2)
            poll = await self.request(
                "POST", path, json={"action": "poll", "current_bookmark": bookmark}
            )
            result = poll.get("result") or {}
            status = result.get("status")
            bookmark = result.get("at_bookmark") or bookmark
        if status == "error":
            raise CloudflareError(result.get("error") or "بارگذاری دیتابیس شکست خورد.")
        return result


def is_admin(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and chat.type == "private" and user.id == ADMIN_ID and ADMIN_ID > 0)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Deploy", callback_data="deploy")],
        [
            InlineKeyboardButton("🔐 وارد کردن Cloudflare API Token", callback_data="token"),
            InlineKeyboardButton("🗑 Delete Panel", callback_data="delete"),
        ],
        [InlineKeyboardButton("🧹 پاک کردن Token", callback_data="forget")],
        [InlineKeyboardButton("🗄 مدیریت", callback_data="dbmenu")],
    ])


def db_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 بکاپ‌گیری از دیتابیس", callback_data="db_backup")],
        [InlineKeyboardButton("📥 بارگذاری دیتابیس", callback_data="db_restore")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="db_back")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.effective_message.reply_text(
        "𝑯𝒖𝒔𝒕𝒆𝑹𝑰𝑿 deploy bot\n\n"
        "توکن فقط در حافظهٔ موقت ربات می‌ماند و با خاموش/روشن شدن پاک می‌شود. "
        "بعد از دریافت، پیام توکن حذف می‌شود؛ با این حال ارسال توکن در تلگرام ریسک دارد.",
        reply_markup=main_keyboard(),
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update) or update.effective_user.id not in WAITING_FOR_TOKEN:
        return
    WAITING_FOR_TOKEN.discard(update.effective_user.id)
    token = (update.effective_message.text or "").strip()
    try:
        await update.effective_message.delete()
    except Exception:
        pass
    if not token or len(token) < 20:
        await update.effective_chat.send_message("توکن قابل‌قبول به نظر نمی‌رسد؛ دوباره از دکمهٔ توکن استفاده کن.", reply_markup=main_keyboard())
        return
    TOKENS[update.effective_user.id] = token
    await update.effective_chat.send_message(
        "توکن دریافت شد و در همین اجرای ربات نگه داشته می‌شود. برای ادامه Deploy را بزن.",
        reply_markup=main_keyboard(),
    )


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    uid = update.effective_user.id
    pending = WAITING_FOR_RESTORE.get(uid)
    if not pending:
        return
    doc = update.effective_message.document
    if not doc:
        return
    if doc.file_size and doc.file_size > MAX_RESTORE_FILE_BYTES:
        await update.effective_message.reply_text("فایل خیلی بزرگ است (حداکثر ۲۰ مگابایت).")
        return
    tg_file = await doc.get_file()
    sql_bytes = bytes(await tg_file.download_as_bytearray())
    if not sql_bytes.strip():
        await update.effective_message.reply_text("فایل خالی است.")
        return
    WAITING_FOR_RESTORE.pop(uid, None)
    PENDING_RESTORE[uid] = {**pending, "sql_bytes": sql_bytes}
    size_kb = len(sql_bytes) / 1024
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأیید بارگذاری", callback_data="db_restore_confirm"),
        InlineKeyboardButton("لغو", callback_data="db_restore_cancel"),
    ]])
    await update.effective_message.reply_text(
        f"فایل دریافت شد: `{doc.file_name or 'backup.sql'}` (~{size_kb:.1f} KB)\n"
        "⚠️ این عملیات SQL فایل را روی دیتابیس فعلی اجرا می‌کند و ممکن است غیرقابل‌برگشت باشد. "
        "بهتر است قبلش بکاپ بگیری.\nادامه بدهم؟",
        parse_mode="Markdown", reply_markup=keyboard,
    )


async def prepare_account(update: Update, account: dict[str, Any], action: str, token: str):
    q = update.callback_query
    cf = Cloudflare(token)
    try:
        dbs = await cf.databases(str(account["id"]))
        matches = [db for db in dbs if db.get("name") == D1_NAME]
        if len(matches) != 1:
            await q.edit_message_text(
                f"دیتابیس دقیقاً یک مورد پیدا نشد: `{D1_NAME}`. "
                "مطمئن شو اسم D1 در همین اکانت است و توکن دسترسی D1 Read دارد.",
                parse_mode="Markdown",
            )
            return
        account_subdomain = await cf.subdomain(str(account["id"]))
        if account_subdomain != EXPECTED_SUBDOMAIN:
            actual = f"{SCRIPT_NAME}.{account_subdomain}.workers.dev" if account_subdomain else "workers.dev برای این اکانت تنظیم نشده"
            await q.edit_message_text(
                f"برای اینکه لینک درخواستی دقیقاً ساخته شود، زیردامنهٔ اکانت باید `{EXPECTED_SUBDOMAIN}.workers.dev` باشد.\n"
                f"الان: `{actual}`\n"
                "زیردامنه را از داشبورد Cloudflare تنظیم کن، بعد دوباره تلاش کن. ربات آن را خودکار عوض نمی‌کند.",
                parse_mode="Markdown",
            )
            return
        exists = await cf.script_exists(str(account["id"]))
        if action == "deploy" and exists:
            await q.edit_message_text(
                f"Worker `{SCRIPT_NAME}` از قبل وجود دارد. اول از دکمهٔ Delete Panel حذفش کن، بعد دوباره Deploy بزن.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Delete Panel", callback_data="delete")]]),
            )
            return
        PENDING[q.from_user.id] = {
            "action": action, "token": token, "account_id": str(account["id"]),
            "account_name": account.get("name", "Cloudflare account"),
            "database_id": matches[0]["uuid"], "database_name": D1_NAME,
            "subdomain": account_subdomain,
        }
        subscription_path = (
            f"/sub/{SUBSCRIPTION_USERNAME}"
            if SUBSCRIPTION_USERNAME
            else "/sub/USERNAME_IN_D1"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأیید", callback_data=f"confirm:{action}"), InlineKeyboardButton("لغو", callback_data="cancel")]
        ])
        await q.edit_message_text(
            f"ساخت و دیپلوی Worker: `{SCRIPT_NAME}`\nاکانت: {account.get('name', 'Cloudflare')}\n"
            f"D1: `{D1_NAME}` با Binding به نام `DB`\n"
            f"الگوی لینک اشتراک: `https://{SCRIPT_NAME}.{account_subdomain}.workers.dev{subscription_path}`\n\n"
            "ادامه بدهم؟",
            parse_mode="Markdown", reply_markup=keyboard,
        )
    except CloudflareError as e:
        await q.edit_message_text(f"Cloudflare رد کرد: {e}")
    except Exception:
        await q.edit_message_text("بررسی Cloudflare ناموفق بود. توکن، دسترسی‌ها و اتصال را بررسی کن.")
    finally:
        await cf.close()


async def prepare_delete_account(update: Update, account: dict[str, Any], token: str):
    q = update.callback_query
    cf = Cloudflare(token)
    try:
        exists = await cf.script_exists(str(account["id"]))
        if not exists:
            await q.edit_message_text(
                f"Worker `{SCRIPT_NAME}` وجود ندارد؛ چیزی برای حذف نیست.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Deploy", callback_data="deploy")]]),
            )
            return
        PENDING[q.from_user.id] = {
            "action": "delete", "token": token, "account_id": str(account["id"]),
            "account_name": account.get("name", "Cloudflare account"),
        }
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأیید حذف", callback_data="confirm:delete"), InlineKeyboardButton("لغو", callback_data="cancel")]
        ])
        await q.edit_message_text(
            f"حذف Worker: `{SCRIPT_NAME}`\nاکانت: {account.get('name', 'Cloudflare')}\n\n"
            "فقط خود Worker حذف می‌شود؛ دیتابیس D1 دست‌نخورده می‌ماند. برای ساخت دوباره بعداً از Deploy استفاده کن.\n\n"
            "ادامه بدهم؟",
            parse_mode="Markdown", reply_markup=keyboard,
        )
    except CloudflareError as e:
        await q.edit_message_text(f"Cloudflare رد کرد: {e}")
    except Exception:
        await q.edit_message_text("بررسی Cloudflare ناموفق بود.")
    finally:
        await cf.close()


async def prepare_db_account(update: Update, account: dict[str, Any], action: str, token: str):
    q = update.callback_query
    cf = Cloudflare(token)
    try:
        dbs = await cf.databases(str(account["id"]))
        matches = [db for db in dbs if db.get("name") == D1_NAME]
        if len(matches) != 1:
            await q.edit_message_text(
                f"دیتابیس دقیقاً یک مورد پیدا نشد: `{D1_NAME}`. "
                "مطمئن شو اسم D1 در همین اکانت است و توکن دسترسی D1 دارد.",
                parse_mode="Markdown",
            )
            return
        account_id = str(account["id"])
        database_id = matches[0]["uuid"]
        if action == "db_backup":
            await run_db_backup(update, token, account_id, database_id)
        elif action == "db_restore":
            WAITING_FOR_RESTORE[q.from_user.id] = {
                "token": token, "account_id": account_id, "database_id": database_id,
            }
            await q.edit_message_text(
                "فایل SQL بکاپ را همین‌جا به‌صورت Document ارسال کن.\n"
                "⚠️ محتوای این فایل روی دیتابیس فعلی اجرا می‌شود."
            )
    except CloudflareError as e:
        await q.edit_message_text(f"Cloudflare رد کرد: {e}")
    except Exception:
        await q.edit_message_text("بررسی Cloudflare ناموفق بود. توکن، دسترسی‌ها و اتصال را بررسی کن.")
    finally:
        await cf.close()


async def run_db_backup(update: Update, token: str, account_id: str, database_id: str):
    q = update.callback_query
    await q.edit_message_text("در حال گرفتن بکاپ از D1… برای دیتابیس‌های بزرگ ممکن است کمی طول بکشد.")
    cf = Cloudflare(token)
    try:
        filename, content = await cf.export_database(account_id, database_id)
        await q.message.reply_document(
            document=content, filename=filename or "backup.sql",
            caption="✅ بکاپ دیتابیس آماده شد.",
        )
        await q.edit_message_text("✅ بکاپ گرفته شد و ارسال شد.", reply_markup=main_keyboard())
    except CloudflareError as e:
        await q.edit_message_text(f"❌ Cloudflare: {e}", reply_markup=main_keyboard())
    except Exception:
        await q.edit_message_text("❌ گرفتن بکاپ ناموفق بود.", reply_markup=main_keyboard())
    finally:
        await cf.close()


async def begin_deployment(update: Update, action: str):
    q = update.callback_query
    token = TOKENS.get(q.from_user.id)
    if not token:
        await q.answer("اول API Token را وارد کن.", show_alert=True)
        await q.edit_message_text("از دکمهٔ زیر برای وارد کردن توکن استفاده کن.", reply_markup=main_keyboard())
        return
    cf = Cloudflare(token)
    try:
        await cf.request("GET", "/user/tokens/verify")
        accounts = await cf.accounts()
        if not accounts:
            await q.edit_message_text("این توکن به هیچ اکانتی دسترسی ندارد.")
            return
        if len(accounts) == 1:
            if action == "delete":
                await prepare_delete_account(update, accounts[0], token)
            else:
                await prepare_account(update, accounts[0], action, token)
        else:
            rows = [[InlineKeyboardButton(str(a.get("name", "Account"))[:50], callback_data=f"account:{action}:{a['id']}")] for a in accounts]
            await q.edit_message_text("اکانت Cloudflare را انتخاب کن:", reply_markup=InlineKeyboardMarkup(rows))
    except CloudflareError as e:
        await q.edit_message_text(f"اعتبارسنجی یا خواندن اکانت‌ها ناموفق بود: {e}")
    except Exception:
        await q.edit_message_text("ارتباط با Cloudflare ناموفق بود؛ دوباره امتحان کن.")
    finally:
        await cf.close()


async def begin_db_action(update: Update, action: str):
    q = update.callback_query
    token = TOKENS.get(q.from_user.id)
    if not token:
        await q.answer("اول API Token را وارد کن.", show_alert=True)
        await q.edit_message_text("از دکمهٔ زیر برای وارد کردن توکن استفاده کن.", reply_markup=main_keyboard())
        return
    cf = Cloudflare(token)
    try:
        await cf.request("GET", "/user/tokens/verify")
        accounts = await cf.accounts()
        if not accounts:
            await q.edit_message_text("این توکن به هیچ اکانتی دسترسی ندارد.")
            return
        if len(accounts) == 1:
            await prepare_db_account(update, accounts[0], action, token)
        else:
            rows = [[InlineKeyboardButton(str(a.get("name", "Account"))[:50], callback_data=f"dbaccount:{action}:{a['id']}")] for a in accounts]
            await q.edit_message_text("اکانت Cloudflare را انتخاب کن:", reply_markup=InlineKeyboardMarkup(rows))
    except CloudflareError as e:
        await q.edit_message_text(f"اعتبارسنجی یا خواندن اکانت‌ها ناموفق بود: {e}")
    except Exception:
        await q.edit_message_text("ارتباط با Cloudflare ناموفق بود؛ دوباره امتحان کن.")
    finally:
        await cf.close()


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("دسترسی ندارید.", show_alert=True)
        return
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id
    if data == "token":
        WAITING_FOR_TOKEN.add(uid)
        await q.edit_message_text("توکن Cloudflare را در همین گفت‌وگوی خصوصی بفرست. پیام توکن بعد از دریافت حذف می‌شود.")
    elif data == "forget":
        TOKENS.pop(uid, None)
        PENDING.pop(uid, None)
        WAITING_FOR_TOKEN.discard(uid)
        WAITING_FOR_RESTORE.pop(uid, None)
        PENDING_RESTORE.pop(uid, None)
        await q.edit_message_text("توکن و اطلاعات موقت پاک شد.", reply_markup=main_keyboard())
    elif data == "dbmenu":
        await q.edit_message_text("مدیریت دیتابیس D1:", reply_markup=db_menu_keyboard())
    elif data == "db_back":
        await q.edit_message_text("𝑯𝒖𝒔𝒕𝒆𝑹𝑰𝑿 deploy bot", reply_markup=main_keyboard())
    elif data in ("deploy", "delete"):
        await begin_deployment(update, data)
    elif data in ("db_backup", "db_restore"):
        await begin_db_action(update, data)
    elif data.startswith("account:"):
        _, action, account_id = data.split(":", 2)
        token = TOKENS.get(uid)
        if not token:
            await q.edit_message_text("توکن پاک شده؛ دوباره واردش کن.", reply_markup=main_keyboard())
            return
        cf = Cloudflare(token)
        try:
            accounts = await cf.accounts()
            account = next((a for a in accounts if str(a.get("id")) == account_id), None)
            if not account:
                await q.edit_message_text("اکانت انتخاب‌شده دیگر در دسترس نیست.")
                return
        finally:
            await cf.close()
        if action == "delete":
            await prepare_delete_account(update, account, token)
        else:
            await prepare_account(update, account, action, token)
    elif data.startswith("dbaccount:"):
        _, action, account_id = data.split(":", 2)
        token = TOKENS.get(uid)
        if not token:
            await q.edit_message_text("توکن پاک شده؛ دوباره واردش کن.", reply_markup=main_keyboard())
            return
        cf = Cloudflare(token)
        try:
            accounts = await cf.accounts()
            account = next((a for a in accounts if str(a.get("id")) == account_id), None)
            if not account:
                await q.edit_message_text("اکانت انتخاب‌شده دیگر در دسترس نیست.")
                return
        finally:
            await cf.close()
        await prepare_db_account(update, account, action, token)
    elif data.startswith("confirm:"):
        action = data.split(":", 1)[1]
        plan = PENDING.get(uid)
        if not plan or plan.get("action") != action:
            await q.edit_message_text("درخواست منقضی شده؛ دوباره شروع کن.", reply_markup=main_keyboard())
            return
        cf = Cloudflare(plan["token"])
        try:
            if action == "delete":
                await q.edit_message_text("در حال حذف Worker…")
                await cf.delete_script(plan["account_id"])
                await q.edit_message_text(
                    f"✅ Worker `{SCRIPT_NAME}` حذف شد.\nبرای ساخت دوباره از دکمهٔ Deploy استفاده کن.",
                    parse_mode="Markdown", reply_markup=main_keyboard(),
                )
            else:
                await q.edit_message_text("در حال انجام عملیات Cloudflare…")
                await cf.deploy(plan["account_id"], plan["database_id"])
                if SUBSCRIPTION_USERNAME:
                    link = f"https://{SCRIPT_NAME}.{plan['subdomain']}.workers.dev/sub/{SUBSCRIPTION_USERNAME}"
                    result = f"✅ HusteRIX Worker دیپلوی شد.\n[باز کردن لینک اشتراک]({link})"
                else:
                    result = (
                        "✅ HusteRIX Worker دیپلوی شد.\n"
                        f"آدرس پایه: `https://{SCRIPT_NAME}.{plan['subdomain']}.workers.dev`\n"
                        f"الگوی اشتراک: `https://{SCRIPT_NAME}.{plan['subdomain']}.workers.dev/sub/USERNAME_IN_D1`"
                    )
                await q.edit_message_text(
                    result + "\n\n"
                    "USERNAME_IN_D1 باید نام کاربری واقعیِ موجود در D1 باشد؛ نام ورود پنل یا تلگرام نیست.",
                    parse_mode="Markdown", reply_markup=main_keyboard(), disable_web_page_preview=True,
                )
        except CloudflareError as e:
            label = "حذف Worker" if action == "delete" else "Cloudflare"
            await q.edit_message_text(f"❌ {label}: {e}", reply_markup=main_keyboard())
        except Exception:
            await q.edit_message_text("❌ عملیات کامل نشد.", reply_markup=main_keyboard())
        finally:
            PENDING.pop(uid, None)
            await cf.close()
    elif data == "db_restore_confirm":
        plan = PENDING_RESTORE.pop(uid, None)
        if not plan:
            await q.edit_message_text("درخواست منقضی شده؛ دوباره شروع کن.", reply_markup=main_keyboard())
            return
        cf = Cloudflare(plan["token"])
        try:
            await q.edit_message_text("در حال بارگذاری و اجرای فایل روی D1…")
            result = await cf.import_database(plan["account_id"], plan["database_id"], plan["sql_bytes"])
            meta = result.get("meta") or {}
            changes = meta.get("changes")
            extra = f"\nردیف‌های تغییریافته: {changes}" if changes is not None else ""
            await q.edit_message_text(f"✅ بارگذاری دیتابیس کامل شد.{extra}", reply_markup=main_keyboard())
        except CloudflareError as e:
            await q.edit_message_text(f"❌ Cloudflare: {e}", reply_markup=main_keyboard())
        except Exception:
            await q.edit_message_text("❌ بارگذاری دیتابیس ناموفق بود.", reply_markup=main_keyboard())
        finally:
            await cf.close()
    elif data == "db_restore_cancel":
        PENDING_RESTORE.pop(uid, None)
        WAITING_FOR_RESTORE.pop(uid, None)
        await q.edit_message_text("لغو شد.", reply_markup=main_keyboard())
    elif data == "cancel":
        PENDING.pop(uid, None)
        await q.edit_message_text("لغو شد.", reply_markup=main_keyboard())


def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN).strip()
    if not bot_token or ADMIN_ID <= 0:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_ID first.")
    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
