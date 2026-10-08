import asyncio
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from aiohttp import web
from openai import AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
AI_MODEL = os.getenv("AI_MODEL", "gpt-5-mini").strip()
DB_PATH = os.getenv("DB_PATH", "data/brozen_ai.db")
BRAND = os.getenv("BRAND_NAME", "brozen AI")
SUPPORT = os.getenv("SUPPORT_USERNAME", "IBrOzen").strip().lstrip("@")
CURRENCY = os.getenv("CURRENCY", "تومان")
WELCOME = os.getenv(
    "WELCOME_TEXT",
    "به brozen AI خوش اومدی ✨\nاینجا می‌تونی از هوش مصنوعی استفاده کنی، پلن بخری و حساب خودت رو مدیریت کنی."
).replace("\\n", "\n")

try:
    ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
except ValueError:
    ADMIN_IDS = set()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY is required")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_IDS must contain at least one numeric Telegram ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("brozen-ai")
ai = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------- DB ----------
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA foreign_keys=ON")

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def db_exec(sql, params=()):
    cur = db.execute(sql, params)
    db.commit()
    return cur

def db_one(sql, params=()):
    return db.execute(sql, params).fetchone()

def db_all(sql, params=()):
    return db.execute(sql, params).fetchall()

def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        created_at TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        is_banned INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS plans(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        price INTEGER NOT NULL,
        duration_days INTEGER NOT NULL,
        credits INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plan_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        coupon_code TEXT,
        discount INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'awaiting_receipt',
        receipt_file_id TEXT,
        receipt_type TEXT,
        created_at TEXT NOT NULL,
        reviewed_at TEXT,
        reviewer_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS subscriptions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        plan_id INTEGER NOT NULL,
        starts_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        credits_total INTEGER NOT NULL,
        credits_used INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS coupons(
        code TEXT PRIMARY KEY,
        percent INTEGER NOT NULL,
        max_uses INTEGER NOT NULL DEFAULT 0,
        uses INTEGER NOT NULL DEFAULT 0,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS chat_history(
        user_id INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)
    defaults = {
        "card_number": "شماره کارت را از پنل ادمین تنظیم کنید",
        "card_name": "نام صاحب کارت را از پنل ادمین تنظیم کنید",
        "welcome": WELCOME,
        "support": SUPPORT,
        "maintenance": "0",
        "system_prompt": "تو دستیار هوش مصنوعی brozen AI هستی. پاسخ دقیق، مفید و محترمانه بده.",
    }
    for k, v in defaults.items():
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    # Seed only if no plan exists.
    if not db_one("SELECT id FROM plans LIMIT 1"):
        db.execute(
            "INSERT INTO plans(name,description,price,duration_days,credits,enabled,created_at) VALUES(?,?,?,?,?,?,?)",
            ("Starter", "پلن نمونه؛ از پنل ادمین قابل تغییر است.", 100000, 30, 100, 1, now_iso())
        )
    db.commit()

init_db()

def setting(key):
    row = db_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else ""

def set_setting(key, value):
    db_exec("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

def upsert_user(u):
    db_exec("""
    INSERT INTO users(id,username,first_name,created_at,last_seen)
    VALUES(?,?,?,?,?)
    ON CONFLICT(id) DO UPDATE SET username=excluded.username,
    first_name=excluded.first_name,last_seen=excluded.last_seen
    """, (u.id, u.username or "", u.first_name or "", now_iso(), now_iso()))

def is_admin(uid): return uid in ADMIN_IDS
def money(n): return f"{int(n):,}".replace(",", "٬")
def user_blocked(uid):
    r = db_one("SELECT is_banned FROM users WHERE id=?", (uid,))
    return bool(r and r["is_banned"])

def active_sub(uid):
    r = db_one("""
    SELECT s.*, p.name plan_name FROM subscriptions s
    JOIN plans p ON p.id=s.plan_id
    WHERE s.user_id=? AND s.active=1 AND s.expires_at>? AND s.credits_used<s.credits_total
    ORDER BY s.expires_at DESC LIMIT 1
    """, (uid, now_iso()))
    return r

def normalize_code(s): return re.sub(r"[^A-Za-z0-9_-]", "", s.strip()).upper()

# ---------- UI ----------
def main_kb(uid):
    rows = [
        [InlineKeyboardButton("✨ شروع چت با AI", callback_data="chat")],
        [InlineKeyboardButton("💎 خرید پلن", callback_data="plans"),
         InlineKeyboardButton("👤 پروفایل", callback_data="profile")],
        [InlineKeyboardButton("🧾 سفارش‌های من", callback_data="orders"),
         InlineKeyboardButton("🎟 کد تخفیف", callback_data="coupon")],
        [InlineKeyboardButton("🆘 پشتیبانی", callback_data="support"),
         InlineKeyboardButton("ℹ️ راهنما", callback_data="help")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🛠 پنل مدیریت", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ برگشت", callback_data="home")]])

def admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار", callback_data="adm_stats"),
         InlineKeyboardButton("👥 کاربران", callback_data="adm_users")],
        [InlineKeyboardButton("💎 مدیریت پلن‌ها", callback_data="adm_plans"),
         InlineKeyboardButton("🧾 سفارش‌های در انتظار", callback_data="adm_orders")],
        [InlineKeyboardButton("🎟 کدهای تخفیف", callback_data="adm_coupons"),
         InlineKeyboardButton("💳 تنظیمات پرداخت", callback_data="adm_payment")],
        [InlineKeyboardButton("⚙️ تنظیمات ربات", callback_data="adm_settings"),
         InlineKeyboardButton("📣 پیام خوش‌آمد", callback_data="adm_welcome")],
        [InlineKeyboardButton("⬅️ منوی اصلی", callback_data="home")],
    ])

