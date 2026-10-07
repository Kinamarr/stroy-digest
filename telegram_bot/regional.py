"""
Региональные источники из sources.json (собирается build_sources.py) + каналы из channels.txt.

Читает всё параллельно, четырьмя способами:
  tg    — веб-лента Телеграм-канала t.me/s/<канал>
  rss   — RSS/Atom-лента сайта
  html  — заголовки-ссылки со страницы сайта
  gnews — Google Новости с запросом site:<домен> (для сайтов, закрытых для зарубежных серверов)

Каждой новости проставляется регион источника — так она попадает в фильтр по региону,
даже если город в тексте не назван.
"""
import asyncio
import json
import re
import sys
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from regions import region_of
from web_scraper import ACTION_STEMS, _is_noise, _obj_matches, find_cities, get_category, match_keywords

HERE = Path(__file__).parent
HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                   '(KHTML, like Gecko) Chrome/124.0 Safari/537.36'),
    'Accept-Language': 'ru-RU,ru;q=0.9',
}
PHONE_RE = re.compile(r'(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}')
EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')

MAX_AGE_DAYS = 10      # новости старше — не берём
TG_PAGES = 2           # страниц ленты на канал (~20 постов каждая)
HTML_MAX_LINKS = 60    # сколько заголовков максимум брать со страницы сайта
GNEWS_TERMS = 'строительство OR строят OR построят OR возведут OR реконструкция OR стройка OR откроют'


def load_sources() -> list:
    path = HERE / 'sources.json'
    sources = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    # Каналы из channels.txt — федеральные и уже проверенные; регион у них определяем по тексту
    known = {s['target'].lower() for s in sources if s['method'] == 'tg'}
    ch_file = HERE / 'channels.txt'
    if ch_file.exists():
        for line in ch_file.read_text(encoding='utf-8').splitlines():
            h = line.strip().lstrip('@')
            if h and not h.startswith('#') and h.lower() not in known:
                sources.append({'method': 'tg', 'target': h, 'region': '', 'name': '@' + h,
                                'construction': False})
                known.add(h.lower())
    return sources


# ── Фильтр и сборка карточки ──────────────────────────────────────────────────
# Для строительных источников — более широкий список «действий»: у них почти всё про стройку,
# но без глагола проходит шум вроде «выкупил помещение в здании театра».
WIDE_ACTIONS = ACTION_STEMS | {
    'откро', 'откры', 'появ', 'проект', 'планир', 'ремонт', 'достро', 'сдад', 'сдал',
    'заверш', 'концепц', 'возобнов', 'благоустр', 'строй',
}
# Школьная/медицинская жизнь, а не стройка
EXTRA_NOISE = ('отопительн', 'учител', 'школьник', 'фермера', 'выпускник', 'олимпиад', 'вакцин',
               'учебный год', 'урок', 'экзамен',
               # «открыл сезон», «фестиваль открылся» — культурная жизнь, не стройка
               'фестивал', 'сезон', 'концерт', 'спектакл', 'выставк', 'гастрол', 'премьер', 'чествов',
               'мошеннич')


def _match(text: str, construction: bool) -> list:
    """Строительный источник: объект + любое из широкого списка действий.
    Общее СМИ: объект + глагол стройки (строгий фильтр)."""
    if not construction:
        return match_keywords(text)
    t = text.lower()
    if _is_noise(text) or any(n in t for n in EXTRA_NOISE):
        return []
    objs = _obj_matches(t)
    acts = [a for a in WIDE_ACTIONS if a in t]
    return (objs + acts)[:6] if objs and acts else []


def _fresh(date_str: str, cutoff: str) -> bool:
    return not date_str or date_str >= cutoff


