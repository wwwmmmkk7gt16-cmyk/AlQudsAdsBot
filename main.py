import os
import logging
import threading
from io import BytesIO

from flask import Flask
from PIL import Image, ImageDraw, ImageFont
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("alquds")

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@MajmaAlQuds")

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN in Render environment variables")

web = Flask(__name__)

@web.get("/")
def home():
    return "AlQuds Ads Bot is running"

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "هلا بيك بمساعد إعلانات مجمع القدس 🌟\n"
        "أرسل صورة المنتج، وبعدها أرسل السعر برسالة مثل: 15000"
    )

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo = update.message.photo[-1]
    file = await photo.get_file()
    data = BytesIO()
    await file.download_to_memory(out=data)
    context.user_data["photo"] = data.getvalue()
    await update.message.reply_text("وصلت الصورة ❤️ هسه أرسل السعر فقط، مثلاً 15000")

def make_caption(price):
    return (
        "✨ وصل حديثاً إلى مجمع القدس ✨\n\n"
        "اختيار مميز وبسعر مناسب، لا تفوّت الفرصة!\n\n"
        f"💰 السعر: {price} دينار\n"
        "📩 للطلب والاستفسار راسلونا\n"
        "🛍️ مجمع القدس"
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = (update.message.text or "").strip()
    if "photo" not in context.user_data:
        await update.message.reply_text("أرسل صورة المنتج أولاً 📸")
        return
    if len(price) > 40 or not any(ch.isdigit() for ch in price):
        await update.message.reply_text("أرسل السعر بالأرقام، مثلاً 15000")
        return
    try:
        image = make_ad_image(context.user_data["photo"], price)
        await context.bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=image,
            caption=make_caption(price)
        )
        context.user_data.clear()
        await update.message.reply_text("تم نشر الإعلان بالقناة بنجاح ✅")
    except Exception:
        log.exception("Publishing failed")
        await update.message.reply_text(
            "ما كدرت أنشر الإعلان. تأكد أن البوت مشرف بالقناة وعنده صلاحية نشر الرسائل."
        )

async def error_handler(update, context):
    log.exception("Telegram update error", exc_info=context.error)

def run_web():
    port = int(os.environ.get("PORT", "10000"))
    web.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    # Flask runs in a helper thread; Telegram polling runs on the main thread.
    threading.Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)
    app.run_polling(drop_pending_updates=True)
