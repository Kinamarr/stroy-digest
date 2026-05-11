import re
import time
import sys
from datetime import datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from keywords import CHANNELS, KEYWORDS, KEYWORD_CATEGORIES
from cities import CITIES

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}

PHONE_RE = re.compile(
    r'(?:\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'
)
EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')

# Конкретный объект строительства — что именно строится
OBJECT_KEYWORDS = set(kw for kws in KEYWORD_CATEGORIES.values() for kw in kws)

# Строительное действие — стройка активно идёт
# Основы слов — ловят все падежи и формы русского языка
# 'строит' совпадает с: строит, строится, строительство, строительства, строителей...
ACTION_STEMS = {
    'строит',     # строит/строится/строительство/строительства/строительству
    'возвод',     # возводят/возводится/возводили
    'возвед',     # возведут/возведение/возведён/возведена
    'постро',     # построят/построили/построен/построена/построено
    'приступ',    # приступили к строительству
    'котлован',   # котлован (точное слово)
    'монолит',    # монолитные работы/монолитный
    'стройплощ',  # стройплощадка
    'введ',       # введён/введена/введено в эксплуатацию
    'разрешени',  # разрешение на строительство
    'закладк',    # закладка камня
    'первый кам', # первый камень
    'ввод в экс', # ввод в эксплуатацию
    'сдан',       # сдан/сдана в эксплуатацию
    'застраива',  # застраивается/застраивают
    'реконстру',  # реконструируется/реконструкция объекта
    'капитальн',  # капитальный ремонт/строительство
}


def load_channels(base_dir: Path) -> list:
    channels_file = base_dir / 'channels.txt'
    if channels_file.exists():
        lines = channels_file.read_text(encoding='utf-8').splitlines()
        channels = [l.strip() for l in lines if l.strip() and not l.strip().startswith('#')]
        if channels:
            return channels
    return CHANNELS


def find_contacts(text):
    phones = list(dict.fromkeys(PHONE_RE.findall(text)))
    emails = list(dict.fromkeys(EMAIL_RE.findall(text)))
    return phones, emails


def find_cities(text):
    text_lower = text.lower()
    return [city for city in CITIES if city.lower() in text_lower]


def get_category(matched_kws):
    for category, kws in KEYWORD_CATEGORIES.items():
        for kw in matched_kws:
            if kw in kws:
                return category
    return 'Общее'


def match_keywords(text: str) -> list:
    """
    Новость проходит только если есть ОДНОВРЕМЕННО:
    1. Конкретный объект (ЖК, завод, склад, школа...)
    2. Строительное действие (строится, котлован, введён в эксплуатацию...)
    """
    t = text.lower()

    has_object = any(kw in t for kw in OBJECT_KEYWORDS)
    has_action = any(stem in t for stem in ACTION_STEMS)

    if not has_object or not has_action:
        return []

    matched_obj = [kw for kw in OBJECT_KEYWORDS if kw in t]
    matched_act = [s for s in ACTION_STEMS if s in t]
    return (matched_obj + matched_act)[:6]


def scrape_channel(channel: str, pages: int = 3) -> list:
    channel_name = channel.lstrip('@')
    results = []
    url = f'https://t.me/s/{channel_name}'

    for page in range(pages):
        try:
            resp = httpx.get(url, headers=HEADERS, timeout=30,
                             follow_redirects=True, verify=False)
            resp.raise_for_status()
        except Exception as e:
            print(f'  Ошибка загрузки {channel}: {e}')
            break

        soup = BeautifulSoup(resp.text, 'html.parser')
        wraps = soup.find_all('div', class_='tgme_widget_message_wrap')

        if not wraps:
            break

        min_id = None
        for wrap in wraps:
            text_el = wrap.find('div', class_='tgme_widget_message_text')
            if not text_el:
                continue
            text = text_el.get_text(separator='\n').strip()
            if not text:
                continue

            matched = match_keywords(text)
            if not matched:
                continue

            link_el = wrap.find('a', class_='tgme_widget_message_date')
            link = link_el['href'] if link_el else f'https://t.me/{channel_name}'

            if link_el:
                try:
                    msg_id = int(link.rstrip('/').split('/')[-1])
                    if min_id is None or msg_id < min_id:
                        min_id = msg_id
                except ValueError:
                    pass

            date_el = wrap.find('time')
            date_str = date_el.get('datetime', '')[:10] if date_el else ''

            phones, emails = find_contacts(text)
            cities = find_cities(text)
            category = get_category(matched)

            results.append({
                'channel': channel,
                'channel_name': channel_name,
                'text': text,
                'link': link,
                'date': date_str,
                'keywords': matched[:4],
                'category': category,
                'cities': cities,
                'phones': phones,
                'emails': emails,
            })

        if min_id and page < pages - 1:
            url = f'https://t.me/s/{channel_name}?before={min_id}'
            time.sleep(1.5)
        else:
            break

    return results


