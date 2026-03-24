#!/usr/bin/env python3
"""
WhatsApp Number Checker Telegram Bot
Features: Fast parallel checking, TXT file support, CSV export, owner/premium system
"""

import logging
import re
import os
import asyncio
import csv
import io
import base64
from typing import List, Optional
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
)
import aiohttp

# ==========================================
# CONFIGURATION
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WHATSAPP_API_URL = os.getenv("WHATSAPP_API_URL", "http://localhost:3001")

# Owner ID - has full access
OWNER_ID = 5926172220

# Track WA connection state for notifications
_wa_was_connected = False
_wa_notified = False

# Premium users set
PREMIUM_USERS = set()
PREMIUM_USERS.add(OWNER_ID)

# Bot mode: "paid" = only premium users can check, "free" = everyone can check (with limits)
BOT_MODE = "paid"

# Daily check limit for free users (0 = unlimited)
FREE_CHECK_LIMIT = 5

# Track daily usage per user: {user_id: {"date": "YYYY-MM-DD", "count": int}}
USER_DAILY_USAGE = {}

# Per-user custom limits: {user_id: limit} (overrides default for that user)
USER_CUSTOM_LIMITS = {}

# ==========================================
# LOGGING
# ==========================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BULK_MODE = 1
WAITING_PURCHASE = 2

OWNER_PENDING_ACTION = {}


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def is_premium(user_id: int) -> bool:
    return user_id in PREMIUM_USERS or is_owner(user_id)


def get_user_limit(user_id: int) -> int:
    """Get the daily check limit for a user (0 = unlimited)"""
    if is_owner(user_id):
        return 0
    if user_id in USER_CUSTOM_LIMITS:
        return USER_CUSTOM_LIMITS[user_id]
    if is_premium(user_id):
        return 0
    return FREE_CHECK_LIMIT


def can_check(user_id: int) -> tuple:
    """Returns (allowed: bool, remaining: int or None, message: str)"""
    global BOT_MODE, FREE_CHECK_LIMIT, USER_DAILY_USAGE

    if is_owner(user_id):
        return (True, None, "")

    if not is_premium(user_id) and BOT_MODE == "paid":
        return (False, 0, "⭐ *Premium Feature*\n\nNumber checking requires a premium subscription.\nTap /start → 💰 Purchase to upgrade!")

    limit = get_user_limit(user_id)
    if limit == 0:
        return (True, None, "")

    today = datetime.now().strftime("%Y-%m-%d")
    usage = USER_DAILY_USAGE.get(user_id, {"date": "", "count": 0})

    if usage["date"] != today:
        usage = {"date": today, "count": 0}
        USER_DAILY_USAGE[user_id] = usage

    remaining = limit - usage["count"]
    if remaining <= 0:
        return (False, 0, f"⚠️ *Daily Limit Reached*\n\nAapki aaj ki limit (*{limit} checks*) khatam ho gayi.\n\nKal dobara try karein ya premium upgrade karein!")

    return (True, remaining, "")


def use_check(user_id: int, count: int = 1):
    """Record check usage for a user"""
    global USER_DAILY_USAGE
    if is_owner(user_id):
        return
    limit = get_user_limit(user_id)
    if limit > 0:
        today = datetime.now().strftime("%Y-%m-%d")
        usage = USER_DAILY_USAGE.get(user_id, {"date": today, "count": 0})
        if usage["date"] != today:
            usage = {"date": today, "count": 0}
        usage["count"] += count
        USER_DAILY_USAGE[user_id] = usage


class BaileysAPIClient:
    """Client for Baileys WhatsApp API — per-user sessions"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def check_connection(self, user_id: str = None) -> dict:
        try:
            params = {}
            if user_id:
                params["userId"] = str(user_id)
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base_url}/status", params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    return await resp.json()
        except Exception as e:
            return {"connected": False, "error": str(e)}

    async def request_pairing_code(self, phone_number: str, user_id: str = None) -> dict:
        clean_number = re.sub(r"\D", "", phone_number)
        try:
            payload = {"number": clean_number}
            if user_id:
                payload["userId"] = str(user_id)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/pair",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    return await response.json()
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def check_number(self, phone_number: str, user_id: str = None) -> dict:
        clean_number = re.sub(r"\D", "", phone_number)
        if len(clean_number) < 10:
            return {"success": False, "error": "Too short", "number": clean_number}
        try:
            payload = {"number": clean_number}
            if user_id:
                payload["userId"] = str(user_id)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/check",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        data.setdefault("number", clean_number)
                        return data
                    else:
                        return {"success": False, "error": f"API {response.status}", "number": clean_number}
        except Exception as e:
            return {"success": False, "error": str(e), "number": clean_number}

    async def check_bulk(self, numbers: List[str], user_id: str = None) -> List[dict]:
        try:
            payload = {"numbers": numbers}
            if user_id:
                payload["userId"] = str(user_id)
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/check-bulk",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data.get("results", [])
                    else:
                        return [{"success": False, "number": n, "error": "API error"} for n in numbers]
        except Exception as e:
            return [{"success": False, "number": n, "error": str(e)} for n in numbers]

    async def disconnect_session(self, user_id: str) -> dict:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/disconnect",
                    json={"userId": str(user_id)},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    return await response.json()
        except Exception as e:
            return {"success": False, "error": str(e)}


class WhatsAppCheckerBot:
    """Telegram Bot Handler"""

    def __init__(self):
        self.api = BaileysAPIClient(WHATSAPP_API_URL)

    def format_phone(self, number: str) -> str:
        """Format number with + prefix for display"""
        clean = re.sub(r"\D", "", number)

        # Always start with +
        if clean.startswith("967") and len(clean) == 12:
            return f"+967 {clean[3:6]} {clean[6:9]} {clean[9:]}"
        if clean.startswith("966") and len(clean) == 12:
            return f"+966 {clean[3:5]} {clean[5:8]} {clean[8:]}"
        if clean.startswith("971") and len(clean) == 12:
            return f"+971 {clean[3:5]} {clean[5:8]} {clean[8:]}"
        if clean.startswith("91") and len(clean) == 12:
            return f"+91 {clean[2:7]} {clean[7:]}"
        if clean.startswith("92") and len(clean) == 12:
            return f"+92 {clean[2:5]} {clean[5:]}"
        if clean.startswith("1") and len(clean) == 11:
            return f"+1 ({clean[1:4]}) {clean[4:7]}-{clean[7:]}"
        if clean.startswith("44") and len(clean) == 12:
            return f"+44 {clean[2:5]} {clean[5:]}"

        return f"+{clean}"

    def clean_number(self, text: str) -> Optional[str]:
        if not text:
            return None
        digits = re.sub(r"\D", "", text)
        if 10 <= len(digits) <= 15:
            return digits
        return None

    def extract_numbers(self, text: str) -> List[str]:
        if not text:
            return []
        parts = re.split(r"[\n\r,;|\s\t]+", text.strip())
        numbers = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            part = re.sub(r"^(Number|Phone|Tel|Mobile|Contact|WhatsApp)[:\s#]*", "", part, flags=re.I)
            part = re.sub(r"^\+", "", part)
            clean = self.clean_number(part)
            if clean and clean not in numbers:
                numbers.append(clean)
        return numbers

    def make_csv_file(self, results: List[dict]) -> io.BytesIO:
        """Create CSV file from results"""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Number", "WhatsApp Status", "Formatted Number"])
        for r in results:
            num = r.get("number", "")
            formatted = self.format_phone(num) if num else ""
            if r.get("success") and r.get("exists"):
                status = "Registered"
            elif r.get("success") and not r.get("exists"):
                status = "Not Registered"
            else:
                status = f"Failed: {r.get('error', 'Unknown')}"
            writer.writerow([f"+{num}" if num and not num.startswith("+") else num, status, formatted])
        content = output.getvalue().encode("utf-8-sig")
        file_obj = io.BytesIO(content)
        file_obj.name = f"whatsapp_check_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return file_obj

    # ============== COMMANDS ==============

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_id = user.id

        async with self.api:
            status = await self.api.check_connection(str(user_id))

        conn_status = "✅ Connected" if status.get("connected") else "⚠️ Not connected"

        if is_owner(user_id):
            role_tag = "👑 *Owner*"
        elif is_premium(user_id):
            role_tag = "⭐ *Premium User*"
        else:
            role_tag = "👤 *Free User*"

        welcome = f"""
