import asyncio
import logging
import os
import sqlite3
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# Tokenni Render muhitidan avtomatik o'qiydi
TOKEN = os.getenv("TOKEN")

router = Router()
bot = Bot(token=TOKEN)
dp = Dispatcher()


# --- BAZA BILAN ISHLASH (SQLite) ---
def init_db():
  conn = sqlite3.connect("bot_database.db")
  cursor = conn.cursor()
  cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            language TEXT
        )
    """)
  conn.commit()
  conn.close()


def set_user_lang(user_id, lang):
  conn = sqlite3.connect("bot_database.db")
  cursor = conn.cursor()
  cursor.execute(
      "INSERT OR REPLACE INTO users (user_id, language) VALUES (?, ?)",
      (user_id, lang),
  )
  conn.commit()
  conn.close()


def get_user_lang(user_id):
  conn = sqlite3.connect("bot_database.db")
  cursor = conn.cursor()
  cursor.execute("SELECT language FROM users WHERE user_id = ?", (user_id,))
  row = cursor.fetchone()
  conn.close()
  return row[0] if row else None


# --- TARJIMALAR LUG'ATI ---
TRANSLATIONS = {
    "uz": {
        "welcome": "Assalomu alaykum! Iltimos, tilni tanlang:",
        "main_menu": "🏠 Asosiy menyu. Kerakli amalni tanlang:",
        "create_post": "📝 Post yaratish",
        "settings": "⚙️ Sozlamalar",
        "stats": "📊 Statistika",
        "post_prompt": (
            "Iltimos, kanalga yuboriladigan post matnini yoki rasmini yuboring:"
        ),
        "btn_prompt": (
            "Endi tugmalarni yuboring.\nFormat: `Matn - https://t.me/...`\nHar"
            " birini yangi qatordan yozing."
        ),
        "success_post": "✅ Post muvaffaqiyatli tayyorlandi!",
    },
    "ru": {
        "welcome": "Здравствуйте! Пожалуйста, выберите язык:",
        "main_menu": "🏠 Главное меню. Выберите действие:",
        "create_post": "📝 Создать пост",
        "settings": "⚙️ Настройки",
        "stats": "📊 Статистика",
        "post_prompt": (
            "Пожалуйста, отправьте текст или медиа для публикации:"
        ),
        "btn_prompt": (
            "Теперь отправьте кнопки.\nФормат: `Текст - https://t.me/...`"
        ),
        "success_post": "✅ Пост успешно готов!",
    },
    "en": {
        "welcome": "Hello! Please choose your language:",
        "main_menu": "🏠 Main menu. Choose an action:",
        "create_post": "📝 Create Post",
        "settings": "⚙️ Settings",
        "stats": "📊 Statistics",
        "post_prompt": "Please send the text or media for the post:",
        "btn_prompt": (
            "Now send the buttons.\nFormat: `Text - https://t.me/...`"
        ),
        "success_post": "✅ Post is successfully ready!",
    },
    "uz_cyrl": {
        "welcome": "Ассалому алайкум! Илтимос, тилни танланг:",
        "main_menu": "🏠 Асосий меню. Керакли амални танланг:",
        "create_post": "📝 Пост яратиш",
        "settings": "⚙️ Созламалар",
        "stats": "📊 Статистика",
        "post_prompt": (
            "Илтимос, каналга юбориладиган пост матнини ёки расмини юборинг:"
        ),
        "btn_prompt": (
            "Энди тугмаларни юборинг.\nФормат: `Матн - https://t.me/...`"
        ),
        "success_post": "✅ Пост муваффақиятли тайёрланди!",
    },
}

lang_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang_uz"),
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
        ],
        [
            InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en"),
            InlineKeyboardButton(
                text="🇺🇿 Ўзбекча (Крил)", callback_data="lang_uz_cyrl"
            ),
        ],
    ]
)


class PostState(StatesGroup):
  waiting_for_content = State()
  waiting_for_buttons = State()


@router.message(CommandStart())
async def cmd_start(message: Message):
  user_id = message.from_user.id
  lang = get_user_lang(user_id)

  if not lang:
    await message.answer(
        "Assalomu alaykum! Iltimos, tilni tanlang:\nЗдравствуйте! Выберите"
        " язык:",
        reply_markup=lang_kb,
    )
  else:
    t = TRANSLATIONS[lang]
    menu_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t["create_post"], callback_data="menu_create"
                )
            ],
            [
                InlineKeyboardButton(
                    text=t["settings"], callback_data="menu_settings"
                ),
                InlineKeyboardButton(
                    text=t["stats"], callback_data="menu_stats"
                ),
            ],
        ]
    )
    await message.answer(t["main_menu"], reply_markup=menu_kb)


@router.callback_query(F.data.startswith("lang_"))
async def process_language(callback: CallbackQuery):
  user_id = callback.from_user.id
  lang = callback.data.split("_")[1]
  if lang == "cyrl":
    lang = "uz_cyrl"

  set_user_lang(user_id, lang)
  t = TRANSLATIONS[lang]

  menu_kb = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text=t["create_post"], callback_data="menu_create"
              )
          ],
          [
              InlineKeyboardButton(
                  text=t["settings"], callback_data="menu_settings"
              ),
              InlineKeyboardButton(
                  text=t["stats"], callback_data="menu_stats"
              ),
          ],
      ]
  )

  await callback.message.edit_text(t["main_menu"], reply_markup=menu_kb)
  await callback.answer()


@router.callback_query(F.data == "menu_create")
async def start_create_post(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  lang = get_user_lang(user_id) or "uz"
  t = TRANSLATIONS[lang]

  await state.set_state(PostState.waiting_for_content)
  await callback.message.answer(t["post_prompt"])
  await callback.answer()


@router.message(PostState.waiting_for_content)
async def receive_post_content(message: Message, state: FSMContext):
  user_id = message.from_user.id
  lang = get_user_lang(user_id) or "uz"
  t = TRANSLATIONS[lang]

  await state.update_data(content_message=message)
  await state.set_state(PostState.waiting_for_buttons)
  await message.answer(t["btn_prompt"], parse_mode="Markdown")


@router.message(PostState.waiting_for_buttons)
async def receive_post_buttons(message: Message, state: FSMContext):
  user_id = message.from_user.id
  lang = get_user_lang(user_id) or "uz"
  t = TRANSLATIONS[lang]

  data = await state.get_data()
  original_msg: Message = data.get("content_message")

  buttons = []
  for line in message.text.split("\n"):
    if "-" in line:
      parts = line.split("-", 1)
      btn_text = parts[0].strip()
      btn_url = parts[1].strip()
      buttons.append(InlineKeyboardButton(text=btn_text, url=btn_url))

  keyboard = (
      InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
  )

  if original_msg.photo:
    await message.answer_photo(
        photo=original_msg.photo[-1].file_id,
        caption=original_msg.caption,
        reply_markup=keyboard,
    )
  elif original_msg.text:
    await message.answer(text=original_msg.text, reply_markup=keyboard)

  await message.answer(t["success_post"])
  await state.clear()


# Render port talabini bajarish uchun kichik veb-server (Bot tinimsiz 24/7 ishlashi uchun)
async def handle(request):
  return web.Response(text="Bot is running 24/7!")


async def web_server():
  app = web.Application()
  app.router.add_get("/", handle)
  runner = web.AppRunner(app)
  await runner.setup()
  port = int(os.getenv("PORT", 10000))
  site = web.TCPSite(runner, "0.0.0.0", port)
  await site.start()


async def main():
  init_db()
  dp.include_router(router)
  await bot.delete_webhook(drop_pending_updates=True)

  # Veb-server va botni birgalikda ishga tushirish
  await web_server()
  await dp.start_polling(bot)


if __name__ == "__main__":
  logging.basicConfig(level=logging.INFO)
  asyncio.run(main())
