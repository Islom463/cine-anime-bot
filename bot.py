"""
@ControllerBot nusxasi (faqat o'zbekcha): kanalga post tayyorlash va yuborish.

Imkoniyatlar:
  • Kanal qo'shish (bot va siz admin bo'lishingiz kerak)
  • Post yaratish: matn, rasm, video, GIF, fayl, audio, ovozli xabar...
  • URL tugmalar (bir qatorda bir nechta), reaksiya tugmalari (sanagich bilan)
  • Ovozsiz yuborish, ko'rib chiqish, hozir yuborish yoki rejalashtirish
  • Rejalashtirilgan postlar ro'yxati, statistika

Render Environment:  BOT_TOKEN (majburiy), ADMIN_ID (ixtiyoriy, /backup uchun), TZ_OFFSET_HOURS (standart 5), DB_PATH
"""
import os
import re
import json
import html
import time
import shutil
import asyncio
import logging
import sqlite3
import tempfile
import threading
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, FSInputFile

logging.basicConfig(level=logging.INFO)

DB_PATH = os.getenv("DB_PATH", "controller.db")
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_ID", "") + " " + os.getenv("ADMIN_IDS", ""))}
try:
    TZ = timezone(timedelta(hours=float(os.getenv("TZ_OFFSET_HOURS", "5"))))
except ValueError:
    TZ = timezone(timedelta(hours=5))


# ---------------- Render uchun veb-server va keep-alive ----------------
async def _root_handler(request):
    return web.Response(text="Bot is running 24/7!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", _root_handler)
    app.router.add_get("/ping", _root_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 10000))).start()


def self_ping():
    while True:
        time.sleep(240)
        try:
            url = os.getenv("RENDER_EXTERNAL_URL") or f"http://127.0.0.1:{os.environ.get('PORT', 10000)}"
            urllib.request.urlopen(url, timeout=20).read()
        except Exception:
            pass


threading.Thread(target=self_ping, daemon=True).start()


def detect_token():
    tok_re = r"\d{6,}:[A-Za-z0-9_-]{30,}"
    env = {k: (v or "").strip().strip('"').strip("'") for k, v in os.environ.items()}
    for k in ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "TOKEN"):
        if re.fullmatch(tok_re, env.get(k, "")):
            return env[k]
    for v in env.values():
        if re.fullmatch(tok_re, v):
            return v
    return ""


