"""
Парсеры веб-сайтов: Коммерсантъ (все регионы), Абирег, Global52,
ИнвестПроекты, Строители.РФ, АРД Эксперт, РБК Недвижимость.
Возвращают список словарей того же формата что и telegram scraper.
"""
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).parent))
from keywords import KEYWORD_CATEGORIES
from cities import CITIES

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}

OBJECT_KEYWORDS = set(kw for kws in KEYWORD_CATEGORIES.values() for kw in kws)
ACTION_STEMS = {
    'строит', 'возвод', 'возвед', 'постро', 'приступ',
    'котлован', 'монолит', 'стройплощ', 'введ', 'разрешени',
    'закладк', 'первый кам', 'ввод в экс', 'сдан',
    'застраива', 'реконстру', 'капитальн',
    'гидроизол', 'водонепрониц', 'дренажн', 'гидротехн',
    'прокладыва', 'монтаж', 'установк', 'прокладк',
}

RU_MONTHS = {
    'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4,
    'мая': 5, 'июня': 6, 'июля': 7, 'августа': 8,
    'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12,
}


def _get(url: str) -> httpx.Response:
    return httpx.get(url, headers=HEADERS, timeout=30,
                     follow_redirects=True, verify=False)


def match_keywords(text: str) -> list:
    """Строгий фильтр: нужен объект И действие (для общих источников)."""
    t = text.lower()
    if not any(kw in t for kw in OBJECT_KEYWORDS):
        return []
    if not any(stem in t for stem in ACTION_STEMS):
        return []
    matched_obj = [kw for kw in OBJECT_KEYWORDS if kw in t]
    matched_act = [s for s in ACTION_STEMS if s in t]
    return (matched_obj + matched_act)[:6]


def match_broad(text: str) -> list:
    """Мягкий фильтр: достаточно объекта ИЛИ действия (для тематических сайтов)."""
    t = text.lower()
    matched_obj = [kw for kw in OBJECT_KEYWORDS if kw in t]
    matched_act = [s for s in ACTION_STEMS if s in t]
    combined = matched_obj + matched_act
    return combined[:6]


def find_cities(text: str) -> list:
    tl = text.lower()
    return [c for c in CITIES if c.lower() in tl]


def get_category(matched_kws: list) -> str:
    for category, kws in KEYWORD_CATEGORIES.items():
        for kw in matched_kws:
            if kw in kws:
                return category
    return 'Общее'


def _make(source_label: str, source_slug: str,
          text: str, link: str, date_str: str, matched: list) -> dict:
    return {
        'channel':      source_label,
        'channel_name': source_slug,
        'text':         text,
        'link':         link,
        'date':         date_str,
        'keywords':     matched[:4],
        'category':     get_category(matched),
        'cities':       find_cities(text),
        'phones':       [],
        'emails':       [],
    }


def _parse_date_dmy(text: str) -> str:
    """DD.MM.YYYY → YYYY-MM-DD"""
    m = re.search(r'(\d{2})\.(\d{2})\.(\d{4})', text)
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)}' if m else ''


def _parse_date_dmy_slash(text: str, year: int) -> str:
    """DD/MM → YYYY-MM-DD (current year)"""
    m = re.search(r'(\d{2})/(\d{2})', text)
    return f'{year}-{m.group(2)}-{m.group(1)}' if m else ''


def _parse_date_ru(text: str) -> str:
    """D месяц YYYY → YYYY-MM-DD"""
    m = re.search(
        r'(\d{1,2})\s+(' + '|'.join(RU_MONTHS) + r')\s+(\d{4})', text
    )
    if not m:
        return ''
    return f'{m.group(3)}-{RU_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}'


# ──────────────────────────────────────────────
# Коммерсантъ — все регионы
# ──────────────────────────────────────────────

def _kommersant_region_ids() -> list:
    """Discover region IDs from Kommersant main navigation."""
    try:
        r = _get('https://www.kommersant.ru')
        soup = BeautifulSoup(r.text, 'html.parser')
        ids = sorted({
            int(re.search(r'/regions/(\d+)', a['href']).group(1))
            for a in soup.find_all('a', href=re.compile(r'^/regions/\d+$'))
        })
        if ids:
            return ids
    except Exception as e:
        print(f'    Коммерсантъ: список регионов — {e}')
    # Fallback: попробовать ID 1–60 (реальных обычно 20–30)
    return list(range(1, 61))


