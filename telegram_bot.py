import asyncio
import logging
import os
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.types import Message, Update
from aiohttp import web  # Добавлен импорт для веб-сервера
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
AIAI_API_KEY = os.getenv("AIAI_API_KEY", "").strip()
AIAI_BASE_URL = os.getenv("AIAI_BASE_URL", "https://api.aiai.by/v1").strip()
CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini").strip()

WHISPER_API_KEY = os.getenv("WHISPER_API_KEY", "").strip() or AIAI_API_KEY
WHISPER_BASE_URL = os.getenv("WHISPER_BASE_URL", "").strip() or AIAI_BASE_URL
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1").strip()

DATA_DIR = Path(os.getenv("DATA_DIR", "data/posts"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

if not TELEGRAM_TOKEN:
    raise RuntimeError("Не заполнен TELEGRAM_TOKEN в .env")
if not AIAI_API_KEY:
    raise RuntimeError("Не заполнен AIAI_API_KEY в .env")

chat_client = AsyncOpenAI(api_key=AIAI_API_KEY, base_url=AIAI_BASE_URL)
whisper_client = AsyncOpenAI(api_key=WHISPER_API_KEY, base_url=WHISPER_BASE_URL)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

SYSTEM_PROMPT = """Ты — помощник продавца рыбного магазина Fish Soligorsk.
Ты ведёшь «умный блокнот»: текущий пост-прайс хранится в тексте.
Правила:
- Верни ТОЛЬКО обновлённый текст поста, без объяснений, без markdown-кода.
- Сохраняй структуру, эмодзи, цены, единицы измерения и категории.
- Если просят убрать товар — удали его.
- Если просят изменить количество, цену или наличие — измени.
- Если присылают новый прайс или голосовое — сделай красивый читаемый пост для клиентов Viber/Instagram.
- Не выдумывай цены и наличие, которых нет во входных данных. Если данных не хватает — оставь как было.
- Пиши по-русски, кратко, аккуратно, без лишней воды.
"""


def post_path(chat_id: int) -> Path:
    return DATA_DIR / f"{chat_id}.txt"


def load_post(chat_id: int) -> str:
    path = post_path(chat_id)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def save_post(chat_id: int, text: str) -> None:
    path = post_path(chat_id)
    path.write_text(text.strip() + "\n", encoding="utf-8")


async def send_long(message: Message, text: str) -> None:
    text = (text or "").strip()
    if not text:
        text = "(пусто)"
    # Telegram limit ~4096 символов
    for i in range(0, len(text), 4000):
        await message.answer(text[i:i + 4000])


async def update_post(message: Message, user_text: str) -> None:
    chat_id = message.chat.id
    current_post = load_post(chat_id)

    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    user_content = (
        f"ТЕКУЩИЙ ПОСТ:\n{current_post or '(пока пусто)'}\n\n"
        f"НОВОЕ СООБЩЕНИЕ / ПРАЙС / ПРАВКА:\n{user_text}\n\n"
        f"Обнови текущий пост и верни только новый текст поста."
    )

    try:
        response = await chat_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
        )
        new_post = (response.choices[0].message.content or "").strip()
        if not new_post:
            raise RuntimeError("LLM вернул пустой текст")

        save_post(chat_id, new_post)
        await send_long(message, new_post)

    except Exception as e:
        logging.exception("Ошибка обновления поста")
        await message.answer(
            "❌ Не смог обновить пост. Проверьте логи бота.\n"
            f"Краткая ошибка: {str(e)[:500]}"
        )


async def transcribe_media(message: Message) -> str:
    media = message.voice or message.audio
    if not media:
        raise ValueError("Нет voice/audio")

    suffix = ".ogg"
    if message.audio and message.audio.file_name:
        suffix = Path(message.audio.file_name).suffix or ".mp3"

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        tmp_path = Path(f.name)

    try:
        await bot.download(media, destination=tmp_path)

        with tmp_path.open("rb") as audio_file:
            result = await whisper_client.audio.transcriptions.create(
                model=WHISPER_MODEL,
                file=audio_file,
            )

        return (result.text or "").strip()
    finally:
        tmp_path.unlink(missing_ok=True)


@dp.message(F.text == "/start")
async def cmd_start(message: Message):
    await message.answer(
        "👋 Я бот-блокнот Fish Soligorsk.\n\n"
        "Пришлите голосовое или текст с прайсом — я сделаю пост.\n"
        "Потом говорите правки: «мойву убери», «икры осталось 3 банки».\n\n"
        "Команды:\n"
        "/show — показать текущий пост\n"
        "/reset — очистить память\n"
        "/help — помощь"
    )


@dp.message(F.text == "/help")
async def cmd_help(message: Message):
    await message.answer(
        "Как пользоваться:\n"
        "1. Отправьте голосовое или текст с товарами/ценами.\n"
        "2. Я сохраню готовый пост в память.\n"
        "3. Пришлите правку голосом или текстом — я перепишу пост.\n\n"
        "Команды:\n"
        "/show — текущий пост\n"
        "/reset — очистить память"
    )


@dp.message(F.text == "/show")
async def cmd_show(message: Message):
    post = load_post(message.chat.id)
    if not post:
        await message.answer("Память пуста. Пришлите первый прайс или голосовое.")
        return
    await send_long(message, post)


@dp.message(F.text == "/reset")
async def cmd_reset(message: Message):
    path = post_path(message.chat.id)
    if path.exists():
        path.unlink()
    await message.answer("🧹 Память очищена. Можно начинать новый пост.")


@dp.message(F.voice | F.audio)
async def handle_voice(message: Message):
    await message.answer("🎙 Слушаю и распознаю…")

    try:
        text = await transcribe_media(message)
    except Exception as e:
        logging.exception("Ошибка распознавания")
        await message.answer(
            "❌ Не удалось распознать аудио. Проверьте настройки Whisper.\n"
            f"Краткая ошибка: {str(e)[:500]}"
        )
        return

    if not text:
        await message.answer("Не разобрал речь. Попробуйте ещё раз.")
        return

    preview = text if len(text) <= 800 else text[:800] + "…"
    await message.answer(f"📝 Распознал:\n{preview}")
    await update_post(message, text)


@dp.message(F.text)
async def handle_text(message: Message):
    if message.text.startswith("/"):
        return
    await update_post(message, message.text)


# ======================= НОВЫЙ БЛОК ДЛЯ WEBHOOK =======================

async def handle_webhook(request: web.Request) -> web.Response:
    """Обработчик входящих запросов от Telegram."""
    try:
        update_data = await request.json()
        update = Update.model_validate(update_data, context={"bot": bot})
        await dp.feed_update(bot, update)
        return web.Response(text="OK")
    except Exception as e:
        logging.error(f"Ошибка при обработке вебхука: {e}")
        return web.Response(status=500)

async def healthcheck(request: web.Request) -> web.Response:
    """Эндпоинт для проверки, что сервис жив (для UptimeRobot)."""
    return web.Response(text="OK")

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    # Render автоматически подставляет сюда ваш публичный URL (например, https://xxx.onrender.com)
    webhook_base_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL")
    
    if not webhook_base_url:
        logging.error("Не задан RENDER_EXTERNAL_URL или WEBHOOK_URL. Вебхук не установлен.")
        return

    webhook_path = "/webhook"
    webhook_full_url = f"{webhook_base_url}{webhook_path}"

    # Устанавливаем вебхук в Telegram
    await bot.set_webhook(webhook_full_url)
    logging.info(f"Вебхук успешно установлен на: {webhook_full_url}")

    # Настраиваем веб-сервер aiohttp
    app = web.Application()
    app.router.add_post(webhook_path, handle_webhook)
    app.router.add_get("/", healthcheck) # Этот адрес будет пинговать UptimeRobot

    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render передает порт через переменную окружения PORT
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {port}. Ожидаю сообщения...")
    
    # Бесконечное ожидание (сервер работает, пока Render не выключит контейнер)
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())