👋 *Hello {user.first_name}!* {role_tag}

I'm a *WhatsApp Number Checker* bot.

🔌 *WhatsApp Status:* {conn_status}

*Features:*
• ⚡ Fast parallel checking
• 📁 TXT file support
• 📊 Real-time progress
• 📈 CSV export
• ♾️ Unlimited checks

*Send me:*
• Any phone number (with country code)
• TXT file with numbers

⚡ *Powered by MuDaSir aWaN*
        """

        keyboard = [
            [InlineKeyboardButton("🔍 Single Check", callback_data="check"),
             InlineKeyboardButton("📋 Bulk Check", callback_data="bulk")],
            [InlineKeyboardButton("📁 TXT File", callback_data="file")],
            [InlineKeyboardButton("💰 Purchase", callback_data="purchase")],
        ]
        if is_owner(update.effective_user.id):
            keyboard.append([InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")])

        await update.message.reply_text(
            welcome, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = """
📖 *Help & Information*

*How to use:*
1️⃣ *Single Check:* Send any number directly
2️⃣ *Bulk Paste:* Use /bulk and paste numbers
3️⃣ *TXT File:* Send .txt file with numbers

*Supported formats:*
• `+92 300 1234567`
• `923001234567`
• `Number: +91 98765 43210`

*Features:*
⚡ Parallel processing (fast)
📊 Real-time progress updates
♾️ Unlimited checks
📄 Shows ALL results
📈 CSV export for spreadsheet view
🔢 Numbers displayed with + prefix
📋 Copy numbers with one click

*Commands:*
/start - Main menu
/bulk - Paste multiple numbers
/status - WhatsApp connection status
/cancel - Cancel operation
        """
        if is_owner(update.effective_user.id):
            help_text += """