def scrape_kommersant() -> list:
    region_ids = _kommersant_region_ids()
    print(f'    Коммерсантъ: {len(region_ids)} регионов')
    results = []
    seen = set()

    for rid in region_ids:
        try:
            r = _get(f'https://www.kommersant.ru/regions/{rid}')
            if r.status_code == 404:
                continue
            soup = BeautifulSoup(r.text, 'html.parser')

            for a in soup.find_all('a', href=re.compile(r'^/doc/\d+')):
                raw = 'https://www.kommersant.ru' + a['href'].split('?')[0]
                if raw in seen:
                    continue

                title = a.get_text(separator=' ', strip=True)
                if len(title) < 20:
                    continue

                # Ищем дату в ближайшем родительском блоке
                date_str = ''
                parent = a.find_parent(['article', 'div', 'li'])
                if parent:
                    date_str = _parse_date_dmy(parent.get_text())

                matched = match_keywords(title)
                if matched:
                    seen.add(raw)
                    results.append(_make('Коммерсантъ', 'kommersant',
                                        title, raw, date_str, matched))
            time.sleep(0.3)
        except Exception as e:
            print(f'    Коммерсантъ регион {rid}: {e}')

    return results


# ──────────────────────────────────────────────
# Абирег
# ──────────────────────────────────────────────

def scrape_abireg() -> list:
    results = []
    year = datetime.now().year
    try:
        r = _get('https://abireg.ru/')
        soup = BeautifulSoup(r.text, 'html.parser')
        seen = set()

        for a in soup.find_all('a', href=re.compile(r'^/newsitem/\d+/')):
            link = f'https://abireg.ru{a["href"]}'
            if link in seen:
                continue
            title = a.get_text(strip=True)
            if len(title) < 15:
                continue

            date_str = ''
            parent = a.find_parent()
            if parent:
                date_str = _parse_date_dmy_slash(parent.get_text(), year)

            matched = match_keywords(title)
            if matched:
                seen.add(link)
                results.append(_make('Абирег', 'abireg',
                                     title, link, date_str, matched))
    except Exception as e:
        print(f'    Абирег: {e}')
    return results


# ──────────────────────────────────────────────
# Global52
# ──────────────────────────────────────────────

def scrape_global52() -> list:
    results = []
    try:
        r = _get('https://global52.ru/news/catalog/1386')
        soup = BeautifulSoup(r.text, 'html.parser')
        seen = set()

        for a in soup.find_all('a', href=re.compile(r'^/news/id/\d+')):
            link = f'https://global52.ru{a["href"]}'
            if link in seen:
                continue
            title = a.get_text(strip=True)
            if len(title) < 15:
                continue

            date_str = ''
            parent = a.find_parent(['div', 'li', 'article'])
            if parent:
                date_str = (_parse_date_ru(parent.get_text())
                            or _parse_date_dmy(parent.get_text()))

            matched = match_keywords(title)
            if matched:
                seen.add(link)
                results.append(_make('Global52', 'global52',
                                     title, link, date_str, matched))
    except Exception as e:
        print(f'    Global52: {e}')
    return results


# ──────────────────────────────────────────────
# ИнвестПроекты
# ──────────────────────────────────────────────

def scrape_investprojects(max_pages: int = 4) -> list:
    results = []
    seen = set()

    for page in range(1, max_pages + 1):
        url = f'https://investprojects.info/project-base?page={page}&onPage=50'
        try:
            r = _get(url)
            soup = BeautifulSoup(r.text, 'html.parser')
            items = soup.find_all('div', id=re.compile(r'^project-\d+$'))
            if not items:
                break

            for item in items:
                a = item.find('a', href=re.compile(r'^/project-base/\d+'))
                if not a:
                    continue
                link = f'https://investprojects.info{a["href"]}'
                if link in seen:
                    continue

                full_text = item.get_text(separator=' ', strip=True)
                title = a.get_text(strip=True)
                combined = f'{title} {full_text}'
                date_str = _parse_date_dmy(full_text)

                matched = match_keywords(combined)
                if matched:
                    seen.add(link)
                    results.append(_make('ИнвестПроекты', 'investprojects',
                                        combined[:800], link, date_str, matched))
            time.sleep(0.5)
        except Exception as e:
            print(f'    ИнвестПроекты стр.{page}: {e}')
            break

    return results


