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

# Ключевые слова с обеих сторон которых не должно быть букв (чтобы 'мост' не ловил
# 'недвижимость', а 'метро' не ловил 'метров' / 'квадратных метров').
_LB = r'(?<![а-яёА-ЯЁa-zA-Z0-9])'   # левая граница слова
_RB_STRICT = {'метро', 'бкл', 'мцд', 'мфц', ' жк ', 'арена',
              'гэс', 'тэс', 'тэц', 'гок', 'тэс'}  # + правая граница
_OBJ_KW_RE = {
    kw: re.compile(
        _LB + re.escape(kw) + (r'(?![а-яёА-ЯЁa-zA-Z0-9])' if kw in _RB_STRICT else ''),
        re.IGNORECASE,
    )
    for kw in OBJECT_KEYWORDS
}

# Фразы, указывающие на аналитику рынка — такие статьи не нужны
NOISE_BLACKLIST = frozenset([
    'рост цен', 'снижение цен', 'вторичн',
    'цены выросли', 'цены упали', 'средняя цена',
    'рухнул на', 'упал на', 'обвал рынк',
    'ипотечный', 'ипотечная ставка', 'ключевая ставка',
    'квадратный метр стои', 'стоимость квадрат',
    'аналитики подсчит', 'эксперты подсчит',
])

RU_MONTHS = {
    'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4,
    'мая': 5, 'июня': 6, 'июля': 7, 'августа': 8,
    'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12,
}


def _get(url: str) -> httpx.Response:
    return httpx.get(url, headers=HEADERS, timeout=30,
                     follow_redirects=True, verify=False)


def _is_noise(text: str) -> bool:
    t = text.lower()
    return any(n in t for n in NOISE_BLACKLIST)


def _obj_matches(t: str) -> list:
    return [kw for kw, pat in _OBJ_KW_RE.items() if pat.search(t)]


def match_keywords(text: str) -> list:
    """Строгий фильтр: нужен объект И действие (для общих источников)."""
    if _is_noise(text):
        return []
    t = text.lower()
    matched_obj = _obj_matches(t)
    if not matched_obj:
        return []
    matched_act = [s for s in ACTION_STEMS if s in t]
    if not matched_act:
        return []
    return (matched_obj + matched_act)[:6]


def match_broad(text: str) -> list:
    """Мягкий фильтр: достаточно объекта ИЛИ действия (для тематических сайтов)."""
    if _is_noise(text):
        return []
    t = text.lower()
    matched_obj = _obj_matches(t)
    matched_act = [s for s in ACTION_STEMS if s in t]
    return (matched_obj + matched_act)[:6]


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
# Google News RSS — все регионы России
# ──────────────────────────────────────────────

def _parse_rss_date(text: str) -> str:
    """RFC 2822 pubDate → YYYY-MM-DD."""
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(text).strftime('%Y-%m-%d')
    except Exception:
        return _parse_date_dmy(text)


def _parse_rss_xml(text: str):
    """Парсит RSS через lxml (xml) или html.parser как fallback, возвращает список item dict."""
    try:
        soup = BeautifulSoup(text, 'xml')
        items = soup.find_all('item')
        if items:
            return items, 'xml'
    except Exception:
        pass
    soup = BeautifulSoup(text, 'html.parser')
    return soup.find_all('item'), 'html'


def _rss_text(el) -> str:
    if el is None:
        return ''
    return (el.string or el.get_text(strip=True) or '').strip()


