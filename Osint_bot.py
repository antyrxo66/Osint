# osint_bot.py
# Advanced Telegram OSINT bot — GBS Systems + multi-source
# pip install python-telegram-bot requests aiohttp
import os, re, json, html, logging, asyncio, time, hashlib
from datetime import datetime, timezone
from collections import defaultdict
import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ParseMode, ChatAction

# ─── Config ───────────────────────────────────────────────────────────────────
API_BASE   = "https://admin.gbssystems.com/public/api/v1/api.php"
API_KEY    = os.environ.get("API_KEY", "8AF6N")
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "9257344160:AAFOmNtzQHaLN_l-lIF2TDHYF63Ah0QLyWY")
ADMIN_IDS  = {int(x) for x in (os.environ.get("ADMIN_IDS", "8126319504").split(",")) if x.strip()}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S"
)
LOG = logging.getLogger("osint")
TG_MAX = 3900

# Rate limit: max requests per user per minute
RATE_LIMIT = 20
RATE_WINDOW = 60
user_hits = defaultdict(list)

# Simple in-memory cache (query → (timestamp, result))
CACHE_TTL = 300  # 5 min
cache = {}

# ─── Commands registry ────────────────────────────────────────────────────────
COMMANDS = {
    "phoneinfo": ("Phone Info",     "📱", "10-digit phone",      r"^\d{10}$"),
    "number":    ("Number Lookup",  "📞", "10-digit phone",      r"^\d{10}$"),
    "hp":        ("HP / Address",   "🏠", "10-digit phone",      r"^\d{10}$"),
    "pan":       ("PAN",            "🆔", "10-char PAN",         r"^[A-Z]{5}\d{4}[A-Z]$"),
    "pan2":      ("PAN v2",         "🪪", "10-char PAN",         r"^[A-Z]{5}\d{4}[A-Z]$"),
    "family":    ("Family / Ration","👨‍👩‍👧", "12-digit Aadhaar",  r"^\d{12}$"),
    "aadhar":    ("Aadhaar",        "🆔", "12-digit Aadhaar",    r"^\d{12}$"),
    "all":       ("Full Dump",      "🎯", "10-digit phone",      r"^\d{10}$"),
    "vehicle":   ("Vehicle",        "🚗", "Vehicle number",      r"^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{4}$"),
    "ifsc":      ("IFSC Bank",      "🏦", "IFSC code",           r"^[A-Z]{4}0[A-Z0-9]{6}$"),
    "pincode":   ("Pincode",        "📍", "6-digit pincode",     r"^\d{6}$"),
    "gst":       ("GSTIN",          "🧾", "15-char GSTIN",       r"^\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]$"),
}

# ─── Helpers ──────────────────────────────────────────────────────────────────
def allowed(uid: int) -> bool:
    return (not ADMIN_IDS) or (uid in ADMIN_IDS)

def rate_ok(uid: int) -> bool:
    now = time.time()
    hits = user_hits[uid]
    user_hits[uid] = [t for t in hits if now - t < RATE_WINDOW]
    if len(user_hits[uid]) >= RATE_LIMIT:
        return False
    user_hits[uid].append(now)
    return True

def cache_get(key: str):
    item = cache.get(key)
    if not item:
        return None
    ts, data = item
    if time.time() - ts > CACHE_TTL:
        cache.pop(key, None)
        return None
    return data

def cache_set(key: str, data):
    cache[key] = (time.time(), data)

def esc(v):
    if v is None or v == "" or v == [] or v == {}:
        return "—"
    return html.escape(str(v))

def kv(title: str, pairs: list) -> str:
    out = [f"<b>{title}</b>"]
    for k, v in pairs:
        if v is None or v == "" or v == [] or v == {}:
            continue
        out.append(f"• <b>{esc(k)}:</b> {esc(v)}")
    return "\n".join(out)

def chunks(text: str, size: int = TG_MAX):
    if len(text) <= size:
        return [text]
    parts, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > size:
            if buf:
                parts.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        parts.append(buf)
    return parts

