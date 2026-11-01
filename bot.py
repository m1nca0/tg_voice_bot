import asyncio
import logging
import os
import tempfile
from pathlib import Path
from contextlib import asynccontextmanager

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import FSInputFile, Message
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
MAX_DOWNLOAD_SIZE = int(os.getenv("MAX_DOWNLOAD_MB", "20")) * 1024 * 1024
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
MAX_CONCURRENT_CONVERSIONS = int(os.getenv("MAX_CONCURRENT_CONVERSIONS", "2"))
FFMPEG_TIMEOUT = int(os.getenv("FFMPEG_TIMEOUT_SECONDS", "240"))

AUDIO_EXTENSIONS = {
    ".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".oga", ".opus",
    ".wma", ".aiff", ".aif", ".alac", ".amr", ".ape", ".m4b",
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("voice-converter")
router = Router()
conversion_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CONVERSIONS)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "telegram-voice-converter"})


async def start_http_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    logger.info("HTTP health server listening on port %s", PORT)
    return runner


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Привет! 🎤\n\n"
        "Отправь аудиофайл как музыку или документ — я превращу его в настоящее "
        "голосовое сообщение Telegram.\n\n"
        "Поддерживаются MP3, M4A, WAV, FLAC, AAC, OGG и другие форматы.\n"
        f"Максимальный размер файла: {MAX_DOWNLOAD_SIZE // (1024 * 1024)} МБ.\n\n"
        "Команда /help — подсказка."
    )


@router.message(Command("help"))
async def help_handler(message: Message) -> None:
    await message.answer(
        "Как пользоваться:\n"
        "1. Отправь MP3/WAV/M4A/FLAC или другой аудиофайл.\n"
        "2. Подожди, пока я его обработаю.\n"
        "3. Получи результат в формате голосового сообщения.\n\n"
        "Лучше отправлять длинные треки как файл/документ, а не как сжатое аудио."
    )


async def convert_to_voice(source: Path, destination: Path) -> None:
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-vn", "-map", "0:a:0", "-ac", "1",
        "-ar", "48000", "-c:a", "libopus", "-b:a", "48k",
        "-vbr", "on", "-compression_level", "10", "-application", "voip",
        "-f", "ogg", str(destination),
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=FFMPEG_TIMEOUT)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError("Превышено время конвертации")

    if process.returncode != 0 or not destination.exists() or destination.stat().st_size == 0:
        details = stderr.decode("utf-8", errors="replace")[-1000:]
        logger.warning("FFmpeg error: %s", details)
        raise RuntimeError("FFmpeg не смог обработать аудио")


@router.message(F.audio | F.voice | F.document)
async def audio_handler(message: Message, bot: Bot) -> None:
    media = message.audio or message.voice or message.document
    if media is None:
        return

    filename = getattr(media, "file_name", None) or "audio"
    if message.document:
        mime = (message.document.mime_type or "").lower()
        ext = Path(filename).suffix.lower()
        if not mime.startswith("audio/") and ext not in AUDIO_EXTENSIONS:
            await message.answer("Это не похоже на аудиофайл. Пришли MP3, WAV, M4A, FLAC или другой аудиоформат.")
            return

    if media.file_size and media.file_size > MAX_DOWNLOAD_SIZE:
        await message.answer(f"Файл больше лимита {MAX_DOWNLOAD_SIZE // (1024 * 1024)} МБ. Уменьши его и отправь ещё раз.")
        return

    status = await message.answer("⏳ Файл получен. Готовлю голосовое сообщение…")
    try:
        async with conversion_semaphore:
            tg_file = await bot.get_file(media.file_id)
            if not tg_file.file_path:
                raise RuntimeError("Telegram не вернул путь к файлу")

            with tempfile.TemporaryDirectory(prefix="voice_") as tmp:
                folder = Path(tmp)
                suffix = Path(filename).suffix.lower()
                if suffix not in AUDIO_EXTENSIONS:
                    suffix = ".bin"
                source = folder / f"input{suffix}"
                output = folder / "voice.ogg"

                await bot.download_file(tg_file.file_path, destination=source)
                if source.stat().st_size > MAX_DOWNLOAD_SIZE:
                    raise ValueError("Файл превышает допустимый размер")

                await status.edit_text("🎧 Конвертирую аудио в формат голосового сообщения…")
                await convert_to_voice(source, output)

                if output.stat().st_size > MAX_UPLOAD_SIZE:
                    raise ValueError("После конвертации файл превышает лимит Telegram для голосовых сообщений")

                await status.delete()
                await message.answer_voice(
                    voice=FSInputFile(output, filename="voice.ogg"),
                    caption="Готово 🎤",
                )

    except ValueError as exc:
        await status.edit_text(f"⚠️ {exc}")
    except FileNotFoundError:
        logger.exception("FFmpeg executable is missing")
        await status.edit_text("На сервере не найден FFmpeg. Проверь Dockerfile и перезапусти деплой.")
    except Exception:
        logger.exception("Audio conversion failed")
        try:
            await status.edit_text("Не получилось обработать файл. Попробуй другой файл или формат.")
        except Exception:
            await message.answer("Не получилось обработать файл. Попробуй отправить его ещё раз.")


@router.message()
async def other_messages_handler(message: Message) -> None:
    await message.answer("Отправь аудиофайл, и я верну его в формате голосового сообщения 🎤")


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения")

    runner = await start_http_server()
    bot = Bot(token=BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    try:
        logger.info("Telegram bot polling started")
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await bot.session.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