def scrape_google_news_rss() -> list:
    """Строительные новости со всей России через Google News RSS (8 общих + приоритетные города)."""
    import urllib.parse
    QUERIES = [
        # Общие строительные запросы (вся Россия)
        'строительство жилого комплекса',
        'строительство завода предприятия',
        'строительство склада логистического центра',
        'строительство школы больницы поликлиники',
        'строительство гостиницы отеля',
        'строительство стадиона бассейна арены',
        'строительство моста тоннеля',
        'строительство резервуара очистных сооружений насосной',
        # Приоритетные города (отдельные запросы для максимального охвата)
        'строительство Воронеж',
        'строительство Воронежская область объект',
        'новостройки строительство Воронеж застройщик',
        # Дальний Восток
        'строительство Хабаровск объект',
        'строительство Владивосток Приморье объект',
        'строительство Якутск Якутия объект',
    ]
    results = []
    seen_titles = set()

    for query in QUERIES:
        try:
            enc = urllib.parse.quote(query)
            url = f'https://news.google.com/rss/search?q={enc}&hl=ru&gl=RU&ceid=RU:ru'
            r = _get(url)
            if r.status_code != 200 or len(r.text) < 500:
                continue

            items, _ = _parse_rss_xml(r.text)
            for item in items:
                title_el = item.find('title')
                pub_el   = item.find('pubdate') or item.find('pubDate')
                src_el   = item.find('source')

                if not title_el:
                    continue
                raw_title = _rss_text(title_el)
                # Убираем " - Источник" в конце заголовка Google News
                title = re.sub(r'\s*[-–]\s*.{3,50}$', '', raw_title).strip() or raw_title

                if title in seen_titles:
                    continue

                # Ссылка: Google News кладёт URL между тегами, ищем regex
                link_m = re.search(
                    r'<link[^>]*>([^<]{20,})</link>|<link[^/]*/?>([^<]{20,})',
                    str(item)
                )
                link = (link_m.group(1) or link_m.group(2) or '').strip() if link_m else ''

                pub  = _rss_text(pub_el)
                src  = _rss_text(src_el) or 'Google News'
                date_str = _parse_rss_date(pub) if pub else ''

                matched = match_broad(title)
                if not matched:
                    continue

                seen_titles.add(title)
                results.append(_make(
                    f'Google News / {src[:30]}',
                    'gnews',
                    title,
                    link,
                    date_str,
                    matched,
                ))
            time.sleep(0.8)
        except Exception as e:
            print(f'    Google News ({query[:30]}): {e}')

    print(f'    Google News: {len(results)} строительных новостей из регионов')
    return results


# ──────────────────────────────────────────────
# Строительная газета — RSS (отраслевое, вся Россия)
# ──────────────────────────────────────────────

def scrape_stroygaz_rss() -> list:
    """Строительная газета RSS — главное отраслевое издание, ~100 статей."""
    results = []
    try:
        r = _get('https://stroygaz.ru/rss/')
        if r.status_code != 200 or len(r.text) < 500:
            return []
        items, _ = _parse_rss_xml(r.text)
        for item in items:
            title_el = item.find('title')
            pub_el   = item.find('pubdate') or item.find('pubDate')
            desc_el  = item.find('description')

            if not title_el:
                continue
            title = _rss_text(title_el)
            # Ссылка через regex (html.parser делает link void-элементом)
            link_m = re.search(r'<link[^>]*>([^<]{20,})</link>', str(item))
            link = link_m.group(1).strip() if link_m else ''
            pub  = _rss_text(pub_el)
            desc = BeautifulSoup(_rss_text(desc_el), 'html.parser').get_text(strip=True)[:250] if desc_el else ''
            date_str = _parse_rss_date(pub) if pub else ''

            combined = f'{title} {desc}'
            matched  = match_broad(combined)
            if not matched:
                continue

            body = title + (f'\n{desc}' if desc else '')
            results.append(_make('Строительная газета', 'stroygaz', body, link, date_str, matched))
    except Exception as e:
        print(f'    Строительная газета: {e}')
    print(f'    Строительная газета: {len(results)} статей')
    return results


# ──────────────────────────────────────────────
# АНКБ — аналитический центр новостроек
# ──────────────────────────────────────────────

def scrape_ancb() -> list:
    """АНКБ — агрегатор строительных новостей, ежедневно ~30 статей."""
    results = []
    base = 'https://ancb.ru'
    try:
        r = _get(f'{base}/news/')
        if r.status_code != 200 or len(r.text) < 1000:
            return []
        soup = BeautifulSoup(r.text, 'html.parser')
        seen = set()
        for a in soup.find_all('a', href=re.compile(r'/news/read/\d+')):
            href = a['href']
            link = href if href.startswith('http') else base + href
            if link in seen:
                continue
            title = a.get_text(strip=True)
            if len(title) < 10:
                continue
            # Дата из текста карточки
            date_str = ''
            parent = a.find_parent(['div', 'article', 'li'])
            if parent:
                date_str = _parse_date_dmy(parent.get_text())
            matched = match_broad(title)
            if not matched:
                continue
            seen.add(link)
            results.append(_make('АНКБ', 'ancb', title, link, date_str, matched))
    except Exception as e:
        print(f'    АНКБ: {e}')
    print(f'    АНКБ: {len(results)} статей')
    return results


