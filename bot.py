import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

# Without this, python-telegram-bot stays almost silent: no per-update logs,
# and even unhandled exceptions may not reach the Railway log stream.
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("husterix-bot")

API = "https://api.cloudflare.com/client/v4"
SCRIPT_NAME = "husterix"
D1_NAME = "zeus-db-eu7n21"
EXPECTED_SUBDOMAIN = "freebirds22"
# The bot token comes only from the TELEGRAM_BOT_TOKEN environment variable
# (see main()) — intentionally no hardcoded fallback in the source. If two
# deployments ever ran with the same token, both would poll get_updates and
# permanently Conflict-loop each other (each kicking the other's connection,
# which looks like every button randomly "not working"). Requiring the env
# var makes that class of bug impossible.
# Optional D1 username for a ready-to-click subscription link.
# Leave blank to show the URL pattern without assuming a username.
SUBSCRIPTION_USERNAME = ""
WORKER_FILE = Path(__file__).with_name("worker.js")

# Secrets are intentionally kept in process memory only and are lost on restart.
TOKENS: dict[int, str] = {}
WAITING_FOR_TOKEN: set[int] = set()
PENDING: dict[int, dict[str, Any]] = {}
# Per-user cache of the last worker list fetched for a given account, so
# opening a worker's info / deleting it doesn't need another list call.
MANAGE_CACHE: dict[int, dict[str, Any]] = {}
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

    async def list_scripts(self, account_id: str) -> list[dict[str, Any]]:
        """All Workers deployed on this account, newest metadata Cloudflare has."""
        data = await self.request("GET", f"/accounts/{account_id}/workers/scripts")
        return data.get("result", [])

    async def delete_script(self, account_id: str, script_name: str) -> None:
        await self.request("DELETE", f"/accounts/{account_id}/workers/scripts/{script_name}")

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


async def safe_edit(q, text: str, **kwargs) -> None:
    """Wraps callback_query.edit_message_text so one bad edit can't silently
    kill the rest of the handler (which is what made buttons look "stuck" -
    the tap was received and processed, but the reply never rendered and
    nothing was logged)."""
    try:
        await q.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return  # harmless: double-tap or identical re-render
        logger.exception("edit_message_text failed (BadRequest): %s", e)
        try:
            await q.message.reply_text(text, **kwargs)
        except Exception:
            logger.exception("fallback reply_text also failed")
    except Exception:
        logger.exception("edit_message_text failed unexpectedly")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception while processing update: %s", update, exc_info=context.error)


def is_admin(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and chat.type == "private" and user.id == ADMIN_ID and ADMIN_ID > 0)


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Deploy", callback_data="deploy")],
        [
            InlineKeyboardButton("🔐 Set New Api", callback_data="token"),
            InlineKeyboardButton("🗄 Manage Panel", callback_data="manage"),
        ],
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
        await update.effective_chat.send_message("توکن قابل‌قبول به نظر نمی‌رسد؛ دوباره از دکمهٔ Set New Api استفاده کن.", reply_markup=main_keyboard())
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
            await safe_edit(q, 
                f"دیتابیس دقیقاً یک مورد پیدا نشد: `{D1_NAME}`. "
                "مطمئن شو اسم D1 در همین اکانت است و توکن دسترسی D1 Read دارد.",
                parse_mode="Markdown",
            )
            return
        account_subdomain = await cf.subdomain(str(account["id"]))
        if account_subdomain != EXPECTED_SUBDOMAIN:
            actual = f"{SCRIPT_NAME}.{account_subdomain}.workers.dev" if account_subdomain else "workers.dev برای این اکانت تنظیم نشده"
            await safe_edit(q, 
                f"برای اینکه لینک درخواستی دقیقاً ساخته شود، زیردامنهٔ اکانت باید `{EXPECTED_SUBDOMAIN}.workers.dev` باشد.\n"
                f"الان: `{actual}`\n"
                "زیردامنه را از داشبورد Cloudflare تنظیم کن، بعد دوباره تلاش کن. ربات آن را خودکار عوض نمی‌کند.",
                parse_mode="Markdown",
            )
            return
        exists = await cf.script_exists(str(account["id"]))
        if exists:
            await safe_edit(q, 
                f"Worker `{SCRIPT_NAME}` از قبل وجود دارد. اول از داخل 🗄 Manage Panel حذفش کن، بعد دوباره Deploy بزن.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗄 Manage Panel", callback_data="manage")]]),
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
        await safe_edit(q, 
            f"ساخت و دیپلوی Worker: `{SCRIPT_NAME}`\nاکانت: {account.get('name', 'Cloudflare')}\n"
            f"D1: `{D1_NAME}` با Binding به نام `DB`\n"
            f"الگوی لینک اشتراک: `https://{SCRIPT_NAME}.{account_subdomain}.workers.dev{subscription_path}`\n\n"
            "ادامه بدهم؟",
            parse_mode="Markdown", reply_markup=keyboard,
        )
    except CloudflareError as e:
        await safe_edit(q, f"Cloudflare رد کرد: {e}")
    except Exception:
        await safe_edit(q, "بررسی Cloudflare ناموفق بود. توکن، دسترسی‌ها و اتصال را بررسی کن.")
    finally:
        await cf.close()


