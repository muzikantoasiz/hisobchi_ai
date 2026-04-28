#!/usr/bin/env python3
"""
Hisobchi AI Bot — Aqlli CRM
Guruh xabarlarini AI orqali tahlil qiladi:
- Lid aniqlash (raqam + ism + brend)
- Reply tahlil (natija: sotildi/sotilmadi/noaniq/ko'tarmadi)
- Eslatma belgilash
"""

import logging
import os
import re
import sqlite3
import json
import asyncio
from datetime import datetime, timedelta
from google import genai
from google.genai import types
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ======================== SOZLAMALAR ========================
BOT_TOKEN      = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_KEY")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "oazimov")
ADMIN_CHAT_ID  = os.environ.get("ADMIN_CHAT_ID", "")
DB_FILE        = "hisobchi_ai.db"

# Gemini config
ai_client_gemini = genai.Client(api_key=OPENAI_API_KEY)

# Holat konstantalari
HOLAT_YANGI      = "yangi"
HOLAT_SOTILDI    = "sotildi"
HOLAT_SOTILMADI  = "sotilmadi"
HOLAT_NOANIQ     = "noaniq"
HOLAT_KOTAR_MADI = "ko'tarmadi"

# ======================== DATABASE ========================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS lidlar (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            telefon       TEXT NOT NULL,
            ism           TEXT,
            brend         TEXT,
            izoh_asl      TEXT,
            holat         TEXT DEFAULT 'yangi',
            admin_izoh    TEXT,
            eslatma_vaqt  TEXT,
            chat_id       TEXT,
            msg_id        INTEGER,
            yuborgan      TEXT,
            yaratilgan    TEXT,
            yangilangan   TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS eslatmalar (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            lid_id     INTEGER,
            vaqt       TEXT,
            yuborildi  INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

def db():
    return sqlite3.connect(DB_FILE)

def lid_saqlash(telefon, ism, brend, izoh, chat_id, msg_id, yuborgan):
    conn = db()
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''
        INSERT INTO lidlar (telefon,ism,brend,izoh_asl,holat,chat_id,msg_id,yuborgan,yaratilgan,yangilangan)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    ''', (telefon, ism, brend, izoh, HOLAT_YANGI, str(chat_id), msg_id, yuborgan, now, now))
    lid_id = c.lastrowid
    conn.commit()
    conn.close()
    return lid_id

def lid_yangilash(lid_id, holat, admin_izoh=None, eslatma_vaqt=None):
    conn = db()
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''UPDATE lidlar SET holat=?, admin_izoh=?, eslatma_vaqt=?, yangilangan=? WHERE id=?''',
              (holat, admin_izoh, eslatma_vaqt, now, lid_id))
    conn.commit()
    conn.close()

def lid_by_id(lid_id):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT * FROM lidlar WHERE id=?", (lid_id,))
    row = c.fetchone()
    conn.close()
    return row

def lid_by_telefon(telefon):
    conn = db()
    c = conn.cursor()
    clean = re.sub(r'[\s\-\(\)]', '', telefon)
    c.execute("""
        SELECT * FROM lidlar 
        WHERE REPLACE(REPLACE(REPLACE(telefon,' ',''),'-',''),'(','') LIKE ?
        ORDER BY id DESC LIMIT 1
    """, (f"%{clean[-9:]}%",))
    row = c.fetchone()
    conn.close()
    return row

