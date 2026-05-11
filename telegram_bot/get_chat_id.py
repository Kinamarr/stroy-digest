# Запусти этот скрипт ОДИН РАЗ чтобы узнать свой CHAT_ID.
# Перед запуском отправь любое сообщение боту @assistent1101bot в Telegram.

import asyncio
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ['BOT_TOKEN']


async def get_chat_id():
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/getUpdates'
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        data = resp.json()

    if not data.get('result'):
        print('Обновлений нет.')
        print('Пожалуйста, отправь любое сообщение боту @assistent1101bot в Telegram и запусти скрипт снова.')
        return

    seen = set()
    for update in data['result']:
        msg = update.get('message') or update.get('channel_post')
        if msg:
            chat = msg['chat']
            chat_id = chat['id']
            if chat_id not in seen:
                seen.add(chat_id)
                name = f"{chat.get('first_name', '')} {chat.get('last_name', '')}".strip()
                title = chat.get('title', '')
                print(f'CHAT_ID: {chat_id}  |  Тип: {chat["type"]}  |  {name or title}')

    if not seen:
        print('Сообщения не найдены. Отправь /start боту @assistent1101bot и попробуй снова.')


asyncio.run(get_chat_id())
