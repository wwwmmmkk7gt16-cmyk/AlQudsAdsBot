import os
import asyncio
import sqlite3
import logging
import threading
import base64
import json
import urllib.request
import urllib.error
import time
import csv
import re
from io import BytesIO, StringIO

from flask import Flask
from PIL import Image, ImageDraw, ImageFont
from datetime import date, datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("alquds")
ADS_TOKEN = os.getenv("BOT_TOKEN")
ORDERS_TOKEN = os.getenv("ORDERS_BOT_TOKEN")
ADMIN = int(os.getenv("ADMIN_ID", "0"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "@MajmaAlQuds")
if not ADS_TOKEN:
    raise RuntimeError("Set BOT_TOKEN (ads bot) in environment variables")
if not ORDERS_TOKEN:
    raise RuntimeError("Set ORDERS_BOT_TOKEN (orders bot) in environment variables")

web = Flask(__name__)

@web.get("/")
def home():
    return "AlQuds Orders + Ads Bot is running"

@web.get("/health")
def health():
    return {"status": "ok"}

def get_font(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()



GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")


def _gemini_describe_product(image_bytes):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing from environment variables")
    url = ("https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    payload = {"contents": [{"parts": [
        {"text": (
            "شاهد صورة المنتج واكتب وصفاً إعلانياً قصيراً جداً باللهجة العراقية "
            "لمجمع القدس. اذكر نوع المنتج إذا كان واضحاً، ثم ميزة ظاهرة واحدة فقط. "
            "لا تخترع ماركة أو خامة أو قياساً أو مواصفات. لا تذكر السعر أو التوصيل "
            "أو وسيلة التواصل. اكتب جملة واحدة قصيرة فقط."
        )},
        {"inline_data": {"mime_type": "image/jpeg",
                         "data": base64.b64encode(image_bytes).decode("ascii")}}
    ]}], "generationConfig": {"temperature": 0.4, "maxOutputTokens": 80}}
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    # One short attempt only: Gemini must never hold up the whole ad workflow.
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    parts = result.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    description = " ".join(p.get("text", "").strip() for p in parts if p.get("text")).strip()
    if not description:
        raise ValueError("Gemini returned empty description")
    return description[:350]


async def describe_product(image_bytes):
    try:
        return await asyncio.to_thread(_gemini_describe_product, image_bytes)
    except Exception as exc:
        # Publish safely even when Gemini is unavailable; do not repeat a false product claim.
        log.warning("Gemini unavailable; publishing with neutral fallback: %s", type(exc).__name__)
        return "✨ منتج متوفر هسه، شوفوا تفاصيله بالصورة"


def make_ad_image(raw, price):
    im = Image.open(BytesIO(raw)).convert("RGB")
    im.thumbnail((1120, 1120))
    canvas = Image.new("RGB", (1200, 1450), "white")
    x = (1200 - im.width) // 2
    y = 35 + (1160 - im.height) // 2
    canvas.paste(im, (x, y))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((0, 1240, 1200, 1450), fill=(24, 35, 52))
    draw.text((600, 1285), "مجمع القدس", font=get_font(58), fill="white", anchor="mm")
    draw.text((600, 1380), f"السعر: {price}", font=get_font(64), fill=(255, 220, 90), anchor="mm")
    out = BytesIO()
    canvas.save(out, format="JPEG", quality=92)
    out.seek(0)
    return out

db = sqlite3.connect("store.db", check_same_thread=False)
db.execute("""CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT, price INTEGER, description TEXT
)""")
db.execute("""CREATE TABLE IF NOT EXISTS orders (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 customer TEXT, phone TEXT, address TEXT,
 items TEXT, total INTEGER
)""")

db.execute("""CREATE TABLE IF NOT EXISTS categories (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL UNIQUE,
 photo_id TEXT
)""")
db.execute("""CREATE TABLE IF NOT EXISTS offers (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 product_id INTEGER NOT NULL,
 original_price INTEGER NOT NULL,
 offer_price INTEGER NOT NULL,
 expires_at TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1
)""")
db.execute("""CREATE TABLE IF NOT EXISTS bot_users (
 user_id INTEGER PRIMARY KEY
)""")
db.execute("""CREATE TABLE IF NOT EXISTS coupons (
 code TEXT PRIMARY KEY, discount_type TEXT NOT NULL, value INTEGER NOT NULL,
 expires_at TEXT, max_uses INTEGER, uses INTEGER NOT NULL DEFAULT 0,
 active INTEGER NOT NULL DEFAULT 1
)""")

def add_column(table, column, definition):
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    if column not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

add_column("products", "photo_id", "TEXT")
add_column("products", "category_id", "INTEGER")
add_column("products", "barcode", "TEXT")
add_column("products", "source_sku", "TEXT")
add_column("products", "source_handle", "TEXT")
add_column("products", "visible", "INTEGER NOT NULL DEFAULT 1")
add_column("products", "import_status", "TEXT")
db.execute("""CREATE TABLE IF NOT EXISTS photo_matches (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 photo_id TEXT NOT NULL,
 product_id INTEGER NOT NULL,
 confidence REAL DEFAULT 0,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP
)""")
add_column("orders", "status", "TEXT DEFAULT 'جديد'")
add_column("orders", "user_id", "INTEGER DEFAULT 0")
add_column("orders", "subtotal", "INTEGER DEFAULT 0")
add_column("orders", "discount", "INTEGER DEFAULT 0")
add_column("orders", "coupon_code", "TEXT")
db.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", ("غير مصنف",))
uncategorized_id = db.execute("SELECT id FROM categories WHERE name=?", ("غير مصنف",)).fetchone()[0]
db.execute("UPDATE products SET category_id=? WHERE category_id IS NULL", (uncategorized_id,))
db.commit()

carts = {}
states = {}
user_coupons = {}

def money(n):
    return f"{n:,} د.ع"

def active_offer(product_id):
    now = datetime.now().isoformat(timespec="seconds")
    return db.execute(
        "SELECT offer_price,expires_at FROM offers WHERE product_id=? AND active=1 AND expires_at>? ORDER BY id DESC LIMIT 1",
        (product_id, now)
    ).fetchone()


def effective_price(product_id, regular_price):
    offer = active_offer(product_id)
    return offer[0] if offer else regular_price


def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍️ تصفح المنتجات", callback_data="products")],
        [InlineKeyboardButton("🛒 سلة المشتريات", callback_data="cart")],
        [InlineKeyboardButton("📦 طلباتي", callback_data="myorders")],
        [InlineKeyboardButton("🔥 العروض الحالية", callback_data="offers")],
        [InlineKeyboardButton("☎️ تواصل ويانا", url="https://t.me/LLL_54")]
    ])

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ إضافة منتج", callback_data="admin_add")],
        [InlineKeyboardButton("📦 إدارة المنتجات", callback_data="admin_products")],
        [InlineKeyboardButton("📂 إدارة الأقسام", callback_data="admin_categories")],
        [InlineKeyboardButton("🛍️ الطلبات الجديدة", callback_data="admin_orders")],
        [InlineKeyboardButton("🎟️ إدارة كوبونات الخصم", callback_data="admin_coupons")],
        [InlineKeyboardButton("🔥 إدارة العروض المؤقتة", callback_data="admin_offers")],
        [InlineKeyboardButton("📥 استيراد منتجات Loyverse", callback_data="admin_import_help")],
        [InlineKeyboardButton("📸 ربط صور بالجملة", callback_data="admin_bulk_help")],
    ])

