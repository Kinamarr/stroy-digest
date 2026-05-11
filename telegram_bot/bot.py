import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError

from keywords import CHANNELS, KEYWORDS

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

API_ID = int(os.environ['TELEGRAM_API_ID'])
API_HASH = os.environ['TELEGRAM_API_HASH']
BOT_TOKEN = os.environ['BOT_TOKEN']
CHAT_ID = int(os.environ['CHAT_ID'])
DIGEST_HOUR = int(os.getenv('DIGEST_HOUR', '9'))
DIGEST_MINUTE = int(os.getenv('DIGEST_MINUTE', '0'))
TIMEZONE = os.getenv('TIMEZONE', 'Europe/Moscow')


def match_keywords(text: str) -> list:
    text_lower = text.lower()
    return [kw for kw in KEYWORDS if kw in text_lower]


async def send_message(text: str):
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
    payload = {
        'chat_id': CHAT_ID,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(url, json=payload)
        if resp.status_code != 200:
            logger.error(f'Telegram API error: {resp.text}')


async def fetch_digest(client: TelegramClient):
    logger.info('Starting daily digest collection...')
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    results = []

    for channel in CHANNELS:
        try:
            entity = await client.get_entity(channel)
            messages = await client.get_messages(entity, limit=200)

            for msg in messages:
                if not msg.text:
                    continue
                msg_date = msg.date
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)
                if msg_date < since:
                    continue

                matched = match_keywords(msg.text)
                if not matched:
                    continue

                channel_name = channel.lstrip('@')
                results.append({
                    'channel': channel,
                    'text': msg.text,
                    'link': f'https://t.me/{channel_name}/{msg.id}',
                    'keywords': matched[:3],
                })

            await asyncio.sleep(1)

        except FloodWaitError as e:
            logger.warning(f'FloodWait for {channel}: sleeping {e.seconds}s')
            await asyncio.sleep(e.seconds)
        except Exception as e:
            logger.error(f'Error fetching {channel}: {e}')

    logger.info(f'Digest ready: {len(results)} messages found')
    await send_digest(results)


async def send_digest(results: list):
    date_str = datetime.now().strftime('%d.%m.%Y')

    if not results:
        await send_message(
            f'🏗 <b>Дайджест {date_str}</b>\n\n'
            f'За последние 24 часа по строительным ключевым словам ничего не найдено.'
        )
        return

    await send_message(
        f'🏗 <b>Строительный дайджест — {date_str}</b>\n'
        f'Найдено: <b>{len(results)}</b> сообщений\n'
        f'Каналы: {" | ".join(CHANNELS)}'
    )

    batch = []
    batch_len = 0

    for item in results:
        keywords_str = ', '.join(item['keywords'])
        preview = item['text'][:400]
        if len(item['text']) > 400:
            preview += '...'

        block = (
            f'\n📌 <b>{item["channel"]}</b>\n'
            f'🔑 <i>{keywords_str}</i>\n'
            f'{preview}\n'
            f'<a href="{item["link"]}">→ Открыть сообщение</a>\n'
            f'{"─" * 25}\n'
        )

        if batch_len + len(block) > 3500:
            await send_message(''.join(batch))
            await asyncio.sleep(1)
            batch = []
            batch_len = 0

        batch.append(block)
        batch_len += len(block)

    if batch:
        await send_message(''.join(batch))


async def main():
    client = TelegramClient('parser_session', API_ID, API_HASH)
    await client.start()
    logger.info('Telegram client authenticated')

    scheduler = AsyncIOScheduler(timezone=TIMEZONE)
    scheduler.add_job(
        fetch_digest,
        trigger='cron',
        hour=DIGEST_HOUR,
        minute=DIGEST_MINUTE,
        args=[client],
        id='daily_digest',
    )
    scheduler.start()
    logger.info(f'Scheduler running. Digest at {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d} {TIMEZONE}')

    # Раскомментируй чтобы запустить дайджест прямо сейчас (для теста):
    # await fetch_digest(client)

    await asyncio.Event().wait()


if __name__ == '__main__':
    asyncio.run(main())
