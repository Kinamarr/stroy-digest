"""
Дозагрузка полного текста для новостей, у которых есть только заголовок
(Google Новости, заголовки со страниц сайтов).

Для каждой такой новости:
  1) ссылку Google Новостей превращаем в адрес настоящей статьи;
  2) скачиваем статью и вытаскиваем основной текст;
  3) проверяем, что текст про ту же новость (слова заголовка встречаются в статье),
     иначе оставляем как было;
  4) подставляем текст, реальную ссылку, телефоны и email.

Работает в пределах бюджета времени — ежедневный запуск не должен растягиваться.
"""
import asyncio
import re
import time

import httpx
from bs4 import BeautifulSoup

from regional import EMAIL_RE, HEADERS, PHONE_RE

SHORT_TEXT = 250          # короче — считаем, что у новости только заголовок
MAX_ITEMS = 400           # сколько новостей максимум дозагружать за запуск
TIME_BUDGET = 300         # секунд на всю дозагрузку
BODY_CHARS = 1500         # сколько текста статьи сохраняем

JUNK = re.compile(r'cookie|подписывайтесь|подпишитесь|нашли ошибку|ctrl\s*\+\s*enter|все права защищены|'
                  r'читайте также|реклама|erid', re.I)


def _decode_gnews(url: str) -> str:
    """news.google.com/rss/articles/… → адрес статьи (пустая строка, если не вышло)."""
    try:
        from googlenewsdecoder import gnewsdecoder
        r = gnewsdecoder(url, interval=1)
        return r.get('decoded_url', '') if (r.get('success') or r.get('status')) else ''
    except Exception:
        return ''


def _extract(html: str) -> str:
    soup = BeautifulSoup(html, 'lxml')
    for junk in soup(['script', 'style', 'nav', 'header', 'footer', 'aside', 'form', 'noscript']):
        junk.decompose()
    root = soup.find('article') or soup
    paras = [p.get_text(' ', strip=True) for p in root.find_all('p')]
    paras = [p for p in paras if len(p) > 60 and not JUNK.search(p)]
    return re.sub(r'\s+', ' ', ' '.join(paras)).strip()


def _stems(text: str) -> set:
    return {w[:5] for w in re.findall(r'[а-яёa-z0-9]{4,}', text.lower())}


def _same_story(title: str, body: str) -> bool:
    """Статья про ту же новость, если в ней есть большинство значимых слов заголовка."""
    t = _stems(title)
    return bool(t) and len(t & _stems(body[:4000])) / len(t) >= 0.5


async def _enrich_one(client, item, sem_gn, sem_web, deadline):
    if time.monotonic() > deadline:
        return False
    title = item['text'].split('\n', 1)[0]
    url = item['link']
    if 'news.google.com' in url:
        async with sem_gn:
            if time.monotonic() > deadline:
                return False
            url = await asyncio.to_thread(_decode_gnews, url)
        if not url:
            return False
    async with sem_web:
        try:
            r = await asyncio.wait_for(client.get(url), timeout=25)
        except Exception:
            return False
    if r.status_code >= 400:
        return False
    body = _extract(r.text)
    if len(body) < 200 or not _same_story(title, body):
        return False
    body = body[:BODY_CHARS]
    item['text'] = f'{title}\n{body}'
    item['link'] = str(r.url)
    item['phones'] = list(dict.fromkeys(item.get('phones', []) + PHONE_RE.findall(body)))
    item['emails'] = list(dict.fromkeys(item.get('emails', []) + EMAIL_RE.findall(body)))
    return True


async def _enrich(items):
    deadline = time.monotonic() + TIME_BUDGET
    sem_gn, sem_web = asyncio.Semaphore(3), asyncio.Semaphore(15)
    async with httpx.AsyncClient(headers=HEADERS, timeout=httpx.Timeout(25, connect=10),
                                 follow_redirects=True, verify=False) as client:
        res = await asyncio.gather(*[_enrich_one(client, it, sem_gn, sem_web, deadline) for it in items])
    return sum(res)


def enrich_short_items(all_results: list) -> None:
    """Дозагружает текст статей. Сначала свежие новости с конкретным типом объекта и регионом."""
    short = [it for it in all_results
             if len(it['text']) < SHORT_TEXT and it.get('category') != 'Общее' and it.get('link')]
    short.sort(key=lambda it: (bool(it.get('region')), it.get('date', '')), reverse=True)
    batch = short[:MAX_ITEMS]
    print(f'  Новостей только с заголовком: {len(short)}, дозагружаю: {len(batch)}')
    if not batch:
        return
    t0 = time.monotonic()
    done = asyncio.run(_enrich(batch))
    print(f'  Полный текст получен: {done} из {len(batch)} за {time.monotonic() - t0:.0f} с')