def barcha_lidlar(holat=None):
    conn = db()
    c = conn.cursor()
    if holat:
        c.execute("SELECT * FROM lidlar WHERE holat=? ORDER BY yaratilgan DESC", (holat,))
    else:
        c.execute("SELECT * FROM lidlar ORDER BY yaratilgan DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def eslatma_qo_sh(lid_id, vaqt_str):
    conn = db()
    c = conn.cursor()
    c.execute("INSERT INTO eslatmalar (lid_id, vaqt) VALUES (?,?)", (lid_id, vaqt_str))
    conn.commit()
    conn.close()

def yuborilmagan_eslatmalar():
    conn = db()
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute('''
        SELECT e.id, e.lid_id, l.telefon, l.ism, l.brend, l.admin_izoh
        FROM eslatmalar e JOIN lidlar l ON e.lid_id=l.id
        WHERE e.yuborildi=0 AND e.vaqt<=?
    ''', (now,))
    rows = c.fetchall()
    conn.close()
    return rows

def eslatma_belgilash(eslatma_id):
    conn = db()
    c = conn.cursor()
    c.execute("UPDATE eslatmalar SET yuborildi=1 WHERE id=?", (eslatma_id,))
    conn.commit()
    conn.close()

# ======================== AI TAHLIL ========================

TIZIM_PROMPT = """Sen O'zbek tilidagi savdo CRM botining AI yordamchisissan.
Guruh xabarlarini tahlil qilib, quyidagi JSON formatda javob ber.

VAZIFA 1 — Yangi xabar (reply emas):
Xabarda telefon raqam bor va savdo lidi bo'lishi mumkin bo'lsa:
{
  "tur": "lid",
  "telefon": "+998XXXXXXXXX",
  "ism": "Ismi yoki bo'sh",
  "brend": "Brend/mahsulot nomi yoki bo'sh",
  "izoh": "Qo'shimcha ma'lumot"
}

Agar lid emas (oddiy suhbat, savol, buyruq, izoh va h.k.) bo'lsa:
{"tur": "bekor"}

VAZIFA 2 — Reply xabar (boshqa xabarga javob):
Admin natija yozgan bo'lsa:
{
  "tur": "natija",
  "holat": "sotildi" | "sotilmadi" | "ko'tarmadi" | "noaniq",
  "eslatma": "2soat" | "ertaga 14:00" | null,
  "izoh": "Admin yozgan izoh matni"
}

Agar reply natija emas bo'lsa: {"tur": "bekor"}

QOIDALAR:
- Telefon raqamni +998 formatiga keltir
- O'zbek va rus tilidagi xabarlarni tush
- "sotildi", "sold", "sotdi", "deal" → holat: sotildi
- "sotilmadi", "olmadi", "rad", "reject" → holat: sotilmadi  
- "ko'tarmadi", "kotarmadi", "javob bermadi", "trubka" → holat: ko'tarmadi
- "noaniq", "keyinroq", "ertaga", "qayta ring", "callback" → holat: noaniq
- Faqat JSON qaytarish, boshqa hech narsa yozma"""

async def ai_tahlil(xabar_matni: str, reply_matni: str = None) -> dict:
    """Xabarni Gemini orqali tahlil qiladi"""
    try:
        if reply_matni:
            user_content = f"Asl xabar: {reply_matni}\n\nAdmin replyi: {xabar_matni}"
        else:
            user_content = f"Xabar: {xabar_matni}"

        prompt = TIZIM_PROMPT + "\n\n" + user_content

        response = await asyncio.to_thread(
            ai_client_gemini.models.generate_content,
            model="models/gemini-2.5-flash-lite",
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=300)
        )

        javob = response.text.strip()
        logger.info(f"AI xom javob: {javob[:200]}")
        # JSON tozalash
        javob = re.sub(r'```json\s*', '', javob)
        javob = re.sub(r'```\s*', '', javob)
        javob = javob.strip()
        # Faqat { } orasidagi JSON ni olish
        json_match = re.search(r'\{.*?\}', javob, re.DOTALL)
        if json_match:
            javob = json_match.group()
        result = json.loads(javob)
        logger.info(f"AI tahlil: {result}")
        return result

    except json.JSONDecodeError as e:
        logger.error(f"AI JSON xato: {e} | Javob: {javob[:100]}")
        return {"tur": "bekor"}
    except Exception as e:
        logger.error(f"AI xato: {e}")
        return {"tur": "bekor"}

def eslatma_parse(matn: str) -> datetime | None:
    """Eslatma vaqtini parse qiladi"""
    if not matn:
        return None
    matn = matn.strip().lower()
    now = datetime.now()
    try:
        m = re.match(r'^(\d+)\s*soat$', matn)
        if m: return now + timedelta(hours=int(m.group(1)))
        m = re.match(r'^(\d+)\s*(min|daqiqa)$', matn)
        if m: return now + timedelta(minutes=int(m.group(1)))
        m = re.match(r'^(\d+)\s*kun$', matn)
        if m: return now + timedelta(days=int(m.group(1)))
        m = re.match(r'^ertaga\s+(\d{1,2}):(\d{2})$', matn)
        if m:
            t = now + timedelta(days=1)
            return t.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0)
        m = re.match(r'^bugun\s+(\d{1,2}):(\d{2})$', matn)
        if m:
            return now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0)
        m = re.match(r'^(\d{1,2}):(\d{2})$', matn)
        if m:
            t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0)
            if t < now: t += timedelta(days=1)
            return t
        return datetime.strptime(matn, "%Y-%m-%d %H:%M")
    except:
        return None