*Owner Commands:*
/addpremium <user_id> - Add premium user
/removepremium <user_id> - Remove premium user
/listpremium - List all premium users
            """
        await update.message.reply_text(help_text, parse_mode="Markdown")

    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        async with self.api:
            status = await self.api.check_connection(user_id)

        if status.get("connected"):
            text = "✅ *WhatsApp Connected*\n\nAapka WhatsApp linked aur ready hai.\n\nUse /check ya number send karo."
        else:
            text = "⚠️ *WhatsApp Not Connected*\n\nApna WhatsApp connect karne ke liye:\n`/pair <your_number>`\n\nExample: `/pair 923001234567`"

        keyboard = [
            [InlineKeyboardButton("🔍 Check Number", callback_data="check"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu")],
        ]
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    async def pair_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)

        if not context.args:
            await update.message.reply_text(
                "📱 *Pair Your WhatsApp*\n\n"
                "Usage: `/pair <phone_number>`\n"
                "Example: `/pair 923001234567`\n\n"
                "Country code zaroori hai!\n"
                "Har user apna WhatsApp pair kar sakta hai.",
                parse_mode="Markdown"
            )
            return

        phone = context.args[0]
        wait_msg = await update.message.reply_text("⏳ Pairing code generate ho raha hai...")

        try:
            async with self.api:
                status = await self.api.check_connection(user_id)

            if status.get("connected"):
                await wait_msg.edit_text("✅ *Aapka WhatsApp pehle se connected hai!*\n\nNumbers check karne ke liye ready.", parse_mode="Markdown")
                return

            async with self.api:
                result = await self.api.request_pairing_code(phone, user_id)

            if result.get("success"):
                code = result.get("code", "")
                await wait_msg.edit_text(
                    f"🔗 *Pairing Code:* `{code}`\n\n"
                    f"📱 *Kaise use karein:*\n"
                    f"1. WhatsApp kholo\n"
                    f"2. Settings → *Linked Devices*\n"
                    f"3. *Link a Device* tap karo\n"
                    f"4. *Link with phone number instead* tap karo\n"
                    f"5. Yeh code enter karo: `{code}`\n\n"
                    f"✅ Code enter karne ke baad bot ready ho jayega!",
                    parse_mode="Markdown"
                )
            else:
                error = result.get("error", "Unknown error")
                await wait_msg.edit_text(f"❌ Error: `{error}`\nDobara try karo.", parse_mode="Markdown")

        except Exception as e:
            await wait_msg.edit_text(f"❌ Error: `{str(e)}`", parse_mode="Markdown")

    async def qrlink_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send WhatsApp QR code as image in chat"""
        user_id = str(update.effective_user.id)

        wait_msg = await update.message.reply_text("⏳ QR code fetch ho raha hai...")

        try:
            async with self.api:
                status = await self.api.check_connection(user_id)

            if status.get("connected"):
                await wait_msg.edit_text("✅ *Aapka WhatsApp pehle se connected hai!*\n\nKoi QR scan ki zaroorat nahi.", parse_mode="Markdown")
                return

            qr_base64 = status.get("qrBase64")

            if not qr_base64:
                await wait_msg.edit_text(
                    "⚠️ *QR code abhi available nahi hai.*\n\n"
                    "Server abhi start ho raha hai. 10-15 seconds baad dobara try karo:\n`/qrlink`",
                    parse_mode="Markdown"
                )
                return

            # Convert base64 to image bytes
            img_data = qr_base64.split(",")[1] if "," in qr_base64 else qr_base64
            img_bytes = base64.b64decode(img_data)
            img_file = io.BytesIO(img_bytes)
            img_file.name = "whatsapp_qr.png"

            await wait_msg.delete()
            await update.message.reply_photo(
                photo=img_file,
                caption=(
                    "📱 *WhatsApp QR Code*\n\n"
                    "Scan karne ka tarika:\n"
                    "1. WhatsApp kholo\n"
                    "2. Settings → *Linked Devices*\n"
                    "3. *Link a Device* tap karo\n"
                    "4. Yeh QR scan karo\n\n"
                    "✅ Scan ke baad bot ready ho jayega!"
                ),
                parse_mode="Markdown"
            )

        except Exception as e:
            logger.error(f"QR fetch error: {e}")
            await wait_msg.edit_text(f"❌ Error: `{str(e)}`\nDobara try karo: `/qrlink`", parse_mode="Markdown")

    async def add_premium(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("❌ Owner only command.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /addpremium <user_id>")
            return
        try:
            uid = int(context.args[0])
            PREMIUM_USERS.add(uid)
            await update.message.reply_text(f"✅ User `{uid}` added as premium!", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")

    async def remove_premium(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("❌ Owner only command.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /removepremium <user_id>")
            return
        try:
            uid = int(context.args[0])
            if uid == OWNER_ID:
                await update.message.reply_text("❌ Cannot remove owner.")
                return
            PREMIUM_USERS.discard(uid)
            await update.message.reply_text(f"✅ User `{uid}` removed from premium.", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID.")

    async def list_premium(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("❌ Owner only command.")
            return
        if not PREMIUM_USERS:
            await update.message.reply_text("No premium users.")
            return
        user_list = "\n".join([f"• `{uid}`{' 👑 Owner' if uid == OWNER_ID else ''}" for uid in PREMIUM_USERS])
        await update.message.reply_text(f"⭐ *Premium Users:*\n{user_list}", parse_mode="Markdown")

    async def set_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        global BOT_MODE
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("❌ Owner only command.")
            return
        if not context.args or context.args[0].lower() not in ("free", "paid"):
            await update.message.reply_text(
                f"📋 *Bot Mode*\n\n"
                f"Current: *{BOT_MODE.upper()}*\n\n"
                f"Usage: `/setmode free` or `/setmode paid`\n\n"
                f"• *FREE* — sab log check kar sakte hain (daily limit ke saath)\n"
                f"• *PAID* — sirf premium users check kar sakte hain",
                parse_mode="Markdown"
            )
            return
        BOT_MODE = context.args[0].lower()
        if BOT_MODE == "free":
            await update.message.reply_text(
                f"✅ Bot mode: *FREE*\n\n"
                f"Ab sab users check kar sakte hain.\n"
                f"Daily limit: *{FREE_CHECK_LIMIT}* checks per user\n"
                f"(0 = unlimited)\n\n"
                f"Limit change: `/setlimit <number>`",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"✅ Bot mode: *PAID*\n\n"
                f"Ab sirf premium users check kar sakte hain.\n"
                f"Free users ko purchase karna padega.",
                parse_mode="Markdown"
            )

    async def set_limit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        global FREE_CHECK_LIMIT
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("❌ Owner only command.")
            return
        if not context.args:
            await update.message.reply_text(
                f"📋 *Check Limit*\n\n"
                f"Current: *{FREE_CHECK_LIMIT}* checks/day per free user\n"
                f"(0 = unlimited)\n\n"
                f"Usage: `/setlimit <number>`\n"
                f"Example: `/setlimit 10` — 10 checks per day\n"
                f"Example: `/setlimit 0` — unlimited",
                parse_mode="Markdown"
            )
            return
        try:
            limit = int(context.args[0])
            if limit < 0:
                await update.message.reply_text("❌ Limit 0 ya usse zyada hona chahiye.")
                return
            FREE_CHECK_LIMIT = limit
            if limit == 0:
                await update.message.reply_text("✅ Free users ke liye *unlimited checks* set ho gaye!", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"✅ Free users ke liye daily limit: *{limit} checks*", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ Invalid number. Example: `/setlimit 10`", parse_mode="Markdown")

    async def set_user_limit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        global USER_CUSTOM_LIMITS
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("❌ Owner only command.")
            return
        if not context.args or len(context.args) < 2:
            lines = ["📋 *User Limits*\n"]
            if USER_CUSTOM_LIMITS:
                for uid, lim in USER_CUSTOM_LIMITS.items():
                    lines.append(f"• `{uid}` → *{lim}*/day")
            else:
                lines.append("Koi custom limit set nahi hai.")
            lines.append(f"\nUsage: `/setuserlimit <user_id> <limit>`")
            lines.append(f"Example: `/setuserlimit 123456 20`")
            lines.append(f"Remove: `/setuserlimit <user_id> 0`")
            await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
            return
        try:
            uid = int(context.args[0])
            limit = int(context.args[1])
            if limit < 0:
                await update.message.reply_text("❌ Limit 0 ya usse zyada hona chahiye.")
                return
            if limit == 0:
                USER_CUSTOM_LIMITS.pop(uid, None)
                await update.message.reply_text(f"✅ User `{uid}` ki custom limit hata di — ab default limit lagegi.", parse_mode="Markdown")
            else:
                USER_CUSTOM_LIMITS[uid] = limit
                await update.message.reply_text(f"✅ User `{uid}` ke liye daily limit: *{limit} checks*", parse_mode="Markdown")
        except ValueError:
            await update.message.reply_text("❌ Invalid. Example: `/setuserlimit 123456 20`", parse_mode="Markdown")

    async def bot_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update.effective_user.id):
            await update.message.reply_text("❌ Owner only command.")
            return
        premium_count = len(PREMIUM_USERS)
        limit_text = "Unlimited" if FREE_CHECK_LIMIT == 0 else str(FREE_CHECK_LIMIT)
        custom_count = len(USER_CUSTOM_LIMITS)
        await update.message.reply_text(
            f"⚙️ *Bot Settings*\n\n"
            f"🔹 Mode: *{BOT_MODE.upper()}*\n"
            f"🔹 Free user limit: *{limit_text}* checks/day\n"
            f"🔹 Premium users: *{premium_count}*\n"
            f"🔹 Custom limits: *{custom_count}* users\n\n"
            f"*Commands:*\n"
            f"• `/setmode free` — sab ke liye free\n"
            f"• `/setmode paid` — sirf premium\n"
            f"• `/setlimit <n>` — free users ki default limit\n"
            f"• `/setuserlimit <id> <n>` — kisi user ki custom limit\n"
            f"• `/addpremium <id>` — premium add\n"
            f"• `/removepremium <id>` — premium remove\n"
            f"• `/listpremium` — premium list",
            parse_mode="Markdown"
        )

    async def check_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        allowed, remaining, msg = can_check(user_id)
        if not allowed:
            await update.message.reply_text(msg, parse_mode="Markdown")
            return
        if not context.args:
            await update.message.reply_text("❌ Usage: `/check <number>`\nExample: `/check +923001234567`", parse_mode="Markdown")
            return
        phone = " ".join(context.args)
        await self.process_single_check(update, context, phone)

    async def process_single_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE, phone_input: str):
        user_id = update.effective_user.id
        allowed, remaining, msg = can_check(user_id)
        if not allowed:
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        clean = self.clean_number(phone_input)
        if not clean:
            await update.message.reply_text("❌ Invalid number. Need 10-15 digits with country code.", parse_mode="Markdown")
            return

        formatted = self.format_phone(clean)
        status_msg = await update.message.reply_text(f"🔍 Checking `{formatted}`...", parse_mode="Markdown")

        async with self.api:
            result = await self.api.check_number(clean, str(user_id))

        use_check(user_id, 1)

        if result.get("success"):
            if result.get("exists"):
                text = f"✅ *WhatsApp Active*\n\n📱 `{formatted}`\n✓ On WhatsApp\n\n*Copy number:*\n`+{clean}`"
            else:
                text = f"❌ *Not on WhatsApp*\n\n📱 `{formatted}`\n✗ No account found\n\n*Number:*\n`+{clean}`"
        else:
            text = f"⚠️ *Check Failed*\n\n📱 `{formatted}`\nError: `{result.get('error', 'Unknown')}`"

        if remaining is not None and remaining > 0:
            text += f"\n\n📊 Remaining today: *{remaining - 1}*"

        keyboard = [
            [InlineKeyboardButton("🔍 Check Another", callback_data="check"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu")],
        ]
        await status_msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    async def bulk_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        allowed, remaining, msg = can_check(user_id)
        if not allowed:
            await update.message.reply_text(msg, parse_mode="Markdown")
            return ConversationHandler.END

        await update.message.reply_text(
            "📋 *Bulk Paste Mode*\n\n"
            "Paste multiple numbers (one per line or separated by commas).\n"
            "⚡ Unlimited checks!\n"
            "📊 Real-time progress every 10 numbers\n\n"
            "Format:\n```\n923001234567\n923009876543\n923001111111\n```\n\n"
            "Or: `923001234567, 923009876543`\n\n"
            "Send /cancel to exit.",
            parse_mode="Markdown",
        )
        return BULK_MODE

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        allowed, remaining, msg = can_check(user_id)
        if not allowed:
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        document = update.message.document
        if not document.file_name.endswith(".txt"):
            await update.message.reply_text("❌ Please upload a .txt file only.", parse_mode="Markdown")
            return

        if document.file_size > 1024 * 1024:
            await update.message.reply_text("❌ File too large. Max 1MB.", parse_mode="Markdown")
            return

        await update.message.reply_text("📁 *Processing file...*", parse_mode="Markdown")

        file = await context.bot.get_file(document.file_id)
        file_content = await file.download_as_bytearray()

        try:
            content = file_content.decode("utf-8")
        except:
            try:
                content = file_content.decode("latin-1")
            except:
                await update.message.reply_text("❌ Could not read file encoding.")
                return

        numbers = self.extract_numbers(content)
        if not numbers:
            await update.message.reply_text(
                "❌ No valid phone numbers found in file.\nMake sure numbers have country code (10-15 digits).",
                parse_mode="Markdown",
            )
            return

        await update.message.reply_text(
            f"✅ Found *{len(numbers)}* numbers in file.\n⚡ Starting parallel check...",
            parse_mode="Markdown",
        )
        await self.process_bulk_with_updates(update, context, numbers)

    async def bulk_process(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        if text.lower().strip() == "/cancel":
            await update.message.reply_text("❌ Cancelled.")
            return ConversationHandler.END

        numbers = self.extract_numbers(text)
        if not numbers:
            await update.message.reply_text(
                "❌ No valid numbers found.\nSend numbers like:\n`923001234567`\n`923009876543`\n\nOr use /cancel.",
                parse_mode="Markdown",
            )
            return BULK_MODE

        await self.process_bulk_with_updates(update, context, numbers)
        return ConversationHandler.END

    async def process_bulk_with_updates(self, update: Update, context: ContextTypes.DEFAULT_TYPE, numbers: List[str]):
        total = len(numbers)
        user_id = str(update.effective_user.id)

        progress_msg = await update.message.reply_text(
            f"⏳ *Starting Check*\n"
            f"📊 Total: *{total}* numbers\n"
            f"✅ Checked: 0\n"
            f"📈 Progress: 0%\n\n"
            f"⚡ Processing...",
            parse_mode="Markdown",
        )

        results = []
        start_time = datetime.now()
        last_ui_update = 0
        batch_size = 20
        delay_between_batches = 0

        async with self.api:
            for i in range(0, total, batch_size):
                batch = numbers[i: i + batch_size]
                batch_results = await asyncio.gather(
                    *[self.api.check_number(n, user_id) for n in batch]
                )
                results.extend(batch_results)
                checked = len(results)

                # Update progress UI every batch or at end
                if checked - last_ui_update >= 20 or checked == total:
                    registered = len([r for r in results if r.get("success") and r.get("exists")])
                    not_found = len([r for r in results if r.get("success") and not r.get("exists")])
                    failed = len([r for r in results if not r.get("success")])
                    progress_pct = int((checked / total) * 100)
                    elapsed = (datetime.now() - start_time).total_seconds()
                    speed = checked / elapsed if elapsed > 0 else 0
                    try:
                        await progress_msg.edit_text(
                            f"⏳ *Checking...*\n"
                            f"📊 Total: *{total}*\n"
                            f"✅ Checked: *{checked}*\n"
                            f"📈 Progress: *{progress_pct}%*\n"
                            f"🟢 Registered: *{registered}*\n"
                            f"🔴 Not found: *{not_found}*\n"
                            f"⚠️ Failed: *{failed}*\n\n"
                            f"⚡ Speed: {speed:.1f}/sec",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                    last_ui_update = checked

                # Small delay between batches
                if i + batch_size < total:
                    await asyncio.sleep(delay_between_batches)

        use_check(update.effective_user.id, total)
        await self.send_final_report(update, results, total, start_time)

    async def send_final_report(self, update: Update, results: List[dict], total: int, start_time: datetime):
        registered = [r for r in results if r.get("success") and r.get("exists")]
        not_reg = [r for r in results if r.get("success") and not r.get("exists")]
        failed = [r for r in results if not r.get("success")]
        duration = (datetime.now() - start_time).total_seconds()

        summary = (
            f"📋 *RESULTS*\n\n"
            f"• Total: *{total}*\n"
            f"• ✅ Registered: *{len(registered)}*\n"
            f"• ❌ Not found: *{len(not_reg)}*\n"
            f"• ⚠️ Failed: *{len(failed)}*\n"
            f"• ⏱ Time: {duration:.1f}s\n"
            f"• ⚡ Speed: {total / max(duration, 0.1):.1f}/sec"
        )
        await update.message.reply_text(summary, parse_mode="Markdown")

        if registered:
            chunks = []
            current_lines = [f"✅ *Registered ({len(registered)}):*"]
            for r in registered:
                num = r.get("number", "") or "unknown"
                line = f"`+{num}`"
                if len("\n".join(current_lines)) + len(line) + 1 > 3500:
                    chunks.append("\n".join(current_lines))
                    current_lines = [f"✅ *Registered (cont.):*"]
                current_lines.append(line)
            chunks.append("\n".join(current_lines))
            for chunk in chunks:
                await update.message.reply_text(chunk, parse_mode="Markdown")

        if not_reg:
            chunks = []
            current_lines = [f"❌ *Not Registered ({len(not_reg)}):*"]
            for r in not_reg:
                num = r.get("number", "") or "unknown"
                line = f"`+{num}`"
                if len("\n".join(current_lines)) + len(line) + 1 > 3500:
                    chunks.append("\n".join(current_lines))
                    current_lines = [f"❌ *Not Registered (cont.):*"]
                current_lines.append(line)
            chunks.append("\n".join(current_lines))
            for chunk in chunks:
                await update.message.reply_text(chunk, parse_mode="Markdown")

        if failed:
            chunks = []
            current_lines = [f"⚠️ *Failed ({len(failed)}):*"]
            for r in failed:
                num = r.get("number", "") or "unknown"
                error = str(r.get("error", "Unknown"))[:20]
                line = f"`+{num}` — {error}"
                if len("\n".join(current_lines)) + len(line) + 1 > 3500:
                    chunks.append("\n".join(current_lines))
                    current_lines = [f"⚠️ *Failed (cont.):*"]
                current_lines.append(line)
            chunks.append("\n".join(current_lines))
            for chunk in chunks:
                await update.message.reply_text(chunk, parse_mode="Markdown")

        keyboard = [
            [InlineKeyboardButton("🔄 New Check", callback_data="bulk"),
             InlineKeyboardButton("🏠 Menu", callback_data="menu")],
        ]
        await update.message.reply_text(
            f"🕐 {datetime.now().strftime('%H:%M:%S')}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def cancel_bulk(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("❌ Cancelled.")
        return ConversationHandler.END

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        global BOT_MODE, FREE_CHECK_LIMIT
        query = update.callback_query
        await query.answer()
        data = query.data
        user = query.from_user
        user_id = user.id

        if data == "menu":
            async with self.api:
                status = await self.api.check_connection(str(user_id))
            conn_status = "✅ Connected" if status.get("connected") else "⚠️ Not connected"

            if is_owner(user_id):
                role_tag = "👑 *Owner*"
            elif is_premium(user_id):
                role_tag = "⭐ *Premium User*"
            else:
                role_tag = "👤 *Free User*"

            welcome = f"""
👋 *Hello {user.first_name}!* {role_tag}

I'm a *WhatsApp Number Checker* bot.
🔌 *WhatsApp Status:* {conn_status}

*Features:*
• ⚡ Fast parallel checking
• 📁 TXT file support
• 📊 Real-time progress
• 📈 CSV spreadsheet export
• ♾️ Unlimited checks

⚡ *Powered by MuDaSir aWaN*
            """
            keyboard = [
                [InlineKeyboardButton("🔍 Single Check", callback_data="check"),
                 InlineKeyboardButton("📋 Bulk Check", callback_data="bulk")],
                [InlineKeyboardButton("📁 TXT File", callback_data="file")],
                [InlineKeyboardButton("💰 Purchase", callback_data="purchase")],
            ]
            if is_owner(user_id):
                keyboard.append([InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")])
            await query.edit_message_text(welcome, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

        elif data == "check":
            allowed, remaining, msg = can_check(user_id)
            if not allowed:
                await query.edit_message_text(
                    msg,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💰 Purchase", callback_data="purchase"), InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
                )
                return
            await query.edit_message_text(
                "🔍 *Check Number*\n\nSend me any phone number to check.\n\n📌 Include country code!\nExamples:\n• `+923001234567`\n• `923001234567`\n• `+1 408 374 2784`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
            )

        elif data == "bulk":
            allowed, remaining, msg = can_check(user_id)
            if not allowed:
                await query.edit_message_text(
                    msg,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💰 Purchase", callback_data="purchase"), InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
                )
                return
            await query.edit_message_text(
                "📋 *Bulk Check*\n\nUse /bulk command to start, then paste your numbers.\nOne per line or separated by commas.\n\nExample:\n```\n923001234567\n923009876543\n923001111111\n```",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
            )

        elif data == "file":
            allowed, remaining, msg = can_check(user_id)
            if not allowed:
                await query.edit_message_text(
                    msg,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💰 Purchase", callback_data="purchase"), InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
                )
                return
            await query.edit_message_text(
                "📁 *Upload TXT File*\n\nSend me a .txt file with phone numbers.\nOne number per line.\n\nFile example:\n```\n923001234567\n923009876543\n923001111111\n```\n\n✅ Up to 1MB file size",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]])
            )

        elif data == "purchase":
            await query.edit_message_text(
                "💰 *Purchase Premium Access*\n\n"
                "✅ Unlock all features:\n"
                "• Single & bulk number checking\n"
                "• TXT file upload support\n"
                "• CSV export (spreadsheet view)\n"
                "• Real-time progress updates\n"
                "• Unlimited checks\n\n"
                "💬 *Contact owner to purchase:*\n"
                "Send a message to: @mudasir\\_awan",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💬 Message Owner", url="https://t.me/mudasir_awan")],
                    [InlineKeyboardButton("🏠 Menu", callback_data="menu")],
                ])
            )

        elif data == "status_check":
            async with self.api:
                status = await self.api.check_connection(str(user_id))
            if status.get("connected"):
                text = "✅ *WhatsApp Connected*\n\nReady to check numbers!"
            else:
                text = "⚠️ *WhatsApp Not Connected*\n\nPehle `/pair <number>` se connect karo."
            await query.edit_message_text(text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="menu")]]))

        elif data == "owner_panel":
            if not is_owner(user_id):
                await query.answer("❌ Sirf owner access kar sakta hai.", show_alert=True)
                return
            count = len(PREMIUM_USERS)
            limit_text = "Unlimited" if FREE_CHECK_LIMIT == 0 else str(FREE_CHECK_LIMIT)
            mode_emoji = "🟢" if BOT_MODE == "free" else "🔴"
            kb = [
                [InlineKeyboardButton(f"{mode_emoji} Mode: {BOT_MODE.upper()}", callback_data="op_toggle_mode")],
                [InlineKeyboardButton(f"📊 Free Limit: {limit_text}/day", callback_data="op_setlimit")],
                [InlineKeyboardButton(f"⭐ Premium Users ({count})", callback_data="op_listpremium")],
                [InlineKeyboardButton("➕ Add Premium", callback_data="op_addpremium"),
                 InlineKeyboardButton("➖ Remove Premium", callback_data="op_removepremium")],
                [InlineKeyboardButton("🎯 Set User Limit", callback_data="op_setuserlimit")],
                [InlineKeyboardButton("⚙️ All Settings", callback_data="op_settings")],
                [InlineKeyboardButton("🏠 Menu", callback_data="menu")]
            ]
            await query.edit_message_text(
                f"👑 *Owner Panel*\n\n"
                f"🔹 Mode: *{BOT_MODE.upper()}*\n"
                f"🔹 Free limit: *{limit_text}*/day\n"
                f"🔹 Premium users: *{count}*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif data == "op_toggle_mode":
            if not is_owner(user_id):
                await query.answer("❌", show_alert=True)
                return
            BOT_MODE = "free" if BOT_MODE == "paid" else "paid"
            await query.answer(f"✅ Mode changed to {BOT_MODE.upper()}")
            count = len(PREMIUM_USERS)
            limit_text = "Unlimited" if FREE_CHECK_LIMIT == 0 else str(FREE_CHECK_LIMIT)
            mode_emoji = "🟢" if BOT_MODE == "free" else "🔴"
            kb = [
                [InlineKeyboardButton(f"{mode_emoji} Mode: {BOT_MODE.upper()}", callback_data="op_toggle_mode")],
                [InlineKeyboardButton(f"📊 Free Limit: {limit_text}/day", callback_data="op_setlimit")],
                [InlineKeyboardButton(f"⭐ Premium Users ({count})", callback_data="op_listpremium")],
                [InlineKeyboardButton("➕ Add Premium", callback_data="op_addpremium"),
                 InlineKeyboardButton("➖ Remove Premium", callback_data="op_removepremium")],
                [InlineKeyboardButton("🎯 Set User Limit", callback_data="op_setuserlimit")],
                [InlineKeyboardButton("⚙️ All Settings", callback_data="op_settings")],
                [InlineKeyboardButton("🏠 Menu", callback_data="menu")]
            ]
            await query.edit_message_text(
                f"👑 *Owner Panel*\n\n"
                f"🔹 Mode: *{BOT_MODE.upper()}*\n"
                f"🔹 Free limit: *{limit_text}*/day\n"
                f"🔹 Premium users: *{count}*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb)
            )

        elif data == "op_setlimit":
            if not is_owner(user_id):
                await query.answer("❌", show_alert=True)
                return
            OWNER_PENDING_ACTION[user_id] = "setlimit"
            await query.answer()
            await query.message.reply_text(
                "📊 *Set Free User Daily Limit*\n\nNumber bhejo (e.g. `10`, `50`)\n`0` = unlimited",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="op_cancel")]])
            )

        elif data == "op_addpremium":
            if not is_owner(user_id):
                await query.answer("❌", show_alert=True)
                return
            OWNER_PENDING_ACTION[user_id] = "addpremium"
            await query.answer()
            await query.message.reply_text(
                "➕ *Add Premium User*\n\nUser ID bhejo:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="op_cancel")]])
            )

        elif data == "op_removepremium":
            if not is_owner(user_id):
                await query.answer("❌", show_alert=True)
                return
            OWNER_PENDING_ACTION[user_id] = "removepremium"
            await query.answer()
            if PREMIUM_USERS:
                lines = ["➖ *Remove Premium User*\n\nUser ID bhejo ya neeche se select karo:\n"]
                for uid in PREMIUM_USERS:
                    lines.append(f"• `{uid}`")
                kb = [[InlineKeyboardButton(str(uid), callback_data=f"op_rmprem_{uid}")] for uid in list(PREMIUM_USERS)[:10]]
                kb.append([InlineKeyboardButton("❌ Cancel", callback_data="op_cancel")])
                await query.message.reply_text("\n".join(lines), parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(kb))
            else:
                OWNER_PENDING_ACTION.pop(user_id, None)
                await query.message.reply_text("📋 Koi premium user nahi hai.")

        elif data.startswith("op_rmprem_"):
            if not is_owner(user_id):
                await query.answer("❌", show_alert=True)
                return
            OWNER_PENDING_ACTION.pop(user_id, None)
            uid = int(data.replace("op_rmprem_", ""))
            PREMIUM_USERS.discard(uid)
            await query.answer(f"✅ Removed {uid}")
            await query.edit_message_text(f"✅ User `{uid}` premium se hata diya.", parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))

        elif data == "op_setuserlimit":
            if not is_owner(user_id):
                await query.answer("❌", show_alert=True)
                return
            OWNER_PENDING_ACTION[user_id] = "setuserlimit_id"
            await query.answer()
            lines = ["🎯 *Set User Custom Limit*\n\nUser ID bhejo:"]
            if USER_CUSTOM_LIMITS:
                lines.append("\n*Current custom limits:*")
                for uid, lim in USER_CUSTOM_LIMITS.items():
                    lines.append(f"• `{uid}` → *{lim}*/day")
            await query.message.reply_text("\n".join(lines), parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="op_cancel")]]))

        elif data == "op_listpremium":
            if not is_owner(user_id):
                await query.answer("❌", show_alert=True)
                return
            await query.answer()
            if PREMIUM_USERS:
                lines = [f"⭐ *Premium Users ({len(PREMIUM_USERS)})*\n"]
                for uid in PREMIUM_USERS:
                    limit_info = ""
                    if uid in USER_CUSTOM_LIMITS:
                        limit_info = f" — limit: *{USER_CUSTOM_LIMITS[uid]}*/day"
                    lines.append(f"• `{uid}`{limit_info}")
                await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))
            else:
                await query.edit_message_text("📋 Koi premium user nahi hai.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))

        elif data == "op_settings":
            if not is_owner(user_id):
                await query.answer("❌", show_alert=True)
                return
            await query.answer()
            premium_count = len(PREMIUM_USERS)
            limit_text = "Unlimited" if FREE_CHECK_LIMIT == 0 else str(FREE_CHECK_LIMIT)
            custom_count = len(USER_CUSTOM_LIMITS)
            lines = [
                f"⚙️ *Bot Settings*\n",
                f"🔹 Mode: *{BOT_MODE.upper()}*",
                f"🔹 Free user limit: *{limit_text}* checks/day",
                f"🔹 Premium users: *{premium_count}*",
                f"🔹 Custom limits: *{custom_count}* users",
            ]
            if USER_CUSTOM_LIMITS:
                lines.append("\n*Custom limits:*")
                for uid, lim in USER_CUSTOM_LIMITS.items():
                    lines.append(f"• `{uid}` → *{lim}*/day")
            await query.edit_message_text("\n".join(lines), parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))

        elif data == "op_cancel":
            OWNER_PENDING_ACTION.pop(user_id, None)
            await query.answer("❌ Cancelled")
            await query.edit_message_text("❌ Action cancelled.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))

        elif data == "qrlink_btn":
            await query.answer()
            try:
                async with self.api:
                    status = await self.api.check_connection(str(user_id))
                if status.get("connected"):
                    await query.message.reply_text("✅ *Aapka WhatsApp pehle se connected hai!*", parse_mode="Markdown")
                    return
                qr_base64 = status.get("qrBase64")
                if not qr_base64:
                    await query.message.reply_text(
                        "⚠️ *QR code abhi available nahi.*\n10-15 seconds baad `/qrlink` dobara bhejo.",
                        parse_mode="Markdown"
                    )
                    return
                img_data = qr_base64.split(",")[1] if "," in qr_base64 else qr_base64
                img_bytes = base64.b64decode(img_data)
                img_file = io.BytesIO(img_bytes)
                img_file.name = "whatsapp_qr.png"
                await query.message.reply_photo(
                    photo=img_file,
                    caption=(
                        "📱 *WhatsApp QR Code*\n\n"
                        "1. WhatsApp kholo\n"
                        "2. Settings → *Linked Devices*\n"
                        "3. *Link a Device* tap karo\n"
                        "4. Yeh QR scan karo\n\n"
                        "✅ Scan ke baad bot ready ho jayega!"
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                await query.message.reply_text(f"❌ Error: `{str(e)}`", parse_mode="Markdown")

    async def wa_connection_watcher(self, context):
        """Background job - health check (no-op in multi-session mode)"""
        pass

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        global FREE_CHECK_LIMIT
        text = update.message.text
        if not text:
            return

        user_id = update.effective_user.id

        if user_id in OWNER_PENDING_ACTION:
            action = OWNER_PENDING_ACTION.pop(user_id)
            text_stripped = text.strip()

            if action == "setlimit":
                try:
                    limit = int(text_stripped)
                    if limit < 0:
                        await update.message.reply_text("❌ Limit 0 ya usse zyada hona chahiye.")
                        return
                    FREE_CHECK_LIMIT = limit
                    if limit == 0:
                        await update.message.reply_text("✅ Free users ke liye *unlimited* checks set!", parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))
                    else:
                        await update.message.reply_text(f"✅ Free users ke liye daily limit: *{limit} checks*", parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))
                except ValueError:
                    await update.message.reply_text("❌ Sirf number bhejo. Dobara try karo.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))
                return

            elif action == "addpremium":
                try:
                    uid = int(text_stripped)
                    PREMIUM_USERS.add(uid)
                    await update.message.reply_text(f"✅ User `{uid}` premium mein add ho gaya!", parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))
                except ValueError:
                    await update.message.reply_text("❌ Valid user ID bhejo.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))
                return

            elif action == "removepremium":
                try:
                    uid = int(text_stripped)
                    PREMIUM_USERS.discard(uid)
                    await update.message.reply_text(f"✅ User `{uid}` premium se hata diya.", parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))
                except ValueError:
                    await update.message.reply_text("❌ Valid user ID bhejo.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))
                return

            elif action == "setuserlimit_id":
                try:
                    uid = int(text_stripped)
                    OWNER_PENDING_ACTION[user_id] = f"setuserlimit_val_{uid}"
                    current = USER_CUSTOM_LIMITS.get(uid, None)
                    current_text = f"Current: *{current}/day*" if current else "Current: *default*"
                    await update.message.reply_text(
                        f"🎯 User `{uid}` ke liye limit bhejo:\n{current_text}\n\n`0` = custom limit hatao (default lagegi)",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="op_cancel")]]))
                except ValueError:
                    await update.message.reply_text("❌ Valid user ID bhejo.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))
                return

            elif action.startswith("setuserlimit_val_"):
                target_uid = int(action.replace("setuserlimit_val_", ""))
                try:
                    limit = int(text_stripped)
                    if limit < 0:
                        await update.message.reply_text("❌ Limit 0 ya usse zyada hona chahiye.")
                        return
                    if limit == 0:
                        USER_CUSTOM_LIMITS.pop(target_uid, None)
                        await update.message.reply_text(f"✅ User `{target_uid}` ki custom limit hata di — default lagegi.", parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))
                    else:
                        USER_CUSTOM_LIMITS[target_uid] = limit
                        await update.message.reply_text(f"✅ User `{target_uid}` ke liye daily limit: *{limit} checks*", parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))
                except ValueError:
                    await update.message.reply_text("❌ Sirf number bhejo.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("👑 Owner Panel", callback_data="owner_panel")]]))
                return

        if re.search(r"\d", text):
            allowed, remaining, msg = can_check(user_id)
            if not allowed:
                await update.message.reply_text(msg, parse_mode="Markdown")
                return

            numbers = self.extract_numbers(text)
            if len(numbers) == 0:
                await update.message.reply_text("❌ No valid phone numbers found.\nNumbers should be 10-15 digits with country code.", parse_mode="Markdown")
                return

            if len(numbers) == 1:
                await self.process_single_check(update, context, text)
            else:
                await update.message.reply_text(f"📊 Detected *{len(numbers)}* numbers. Starting bulk check...", parse_mode="Markdown")
                await self.process_bulk_with_updates(update, context, numbers)
        else:
            await update.message.reply_text("🤔 Send me a phone number, upload a .txt file, or use /help")


def main():
    print("🤖 Starting WhatsApp Number Checker Bot...")
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN is not set. Please add it as a secret in Replit.")
        return
    print(f"   Bot Token: {TELEGRAM_BOT_TOKEN[:20]}...")
    print(f"   Owner ID: {OWNER_ID}")
    print(f"   WhatsApp API: {WHATSAPP_API_URL}")

    bot = WhatsAppCheckerBot()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    bulk_handler = ConversationHandler(
        entry_points=[CommandHandler("bulk", bot.bulk_start)],
        states={
            BULK_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.bulk_process)]
        },
        fallbacks=[CommandHandler("cancel", bot.cancel_bulk)],
    )

    app.add_handler(CommandHandler("start", bot.start))
    app.add_handler(CommandHandler("help", bot.help))
    app.add_handler(CommandHandler("check", bot.check_cmd))
    app.add_handler(CommandHandler("status", bot.status_cmd))
    app.add_handler(CommandHandler("pair", bot.pair_cmd))
    app.add_handler(CommandHandler("qrlink", bot.qrlink_cmd))
    app.add_handler(CommandHandler("addpremium", bot.add_premium))
    app.add_handler(CommandHandler("removepremium", bot.remove_premium))
    app.add_handler(CommandHandler("listpremium", bot.list_premium))
    app.add_handler(CommandHandler("setmode", bot.set_mode))
    app.add_handler(CommandHandler("setlimit", bot.set_limit))
    app.add_handler(CommandHandler("setuserlimit", bot.set_user_limit))
    app.add_handler(CommandHandler("settings", bot.bot_settings))
    app.add_handler(MessageHandler(filters.Document.ALL, bot.handle_document))
    app.add_handler(bulk_handler)
    app.add_handler(CallbackQueryHandler(bot.handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))

    async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.error(f"Error: {context.error}")
        if update and update.effective_message:
            await update.effective_message.reply_text("❌ Error occurred. Try again.")

    app.add_error_handler(error_handler)

    async def post_init(application):
        from telegram import BotCommand
        await application.bot.set_my_commands([
            BotCommand("start", "🏠 Main Menu"),
            BotCommand("pair", "🔗 Pair WhatsApp"),
            BotCommand("qrlink", "📱 QR Code Link"),
        ])

    app.post_init = post_init

    app.job_queue.run_repeating(bot.wa_connection_watcher, interval=8, first=10)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
