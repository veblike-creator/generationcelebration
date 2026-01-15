import asyncio
import logging
import sqlite3
import base64
import os
from io import BytesIO
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from openai import AsyncOpenAI

BOT_TOKEN = os.getenv("BOT_TOKEN", "8594342469:AAEW_7iGUZrwnLGcocOLduPl14eFExMeo-4")
API_KEY = os.getenv("API_KEY", "sk-dd7I7EH6Gtg0zBTDManlSPCLoBN8rQPAatfF57GFebec8vgBHVbnx15JTKMa")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6387718314"))

BASE_URL = "https://api.aitunnel.ru/v1/"
FREE_LIMIT = 3
PREMIUM_LIMIT = 10
DB_FILE = "users.db"

client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class GenState(StatesGroup):
    waiting_prompt = State()

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        is_premium INTEGER DEFAULT 0,
        img_count INTEGER DEFAULT 0,
        last_reset TEXT
    )""")
    conn.commit()
    conn.close()

def get_limit(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    c.execute("SELECT is_premium, img_count, last_reset FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row is None:
        c.execute("INSERT INTO users (user_id, last_reset) VALUES (?, ?)", (user_id, today))
        conn.commit()
        conn.close()
        return FREE_LIMIT, False
    prem, count, reset = row
    if reset != today:
        c.execute("UPDATE users SET img_count = 0, last_reset = ? WHERE user_id = ?", (today, user_id))
        conn.commit()
        conn.close()
        return PREMIUM_LIMIT if prem else FREE_LIMIT, bool(prem)
    limit = PREMIUM_LIMIT if prem else FREE_LIMIT
    conn.close()
    return max(0, limit - count), bool(prem)

def use_limit(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE users SET img_count = img_count + 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Генерация", callback_data="generate")],
        [InlineKeyboardButton(text="⭐ Премиум", callback_data="premium")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")]
    ])

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    init_db()
    await message.answer(
        "🚀 **PhotoGen Bot** - генерация фото!\n\n"
        "📤 *Фото + промпт* = remix\n"
        "✍️ *Текст* = создание с нуля\n\n"
        "⚡ Free: 3/день | Premium: 10/день",
        reply_markup=main_keyboard(),
        parse_mode="MarkdownV2"
    )

@dp.callback_query(F.data == "generate")
async def generate_callback(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "📤 **Отправь фото** (PNG/JPG)\n"
        "💡 Потом промпт: `добавь закат`, `аниме стиль`",
        parse_mode="MarkdownV2"
    )
    await callback.answer()

@dp.callback_query(F.data == "premium")
async def premium_callback(callback: types.CallbackQuery):
    await callback.answer("💎 /set_premium [ID пользователя]", show_alert=True)

@dp.callback_query(F.data == "help")
async def help_callback(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "ℹ️ **Помощь**\n\n"
        "💡 `кот в космосе`\n"
        "`добавь шляпу`\n"
        "`реализм, студийное фото`\n\n"
        "⚙️ Лимиты: Free=3, Premium=10/день",
        parse_mode="MarkdownV2"
    )
    await callback.answer()

@dp.message(F.photo)
async def photo_handler(message: types.Message, state: FSMContext):
    photo_file = BytesIO()
    await message.photo[-1].download(photo_file)
    photo_bytes = photo_file.getvalue()

    if photo_bytes.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = "image/png"
    elif photo_bytes.startswith(b'\xFF\xD8'):
        mime = "image/jpeg"
    else:
        await message.answer("❌ Только PNG/JPG!")
        return

    b64_data = base64.b64encode(photo_bytes).decode()
    image_url = f"data:{mime};base64,{b64_data}"

    await state.update_data(image_url=image_url)
    await message.answer("✅ Фото загружено! 💭 **Промпт:**", parse_mode="MarkdownV2")
    await state.set_state(GenState.waiting_prompt)

@dp.message(GenState.waiting_prompt)
async def generate_image(message: types.Message, state: FSMContext):
    data = await state.get_data()
    image_url = data["image_url"]
    prompt = message.text or "улучши фото"

    user_id = message.from_user.id
    remaining, is_premium = get_limit(user_id)

    if remaining <= 0:
        await message.answer(
            f"❌ **Лимит исчерпан**\n"
            f"Premium ({'✅' if is_premium else '❌'}): 10/день",
            parse_mode="MarkdownV2"
        )
        await state.clear()
        return

    await message.answer("🎨 **Генерирую фото...**")

    try:
        response = await client.chat.completions.create(
            model="gemini-2.5-flash-image-preview",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Перегенерируй фото по инструкции: {prompt}"},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]
            }],
            modalities=["image", "text"]
        )

        assistant_message = response.choices[0].message
        if assistant_message.images:
            img_url = assistant_message.images[0].image_url.url
            if ',' in img_url:
                b64_content = img_url.split(',')[1]
            else:
                b64_content = img_url

            img_bytes = base64.b64decode(b64_content)
            photo = BufferedInputFile(img_bytes, filename="generated.png")

            caption = f"✅ **Готово!**\nОсталось: {remaining - 1}/{PREMIUM_LIMIT if is_premium else FREE_LIMIT}"
            await message.answer_photo(photo, caption=caption, parse_mode="MarkdownV2")
            use_limit(user_id)
        else:
            await message.answer("❌ Попробуй другой промпт. Добавь 'создай фото...'")

    except Exception as e:
        await message.answer(f"🚨 Ошибка API: {str(e)[:100]}")

    await state.clear()

@dp.message(F.text)
async def text_to_image(message: types.Message):
    prompt = message.text
    user_id = message.from_user.id
    remaining, is_premium = get_limit(user_id)

    if remaining <= 0:
        await message.answer("❌ Лимит. /set_premium ID")
        return

    await message.answer("🎨 **Создаю по тексту...**")

    try:
        response = await client.chat.completions.create(
            model="gemini-2.5-flash-image-preview",
            messages=[{"role": "user", "content": f"Создай качественное фото: {prompt}"}],
            modalities=["image", "text"]
        )

        assistant_message = response.choices[0].message
        if assistant_message.images:
            img_url = assistant_message.images[0].image_url.url
            b64_content = img_url.split(',')[1] if ',' in img_url else img_url
            img_bytes = base64.b64decode(b64_content)
            photo = BufferedInputFile(img_bytes, filename="generated.png")

            caption = f"✅ **Готово!**\nОсталось: {remaining - 1}/{PREMIUM_LIMIT if is_premium else FREE_LIMIT}"
            await message.answer_photo(photo, caption=caption, parse_mode="MarkdownV2")
            use_limit(user_id)

    except Exception as e:
        await message.answer(f"🚨 {str(e)[:100]}")

@dp.message(Command("set_premium"))
async def set_premium(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return await message.answer("🚫 Только админ")
    try:
        target_id = int(message.text.split(maxsplit=1)[1])
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE users SET is_premium = 1 WHERE user_id = ?", (target_id,))
        conn.commit()
        conn.close()
        await message.answer(f"✅ Premium выдан: {target_id}")
    except:
        await message.answer("❌ /set_premium 123456789")

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    print("🤖 PhotoGen Bot готов!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