def raw_block(raw) -> str:
    try:
        s = json.dumps(raw, indent=2, ensure_ascii=False)
    except Exception:
        return ""
    if len(s) > TG_MAX - 250:
        s = s[: TG_MAX - 250] + "\n…[truncated]"
    return "<pre>" + html.escape(s) + "</pre>"

# ─── API layer ────────────────────────────────────────────────────────────────
def call_api(cmd: str, query: str, timeout: int = 35) -> dict:
    cache_key = f"{cmd}:{query}"
    cached = cache_get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    t0 = datetime.now()
    try:
        r = requests.get(
            API_BASE,
            params={"key": API_KEY, "cmd": cmd, "query": query},
            timeout=timeout,
            headers={
                "User-Agent": "OSINTBot/2.0",
                "Accept": "application/json",
                "X-Request-ID": hashlib.md5(f"{cmd}{query}{time.time()}".encode()).hexdigest()[:12],
            },
        )
        elapsed = (datetime.now() - t0).total_seconds()
        try:
            body = r.json()
        except ValueError:
            result = {
                "raw": None, "status": r.status_code,
                "text": r.text[:800], "elapsed": elapsed, "err": "non_json", "cached": False
            }
            return result
        result = {"raw": body, "status": r.status_code, "elapsed": elapsed, "err": None, "cached": False}
        if r.status_code == 200 and body:
            cache_set(cache_key, result)
        return result
    except requests.RequestException as e:
        return {
            "raw": None, "status": 0, "text": str(e),
            "elapsed": (datetime.now() - t0).total_seconds(),
            "err": "network", "cached": False
        }

# Extra free public APIs (no key needed)
def lookup_ifsc(ifsc: str) -> dict:
    try:
        r = requests.get(f"https://ifsc.razorpay.com/{ifsc}", timeout=12)
        if r.status_code == 200:
            return {"raw": r.json(), "status": 200, "elapsed": 0, "err": None}
        return {"raw": None, "status": r.status_code, "text": r.text[:300], "err": "http"}
    except Exception as e:
        return {"raw": None, "status": 0, "text": str(e), "err": "network"}

def lookup_pincode(pin: str) -> dict:
    try:
        r = requests.get(f"https://api.postalpincode.in/pincode/{pin}", timeout=12)
        if r.status_code == 200:
            return {"raw": r.json(), "status": 200, "elapsed": 0, "err": None}
        return {"raw": None, "status": r.status_code, "text": r.text[:300], "err": "http"}
    except Exception as e:
        return {"raw": None, "status": 0, "text": str(e), "err": "network"}

