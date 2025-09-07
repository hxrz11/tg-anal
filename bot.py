import os
import asyncio
import re
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorClient
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
    chat = message.chat
    doc = {
        "chat_id": chat.id,
        "chat_title": chat.title,
        "message_id": message.message_id,
        "sender_id": message.from_user.id,
        "date": message.date,
        "text": message.text or "",
    }
    await db.messages.insert_one(doc)
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
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    data = query.data
    if data == "chats":
        pipeline = [
            {"$group": {"_id": "$chat_id", "title": {"$last": "$chat_title"}}}
        ]
        rows = await db.messages.aggregate(pipeline).to_list(None)
        text = "\n".join(f"{r['_id']} — {r.get('title', '')}" for r in rows)
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
        await query.edit_message_text("Отправьте: send <chat_id> <текст>")
    elif data == "pin":
        await query.edit_message_text("Отправьте: pin <chat_id> <текст>")
    elif data == "summary":
        await query.edit_message_text(
            "Отправьте: summary <chat_id> <YYYY-MM-DD> <YYYY-MM-DD>"
        )


async def send_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    match = context.matches[0]
    chat_id = int(match.group(1))
    text = match.group(2)
    await context.bot.send_message(chat_id, text)
    await update.message.reply_text("Отправлено")


async def send_and_pin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    match = context.matches[0]
    chat_id = int(match.group(1))
    text = match.group(2)
    msg = await context.bot.send_message(chat_id, text)
    await context.bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
    await update.message.reply_text("Отправлено и закреплено")


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    match = context.matches[0]
    chat_id = int(match.group(1))
    start = datetime.fromisoformat(match.group(2))
    end = datetime.fromisoformat(match.group(3))
    cursor = (
        db.messages.find({"chat_id": chat_id, "date": {"$gte": start, "$lte": end}})
        .sort("date", 1)
    )
    texts = [doc["text"] async for doc in cursor if doc["text"]]
    if not texts:
        await update.message.reply_text("Нет сообщений")
        return
    summary = await summarize_text("\n".join(texts))
    await update.message.reply_text(summary)


async def create_checklist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    match = context.matches[0]
    chat_id = int(match.group(1))
    raw = match.group(2)
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
    chat_id = update.effective_chat.id
    checklist = await db.checklists.find_one({"chat_id": chat_id})
    if not checklist:
        return
    idx = int(context.matches[0].group(1)) - 1
    if idx < 0 or idx >= len(checklist["items"]):
        return
    if checklist["items"][idx].get("done"):
        return
    checklist["items"][idx]["done"] = True
    await db.checklists.update_one(
        {"_id": checklist["_id"]}, {"$set": {"items": checklist["items"]}}
    )
    await context.bot.edit_message_text(
        format_checklist(checklist["items"]), chat_id, checklist["message_id"]
    )
    await update.message.reply_text("Задача закрыта")


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(MessageHandler(filters.ChatType.GROUPS, save_message))
    application.add_handler(CommandHandler("admin", admin_menu))
    application.add_handler(CallbackQueryHandler(callbacks))
    application.add_handler(
        MessageHandler(filters.Regex(r"^send (-?\d+) (.+)$"), send_message)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^pin (-?\d+) (.+)$"), send_and_pin)
    )
    application.add_handler(
        MessageHandler(
            filters.Regex(r"^summary (-?\d+) (\d{4}-\d{2}-\d{2}) (\d{4}-\d{2}-\d{2})$"),
            summary_cmd,
        )
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^checklist (-?\d+) (.+)$"), create_checklist)
    )
    application.add_handler(
        MessageHandler(filters.Regex(r"^done (\d+)$") & filters.ChatType.GROUPS, close_task)
    )

    print("Bot started")
    application.run_polling()


if __name__ == "__main__":
    main()