async def begin_deployment(update: Update):
    q = update.callback_query
    token = TOKENS.get(q.from_user.id)
    if not token:
        await safe_edit(q, "اول از دکمهٔ Set New Api توکن Cloudflare را وارد کن.", reply_markup=main_keyboard())
        return
    cf = Cloudflare(token)
    try:
        await cf.request("GET", "/user/tokens/verify")
        accounts = await cf.accounts()
        if not accounts:
            await safe_edit(q, "این توکن به هیچ اکانتی دسترسی ندارد.")
            return
        if len(accounts) == 1:
            await prepare_account(update, accounts[0], "deploy", token)
        else:
            rows = [[InlineKeyboardButton(str(a.get("name", "Account"))[:50], callback_data=f"account:deploy:{a['id']}")] for a in accounts]
            await safe_edit(q, "اکانت Cloudflare را انتخاب کن:", reply_markup=InlineKeyboardMarkup(rows))
    except CloudflareError as e:
        await safe_edit(q, f"اعتبارسنجی یا خواندن اکانت‌ها ناموفق بود: {e}")
    except Exception:
        await safe_edit(q, "ارتباط با Cloudflare ناموفق بود؛ دوباره امتحان کن.")
    finally:
        await cf.close()


async def begin_manage(update: Update):
    q = update.callback_query
    uid = q.from_user.id
    token = TOKENS.get(uid)
    if not token:
        await safe_edit(q, "اول از دکمهٔ Set New Api توکن Cloudflare را وارد کن.", reply_markup=main_keyboard())
        return
    cf = Cloudflare(token)
    try:
        await cf.request("GET", "/user/tokens/verify")
        accounts = await cf.accounts()
        if not accounts:
            await safe_edit(q, "این توکن به هیچ اکانتی دسترسی ندارد.", reply_markup=main_keyboard())
            return
    except CloudflareError as e:
        await safe_edit(q, f"اعتبارسنجی یا خواندن اکانت‌ها ناموفق بود: {e}", reply_markup=main_keyboard())
        return
    except Exception:
        await safe_edit(q, "ارتباط با Cloudflare ناموفق بود؛ دوباره امتحان کن.", reply_markup=main_keyboard())
        return
    finally:
        await cf.close()

    if len(accounts) == 1:
        await show_worker_list(update, str(accounts[0]["id"]), token)
    else:
        rows = [[InlineKeyboardButton(str(a.get("name", "Account"))[:50], callback_data=f"manageaccount:{a['id']}")] for a in accounts]
        rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="manageback")])
        await safe_edit(q, "اکانت Cloudflare را انتخاب کن:", reply_markup=InlineKeyboardMarkup(rows))