def categories_keyboard():
    rows = db.execute("SELECT id,name FROM categories ORDER BY name").fetchall()
    buttons = [[InlineKeyboardButton(name, callback_data=f"choosecat:{cid}")]
               for cid, name in rows]
    buttons.append([InlineKeyboardButton("بدون قسم", callback_data="choosecat:0")])
    return InlineKeyboardMarkup(buttons)

async def show_categories(q):
    rows = db.execute("SELECT id,name,photo_id FROM categories ORDER BY name").fetchall()
    if not rows:
        await q.message.reply_text("ماكو أقسام حالياً.")
        return
    await q.message.reply_text("📂 اختار القسم:")
    for cid, name, photo in rows:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("عرض المنتجات", callback_data=f"category:{cid}")
        ]])
        if photo:
            await q.message.reply_photo(photo, caption=f"📂 {name}", reply_markup=kb)
        else:
            await q.message.reply_text(f"📂 {name}", reply_markup=kb)

async def show_category_admin(q):
    rows = db.execute("SELECT id,name FROM categories ORDER BY name").fetchall()
    buttons = [
        [InlineKeyboardButton(f"✏️ {name}", callback_data=f"catedit:{cid}"),
         InlineKeyboardButton("🗑️ حذف", callback_data=f"catdel:{cid}")]
        for cid, name in rows
    ]
    buttons.append([InlineKeyboardButton("➕ إضافة قسم", callback_data="admin_add_category")])
    await q.message.reply_text(
        "📂 إدارة الأقسام\nاختر قسماً للتعديل أو الحذف:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

def order_status_keyboard(order_id, status):
    if status == "جديد":
        rows = [[
            InlineKeyboardButton("✅ قبول الطلب", callback_data=f"accept:{order_id}"),
            InlineKeyboardButton("❌ رفض الطلب", callback_data=f"reject:{order_id}")
        ]]
    elif status == "تم قبول الطلب":
        rows = [[InlineKeyboardButton("🧰 بدء التجهيز", callback_data=f"stage:{order_id}:قيد التجهيز")]]
    elif status == "قيد التجهيز":
        rows = [[InlineKeyboardButton("🚚 خرج للتوصيل", callback_data=f"stage:{order_id}:خرج للتوصيل")]]
    elif status == "خرج للتوصيل":
        rows = [[InlineKeyboardButton("🏁 تم التسليم", callback_data=f"stage:{order_id}:تم التسليم")]]
    else:
        rows = []
    return InlineKeyboardMarkup(rows) if rows else None




def _clean_int(value):
    value = str(value or "").strip().replace(",", "")
    if not value or value.lower() == "variable":
        return None
    try:
        number = float(value)
        return int(number) if number > 0 else None
    except Exception:
        return None


def import_loyverse_csv(raw_bytes):
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(StringIO(text))
    required = {"Name", "Price [مجمع القدس]"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise ValueError("ملف CSV مو بصيغة تصدير Loyverse المتوقعة")
    inserted = updated = skipped = 0
    uncid = db.execute("SELECT id FROM categories WHERE name=?", ("غير مصنف",)).fetchone()[0]
    for row in reader:
        name = (row.get("Name") or "").strip()
        if not name:
            skipped += 1
            continue
        price = _clean_int(row.get("Price [مجمع القدس]"))
        if price is None:
            skipped += 1
            continue
        category = (row.get("Category") or "").strip() or "غير مصنف"
        db.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (category,))
        cat_id = db.execute("SELECT id FROM categories WHERE name=?", (category,)).fetchone()[0]
        sku = (row.get("SKU") or "").strip() or None
        handle = (row.get("Handle") or "").strip() or None
        barcode = (row.get("Barcode") or "").strip() or None
        existing = None
        if sku:
            existing = db.execute("SELECT id FROM products WHERE source_sku=?", (sku,)).fetchone()
        if not existing and barcode:
            existing = db.execute("SELECT id FROM products WHERE barcode=?", (barcode,)).fetchone()
        if not existing:
            db.execute("""INSERT INTO products(name,price,description,category_id,barcode,source_sku,source_handle,visible,import_status)
                          VALUES(?,?,?,?,?,?,?,0,'بانتظار الصورة')""",
                       (name, price, (row.get("Description") or "").strip(), cat_id, barcode, sku, handle))
            inserted += 1
        else:
            db.execute("""UPDATE products SET name=?,price=?,description=?,category_id=?,barcode=COALESCE(?,barcode),
                          source_sku=COALESCE(?,source_sku),source_handle=COALESCE(?,source_handle),
                          visible=CASE WHEN photo_id IS NULL THEN 0 ELSE visible END,
                          import_status=CASE WHEN photo_id IS NULL THEN 'بانتظار الصورة' ELSE import_status END WHERE id=?""",
                       (name, price, (row.get("Description") or "").strip(), cat_id, barcode, sku, handle, existing[0]))
            updated += 1
    db.commit()
    return inserted, updated, skipped


def _gemini_match_product(image_bytes, products):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY غير مضبوط")

    candidates = [
        {"id": p[0], "name": p[1], "category": p[2] or "",
         "barcode": p[3] or "", "sku": p[4] or ""}
        for p in products
    ]
    prompt = (
        "أنت نظام مطابقة صور لمتجر. اختر المنتج المطابق للصورة من القائمة فقط. "
        "اعتمد على النص الظاهر والباركود والشكل. لا تخمّن عند التشابه. "
        "أرجع كائن JSON صالحاً فقط دون Markdown أو شرح، بالشكل: "
        "{\"product_id\": رقم أو null, \"confidence\": رقم من 0 إلى 1, "
        "\"reason\": \"سبب قصير بالعربية\"}. "
        "إذا لا يوجد تطابق واضح اجعل product_id null و confidence أقل من 0.7.\n"
        "القائمة:\n" + json.dumps(candidates, ensure_ascii=False)
    )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode("ascii")
                }}
            ]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "maxOutputTokens": 160
        }
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_error = None
    for attempt, delay in enumerate((0, 5, 12, 20), start=1):
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                response_text = response.read().decode("utf-8", errors="replace")

            if not response_text.strip():
                raise ValueError("Gemini returned an empty HTTP response")

            envelope = json.loads(response_text)
            candidates_out = envelope.get("candidates") or []
            if not candidates_out:
                prompt_feedback = envelope.get("promptFeedback", {})
                raise ValueError(
                    "Gemini returned no candidates: "
                    + json.dumps(prompt_feedback, ensure_ascii=False)[:300]
                )

            parts = (candidates_out[0].get("content") or {}).get("parts") or []
            raw = "".join(part.get("text", "") for part in parts).strip()
            if not raw:
                finish_reason = candidates_out[0].get("finishReason", "unknown")
                raise ValueError(f"Gemini returned empty model text (finishReason={finish_reason})")

            # Tolerate accidental Markdown fences or text around the JSON object.
            cleaned = raw.strip()
            cleaned = re.sub(r"^\s*```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```\s*$", "", cleaned)
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                first = cleaned.find("{")
                last = cleaned.rfind("}")
                if first < 0 or last <= first:
                    raise ValueError("Gemini response did not contain a JSON object")
                data = json.loads(cleaned[first:last + 1])

            if not isinstance(data, dict):
                raise ValueError("Gemini JSON response was not an object")

            confidence = float(data.get("confidence", 0) or 0)
            product_id = data.get("product_id")
            if product_id is not None:
                product_id = int(product_id)
            return {
                "product_id": product_id,
                "confidence": max(0.0, min(1.0, confidence)),
                "reason": str(data.get("reason", ""))[:200],
            }

        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
            log.warning("Gemini temporary HTTP %s on attempt %s/4", exc.code, attempt)
        except (TimeoutError, urllib.error.URLError, ConnectionError,
                ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            log.warning(
                "Gemini request/response failed on attempt %s/4: %s: %s",
                attempt, type(exc).__name__, str(exc)[:180]
            )

    raise RuntimeError(
        "Gemini unavailable or returned invalid JSON after 4 attempts: "
        f"{type(last_error).__name__ if last_error else 'unknown error'}"
    )


async def import_csv_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN:
        return
    context.user_data["awaiting_loyverse_csv"] = True
    await update.message.reply_text("📥 أرسل ملف تصدير Loyverse بصيغة CSV هنا. راح أضيف المنتجات وأخفيها عن الزبائن إلى أن نربط صورها.\nاكتب /cancel للإلغاء.")


async def csv_document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN or not context.user_data.get("awaiting_loyverse_csv"):
        return
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".csv"):
        await update.message.reply_text("أرسل ملف CSV فقط.")
        return
    tgfile = await doc.get_file()
    buf = BytesIO()
    await tgfile.download_to_memory(out=buf)
    try:
        inserted, updated, skipped = await asyncio.to_thread(import_loyverse_csv, buf.getvalue())
    except Exception as exc:
        log.exception("Loyverse CSV import failed")
        await update.message.reply_text(f"تعذر استيراد الملف: {exc}")
        return
    context.user_data.pop("awaiting_loyverse_csv", None)
    await update.message.reply_text(
        f"✅ اكتمل الاستيراد\n➕ منتجات جديدة: {inserted}\n🔄 منتجات محدثة: {updated}\n"
        f"⏭️ متروكة (سعر متغير/بيانات ناقصة): {skipped}\n\nهسه استخدم /bulkphotos وابدأ إرسال صور المنتجات."
    )


