"""Закупки с открытых торговых площадок по профилю АКВАСТОП → tenders.json.

Только открытые страницы поиска, без входа и обхода защит; запросы к одной площадке идут
по очереди с паузой. Запуск: python tenders.py
"""
import concurrent.futures as cf
import html
import json
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

OUT = Path(__file__).parent / 'tenders.json'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                         'Chrome/130.0 Safari/537.36', 'Accept-Language': 'ru'}
PAUSE = 3

# Намеренно НЕ собираем: B2B-Center (капча, «регламент не допускает использование ботов», 24.11.2026)
# ЭТП ГПБ (robots.txt закрывает поиск: Disallow: /*?) и ЭТП ЛСР (блокирует небраузерные запросы). Для них — регистрация поставщика и уведомления.

# Прямой спрос на материал
Q_DIRECT = ['гидрошпонка', 'гидроизоляционная шпонка', 'ватерстоп', 'аквастоп', 'гидроизоляция',
            'гидроизоляционные материалы', 'деформационный шов', 'герметизация швов', 'инъектирование']
# Объекты, где наш материал нужен
Q_OBJECT = ['строительство подземный паркинг', 'подземная автостоянка', 'строительство очистных сооружений',
            'реконструкция очистных сооружений', 'строительство резервуаров', 'насосная станция строительство',
            'гидротехнические сооружения', 'реконструкция плотины', 'капитальный ремонт плотины', 'гидроузел',
            'строительство тоннеля', 'метрополитен строительство', 'строительство бассейна',
            'строительство школы', 'строительство больницы', 'строительство поликлиники',
            'монолитные работы', 'устройство фундамента', 'капитальный ремонт моста']

P_HOT = re.compile(r'шпонк|ватерстоп|waterstop|аквастоп|деформационн\w+\s+(?:\w+\s+)?шв?о?в|деформационн\w+\s+шов', re.I)
P_WARM = re.compile(r'гидроизол|инъе(?:ктир|кцион)|герметизац\w+\s+(?:\w+\s+){0,2}шв', re.I)
P_WORK = re.compile(r'строител|реконструкц|капитальн\w+ ремонт|монолит|смр\b|возведен|устройств|проектн|изыскат|'
                    r'подземн|проектирован|генеральн\w+ подряд', re.I)
P_OBJ = re.compile(r'хвостохранил|дренаж|котлован|подземн|паркинг|автостоянк|очистн|резервуар|насосн|\bкнс\b|гидротехн|плотин|дамб|гидроуз|'
                   r'водохранил|\bгэс\b|тоннел|метро|бассейн|школ|больни|поликлин|медицинск|фундамент|мост|'
                   r'путепровод|водовод|коллектор|водоснабж|канализ|берегоукреп|шлюз|монолит|жило\w+ дом', re.I)
P_DAM = re.compile(r'хвостохранил|плотин|дамб|гидроуз|гидротехн|водохранил|\bгэс\b|водосброс|шлюз', re.I)
P_ROOF = re.compile(r'кровл|кровель|крыш', re.I)
P_PROJ = re.compile(r'проектн|изыскат|проектирован|\bпир\b|\bпсд\b', re.I)
# не стройка и не наш материал
P_NOISE = re.compile(r'продукт\w+ питан|охран\w+ (?:объект|услуг)|уборк|клининг|страхован|вывоз|\bтко\b|медицинск\w+ (?:издел|оборуд)|'
                     r'шовн\w+ материал|хирург|лекарств|мебел|питани[ея]|канцеляр|обслуживани|оценк\w+ имуществ|'
                     r'аттестац|утилизац|картридж|спецодежд|аренд|продаж|реализаци\w+ имуществ|экскурс|'
                     r'шпон(?:а|у|ом|ки-затыч)|шпонк\w* (?:призмат|сегмент|гост 23360)|строительн\w+ контрол\w+ аттест|'
                     r'промывк|дератизац|озеленен|поставк\w+ (?:автомоб|насос|электро|кабел|труб|топлив|хими|реагент)|'
                     r'комплект\w* |компьютер|программн\w+ обесп|услуг\w+ по лабор|периодическ|'
                     r'^(?:\S+\s+){0,3}?[«"]?поставка|^оборудование для|^оказание услуг по (?:перевозк|обращен|лаборат|производству стац)|'
                     r'поставк\w+,? (?:расстанов|сейф|систем|и монтаж)|конденсатор|гидрант|пользован\w+ недр|строительн\w+ контрол|'
                     r'тепло(?:вой|вых|вая) сет|тепловой изол|теплотрасс|моэк|вентиляци|электроосвещ|видеонаблюд|отделочн\w+ работ|'
                     r'автомобильн\w+ дорог\w* (?:с\.|по улиц)|^снос|работ по сносу|резинов\w+ крошк|временн\w+ дорог|хоккейн|'
                     r'информационной стел|опор\w+ вл|пластовой вод|металлодетект|строительных лесов|технологической обвязк|'
                     r'закупка в области|улично-дорожн|сжатого воздуха|отходов строит|инженерных сетей\) здания|'
                     r'мониторинг подземных вод|переустройство сооружений связи|теплоснабж|тепловодоснабж|теплогидроизол|проб подземных|запасов пресных|'
                     r'подземными горны|содержани\w+ искусств|земельно-правов|внутренней отделк|автоматизац|кл-10|электроснабжен|дороги? (?:в д\.|по ул)|'
                     r'сизо|кабельная эстакада|газопровод надзем', re.I)

