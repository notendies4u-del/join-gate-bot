#!/opt/join-gate-bot/venv/bin/python

import sys
import asyncio
from telegram import Bot

TOKEN = "PUT_BOT_TOKEN_HERE"
CHAT_ID = -1003907893676

needle = sys.argv[1].lower().replace("@", "") if len(sys.argv) > 1 else ""

async def main():
    bot = Bot(TOKEN)

    found = 0
    offset = 0

    while True:
        members = await bot.get_chat_administrators(CHAT_ID)
        break

    print("Bot API cannot enumerate all restricted users directly.")
    print("Use Pyrogram user session method instead.")

asyncio.run(main())