# ──────────────────────────────────────────────
# Строители.РФ
# ──────────────────────────────────────────────

def scrape_stroiteli_rf() -> list:
    results = []
    base = 'https://xn--80acgfbsl1azdqr.xn--p1ai'
    try:
        r = _get(f'{base}/news/stroitelstvo')
        soup = BeautifulSoup(r.text, 'html.parser')
        seen = set()

        for a in soup.find_all('a', href=re.compile(r'/news/')):
            href = a['href']
            if href in ('/news/stroitelstvo', '/news/') or href in seen:
                continue
            link = href if href.startswith('http') else base + href
            title = a.get_text(strip=True)
            if len(title) < 15:
                continue

            date_str = ''
            parent = a.find_parent(['div', 'li', 'article'])
            if parent:
                date_str = _parse_date_dmy(parent.get_text())

            matched = match_keywords(title)
            if matched:
                seen.add(href)
                results.append(_make('Строители.РФ', 'stroiteli_rf',
                                     title, link, date_str, matched))
    except Exception as e:
        print(f'    Строители.РФ: {e}')
    return results


# ──────────────────────────────────────────────
# АРД Эксперт
# ──────────────────────────────────────────────

def scrape_ardexpert() -> list:
    results = []
    try:
        r = _get('https://ardexpert.ru/article')
        soup = BeautifulSoup(r.text, 'html.parser')
        seen = set()

        for a in soup.find_all('a', href=re.compile(r'/article/')):
            href = a['href']
            link = href if href.startswith('http') else f'https://ardexpert.ru{href}'
            if link in seen:
                continue
            title = a.get_text(strip=True)
            if len(title) < 20:
                continue

            date_str = ''
            parent = a.find_parent(['div', 'li', 'article'])
            if parent:
                date_str = _parse_date_dmy(parent.get_text())

            matched = match_broad(title)
            if matched:
                seen.add(link)
                results.append(_make('АРД Эксперт', 'ardexpert',
                                     title, link, date_str, matched))
    except Exception as e:
        print(f'    АРД Эксперт: {e}')
    return results


# ──────────────────────────────────────────────
# РБК Недвижимость
# ──────────────────────────────────────────────

def scrape_realty_rbc() -> list:
    results = []
    try:
        r = _get('https://realty.rbc.ru/')
        soup = BeautifulSoup(r.text, 'html.parser')
        seen = set()

        for a in soup.find_all('a', href=re.compile(r'realty\.rbc\.ru/(news|rbc_realty)/')):
            href = a['href']
            if href in seen:
                continue
            title = a.get_text(strip=True)
            if len(title) < 20:
                continue

            date_str = ''
            parent = a.find_parent(['div', 'li', 'article'])
            if parent:
                date_str = _parse_date_dmy(parent.get_text())

            matched = match_broad(title)
            if matched:
                seen.add(href)
                results.append(_make('РБК Недвижимость', 'realty_rbc',
                                     title, href, date_str, matched))
    except Exception as e:
        print(f'    РБК Недвижимость: {e}')
    return results


# ──────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────

WEB_SOURCES = [
    ('Коммерсантъ (регионы)', scrape_kommersant),
    ('Абирег',                 scrape_abireg),
    ('Global52',               scrape_global52),
    ('ИнвестПроекты',          scrape_investprojects),
    ('Строители.РФ',           scrape_stroiteli_rf),
    ('АРД Эксперт',            scrape_ardexpert),
    ('РБК Недвижимость',       scrape_realty_rbc),
]


def scrape_all_web() -> list:
    all_results = []
    for name, fn in WEB_SOURCES:
        print(f'  Читаю {name}...')
        try:
            items = fn()
            print(f'    Найдено: {len(items)} объектов')
            all_results.extend(items)
        except Exception as e:
            print(f'    Ошибка: {e}')
        time.sleep(1)
    return all_results
