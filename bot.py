import os
import logging

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
        "/models - Mevcut modelleri listele\n\n"
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

    # Telegram mesaj limiti 4096 karakter
    if len(reply) > 4096:
        for i in range(0, len(reply), 4096):
            await update.message.reply_text(reply[i : i + 4096])
    else:
        await update.message.reply_text(reply)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN ayarlanmamış. .env dosyasını kontrol et.")
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY ayarlanmamış. .env dosyasını kontrol et.")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("model", set_model))
    app.add_handler(CommandHandler("models", list_models))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot başlatılıyor...")
    app.run_polling()


if __name__ == "__main__":
    main()
