import asyncio
import logging
import sqlite3
import base64
import re
from io import BytesIO
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from openai import AsyncOpenAI

BOT_TOKEN = "8594342469:AAEW_7iGUZrwnLGcocOLduPl14eFExMeo-4"
API_KEY = "sk-aitunnel-iP4KByEtsVaxNJoAP6O1jmPgoqAHGxiD"
ADMIN_ID = 6387718314

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
    c.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, is_premium INTEGER DEFAULT 0, img_count INTEGER DEFAULT 0, last_reset TEXT)")
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

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Генерация", callback_data="gen")],
        [InlineKeyboardButton(text="⭐ Premium", callback_data="prem")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")]
    ])

@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    init_db()
    await msg.answer("🚀 PhotoGen Bot - AI фото!\n📤 Фото+текст=remix | Текст=txt2img\nFree:3/d | Premium:10/d", reply_markup=main_kb())

@dp.callback_query(F.data == "gen")
async def gen_cb(cb: types.CallbackQuery):
    await cb.message.edit_text("📤 Отправь фото, потом промпт (добавь закат)")
    await cb.answer()

@dp.callback_query(F.data == "prem")
async def prem_cb(cb: types.CallbackQuery):
    await cb.answer("Premium: /set_premium ID")

@dp.callback_query(F.data == "help")
async def help_cb(cb: types.CallbackQuery):
    await cb.message.edit_text("Примеры: `кот в космосе`, `добавь шляпу`, `реализм`\nFree=3 Premium=10/d")
    await cb.answer()

@dp.message(F.photo)
async def photo_handler(msg: types.Message, state: FSMContext):
    photo_file = BytesIO()
    await msg.photo[-1].download(destination_file=photo_file)
    photo_bytes = photo_file.getvalue()

    mime = "image/png" if photo_bytes.startswith(b'\x89PNG') else "image/jpeg"
    b64 = base64.b64encode(photo_bytes).decode()
    image_data = f"data:{mime};base64,{b64}"

    await state.update_data(image=image_data)
    await msg.answer("✅ Фото готово! Промпт:")
    await state.set_state(GenState.waiting_prompt)

@dp.message(GenState.waiting_prompt)
async def generate_photo(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    image_b64 = data["image"]
    prompt = msg.text or "улучши фото"

    user_id = msg.from_user.id
    remaining, is_prem = get_limit(user_id)
    if remaining <= 0:
        await msg.answer("❌ Лимит. /set_premium ID")
        await state.clear()
        return

    await msg.answer("🎨 Генерирую...")

    try:
        resp = await client.chat.completions.create(
            model="gemini-2.5-flash-image-preview",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Перегенерируй фото: {prompt}"},
                    {"type": "image_url", "image_url": {"url": image_b64}}
                ]
            }],
            modalities=["image", "text"]
        )

        # ИСПРАВЛЕНО: Парсим images ИЛИ base64 из content
        message = resp.choices[0].message
        img_bytes = None

        # 1. Если есть images поле
        if hasattr(message, 'images') and message.images:
            img_url = message.images[0].image_url.url
            b64_img = img_url.split(",")[1]
            img_bytes = base64.b64decode(b64_img)

        # 2. Парсим base64 из текста (data:image/png;base64,...)
        if not img_bytes:
            content = message.content or ""
            img_match = re.search(r'data:image/[a-z]+;base64,([A-Za-z0-9+/=]+)', content)
            if img_match:
                b64_img = img_match.group(1)
                img_bytes = base64.b64decode(b64_img)

        if img_bytes:
            photo_file = BufferedInputFile(img_bytes, "result.png")
            await msg.answer_photo(photo_file, caption=f"✅ Готово! Осталось: {remaining-1}")
            use_limit(user_id)
        else:
            await msg.answer(f"❌ Изображение не найдено.\nОтвет: {message.content[:200]}")

    except Exception as e:
        await msg.answer(f"🚨 Ошибка: {str(e)}")

    await state.clear()

@dp.message(F.text)
async def text_generate(msg: types.Message):
    prompt = msg.text
    user_id = msg.from_user.id
    remaining, is_prem = get_limit(user_id)
    if remaining <= 0:
        await msg.answer("❌ Лимит!")
        return

    await msg.answer("🎨 Создаю...")

    try:
        resp = await client.chat.completions.create(
            model="gemini-2.5-flash-image-preview",
            messages=[{"role": "user", "content": f"Создай фото: {prompt}"}],
            modalities=["image", "text"]
        )

        message = resp.choices[0].message
        img_bytes = None

        if hasattr(message, 'images') and message.images:
            img_url = message.images[0].image_url.url
            b64_img = img_url.split(",")[1]
            img_bytes = base64.b64decode(b64_img)

        if not img_bytes:
            content = message.content or ""
            img_match = re.search(r'data:image/[a-z]+;base64,([A-Za-z0-9+/=]+)', content)
            if img_match:
                b64_img = img_match.group(1)
                img_bytes = base64.b64decode(b64_img)

        if img_bytes:
            photo_file = BufferedInputFile(img_bytes, "result.png")
            await msg.answer_photo(photo_file, caption=f"✅ Готово! Осталось: {remaining-1}")
            use_limit(user_id)
        else:
            await msg.answer(f"❌ Ошибка. Ответ: {message.content[:200]}")

    except Exception as e:
        await msg.answer(f"🚨 {str(e)}")

@dp.message(Command("set_premium"))
async def admin_cmd(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(msg.text.split()[1])
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("UPDATE users SET is_premium = 1 WHERE user_id = ?", (uid,))
        conn.commit()
        conn.close()
        await msg.answer(f"✅ Premium: {uid}")
    except:
        await msg.answer("❌ /set_premium 123456")

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    print("🤖 Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