MONTHS = {m: i + 1 for i, m in enumerate(['янв', 'фев', 'мар', 'апр', 'ма', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'])}


def clean(s) -> str:
    return re.sub(r'\s+', ' ', html.unescape(str(s or ''))).strip()


def node(el) -> str:
    return clean(el.get_text(' ')) if el else ''


def to_iso(s: str) -> str:
    s = clean(s)
    if m := re.search(r'(\d{2})\.(\d{2})\.(\d{4})', s):
        return f'{m[3]}-{m[2]}-{m[1]}'
    if m := re.search(r'(\d{4})-(\d{2})-(\d{2})', s):
        return m[0]
    if m := re.search(r'(\d{1,2})\s+([а-я]+)\s+(\d{4})', s.lower()):
        for stem in sorted(MONTHS, key=len, reverse=True):
            if m[2].startswith(stem):
                return f'{m[3]}-{MONTHS[stem]:02d}-{int(m[1]):02d}'
    return ''


class Blocked(Exception):
    """Площадка попросила остановиться — больше её не трогаем."""


def get(cli, url):
    for attempt in range(2):
        try:
            r = cli.get(url)
        except httpx.HTTPError:
            if attempt:
                raise
            time.sleep(5)
            continue
        if r.status_code in (401, 403, 429) or 'captcha' in str(r.url) \
                or re.search('Вы заблокированы|использование ботов', r.text[:20000]):
            raise Blocked(f'отказ площадки ({r.status_code})')
        r.raise_for_status()
        time.sleep(PAUSE)
        return r
    return None


def soup(r):
    s = BeautifulSoup(r.text, 'lxml')
    for x in s(['script', 'style', 'svg']):
        x.decompose()
    return s


def row(platform, num, title, link, customer='', region='', price='', published='', deadline='', kind=''):
    region = re.sub(r'^\d{2}\.\s*', '', clean(region))
    price = clean(price)
    if not re.search(r'[1-9]', price):
        price = ''
    return dict(platform=platform, num=clean(num), title=clean(title), link=link, customer=clean(customer),
                region=region, price=price, published=published, deadline=deadline, kind=clean(kind))


def fabrikant(cli, q, page):
    s = soup(get(cli, f'https://www.fabrikant.ru/procedure/search?query={quote(q)}'
                      f'&page_limit=40&page_number={page + 1}'))
    seen = set()
    for a in s.select('a[data-slot=anchor][href*="procedure"]'):
        if not a.get_text(strip=True) or a['href'] in seen or len(a.get_text(strip=True)) < 12:
            continue
        box = a.find_parent('div', class_=re.compile('justify-between'))
        if not box:
            continue
        seen.add(a['href'])
        t = node(box)

        def f(label, stop):
            m = re.search(label + r'\s+(.+?)(?=\s+(?:' + stop + r')|$)', t)
            return m[1] if m else ''
        customer = (f('Заказчик', 'Дата публикации|Организатор|Дата окончания')
                    or f('Организатор', 'Заказчик|Дата публикации|Дата окончания'))
        pm = re.search(r'([\d ]{4,}(?:\s*,\s*\d{2})?)\s*RUB', t)
        link = urljoin('https://www.fabrikant.ru', a['href'])
        yield row('Фабрикант', link.rstrip('/').split('/')[-1], node(a), link, customer, '',
                  re.sub(r'\s+,\s+', ',', pm[1]) + ' руб.' if pm else '',
                  to_iso(f('Дата публикации', 'Дата окончания|Осталось|Цена')),
                  to_iso(f('Дата окончания приёма заявок', 'Осталось|Цена')))


def roseltorg(cli, q, page):
    s = soup(get(cli, f'https://www.roseltorg.ru/procedures/search?query_field={quote(q)}&status%5B%5D=0'
                      + (f'&page={page}' if page else '')))
    for c in s.select('.search-results__item'):
        a = c.select_one('.search-results__subject a')
        if not a or 'Прием' not in node(c.select_one('.search-results__status')):
            continue
        cust = re.sub(r'^Организатор\s*', '', node(c.select_one('.search-results__customer a')))
        pr = c.select_one('.search-results__sum .desktop')
        yield row('Росэлторг', c.get('data-feature-favorite-lots-procedure-number', ''), node(a),
                  urljoin('https://www.roseltorg.ru', a['href']), cust, node(c.select_one('.search-results__region')),
                  re.sub(r'\s+,', ',', node(pr)), '', to_iso(node(c.select_one('.search-results__time'))),
                  node(c.select_one('.search-results__type')))


def tektorg(cli, q, page):
    t = get(cli, f'https://www.tektorg.ru/procedures?name={quote(q)}' + (f'&page={page + 1}' if page else '')).text
    hrefs = {m[2]: 'https://www.tektorg.ru' + m[1] for m in re.finditer(r'href="(/[^"]*procedures/(\d+))"', t)}
    d = json.loads(re.search(r'__NEXT_DATA__"[^>]*>(.*?)</script>', t, re.S)[1])
    for x in d['props']['pageProps']['initialReduxState']['listingProcedures']['data'] or []:
        if not re.search(r'при[её]м', x.get('statusName') or '', re.I):
            continue
        dt = x.get('dates') or {}
        geo = x.get('geoPoints') or []
        reg = geo[0].get('name', '') if geo and isinstance(geo[0], dict) else ''
        yield row('ТЭК-Торг', x.get('registryNumber') or x.get('remoteId'), x['title'],
                  hrefs.get(str(x['id']), 'https://www.tektorg.ru/procedures?name=' + quote(str(x.get('registryNumber') or ''))), x.get('organizerName'), reg,
                  x.get('sumPrice') or '', to_iso(dt.get('datePublished')), to_iso(dt.get('dateEndRegistration')),
                  x.get('typeName'))


def mosrenstroy(cli, _q, _page):
    for x in get(cli, 'https://tender.gk-mrs.ru/get_tenders.php').json():
        if x.get('status') != 'Действующий':
            continue
        yield row('Мосренстрой', x['number'], html.unescape(x['theme']), 'https://gk-mrs.ru/tenders.php',
                  'ГК «Мосренстрой»', 'Москва', '', to_iso(x['date_from']), to_iso(x['date_to']))


def rushydro(cli, _q, page):
    """Сайт закупок группы РусГидро. Срока подачи в списке нет (только в извещении на ЕИС)."""
    s = soup(get(cli, 'https://zakupki.rushydro.ru/Procurement/PurchaseInformation?sectionId=4&isArchive=False'
                      f'&page={page + 1}&pageSize=50'))
    for tr in s.select('table tbody tr'):
        td = tr.select('td')
        a = tr.find('a', href=re.compile('PurchaseInformation/Update'))
        if not a or len(td) < 7 or 'Сбор заявок' not in node(td[6]):
            continue
        head = node(td[0])
        title = re.sub(r'\s*\(Лот № [^)]*\)\s*$', '', node(td[2].find('span')))
        yield row('РусГидро', head.split()[0], title,
                  urljoin('https://zakupki.rushydro.ru', a['href'].split('?')[0]) + '?sectionId=4',
                  node(td[3]), '', node(td[5]).replace('Российский рубль', 'руб.'), to_iso(head), '')


SOURCES = {'РусГидро': rushydro, 'Фабрикант': fabrikant, 'Росэлторг': roseltorg,
           'ТЭК-Торг': tektorg, 'Мосренстрой': mosrenstroy}


# компании, чьи закупки ищем по имени: площадка → {запрос: фрагмент названия заказчика}
COMPANY = {'Фабрикант': {'ЮГК': 'югк', 'южуралзолото': 'южуралзолото'}}
PAGES = {'РусГидро': 6, 'Фабрикант': 3, 'Росэлторг': 5, 'ТЭК-Торг': 5}      # предел страниц на один запрос


def grade(r):
    """→ (группа, приоритет) или None, если закупка не наша."""
    t = r['title']
    if P_HOT.search(t) and not re.search(r'шпонк\w* (?:призмат|сегмент)|вал\w*\b.*шпонк', t, re.I):
        return 'Шпонки и деформационные швы', 1
    if P_WARM.search(t) and not re.search(r'облицовк|межпанельн|окраск|стоматолог|автомойк|медицинск|теплогидроизол', t, re.I):
        return ('Кровельная гидроизоляция', 4) if P_ROOF.search(t) else ('Гидроизоляция и инъектирование', 2)
    if P_NOISE.search(t) or not P_OBJ.search(t) or not P_WORK.search(t):
        return None
    if P_DAM.search(t):
        return 'Плотины и гидросооружения', 2
    if P_PROJ.search(t) and not re.search(r'строительно|выполнен\w+ строит|под ключ', t, re.I):
        return 'Проектирование объектов', 3
    return 'Строительство объектов', 3


def collect(name, fn):
    rows, err = [], ''
    queries = ['-'] if name in ('Мосренстрой', 'РусГидро') else Q_DIRECT + Q_OBJECT + list(COMPANY.get(name, {}))
    ok = pages = 0
    with httpx.Client(headers=HEADERS, timeout=45, follow_redirects=True) as cli:
        for q in queries:
            try:
                got = set()
                for page in range(PAGES.get(name, 1)):
                    new = [r for r in fn(cli, q, page) if r['link'] + r['num'] not in got]
                    if name == 'Фабрикант':
                        new = [r for r in new if r['deadline'] >= date.today().isoformat()]
                    who = COMPANY.get(name, {}).get(q)
                    if who:
                        new = [r for r in new if re.search(r'\b' + who + r'\b', r['customer'], re.I)]
                    if not new:                              # открытых на странице нет — дальше не листаем
                        break
                    got |= {r['link'] + r['num'] for r in new}
                    rows += new
                    pages += 1
                ok += 1
            except Blocked as e:
                err = str(e)
                break
            except Exception as e:                           # один сбойный запрос не валит площадку
                err = f'{type(e).__name__}: {e}'[:120]
    print(f'{name}: страниц с открытыми закупками {pages}', flush=True)
    return name, rows, ok, len(queries), err


def main():
    today = date.today().isoformat()
    old = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {}
    known = {r['key']: r for r in old.get('rows', [])}
    result, stats, failed = {}, [], set()
    with cf.ThreadPoolExecutor(len(SOURCES)) as ex:
        for name, rows, ok, total, err in ex.map(lambda kv: collect(*kv), SOURCES.items()):
            uniq = {}
            for r in rows:
                uniq[r['num'] or r['link']] = r
            kept = 0
            for k, r in uniq.items():
                g = grade(r)
                if not g or (r['deadline'] < today and name != 'РусГидро'):
                    continue
                r['group'], r['priority'] = g
                r['key'] = f'{name}:{k}'
                r['first_seen'] = known.get(r['key'], {}).get('first_seen', today)
                result[r['key']] = r
                kept += 1
            if ok < total or not uniq:
                failed.add(name)
            stats.append(dict(platform=name, ok=ok, total=total, found=len(uniq), kept=kept, error=err))
            print(f'{name:12} запросов {ok}/{total}, найдено {len(uniq)}, наших и открытых {kept} {err}')
    # площадка частично не ответила → её ещё открытые закупки с прошлого раза не теряем
    for k, r in known.items():
        if k not in result and (r['platform'] in failed or r['platform'] not in SOURCES) and (r['deadline'] or '9999') >= today:
            result[k] = r
    # одна и та же закупка на двух площадках: одинаковый предмет и срок
    seen, rows = set(), []
    for r in sorted(result.values(), key=lambda r: (r['priority'], r['deadline'] or '9999', -len(r['customer'] + r['price']))):
        sig = (re.sub(r'\W', '', r['title'].lower()), r['deadline'],
               r['customer'] if len(r['title']) < 60 else '')
        if sig not in seen:
            seen.add(sig)
            rows.append(r)
    OUT.write_text(json.dumps(dict(updated=datetime.now().isoformat(timespec='minutes'), stats=stats, rows=rows),
                              ensure_ascii=False, indent=1), encoding='utf-8')
    print('Итого:', len(rows))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