def _make(src: dict, text: str, link: str, date_str: str, matched: list, contacts: bool = False) -> dict:
    cities = find_cities(text)
    phones = list(dict.fromkeys(PHONE_RE.findall(text))) if contacts else []
    emails = list(dict.fromkeys(EMAIL_RE.findall(text))) if contacts else []
    return {
        'channel': src['name'][:60],
        'channel_name': src['target'],
        'text': text,
        'link': link,
        'date': date_str,
        'keywords': matched[:4],
        'category': get_category(matched),
        'cities': cities,
        'region': src.get('region') or region_of(cities),
        'phones': phones,
        'emails': emails,
    }


def _parse_date(text: str) -> str:
    text = (text or '').strip()
    if not text:
        return ''
    try:
        return parsedate_to_datetime(text).strftime('%Y-%m-%d')
    except Exception:
        pass
    m = re.match(r'(\d{4}-\d{2}-\d{2})', text)              # ISO 8601 (Atom)
    if m:
        return m.group(1)
    m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', text)
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)}' if m else ''


# ── Способы чтения ────────────────────────────────────────────────────────────
async def read_tg(client, src, cutoff):
    handle, out, url = src['target'], [], f'https://t.me/s/{src["target"]}'
    for page in range(TG_PAGES):
        r = await client.get(url)
        soup = BeautifulSoup(r.text, 'html.parser')
        wraps = soup.find_all('div', class_='tgme_widget_message_wrap')
        if not wraps:
            break
        min_id, too_old = None, False
        for w in wraps:
            link_el = w.find('a', class_='tgme_widget_message_date')
            link = link_el['href'] if link_el else f'https://t.me/{handle}'
            try:
                mid = int(link.rstrip('/').split('/')[-1])
                min_id = mid if min_id is None else min(min_id, mid)
            except ValueError:
                pass
            t_el = w.find('div', class_='tgme_widget_message_text')
            if not t_el:
                continue
            time_el = w.find('time')
            date_str = time_el.get('datetime', '')[:10] if time_el else ''
            if not _fresh(date_str, cutoff):
                too_old = True
                continue
            text = t_el.get_text(separator='\n').strip()
            matched = _match(text, src['construction'])
            if matched:
                out.append(_make(src, text, link, date_str, matched, contacts=True))
        if too_old or not min_id or page == TG_PAGES - 1:
            break
        url = f'https://t.me/s/{handle}?before={min_id}'
        await asyncio.sleep(1)
    return out


async def read_rss(client, src, cutoff):
    r = await client.get(src['target'])
    soup = BeautifulSoup(r.content, 'xml')
    out = []
    for it in soup.find_all(['item', 'entry'])[:100]:
        title = it.find('title')
        title = title.get_text(' ', strip=True) if title else ''
        if not title:
            continue
        link_el = it.find('link')
        link = ''
        if link_el is not None:
            link = (link_el.get('href') or link_el.get_text(strip=True) or '').strip()
        date_el = it.find(['pubDate', 'published', 'updated', 'date'])
        date_str = _parse_date(date_el.get_text(strip=True) if date_el else '')
        if not _fresh(date_str, cutoff):
            continue
        desc_el = it.find(['description', 'summary', 'content', 'encoded'])
        desc = BeautifulSoup(desc_el.get_text(), 'html.parser').get_text(' ', strip=True)[:500] if desc_el else ''
        text = f'{title}\n{desc}' if desc and desc[:60] not in title else title
        matched = _match(text, src['construction'])
        if matched:
            out.append(_make(src, text, link or src['target'], date_str, matched))
    return out


async def read_html(client, src, cutoff):
    r = await client.get(src['target'])
    base = str(r.url)
    host = urlparse(base).netloc
    soup = BeautifulSoup(r.text, 'lxml')
    for junk in soup.find_all(['header', 'footer', 'nav']):
        junk.decompose()
    out, seen = [], set()
    for a in soup.find_all('a', href=True):
        title = a.get_text(' ', strip=True)
        href = urljoin(base, a['href'])
        if not (30 <= len(title) <= 220) or urlparse(href).netloc != host or href in seen:
            continue
        seen.add(href)
        if len(seen) > HTML_MAX_LINKS:
            break
        matched = _match(title, src['construction'])
        if matched:
            out.append(_make(src, title, href, '', matched))
    return out