async def bulk_photos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN:
        return
    count = db.execute("SELECT COUNT(*) FROM products WHERE visible=0 AND import_status='بانتظار الصورة'").fetchone()[0]
    if not count:
        await update.message.reply_text("ماكو منتجات مستوردة تنتظر الصور. استورد CSV أولاً باستخدام /importcsv")
        return
    context.user_data["bulk_photo_mode"] = True
    context.user_data["bulk_photo_count"] = 0
    await update.message.reply_text(
        f"📸 وضع ربط الصور بالجملة اشتغل. عندك {count} منتج ينتظر الصورة.\n"
        "أرسل الصور واحدة ورا الثانية أو كألبوم. راح أطابق الواضح تلقائياً، والمشكوك بيه أخليه للمراجعة.\n"
        "لإنهاء الدفعة أرسل /endbulk"
    )


async def end_bulk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN:
        return
    context.user_data.pop("bulk_photo_mode", None)
    context.user_data.pop("bulk_photo_count", None)
    pending = db.execute("SELECT COUNT(*) FROM photo_matches").fetchone()[0]
    await update.message.reply_text(f"انتهى استقبال الصور. الصور اللي تحتاج مراجعة محفوظة: {pending}.")


async def bulk_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN or not context.user_data.get("bulk_photo_mode"):
        return
    photo = update.message.photo[-1]
    tgfile = await photo.get_file()
    buf = BytesIO()
    await tgfile.download_to_memory(out=buf)
    candidates = db.execute("""SELECT id,name,(SELECT name FROM categories c WHERE c.id=p.category_id),barcode,source_sku
                               FROM products p WHERE visible=0 AND import_status='بانتظار الصورة' ORDER BY name LIMIT 250""").fetchall()
    if not candidates:
        context.user_data["bulk_photo_mode"] = False
        await update.message.reply_text("🎉 خلصت كل المنتجات اللي كانت تنتظر الصور!")
        return
    try:
        match = await asyncio.to_thread(_gemini_match_product, buf.getvalue(), candidates)
        pid = match.get("product_id")
        confidence = float(match.get("confidence", 0) or 0)
        found = next((p for p in candidates if p[0] == pid), None)
        if found and confidence >= 0.82:
            db.execute("UPDATE products SET photo_id=?,visible=1,import_status='مرتبط تلقائياً' WHERE id=?", (photo.file_id, pid))
            db.commit()
            price = db.execute("SELECT price FROM products WHERE id=?", (pid,)).fetchone()[0]
            await update.message.reply_text(f"✅ تم الربط: {found[1]}\nالسعر: {money(price)}\nالثقة: {confidence:.0%}")
        elif found:
            cur = db.execute("INSERT INTO photo_matches(photo_id,product_id,confidence) VALUES(?,?,?)", (photo.file_id, found[0], confidence))
            db.commit()
            mid = cur.lastrowid
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ اعتماد هذا المنتج", callback_data=f"matchapprove:{mid}"), InlineKeyboardButton("❌ تجاهل", callback_data=f"matchskip:{mid}")]])
            price = db.execute("SELECT price FROM products WHERE id=?", (found[0],)).fetchone()[0]
            await update.message.reply_photo(photo.file_id, caption=f"⚠️ تأكيد الربط مطلوب\nالاقتراح: {found[1]}\nالسعر: {money(price)}\nالثقة: {confidence:.0%}", reply_markup=kb)
        else:
            await update.message.reply_text(f"⚠️ ما لكيت تطابق واضح لهالصورة.\nالثقة: {confidence:.0%}\nجرّب تصور المنتج ويظهر اسمه أو باركوده بوضوح.")
    except Exception:
        log.exception("Bulk image matching failed")
        await update.message.reply_text("تعذر تحليل هالصورة حالياً. خليها بدون ربط وجرب مرة ثانية بعدين.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db.execute("INSERT OR IGNORE INTO bot_users(user_id) VALUES(?)", (update.effective_user.id,))
    db.commit()
    await update.message.reply_text(
        "أهلاً بيك بمجمع القدس ❤️\n"
        "اختار من القائمة واطلب بكل سهولة.\n\n"
        "🚚 التوصيل مجاني داخل النجف",
        reply_markup=menu()
    )

async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN:
        return
    await update.message.reply_text("⚙️ لوحة إدارة مجمع القدس", reply_markup=admin_menu())

async def show_products(q, category_id=None):
    if category_id is None:
        rows = db.execute(
            "SELECT id,name,price,description,photo_id FROM products ORDER BY id DESC"
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id,name,price,description,photo_id FROM products WHERE category_id=? ORDER BY id DESC",
            (category_id,)
        ).fetchall()
    if not rows:
        await q.message.reply_text("حالياً ماكو منتجات بهذا القسم ❤️")
        return
    for pid, name, price, desc, photo in rows:
        offer = active_offer(pid)
        if offer:
            text = f"🔥 عرض مؤقت: {name}\\nالسعر السابق: {money(price)}\\n💰 سعر العرض: {money(offer[0])}\\n{desc or ''}"
        else:
            text = f"🛍️ {name}\\n💰 {money(price)}\\n{desc or ''}"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ أضف للسلة", callback_data=f"add:{pid}")
        ]])
        if photo:
            await q.message.reply_photo(photo, caption=text, reply_markup=kb)
        else:
            # Imported products can be browsed before their photo is matched.
            await q.message.reply_text(text + "\n📷 الصورة قريباً", reply_markup=kb)