CAT_ICON = {
    'Жилой дом / ЖК':      '🏗',
    'Завод / Производство': '🏭',
    'Офис / ТЦ':            '🏢',
    'Гостиница / Отель':    '🏨',
    'Склад / Логистика':    '📦',
    'Социальный объект':    '🏥',
    'Спортивный объект':    '🏟',
    'Дорога / Мост':        '🌉',
    'Метро / Подземное':    '🚇',
}

CAT_COLOR = {
    'Жилой дом / ЖК':      '#3fb950',
    'Завод / Производство': '#f0883e',
    'Офис / ТЦ':            '#58a6ff',
    'Гостиница / Отель':    '#f78166',
    'Склад / Логистика':    '#bc8cff',
    'Социальный объект':    '#39d353',
    'Спортивный объект':    '#e3b341',
    'Дорога / Мост':        '#79c0ff',
    'Метро / Подземное':    '#d2a8ff',
}


def generate_html(all_results: list, archive_links: list) -> str:
    date_str = datetime.now().strftime('%d.%m.%Y')
    all_cities   = sorted({city for r in all_results for city in r['cities']})
    all_channels = sorted({r['channel'] for r in all_results})
    all_categories = sorted({r['category'] for r in all_results})
    total = len(all_results)

    cards_html = ''
    for i, item in enumerate(all_results):
        card_id = f"c{i}"

        cities_html = ''.join(
            f'<span class="tag city">{c}</span>' for c in item['cities']
        )
        kw_html = ''.join(
            f'<span class="tag kw">{k}</span>' for k in item['keywords'][:3]
        )

        contacts_html = ''
        for p in item['phones']:
            contacts_html += f'<a href="tel:{p}" class="contact phone">📞 {p}</a>'
        for e in item['emails']:
            contacts_html += f'<a href="mailto:{e}" class="contact email">✉️ {e}</a>'

        cat_slug = re.sub(r'[^a-zA-Zа-яёА-ЯЁ]', '', item['category'])
        cities_data = ','.join(item['cities'])
        preview = item['text'][:700] + ('...' if len(item['text']) > 700 else '')
        has_contacts = bool(item['phones'] or item['emails'])
        contact_block = f'<div class="contacts">{contacts_html}</div>' if has_contacts else ''
        contact_marker = '<span class="has-contacts-badge">📋 Контакты</span>' if has_contacts else ''

        cat_color = CAT_COLOR.get(item['category'], '#58a6ff')
        cat_icon  = CAT_ICON.get(item['category'], '🏗')

        cards_html += f'''
        <div class="card {'has-contacts' if has_contacts else ''}"
             id="{card_id}"
             data-channel="{item['channel']}"
             data-category="{item['category']}"
             data-cities="{cities_data}"
             data-contacts="{'1' if has_contacts else '0'}"
             style="border-left-color:{cat_color}">
          <div class="card-header">
            <span class="cat-badge" style="color:{cat_color};border-color:{cat_color}22;background:{cat_color}18">{cat_icon} {item['category']}</span>
            <span class="channel-badge">{item['channel']}</span>
            {contact_marker}
            <span class="date">{item['date']}</span>
            <button class="fav-btn" onclick="toggleFav(this,'{card_id}')" title="В избранное">☆</button>
          </div>
          {contact_block}
          <div class="tags">{cities_html}{kw_html}</div>
          <p class="card-text">{preview}</p>
          <a href="{item['link']}" target="_blank" rel="noopener" class="source-link">→ Открыть источник</a>
        </div>'''

    channel_opts = ''.join(f'<option value="{c}">{c}</option>' for c in all_channels)
    city_opts    = ''.join(f'<option value="{c}">{c}</option>' for c in all_cities)
    cat_opts     = ''.join(
        f'<option value="{c}">{CAT_ICON.get(c,"")} {c}</option>'
        for c in all_categories
    )

    archive_html = ''
    if archive_links:
        items = ''.join(f'<a href="{n}" class="arch-link">{l}</a>' for n, l in archive_links)
        archive_html = f'<div class="archive-bar">📁 Архив: {items}</div>'

    return f'''<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Стройдайджест — {date_str}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}}

/* ── Шапка ── */
.header{{background:#010409;border-bottom:1px solid #21262d;padding:14px 24px;position:sticky;top:0;z-index:200;display:flex;align-items:center;justify-content:space-between;gap:16px}}
.header h1{{font-size:1.2rem;font-weight:700;color:#f0f6fc;letter-spacing:-.3px}}
.header .sub{{font-size:.78rem;color:#8b949e}}
.archive-bar{{background:#010409;border-bottom:1px solid #21262d;padding:6px 24px;font-size:.75rem;display:flex;gap:10px;flex-wrap:wrap;color:#8b949e}}
.arch-link{{color:#58a6ff;text-decoration:none}}.arch-link:hover{{text-decoration:underline}}

/* ── Табы ── */
.tabs{{background:#0d1117;border-bottom:1px solid #21262d;display:flex;padding:0 24px;position:sticky;top:49px;z-index:199}}
.tab-btn{{padding:10px 18px;border:none;background:none;cursor:pointer;font-size:.85rem;color:#8b949e;border-bottom:2px solid transparent;margin-bottom:-1px;transition:color .15s,border-color .15s}}
.tab-btn.active{{color:#f0f6fc;border-bottom-color:#f78166;font-weight:600}}
.tab-btn:hover{{color:#c9d1d9}}

/* ── Фильтры ── */
.filters{{background:#161b22;border-bottom:1px solid #21262d;padding:10px 24px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;position:sticky;top:93px;z-index:198}}
.filters label{{font-size:.74rem;color:#8b949e}}
.filters select{{padding:5px 8px;border:1px solid #30363d;border-radius:6px;font-size:.78rem;background:#21262d;color:#c9d1d9;cursor:pointer}}
.filters select:focus{{outline:none;border-color:#58a6ff}}
.filters button{{padding:5px 12px;background:#21262d;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;cursor:pointer;font-size:.78rem;transition:background .15s}}
.filters button:hover{{background:#30363d;color:#f0f6fc}}
.fcontacts{{display:flex;align-items:center;gap:5px;font-size:.78rem;color:#8b949e;cursor:pointer}}
.fcontacts input{{accent-color:#3fb950}}

/* ── Статистика ── */
.stats{{padding:8px 24px;font-size:.76rem;color:#8b949e}}.stats b{{color:#c9d1d9}}

/* ── Сетка ── */
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(380px,1fr));gap:12px;padding:12px 24px 48px}}

/* ── Карточка ── */
.card{{background:#161b22;border-radius:8px;padding:14px 16px;border:1px solid #21262d;border-left:3px solid #58a6ff;transition:border-color .15s,box-shadow .15s}}
.card:hover{{border-color:#30363d;box-shadow:0 4px 20px rgba(0,0,0,.4)}}
.card-header{{display:flex;align-items:center;gap:6px;margin-bottom:9px;flex-wrap:wrap}}

/* Бейдж категории */
.cat-badge{{font-size:.72rem;padding:3px 9px;border-radius:20px;font-weight:600;border:1px solid;white-space:nowrap}}
.channel-badge{{background:#21262d;color:#8b949e;font-size:.68rem;padding:2px 7px;border-radius:10px}}
.has-contacts-badge{{font-size:.66rem;background:#0d2818;color:#3fb950;border:1px solid #1a4731;padding:2px 7px;border-radius:10px;font-weight:600}}
.date{{margin-left:auto;font-size:.68rem;color:#6e7681;white-space:nowrap}}
.fav-btn{{background:none;border:none;font-size:1rem;cursor:pointer;color:#484f58;padding:0 2px;line-height:1;flex-shrink:0;transition:color .15s}}
.fav-btn.fav-active{{color:#e3b341}}
.fav-btn:hover{{color:#e3b341}}

/* Контакты */
.contacts{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;padding:8px 10px;background:#0d2818;border-radius:6px;border:1px solid #1a4731}}
.contact{{font-size:.78rem;padding:3px 9px;border-radius:5px;font-weight:600;text-decoration:none}}
.contact.phone{{background:#1a4731;color:#3fb950}}
.contact.email{{background:#1a3a5c;color:#58a6ff}}

/* Теги */
.tags{{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:9px}}
.tag{{font-size:.67rem;padding:2px 7px;border-radius:8px}}
.tag.city{{background:#1a3a5c;color:#79c0ff}}
.tag.kw{{background:#2d1f0e;color:#e3b341}}

/* Текст и ссылка */
.card-text{{font-size:.83rem;line-height:1.65;color:#8b949e;white-space:pre-wrap;word-break:break-word;margin-bottom:10px}}
.source-link{{font-size:.76rem;color:#58a6ff;text-decoration:none}}
.source-link:hover{{text-decoration:underline;color:#79c0ff}}

/* Пустое состояние */
.empty-state{{grid-column:1/-1;text-align:center;padding:80px 20px;color:#484f58}}
.empty-state .icon{{font-size:2.5rem;margin-bottom:10px}}
.empty-state p{{font-size:.9rem}}
.hidden{{display:none!important}}

/* Скроллбар */
::-webkit-scrollbar{{width:6px;height:6px}}
::-webkit-scrollbar-track{{background:#0d1117}}
::-webkit-scrollbar-thumb{{background:#30363d;border-radius:3px}}
::-webkit-scrollbar-thumb:hover{{background:#484f58}}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>🏗 Стройдайджест</h1>
    <div class="sub">{date_str} · {total} объектов · {len(all_channels)} каналов</div>
  </div>
</div>
{archive_html}

<div class="tabs">
  <button class="tab-btn active" id="tab-all" onclick="switchTab('all')">Все ({total})</button>
  <button class="tab-btn" id="tab-favs" onclick="switchTab('favs')">★ Избранное (<span id="fav-count">0</span>)</button>
</div>

<div class="filters">
  <div style="display:flex;align-items:center;gap:5px">
    <label>Тип объекта</label>
    <select id="fCat" onchange="applyFilter()">
      <option value="">Все типы</option>{cat_opts}
    </select>
  </div>
  <div style="display:flex;align-items:center;gap:5px">
    <label>Город</label>
    <select id="fCity" onchange="applyFilter()">
      <option value="">Все города</option>{city_opts}
    </select>
  </div>
  <div style="display:flex;align-items:center;gap:5px">
    <label>Канал</label>
    <select id="fCh" onchange="applyFilter()">
      <option value="">Все каналы</option>{channel_opts}
    </select>
  </div>
  <label class="fcontacts">
    <input type="checkbox" id="fContacts" onchange="applyFilter()">
    Только с контактами
  </label>
  <button onclick="resetFilter()">Сбросить</button>
</div>

<div class="stats">Показано: <b id="vis-count">{total}</b></div>

<div class="grid" id="grid">
{cards_html}
  <div class="empty-state hidden" id="empty-state">
    <div class="icon">🔍</div>
    <p>Ничего не найдено по выбранным фильтрам</p>
  </div>
</div>

<script>
var currentTab='all';
function loadFavs(){{return JSON.parse(localStorage.getItem('stroy_favs')||'{{}}');}}
function saveFavs(f){{localStorage.setItem('stroy_favs',JSON.stringify(f));}}
function toggleFav(btn,id){{
  var f=loadFavs();
  if(f[id]){{delete f[id];btn.textContent='☆';btn.classList.remove('fav-active');}}
  else{{f[id]=1;btn.textContent='★';btn.classList.add('fav-active');}}
  saveFavs(f);updateFavCount();
  if(currentTab==='favs')applyFilter();
}}
function updateFavCount(){{
  document.getElementById('fav-count').textContent=Object.keys(loadFavs()).length;
}}
function initFavs(){{
  var f=loadFavs();
  Object.keys(f).forEach(function(id){{
    var btn=document.querySelector('#'+id+' .fav-btn');
    if(btn){{btn.textContent='★';btn.classList.add('fav-active');}}
  }});
  updateFavCount();
}}
function switchTab(tab){{
  currentTab=tab;
  document.getElementById('tab-all').classList.toggle('active',tab==='all');
  document.getElementById('tab-favs').classList.toggle('active',tab==='favs');
  applyFilter();
}}
function applyFilter(){{
  var ch=document.getElementById('fCh').value;
  var cat=document.getElementById('fCat').value;
  var city=document.getElementById('fCity').value;
  var onlyCont=document.getElementById('fContacts').checked;
  var favs=loadFavs();
  var cards=document.querySelectorAll('.card');
  var visible=0;
  cards.forEach(function(c){{
    var ok=(!ch||c.dataset.channel===ch)
        &&(!cat||c.dataset.category===cat)
        &&(!city||c.dataset.cities.split(',').indexOf(city)!==-1)
        &&(!onlyCont||c.dataset.contacts==='1')
        &&(currentTab!=='favs'||favs[c.id]);
    c.classList.toggle('hidden',!ok);
    if(ok)visible++;
  }});
  document.getElementById('vis-count').textContent=visible;
  document.getElementById('empty-state').classList.toggle('hidden',visible>0);
}}
function resetFilter(){{
  ['fCh','fCat','fCity'].forEach(function(id){{document.getElementById(id).value='';}});
  document.getElementById('fContacts').checked=false;
  applyFilter();
}}
initFavs();
</script>
</body>
</html>'''