# ──────────────────────────────────────────────
# Горком36 — строительство Воронежа
# ──────────────────────────────────────────────

def scrape_gorcom36() -> list:
    """Горком36 — воронежский портал, раздел строительства."""
    results = []
    base = 'https://gorcom36.ru'
    try:
        r = _get(f'{base}/rubric/stroitelstvo/')
        if r.status_code != 200 or len(r.text) < 500:
            return []
        soup = BeautifulSoup(r.text, 'html.parser')
        seen = set()
        for a in soup.find_all('a', href=re.compile(r'^/?content/')):
            href = a['href']
            link = href if href.startswith('http') else f'{base}/{href.lstrip("/")}'
            if link in seen:
                continue
            title = a.get_text(strip=True)
            if len(title) < 15:
                continue
            date_str = ''
            parent = a.find_parent(['div', 'article', 'li'])
            if parent:
                date_str = _parse_date_dmy(parent.get_text())
            matched = match_broad(title)
            if not matched:
                continue
            seen.add(link)
            results.append(_make('Горком36 (Воронеж)', 'gorcom36', title, link, date_str, matched))
    except Exception as e:
        print(f'    Горком36: {e}')
    print(f'    Горком36: {len(results)} статей')
    return results


# ──────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────

def _otc_detail(href: str) -> tuple:
    """Fetch customer and price from an OTC tender detail page."""
    try:
        r = _get(href)
        if r.status_code != 200 or len(r.text) < 1000:
            return '', ''
        soup = BeautifulSoup(r.text, 'html.parser')
        text = soup.get_text(separator=' | ', strip=True)
        m_cust = re.search(r'Организатор\s*\|\s*(.{5,120}?)\s*\|', text)
        customer = m_cust.group(1).strip()[:100] if m_cust else ''
        m_price = re.search(r'([\d\s]+[,\.]\d+\s*₽)', text)
        price = m_price.group(1).strip()[:40] if m_price else ''
        return customer, price
    except Exception:
        return '', ''


def scrape_otc() -> list:
    """Тендеры на строительство с otc.ru — парсит 1000 новых лотов, фильтрует по ключевым словам."""
    results = []
    try:
        r = _get('https://otc.ru/new-tenders/')
        if r.status_code != 200 or len(r.text) < 10000:
            print(f'    ОТС.ру: недоступно ({r.status_code})')
            return []

        soup = BeautifulSoup(r.text, 'html.parser')
        candidates = []
        seen = set()

        for a in soup.find_all('a', href=re.compile(r'^/buy/l\d')):
            href_rel = a.get('href', '')
            if href_rel in seen:
                continue
            p = a.find_next_sibling('p')
            if not p:
                continue
            title = p.get_text(strip=True)
            if not title:
                continue
            matched = match_broad(title)
            if not matched:
                continue
            seen.add(href_rel)
            candidates.append((href_rel, title, matched))

        print(f'    ОТС.ру: {len(candidates)} строительных лотов из ~1000')

        # Fetch detail pages for up to 20 candidates to get customer + price
        for href_rel, title, matched in candidates[:20]:
            href = 'https://otc.ru' + href_rel
            customer, price = _otc_detail(href)
            body = title[:200]
            if customer:
                body += f'\nЗаказчик: {customer}'
            if price:
                body += f'\nЦена: {price}'
            results.append(_make('ОТС.ру', 'otc', body, href, '', matched))
            time.sleep(0.3)

    except Exception as e:
        print(f'    ОТС.ру: {e}')

    print(f'    ОТС.ру: итого {len(results)} тендеров')
    return results


# ──────────────────────────────────────────────
# Промышленные источники: Норникель, РусГидро
# ──────────────────────────────────────────────

