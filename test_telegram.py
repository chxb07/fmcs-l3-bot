import os
import asyncio
from dotenv import load_dotenv
from telegram.ext import ApplicationBuilder

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

async def main():
    if not TOKEN:
        print("Error: BOT_TOKEN not found in .env")
        return

    try:
        app = (
            ApplicationBuilder()
            .token(TOKEN)
            .connect_timeout(20)
            .read_timeout(30)
            .write_timeout(20)
            .pool_timeout(20)
            .build()
        )

        async with app:
            me = await app.bot.get_me()
            print("Connected successfully!")
            print("Bot name:", me.first_name)
            print("Bot username:", me.username)

    except Exception as error:
        print("Connection failed:", type(error).__name__)
        print("Details:", str(error))

asyncio.run(main())