def coupon_discount(uid, subtotal):
    code = user_coupons.get(uid)
    if not code:
        return 0, None
    row = db.execute("SELECT discount_type,value,expires_at,max_uses,uses,active FROM coupons WHERE code=?", (code,)).fetchone()
    if not row:
        user_coupons.pop(uid, None)
        return 0, None
    kind, value, expiry, maximum, uses, active = row
    if not active or (expiry and expiry < date.today().isoformat()) or (maximum is not None and uses >= maximum):
        user_coupons.pop(uid, None)
        return 0, None
    discount = round(subtotal * value / 100) if kind == "percent" else value
    return min(subtotal, max(0, discount)), code


def check_coupon(code):
    row = db.execute("SELECT discount_type,value,expires_at,max_uses,uses,active FROM coupons WHERE code=?", (code.upper(),)).fetchone()
    if not row:
        return None, "هذا الكوبون غير موجود."
    kind, value, expiry, maximum, uses, active = row
    if not active:
        return None, "هذا الكوبون متوقف حالياً."
    if expiry and expiry < date.today().isoformat():
        return None, "انتهت صلاحية هذا الكوبون."
    if maximum is not None and uses >= maximum:
        return None, "تم استنفاد عدد مرات استخدام الكوبون."
    return row, None


async def show_offer_admin(q):
    now = datetime.now().isoformat(timespec="seconds")
    rows = db.execute(
        "SELECT o.id,p.name,o.original_price,o.offer_price,o.expires_at,o.active "
        "FROM offers o JOIN products p ON p.id=o.product_id "
        "WHERE o.active=1 AND o.expires_at>? AND p.visible=1 ORDER BY o.id DESC", (now,)
    ).fetchall()
    lines = ["🔥 العروض الفعالة:"]
    buttons = []
    for oid, name, old_price, new_price, expiry, active in rows:
        lines.append(f"• {name}: {money(old_price)} ← {money(new_price)}\\nينتهي: {expiry.replace('T', ' ')}")
        buttons.append([InlineKeyboardButton(f"⛔ إيقاف عرض {name[:25]}", callback_data=f"offerstop:{oid}")])
    if not rows:
        lines.append("ماكو عروض فعالة حالياً.")
    buttons.append([InlineKeyboardButton("➕ إنشاء عرض جديد", callback_data="offer_add")])
    await q.message.reply_text("\\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def show_customer_offers(q):
    now = datetime.now().isoformat(timespec="seconds")
    rows = db.execute(
        "SELECT p.id,p.name,o.original_price,o.offer_price,o.expires_at,p.description,p.photo_id "
        "FROM offers o JOIN products p ON p.id=o.product_id "
        "WHERE o.active=1 AND o.expires_at>? AND p.visible=1 ORDER BY o.id DESC", (now,)
    ).fetchall()
    if not rows:
        await q.message.reply_text("حالياً ماكو عروض سارية ❤️")
        return
    for pid, name, old_price, new_price, expiry, desc, photo in rows:
        caption = (f"🔥 عرض مؤقت من مجمع القدس\\n🛍️ {name}\\n"
                   f"قبل: {money(old_price)}\\n💰 هسه: {money(new_price)}\\n"
                   f"⏰ ينتهي: {expiry.replace('T', ' ')}\\n{desc or ''}")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ أضف للسلة بسعر العرض", callback_data=f"add:{pid}")]])
        if photo:
            await q.message.reply_photo(photo, caption=caption, reply_markup=kb)
        else:
            await q.message.reply_text(caption, reply_markup=kb)


async def show_coupon_admin(q):
    rows = db.execute("SELECT code,discount_type,value,expires_at,max_uses,uses,active FROM coupons ORDER BY code").fetchall()
    lines = ["🎟️ كوبونات الخصم:"]
    buttons = []
    for code, kind, value, expiry, maximum, uses, active in rows:
        amount = f"{value}%" if kind == "percent" else money(value)
        lines.append(f"{code}: خصم {amount} | الاستخدام {uses}/{maximum if maximum is not None else '∞'} | {'فعال' if active else 'متوقف'}")
        buttons.append([InlineKeyboardButton(("⛔ إيقاف " if active else "▶️ تفعيل ") + code, callback_data=("couponoff:" if active else "couponon:") + code)])
    buttons.append([InlineKeyboardButton("➕ إنشاء كوبون", callback_data="coupon_add")])
    await q.message.reply_text("\\n".join(lines), reply_markup=InlineKeyboardMarkup(buttons))


async def show_cart(q, uid):
    cart = carts.get(uid, {})
    if not cart:
        await q.message.reply_text("سلتك فارغة 🛒")
        return
    subtotal = 0
    lines = []
    for pid, qty in cart.items():
        row = db.execute("SELECT name,price FROM products WHERE id=?", (pid,)).fetchone()
        if row:
            name, regular_price = row
            price = effective_price(pid, regular_price)
            subtotal += price * qty
            offer_note = " 🔥" if price != regular_price else ""
            lines.append(f"{name}{offer_note} × {qty} = {money(price*qty)}")
    discount, code = coupon_discount(uid, subtotal)
    total = subtotal - discount
    coupon_line = f"\\n🎟️ الكوبون: {code}\\n💸 الخصم: {money(discount)}" if code else "\\n🎟️ لا يوجد كوبون مطبق"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎟️ إدخال كوبون خصم", callback_data="apply_coupon")],
        [InlineKeyboardButton("❌ إزالة الكوبون", callback_data="remove_coupon")],
        [InlineKeyboardButton("✅ تثبيت الطلب", callback_data="checkout")],
        [InlineKeyboardButton("🗑️ إفراغ السلة", callback_data="clear")]
    ])
    await q.message.reply_text("🛒 سلتك:\\n\\n" + "\\n".join(lines) +
        f"\\n\\nالمجموع: {money(subtotal)}" + coupon_line +
        f"\\n🚚 التوصيل مجاني\\n💰 الإجمالي: {money(total)}", reply_markup=kb)


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if uid == ADMIN and data.startswith("matchapprove:"):
        mid = int(data.split(":")[1])
        row = db.execute("SELECT photo_id,product_id FROM photo_matches WHERE id=?", (mid,)).fetchone()
        if row:
            db.execute("UPDATE products SET photo_id=?,visible=1,import_status='تم تأكيد الصورة' WHERE id=?", (row[0], row[1]))
            db.execute("DELETE FROM photo_matches WHERE id=?", (mid,))
            db.commit()
            await q.message.reply_text("✅ تم ربط الصورة بالمنتج وإظهاره للزبائن.")
        else:
            await q.message.reply_text("انتهت صلاحية طلب المراجعة.")
    elif uid == ADMIN and data.startswith("matchskip:"):
        mid = int(data.split(":")[1])
        db.execute("DELETE FROM photo_matches WHERE id=?", (mid,))
        db.commit()
        await q.message.reply_text("تم تجاهل المطابقة. المنتج بقي مخفياً.")
    elif uid == ADMIN and data == "admin_import_help":
        await q.message.reply_text("لاستيراد قائمة Loyverse استخدم الأمر /importcsv ثم أرسل ملف CSV.")
    elif uid == ADMIN and data == "admin_bulk_help":
        await q.message.reply_text("لربط الصور بالجملة استخدم /bulkphotos، أرسل الصور، ثم /endbulk عند الانتهاء.")
    elif data == "offers":
        await show_customer_offers(q)
    elif uid == ADMIN and data == "admin_offers":
        await show_offer_admin(q)
    elif uid == ADMIN and data == "offer_add":
        rows = db.execute("SELECT id,name,price FROM products ORDER BY name").fetchall()
        if not rows:
            await q.message.reply_text("أضف منتجات أولاً حتى تنشئ عروضاً.")
            return
        buttons = [[InlineKeyboardButton(f"{name} — {money(price)}", callback_data=f"offerproduct:{pid}")]
                   for pid, name, price in rows]
        await q.message.reply_text("اختار المنتج الذي تريد تسوي له عرض:", reply_markup=InlineKeyboardMarkup(buttons))
    elif uid == ADMIN and data.startswith("offerproduct:"):
        pid = int(data.split(":")[1])
        row = db.execute("SELECT name,price FROM products WHERE id=?", (pid,)).fetchone()
        if not row:
            await q.message.reply_text("المنتج غير موجود.")
            return
        states[uid] = {"step": "offer_price", "product_id": pid, "product_name": row[0], "original_price": row[1]}
        await q.message.reply_text(f"🔥 المنتج: {row[0]}\\nالسعر الحالي: {money(row[1])}\\nاكتب سعر العرض بالدينار:")
    elif uid == ADMIN and data.startswith("offerstop:"):
        oid = int(data.split(":")[1])
        db.execute("UPDATE offers SET active=0 WHERE id=?", (oid,))
        db.commit()
        await q.message.reply_text("تم إيقاف العرض.")
        await show_offer_admin(q)
    elif uid == ADMIN and data == "admin_coupons":
        await show_coupon_admin(q)
    elif uid == ADMIN and data == "coupon_add":
        states[uid] = {"step": "coupon_create"}
        await q.message.reply_text(
            "أرسل بيانات الكوبون بهذا الشكل:\\n"
            "CODE | percent أو fixed | القيمة | تاريخ الانتهاء YYYY-MM-DD أو - | عدد الاستخدامات أو -\\n\\n"
            "مثال: ALQUDS10 | percent | 10 | 2026-12-31 | 100\\n"
            "مثال: SAVE5000 | fixed | 5000 | - | -"
        )
    elif uid == ADMIN and (data.startswith("couponoff:") or data.startswith("couponon:")):
        code = data.split(":", 1)[1]
        db.execute("UPDATE coupons SET active=? WHERE code=?", (0 if data.startswith("couponoff:") else 1, code))
        db.commit()
        await show_coupon_admin(q)
    elif data == "apply_coupon":
        states[uid] = {"step": "customer_coupon"}
        await q.message.reply_text("🎟️ اكتب رمز كوبون الخصم:")
    elif data == "remove_coupon":
        user_coupons.pop(uid, None)
        await q.message.reply_text("تمت إزالة الكوبون من سلتك.")
        await show_cart(q, uid)
    elif data == "products":
        await show_categories(q)
    elif data.startswith("category:"):
        await show_products(q, int(data.split(":")[1]))
    elif data.startswith("choosecat:") and uid == ADMIN:
        cid = int(data.split(":")[1])
        state = states.get(uid, {})
        if state.get("step") != "product_category":
            return
        if cid == 0:
            cid = db.execute("SELECT id FROM categories WHERE name=?", ("غير مصنف",)).fetchone()[0]
        db.execute(
            "INSERT INTO products(name,price,description,photo_id,category_id) VALUES(?,?,?,?,?)",
            (state["name"], state["price"], state["description"], state.get("photo"), cid)
        )
        db.commit()
        states.pop(uid, None)
        await q.message.reply_text("✅ تمت إضافة المنتج وربطه بالقسم بنجاح!", reply_markup=admin_menu())
    elif data.startswith("add:"):
        pid = int(data.split(":")[1])
        if db.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone():
            carts.setdefault(uid, {})
            carts[uid][pid] = carts[uid].get(pid, 0) + 1
            action_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 اطلب الآن", callback_data="cart")],
                [InlineKeyboardButton("🛍️ تابع التصفح", callback_data="products")]
            ])
            await q.message.reply_text(
                "✅ انضاف المنتج للسلة بنجاح!\nشنو تحب تسوي هسه؟",
                reply_markup=action_kb
            )
    elif data == "cart":
        await show_cart(q, uid)
    elif data == "clear":
        carts[uid] = {}
        user_coupons.pop(uid, None)
        await q.message.reply_text("تم إفراغ السلة.")
    elif data == "checkout":
        if not carts.get(uid):
            await q.message.reply_text("السلة فارغة.")
            return
        states[uid] = {"step": "name"}
        await q.message.reply_text("📝 اكتب اسمك الكامل:")
    elif data == "myorders":
        rows = db.execute(
            "SELECT id,total,status FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (uid,)
        ).fetchall()
        if not rows:
            await q.message.reply_text("ما عندك طلبات سابقة.")
        else:
            buttons = [[InlineKeyboardButton(
                f"📦 طلب #{oid} — {money(total)} — {status or 'جديد'}",
                callback_data=f"track:{oid}"
            )] for oid, total, status in rows]
            await q.message.reply_text(
                "📦 طلباتك السابقة\nاضغط على أي طلب حتى تشوف تفاصيله ومراحل توصيله:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
    elif data.startswith("track:"):
        oid = int(data.split(":")[1])
        order = db.execute(
            "SELECT customer,phone,address,items,total,status FROM orders WHERE id=? AND user_id=?",
            (oid, uid)
        ).fetchone()
        if not order:
            await q.message.reply_text("ما لكيت هذا الطلب ضمن طلبات حسابك.")
            return
        customer, phone, address, items, total, status = order
        status = status or "جديد"
        stages = ["جديد", "تم قبول الطلب", "قيد التجهيز", "خرج للتوصيل", "تم التسليم"]
        if status == "تم رفض الطلب":
            progress = "❌ تم رفض الطلب\nإذا تحتاج مساعدة تواصل ويانا @LLL_54"
        else:
            lines = []
            current = stages.index(status) if status in stages else 0
            for index, stage in enumerate(stages):
                mark = "🟢" if index <= current else "⚪"
                lines.append(f"{mark} {stage}")
            progress = "مراحل الطلب:\n" + "\n".join(lines)
        text = (
            f"📦 تفاصيل الطلب #{oid}\n"
            f"👤 {customer}\n📞 {phone}\n📍 {address}\n\n"
            f"🛍️ المنتجات:\n{items}\n"
            f"💰 الإجمالي: {money(total)}\n\n"
            f"{progress}"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ رجوع إلى طلباتي", callback_data="myorders")]])
        await q.message.reply_text(text, reply_markup=kb)

    elif uid == ADMIN and data == "admin_categories":
        await show_category_admin(q)
    elif uid == ADMIN and data == "admin_add_category":
        states[uid] = {"step": "category_photo"}
        await q.message.reply_text("📸 أرسل صورة القسم، أو اكتب /skip بدون صورة.")
    elif uid == ADMIN and data.startswith("catedit:"):
        cid = int(data.split(":")[1])
        row = db.execute("SELECT name FROM categories WHERE id=?", (cid,)).fetchone()
        if not row:
            await q.message.reply_text("القسم غير موجود.")
            return
        states[uid] = {"step": "edit_category_name", "category_id": cid}
        await q.message.reply_text(f"✏️ اكتب الاسم الجديد للقسم (الحالي: {row[0]}):")
    elif uid == ADMIN and data.startswith("catdel:"):
        cid = int(data.split(":")[1])
        uncid = db.execute("SELECT id FROM categories WHERE name=?", ("غير مصنف",)).fetchone()[0]
        if cid == uncid:
            await q.message.reply_text("لا يمكن حذف قسم غير مصنف.")
            return
        db.execute("UPDATE products SET category_id=? WHERE category_id=?", (uncid, cid))
        db.execute("DELETE FROM categories WHERE id=?", (cid,))
        db.commit()
        await q.message.reply_text("✅ تم حذف القسم ونقل منتجاته إلى (غير مصنف).")
    elif uid == ADMIN and data == "admin_add":
        states[uid] = {"step": "photo"}
        await q.message.reply_text("📸 أرسل صورة المنتج، أو اكتب /skip بدون صورة.")
    elif uid == ADMIN and data == "admin_products":
        rows = db.execute("SELECT id,name,price FROM products ORDER BY id DESC").fetchall()
        if not rows:
            await q.message.reply_text("ماكو منتجات حالياً.")
        for pid, name, price in rows:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🗑️ حذف المنتج", callback_data=f"delete:{pid}")
            ]])
            await q.message.reply_text(f"#{pid} — {name} — {money(price)}", reply_markup=kb)
    elif uid == ADMIN and data.startswith("delete:"):
        pid = int(data.split(":")[1])
        db.execute("DELETE FROM products WHERE id=?", (pid,))
        db.commit()
        await q.message.reply_text("✅ تم حذف المنتج.")
    elif uid == ADMIN and data == "admin_orders":
        rows = db.execute(
            "SELECT id,customer,phone,address,items,total,status FROM orders "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
        if not rows:
            await q.message.reply_text("ماكو طلبات حالياً.")
        for r in rows:
            text = (
                f"📦 طلب #{r[0]}\n👤 {r[1]}\n📞 {r[2]}\n"
                f"📍 {r[3]}\n{r[4]}\n💰 {money(r[5])}\n"
                f"الحالة: {r[6]}"
            )
            kb = order_status_keyboard(r[0], r[6])
            await q.message.reply_text(text, reply_markup=kb)
    elif uid == ADMIN and (data.startswith("accept:") or data.startswith("reject:")):
        oid = int(data.split(":")[1])
        row = db.execute(
            "SELECT status,user_id FROM orders WHERE id=?", (oid,)
        ).fetchone()
        if not row:
            await q.message.reply_text("الطلب غير موجود.")
            return
        if row[0] != "جديد":
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("⚠️ هذا الطلب تم حسمه أو تحديثه سابقاً.")
            return

        status = "تم قبول الطلب" if data.startswith("accept:") else "تم رفض الطلب"
        cur = db.execute(
            "UPDATE orders SET status=? WHERE id=? AND status='جديد'",
            (status, oid)
        )
        db.commit()
        if cur.rowcount != 1:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("⚠️ تم تحديث هذا الطلب مسبقاً.")
            return

        await q.edit_message_reply_markup(reply_markup=order_status_keyboard(oid, status))
        await q.message.reply_text(f"✅ تم تحديث الطلب #{oid}: {status}")
        if row[1]:
            try:
                await context.bot.send_message(row[1], f"تحديث طلبك #{oid}: {status} ❤️")
            except Exception:
                pass

    elif uid == ADMIN and data.startswith("stage:"):
        parts = data.split(":", 2)
        oid = int(parts[1])
        new_status = parts[2]
        allowed = {
            "تم قبول الطلب": "قيد التجهيز",
            "قيد التجهيز": "خرج للتوصيل",
            "خرج للتوصيل": "تم التسليم",
        }
        row = db.execute(
            "SELECT status,user_id FROM orders WHERE id=?", (oid,)
        ).fetchone()
        if not row:
            await q.message.reply_text("الطلب غير موجود.")
            return
        if allowed.get(row[0]) != new_status:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("⚠️ لا يمكن تغيير حالة الطلب بهذا الترتيب.")
            return
        cur = db.execute(
            "UPDATE orders SET status=? WHERE id=? AND status=?",
            (new_status, oid, row[0])
        )
        db.commit()
        if cur.rowcount != 1:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("⚠️ تم تحديث الطلب مسبقاً.")
            return
        await q.edit_message_reply_markup(reply_markup=order_status_keyboard(oid, new_status))
        await q.message.reply_text(f"✅ الطلب #{oid}: {new_status}")
        if row[1]:
            try:
                await context.bot.send_message(
                    row[1], f"📦 تحديث طلبك #{oid}: {new_status} ❤️"
                )
            except Exception:
                pass

async def messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = states.get(uid)
    if not state:
        return

    if state["step"] in ("photo", "category_photo"):
        if update.message.photo:
            state["photo"] = update.message.photo[-1].file_id
        elif update.message.text == "/skip":
            state["photo"] = None
        else:
            await update.message.reply_text("أرسل صورة أو اكتب /skip.")
            return
        if state["step"] == "category_photo":
            state["step"] = "category_name"
            await update.message.reply_text("✏️ اكتب اسم القسم:")
        else:
            state["step"] = "product_name"
            await update.message.reply_text("✏️ اكتب اسم المنتج:")
        return

    if not update.message.text:
        await update.message.reply_text("أرسل النص المطلوب من فضلك.")
        return
    value = update.message.text.strip()
    step = state["step"]

    if uid == ADMIN:
        if step == "offer_price":
            try:
                offer_price = int(value.replace(",", ""))
                if offer_price <= 0 or offer_price >= state["original_price"]:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("اكتب سعر عرض صحيح وأقل من السعر الحالي.")
                return
            state["offer_price"] = offer_price
            state["step"] = "offer_hours"
            await update.message.reply_text("⏰ كم ساعة يستمر العرض؟ اكتب عدد الساعات (مثلاً 24):")
            return
        if step == "offer_hours":
            try:
                hours = int(value)
                if hours < 1 or hours > 720:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("اكتب عدد ساعات من 1 إلى 720.")
                return
            expiry = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="seconds")
            db.execute("UPDATE offers SET active=0 WHERE product_id=?", (state["product_id"],))
            db.execute(
                "INSERT INTO offers(product_id,original_price,offer_price,expires_at,active) VALUES(?,?,?,?,1)",
                (state["product_id"], state["original_price"], state["offer_price"], expiry)
            )
            db.commit()
            product = db.execute("SELECT photo_id FROM products WHERE id=?", (state["product_id"],)).fetchone()
            caption = (f"🔥 عرض مؤقت من مجمع القدس\\n🛍️ {state['product_name']}\\n"
                       f"قبل: {money(state['original_price'])}\\n💰 هسه: {money(state['offer_price'])}\\n"
                       f"⏰ العرض لمدة {hours} ساعة\\n🚚 التوصيل مجاني داخل النجف")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛍️ اطلبه هسه", callback_data=f"add:{state['product_id']}")]])
            recipients = {r[0] for r in db.execute("SELECT user_id FROM bot_users").fetchall()}
            recipients.update(r[0] for r in db.execute("SELECT DISTINCT user_id FROM orders WHERE user_id IS NOT NULL AND user_id!=0").fetchall())
            sent = 0
            for customer_id in recipients:
                try:
                    if product and product[0]:
                        await context.bot.send_photo(customer_id, product[0], caption=caption, reply_markup=kb)
                    else:
                        await context.bot.send_message(customer_id, caption, reply_markup=kb)
                    sent += 1
                except Exception:
                    pass
            states.pop(uid, None)
            await update.message.reply_text(
                f"✅ تم إنشاء العرض ونشره.\\n📨 وصل إلى {sent} زبون.\\n⏰ ينتهي: {expiry.replace('T', ' ')}",
                reply_markup=admin_menu()
            )
            return
        if step == "coupon_create":
            parts = [part.strip() for part in value.split("|")]
            if len(parts) != 5:
                await update.message.reply_text("الصيغة غير صحيحة. أرسل خمس خانات مفصولة بعلامة |.")
                return
            code, kind, raw_value, expiry, maximum = parts
            code, kind = code.upper(), kind.lower()
            try:
                amount = int(raw_value)
                if not code or kind not in ("percent", "fixed") or amount <= 0 or (kind == "percent" and amount > 100):
                    raise ValueError
                expiry_value = None if expiry == "-" else expiry
                if expiry_value:
                    date.fromisoformat(expiry_value)
                max_value = None if maximum == "-" else int(maximum)
                if max_value is not None and max_value <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("تحقق من النوع والقيمة والتاريخ وعدد الاستخدامات.")
                return
            try:
                db.execute("INSERT INTO coupons(code,discount_type,value,expires_at,max_uses) VALUES(?,?,?,?,?)",
                           (code, kind, amount, expiry_value, max_value))
                db.commit()
            except sqlite3.IntegrityError:
                await update.message.reply_text("هذا الرمز موجود مسبقاً. اختر رمزاً آخر.")
                return
            states.pop(uid, None)
            await update.message.reply_text(f"✅ تم إنشاء الكوبون {code}.", reply_markup=admin_menu())
            return
        if step == "category_name":
            try:
                db.execute("INSERT INTO categories(name,photo_id) VALUES(?,?)",
                           (value, state.get("photo")))
                db.commit()
            except sqlite3.IntegrityError:
                await update.message.reply_text("هذا القسم موجود مسبقاً. اكتب اسماً آخر:")
                return
            states.pop(uid, None)
            await update.message.reply_text("✅ تمت إضافة القسم بنجاح!", reply_markup=admin_menu())
            return
        if step == "edit_category_name":
            try:
                db.execute("UPDATE categories SET name=? WHERE id=?",
                           (value, state["category_id"]))
                db.commit()
            except sqlite3.IntegrityError:
                await update.message.reply_text("اسم القسم مستخدم. جرّب اسماً آخر:")
                return
            states.pop(uid, None)
            await update.message.reply_text("✅ تم تعديل اسم القسم.", reply_markup=admin_menu())
            return
        if step == "product_name":
            state["name"] = value
            state["step"] = "product_price"
            await update.message.reply_text("💰 اكتب سعر المنتج بالدينار:")
            return
        if step == "product_price":
            try:
                price = int(value.replace(",", ""))
                if price <= 0:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("اكتب السعر بالأرقام فقط.")
                return
            state["price"] = price
            state["step"] = "product_desc"
            await update.message.reply_text("📝 اكتب وصف المنتج:")
            return
        if step == "product_desc":
            state["description"] = value
            state["step"] = "product_category"
            await update.message.reply_text(
                "📂 اختار القسم الذي ينتمي إليه المنتج:",
                reply_markup=categories_keyboard()
            )
            return

    if step == "customer_coupon":
        code = value.upper()
        coupon, error = check_coupon(code)
        if error:
            await update.message.reply_text(error + "\\nحاول مرة ثانية أو أرسل /cancel.")
            return
        user_coupons[uid] = code
        states.pop(uid, None)
        await update.message.reply_text("✅ تم تطبيق الكوبون! ارجع إلى السلة لمشاهدة الخصم.")
        return

    if step == "name":
        state["customer"] = value
        state["step"] = "phone"
        await update.message.reply_text("📞 اكتب رقم هاتفك:")
    elif step == "phone":
        state["phone"] = value
        state["step"] = "address"
        await update.message.reply_text("📍 اكتب عنوان التوصيل بالتفصيل:")
    elif step == "address":
        cart = carts.get(uid, {})
        subtotal = 0
        lines = []
        for pid, qty in cart.items():
            row = db.execute("SELECT name,price FROM products WHERE id=?", (pid,)).fetchone()
            if row:
                name, regular_price = row
                price = effective_price(pid, regular_price)
                subtotal += price * qty
                offer_note = " (عرض)" if price != regular_price else ""
                lines.append(f"{name}{offer_note} × {qty} — {money(price)}")
        if not lines:
            states.pop(uid, None)
            await update.message.reply_text("السلة فارغة، ابدأ من جديد.")
            return
        items = "\n".join(lines)
        discount, coupon_code = coupon_discount(uid, subtotal)
        total = subtotal - discount
        if coupon_code:
            db.execute("UPDATE coupons SET uses=uses+1 WHERE code=?", (coupon_code,))
        cur = db.execute(
            "INSERT INTO orders(customer,phone,address,items,total,user_id,status,subtotal,discount,coupon_code) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (state["customer"], state["phone"], value, items, total, uid, "جديد", subtotal, discount, coupon_code)
        )
        oid = cur.lastrowid
        db.commit()
        await context.bot.send_message(
            ADMIN,
            f"🛍️ طلب جديد #{oid}\n👤 {state['customer']}\n"
            f"📞 {state['phone']}\n📍 {value}\n\n"
            f"{items}\nالمجموع: {money(subtotal)}\n"
            f"🎟️ الكوبون: {coupon_code or 'لا يوجد'}\n"
            f"💸 الخصم: {money(discount)}\n🚚 التوصيل مجاني\n💰 الإجمالي: {money(total)}",
            reply_markup=order_status_keyboard(oid, "جديد")
        )
        carts[uid] = {}
        user_coupons.pop(uid, None)
        states.pop(uid, None)
        await update.message.reply_text(
            f"✅ استلمنا طلبك رقم #{oid}\nشكراً لتسوقك من مجمع القدس ❤️",
            reply_markup=menu()
        )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    states.pop(update.effective_user.id, None)
    context.user_data.pop("ad_mode", None)
    context.user_data.pop("ad_photo", None)
    context.user_data.pop("awaiting_loyverse_csv", None)
    context.user_data.pop("bulk_photo_mode", None)
    await update.message.reply_text("تم إلغاء العملية.")

