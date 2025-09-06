import os
import asyncio
from datetime import datetime

from telethon import TelegramClient, events, Button
from motor.motor_asyncio import AsyncIOMotorClient
from openai import OpenAI

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
ADMIN_IDS = {int(i) for i in os.environ.get("ADMIN_IDS", "").split(",") if i}

client = TelegramClient("bot_session", API_ID, API_HASH).start(bot_token=BOT_TOKEN)
db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client.tg_anal
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

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

@client.on(events.NewMessage)
async def save_message(event):
    if event.is_private:
        return
    doc = {
        "chat_id": event.chat_id,
        "message_id": event.id,
        "sender_id": event.sender_id,
        "date": event.date,
        "text": event.raw_text,
    }
    await db.messages.insert_one(doc)
    await db.users.update_one(
        {"user_id": event.sender_id},
        {"$set": {"user_id": event.sender_id}},
        upsert=True,
    )

@client.on(events.NewMessage(pattern="/admin"))
async def admin_menu(event):
    if event.sender_id not in ADMIN_IDS:
        return
    buttons = [
        [Button.inline("Chats", b"chats"), Button.inline("Stats", b"stats")],
        [Button.inline("Send", b"send"), Button.inline("Pin", b"pin")],
        [Button.inline("Summary", b"summary"), Button.inline("Users", b"users")],
    ]
    await event.respond("Выберите действие:", buttons=buttons)

@client.on(events.CallbackQuery)
async def callbacks(event):
    if event.sender_id not in ADMIN_IDS:
        return
    data = event.data.decode()
    if data == "chats":
        dialogs = await client.get_dialogs()
        text = "\n".join(f"{d.id} — {d.title}" for d in dialogs if d.is_group)
        await event.edit("Список чатов:\n" + (text or "Нет"))
    elif data == "stats":
        pipeline = [{"$group": {"_id": "$chat_id", "count": {"$sum": 1}}}]
        rows = await db.messages.aggregate(pipeline).to_list(None)
        text = "\n".join(f"{r['_id']}: {r['count']}" for r in rows)
        await event.edit("Статистика:\n" + (text or "Нет данных"))
    elif data == "users":
        users = await db.users.distinct("user_id")
        text = "\n".join(str(u) for u in users)
        await event.edit("Пользователи:\n" + (text or "Нет"))
    elif data == "send":
        await event.edit("Отправьте: send <chat_id> <текст>")
    elif data == "pin":
        await event.edit("Отправьте: pin <chat_id> <текст>")
    elif data == "summary":
        await event.edit("Отправьте: summary <chat_id> <YYYY-MM-DD> <YYYY-MM-DD>")

@client.on(events.NewMessage(pattern=r"^send (-?\d+) (.+)$"))
async def send_message(event):
    if event.sender_id not in ADMIN_IDS:
        return
    chat_id = int(event.pattern_match.group(1))
    text = event.pattern_match.group(2)
    await client.send_message(chat_id, text)
    await event.reply("Отправлено")

@client.on(events.NewMessage(pattern=r"^pin (-?\d+) (.+)$"))
async def send_and_pin(event):
    if event.sender_id not in ADMIN_IDS:
        return
    chat_id = int(event.pattern_match.group(1))
    text = event.pattern_match.group(2)
    msg = await client.send_message(chat_id, text)
    await client.pin_message(chat_id, msg.id, notify=False)
    await event.reply("Отправлено и закреплено")

@client.on(events.NewMessage(pattern=r"^summary (-?\d+) (\d{4}-\d{2}-\d{2}) (\d{4}-\d{2}-\d{2})$"))
async def summary_cmd(event):
    if event.sender_id not in ADMIN_IDS:
        return
    chat_id = int(event.pattern_match.group(1))
    start = datetime.fromisoformat(event.pattern_match.group(2))
    end = datetime.fromisoformat(event.pattern_match.group(3))
    cursor = db.messages.find({"chat_id": chat_id, "date": {"$gte": start, "$lte": end}}).sort("date", 1)
    texts = [doc["text"] async for doc in cursor if doc["text"]]
    if not texts:
        await event.reply("Нет сообщений")
        return
    summary = await summarize_text("\n".join(texts))
    await event.reply(summary)

print("Bot started")
client.run_until_disconnected()