# Слова-триггеры для страниц промышленных компаний (шире чем match_broad)
INDUSTRY_TRIGGERS = frozenset([
    'строит', 'постро', 'возвод', 'возвед', 'введ', 'ввод', 'сдан',
    'реконстру', 'капитальн', 'модерниз', 'капремонт',
    'закупк', 'тендер', 'торги', 'поставк', 'конкурс',
    'рудник', 'шахт', 'карьер', 'обогатит',
    'гидроузел', 'плотин', 'турбин', 'агрегат', 'энергоблок',
    'инвестиц', 'объект', 'сооружен', 'комплекс',
])


def match_industry(text: str) -> list:
    """Для промышленных источников — ловит строительство, закупки, объекты."""
    t = text.lower()
    hits = [s for s in INDUSTRY_TRIGGERS if s in t]
    return hits[:6] if hits else []


def scrape_nornickel() -> list:
    """Пресс-релизы Норникеля о проектах, строительстве, инвестициях."""
    results = []
    base = 'https://www.nornickel.ru'
    seen = set()
    try:
        r = _get(f'{base}/press-center/news/')
        if r.status_code != 200:
            print(f'    Норникель: HTTP {r.status_code}')
            return []
        soup = BeautifulSoup(r.text, 'html.parser')
        for a in soup.find_all('a', href=re.compile(r'/press-center/news/\d')):
            href = a['href']
            link = f'{base}{href}' if href.startswith('/') else href
            if link in seen:
                continue
            title = a.get_text(strip=True)
            if len(title) < 10:
                continue
            matched = match_broad(title) or match_industry(title)
            if not matched:
                continue
            seen.add(link)
            results.append(_make('Норникель', 'nornickel', title, link, '', matched))
    except Exception as e:
        print(f'    Норникель: {e}')
    print(f'    Норникель: {len(results)} статей')
    return results


def scrape_rushydro() -> list:
    """Пресс-релизы РусГидро о строительстве ГЭС и объектов энергетики."""
    results = []
    base = 'https://www.rushydro.ru'
    seen = set()
    try:
        r = _get(f'{base}/press/news/')
        if r.status_code != 200:
            print(f'    РусГидро: HTTP {r.status_code}')
            return []
        soup = BeautifulSoup(r.text, 'html.parser')
        for a in soup.find_all('a', href=re.compile(r'/press/news/')):
            href = a['href']
            if href.endswith('/news/') or href == '/press/news/':
                continue
            link = f'{base}{href}' if href.startswith('/') else href
            if link in seen:
                continue
            title = a.get_text(strip=True)
            if len(title) < 10:
                continue
            matched = match_broad(title) or match_industry(title)
            if not matched:
                continue
            seen.add(link)
            results.append(_make('РусГидро', 'rushydro', title, link, '', matched))
    except Exception as e:
        print(f'    РусГидро: {e}')
    print(f'    РусГидро: {len(results)} статей')
    return results


def scrape_nornickel_tenders() -> list:
    """Закупки Норникеля — строительно-монтажные и проектные работы."""
    results = []
    base = 'https://zakupki.nornickel.ru'
    seen = set()
    try:
        # Поиск закупок по строительству
        for query in ['строительство', 'гидроизоляция', 'монтаж']:
            enc = query.replace(' ', '+')
            r = _get(f'{base}/search?q={enc}')
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, 'html.parser')
            for a in soup.find_all('a', href=re.compile(r'/(tender|lot|purchase)/')):
                href = a['href']
                link = f'{base}{href}' if href.startswith('/') else href
                if link in seen:
                    continue
                title = a.get_text(strip=True)
                if len(title) < 10:
                    continue
                matched = match_industry(title) or match_broad(title)
                if not matched:
                    continue
                seen.add(link)
                results.append(_make('Норникель (закупки)', 'nornickel_tenders',
                                     title, link, '', matched))
            time.sleep(0.5)
    except Exception as e:
        print(f'    Норникель закупки: {e}')
    print(f'    Норникель (закупки): {len(results)} тендеров')
    return results