async def ad_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN:
        return
    states.pop(ADMIN, None)
    context.user_data["ad_mode"] = True
    context.user_data.pop("ad_photo", None)
    await update.message.reply_text("🖼️ إنشاء إعلان جديد\\nأرسل صورة المنتج، وبعدها أرسل السعر بالأرقام.")

async def ad_photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN or not context.user_data.get("ad_mode"):
        return
    await update.message.reply_text("وصلتني الصورة ❤️ دا أحفظها، انتظر لحظة...")
    try:
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        data = BytesIO()
        await tg_file.download_to_memory(out=data)
        context.user_data["ad_photo"] = data.getvalue()
        await update.message.reply_text("تم حفظ الصورة ✅ هسه أرسل السعر فقط، مثلاً 15000")
    except Exception as exc:
        log.exception("Could not download product photo")
        await update.message.reply_text(
            "وصلتني الصورة، لكن تعذّر تنزيلها من تيليجرام بسبب الاتصال. "
            "انتظر قليلاً وأعد إرسال الصورة."
        )


async def ad_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN or not context.user_data.get("ad_mode"):
        return
    price = (update.message.text or "").strip()
    if "ad_photo" not in context.user_data:
        await update.message.reply_text("أرسل صورة المنتج أولاً 📸")
        return
    if len(price) > 40 or not any(ch.isdigit() for ch in price):
        await update.message.reply_text("أرسل السعر بالأرقام، مثلاً 15000")
        return
    try:
        raw_photo = context.user_data["ad_photo"]
        await update.message.reply_text("⏳ دا أجهز وصف المنتج وأنشر الإعلان...")
        description = await describe_product(raw_photo)
        image = make_ad_image(raw_photo, price)
        caption = (
            f"🛍️ {description}\n\n"
            f"💰 السعر: {price} دينار\n"
            "📩 للطلب والحجز: @LLL_54\n"
            "🚚 التوصيل مجاني داخل النجف\n"
            "🛍️ مجمع القدس"
        )
        await context.bot.send_photo(chat_id=CHANNEL_ID, photo=image, caption=caption)
        context.user_data.pop("ad_mode", None)
        context.user_data.pop("ad_photo", None)
        await update.message.reply_text("تم نشر الإعلان بالقناة بنجاح ✅")
    except Exception as exc:
        log.exception("Publishing failed")
        await update.message.reply_text(
            "ما اكتمل نشر الإعلان. تأكد من اتصال تيليجرام وصلاحية البوت بالقناة، "
            "وبعدها جرّب مرة ثانية."
        )