def format_pul(n): return f"{n:,.0f} so'm"

def lid_karta(lid):
    emoji = {"yangi":"🆕","sotildi":"✅","sotilmadi":"❌","noaniq":"⏳","ko'tarmadi":"📵"}.get(lid[5],"❓")
    return (
        f"{emoji} *Lid #{lid[0]}*\n"
        f"📞 `{lid[1]}`\n"
        f"👤 {lid[2] or '—'}  🏷 {lid[3] or '—'}\n"
        f"📊 Holat: *{lid[5]}*\n"
        f"🗒 {lid[6] or '—'}\n"
        f"📅 {lid[11] or '—'}"
    )

# ======================== HANDLERS ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Hisobchi AI Bot* — Aqlli CRM\n\n"
        "🤖 Men guruh xabarlarini AI orqali tahlil qilaman.\n"
        "Raqam ko'rsam — lid sifatida saqlayman.\n"
        "Admin reply qilsa — natijani qayd qilaman.\n\n"
        "📋 /lidlar /yangi /noaniq /sotildi /royxat",
        parse_mode="Markdown"
    )

async def xabar_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Barcha xabarlarni AI orqali tahlil qiladi"""
    msg = update.message
    if not msg or not msg.text:
        return

    logger.info(f"Xabar: {msg.chat.type} | {msg.text[:60]}")

    # ===== REPLY xabar =====
    if msg.reply_to_message:
        replied_text = msg.reply_to_message.text or ""

        # Bot xabaridan lid_id topish
        lid_id = None
        lid_match = re.search(r'Lid #(\d+)', replied_text)
        if lid_match:
            lid_id = int(lid_match.group(1))

        # AI tahlil
        tahlil = await ai_tahlil(msg.text, replied_text)

        if tahlil.get("tur") == "natija" and lid_id:
            holat    = tahlil.get("holat", HOLAT_NOANIQ)
            izoh     = tahlil.get("izoh", msg.text)
            eslatma_str = tahlil.get("eslatma")

            eslatma_dt  = eslatma_parse(eslatma_str) if eslatma_str else None
            eslatma_vaqt = eslatma_dt.strftime("%Y-%m-%d %H:%M:%S") if eslatma_dt else None

            lid_yangilash(lid_id, holat, izoh, eslatma_vaqt)
            lid = lid_by_id(lid_id)

            if eslatma_dt and holat == HOLAT_NOANIQ:
                eslatma_qo_sh(lid_id, eslatma_vaqt)

            holat_emoji = {"sotildi":"✅","sotilmadi":"❌","ko'tarmadi":"📵","noaniq":"⏳"}.get(holat,"📋")
            eslatma_text = f"\n⏰ Eslatma: *{eslatma_dt.strftime('%d-%m-%Y %H:%M')}*" if eslatma_dt else ""

            await msg.reply_text(
                f"{holat_emoji} *Lid #{lid_id}* yangilandi!\n\n"
                f"📞 `{lid[1]}`\n"
                f"👤 {lid[2] or '—'}  🏷 {lid[3] or '—'}\n"
                f"📊 *{holat}*\n"
                f"🗒 {izoh}{eslatma_text}",
                parse_mode="Markdown"
            )

    # ===== ODDIY xabar — lid tekshirish =====
    tahlil = await ai_tahlil(msg.text)

    logger.info(f"AI tur: {tahlil.get('tur')}")
    if tahlil.get("tur") != "lid":
        logger.info("Lid emas, o'tkazildi")
        return

    telefon = tahlil.get("telefon", "")
    ism     = tahlil.get("ism", "")
    brend   = tahlil.get("brend", "")
    izoh    = tahlil.get("izoh", "")

    logger.info(f"Lid ma'lumot: tel={telefon} ism={ism} brend={brend}")

    if not telefon:
        logger.info("Telefon yo'q, o'tkazildi")
        return

    # Takroriy tekshirish
    mavjud = lid_by_telefon(telefon)
    logger.info(f"Mavjud lid: {mavjud}")
    if mavjud and mavjud[5] in (HOLAT_YANGI, HOLAT_NOANIQ, HOLAT_KOTAR_MADI):
        await msg.reply_text(
            f"⚠️ Bu raqam allaqachon ro'yxatda!\n"
            f"📞 `{telefon}` — *{mavjud[5]}* (Lid #{mavjud[0]})",
            parse_mode="Markdown"
        )
        return

    # Yangi lid saqlash
    logger.info(f"Lid saqlanmoqda: {telefon}")
    yuborgan = f"@{msg.from_user.username}" if msg.from_user.username else msg.from_user.full_name
    lid_id = lid_saqlash(telefon, ism, brend, msg.text, msg.chat_id, msg.message_id, yuborgan)
    logger.info(f"Lid saqlandi: #{lid_id}")
    lid = lid_by_id(lid_id)

    keyboard = [[
        InlineKeyboardButton("✅ Sotildi",    callback_data=f"n_sotildi_{lid_id}"),
        InlineKeyboardButton("❌ Sotilmadi",  callback_data=f"n_sotilmadi_{lid_id}"),
    ],[
        InlineKeyboardButton("📵 Ko'tarmadi", callback_data=f"n_kotarmadi_{lid_id}"),
        InlineKeyboardButton("⏳ Noaniq",     callback_data=f"n_noaniq_{lid_id}"),
    ]]

    await msg.reply_text(
        f"🆕 *Yangi Lid #{lid_id}* qayd qilindi!\n\n"
        f"📞 `{telefon}`\n"
        f"👤 {ism or '—'}  🏷 {brend or '—'}\n"
        f"💬 {izoh or msg.text[:100]}\n"
        f"👤 Yuborgan: {yuborgan}\n\n"
        f"@{ADMIN_USERNAME} — qo'ng'iroq qiling! 📞",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

    if ADMIN_CHAT_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🔔 *Yangi Lid #{lid_id}!*\n\n📞 `{telefon}`\n👤 {ism or '—'}  🏷 {brend or '—'}\n\nQo'ng'iroq qiling!",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Admin xabar xato: {e}")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    m = re.match(r'^n_(\w+)_(\d+)$', query.data)
    if not m:
        return

    holat_key = m.group(1)
    lid_id    = int(m.group(2))

    holat_map = {
        "sotildi":   HOLAT_SOTILDI,
        "sotilmadi": HOLAT_SOTILMADI,
        "kotarmadi": HOLAT_KOTAR_MADI,
        "noaniq":    HOLAT_NOANIQ,
    }
    holat = holat_map.get(holat_key)
    if not holat:
        return

    if holat == HOLAT_NOANIQ:
        context.chat_data["noaniq_lid"] = lid_id
        await query.message.reply_text(
            f"⏳ Lid #{lid_id} noaniq belgilandi.\n\n"
            f"Eslatma vaqtini yozing:\n"
            f"`2soat` | `30min` | `ertaga 14:00` | `bugun 18:30` | `1kun`",
            parse_mode="Markdown"
        )
        return

    kim = f"@{query.from_user.username}" if query.from_user.username else query.from_user.full_name
    lid_yangilash(lid_id, holat, f"Tugma: {holat} — {kim}")

    emoji = {"sotildi":"✅","sotilmadi":"❌","kotarmadi":"📵"}.get(holat_key,"📋")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    await query.message.reply_text(
        f"{emoji} Lid #{lid_id} — *{holat}*\n👤 {kim}",
        parse_mode="Markdown"
    )

async def noaniq_vaqt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lid_id = context.chat_data.get("noaniq_lid")
    if not lid_id or update.message.reply_to_message:
        return

    matn = update.message.text.strip().lower()
    dt = eslatma_parse(matn)
    if not dt:
        return

    vaqt_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    lid_yangilash(lid_id, HOLAT_NOANIQ, f"Noaniq — {matn}", vaqt_str)
    eslatma_qo_sh(lid_id, vaqt_str)
    context.chat_data.pop("noaniq_lid", None)

    await update.message.reply_text(
        f"⏳ Lid #{lid_id} — noaniq\n⏰ Eslatma: *{dt.strftime('%d-%m-%Y %H:%M')}*",
        parse_mode="Markdown"
    )

# ======================== RO'YXAT BUYRUQLARI ========================
async def cmd_lidlar(update, context):
    lidlar = barcha_lidlar()
    if not lidlar:
        await update.message.reply_text("📭 Lidlar yo'q.")
        return
    emoji_map = {"yangi":"🆕","sotildi":"✅","sotilmadi":"❌","noaniq":"⏳","ko'tarmadi":"📵"}
    matn = f"📋 *Barcha lidlar* — {len(lidlar)} ta\n\n"
    for l in lidlar[:20]:
        e = emoji_map.get(l[5],"❓")
        matn += f"{e} #{l[0]} `{l[1]}` {l[2] or ''} {l[3] or ''}\n"
    await update.message.reply_text(matn, parse_mode="Markdown")

async def cmd_yangi(update, context):
    lidlar = barcha_lidlar(HOLAT_YANGI)
    if not lidlar:
        await update.message.reply_text("📭 Yangi lid yo'q.")
        return
    matn = f"🆕 *Yangi lidlar* — {len(lidlar)} ta\n\n"
    for l in lidlar[:15]:
        matn += f"#{l[0]} `{l[1]}` {l[2] or ''} {l[3] or ''} — {l[11][:10]}\n"
    await update.message.reply_text(matn, parse_mode="Markdown")

async def cmd_noaniq(update, context):
    lidlar = barcha_lidlar(HOLAT_NOANIQ)
    if not lidlar:
        await update.message.reply_text("📭 Noaniq lid yo'q.")
        return
    matn = f"⏳ *Noaniq (bog'lanish kerak)* — {len(lidlar)} ta\n\n"
    for l in lidlar[:15]:
        eslatma = f" ⏰{l[7][:16]}" if l[7] else ""
        matn += f"#{l[0]} `{l[1]}` {l[2] or ''}{eslatma}\n"
    await update.message.reply_text(matn, parse_mode="Markdown")

async def cmd_sotildi(update, context):
    lidlar = barcha_lidlar(HOLAT_SOTILDI)
    matn = f"✅ *Sotildi* — {len(lidlar)} ta\n\n"
    for l in lidlar[:15]:
        matn += f"#{l[0]} `{l[1]}` {l[2] or ''} {l[3] or ''}\n"
    await update.message.reply_text(matn or "📭 Yo'q", parse_mode="Markdown")

async def cmd_royxat(update, context):
    barcha = barcha_lidlar()
    yangi     = sum(1 for l in barcha if l[5]==HOLAT_YANGI)
    noaniq    = sum(1 for l in barcha if l[5]==HOLAT_NOANIQ)
    sotildi   = sum(1 for l in barcha if l[5]==HOLAT_SOTILDI)
    sotilmadi = sum(1 for l in barcha if l[5]==HOLAT_SOTILMADI)
    kotarmadi = sum(1 for l in barcha if l[5]==HOLAT_KOTAR_MADI)

    await update.message.reply_text(
        f"📊 *Umumiy ro'yxat*\n\n"
        f"🆕 Yangi: {yangi}\n"
        f"⏳ Noaniq: {noaniq}\n"
        f"✅ Sotildi: {sotildi}\n"
        f"❌ Sotilmadi: {sotilmadi}\n"
        f"📵 Ko'tarmadi: {kotarmadi}\n"
        f"📦 Jami: {len(barcha)}",
        parse_mode="Markdown"
    )

# ======================== ESLATMA CHECKER ========================
async def eslatma_tekshir(context: ContextTypes.DEFAULT_TYPE):
    for e in yuborilmagan_eslatmalar():
        eslatma_id, lid_id, telefon, ism, brend, izoh = e
        try:
            if ADMIN_CHAT_ID:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"🔔 *Eslatma!* Lid #{lid_id}\n\n"
                        f"📞 `{telefon}`\n"
                        f"👤 {ism or '—'}  🏷 {brend or '—'}\n"
                        f"🗒 {izoh or '—'}\n\n"
                        f"@{ADMIN_USERNAME} — bog'laning!"
                    ),
                    parse_mode="Markdown"
                )
            eslatma_belgilash(eslatma_id)
        except Exception as err:
            logger.error(f"Eslatma xato: {err}")

# ======================== MAIN ========================
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("❌ BOT_TOKEN topilmadi!")
        return
    if OPENAI_API_KEY == "YOUR_GEMINI_KEY":
        print("❌ OPENAI_API_KEY topilmadi!")
        print("   export GEMINI_API_KEY='AIza...' qiling")
        return

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("lidlar",    cmd_lidlar))
    app.add_handler(CommandHandler("yangi",     cmd_yangi))
    app.add_handler(CommandHandler("noaniq",    cmd_noaniq))
    app.add_handler(CommandHandler("sotildi",   cmd_sotildi))
    app.add_handler(CommandHandler("royxat",    cmd_royxat))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Barcha text xabarlar — AI tahlil qiladi
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, xabar_handler))

    app.job_queue.run_repeating(eslatma_tekshir, interval=60, first=10)

    print("🚀 Hisobchi AI Bot ishga tushdi!")
    print(f"👤 Admin: @{ADMIN_USERNAME}")
    print(f"🤖 AI: Gemini Flash")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