def scrape_rushydro_tenders() -> list:
    """Закупки РусГидро — строительство и оборудование для ГЭС."""
    results = []
    base = 'https://zakupki.rushydro.ru'
    seen = set()
    try:
        r = _get(f'{base}/zakupki/')
        if r.status_code != 200:
            # Запасной URL
            r = _get(f'https://www.rushydro.ru/procurement/')
        if r.status_code != 200:
            print(f'    РусГидро закупки: HTTP {r.status_code}')
            return []
        soup = BeautifulSoup(r.text, 'html.parser')
        for a in soup.find_all('a', href=True):
            href = a['href']
            title = a.get_text(strip=True)
            if len(title) < 15:
                continue
            matched = match_industry(title) or match_broad(title)
            if not matched:
                continue
            link = f'{base}{href}' if href.startswith('/') else href
            if link in seen:
                continue
            seen.add(link)
            results.append(_make('РусГидро (закупки)', 'rushydro_tenders',
                                 title, link, '', matched))
    except Exception as e:
        print(f'    РусГидро закупки: {e}')
    print(f'    РусГидро (закупки): {len(results)} тендеров')
    return results


def scrape_google_news_industry() -> list:
    """Google News — крупные промышленные проекты и горнодобывающая отрасль."""
    import urllib.parse
    QUERIES = [
        'Норникель строительство объект',
        'РусГидро строительство ГЭС объект',
        'АЛРОСА строительство рудник',
        'горнодобывающий строительство завод карьер',
        'строительство гидроэлектростанции объект',
        'строительство обогатительная фабрика рудник',
    ]
    results = []
    seen_titles: set = set()
    for query in QUERIES:
        try:
            enc = urllib.parse.quote(query)
            r = _get(f'https://news.google.com/rss/search?q={enc}&hl=ru&gl=RU&ceid=RU:ru')
            items, _ = _parse_rss_xml(r.text)
            for item in items:
                t_el  = item.find('title')
                pub   = item.find('pubDate')
                title = _rss_text(t_el)
                title_clean = re.sub(r'\s*[-–]\s*.{3,50}$', '', title).strip() or title
                if title_clean in seen_titles or len(title_clean) < 10:
                    continue
                link_m = re.search(r'<link[^>]*>([^<]{20,})</link>|<link[^/]*/?>([^<]{20,})', str(item))
                link = (link_m.group(1) or link_m.group(2) or '').strip() if link_m else ''
                if not link:
                    continue
                matched = match_broad(title_clean) or match_industry(title_clean)
                if not matched:
                    continue
                seen_titles.add(title_clean)
                date_str = _parse_rss_date(pub) if pub else ''
                results.append(_make('Google News (промышленность)', 'gnews_industry',
                                     title_clean, link, date_str, matched))
        except Exception as e:
            print(f'    Google News industry [{query}]: {e}')
        time.sleep(0.3)
    print(f'    Google News (промышленность): {len(results)} статей')
    return results


INDUSTRY_SOURCES = [
    ('Норникель (новости)',       scrape_nornickel),
    ('РусГидро (новости)',        scrape_rushydro),
    ('Норникель (закупки)',       scrape_nornickel_tenders),
    ('РусГидро (закупки)',        scrape_rushydro_tenders),
    ('Google News (пром-сть)',    scrape_google_news_industry),
]


def scrape_all_industry() -> list:
    """Запускает все промышленные источники, помечает результаты флагом industry=True."""
    all_results = []
    for name, fn in INDUSTRY_SOURCES:
        print(f'  [Пром] {name}...')
        try:
            items = fn()
            for it in items:
                it['industry'] = True
            print(f'    Найдено: {len(items)} объектов')
            all_results.extend(items)
        except Exception as e:
            print(f'    Ошибка: {e}')
        time.sleep(1)
    return all_results


WEB_SOURCES = [
    # Региональные агрегаторы
    ('Google News (регионы)',  scrape_google_news_rss),
    ('Строительная газета',   scrape_stroygaz_rss),
    ('АНКБ',                  scrape_ancb),
    # Приоритет: Воронеж
    ('Горком36 (Воронеж)',    scrape_gorcom36),
    # Тематические сайты
    ('Коммерсантъ (регионы)', scrape_kommersant),
    ('Абирег',                scrape_abireg),
    ('Global52',              scrape_global52),
    ('ИнвестПроекты',         scrape_investprojects),
    ('Строители.РФ',          scrape_stroiteli_rf),
    ('АРД Эксперт',           scrape_ardexpert),
    ('РБК Недвижимость',      scrape_realty_rbc),
    # Тендеры
    ('ОТС.ру (тендеры)',      scrape_otc),
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
