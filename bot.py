import os
import json
import logging
from datetime import time, timezone
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import anthropic

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Kullanıcı bazında konuşma geçmişi
conversations: dict[int, list[dict]] = {}

MODEL_OPTIONS = {
    "opus": "claude-opus-4-20250514",
    "sonnet": "claude-sonnet-4-20250514",
    "haiku": "claude-haiku-4-5-20251001",
}
DEFAULT_MODEL = "sonnet"

# Rutinler (kalıcı, JSON'da saklanır)
# Yapı: { "<user_id>": [ {"id": int, "time": "HH:MM", "prompt": str,
#                          "model": str, "chat_id": int} , ... ] }
ROUTINES_FILE = Path(os.getenv("ROUTINES_FILE", "routines.json"))
routines: dict[str, list[dict]] = {}
JOB_PREFIX = "routine:"


def load_routines() -> None:
    global routines
    if not ROUTINES_FILE.exists():
        routines = {}
        return
    try:
        data = json.loads(ROUTINES_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            routines = {str(k): list(v) for k, v in data.items()}
        else:
            routines = {}
    except Exception as e:
        logger.error("Rutinler yüklenemedi: %s", e)
        routines = {}


def save_routines() -> None:
    try:
        ROUTINES_FILE.write_text(
            json.dumps(routines, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error("Rutinler kaydedilemedi: %s", e)


def next_routine_id(user_id: int) -> int:
    items = routines.get(str(user_id), [])
    return (max((r["id"] for r in items), default=0)) + 1


def parse_hhmm(text: str) -> time | None:
    parts = text.split(":")
    if len(parts) != 2:
        return None
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return time(hour=hh, minute=mm, tzinfo=timezone.utc)


def get_user_model(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("model", DEFAULT_MODEL)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    conversations.pop(user_id, None)
    await update.message.reply_text(
        "Merhaba! Ben Claude ile çalışan bir asistanım.\n\n"
        "Komutlar:\n"
        "/model <opus|sonnet|haiku> - Model seç\n"
        "/clear - Konuşma geçmişini temizle\n"
        "/models - Mevcut modelleri listele\n"
        "/routine_add HH:MM <istek> - Günlük rutin ekle (UTC)\n"
        "/routines - Rutinleri listele\n"
        "/routine_remove <id> - Rutini sil\n\n"
        "Bana bir şey yaz, Claude'a sorayım!"
    )


async def set_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        current = get_user_model(context)
        await update.message.reply_text(
            f"Şu anki model: {current}\n"
            f"Kullanım: /model <{'|'.join(MODEL_OPTIONS.keys())}>"
        )
        return

    choice = context.args[0].lower()
    if choice not in MODEL_OPTIONS:
        await update.message.reply_text(
            f"Geçersiz model. Seçenekler: {', '.join(MODEL_OPTIONS.keys())}"
        )
        return

    context.user_data["model"] = choice
    await update.message.reply_text(f"Model değiştirildi: {choice} ({MODEL_OPTIONS[choice]})")


async def list_models(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = get_user_model(context)
    lines = ["Mevcut modeller:\n"]
    for key, model_id in MODEL_OPTIONS.items():
        marker = " ✓" if key == current else ""
        lines.append(f"  • {key} → {model_id}{marker}")
    await update.message.reply_text("\n".join(lines))


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    conversations.pop(user_id, None)
    await update.message.reply_text("Konuşma geçmişi temizlendi.")


async def send_long(message_send, text: str) -> None:
    if len(text) > 4096:
        for i in range(0, len(text), 4096):
            await message_send(text[i : i + 4096])
    else:
        await message_send(text)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user_text = update.message.text

    if user_id not in conversations:
        conversations[user_id] = []

    conversations[user_id].append({"role": "user", "content": user_text})

    # Son 20 mesajı tut
    if len(conversations[user_id]) > 20:
        conversations[user_id] = conversations[user_id][-20:]

    model_key = get_user_model(context)
    model_id = MODEL_OPTIONS[model_key]

    await update.message.chat.send_action("typing")

    success = False
    try:
        response = claude_client.messages.create(
            model=model_id,
            max_tokens=4096,
            messages=conversations[user_id],
        )
        reply = response.content[0].text
        success = True
    except anthropic.APIError as e:
        logger.error("Claude API hatası: %s", e)
        reply = f"Claude API hatası: {e.message}"
        conversations[user_id].pop()
    except Exception as e:
        logger.error("Beklenmeyen hata: %s", e)
        reply = "Bir hata oluştu, lütfen tekrar dene."
        conversations[user_id].pop()

    if success:
        conversations[user_id].append({"role": "assistant", "content": reply})

    await send_long(update.message.reply_text, reply)


# --- Rutinler ---------------------------------------------------------------


async def run_routine_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue tarafından zamanlanmış bir rutinin tetiklenmesi."""
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    prompt = data.get("prompt")
    model_key = data.get("model", DEFAULT_MODEL)
    model_id = MODEL_OPTIONS.get(model_key, MODEL_OPTIONS[DEFAULT_MODEL])
    routine_id = data.get("id")

    if not chat_id or not prompt:
        logger.warning("Geçersiz rutin verisi: %s", data)
        return

    try:
        response = claude_client.messages.create(
            model=model_id,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        reply = response.content[0].text
    except anthropic.APIError as e:
        logger.error("Rutin Claude API hatası: %s", e)
        reply = f"Rutin çalıştırılamadı (Claude API hatası): {e.message}"
    except Exception as e:
        logger.error("Rutin beklenmeyen hata: %s", e)
        reply = "Rutin çalıştırılırken bir hata oluştu."

    header = f"⏰ Rutin #{routine_id}\n\n" if routine_id is not None else ""
    text = header + reply

    async def _send(chunk: str) -> None:
        await context.bot.send_message(chat_id=chat_id, text=chunk)

    await send_long(_send, text)


def schedule_routine(application: Application, user_id: int, routine: dict) -> None:
    t = parse_hhmm(routine["time"])
    if t is None:
        logger.warning("Rutin için geçersiz zaman, atlandı: %s", routine)
        return
    name = f"{JOB_PREFIX}{user_id}:{routine['id']}"
    application.job_queue.run_daily(
        run_routine_job,
        time=t,
        name=name,
        data={
            "chat_id": routine["chat_id"],
            "prompt": routine["prompt"],
            "model": routine.get("model", DEFAULT_MODEL),
            "id": routine["id"],
        },
    )


def unschedule_routine(application: Application, user_id: int, routine_id: int) -> None:
    name = f"{JOB_PREFIX}{user_id}:{routine_id}"
    for job in application.job_queue.get_jobs_by_name(name):
        job.schedule_removal()


async def routine_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.application.job_queue is None:
        await update.message.reply_text(
            "JobQueue mevcut değil. Lütfen 'python-telegram-bot[job-queue]' kurun."
        )
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Kullanım: /routine_add HH:MM <istek>\n"
            "Örnek: /routine_add 09:00 Bugün için kısa bir motivasyon mesajı yaz.\n"
            "Not: Saat UTC olarak yorumlanır."
        )
        return

    when = parse_hhmm(context.args[0])
    if when is None:
        await update.message.reply_text(
            "Geçersiz saat formatı. HH:MM kullanın (örn. 09:00, 21:30)."
        )
        return

    prompt = " ".join(context.args[1:]).strip()
    if not prompt:
        await update.message.reply_text("İstek boş olamaz.")
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    model_key = get_user_model(context)

    routine = {
        "id": next_routine_id(user_id),
        "time": context.args[0],
        "prompt": prompt,
        "model": model_key,
        "chat_id": chat_id,
    }
    routines.setdefault(str(user_id), []).append(routine)
    save_routines()
    schedule_routine(context.application, user_id, routine)

    await update.message.reply_text(
        f"Rutin eklendi (#{routine['id']}): her gün {routine['time']} UTC.\n"
        f"Model: {model_key}\n"
        f"İstek: {prompt}"
    )


async def routine_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    items = routines.get(str(user_id), [])
    if not items:
        await update.message.reply_text(
            "Henüz rutin yok. /routine_add HH:MM <istek> ile ekleyin."
        )
        return

    lines = ["Rutinleriniz (UTC):\n"]
    for r in sorted(items, key=lambda x: (x["time"], x["id"])):
        prompt_preview = r["prompt"]
        if len(prompt_preview) > 80:
            prompt_preview = prompt_preview[:77] + "..."
        lines.append(
            f"  #{r['id']} • {r['time']} • [{r.get('model', DEFAULT_MODEL)}] {prompt_preview}"
        )
    await update.message.reply_text("\n".join(lines))


async def routine_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Kullanım: /routine_remove <id>")
        return
    try:
        rid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Geçersiz id. Sayı bekleniyor.")
        return

    user_id = update.effective_user.id
    items = routines.get(str(user_id), [])
    new_items = [r for r in items if r["id"] != rid]
    if len(new_items) == len(items):
        await update.message.reply_text(f"#{rid} numaralı rutin bulunamadı.")
        return

    routines[str(user_id)] = new_items
    if not new_items:
        routines.pop(str(user_id), None)
    save_routines()
    unschedule_routine(context.application, user_id, rid)
    await update.message.reply_text(f"Rutin #{rid} silindi.")


async def schedule_all_routines(application: Application) -> None:
    if application.job_queue is None:
        logger.warning(
            "JobQueue yok; rutinler yüklendi ama zamanlanamayacak. "
            "'python-telegram-bot[job-queue]' kurun."
        )
        return
    count = 0
    for user_id_str, items in routines.items():
        try:
            user_id = int(user_id_str)
        except ValueError:
            continue
        for routine in items:
            schedule_routine(application, user_id, routine)
            count += 1
    if count:
        logger.info("%d rutin zamanlandı.", count)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN ayarlanmamış. .env dosyasını kontrol et.")
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY ayarlanmamış. .env dosyasını kontrol et.")

    load_routines()

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(schedule_all_routines)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("model", set_model))
    app.add_handler(CommandHandler("models", list_models))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(CommandHandler("routine_add", routine_add))
    app.add_handler(CommandHandler("routines", routine_list))
    app.add_handler(CommandHandler("routine_remove", routine_remove))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot başlatılıyor...")
    app.run_polling()


if __name__ == "__main__":
    main()