async def read_gnews(client, src, cutoff):
    # Строительные слова — прямо в запросе, иначе Google отдаёт все статьи сайта подряд
    q = quote(f'site:{src["target"]} ({GNEWS_TERMS}) when:{MAX_AGE_DAYS}d')
    r = await client.get(f'https://news.google.com/rss/search?q={q}&hl=ru&gl=RU&ceid=RU:ru')
    soup = BeautifulSoup(r.content, 'xml')
    out = []
    for it in soup.find_all('item'):
        raw = it.find('title').get_text(strip=True) if it.find('title') else ''
        title = re.sub(r'\s+[-–—]\s+[^-–—]{2,60}$', '', raw).strip() or raw
        link = it.find('link').get_text(strip=True) if it.find('link') else ''
        date_str = _parse_date(it.find('pubDate').get_text(strip=True) if it.find('pubDate') else '')
        if not title or not _fresh(date_str, cutoff):
            continue
        matched = _match(title, True)   # запрос уже строительный — фильтр как у строительных источников
        if matched:
            out.append(_make(src, title, link, date_str, matched))
    return out


READERS = {'tg': read_tg, 'rss': read_rss, 'html': read_html, 'gnews': read_gnews}
LIMITS = {'tg': 5, 'rss': 25, 'html': 25, 'gnews': 3}


def _title_key(item: dict) -> str:
    first = item['text'].split('\n', 1)[0]
    # «Владислав Овчинский: дом построят…» и «Дом построят…» — одна новость
    first = re.sub(r'^[А-ЯЁ][а-яё]+ [А-ЯЁ][а-яё]+:\s*', '', first).lower()
    return re.sub(r'[^а-яёa-z0-9]', '', first)[:70]


async def _scrape_all(sources: list, cutoff: str):
    sems = {m: asyncio.Semaphore(n) for m, n in LIMITS.items()}
    stats = {m: [0, 0, 0] for m in READERS}           # [источников, ошибок, новостей]
    results = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(25, connect=12),
                                 follow_redirects=True, verify=False,
                                 limits=httpx.Limits(max_connections=60)) as client:
        async def one(src):
            m = src['method']
            async with sems[m]:
                stats[m][0] += 1
                try:
                    items = await asyncio.wait_for(READERS[m](client, src, cutoff), timeout=90)
                    stats[m][2] += len(items)
                    results.extend(items)
                except Exception:
                    stats[m][1] += 1
                if m == 'gnews':
                    await asyncio.sleep(1)     # Google не любит частые запросы

        await asyncio.gather(*[one(s) for s in sources if s['method'] in READERS])
    return results, stats


def scrape_regional() -> list:
    sources = load_sources()
    cutoff = (datetime.now() - timedelta(days=MAX_AGE_DAYS)).strftime('%Y-%m-%d')
    print(f'  Источников: {len(sources)} (новости с {cutoff})')
    raw, stats = asyncio.run(_scrape_all(sources, cutoff))
    names = {'tg': 'Телеграм', 'rss': 'RSS', 'html': 'Страницы сайтов', 'gnews': 'Google Новости'}
    for m, (n, err, found) in stats.items():
        print(f'    {names[m]:16} источников: {n:4}  ошибок: {err:3}  новостей: {found}')

    # Дубли: одна и та же ссылка или один и тот же заголовок из разных источников
    out, seen_links, seen_titles = [], set(), set()
    for it in sorted(raw, key=lambda x: x['date'], reverse=True):
        k = _title_key(it)
        if it['link'] in seen_links or (k and k in seen_titles):
            continue
        seen_links.add(it['link'])
        seen_titles.add(k)
        out.append(it)
    print(f'  Региональные источники: {len(out)} новостей (без дублей, было {len(raw)})')
    return out


if __name__ == '__main__':
    items = scrape_regional()
    from collections import Counter
    print(Counter(i['region'] or '—' for i in items).most_common(30))
    print(Counter(i['category'] for i in items).most_common())