async def show_worker_list(update: Update, account_id: str, token: str):
    """Manage Panel: list every Worker deployed on this account as a button."""
    q = update.callback_query
    uid = q.from_user.id
    cf = Cloudflare(token)
    try:
        scripts = await cf.list_scripts(account_id)
    except CloudflareError as e:
        await safe_edit(q, f"Cloudflare رد کرد: {e}", reply_markup=main_keyboard())
        return
    except Exception:
        await safe_edit(q, "خواندن لیست Worker ها ناموفق بود.", reply_markup=main_keyboard())
        return
    finally:
        await cf.close()

    MANAGE_CACHE[uid] = {
        "token": token,
        "account_id": account_id,
        "scripts": {str(s.get("id")): s for s in scripts},
    }

    if not scripts:
        await safe_edit(q, 
            "هیچ Worker ای روی این اکانت دیپلوی نشده.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="manageback")]]),
        )
        return

    rows = [
        [InlineKeyboardButton(f"⚙️ {s.get('id')}", callback_data=f"workerinfo:{account_id}:{s.get('id')}")]
        for s in scripts
    ]
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="manageback")])
    await safe_edit(q, 
        f"Worker های دیپلوی‌شده روی این اکانت ({len(scripts)}):",
        reply_markup=InlineKeyboardMarkup(rows),
    )


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
        await safe_edit(q, "توکن Cloudflare را در همین گفت‌وگوی خصوصی بفرست. پیام توکن بعد از دریافت حذف می‌شود.")

    elif data == "forget":
        TOKENS.pop(uid, None)
        PENDING.pop(uid, None)
        MANAGE_CACHE.pop(uid, None)
        WAITING_FOR_TOKEN.discard(uid)
        await safe_edit(q, "توکن و اطلاعات موقت پاک شد.", reply_markup=main_keyboard())

    elif data == "deploy":
        await begin_deployment(update)

    elif data == "manage":
        await begin_manage(update)

    elif data == "manageback":
        await safe_edit(q, "𝑯𝒖𝒔𝒕𝒆𝑹𝑰𝑿 deploy bot", reply_markup=main_keyboard())

    elif data.startswith("account:"):
        _, action, account_id = data.split(":", 2)
        token = TOKENS.get(uid)
        if not token:
            await safe_edit(q, "توکن پاک شده؛ دوباره واردش کن.", reply_markup=main_keyboard())
            return
        cf = Cloudflare(token)
        try:
            accounts = await cf.accounts()
            account = next((a for a in accounts if str(a.get("id")) == account_id), None)
            if not account:
                await safe_edit(q, "اکانت انتخاب‌شده دیگر در دسترس نیست.")
                return
        finally:
            await cf.close()
        await prepare_account(update, account, action, token)

    elif data.startswith("manageaccount:"):
        account_id = data.split(":", 1)[1]
        token = TOKENS.get(uid)
        if not token:
            await safe_edit(q, "توکن پاک شده؛ دوباره واردش کن.", reply_markup=main_keyboard())
            return
        await show_worker_list(update, account_id, token)

    elif data.startswith("workerback:"):
        account_id = data.split(":", 1)[1]
        token = TOKENS.get(uid)
        if not token:
            await safe_edit(q, "توکن پاک شده؛ دوباره واردش کن.", reply_markup=main_keyboard())
            return
        await show_worker_list(update, account_id, token)

    elif data.startswith("workerinfo:"):
        _, account_id, script_name = data.split(":", 2)
        cache = MANAGE_CACHE.get(uid)
        token = TOKENS.get(uid)
        if not token or not cache or cache.get("account_id") != account_id:
            await safe_edit(q, "اطلاعات منقضی شده؛ دوباره وارد Manage Panel شو.", reply_markup=main_keyboard())
            return
        script = cache["scripts"].get(script_name)
        if not script:
            await safe_edit(q, 
                "این Worker دیگر پیدا نشد؛ لیست به‌روزرسانی می‌شود.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"workerback:{account_id}")]]),
            )
            return
        created = script.get("created_on", "—")
        modified = script.get("modified_on", "—")
        usage_model = script.get("usage_model", "—")
        text = (
            f"⚙️ Worker: `{script_name}`\n"
            f"ساخته‌شده: `{created}`\n"
            f"آخرین ویرایش: `{modified}`\n"
            f"مدل مصرف: `{usage_model}`"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 حذف", callback_data=f"workerdel:{account_id}:{script_name}")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"workerback:{account_id}")],
        ])
        await safe_edit(q, text, parse_mode="Markdown", reply_markup=keyboard)

    elif data.startswith("workerdel:"):
        _, account_id, script_name = data.split(":", 2)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ تأیید حذف", callback_data=f"workerdelconfirm:{account_id}:{script_name}"),
            InlineKeyboardButton("لغو", callback_data=f"workerinfo:{account_id}:{script_name}"),
        ]])
        await safe_edit(q, 
            f"مطمئنی می‌خوای Worker `{script_name}` حذف بشه؟ این عملیات برگشت‌ناپذیر است.",
            parse_mode="Markdown", reply_markup=keyboard,
        )

    elif data.startswith("workerdelconfirm:"):
        _, account_id, script_name = data.split(":", 2)
        token = TOKENS.get(uid)
        if not token:
            await safe_edit(q, "توکن پاک شده؛ دوباره واردش کن.", reply_markup=main_keyboard())
            return
        cf = Cloudflare(token)
        try:
            await safe_edit(q, "در حال حذف Worker…")
            await cf.delete_script(account_id, script_name)
            cache = MANAGE_CACHE.get(uid)
            if cache and cache.get("account_id") == account_id:
                cache["scripts"].pop(script_name, None)
            await safe_edit(q, 
                f"✅ Worker `{script_name}` حذف شد.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به لیست", callback_data=f"workerback:{account_id}")]]),
            )
        except CloudflareError as e:
            await safe_edit(q, f"❌ Cloudflare: {e}", reply_markup=main_keyboard())
        except Exception:
            await safe_edit(q, "❌ حذف ناموفق بود.", reply_markup=main_keyboard())
        finally:
            await cf.close()

    elif data.startswith("confirm:"):
        action = data.split(":", 1)[1]
        plan = PENDING.get(uid)
        if not plan or plan.get("action") != action:
            await safe_edit(q, "درخواست منقضی شده؛ دوباره شروع کن.", reply_markup=main_keyboard())
            return
        cf = Cloudflare(plan["token"])
        try:
            await safe_edit(q, "در حال انجام عملیات Cloudflare…")
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
            await safe_edit(q, 
                result + "\n\n"
                "USERNAME_IN_D1 باید نام کاربری واقعیِ موجود در D1 باشد؛ نام ورود پنل یا تلگرام نیست.",
                parse_mode="Markdown", reply_markup=main_keyboard(), disable_web_page_preview=True,
            )
        except CloudflareError as e:
            await safe_edit(q, f"❌ Cloudflare: {e}", reply_markup=main_keyboard())
        except Exception:
            await safe_edit(q, "❌ عملیات کامل نشد.", reply_markup=main_keyboard())
        finally:
            PENDING.pop(uid, None)
            await cf.close()

    elif data == "cancel":
        PENDING.pop(uid, None)
        await safe_edit(q, "لغو شد.", reply_markup=main_keyboard())


async def clear_webhook(app: Application) -> None:
    # A leftover webhook (from a previous deploy, a crash-restart race, or
    # manual testing) blocks get_updates with a Conflict error. Clearing it
    # on every startup makes polling self-healing.
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass


def main():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN env var is not set. Set it on the host running "
            "this bot (Railway → Variables) — no hardcoded fallback on purpose, "
            "to avoid two deployments ever sharing one token."
        )
    if ADMIN_ID <= 0:
        raise SystemExit("Set TELEGRAM_ADMIN_ID first.")
    app = Application.builder().token(bot_token).post_init(clear_webhook).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    logger.info("HusteRIX deploy bot starting… token ends in ...%s", bot_token[-6:])
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
