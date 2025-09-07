import os
import asyncio
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorClient
from openai import OpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ADMIN_IDS = {int(i) for i in os.environ.get("ADMIN_IDS", "").split(",") if i}

db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client.tg_anal
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def format_checklist(items):
    lines = ["Чеклист:"]
    for i, item in enumerate(items, 1):
        mark = "[x]" if item.get("done") else "[ ]"
        lines.append(f"{i}. {mark} {item['text']}")
    return "\n".join(lines)


async def summarize_text(text: str) -> str:
    if not openai_client:
        return "OpenAI key not configured"

    def _call():
        prompt = (
            "Ниже сообщения.\n"
            "Сделай краткую сводку:\n\n"
            f"{text}"
        )
        resp = openai_client.responses.create(model="gpt-4.1-mini", input=prompt)
        return resp.output_text

    return await asyncio.to_thread(_call)


async def save_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message.chat.type == "private":
        return
    doc = {
        "chat_id": message.chat_id,
        "message_id": message.message_id,
        "sender_id": message.from_user.id if message.from_user else None,
        "date": message.date,
        "text": message.text or "",
    }
    await db.messages.insert_one(doc)
    if message.from_user:
        await db.users.update_one(
            {"user_id": message.from_user.id},
            {"$set": {"user_id": message.from_user.id}},
            upsert=True,
        )


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    keyboard = [
        [
            InlineKeyboardButton("Chats", callback_data="chats"),
            InlineKeyboardButton("Stats", callback_data="stats"),
        ],
        [
            InlineKeyboardButton("Send", callback_data="send"),
            InlineKeyboardButton("Pin", callback_data="pin"),
        ],
        [
            InlineKeyboardButton("Summary", callback_data="summary"),
            InlineKeyboardButton("Users", callback_data="users"),
        ],
    ]
    await update.message.reply_text(
        "Выберите действие:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "chats":
        chat_ids = await db.messages.distinct("chat_id")
        lines = []
        for cid in chat_ids:
            try:
                chat = await context.bot.get_chat(cid)
                title = chat.title or ""
            except Exception:
                title = ""
            lines.append(f"{cid} — {title}")
        text = "\n".join(lines)
        await query.edit_message_text("Список чатов:\n" + (text or "Нет"))
    elif data == "stats":
        pipeline = [{"$group": {"_id": "$chat_id", "count": {"$sum": 1}}}]
        rows = await db.messages.aggregate(pipeline).to_list(None)
        text = "\n".join(f"{r['_id']}: {r['count']}" for r in rows)
        await query.edit_message_text("Статистика:\n" + (text or "Нет данных"))
    elif data == "users":
        users = await db.users.distinct("user_id")
        text = "\n".join(str(u) for u in users)
        await query.edit_message_text("Пользователи:\n" + (text or "Нет"))
    elif data == "send":
        await query.edit_message_text("Отправьте: /send <chat_id> <текст>")
    elif data == "pin":
        await query.edit_message_text("Отправьте: /pin <chat_id> <текст>")
    elif data == "summary":
        await query.edit_message_text(
            "Отправьте: /summary <chat_id> <YYYY-MM-DD> <YYYY-MM-DD>"
        )


async def send_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /send <chat_id> <text>")
        return
    chat_id = int(context.args[0])
    text = " ".join(context.args[1:])
    await context.bot.send_message(chat_id, text)
    await update.message.reply_text("Отправлено")


async def send_and_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /pin <chat_id> <text>")
        return
    chat_id = int(context.args[0])
    text = " ".join(context.args[1:])
    msg = await context.bot.send_message(chat_id, text)
    await context.bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
    await update.message.reply_text("Отправлено и закреплено")


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /summary <chat_id> <YYYY-MM-DD> <YYYY-MM-DD>"
        )
        return
    chat_id = int(context.args[0])
    start = datetime.fromisoformat(context.args[1])
    end = datetime.fromisoformat(context.args[2])
    cursor = db.messages.find(
        {"chat_id": chat_id, "date": {"$gte": start, "$lte": end}}
    ).sort("date", 1)
    texts = [doc["text"] async for doc in cursor if doc["text"]]
    if not texts:
        await update.message.reply_text("Нет сообщений")
        return
    summary = await summarize_text("\n".join(texts))
    await update.message.reply_text(summary)


async def create_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /checklist <chat_id> task1; task2")
        return
    chat_id = int(context.args[0])
    raw = " ".join(context.args[1:])
    tasks = [t.strip() for t in raw.split(";") if t.strip()]
    items = [{"text": t, "done": False} for t in tasks]
    msg = await context.bot.send_message(chat_id, format_checklist(items))
    await db.checklists.update_one(
        {"chat_id": chat_id},
        {"$set": {"chat_id": chat_id, "message_id": msg.message_id, "items": items}},
        upsert=True,
    )
    await update.message.reply_text("Чеклист создан")


async def close_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type == "private":
        return
    if len(context.args) < 1:
        return
    idx = int(context.args[0]) - 1
    checklist = await db.checklists.find_one({"chat_id": update.effective_chat.id})
    if not checklist or idx < 0 or idx >= len(checklist["items"]):
        return
    if checklist["items"][idx].get("done"):
        return
    checklist["items"][idx]["done"] = True
    await db.checklists.update_one(
        {"_id": checklist["_id"]},
        {"$set": {"items": checklist["items"]}},
    )
    await context.bot.edit_message_text(
        chat_id=update.effective_chat.id,
        message_id=checklist["message_id"],
        text=format_checklist(checklist["items"]),
    )
    await update.message.reply_text("Задача закрыта")


def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(MessageHandler(filters.ALL, save_message), group=1)
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(CommandHandler("send", send_message))
    application.add_handler(CommandHandler("pin", send_and_pin))
    application.add_handler(CommandHandler("summary", summary_cmd))
    application.add_handler(CommandHandler("checklist", create_checklist))
    application.add_handler(CommandHandler("done", close_task))
    print("Bot started")
    application.run_polling()


if __name__ == "__main__":
    main()