# ─── Pretty printers ──────────────────────────────────────────────────────────
def pretty(cmd: str, query: str, raw) -> str | None:
    if not raw:
        return None
    try:
        if cmd in ("phoneinfo", "number", "all"):
            d = raw.get("data") or raw
            loc = d.get("location_details") or {}
            return kv(f"📱 {cmd.upper()} — {query}", [
                ("Name", d.get("name") or d.get("Name")),
                ("Number", d.get("number") or d.get("phone") or query),
                ("Father / Relative", d.get("fname") or d.get("father") or d.get("relative")),
                ("Email", d.get("email")),
                ("Address", d.get("address") or d.get("Address")),
                ("Country", loc.get("country") or d.get("country")),
                ("State", loc.get("state") or d.get("state")),
                ("City", loc.get("city") or d.get("city")),
                ("Carrier", loc.get("carrier") or d.get("operator") or d.get("circle")),
                ("Circle", d.get("circle")),
            ])

        if cmd == "hp":
            d = raw.get("data") or raw
            return kv(f"🏠 HP / Address — {query}", [
                ("Name", d.get("consumerName") or d.get("name")),
                ("Phone", d.get("consumerPhone1") or d.get("phone")),
                ("Alt Phone", d.get("consumerPhone2")),
                ("DOB", d.get("dob") or d.get("dateOfBirth")),
                ("Address", d.get("address") or d.get("fullAddress")),
                ("Pin", d.get("pinCode") or d.get("pincode")),
                ("State", d.get("state")),
                ("District", d.get("district")),
            ])

        if cmd in ("pan", "pan2"):
            d = raw.get("data") or raw
            return kv(f"🆔 {cmd.upper()} — {query}", [
                ("Full Name", d.get("customerName") or d.get("name")),
                ("First", d.get("firstName")),
                ("Middle", d.get("middleName")),
                ("Last", d.get("lastName")),
                ("DOB", d.get("dob") or d.get("dateOfBirth")),
                ("Gender", d.get("gender")),
                ("Status", d.get("panStatus") or d.get("status")),
                ("Aadhaar Linked", d.get("aadhaarLinked") or d.get("isAadhaarLinked")),
            ])

        if cmd == "aadhar":
            res = raw.get("RESULT") or raw.get("data") or (raw if isinstance(raw, list) else [])
            if not isinstance(res, list):
                res = [res]
            out = [f"<b>🆔 Aadhaar — {len(res)} record(s)</b>"]
            for i, r in enumerate(res[:20], 1):
                out.append(
                    f"\n<b>[{i}] {esc(r.get('NAME') or r.get('name'))}</b>"
                    f" (s/o {esc(r.get('FNAME') or r.get('father') or r.get('relative'))})\n"
                    f"   📞 <code>{esc(r.get('NUMBER') or r.get('mobile') or r.get('phone'))}</code>"
                    f" ↪ <code>{esc(r.get('ALT') or r.get('alt'))}</code>\n"
                    f"   ✉ {esc(r.get('EMAIL') or r.get('email'))} · {esc(r.get('CIRCLE') or r.get('circle'))}\n"
                    f"   📍 {esc(r.get('ADDRESS') or r.get('address'))}"
                )
            return "\n".join(out)

        if cmd == "family":
            d = ((raw.get("details") or {}).get("pd")) or raw.get("data") or raw
            mem = d.get("memberDetailsList") or d.get("members") or []
            out = [
                f"<b>👨‍👩‍👧 Family / Ration</b>",
                f"• RC No: <code>{esc(raw.get('rationCardNumber') or d.get('rationCardNumber'))}</code>",
                f"• Address: {esc(d.get('address'))}",
                f"• District: {esc(d.get('homeDistName') or d.get('district'))}",
                f"• State: {esc(d.get('homeStateName') or d.get('state'))}",
                f"• FPS: {esc(d.get('fpsId'))}",
                f"• Scheme: {esc(d.get('schemeName'))}",
                f"\n<b>Members ({len(mem)})</b>"
            ]
            for m in mem:
                out.append(
                    f"   • {esc(m.get('memberName') or m.get('name'))}"
                    f" — {esc(m.get('releationship_name') or m.get('relation'))}"
                    f" · {esc(m.get('age') or '')}"
                )
            return "\n".join(out)

        if cmd == "ifsc":
            d = raw if isinstance(raw, dict) else {}
            return kv(f"🏦 IFSC — {query}", [
                ("Bank", d.get("BANK")),
                ("Branch", d.get("BRANCH")),
                ("Address", d.get("ADDRESS")),
                ("City", d.get("CITY")),
                ("District", d.get("DISTRICT")),
                ("State", d.get("STATE")),
                ("IFSC", d.get("IFSC")),
                ("MICR", d.get("MICR")),
                ("Contact", d.get("CONTACT")),
            ])

        if cmd == "pincode":
            arr = raw if isinstance(raw, list) else []
            if arr and isinstance(arr[0], dict):
                post = arr[0].get("PostOffice") or []
                out = [f"<b>📍 Pincode {query}</b> — {esc(arr[0].get('Message'))}"]
                for p in post[:12]:
                    out.append(
                        f"\n• <b>{esc(p.get('Name'))}</b> ({esc(p.get('BranchType'))})\n"
                        f"  {esc(p.get('District'))}, {esc(p.get('State'))} · {esc(p.get('Circle'))}"
                    )
                return "\n".join(out)
            return None

        if cmd == "vehicle":
            d = raw.get("data") or raw
            return kv(f"🚗 Vehicle — {query}", [
                ("Owner", d.get("owner") or d.get("ownerName")),
                ("Reg No", d.get("regNo") or query),
                ("Model", d.get("model") or d.get("vehicleModel")),
                ("Make", d.get("make") or d.get("manufacturer")),
                ("Class", d.get("class") or d.get("vehicleClass")),
                ("Fuel", d.get("fuel") or d.get("fuelType")),
                ("Reg Date", d.get("regDate") or d.get("registrationDate")),
                ("Fitness", d.get("fitnessUpto")),
                ("Insurance", d.get("insuranceUpto")),
                ("Chassis", d.get("chassis")),
                ("Engine", d.get("engine")),
            ])

        if cmd == "gst":
            d = raw.get("data") or raw
            return kv(f"🧾 GSTIN — {query}", [
                ("Legal Name", d.get("legalName") or d.get("lgnm")),
                ("Trade Name", d.get("tradeName") or d.get("tradeNam")),
                ("Status", d.get("status") or d.get("sts")),
                ("Reg Date", d.get("regDate") or d.get("rgdt")),
                ("State", d.get("state") or d.get("stj")),
                ("Address", d.get("address") or d.get("pradr")),
                ("Constitution", d.get("constitution") or d.get("ctb")),
            ])

    except Exception as e:
        LOG.warning("pretty error %s: %s", cmd, e)
    return None