async def error_handler(update, context):
    log.exception("Telegram update error", exc_info=context.error)

async def run_application(app):
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        await asyncio.Event().wait()
    finally:
        if app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()

async def main_async():
    if not ADMIN:
        raise ValueError("Set ADMIN_ID in environment variables")

    orders_app = Application.builder().token(ORDERS_TOKEN).build()
    orders_app.add_handler(CommandHandler("start", start))
    orders_app.add_handler(CommandHandler("admin", admin))
    orders_app.add_handler(CommandHandler("cancel", cancel))
    orders_app.add_handler(CommandHandler("importcsv", import_csv_command))
    orders_app.add_handler(CommandHandler("bulkphotos", bulk_photos_command))
    orders_app.add_handler(CommandHandler("endbulk", end_bulk_command))
    orders_app.add_handler(MessageHandler(filters.Document.ALL, csv_document_handler))
    orders_app.add_handler(MessageHandler(filters.PHOTO, bulk_photo_handler), group=1)
    orders_app.add_handler(CallbackQueryHandler(buttons))
    orders_app.add_handler(
        MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), messages)
    )
    orders_app.add_error_handler(error_handler)

    ads_app = Application.builder().token(ADS_TOKEN).build()
    ads_app.add_handler(CommandHandler("start", ad_start))
    ads_app.add_handler(CommandHandler("ad", ad_start))
    ads_app.add_handler(CommandHandler("cancel", cancel))
    ads_app.add_handler(MessageHandler(filters.PHOTO, ad_photo_handler))
    ads_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ad_text_handler))
    ads_app.add_error_handler(error_handler)

    print("AlQuds Orders bot and AlQuds Ads bot are starting with separate tokens...")
    await asyncio.gather(run_application(orders_app), run_application(ads_app))

def main():
    threading.Thread(
        target=lambda: web.run(
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "10000")),
            use_reloader=False
        ),
        daemon=True
    ).start()
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