TOKEN = detect_token()
if not TOKEN:
    while True:
        logging.error("BOT_TOKEN topilmadi! Render Environment'ga BOT_TOKEN ni kiriting.")
        time.sleep(30)

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# ---------------- Baza ----------------
def db():
    return closing(sqlite3.connect(DB_PATH))


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, name TEXT, joined TEXT);
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id INTEGER, title TEXT, username TEXT,
            UNIQUE(owner_id, chat_id));
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id INTEGER, message_id INTEGER,
            rows_json TEXT, reacts_json TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS votes (post_id INTEGER, user_id INTEGER, idx INTEGER, PRIMARY KEY (post_id, user_id));
        CREATE TABLE IF NOT EXISTS scheduled (
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, chat_id INTEGER, channel_title TEXT,
            src_chat INTEGER, src_msg INTEGER, rows_json TEXT, reacts_json TEXT, silent INTEGER, run_at TEXT);
        """)
        c.commit()


init_db()


def utc_str(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def local_now():
    return datetime.now(TZ)


def add_user(user):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users (user_id, name, joined) VALUES (?, ?, ?)",
                  (user.id, user.full_name, utc_str(datetime.now(timezone.utc))))
        c.commit()


def get_channels(owner_id):
    with db() as c:
        return c.execute("SELECT id, chat_id, title, username FROM channels WHERE owner_id = ? ORDER BY id", (owner_id,)).fetchall()


def get_channel(owner_id, cid):
    with db() as c:
        return c.execute("SELECT id, chat_id, title, username FROM channels WHERE owner_id = ? AND id = ?", (owner_id, cid)).fetchone()


def e(s):
    return html.escape(str(s), quote=False)


# ---------------- Tugmalar va parserlar ----------------
URL_PAT = r"(?:https?://|tg://)\S+|(?:t\.me|telegram\.me)/\S+|@\w{4,}"


def normalize_url(u):
    u = u.strip()
    if u.startswith("@"):
        return "https://t.me/" + u[1:]
    if re.match(r"^(t\.me|telegram\.me)/", u):
        return "https://" + u
    return u


def parse_buttons(text):
    """Har qator = bitta qator. Bir qatorda bir nechta tugma: «|» bilan. Format: Matn - havola"""
    rows, errors = [], []
    for line in text.splitlines():
        if not line.strip():
            continue
        row = []
        for part in line.split("|"):
            part = part.strip()
            m = re.match(r"^(.+?)\s*[-–—]\s*(" + URL_PAT + r")$", part)
            if not m:
                errors.append(part)
                continue
            row.append([m.group(1).strip()[:64], normalize_url(m.group(2))])
        if row:
            rows.append(row[:4])
    return rows[:20], errors


def parse_reacts(text):
    items = [t for t in text.split() if t and len(t) <= 10]
    seen, out = set(), []
    for t in items:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:6]


def parse_when(text, now):
    """now: tz-aware. Natija: tz-aware datetime yoki None."""
    t = text.strip().lower()
    try:
        m = re.fullmatch(r"(bugun|ertaga|indinga)?\s*(\d{1,2})[:.](\d{2})", t)
        if m:
            h, mi = int(m.group(2)), int(m.group(3))
            base = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if m.group(1) == "ertaga":
                base += timedelta(days=1)
            elif m.group(1) == "indinga":
                base += timedelta(days=2)
            elif m.group(1) is None and base <= now:
                base += timedelta(days=1)
            return base if base > now else None
        m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s+(\d{1,2})[:.](\d{2})", t)
        if m:
            d, mo, y, h, mi = int(m.group(1)), int(m.group(2)), m.group(3), int(m.group(4)), int(m.group(5))
            y = now.year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
            dt = datetime(y, mo, d, h, mi, tzinfo=now.tzinfo)
            if not m.group(3) and dt <= now:
                dt = dt.replace(year=y + 1)
            return dt if dt > now else None
        m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2})[:.](\d{2})", t)
        if m:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)), tzinfo=now.tzinfo)
            return dt if dt > now else None
    except ValueError:
        return None
    return None


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def post_kb(rows, reacts, counts=None, post_id=None):
    kb = [[InlineKeyboardButton(text=t, url=u) for t, u in row] for row in rows]
    if reacts:
        line = []
        for i, em in enumerate(reacts):
            n = counts[i] if counts else 0
            line.append(InlineKeyboardButton(text=f"{em} {n}" if n else em, callback_data=f"r:{post_id}:{i}" if post_id else "noop"))
        kb.append(line)
    return InlineKeyboardMarkup(inline_keyboard=kb) if kb else None


MENU_ITEMS = {
    "📝 Post yaratish": "new", "📢 Kanallarim": "channels", "⏰ Rejalashtirilgan": "scheduled",
    "📊 Statistika": "stats", "❓ Yordam": "help",
}


def menu_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📝 Post yaratish")],
        [KeyboardButton(text="📢 Kanallarim"), KeyboardButton(text="⏰ Rejalashtirilgan")],
        [KeyboardButton(text="📊 Statistika"), KeyboardButton(text="❓ Yordam")],
    ], resize_keyboard=True)


def editor_kb(d):
    silent = d.get("silent")
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🔗 URL tugmalar", "ed:urls"), btn("😀 Reaksiyalar", "ed:reacts")],
        [btn("🧹 Tugmalarni tozalash", "ed:clear"), btn("🔕 Ovozsiz: ha" if silent else "🔔 Ovozsiz: yo'q", "ed:silent")],
        [btn("📤 Hozir yuborish", "ed:send"), btn("⏰ Rejalashtirish", "ed:sched")],
        [btn("❌ Bekor qilish", "ed:cancel")],
    ])


REACT_PRESETS = ["👍 ❤️ 🔥", "👍 👎", "😂 😮 😢", "❤️ 🔥 👏 🎉", "🤩 😍 🥰"]

HELP = (
    "❓ <b>Yordam</b>\n\n"
    "1️⃣ <b>📢 Kanallarim</b> → kanal qo'shing (botni kanalga admin qiling).\n"
    "2️⃣ <b>📝 Post yaratish</b> → postni yuboring (matn, rasm, video, fayl...).\n"
    "3️⃣ Tugma yoki reaksiya qo'shing, keyin <b>Hozir yuborish</b> yoki <b>Rejalashtirish</b>.\n\n"
    "🔗 <b>URL tugma formati</b> (har qator = bir qator tugma):\n"
    "<code>Kanalga o'tish - https://t.me/kanal</code>\n"
    "<code>Sayt - https://site.uz | Aloqa - @username</code>\n"
    "(bir qatorda bir nechta tugma «|» bilan)\n\n"
    "⏰ <b>Vaqt formati</b>: <code>18:30</code>, <code>ertaga 09:00</code>, <code>25.12 20:00</code>, <code>2026-12-25 20:00</code>\n"
    f"Vaqt zonasi: UTC{TZ.utcoffset(None).total_seconds() / 3600:+.0f}\n\n"
    "⚠️ Rejalashtirilgan post yuborilishi uchun asl xabarni botdan o'chirib yubormang."
)


class S(StatesGroup):
    content = State()
    editor = State()
    urls = State()
    reacts = State()
    when = State()
    channel = State()


def not_menu(m):
    t = m.text
    return not (t and (t in MENU_ITEMS or t.startswith("/")))


def text_not_menu(m):
    return bool(m.text) and not_menu(m)


# ---------------- Yordamchi: editor ----------------
async def safe_delete(chat_id, message_id):
    if message_id:
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass


async def show_editor(chat_id, state, note=None):
    data = await state.get_data()
    d = data.get("draft")
    if not d:
        return await bot.send_message(chat_id, "⚠️ Qoralama topilmadi. «📝 Post yaratish» ni bosing.")
    await safe_delete(chat_id, d.get("preview_id"))
    await safe_delete(chat_id, d.get("panel_id"))
    kb = post_kb(d["rows"], d["reacts"])
    try:
        prev = await bot.copy_message(chat_id=chat_id, from_chat_id=d["src_chat"], message_id=d["src_msg"], reply_markup=kb)
    except Exception as ex:
        logging.warning("preview xatosi: %s", ex)
        d["rows"] = []
        await state.update_data(draft=d)
        await bot.send_message(chat_id, f"❌ Tugmalarda xatolik (havola noto'g'ri bo'lishi mumkin): <code>{e(str(ex)[:150])}</code>\nTugmalar tozalandi.")
        prev = await bot.copy_message(chat_id=chat_id, from_chat_id=d["src_chat"], message_id=d["src_msg"])
    info = f"🛠 <b>Post tahrirlagichi</b>\n🔗 Tugmalar: {sum(len(r) for r in d['rows'])} ta · 😀 Reaksiyalar: {' '.join(d['reacts']) or '—'}"
    if note:
        info = note + "\n" + info
    panel = await bot.send_message(chat_id, info, reply_markup=editor_kb(d))
    d["preview_id"], d["panel_id"] = prev.message_id, panel.message_id
    await state.update_data(draft=d)
    await state.set_state(S.editor)


async def get_draft(state):
    return (await state.get_data()).get("draft")


# ---------------- Yuborish ----------------
async def publish_post(owner_id, chat_id, src_chat, src_msg, rows, reacts, silent):
    with db() as c:
        cur = c.execute("INSERT INTO posts (owner_id, chat_id, rows_json, reacts_json, created) VALUES (?, ?, ?, ?, ?)",
                        (owner_id, chat_id, json.dumps(rows), json.dumps(reacts), utc_str(datetime.now(timezone.utc))))
        post_id = cur.lastrowid
        c.commit()
    try:
        kb = post_kb(rows, reacts, [0] * len(reacts) if reacts else None, post_id)
        res = await bot.copy_message(chat_id=chat_id, from_chat_id=src_chat, message_id=src_msg, reply_markup=kb, disable_notification=bool(silent))
    except Exception:
        with db() as c:
            c.execute("DELETE FROM posts WHERE id = ?", (post_id,))
            c.commit()
        raise
    with db() as c:
        c.execute("UPDATE posts SET message_id = ? WHERE id = ?", (res.message_id, post_id))
        c.commit()
    return res.message_id


def explain(ex):
    s = str(ex).lower()
    if "not enough rights" in s or "have no rights" in s or "administrator" in s or "forbidden" in s:
        return "Botda kanalga post yuborish huquqi yo'q (admin qiling)."
    if "chat not found" in s:
        return "Kanal topilmadi."
    if "button_url_invalid" in s or "url host is empty" in s:
        return "Tugma havolasi noto'g'ri."
    return str(ex)[:150]


# ---------------- Handlerlar: boshlash va menyu ----------------
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    add_user(message.from_user)
    await message.answer(
        "👋 Salom! Men kanalingiz uchun postlar tayyorlayman: tugmalar, reaksiyalar, rejalashtirish.\n\n"
        "Boshlash uchun avval <b>📢 Kanallarim</b> orqali kanal qo'shing.", reply_markup=menu_kb())


@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Bekor qilindi.", reply_markup=menu_kb())


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(HELP)


@dp.message(F.text.in_(set(MENU_ITEMS)))
async def on_menu(message: types.Message, state: FSMContext):
    await state.clear()
    add_user(message.from_user)
    act = MENU_ITEMS[message.text]
    if act == "new":
        await state.set_state(S.content)
        return await message.answer("📝 Kanalga yuboriladigan postni yuboring: matn, rasm, video, GIF, fayl, audio...\n(Bitta xabar. Albom qo'llab-quvvatlanmaydi.)\n\nBekor qilish: /cancel")
    if act == "channels":
        return await show_channels(message)
    if act == "scheduled":
        return await show_scheduled(message)
    if act == "stats":
        return await show_stats(message)
    await message.answer(HELP)


# ---------------- Kanallar ----------------
async def show_channels(message):
    rows = get_channels(message.from_user.id)
    kb = [[btn(f"❌ {t[:30]}", f"chdel:{cid}")] for cid, _, t, _ in rows]
    kb.append([btn("➕ Kanal qo'shish", "chadd")])
    text = "📢 <b>Kanallaringiz:</b>\n" + ("\n".join(f"• {e(t)}" + (f" (@{e(u)})" if u else "") for _, _, t, u in rows) if rows else "Hali kanal yo'q.")
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data == "chadd")
async def cb_chadd(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(S.channel)
    me = await bot.me()
    await callback.message.answer(
        f"➕ <b>Kanal qo'shish</b>\n\n1) @{e(me.username)} ni kanalingizga <b>admin</b> qiling (post yuborish huquqi bilan).\n"
        "2) Kanaldan istalgan xabarni shu yerga <b>forward</b> qiling yoki kanal <b>@username</b> ini yuboring.\n\nBekor qilish: /cancel")


async def resolve_channel(message):
    chat = None
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat is None:
        chat = getattr(message, "forward_from_chat", None)
    if chat is None and message.text:
        ref = message.text.strip()
        m = re.search(r"(?:t\.me|telegram\.me)/([A-Za-z0-9_]{4,})", ref)
        if m:
            ref = "@" + m.group(1)
        elif re.fullmatch(r"-?\d{5,}", ref):
            ref = int(ref)
        elif not ref.startswith("@"):
            raise ValueError("Kanaldan xabar forward qiling yoki @username yuboring.")
        try:
            chat = await bot.get_chat(ref)
        except Exception:
            raise ValueError("Kanal topilmadi. @username to'g'riligini tekshiring.")
    if chat is None:
        raise ValueError("Kanaldan xabar forward qiling yoki @username yuboring.")
    if getattr(chat, "type", "") not in ("channel", "supergroup", "group"):
        raise ValueError("Bu kanal yoki guruh emas.")
    me = await bot.me()
    try:
        member = await bot.get_chat_member(chat.id, me.id)
    except Exception:
        raise ValueError("Bot bu kanalda topilmadi. Avval botni admin qiling.")
    if member.status not in ("administrator", "creator"):
        raise ValueError("Bot kanalda admin emas. Avval botni admin qiling.")
    if getattr(chat, "type", "") == "channel" and getattr(member, "can_post_messages", True) is False:
        raise ValueError("Botda post yuborish huquqi yo'q. Admin huquqlarida «Xabar yuborish» ni yoqing.")
    try:
        um = await bot.get_chat_member(chat.id, message.from_user.id)
    except Exception:
        raise ValueError("Sizning huquqingizni tekshirib bo'lmadi.")
    if um.status not in ("administrator", "creator"):
        raise ValueError("Siz bu kanalda admin emassiz.")
    return chat


@dp.message(S.channel, not_menu)
async def on_channel_input(message: types.Message, state: FSMContext):
    try:
        chat = await resolve_channel(message)
    except ValueError as ex:
        return await message.answer(f"❌ {e(ex)}")
    with db() as c:
        c.execute("INSERT OR REPLACE INTO channels (owner_id, chat_id, title, username) VALUES (?, ?, ?, ?)",
                  (message.from_user.id, chat.id, chat.title or str(chat.id), getattr(chat, "username", None)))
        c.commit()
    await state.clear()
    await message.answer(f"✅ <b>{e(chat.title)}</b> qo'shildi!", reply_markup=menu_kb())


class _Wrap:
    """show_channels uchun: javob callback xabariga, from_user — tugmani bosgan foydalanuvchi."""
    def __init__(self, cb):
        self._m, self.from_user = cb.message, cb.from_user

    async def answer(self, *a, **k):
        return await self._m.answer(*a, **k)


@dp.callback_query(F.data.startswith("chdel:"))
async def cb_chdel(callback: types.CallbackQuery):
    await callback.answer("O'chirildi")
    with db() as c:
        c.execute("DELETE FROM channels WHERE id = ? AND owner_id = ?", (int(callback.data.split(":")[1]), callback.from_user.id))
        c.commit()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await show_channels(_Wrap(callback))


# ---------------- Post yaratish ----------------
@dp.message(S.content, not_menu)
async def on_content(message: types.Message, state: FSMContext):
    d = {"src_chat": message.chat.id, "src_msg": message.message_id, "rows": [], "reacts": [], "silent": False}
    await state.update_data(draft=d)
    await show_editor(message.chat.id, state)


@dp.callback_query(F.data.startswith("ed:"))
async def on_editor(callback: types.CallbackQuery, state: FSMContext):
    action = callback.data.split(":", 1)[1]
    chat_id = callback.message.chat.id
    d = await get_draft(state)
    if not d:
        await callback.answer("Qoralama topilmadi. Qaytadan boshlang.", show_alert=True)
        return
    await callback.answer()
    if action == "urls":
        await state.set_state(S.urls)
        return await callback.message.answer(
            "🔗 Tugmalarni yuboring (har qator = bir qator):\n<code>Matn - https://t.me/kanal</code>\n"
            "<code>Sayt - https://site.uz | Aloqa - @username</code>\n\nBekor qilish: /cancel")
    if action == "reacts":
        kb = [[btn(p, f"rx:{i}")] for i, p in enumerate(REACT_PRESETS)]
        kb.append([btn("✍️ O'zim yozaman", "rx:custom"), btn("🚫 Olib tashlash", "rx:none")])
        return await callback.message.answer("😀 Reaksiyalarni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    if action == "clear":
        d["rows"], d["reacts"] = [], []
        await state.update_data(draft=d)
        return await show_editor(chat_id, state, "🧹 Tugmalar tozalandi.")
    if action == "silent":
        d["silent"] = not d.get("silent")
        await state.update_data(draft=d)
        return await show_editor(chat_id, state)
    if action == "cancel":
        await safe_delete(chat_id, d.get("preview_id"))
        await safe_delete(chat_id, d.get("panel_id"))
        await state.clear()
        return await callback.message.answer("✅ Bekor qilindi.", reply_markup=menu_kb())
    if action in ("send", "sched"):
        channels = get_channels(callback.from_user.id)
        if not channels:
            return await callback.message.answer("📢 Avval kanal qo'shing: «📢 Kanallarim».")
        if action == "sched":
            await state.set_state(S.when)
            return await callback.message.answer(
                "⏰ Yuborish vaqtini yozing:\n<code>18:30</code> · <code>ertaga 09:00</code> · <code>25.12 20:00</code> · <code>2026-12-25 20:00</code>\n\nBekor qilish: /cancel")
        return await ask_channel(callback.message, channels, "pub")


async def ask_channel(message, channels, prefix):
    kb = [[btn(f"📢 {t[:40]}", f"{prefix}:{cid}")] for cid, _, t, _ in channels]
    await message.answer("Qaysi kanalga?", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("rx:"))
async def on_react_pick(callback: types.CallbackQuery, state: FSMContext):
    d = await get_draft(state)
    if not d:
        return await callback.answer("Qoralama topilmadi.", show_alert=True)
    await callback.answer()
    val = callback.data.split(":", 1)[1]
    if val == "custom":
        await state.set_state(S.reacts)
        return await callback.message.answer("✍️ Emojilarni bo'sh joy bilan yuboring (eng ko'pi 6 ta). Masalan: <code>👍 ❤️ 🔥</code>")
    d["reacts"] = [] if val == "none" else parse_reacts(REACT_PRESETS[int(val)])
    await state.update_data(draft=d)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await show_editor(callback.message.chat.id, state)


@dp.message(S.reacts, text_not_menu)
async def on_reacts_input(message: types.Message, state: FSMContext):
    d = await get_draft(state)
    reacts = parse_reacts(message.text)
    if not reacts:
        return await message.answer("❌ Emoji topilmadi. Masalan: <code>👍 ❤️ 🔥</code>")
    d["reacts"] = reacts
    await state.update_data(draft=d)
    await show_editor(message.chat.id, state)


@dp.message(S.urls, text_not_menu)
async def on_urls_input(message: types.Message, state: FSMContext):
    d = await get_draft(state)
    rows, errors = parse_buttons(message.text)
    if not rows:
        return await message.answer("❌ Format noto'g'ri. Namuna:\n<code>Kanalga o'tish - https://t.me/kanal</code>")
    d["rows"] = (d["rows"] + rows)[:20]
    await state.update_data(draft=d)
    note = f"⚠️ Tushunilmagan qismlar: {e('; '.join(errors)[:100])}" if errors else None
    await show_editor(message.chat.id, state, note)


@dp.message(S.when, text_not_menu)
async def on_when_input(message: types.Message, state: FSMContext):
    when = parse_when(message.text, local_now())
    if not when:
        return await message.answer("❌ Vaqt noto'g'ri yoki o'tib ketgan. Namuna: <code>18:30</code> yoki <code>25.12 20:00</code>")
    d = await get_draft(state)
    d["when"] = utc_str(when)
    d["when_label"] = when.strftime("%d.%m.%Y %H:%M")
    await state.update_data(draft=d)
    await state.set_state(S.editor)
    channels = get_channels(message.from_user.id)
    if len(channels) == 1:
        return await schedule_to(message, state, channels[0], message.from_user.id)
    await ask_channel(message, channels, "sch")


async def schedule_to(message, state, channel, owner_id):
    d = await get_draft(state)
    if not d or not d.get("when"):
        return await message.answer("⚠️ Qoralama topilmadi.")
    cid, chat_id, title, _ = channel
    with db() as c:
        c.execute("INSERT INTO scheduled (owner_id, chat_id, channel_title, src_chat, src_msg, rows_json, reacts_json, silent, run_at) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (owner_id, chat_id, title, d["src_chat"], d["src_msg"], json.dumps(d["rows"]), json.dumps(d["reacts"]), int(bool(d.get("silent"))), d["when"]))
        c.commit()
    label = d.get("when_label")
    await state.clear()
    await message.answer(f"⏰ Rejalashtirildi: <b>{e(title)}</b> — {label}", reply_markup=menu_kb())


@dp.callback_query(F.data.startswith("sch:"))
async def cb_schedule_channel(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    ch = get_channel(callback.from_user.id, int(callback.data.split(":")[1]))
    if not ch:
        return await callback.message.answer("⚠️ Kanal topilmadi.")
    await schedule_to(callback.message, state, ch, callback.from_user.id)


@dp.callback_query(F.data.startswith("pub:"))
async def cb_publish(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    d = await get_draft(state)
    ch = get_channel(callback.from_user.id, int(callback.data.split(":")[1]))
    if not d or not ch:
        return await callback.message.answer("⚠️ Qoralama yoki kanal topilmadi.")
    try:
        await publish_post(callback.from_user.id, ch[1], d["src_chat"], d["src_msg"], d["rows"], d["reacts"], d.get("silent"))
    except Exception as ex:
        logging.warning("publish xatosi: %s", ex)
        return await callback.message.answer(f"❌ Yuborilmadi: {e(explain(ex))}")
    await safe_delete(callback.message.chat.id, d.get("preview_id"))
    await safe_delete(callback.message.chat.id, d.get("panel_id"))
    await state.clear()
    await callback.message.answer(f"✅ Post <b>{e(ch[2])}</b> kanaliga yuborildi!", reply_markup=menu_kb())


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: types.CallbackQuery):
    await callback.answer("Bu ko'rib chiqish rejimi")


# ---------------- Reaksiyalar (kanaldagi postda) ----------------
def vote_counts(c, post_id, n):
    counts = [0] * n
    for idx, cnt in c.execute("SELECT idx, COUNT(*) FROM votes WHERE post_id = ? GROUP BY idx", (post_id,)):
        if 0 <= idx < n:
            counts[idx] = cnt
    return counts


@dp.callback_query(F.data.startswith("r:"))
async def cb_react(callback: types.CallbackQuery):
    try:
        _, pid, idx = callback.data.split(":")
        pid, idx = int(pid), int(idx)
    except ValueError:
        return await callback.answer()
    with db() as c:
        row = c.execute("SELECT rows_json, reacts_json FROM posts WHERE id = ?", (pid,)).fetchone()
        if not row:
            return await callback.answer("Post topilmadi")
        rows, reacts = json.loads(row[0]), json.loads(row[1])
        if not (0 <= idx < len(reacts)):
            return await callback.answer()
        cur = c.execute("SELECT idx FROM votes WHERE post_id = ? AND user_id = ?", (pid, callback.from_user.id)).fetchone()
        if cur and cur[0] == idx:
            c.execute("DELETE FROM votes WHERE post_id = ? AND user_id = ?", (pid, callback.from_user.id))
            toast = "Olib tashlandi"
        else:
            c.execute("INSERT OR REPLACE INTO votes (post_id, user_id, idx) VALUES (?, ?, ?)", (pid, callback.from_user.id, idx))
            toast = reacts[idx]
        c.commit()
        counts = vote_counts(c, pid, len(reacts))
    await callback.answer(toast)
    try:
        await callback.message.edit_reply_markup(reply_markup=post_kb(rows, reacts, counts, pid))
    except Exception:
        pass


# ---------------- Rejalashtirilganlar va statistika ----------------
async def show_scheduled(message):
    with db() as c:
        rows = c.execute("SELECT id, channel_title, run_at FROM scheduled WHERE owner_id = ? ORDER BY run_at", (message.from_user.id,)).fetchall()
    if not rows:
        return await message.answer("⏰ Rejalashtirilgan postlar yo'q.")
    lines, kb = ["⏰ <b>Rejalashtirilgan postlar:</b>"], []
    for sid, title, run_at in rows:
        dt = datetime.strptime(run_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(TZ)
        lines.append(f"• {dt.strftime('%d.%m %H:%M')} — {e(title)}")
        kb.append([btn(f"❌ {dt.strftime('%d.%m %H:%M')} {title[:20]}", f"schdel:{sid}")])
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@dp.callback_query(F.data.startswith("schdel:"))
async def cb_schdel(callback: types.CallbackQuery):
    await callback.answer("Bekor qilindi")
    with db() as c:
        c.execute("DELETE FROM scheduled WHERE id = ? AND owner_id = ?", (int(callback.data.split(":")[1]), callback.from_user.id))
        c.commit()
    try:
        await callback.message.delete()
    except Exception:
        pass


async def show_stats(message):
    uid = message.from_user.id
    channels = get_channels(uid)
    with db() as c:
        posts = c.execute("SELECT COUNT(*) FROM posts WHERE owner_id = ? AND message_id IS NOT NULL", (uid,)).fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM scheduled WHERE owner_id = ?", (uid,)).fetchone()[0]
        votes = c.execute("SELECT COUNT(*) FROM votes WHERE post_id IN (SELECT id FROM posts WHERE owner_id = ?)", (uid,)).fetchone()[0]
    lines = ["📊 <b>Statistika</b>\n", f"📢 Kanallar: {len(channels)}", f"📝 Yuborilgan postlar: {posts}", f"⏰ Rejalashtirilgan: {pending}", f"👍 Jami reaksiyalar: {votes}"]
    for _, chat_id, title, _ in channels:
        try:
            n = await bot.get_chat_member_count(chat_id)
            lines.append(f"• {e(title)} — 👥 {n:,}".replace(",", " "))
        except Exception:
            lines.append(f"• {e(title)} — 👥 ?")
    await message.answer("\n".join(lines))


async def scheduled_worker():
    while True:
        try:
            with db() as c:
                due = c.execute(
                    "SELECT id, owner_id, chat_id, channel_title, src_chat, src_msg, rows_json, reacts_json, silent FROM scheduled "
                    "WHERE run_at <= ? ORDER BY run_at LIMIT 20", (utc_str(datetime.now(timezone.utc)),)).fetchall()
            for sid, owner, chat_id, title, src_chat, src_msg, rj, rcj, silent in due:
                with db() as c:
                    c.execute("DELETE FROM scheduled WHERE id = ?", (sid,))
                    c.commit()
                try:
                    await publish_post(owner, chat_id, src_chat, src_msg, json.loads(rj), json.loads(rcj), silent)
                    msg = f"✅ Rejalashtirilgan post <b>{e(title)}</b> kanaliga yuborildi."
                except Exception as ex:
                    logging.warning("scheduled xatosi: %s", ex)
                    msg = f"❌ Rejalashtirilgan post <b>{e(title)}</b> kanaliga yuborilmadi: {e(explain(ex))}"
                try:
                    await bot.send_message(owner, msg)
                except Exception:
                    pass
        except Exception:
            logging.exception("scheduled_worker")
        await asyncio.sleep(15)


# ---------------- Zaxira nusxa (admin) ----------------
restore_wait = set()


@dp.message(Command("backup"))
async def cmd_backup(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    path = os.path.join(tempfile.gettempdir(), "controller_backup.db")
    with db() as src, closing(sqlite3.connect(path)) as dst:
        src.backup(dst)
    try:
        await message.answer_document(FSInputFile(path), caption="💾 Baza nusxasi")
    finally:
        os.remove(path)


@dp.message(Command("restore"))
async def cmd_restore(message: types.Message, state: FSMContext):
    if message.from_user.id in ADMIN_IDS:
        await state.clear()
        restore_wait.add(message.from_user.id)
        await message.answer("📥 Endi .db faylni yuboring.")


@dp.message(F.document)
async def on_document(message: types.Message):
    uid = message.from_user.id
    if uid in restore_wait and uid in ADMIN_IDS:
        restore_wait.discard(uid)
        tmp = os.path.join(tempfile.gettempdir(), f"restore_{uid}.db")
        try:
            await bot.download(message.document.file_id, destination=tmp)
            with closing(sqlite3.connect(tmp)) as c:
                names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"channels", "posts"} <= names:
                return await message.answer("❌ Bu fayl mos emas.")
            shutil.copyfile(tmp, DB_PATH + ".new")
            os.replace(DB_PATH + ".new", DB_PATH)
            init_db()
            await message.answer("✅ Baza tiklandi!")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


async def main():
    asyncio.create_task(start_web_server())
    asyncio.create_task(scheduled_worker())
    while True:
        try:
            print("Bot polling boshlandi...")
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot, drop_pending_updates=True)
        except Exception as ex:
            logging.error(f"Polling xatoligi: {ex}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