# ---------- Common handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user: return
    upsert_user(update.effective_user)
    if user_blocked(update.effective_user.id):
        await update.message.reply_text("⛔ دسترسی این حساب محدود شده است.")
        return
    text = setting("welcome") or WELCOME
    await update.message.reply_text(
        f"🤖 <b>{BRAND}</b>\n\n{text}",
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb(update.effective_user.id)
    )

async def plans_text():
    plans = db_all("SELECT * FROM plans WHERE enabled=1 ORDER BY price")
    if not plans:
        return "فعلاً هیچ پلنی فعال نیست.", []
    buttons = []
    lines = ["💎 <b>پلن‌های فعال</b>\n"]
    for p in plans:
        lines.append(
            f"🔹 <b>{p['name']}</b>\n"
            f"💰 {money(p['price'])} {CURRENCY} | ⏳ {p['duration_days']} روز | ⚡ {p['credits']} اعتبار\n"
            f"📝 {p['description']}\n"
        )
        buttons.append([InlineKeyboardButton(f"خرید {p['name']} — {money(p['price'])}", callback_data=f"buy:{p['id']}")])
    return "\n".join(lines), buttons

async def show_plans(q):
    text, buttons = await plans_text()
    buttons.append([InlineKeyboardButton("⬅️ برگشت", callback_data="home")])
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))

async def profile_text(uid):
    u = db_one("SELECT * FROM users WHERE id=?", (uid,))
    s = db_one("""
      SELECT s.*,p.name FROM subscriptions s JOIN plans p ON p.id=s.plan_id
      WHERE s.user_id=? ORDER BY s.active DESC,s.expires_at DESC LIMIT 1
    """, (uid,))
    orders = db_one("SELECT COUNT(*) c FROM orders WHERE user_id=?", (uid,))["c"]
    if s and s["active"] and s["expires_at"] > now_iso():
        remain = max(0, s["credits_total"]-s["credits_used"])
        sub = f"💎 {s['name']}\n⏳ پایان: {s['expires_at'][:19].replace('T',' ')} UTC\n⚡ اعتبار باقی‌مانده: {remain}/{s['credits_total']}"
    else:
        sub = "❌ اشتراک فعال نداری"
    return f"""👤 <b>پروفایل {BRAND}</b>

🆔 ID: <code>{uid}</code>
📛 نام: {u['first_name'] if u else '-'}
🔗 @{u['username'] if u and u['username'] else 'ندارد'}

{sub}
🧾 تعداد سفارش: {orders}"""

