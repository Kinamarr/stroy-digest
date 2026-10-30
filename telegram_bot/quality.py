"""
Контроль качества дайджеста — последний шаг перед публикацией.

prefilter() — до дозагрузки текстов (чтобы не тратить на мусор бюджет времени):
  • новости старше MAX_AGE_DAYS и с датой из будущего — выбрасываем;
  • без даты (заголовки со страниц сайтов) — только если ссылки не было в прошлых выпусках;
  • зарубежные новости и криминал — выбрасываем;
  • «Общее» (объект не распознан) — только при явных признаках стройки/тендера;
  • склейка дублей между источниками по похожести заголовков.

finalize() — после дозагрузки:
  • чистим HTML-остатки, обрезаем текст по концу предложения;
  • сортируем: свежие выше, внутри дня — самые полезные для продаж.
"""
import html
import re
from datetime import datetime, timedelta
from pathlib import Path

MAX_AGE_DAYS = 10
ARCHIVE_DAYS = 7          # сколько прошлых выпусков смотреть для новостей без даты

_LB = r'(?<![а-яёa-z0-9])'


def _re(words) -> re.Pattern:
    return re.compile(_LB + '(?:' + '|'.join(words) + ')', re.IGNORECASE)


# Вес признака → его основы. Чем выше сумма, тем полезнее новость для продаж гидроизоляции.
SCORE_RULES = [
    (3, _re(['гидроизол', 'водонепрониц', 'гидрошпон'])),
    (2, _re(['тендер', 'госконтракт', 'контракт', 'подрядчик', 'генподряд', 'аукцион',
             'фундамент', 'котлован', 'тоннел', 'очистн', 'паркинг', 'подземн',
             'разрешение на строительство', 'млрд'])),
    (1, _re(['млн', 'застройщик', 'проектн', 'экспертиз', 'бассейн', 'метро', 'резервуар',
             'монолит', 'кровл', 'подвал'])),
    (-1, _re(['благоустр', 'ремонт подъезд', 'покраск'])),
    (-2, _re(['скамейк', 'скамей', 'урн[ыа]?(?![а-яё])', 'клумб', 'новогодн', 'ёлк'])),
    # «суд» — с окончанием, чтобы не задеть «судостроение», «задержан» — не «задержка сроков»
    (-3, _re(['суд(?:а|у|ом|е|ы|ов|ебн[а-яё]*)?(?![а-яё])', 'арбитраж', 'хищен', 'похитил', 'похищ', 'задержан', 'уголовн', 'мошен', 'взятк',
              'арестова', 'обвиня', 'приговор', 'погиб', 'дтп'])),
]

FOREIGN_RE = re.compile(
    _LB + r'(?:казахстан|беларус|белорус|украин|узбекистан|кыргыз|киргиз|таджикистан|'
          r'армени|азербайджан|вьетнам|лаос(?:е|а|у)?(?![а-яё])|монголи|оккупант|'
          r'минск(?:е|а|у)?(?![а-яё])|алматы|астан[аеуы](?![а-яё])|ташкент|бишкек|ереван|баку(?![а-яё]))',
    re.IGNORECASE)

# «Общее» оставляем только с такими признаками: тендер, подрядчик, крупная сумма, гидроизоляция
GENERAL_KEEP_MIN_SCORE = 2


def score(text: str) -> int:
    return sum(w for w, pat in SCORE_RULES if pat.search(text))


def _cutoff() -> str:
    return (datetime.now() - timedelta(days=MAX_AGE_DAYS)).strftime('%Y-%m-%d')


def _past_links(archive_dir: Path) -> set:
    """Ссылки из прошлых выпусков — чтобы заголовки без даты не повторялись каждый день."""
    if not archive_dir or not archive_dir.exists():
        return set()
    today = datetime.now().strftime('%Y-%m-%d')
    files = sorted((f for f in archive_dir.glob('????-??-??.html') if f.stem < today), reverse=True)
    links = set()
    for f in files[:ARCHIVE_DAYS]:
        links.update(html.unescape(h) for h in re.findall(r'href="(https?://[^"]+)"', f.read_text(encoding='utf-8')))
    return links


# ── Дубли между источниками ───────────────────────────────────────────────────
def _stems(text: str) -> frozenset:
    first = text.split('\n', 1)[0].lower()
    return frozenset(w[:6] for w in re.findall(r'[а-яёa-z0-9]{4,}', first))


