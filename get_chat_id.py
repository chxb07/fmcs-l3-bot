import os
import asyncio
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

async def main():
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .connect_timeout(20)
        .read_timeout(30)
        .build()
    )

    async with app:
        updates = await app.bot.get_updates(timeout=10)

        found = False
        for update in updates:
            if update.effective_chat:
                print("Chat ID:", update.effective_chat.id)
                print("Chat type:", update.effective_chat.type)
                found = True

        if not found:
            print("No pending updates found.")
            print("Open your bot in Telegram and send /start again.")

asyncio.run(main())