def main():
    import webbrowser

    if getattr(sys, 'frozen', False):
        base_dir = Path(sys.executable).parent
    else:
        base_dir = Path(__file__).parent

    channels = load_channels(base_dir)

    print(f'Запуск парсера — {datetime.now().strftime("%d.%m.%Y %H:%M")}')
    print(f'Каналов: {len(channels)}')
    all_results = []

    for channel in channels:
        print(f'  Читаю {channel}...')
        results = scrape_channel(channel)
        print(f'    Найдено: {len(results)} объектов')
        all_results.extend(results)
        time.sleep(2)

    print(f'\nВсего найдено: {len(all_results)} объектов')

    yadisk = Path(r'C:\Users\Masha\Yandex.Disk\Стройдайджест')
    out_dir = yadisk if yadisk.parent.exists() else base_dir / 'digests'
    out_dir.mkdir(exist_ok=True)

    date_str = datetime.now().strftime('%Y-%m-%d')
    existing = sorted(out_dir.glob('????-??-??.html'), reverse=True)
    archive_links = [(f.name, f.stem) for f in existing[:14]]

    if not all_results:
        print('Ничего не найдено — попробуй добавить каналы в channels.txt')
        input('\nНажми Enter чтобы закрыть...')
        return

    html = generate_html(all_results, archive_links)
    dated_file = out_dir / f'{date_str}.html'
    latest_file = out_dir / 'сегодня.html'
    dated_file.write_text(html, encoding='utf-8')
    latest_file.write_text(html, encoding='utf-8')

    print(f'Сохранено: {dated_file}')
    webbrowser.open(latest_file.as_uri())
    input('\nГотово! Нажми Enter чтобы закрыть...')


if __name__ == '__main__':
    main()