async def profile(q):
    await q.edit_message_text(await profile_text(q.from_user.id), parse_mode=ParseMode.HTML, reply_markup=back_kb())

async def help_text():
    return """ℹ️ <b>راهنمای brozen AI</b>

✨ برای استفاده از AI روی «شروع چت با AI» بزن.
💎 برای خرید، یک پلن را انتخاب کن.
🧾 بعد از پرداخت، رسید را همین‌جا بفرست تا برای ادمین ارسال شود.
✅ پس از تأیید ادمین، اشتراک فعال می‌شود.
🎟 کد تخفیف را قبل از ایجاد سفارش وارد کن.

دستورات:
/start — منوی اصلی
/plans — پلن‌ها
/profile — پروفایل
/cancel — لغو عملیات جاری"""

# ---------- Purchase flow ----------
async def create_order(q, context, plan_id):
    p = db_one("SELECT * FROM plans WHERE id=? AND enabled=1", (plan_id,))
    if not p:
        await q.answer("این پلن در دسترس نیست.", show_alert=True); return
    # One pending order at a time.
    old = db_one("SELECT id FROM orders WHERE user_id=? AND status='awaiting_receipt' ORDER BY id DESC LIMIT 1", (q.from_user.id,))
    if old:
        await q.answer("یک سفارش در انتظار رسید داری.", show_alert=True)
        return
    discount = int(context.user_data.get("discount", 0) or 0)
    code = context.user_data.get("coupon_code")
    amount = max(0, p["price"] - (p["price"]*discount//100))
    oid = db_exec(
        """INSERT INTO orders(user_id,plan_id,amount,coupon_code,discount,status,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (q.from_user.id, plan_id, amount, code, discount, "awaiting_receipt", now_iso())
    ).lastrowid
    context_dummy = None
    card = setting("card_number")
    name = setting("card_name")
    await q.edit_message_text(
        f"""🧾 <b>سفارش #{oid}</b>

💎 پلن: <b>{p['name']}</b>
💰 مبلغ نهایی: <b>{money(amount)} {CURRENCY}</b>
{f"🎟 تخفیف: {discount}%" if discount else ""}

💳 <b>اطلاعات پرداخت</b>
شماره کارت:
<code>{card}</code>
به نام: <b>{name}</b>

📸 بعد از واریز، <b>عکس یا فایل رسید</b> را در همین چت ارسال کن.
⏳ سفارش بعد از بررسی ادمین فعال می‌شود.""",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو سفارش", callback_data=f"cancelorder:{oid}")]])
    )

async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if user_blocked(uid): return
    row = db_one("""SELECT o.*,p.name plan_name FROM orders o JOIN plans p ON p.id=o.plan_id
                    WHERE o.user_id=? AND o.status='awaiting_receipt' ORDER BY o.id DESC LIMIT 1""", (uid,))
    if not row:
        await update.message.reply_text("سفارش فعالی برای دریافت رسید ندارید.")
        return
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        rtype = "photo"
    elif update.message.document:
        file_id = update.message.document.file_id
        rtype = "document"
    else:
        return
    db_exec("UPDATE orders SET receipt_file_id=?,receipt_type=?,status='pending_review' WHERE id=?", (file_id,rtype,row["id"]))
    await update.message.reply_text("✅ رسید دریافت شد. برای ادمین ارسال شد؛ بعد از تأیید اشتراک فعال می‌شود.")
    caption = (
        f"🧾 <b>رسید جدید</b>\n\n"
        f"Order: <code>#{row['id']}</code>\n"
        f"User: <code>{uid}</code> | @{update.effective_user.username or '-'}\n"
        f"Plan: <b>{row['plan_name']}</b>\n"
        f"Amount: <b>{money(row['amount'])} {CURRENCY}</b>"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأیید", callback_data=f"approve:{row['id']}"),
        InlineKeyboardButton("❌ رد", callback_data=f"reject:{row['id']}")
    ]])
    for aid in ADMIN_IDS:
        try:
            if rtype == "photo":
                await context.bot.send_photo(aid, file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await context.bot.send_document(aid, file_id, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            log.exception("failed to notify admin %s", aid)

# ---------- AI ----------
async def chat_start(q, context):
    sub = active_sub(q.from_user.id)
    if not sub:
        await q.answer("برای چت با AI اول یک پلن فعال تهیه کن.", show_alert=True)
        return
    context.user_data["chat_mode"] = True
    await q.edit_message_text(
        f"✨ <b>حالت AI فعال شد</b>\n\nپیامت رو بفرست. برای خروج /cancel رو بزن.\n⚡ اعتبار باقی‌مانده: {sub['credits_total']-sub['credits_used']}",
        parse_mode=ParseMode.HTML,
        reply_markup=back_kb()
    )

async def ai_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("chat_mode"): return
    uid = update.effective_user.id
    if user_blocked(uid): return
    sub = active_sub(uid)
    if not sub:
        context.user_data["chat_mode"] = False
        await update.message.reply_text("اشتراکت تمام شده؛ یک پلن جدید تهیه کن.", reply_markup=main_kb(uid))
        return
    remaining = sub["credits_total"] - sub["credits_used"]
    if remaining <= 0:
        await update.message.reply_text("⚠️ اعتبار این پلن تمام شده است.")
        return
    prompt = update.message.text.strip()
    if not prompt: return
    db_exec("INSERT INTO chat_history(user_id,role,content,created_at) VALUES(?,?,?,?)", (uid,"user",prompt,now_iso()))
    hist = db_all("SELECT role,content FROM chat_history WHERE user_id=? ORDER BY rowid DESC LIMIT 12", (uid,))
    messages = [{"role":"system","content":setting("system_prompt")}] + [
        {"role": r["role"], "content": r["content"]} for r in reversed(hist)
    ]
    try:
        await update.message.chat.send_action("typing")
        resp = await ai.chat.completions.create(model=AI_MODEL, messages=messages)
        answer = resp.choices[0].message.content or "پاسخی دریافت نشد."
    except Exception:
        log.exception("AI error")
        await update.message.reply_text("⚠️ فعلاً در پاسخ‌گویی مشکلی پیش آمد. کمی بعد دوباره امتحان کن.")
        return
    db_exec("INSERT INTO chat_history(user_id,role,content,created_at) VALUES(?,?,?,?)", (uid,"assistant",answer,now_iso()))
    db_exec("UPDATE subscriptions SET credits_used=credits_used+1 WHERE id=?", (sub["id"],))
    await update.message.reply_text(f"{answer}\n\n⚡ اعتبار باقی‌مانده: {remaining-1}")

# ---------- Admin ----------
async def admin_panel(q):
    if not is_admin(q.from_user.id): return
    await q.edit_message_text("🛠 <b>پنل مدیریت brozen AI</b>\n\nهمه تنظیمات اصلی از همین‌جا قابل مدیریت است.", parse_mode=ParseMode.HTML, reply_markup=admin_kb())

async def admin_stats(q):
    users = db_one("SELECT COUNT(*) c FROM users")["c"]
    active = db_one("SELECT COUNT(*) c FROM subscriptions WHERE active=1 AND expires_at>?", (now_iso(),))["c"]
    pending = db_one("SELECT COUNT(*) c FROM orders WHERE status='pending_review'")["c"]
    revenue = db_one("SELECT COALESCE(SUM(amount),0) s FROM orders WHERE status='approved'")["s"]
    await q.edit_message_text(f"📊 <b>آمار</b>\n\n👥 کاربران: {users}\n💎 اشتراک فعال: {active}\n🧾 رسیدهای در انتظار: {pending}\n💰 فروش تأییدشده: {money(revenue)} {CURRENCY}", parse_mode=ParseMode.HTML, reply_markup=back_admin())

def back_admin(): return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ پنل مدیریت", callback_data="admin")]])

async def admin_orders(q):
    rows = db_all("""SELECT o.id,o.user_id,o.amount,p.name,o.created_at FROM orders o JOIN plans p ON p.id=o.plan_id
                     WHERE o.status='pending_review' ORDER BY o.id DESC LIMIT 15""")
    if not rows:
        text="🧾 سفارشی در انتظار بررسی نیست."
        kb=back_admin()
    else:
        text="🧾 <b>سفارش‌های در انتظار</b>\n\n"
        btn=[]
        for r in rows:
            text += f"#{r['id']} — {r['name']} — {money(r['amount'])} — <code>{r['user_id']}</code>\n"
            btn.append([InlineKeyboardButton(f"بررسی #{r['id']}", callback_data=f"review:{r['id']}")])
        btn.append([InlineKeyboardButton("⬅️ پنل مدیریت", callback_data="admin")])
        kb=InlineKeyboardMarkup(btn)
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

async def review_order(q, oid):
    if not is_admin(q.from_user.id): return
    o=db_one("""SELECT o.*,p.name,p.duration_days,p.credits,u.username FROM orders o
                JOIN plans p ON p.id=o.plan_id JOIN users u ON u.id=o.user_id WHERE o.id=?""",(oid,))
    if not o: await q.answer("سفارش پیدا نشد.",show_alert=True); return
    text=f"""🧾 <b>بررسی سفارش #{oid}</b>

👤 User: <code>{o['user_id']}</code> @{o['username'] or '-'}
💎 Plan: {o['name']}
💰 Amount: {money(o['amount'])} {CURRENCY}
📅 ثبت: {o['created_at']}"""
    await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأیید و فعال‌سازی",callback_data=f"approve:{oid}"),
        InlineKeyboardButton("❌ رد",callback_data=f"reject:{oid}")
    ],[InlineKeyboardButton("⬅️ سفارش‌ها",callback_data="adm_orders")]]))

async def approve_order(q, oid):
    if not is_admin(q.from_user.id): return
    o=db_one("""SELECT o.*,p.duration_days,p.credits,p.name FROM orders o JOIN plans p ON p.id=o.plan_id WHERE o.id=?""",(oid,))
    if not o or o["status"] not in ("pending_review","awaiting_receipt"):
        await q.answer("این سفارش قبلاً بررسی شده یا معتبر نیست.",show_alert=True); return
    start=datetime.now(timezone.utc)
    end=start+timedelta(days=o["duration_days"])
    db_exec("UPDATE orders SET status='approved',reviewed_at=?,reviewer_id=? WHERE id=?", (now_iso(),q.from_user.id,oid))
    db_exec("UPDATE subscriptions SET active=0 WHERE user_id=? AND active=1", (o["user_id"],))
    db_exec("""INSERT INTO subscriptions(user_id,plan_id,starts_at,expires_at,credits_total,credits_used,active)
               VALUES(?,?,?,?,?,?,1)""",(o["user_id"],o["plan_id"],start.isoformat(),end.isoformat(),o["credits"],0))
    await q.answer("تأیید شد.")
    try:
        await q.bot.send_message(o["user_id"], f"🎉 پرداخت سفارش #{oid} تأیید شد!\n💎 پلن <b>{o['name']}</b> فعال شد.", parse_mode=ParseMode.HTML)
    except Exception: pass
    await admin_orders(q)

async def reject_order(q, oid):
    if not is_admin(q.from_user.id): return
    o=db_one("SELECT * FROM orders WHERE id=?",(oid,))
    if not o or o["status"] not in ("pending_review","awaiting_receipt"):
        await q.answer("این سفارش قبلاً بررسی شده.",show_alert=True); return
    db_exec("UPDATE orders SET status='rejected',reviewed_at=?,reviewer_id=? WHERE id=?", (now_iso(),q.from_user.id,oid))
    await q.answer("رد شد.")
    try: await q.bot.send_message(o["user_id"], f"❌ رسید سفارش #{oid} تأیید نشد.\nاگر اشتباهی رخ داده با پشتیبانی تماس بگیر.")
    except Exception: pass
    await admin_orders(q)

# ---------- Conversation-based admin editor ----------
ADMIN_INPUT = 10
async def admin_command(update, context):
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("🛠 پنل مدیریت", reply_markup=admin_kb())

async def add_plan_start(update, context):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    context.user_data["new_plan"]={}
    await update.message.reply_text("نام پلن را بفرست:")
    return ADMIN_INPUT

async def admin_text_input(update, context):
    if not is_admin(update.effective_user.id): return ConversationHandler.END
    data=context.user_data.get("new_plan")
    if data is not None:
        step=len(data)
        prompts=["توضیحات پلن:","قیمت به تومان (فقط عدد):","مدت به روز:","تعداد اعتبار:"]
        if step==0: data["name"]=update.message.text
        elif step==1: data["description"]=update.message.text
        elif step==2:
            if not update.message.text.isdigit(): await update.message.reply_text("فقط عدد بفرست."); return ADMIN_INPUT
            data["price"]=int(update.message.text)
        elif step==3:
            if not update.message.text.isdigit(): await update.message.reply_text("فقط عدد بفرست."); return ADMIN_INPUT
            data["duration_days"]=int(update.message.text)
        elif step==4:
            if not update.message.text.isdigit(): await update.message.reply_text("فقط عدد بفرست."); return ADMIN_INPUT
            data["credits"]=int(update.message.text)
            db_exec("""INSERT INTO plans(name,description,price,duration_days,credits,enabled,created_at)
                       VALUES(?,?,?,?,?,?,?)""",(data["name"],data["description"],data["price"],data["duration_days"],data["credits"],1,now_iso()))
            context.user_data.pop("new_plan",None)
            await update.message.reply_text("✅ پلن ساخته شد.",reply_markup=admin_kb())
            return ConversationHandler.END
        await update.message.reply_text(prompts[step] if step < 4 else "تعداد اعتبار:")
        return ADMIN_INPUT
    return ConversationHandler.END

# ---------- Callbacks ----------
async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query
    await q.answer()
    uid=q.from_user.id
    upsert_user(q.from_user)
    data=q.data
    if data=="home":
        await q.edit_message_text(f"🤖 <b>{BRAND}</b>\n\n{setting('welcome')}",parse_mode=ParseMode.HTML,reply_markup=main_kb(uid))
    elif data=="plans": await show_plans(q)
    elif data=="profile": await profile(q)
    elif data=="help": await q.edit_message_text(await help_text(),parse_mode=ParseMode.HTML,reply_markup=back_kb())
    elif data=="support":
        await q.edit_message_text(f"🆘 پشتیبانی:\n@{setting('support') or SUPPORT}",reply_markup=back_kb())
    elif data=="chat": await chat_start(q,context)
    elif data.startswith("buy:"): await create_order(q,context,int(data.split(":")[1]))
    elif data.startswith("cancelorder:"):
        oid=int(data.split(":")[1])
        db_exec("UPDATE orders SET status='cancelled' WHERE id=? AND user_id=? AND status='awaiting_receipt'",(oid,uid))
        await q.edit_message_text("❌ سفارش لغو شد.",reply_markup=main_kb(uid))
    elif data=="orders":
        rows=db_all("SELECT o.id,p.name,o.amount,o.status,o.created_at FROM orders o JOIN plans p ON p.id=o.plan_id WHERE o.user_id=? ORDER BY o.id DESC LIMIT 10",(uid,))
        text="🧾 <b>سفارش‌های من</b>\n\n"
        for r in rows:
            text+=f"#{r['id']} | {r['name']} | {money(r['amount'])} | {r['status']}\n"
        await q.edit_message_text(text if rows else "🧾 هنوز سفارشی نداری.",parse_mode=ParseMode.HTML,reply_markup=back_kb())
    elif data=="coupon":
        await q.edit_message_text("🎟 برای استفاده از کد تخفیف، کد را به شکل /coupon CODE ارسال کن.",reply_markup=back_kb())
    elif data=="admin": await admin_panel(q)
    elif data=="adm_stats": await admin_stats(q)
    elif data=="adm_orders": await admin_orders(q)
    elif data.startswith("review:"): await review_order(q,int(data.split(":")[1]))
    elif data.startswith("approve:"): await approve_order(q,int(data.split(":")[1]))
    elif data.startswith("reject:"): await reject_order(q,int(data.split(":")[1]))
    elif data=="adm_plans":
        rows=db_all("SELECT * FROM plans ORDER BY id DESC")
        text="💎 <b>مدیریت پلن‌ها</b>\n\n"+("\n".join([f"#{p['id']} {p['name']} | {money(p['price'])} | {p['duration_days']}d | {p['credits']}c | {'فعال' if p['enabled'] else 'خاموش'}" for p in rows]) or "هیچ پلنی نیست.")
        await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ ساخت پلن",callback_data="newplan_help")],
            *[[InlineKeyboardButton(f"🔄 {p['name']}",callback_data=f"toggleplan:{p['id']}")] for p in rows],
            [InlineKeyboardButton("⬅️ پنل",callback_data="admin")]
        ]))
    elif data=="newplan_help":
        await q.edit_message_text("برای ساخت پلن از دستور /newplan استفاده کن.\nبه‌ترتیب نام، توضیحات، قیمت، مدت و اعتبار را می‌پرسد.",reply_markup=back_admin())
    elif data.startswith("toggleplan:"):
        pid=int(data.split(":")[1])
        db_exec("UPDATE plans SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(pid,))
        await q.edit_message_text("✅ وضعیت پلن تغییر کرد.",reply_markup=back_admin())
    elif data=="adm_payment":
        await q.edit_message_text(f"💳 <b>تنظیمات پرداخت</b>\n\nشماره کارت: <code>{setting('card_number')}</code>\nنام: {setting('card_name')}\n\nبرای تغییر:\n/setcard شماره|نام",parse_mode=ParseMode.HTML,reply_markup=back_admin())
    elif data=="adm_welcome":
        await q.edit_message_text(f"📣 متن فعلی:\n\n{setting('welcome')}\n\nبرای تغییر:\n/setwelcome متن جدید",reply_markup=back_admin())
    elif data=="adm_settings":
        await q.edit_message_text(f"⚙️ مدل AI: <code>{AI_MODEL}</code>\nپشتیبانی: @{setting('support')}\nنگهداری: {setting('maintenance')}\n\n/setmodel مدل\n/setsupport یوزرنیم\n/maintenance on|off",parse_mode=ParseMode.HTML,reply_markup=back_admin())
    elif data=="adm_coupons":
        rows=db_all("SELECT * FROM coupons ORDER BY created_at DESC LIMIT 20")
        text="🎟 <b>کدها</b>\n\n"+("\n".join([f"{r['code']} — {r['percent']}% — {r['uses']}/{r['max_uses'] or '∞'}" for r in rows]) or "کدی ثبت نشده.")+"\n\nساخت: /addcoupon CODE PERCENT [MAX_USES]"
        await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=back_admin())
    elif data=="adm_users":
        rows=db_all("SELECT id,username,first_name,last_seen,is_banned FROM users ORDER BY last_seen DESC LIMIT 20")
        text="👥 <b>آخرین کاربران</b>\n\n"+("\n".join([f"<code>{r['id']}</code> @{r['username'] or '-'} {'⛔' if r['is_banned'] else ''}" for r in rows]) or "کاربری نیست.")
        await q.edit_message_text(text,parse_mode=ParseMode.HTML,reply_markup=back_admin())

# ---------- Admin commands ----------
async def coupon_cmd(update, context):
    if not context.args: await update.message.reply_text("فرمت: /coupon CODE"); return
    code=normalize_code(context.args[0])
    c=db_one("SELECT * FROM coupons WHERE code=? AND enabled=1",(code,))
    if not c or (c["max_uses"] and c["uses"]>=c["max_uses"]):
        await update.message.reply_text("❌ کد نامعتبر یا تمام‌شده است."); return
    context.user_data["coupon_code"]=code
    context.user_data["discount"]=c["percent"]
    await update.message.reply_text(f"✅ کد {code} فعال شد: {c['percent']}٪ تخفیف.\nحالا /plans را بزن و پلن را انتخاب کن.")

async def addcoupon(update, context):
    if not is_admin(update.effective_user.id): return
    if len(context.args)<2:
        await update.message.reply_text("فرمت: /addcoupon CODE PERCENT [MAX_USES]"); return
    code=normalize_code(context.args[0])
    try: pct=int(context.args[1]); maxu=int(context.args[2]) if len(context.args)>2 else 0
    except: await update.message.reply_text("اعداد نامعتبر."); return
    if not 1<=pct<=100: await update.message.reply_text("درصد باید 1 تا 100 باشد."); return
    db_exec("INSERT OR REPLACE INTO coupons(code,percent,max_uses,uses,enabled,created_at) VALUES(?,?,?,?,?,?)",(code,pct,maxu,0,1,now_iso()))
    await update.message.reply_text(f"✅ کد {code} با {pct}% ساخته شد.")

async def setcard(update, context):
    if not is_admin(update.effective_user.id): return
    raw=" ".join(context.args)
    if "|" not in raw: await update.message.reply_text("فرمت: /setcard شماره کارت|نام صاحب کارت"); return
    card,name=[x.strip() for x in raw.split("|",1)]
    set_setting("card_number",card); set_setting("card_name",name)
    await update.message.reply_text("✅ اطلاعات پرداخت ذخیره شد.")

async def setwelcome(update, context):
    if not is_admin(update.effective_user.id): return
    txt=update.message.text.partition(" ")[2].strip()
    if not txt: await update.message.reply_text("فرمت: /setwelcome متن"); return
    set_setting("welcome",txt); await update.message.reply_text("✅ متن خوش‌آمد تغییر کرد.")

async def setsupport(update, context):
    if not is_admin(update.effective_user.id): return
    if not context.args: return
    set_setting("support",context.args[0].lstrip("@")); await update.message.reply_text("✅ پشتیبانی تغییر کرد.")

async def maintenance(update, context):
    if not is_admin(update.effective_user.id): return
    val=(context.args[0].lower() if context.args else "off")
    set_setting("maintenance","1" if val in ("on","1","true") else "0")
    await update.message.reply_text("✅ وضعیت نگهداری تغییر کرد.")

async def cancel(update, context):
    context.user_data.clear()
    await update.message.reply_text("لغو شد.",reply_markup=main_kb(update.effective_user.id))

# ---------- Health ----------
async def health(request):
    return web.json_response({"status":"ok","service":BRAND,"time":now_iso()})

async def start_health_server():
    app=web.Application()
    app.router.add_get("/",health)
    app.router.add_get("/health",health)
    runner=web.AppRunner(app)
    await runner.setup()
    port=int(os.getenv("PORT","8080"))
    site=web.TCPSite(runner,"0.0.0.0",port)
    await site.start()
    log.info("health server listening on %s",port)
    return runner

async def post_init(app):
    await start_health_server()

def main():
    application=Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start",start))
    async def plans_cmd(u,c):
        text, buttons = await plans_text()
        buttons.append([InlineKeyboardButton("⬅️ برگشت", callback_data="home")])
        await u.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
    async def profile_cmd(u,c):
        await u.message.reply_text(await profile_text(u.effective_user.id), parse_mode=ParseMode.HTML, reply_markup=back_kb())
    async def help_cmd(u,c):
        await u.message.reply_text(await help_text(), parse_mode=ParseMode.HTML, reply_markup=back_kb())
    application.add_handler(CommandHandler("plans",plans_cmd))
    application.add_handler(CommandHandler("profile",profile_cmd))
    application.add_handler(CommandHandler("help",help_cmd))
    application.add_handler(CommandHandler("cancel",cancel))
    application.add_handler(CommandHandler("coupon",coupon_cmd))
    application.add_handler(CommandHandler("admin",admin_command))
    application.add_handler(CommandHandler("addcoupon",addcoupon))
    application.add_handler(CommandHandler("setcard",setcard))
    application.add_handler(CommandHandler("setwelcome",setwelcome))
    application.add_handler(CommandHandler("setsupport",setsupport))
    application.add_handler(CommandHandler("maintenance",maintenance))
    conv=ConversationHandler(
        entry_points=[CommandHandler("newplan",add_plan_start)],
        states={ADMIN_INPUT:[MessageHandler(filters.TEXT & ~filters.COMMAND,admin_text_input)]},
        fallbacks=[CommandHandler("cancel",cancel)]
    )
    # Replace standalone newplan handler with conversation handler by adding conversation before text.
    application.add_handler(conv)
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL,handle_receipt))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,ai_message))
    log.info("%s starting",BRAND)
    application.run_polling(allowed_updates=Update.ALL_TYPES,drop_pending_updates=True)

if __name__=="__main__":
    main()
