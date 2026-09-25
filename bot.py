import asyncio
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
SUB_USER = "Sara-c2c79ff8"
WORKER_FILE = Path(__file__).with_name("worker.js")

# Secrets are intentionally kept in process memory only and are lost on restart.
TOKENS: dict[int, str] = {}
WAITING_FOR_TOKEN: set[int] = set()
PENDING: dict[int, dict[str, Any]] = {}
ADMIN_ID = int(os.environ.get("TELEGRAM_ADMIN_ID", "0"))


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
            "compatibility_date": "2026-09-26",
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


def is_admin(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and chat.type == "private" and user.id == ADMIN_ID and ADMIN_ID > 0)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔐 وارد کردن Cloudflare API Token", callback_data="token")],
        [InlineKeyboardButton("🚀 Deploy", callback_data="deploy")],
        [InlineKeyboardButton("♻️ حذف و Redeploy", callback_data="redeploy")],
        [InlineKeyboardButton("🧹 پاک کردن Token", callback_data="forget")],
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
                f"Worker `{SCRIPT_NAME}` از قبل وجود دارد. برای جلوگیری از overwrite ناخواسته، Deploy انجام نشد؛ از دکمهٔ Redeploy استفاده کن.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("♻️ حذف و Redeploy", callback_data="redeploy")]]),
            )
            return
        if action == "redeploy" and not exists:
            await q.edit_message_text(
                f"Worker `{SCRIPT_NAME}` هنوز وجود ندارد. از Deploy استفاده کن، نه Redeploy.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Deploy", callback_data="deploy")]]),
            )
            return
        PENDING[q.from_user.id] = {
            "action": action, "token": token, "account_id": str(account["id"]),
            "account_name": account.get("name", "Cloudflare account"),
            "database_id": matches[0]["uuid"], "database_name": D1_NAME,
            "subdomain": account_subdomain,
        }
        verb = "حذف کامل Worker موجود و ساخت دوباره" if action == "redeploy" else "ساخت و دیپلوی Worker"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تأیید", callback_data=f"confirm:{action}"), InlineKeyboardButton("لغو", callback_data="cancel")]
        ])
        await q.edit_message_text(
            f"{verb}: `{SCRIPT_NAME}`\nاکانت: {account.get('name', 'Cloudflare')}\n"
            f"D1: `{D1_NAME}` با Binding به نام `DB`\n"
            f"آدرس مورد انتظار: `https://{SCRIPT_NAME}.{account_subdomain}.workers.dev/sub/{SUB_USER}`\n\n"
            + ("حذف Worker می‌تواند چند لحظه قطعی ایجاد کند؛ داده‌های D1 حذف نمی‌شود." if action == "redeploy" else "")
            + "\nادامه بدهم؟",
            parse_mode="Markdown", reply_markup=keyboard,
        )
    except CloudflareError as e:
        await q.edit_message_text(f"Cloudflare رد کرد: {e}")
    except Exception:
        await q.edit_message_text("بررسی Cloudflare ناموفق بود. توکن، دسترسی‌ها و اتصال را بررسی کن.")
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
        await q.edit_message_text("توکن و اطلاعات موقت پاک شد.", reply_markup=main_keyboard())
    elif data in ("deploy", "redeploy"):
        await begin_deployment(update, data)
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
        await prepare_account(update, account, action, token)
    elif data.startswith("confirm:"):
        action = data.split(":", 1)[1]
        plan = PENDING.get(uid)
        if not plan or plan.get("action") != action:
            await q.edit_message_text("درخواست منقضی شده؛ دوباره شروع کن.", reply_markup=main_keyboard())
            return
        cf = Cloudflare(plan["token"])
        try:
            await q.edit_message_text("در حال انجام عملیات Cloudflare…")
            if action == "redeploy":
                await cf.delete_script(plan["account_id"])
            await cf.deploy(plan["account_id"], plan["database_id"])
            link = f"https://{SCRIPT_NAME}.{plan['subdomain']}.workers.dev/sub/{SUB_USER}"
            await q.edit_message_text(
                f"✅ HusteRIX Worker دیپلوی شد.\n[باز کردن لینک اشتراک]({link})\n\n"
                "اگر این کاربر در دیتابیس D1 وجود نداشته باشد، لینک 404 می‌دهد.",
                parse_mode="Markdown", reply_markup=main_keyboard(), disable_web_page_preview=True,
            )
        except CloudflareError as e:
            prefix = "Worker حذف شد اما دیپلوی کامل نشد. " if action == "redeploy" else ""
            await q.edit_message_text(f"❌ {prefix}Cloudflare: {e}", reply_markup=main_keyboard())
        except Exception:
            await q.edit_message_text("❌ عملیات کامل نشد. اگر Redeploy بود، در داشبورد بررسی کن Worker حذف نشده باشد.", reply_markup=main_keyboard())
        finally:
            PENDING.pop(uid, None)
            await cf.close()
    elif data == "cancel":
        PENDING.pop(uid, None)
        await q.edit_message_text("لغو شد.", reply_markup=main_keyboard())


def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token or ADMIN_ID <= 0:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_ID first.")
    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