def _dedupe(items: list) -> list:
    """Одна новость из разных источников: совпадает ссылка или ≥60% значимых слов заголовка,
    причём среди общих есть редкие слова (место, название, фамилия) — иначе «станцию метро
    планируют построить» в Кудрово и в Нижнем склеились бы. Оставляем версию с самым длинным текстом."""
    items = sorted(items, key=lambda it: len(it['text']), reverse=True)
    all_stems = [_stems(it['text']) for it in items]
    df = {}
    for s in all_stems:
        for w in s:
            df[w] = df.get(w, 0) + 1
    rare = lambda w: df[w] <= 3          # встречается в 1–3 заголовках: место, название, фамилия
    kept, kept_stems, index, links = [], [], {}, set()
    for it, s in zip(items, all_stems):
        link = it.get('link', '')
        if link and link in links:
            continue
        dup = False
        if len(s) >= 4:
            candidates = {i for w in s for i in index.get(w, ())}
            for i in candidates:
                o = kept_stems[i]
                shared = s & o
                ratio = len(shared) / min(len(s), len(o))
                n_rare = sum(map(rare, shared))
                if ratio >= 0.6 and (n_rare >= 2 or (n_rare >= 1 and ratio >= 0.75)):
                    dup = True
                    break
        if dup:
            continue
        if link:
            links.add(link)
        if len(s) >= 4:
            for w in s:
                index.setdefault(w, []).append(len(kept))
        kept.append(it)
        kept_stems.append(s)
    return kept


def prefilter(items: list, archive_dir: Path = None) -> list:
    cutoff = _cutoff()
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    past = _past_links(archive_dir)
    stats = {'старые': 0, 'повтор из прошлых выпусков': 0, 'зарубежные': 0, 'нерелевантные': 0}
    out = []
    for it in items:
        date = it.get('date') or ''
        if date > tomorrow:                       # кривая дата из будущего — считаем неизвестной
            it['date'] = date = ''
        if date and date < cutoff:
            stats['старые'] += 1
            continue
        if not date and it.get('link') in past:
            stats['повтор из прошлых выпусков'] += 1
            continue
        text = it['text']
        if FOREIGN_RE.search(text):
            stats['зарубежные'] += 1
            continue
        it['score'] = score(text)
        if it['score'] <= -2 or (it.get('category') == 'Общее' and it['score'] < GENERAL_KEEP_MIN_SCORE):
            stats['нерелевантные'] += 1
            continue
        out.append(it)
    before = len(out)
    out = _dedupe(out)
    print('  Отсеяно: ' + ', '.join(f'{k} {v}' for k, v in stats.items())
          + f', дубли {before - len(out)}. Осталось {len(out)} из {len(items)}')
    return out


# ── Текст ─────────────────────────────────────────────────────────────────────
_SENT_END = re.compile(r'[.!?…»")](?=\s|$)')


def trim_to_sentence(text: str, min_len: int = 60) -> str:
    """Текст, обрезанный посередине («через железнодорожные п»), — до конца последнего предложения."""
    text = text.rstrip()
    if not text or text[-1] in '.!?…»")':
        return text
    ends = [m.end() for m in _SENT_END.finditer(text)]
    head = text.split('\n', 1)[0]
    ends = [e for e in ends if e >= max(min_len, len(head))]
    return text[:ends[-1]] if ends else text


def clean_text(text: str) -> str:
    text = html.unescape(html.unescape(text or ''))
    text = re.sub(r'<[^>]{1,200}>', ' ', text)
    text = re.sub(r'[ \t\xa0]+', ' ', text)
    text = re.sub(r' *\n[ \n]*', '\n', text).strip()
    # Первая строка из одних эмодзи («🤖», «⚡️») — не заголовок
    lines = text.split('\n')
    while len(lines) > 1 and not re.search(r'[A-Za-zА-Яа-яЁё0-9]', lines[0]):
        lines.pop(0)
    text = '\n'.join(lines)
    return trim_to_sentence(text)


def finalize(items: list) -> list:
    for it in items:
        it['text'] = clean_text(it['text'])
        it['score'] = score(it['text'])
    items.sort(key=lambda it: (it.get('date') or '', it['score']), reverse=True)
    return items