# ─── Handlers ─────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        await update.message.reply_text("⛔ Not authorized.")
        return

    kb = []
    row = []
    for cmd, (label, emoji, _, _) in COMMANDS.items():
        row.append(InlineKeyboardButton(f"{emoji} {label}", callback_data=f"cmd:{cmd}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    text = (
        "<b>🕵️ OSINT Bot v2</b>\n"
        f"User: <code>{uid}</code>\n\n"
        "Button se choose karo ya direct:\n"
        "<code>/cmd &lt;name&gt; &lt;query&gt;</code>\n\n"
        "Examples:\n"
        "• <code>/cmd pan CFXPV5799D</code>\n"
        "• <code>/cmd phoneinfo 9876543210</code>\n"
        "• <code>/cmd ifsc SBIN0005943</code>\n"
        "• <code>/cmd pincode 110001</code>\n\n"
        "Raw JSON har result ke saath aata hai.\n"
        "Cache 5 min · Rate limit 20/min"
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id):
        return
    lines = ["<b>Available commands</b>\n"]
    for cmd, (label, emoji, hint, _) in COMMANDS.items():
        lines.append(f"{emoji} <code>{cmd}</code> — {label}\n   needs: <i>{hint}</i>")
    lines.append("\n<code>/cmd name query</code> se bhi chalega.")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not allowed(uid):
        return
    await update.message.reply_text(
        f"<b>Stats</b>\n"
        f"• Cache entries: {len(cache)}\n"
        f"• Your hits (last min): {len(user_hits.get(uid, []))}\n"
        f"• Rate limit: {RATE_LIMIT}/{RATE_WINDOW}s\n"
        f"• Admins: {len(ADMIN_IDS)}",
        parse_mode=ParseMode.HTML
    )

async def clear_cache(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    n = len(cache)
    cache.clear()
    await update.message.reply_text(f"🧹 Cache cleared ({n} entries).")

async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(q.from_user.id):
        await q.edit_message_text("⛔ Not authorized.")
        return
    data = q.data or ""
    if not data.startswith("cmd:"):
        return
    cmd = data.split(":", 1)[1]
    if cmd not in COMMANDS:
        await q.edit_message_text("Unknown command.")
        return
    label, emoji, hint, _ = COMMANDS[cmd]
    ctx.user_data["pending"] = cmd
    await q.edit_message_text(
        f"{emoji} <b>{label}</b>\n\nSend me the <b>{hint}</b>.",
        parse_mode=ParseMode.HTML
    )

async def cmd_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    if len(ctx.args or []) < 2:
        await update.message.reply_text("Usage: <code>/cmd &lt;name&gt; &lt;query&gt;</code>", parse_mode=ParseMode.HTML)
        return
    name = ctx.args[0].lower()
    if name not in COMMANDS:
        await update.message.reply_text(f"Unknown: <code>{html.escape(name)}</code>\n/help dekho.", parse_mode=ParseMode.HTML)
        return
    query = " ".join(ctx.args[1:]).strip()
    await _run(update, ctx, name, query)

async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Not authorized.")
        return
    cmd = ctx.user_data.pop("pending", None)
    if not cmd:
        await update.message.reply_text("Type /start or /help")
        return
    await _run(update, ctx, cmd, update.message.text.strip())

async def _run(update: Update, ctx: ContextTypes.DEFAULT_TYPE, cmd: str, query: str):
    uid = update.effective_user.id
    if not rate_ok(uid):
        await update.message.reply_text(
            f"⏳ Rate limit hit ({RATE_LIMIT}/{RATE_WINDOW}s). Thoda wait kar."
        )
        return

    _, emoji, hint, rx = COMMANDS[cmd]

    # Normalize
    if cmd.startswith("pan") or cmd in ("ifsc", "vehicle", "gst"):
        norm = re.sub(r"\s+", "", query).upper()
    elif cmd in ("phoneinfo", "number", "hp", "all", "aadhar", "family", "pincode"):
        norm = re.sub(r"\D", "", query)
    else:
        norm = query.strip()

    if not re.match(rx, norm):
        await update.message.reply_text(
            f"❌ Invalid {hint}:\n<code>{html.escape(query)}</code>",
            parse_mode=ParseMode.HTML
        )
        return

    status_msg = await update.message.reply_text(
        f"🔎 {emoji} querying <code>{html.escape(norm)}</code>…",
        parse_mode=ParseMode.HTML
    )
    await ctx.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    # Route special public APIs
    if cmd == "ifsc":
        resp = await asyncio.to_thread(lookup_ifsc, norm)
    elif cmd == "pincode":
        resp = await asyncio.to_thread(lookup_pincode, norm)
    else:
        resp = await asyncio.to_thread(call_api, cmd, norm)

    raw = resp.get("raw")
    err = resp.get("err")
    elapsed = resp.get("elapsed", 0)
    cached = resp.get("cached", False)

    if err or raw is None:
        await status_msg.edit_text(
            f"❌ <b>Failed</b>\n"
            f"cmd: <code>{cmd}</code> · query: <code>{html.escape(norm)}</code>\n"
            f"reason: <code>{esc(err or 'empty')}</code>\n"
            f"<i>{esc(resp.get('text', ''))}</i>",
            parse_mode=ParseMode.HTML
        )
        return

    head = pretty(cmd, norm, raw) or f"<b>{emoji} {cmd} — {norm}</b>"
    cache_tag = " · cached" if cached else ""
    footer = f"\n\n<i>⏱ {elapsed:.1f}s · HTTP {resp.get('status', '?')}{cache_tag}</i>"
    full = head + "\n\n<b>Raw JSON</b>\n" + raw_block(raw) + footer

    parts = chunks(full)
    for i, part in enumerate(parts):
        if i == 0:
            await status_msg.edit_text(
                part, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
        else:
            await update.message.reply_text(
                part, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )

# ─── Main ─────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    cmds = [
        BotCommand("start", "Menu + buttons"),
        BotCommand("help", "All commands"),
        BotCommand("cmd", "Direct: /cmd name query"),
        BotCommand("stats", "Cache + rate stats"),
        BotCommand("clearcache", "Admin: clear cache"),
    ]
    await app.bot.set_my_commands(cmds)
    LOG.info("Commands registered")

def main():
    if not BOT_TOKEN or "PUT_YOUR" in BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN env")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("cmd", cmd_handler))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("clearcache", clear_cache))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    LOG.info("OSINT Bot v2 up — %d cmds | admins=%s", len(COMMANDS), ADMIN_IDS)
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
