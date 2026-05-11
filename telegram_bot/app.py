import asyncio
import json
import os
import sys
import threading
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template
from telethon import TelegramClient
from telethon.errors import FloodWaitError


# ── Пути: внутри .exe vs рядом с .exe ────────────────────────────────────────

def _res(rel: str) -> str:
    """Ресурсы внутри бандла (templates, keywords и т.д.)"""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _data(rel: str) -> str:
    """Файлы пользователя — всегда рядом с .exe или скриптом"""
    if hasattr(sys, '_MEIPASS'):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


# ── Конфигурация ──────────────────────────────────────────────────────────────

load_dotenv(_data('.env'))

API_ID        = int(os.environ['TELEGRAM_API_ID'])
API_HASH      = os.environ['TELEGRAM_API_HASH']
DIGEST_HOUR   = int(os.getenv('DIGEST_HOUR', '9'))
DIGEST_MINUTE = int(os.getenv('DIGEST_MINUTE', '0'))
TIMEZONE      = os.getenv('TIMEZONE', 'Europe/Moscow')

DIGESTS_DIR  = Path(_data('digests'))
SESSION_FILE = Path(_data('parser_session.session'))
DIGESTS_DIR.mkdir(exist_ok=True)

# keywords.py лежит рядом со скриптом / внутри бандла
sys.path.insert(0, _res('.'))
from keywords import CHANNELS, KEYWORD_CATEGORIES, KEYWORDS  # noqa: E402

# ── Flask ─────────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder=_res('templates'))

_scraping = False
_scraping_lock = threading.Lock()


# ── Вспомогательные функции ───────────────────────────────────────────────────

def today_file() -> Path:
    return DIGESTS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.json"


def match_keywords(text: str) -> list:
    text_lower = text.lower()
    return [kw for kw in KEYWORDS if kw in text_lower]


def get_category(matched: list) -> str:
    for cat, kws in KEYWORD_CATEGORIES.items():
        for kw in matched:
            if kw in kws:
                return cat
    return 'Общее'


def _group_by_category(items: list) -> dict:
    grouped = {}
    for item in items:
        cat = item['category']
        grouped.setdefault(cat, []).append(item)
    return grouped


# ── Сбор дайджеста ────────────────────────────────────────────────────────────

async def _scrape_async():
    client = TelegramClient(str(SESSION_FILE.with_suffix('')), API_ID, API_HASH)
    await client.start()

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
                    'text': msg.text[:600] + ('...' if len(msg.text) > 600 else ''),
                    'link': f'https://t.me/{channel_name}/{msg.id}',
                    'keywords': matched[:3],
                    'category': get_category(matched),
                })
            await asyncio.sleep(1)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
        except Exception as e:
            print(f'Ошибка при чтении {channel}: {e}')

    await client.disconnect()

    data = {
        'date': datetime.now().strftime('%d.%m.%Y'),
        'generated_at': datetime.now().strftime('%H:%M'),
        'count': len(results),
        'items': results,
    }
    today_file().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Дайджест готов: {len(results)} сообщений')


def _scrape_task():
    global _scraping
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_scrape_async())
    except Exception as e:
        print(f'Ошибка сбора: {e}')
    finally:
        loop.close()
        with _scraping_lock:
            _scraping = False


# ── Маршруты Flask ────────────────────────────────────────────────────────────

@app.route('/')
def index():
    f = today_file()
    data, grouped = None, {}
    if f.exists():
        data = json.loads(f.read_text(encoding='utf-8'))
        grouped = _group_by_category(data['items'])

    with _scraping_lock:
        is_scraping = _scraping

    return render_template(
        'index.html',
        data=data,
        grouped=grouped,
        scraping=is_scraping,
        session_ok=SESSION_FILE.exists(),
        digest_time=f'{DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d}',
    )


@app.route('/scrape')
def scrape():
    global _scraping
    with _scraping_lock:
        if not _scraping:
            _scraping = True
            threading.Thread(target=_scrape_task, daemon=True).start()
    return redirect('/')


@app.route('/status')
def status():
    with _scraping_lock:
        return jsonify({'scraping': _scraping})


# ── Авторизация (первый запуск) ───────────────────────────────────────────────

async def _auth_only():
    print('\n=== Авторизация в Telegram ===')
    client = TelegramClient(str(SESSION_FILE.with_suffix('')), API_ID, API_HASH)
    await client.start()
    print('Авторизация прошла успешно! Сессия сохранена.\n')
    await client.disconnect()


# ── Запуск ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # Первый запуск — нет сессии → авторизуемся
    if not SESSION_FILE.exists():
        print('\nФайл сессии не найден. Нужна одноразовая авторизация в Telegram.\n')
        asyncio.run(_auth_only())

    # Планировщик ежедневного сбора
    scheduler = BackgroundScheduler(timezone=TIMEZONE)
    scheduler.add_job(_scrape_task, 'cron', hour=DIGEST_HOUR, minute=DIGEST_MINUTE)
    scheduler.start()

    # Открыть браузер через секунду после старта
    threading.Timer(1.2, lambda: webbrowser.open('http://localhost:5000')).start()

    print(f'\n  Сайт: http://localhost:5000')
    print(f'  Автосбор каждый день в {DIGEST_HOUR:02d}:{DIGEST_MINUTE:02d}\n')

    app.run(debug=False, port=5000, use_reloader=False)
