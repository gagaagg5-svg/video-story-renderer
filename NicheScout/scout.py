"""Niche Scout 0.1 — local desktop research client, Python 3.10+, stdlib only."""
from __future__ import annotations
import csv
import io
import json
import math
import os
import queue
import re
import statistics
import threading
import urllib.error
import urllib.request
import webbrowser
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
ENDPOINT = 'https://mcp.vidiq.com/mcp'
ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'NicheScout'
LANGS = {'Польский': 'pl', 'Румынский': 'ro', 'Испанский': 'es', 'Английский': 'en'}
SEEDS = {
    'pl': 'historie z życia\nopowieści historyczne\nciekawostki o zwierzętach',
    'ro': 'povești de viață\npovești istorice\ncuriozități despre animale',
    'es': 'historias de vida\nhistorias para dormir\ncuriosidades de animales',
    'en': 'life stories\nhistorical sleep stories\nanimal facts',
}
METHOD = '''МЕТОД И ГРАНИЦЫ ВЫВОДОВ

1. Выбирайте язык и тему. Ищите недавние видео: исходный ориентир — 100 000 просмотров за последние 21 день. Порог можно менять для небольших рынков.
2. Смотрите на размер канала и текущий VPH. Средние просмотры в час с публикации — другая величина; программа не подменяет ими VPH vidIQ.
3. Выберите канал и проверьте последние длинные видео. Несколько успешных публикаций — полезный сигнал повторяемости; один хит — основание для дальнейшего исследования.
4. «Возраст канала» — дата регистрации. Он не равен возрасту проекта. Начало доступной выборки — только начало этой выборки, не первая публикация за всю историю.
5. Количество найденных конкурентов относится только к загруженной выборке. Малое число результатов не доказывает низкую конкуренцию или высокий спрос.
6. Язык заголовка и страна автора не раскрывают страны зрителей. Для румынского используется поиск фраз: язык заголовка НЕ подтверждён автоматически.
7. RPM вводится вручную как сценарий. Чужие фактические RPM, удержание, монетизация и география аудитории неизвестны. Длинное видео не гарантирует доход ×10.
8. Монетизация требует проверки YouTube. Достижение числовых порогов не означает одобрение партнёрской программы. Производство за 20 минут — чужое утверждение, не измеренный результат.
9. В методе «разведчиков» полезны отдельные подборки по языкам. Эта программа ищет по данным vidIQ, а не воспроизводит персональную главную YouTube. Создавать/покупать аккаунты, смотреть видео и ставить лайки для работы программы не требуется.
10. Данные и расчёты используются по условиям vidIQ и YouTube. Для самостоятельной аналитики через YouTube API могут потребоваться дополнительные разрешения. Локальная история автоматически очищается через 30 дней; это не заменяет разрешение на использование данных.

Рабочий цикл: найти кандидатов → проверить несколько каналов → изучить содержание → оценить стоимость производства → сделать оригинальные пробные публикации → оценить собственные результаты.
'''

def now():
    return datetime.now(UTC)

def stamp(value):
    if value is None or value == '':
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except (ValueError, OverflowError, OSError, TypeError):
        return None

def number(value):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        v = float(value)
        return v if math.isfinite(v) and v >= 0 else None
    except (ValueError, TypeError):
        return None

def seconds(value):
    n = number(value)
    if n is not None:
        return n
    m = re.fullmatch(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?', str(value))
    return sum(float(x or 0) * factor for x, factor in zip(m.groups(), (86400, 3600, 60, 1))) if m else None

def unwrap(payload):
    if isinstance(payload, list):
        return {'videos': payload}
    if not isinstance(payload, dict):
        raise ValueError('Ожидается JSON-объект или список видео.')
    if payload.get('isError'):
        raise RuntimeError('vidIQ вернул ошибку. Запрос не повторён; проверьте баланс и параметры.')
    if isinstance(payload.get('structuredContent'), dict):
        return payload['structuredContent']
    for item in payload.get('content', []):
        if item.get('type') == 'text':
            try:
                decoded = json.loads(item['text'])
                if isinstance(decoded, dict):
                    return decoded
            except (ValueError, KeyError):
                pass
    return payload

@dataclass
class Video:
    video_id: str
    title: str
    channel_id: str | None
    channel_title: str
    published: str | None
    views: float | None
    subscribers: float | None
    duration: float | None
    vph: float | None
    breakout: float | None
    language: str
    language_evidence: str
    topic: str
    source: str
    observed_at: str

def normalize(payload, language='unknown', topic='', source='import', observed_at=None, evidence='не проверен'):
    data = unwrap(payload)
    rows = data.get('videos', data.get('results'))
    if not isinstance(rows, list):
        raise ValueError('В ответе нет массива videos/results.')
    observed = observed_at or now().isoformat()
    out = []
    for r in rows:
        if not isinstance(r, dict) or r.get('kind', 'video') != 'video':
            continue
        if 'video_id' in r:
            out.append(Video(**{k: r.get(k) for k in Video.__dataclass_fields__}))
            continue
        vid = r.get('videoId', r.get('id'))
        if not isinstance(vid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', vid):
            continue
        dt = stamp(r.get('videoPublishedAt', r.get('publishedAt')))
        out.append(Video(vid, str(r.get('videoTitle', r.get('title', ''))), r.get('channelId'),
                         str(r.get('channelTitle') or 'Название неизвестно'), dt.isoformat() if dt else None,
                         number(r.get('viewCount')), number(r.get('subscriberCount')),
                         seconds(r.get('videoDuration', r.get('duration'))), number(r.get('vph')),
                         number(r.get('breakoutScore', r.get('outlierScore'))),
                         r.get('videoTitleLanguage') or language, evidence, topic, source, observed))
    return out

def unique(videos):
    found = {}
    for v in videos:
        old = found.get(v.video_id)
        if old is None or (stamp(v.observed_at) or datetime.min.replace(tzinfo=UTC)) >= (stamp(old.observed_at) or datetime.min.replace(tzinfo=UTC)):
            found[v.video_id] = v
    return list(found.values())

def select(videos, days, min_views, max_subs, min_minutes, at=None):
    at = at or now()
    selected = []
    for v in unique(videos):
        dt = stamp(v.published)
        if dt is None or not 0 <= (at - dt).total_seconds() <= days * 86400:
            continue
        if v.views is None or v.views < min_views:
            continue
        if v.subscribers is None or v.subscribers > max_subs:
            continue
        if v.duration is None or v.duration < min_minutes * 60:
            continue
        selected.append(v)
    return sorted(selected, key=lambda v: (v.vph is not None, v.vph or 0, v.views or 0), reverse=True)

def summarize(videos):
    groups = defaultdict(list)
    for v in unique(videos):
        groups[(v.language, v.topic)].append(v)
    lines = []
    for (lang, topic), rows in sorted(groups.items()):
        channels = {v.channel_id for v in rows if v.channel_id}
        hits = defaultdict(int)
        for v in rows:
            if v.channel_id:
                hits[v.channel_id] += 1
        lines.append(f'{lang} | {topic or "Все темы"}: {len(rows)} видео; {len(channels)} известных каналов; '
                     f'{sum(n >= 2 for n in hits.values())} каналов с ≥2 найденными хитами.')
    return '\n'.join(lines) or 'Нет видео по текущим условиям. Попробуйте другую тему или более мягкие пороги.'

def revenue(views, rpm, cost):
    return views / 1000 * rpm - cost

def signed_number(value):
    if value is None or isinstance(value,bool):
        return None
    try:
        n=float(value)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):
        return None

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def decode_rpc(raw, request_id):
    text = raw.decode('utf-8-sig')
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get('id') == request_id:
            return obj
    except ValueError:
        pass
    # MCP may return JSON via a finite SSE stream. Join multiline data events.
    for event in re.split(r'\r?\n\r?\n', text):
        data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
        if data:
            try:
                obj = json.loads(data)
                if isinstance(obj, dict) and obj.get('id') == request_id:
                    return obj
            except ValueError:
                continue
    raise RuntimeError('Не удалось прочитать ответ MCP. Запрос не повторён.')

class MCP:
    """Read-only allowlist. No automatic retries, no credentials on disk."""
    COSTS = {'vidiq_balance': 0, 'vidiq_trending_videos': 5, 'vidiq_outliers': 5,
             'vidiq_youtube_search': 5, 'vidiq_channel_stats': 5, 'vidiq_channel_videos': 5}

    def __init__(self, key, budget=15):
        if not key.strip():
            raise ValueError('Введите API key из vidIQ → Account Settings → MCP.')
        self.key = key.strip()
        self.budget = budget
        self.reserved = 0
        self.actual = 0
        self.unknown_charges = 0
        self.session = None
        self.counter = 0
        self.tools = {}
        self.protocol = '2025-03-26'

    def rpc(self, method, params, notification=False):
        self.counter += 1
        body = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            body['id'] = self.counter
        headers = {'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json',
                   'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': self.protocol}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        req = urllib.request.Request(ENDPOINT, json.dumps(body).encode(), headers, method='POST')
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=60) as response:
                if response.headers.get('Mcp-Session-Id'):
                    self.session = response.headers['Mcp-Session-Id']
                if notification:
                    return {}
                if 'text/event-stream' in response.headers.get('Content-Type', ''):
                    chunks = bytearray()
                    while len(chunks) < 8 * 1024 * 1024:
                        line = response.readline()
                        if not line:
                            break
                        chunks.extend(line)
                        if line.strip() == b'':
                            try:
                                result = decode_rpc(bytes(chunks), self.counter)
                                break
                            except RuntimeError:
                                continue
                    else:
                        raise RuntimeError('Ответ MCP слишком большой.')
                    if 'result' not in locals():
                        result = decode_rpc(bytes(chunks), self.counter)
                else:
                    result = decode_rpc(response.read(8 * 1024 * 1024), self.counter)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'vidIQ: HTTP {exc.code}. Проверьте API key, доступ и баланс. Автоповтор отключён.') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError('Нет ответа vidIQ. Проверьте интернет. Запрос не повторён; списание может быть неизвестно.') from None
        if 'error' in result:
            raise RuntimeError('Сервер MCP отклонил запрос. Проверьте доступ и совместимость API; запрос не повторён.')
        return result.get('result', {})

    def connect(self):
        init = self.rpc('initialize', {'protocolVersion': self.protocol, 'capabilities': {},
                                      'clientInfo': {'name': 'NicheScout', 'version': '0.1.0'}})
        self.protocol = init.get('protocolVersion', self.protocol)
        self.rpc('notifications/initialized', {}, notification=True)
        cursor = None
        for _ in range(20):
            params = {'cursor': cursor} if cursor else {}
            result = self.rpc('tools/list', params)
            for tool in result.get('tools', []):
                self.tools[tool['name']] = tool
            cursor = result.get('nextCursor')
            if not cursor:
                break
        return self.call('vidiq_balance', {})

    def call(self, logical_name, arguments):
        if logical_name not in self.COSTS:
            raise ValueError('Этот инструмент не разрешён программой.')
        candidates = [name for name in self.tools if name == logical_name or name.endswith('__' + logical_name)]
        if len(candidates) != 1:
            raise RuntimeError(f'Инструмент {logical_name} недоступен или неоднозначен в этом подключении.')
        name = candidates[0]
        schema = self.tools[name].get('inputSchema', {})
        props = schema.get('properties', {})
        unknown = set(arguments) - set(props)
        missing = set(schema.get('required', [])) - set(arguments)
        if unknown or missing:
            raise RuntimeError('Схема vidIQ изменилась. Нужна проверка совместимости программы.')
        price = self.COSTS[logical_name]
        if self.reserved + price > self.budget:
            raise RuntimeError('Достигнут лимит кредитов этого запуска. Частичные данные сохранены в таблице.')
        self.reserved += price
        try:
            raw = self.rpc('tools/call', {'name': name, 'arguments': arguments})
            data = unwrap(raw)
        except Exception:
            if price:
                self.unknown_charges += 1
            raise
        charged = number(data.get('_credits', {}).get('used'))
        if charged is None:
            if price:
                self.unknown_charges += 1
        else:
            self.actual += charged
        return data

def plan(language, queries, days, min_views, max_subs, mode='trending', at=None):
    cutoff = ((at or now()) - timedelta(days=days)).isoformat()
    if language == 'ro':
        if not queries:
            raise ValueError('Для румынского добавьте хотя бы одну поисковую фразу.')
        return [('vidiq_youtube_search', {'query': q, 'type': ['video'], 'order': 'viewCount',
                 'publishedAfter': cutoff, 'regionCode': 'RO', 'videoDuration': 'long', 'limit': 50}, q,
                 'поиск фразы; язык не подтверждён') for q in queries]
    if mode == 'outliers':
        return [('vidiq_outliers', dict({'language': language, 'contentType': 'long',
                 'publishedWithin': 'thisMonth' if days <= 30 else 'threeMonths' if days <= 90 else 'sixMonths',
                 'minViews': min_views, 'maxSubscribers': max_subs, 'sort': 'vph', 'limit': 50},
                 **({'keyword': q} if q else {})), q or 'Все темы', 'фильтр языка заголовка vidIQ') for q in (queries or [''])]
    return [('vidiq_trending_videos', dict({'videoFormat': 'long', 'videoTitleLanguage': language,
             'videoPublishedAfter': cutoff[:10], 'viewCountMin': min_views, 'subscriberCountMax': max_subs,
             'sortBy': 'vph', 'limit': 50}, **({'titleQuery': q} if q else {})), q or 'Все темы',
             'фильтр языка заголовка vidIQ') for q in (queries or [''])]

def audit_text(channel, stats, recent, days, threshold, at=None):
    at = at or now()
    rows = normalize(recent)
    cutoff = at - timedelta(days=days)
    recent_rows = [v for v in unique(rows) if stamp(v.published) and cutoff <= stamp(v.published) <= at]
    known = [v.views for v in recent_rows if v.views is not None]
    hits = sum(v >= threshold for v in known)
    created = stamp(stats.get('publishedAt'))
    growth = stats.get('growth', {})
    daily = stats.get('dailyStats') or []
    current = stats.get('currentStats') or {}
    dates = [stamp(v.published) for v in rows if stamp(v.published)]
    def val(x):
        return 'неизвестно' if x is None else f'{x:,.0f}'
    lines = [f'Канал: {stats.get("title") or channel}', f'ID: {channel}', f'Проверено: {at.isoformat()}',
             f'Подписчики сейчас: {val(number(current.get("subscribers")))}',
             f'Регистрация: {created.date() if created else "неизвестна"}',
             f'Возраст регистрации: {(at-created).days if created else "неизвестен"} дней',
             f'Начало возвращённой выборки: {min(dates).date() if dates else "неизвестно"}',
             f'Длинных видео в ответе: {len(rows)}; за последние {days} дней: {len(recent_rows)}',
             f'Из них с известными просмотрами: {len(known)}; ≥{threshold:,}: {hits}',
             f'Медиана просмотров этой свежей выборки: {val(statistics.median(known) if known else None)}',
             f'Прирост подписчиков за запрошенный период: {val(signed_number(growth.get("subscribersGained")))}',
             f'Прирост просмотров за запрошенный период: {val(signed_number(growth.get("viewsGained")))}',
             f'Точек истории от поставщика: {len(daily)}', '',
             'Повторяемость: ' + ('найдено несколько успешных публикаций; изучите их темы.' if hits >= 2 else
                                  'в этой выборке недостаточно подтверждений нескольких успешных публикаций.'),
             'Это ограниченная выборка последних длинных видео. Отсутствующие данные неизвестны.',
             'Подписчики/просмотры не подтверждают монетизацию, прибыль или естественное происхождение трафика.',
             'Страна канала не равна стране зрителей. Чужой RPM и удержание не получены.']
    return '\n'.join(lines), rows

def save_run(videos, audits):
    DATA.mkdir(parents=True, exist_ok=True)
    for path in DATA.glob('run_*.json'):
        if now().timestamp() - path.stat().st_mtime > 30 * 86400:
            path.unlink()
    dest = DATA / ('run_' + now().strftime('%Y%m%d_%H%M%S_%f') + '.json')
    dest.write_text(json.dumps({'version': 1, 'observed_at': now().isoformat(),
                              'videos': [asdict(v) for v in unique(videos)], 'audits': audits}, ensure_ascii=False, indent=2), encoding='utf-8')
    return dest

def safe_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value

def export_csv(videos, dest):
    with open(dest, 'w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerow(['title', 'channel', 'language', 'language_evidence', 'topic', 'views', 'subscribers',
                         'duration_minutes', 'published', 'vph_vidiq', 'breakout_vidiq', 'source', 'observed_at', 'url'])
        for v in videos:
            writer.writerow([safe_cell(x) for x in [v.title, v.channel_title, v.language, v.language_evidence,
                 v.topic, v.views, v.subscribers, v.duration / 60 if v.duration is not None else None,
                 v.published, v.vph, v.breakout, v.source, v.observed_at, 'https://www.youtube.com/watch?v=' + v.video_id]])

def main(self_test=False, report_path=None):
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog

    app = tk.Tk()
    app.title('Niche Scout · Поиск растущих ниш')
    app.geometry('1320x850')
    app.minsize(1000, 690)
    style = ttk.Style(app)
    style.theme_use('clam')
    style.configure('TFrame', background='#f0f3f7')
    style.configure('TLabel', background='#f0f3f7', foreground='#182536', font=('Segoe UI', 10))
    style.configure('Title.TLabel', font=('Segoe UI', 23, 'bold'))
    style.configure('Treeview', rowheight=31, font=('Segoe UI', 10))
    style.configure('Treeview.Heading', font=('Segoe UI', 10, 'bold'))
    style.configure('TButton', padding=7)
    outer = ttk.Frame(app, padding=18)
    outer.pack(fill='both', expand=True)
    ttk.Label(outer, text='Niche Scout', style='Title.TLabel').pack(anchor='w')
    ttk.Label(outer, text='Свежие просмотры · Небольшие каналы · Проверка повторяемости', foreground='#52667a').pack(anchor='w', pady=(0, 12))
    controls = ttk.Frame(outer)
    controls.pack(fill='x')
    key = tk.StringVar(value=os.environ.get('VIDIQ_API_KEY', ''))
    ttk.Label(controls, text='vidIQ API key').grid(row=0, column=0, sticky='w')
    ttk.Entry(controls, textvariable=key, show='•', width=42).grid(row=0, column=1, padx=8)
    ttk.Button(controls, text='Где получить ключ', command=lambda: webbrowser.open('https://app.vidiq.com')).grid(row=0, column=2)
    status = tk.StringVar(value='Ключ хранится только в памяти. Импорт работает без подключения. Демо — вымышленные данные.')
    ttk.Label(outer, textvariable=status, wraplength=1250).pack(anchor='w', pady=10)
    notebook = ttk.Notebook(outer)
    notebook.pack(fill='both', expand=True)
    search_tab, audit_tab, money_tab, method_tab = [ttk.Frame(notebook, padding=12) for _ in range(4)]
    for tab, name in zip((search_tab, audit_tab, money_tab, method_tab), ('Поиск', 'Проверка канала', 'Сценарии прибыли', 'Метод и ограничения')):
        notebook.add(tab, text=name)
    filters = ttk.Frame(search_tab)
    filters.pack(fill='x')
    lang = tk.StringVar(value='Польский')
    mode = tk.StringVar(value='trending')
    vals = {k: tk.StringVar(value=v) for k, v in [('days','21'),('views','100000'),('subs','50000'),('minutes','20'),('budget','15')]}
    ttk.Label(filters, text='Язык').grid(row=0, column=0, sticky='w')
    lang_combo = ttk.Combobox(filters, textvariable=lang, values=list(LANGS), state='readonly', width=15)
    lang_combo.grid(row=1, column=0, padx=(0, 12))
    for col, (k, label) in enumerate([('days','Дней (1–180)'),('views','Просмотров от'),('subs','Подписчиков до'),('minutes','Минут от (≥20)'),('budget','Лимит кредитов')], 1):
        ttk.Label(filters, text=label).grid(row=0, column=col, sticky='w')
        ttk.Entry(filters, textvariable=vals[k], width=15).grid(row=1, column=col, padx=(0, 12))
    ttk.Combobox(filters, textvariable=mode, values=['trending','outliers'], state='readonly', width=12).grid(row=1, column=6)
    ttk.Label(search_tab, text='Темы / поисковые фразы: по одной на строку. Пусто = все темы (кроме румынского).').pack(anchor='w', pady=(10, 2))
    query_box = tk.Text(search_tab, height=3, font=('Segoe UI', 10), wrap='word')
    query_box.pack(fill='x')
    query_box.insert('1.0', SEEDS['pl'].splitlines()[0])
    def set_seeds(event=None):
        query_box.delete('1.0','end')
        query_box.insert('1.0', SEEDS[LANGS[lang.get()]].splitlines()[0])
    lang_combo.bind('<<ComboboxSelected>>', set_seeds)
    buttons = ttk.Frame(search_tab)
    buttons.pack(fill='x', pady=8)
    state = {'videos': [], 'visible': [], 'audits': [], 'busy': False, 'selected': None}
    messages = queue.Queue()

    def parameters():
        try:
            p = {k: int(v.get()) for k,v in vals.items()}
        except ValueError:
            raise ValueError('В фильтрах нужны целые числа.')
        if not 1 <= p['days'] <= 180 or min(p['views'],p['subs'],p['budget']) < 0 or p['minutes'] < 20:
            raise ValueError('Дней: 1–180. Длительность: минимум 20 минут. Остальные значения: неотрицательные.')
        return p

    def display():
        try:
            p = parameters()
            visible = select(state['videos'],p['days'],p['views'],p['subs'],p['minutes'])
        except ValueError as exc:
            messagebox.showerror('Параметры',str(exc)); return
        state['visible'] = visible
        tree.delete(*tree.get_children())
        def fmt(n):
            return '—' if n is None else f'{n:,.0f}'
        for idx,v in enumerate(visible):
            tree.insert('', 'end', iid=str(idx), values=(v.title,v.channel_title,v.language,fmt(v.views),fmt(v.subscribers),
                        fmt(v.vph),fmt(v.duration / 60 if v.duration is not None else None),(v.published or '—')[:10]))
        summary.set(summarize(visible) + f'\nЗагружено {len(unique(state["videos"]))}; неизвестные дата/просмотры/подписчики/длительность не проходят фильтр.')

    def work(fn):
        if state['busy']:
            return
        state['busy'] = True
        status.set('Выполняется запрос. Частичные результаты появятся по мере получения…')
        for button in network_buttons:
            button.configure(state='disabled')
        def runner():
            try:
                fn()
            except Exception as exc:
                messages.put(('error', str(exc)))
            finally:
                messages.put(('done',None))
        threading.Thread(target=runner,daemon=True).start()

    def connect_only():
        api_key = key.get()
        def run():
            client = MCP(api_key,0)
            balance = client.connect()
            total = balance.get('totalCredits')
            messages.put(('status',f'Подключено. Баланс: {"безлимит" if balance.get("type")=="unlimited" else total if total is not None else "неизвестен"}. Исследование ещё не запущено.'))
        work(run)

    def run_search():
        try:
            p = parameters()
            language = LANGS[lang.get()]
            queries = list(dict.fromkeys(q.strip() for q in query_box.get('1.0','end').splitlines() if q.strip()))
            tasks = plan(language, queries,p['days'],p['views'],p['subs'],mode.get())
            estimate = len(tasks)*5
            if estimate > p['budget']:
                raise ValueError(f'План: {estimate} кредитов, лимит: {p["budget"]}. Сократите число фраз или измените лимит.')
        except ValueError as exc:
            messagebox.showerror('Параметры', str(exc)); return
        if not messagebox.askokcancel('Запуск поиска',f'{len(tasks)} запрос(а), ожидаемо {estimate} кредитов vidIQ.\nДанные ограничены базой и выборкой сервиса.\nЗапустить?'):
            return
        if state['videos']:
            try:
                save_run(state['videos'],state['audits'])
            except OSError:
                messagebox.showerror('Сохранение','Не удалось сохранить предыдущий запуск. Экспортируйте результаты перед новым поиском.')
                return
        state['videos']=[]
        state['audits']=[]
        display()
        api_key = key.get()
        def run():
            client = MCP(api_key,p['budget'])
            try:
                balance = client.connect()
                total = number(balance.get('totalCredits'))
                if balance.get('type') != 'unlimited' and (total is None or total < estimate):
                    raise RuntimeError('Баланс неизвестен или недостаточен; платные запросы не отправлены.')
                for name,args,topic,evidence in tasks:
                    payload = client.call(name,args)
                    rows = normalize(payload, language,topic,'vidIQ / '+name,evidence=evidence)
                    messages.put(('rows',rows))
            finally:
                messages.put(('credits', (client.actual,client.unknown_charges,client.reserved)))
        work(run)

    def import_data():
        if state['busy']:
            return
        file = filedialog.askopenfilename(filetypes=[('JSON','*.json')])
        if not file:
            return
        try:
            content = json.loads(Path(file).read_text(encoding='utf-8-sig'))
            language = content.get('language','unknown') if isinstance(content,dict) else 'unknown'
            rows = normalize(content,language=language,source='Импорт: '+Path(file).name,
                             observed_at=content.get('observed_at') if isinstance(content,dict) else None)
            state['videos'] = unique(state['videos']+rows)
            state['audits'].extend(content.get('audits',[]) if isinstance(content,dict) else [])
            display()
            status.set(f'Импортировано {len(rows)} видео. Возраст считается на сегодня; дата наблюдения сохранена. Раздел «Метод» объясняет ограничения.')
        except Exception as exc:
            messagebox.showerror('Импорт',str(exc))

    def demo():
        if state['busy']:
            return
        vals['days'].set('21'); vals['views'].set('100000'); vals['subs'].set('50000'); vals['minutes'].set('20')
        rows = []
        for i,(views,subs,age,vph) in enumerate([(210000,2200,6,380),(145000,2200,12,160),(118000,4800,8,210)]):
            rows.append({'videoId':f'DEMO{i:07d}','videoTitle':f'ДЕМО — вымышленная история {i+1}',
                        'channelId':'DEMO_CHANNEL_'+str(i//2),'channelTitle':'ДЕМО канал '+str(i//2+1),
                        'viewCount':views,'subscriberCount':subs,'videoDuration':3600,
                        'videoPublishedAt':(now()-timedelta(days=age)).isoformat(),'vph':vph})
        state['videos'] = normalize({'videos':rows},'pl','Демонстрация','ВЫМЫШЛЕННЫЕ ДАННЫЕ',evidence='демо')
        state['audits'] = []
        display()
        status.set('ДЕМО: все каналы, видео и показатели вымышлены. Ссылки и платная проверка демо отключены.')

    def export():
        if not state['visible']:
            messagebox.showinfo('Экспорт','В таблице пока нет результатов.'); return
        file = filedialog.asksaveasfilename(defaultextension='.csv',initialfile='NicheScout_results.csv',filetypes=[('CSV','*.csv')])
        if file:
            export_csv(state['visible'],file)
            status.set('CSV сохранён. Это отобранная выборка, а не полный список конкурентов.')

    def save():
        try:
            file=save_run(state['videos'],state['audits'])
            status.set('Результаты сохранены: '+str(file))
        except OSError:
            messagebox.showerror('Сохранение','Не удалось записать результаты. Выберите доступную папку через экспорт.')

    def selected():
        ids=tree.selection()
        return state['visible'][int(ids[0])] if ids else None

    def open_video(event=None):
        v=selected()
        if v and not v.video_id.startswith('DEMO'):
            webbrowser.open('https://www.youtube.com/watch?v='+v.video_id)

    def audit():
        v=selected()
        if not v or not v.channel_id or v.video_id.startswith('DEMO'):
            messagebox.showinfo('Проверка','Выберите настоящее видео с известным ID канала.'); return
        try:
            p=parameters()
        except ValueError as exc:
            messagebox.showerror('Параметры',str(exc)); return
        if p['budget'] < 10:
            messagebox.showinfo('Лимит','Проверка требует лимит хотя бы 10 кредитов.'); return
        if not messagebox.askokcancel('Проверка канала','История канала + последние длинные видео: ожидаемо 10 кредитов vidIQ.\nПродолжить?'):
            return
        api_key=key.get()
        def run():
            client=MCP(api_key,10)
            try:
                balance=client.connect()
                total=number(balance.get('totalCredits'))
                if balance.get('type')!='unlimited' and (total is None or total<10):
                    raise RuntimeError('Недостаточный или неизвестный баланс; платная проверка не начата.')
                data=client.call('vidiq_channel_stats',{'channelId':v.channel_id,'from':(now()-timedelta(days=p['days'])).date().isoformat(),'to':now().date().isoformat()})
                recent=client.call('vidiq_channel_videos',{'channelId':v.channel_id,'popular':False,'videoFormat':'long'})
                report,_=audit_text(v.channel_id,data,recent,p['days'],p['views'])
                messages.put(('audit',report))
            finally:
                messages.put(('credits',(client.actual,client.unknown_charges,client.reserved)))
        work(run)

    check_button=ttk.Button(controls,text='Проверить подключение · 0',command=connect_only)
    check_button.grid(row=0,column=3,padx=8)
    search_button=ttk.Button(buttons,text='Найти кандидатов',command=run_search)
    search_button.pack(side='left',padx=(0,6))
    ttk.Button(buttons,text='Применить фильтры',command=display).pack(side='left',padx=6)
    audit_button=ttk.Button(buttons,text='Проверить выбранный канал · 10',command=audit)
    audit_button.pack(side='left',padx=6)
    ttk.Button(buttons,text='Открыть видео',command=open_video).pack(side='left',padx=6)
    ttk.Button(buttons,text='Импорт JSON',command=import_data).pack(side='left',padx=6)
    ttk.Button(buttons,text='Демо',command=demo).pack(side='left',padx=6)
    network_buttons=[check_button,search_button,audit_button]
    table_frame=ttk.Frame(search_tab)
    table_frame.pack(fill='both',expand=True)
    cols=('title','channel','lang','views','subs','vph','minutes','date')
    tree=ttk.Treeview(table_frame,columns=cols,show='headings',selectmode='browse')
    for col,label,width in zip(cols,['Видео','Канал','Язык','Просмотры','Подписчики','VPH vidIQ','Минут','Дата'],[370,180,55,100,100,90,65,100]):
        tree.heading(col,text=label)
        tree.column(col,width=width,minwidth=45,stretch=col in ('title','channel'))
    yscroll=ttk.Scrollbar(table_frame,orient='vertical',command=tree.yview)
    xscroll=ttk.Scrollbar(table_frame,orient='horizontal',command=tree.xview)
    tree.configure(yscrollcommand=yscroll.set,xscrollcommand=xscroll.set)
    tree.grid(row=0,column=0,sticky='nsew'); yscroll.grid(row=0,column=1,sticky='ns'); xscroll.grid(row=1,column=0,sticky='ew')
    table_frame.rowconfigure(0,weight=1); table_frame.columnconfigure(0,weight=1)
    tree.bind('<Double-1>',open_video)
    summary=tk.StringVar(value='Результаты появятся после поиска, импорта или загрузки демо.')
    ttk.Label(search_tab,textvariable=summary,wraplength=1200).pack(anchor='w',pady=8)
    foot=ttk.Frame(search_tab); foot.pack(fill='x')
    ttk.Button(foot,text='Экспорт таблицы CSV',command=export).pack(side='left',padx=(0,8))
    ttk.Button(foot,text='Сохранить полный запуск',command=save).pack(side='left')
    audit_box=tk.Text(audit_tab,font=('Segoe UI',11),wrap='word',padx=14,pady=14)
    audit_box.pack(fill='both',expand=True)
    audit_box.insert('1.0','Выберите видео в таблице и нажмите «Проверить выбранный канал».\nЗдесь будут история роста и проверка повторяемости публикаций.')
    method_box=tk.Text(method_tab,font=('Segoe UI',11),wrap='word',padx=14,pady=14)
    method_box.pack(fill='both',expand=True); method_box.insert('1.0',METHOD); method_box.configure(state='disabled')
    ttk.Label(money_tab,text='Сценарий дохода после расходов',font=('Segoe UI',18,'bold')).pack(anchor='w')
    ttk.Label(money_tab,text='Все значения вводите сами. Это условный расчёт, не RPM выбранного языка и не прогноз роста.',wraplength=1000).pack(anchor='w',pady=12)
    money=ttk.Frame(money_tab); money.pack(anchor='w')
    mvars={k:tk.StringVar(value=v) for k,v in [('views','100000'),('cost','100'),('low','1'),('base','3'),('high','5')]}
    for i,(k,label) in enumerate([('views','Просмотры за период'),('cost','Расходы за тот же период, $'),('low','Низкий RPM, $'),('base','Средний RPM, $'),('high','Высокий RPM, $')]):
        ttk.Label(money,text=label).grid(row=i,column=0,sticky='w',pady=5)
        ttk.Entry(money,textvariable=mvars[k],width=20).grid(row=i,column=1,padx=15)
    money_result=tk.StringVar(value='Примерные RPM 1 / 3 / 5 — произвольные допущения, не данные о странах.')
    def calc():
        try:
            nums={k:number(v.get().replace(',','.')) for k,v in mvars.items()}
            if any(v is None for v in nums.values()) or not nums['low']<=nums['base']<=nums['high']:
                raise ValueError()
            result=[]
            for k,title in [('low','Низкий'),('base','Средний'),('high','Высокий')]:
                result.append(f'{title}: доход ${nums["views"]/1000*nums[k]:,.2f}; после расходов ${revenue(nums["views"],nums[k],nums["cost"]):,.2f}')
            money_result.set('\n'.join(result)+'\nДо налогов. Для немонетизированного канала рекламный доход автора может быть $0.')
        except ValueError:
            messagebox.showerror('Расчёт','Введите неотрицательные числа. RPM: низкий ≤ средний ≤ высокий.')
    ttk.Button(money_tab,text='Рассчитать',command=calc).pack(anchor='w',pady=12)
    ttk.Label(money_tab,textvariable=money_result,font=('Segoe UI',12),wraplength=1000).pack(anchor='w')

    def poll():
        try:
            while True:
                kind,value=messages.get_nowait()
                if kind=='rows':
                    state['videos']=unique(state['videos']+value); display()
                elif kind=='audit':
                    state['audits'].append(value); audit_box.delete('1.0','end'); audit_box.insert('1.0',value); notebook.select(audit_tab)
                elif kind=='error':
                    messagebox.showerror('Запрос остановлен',value); status.set(value)
                elif kind=='credits':
                    actual,unknown,reserved=value
                    status.set(f'Подтверждённое списание в этом запуске: {actual:g}; запросов с неизвестным списанием: {unknown}. Зарезервировано по плану: {reserved}.')
                elif kind=='status':
                    status.set(value)
                elif kind=='done':
                    state['busy']=False
                    for b in network_buttons: b.configure(state='normal')
                    if state['videos']:
                        try: save_run(state['videos'],state['audits'])
                        except OSError: status.set(status.get()+' Автосохранение не удалось — используйте экспорт.')
        except queue.Empty:
            pass
        app.after(100,poll)
    poll()
    smoke_status = {'code': 0}
    if self_test:
        def smoke():
            report = {'ok': False}
            try:
                # Exercise the actual widgets and callbacks; never make network calls.
                key.set('')
                demo()
                app.update_idletasks()
                assert len(tree.get_children()) == 3, 'Demo table did not show 3 rows'
                assert len(state['visible']) == 3, 'Demo filtering failed'
                calc()
                assert '$200.00' in money_result.get(), 'Profit callback failed'
                notebook.select(method_tab)
                app.update_idletasks()
                assert app.winfo_width() >= 1000, 'Main window did not initialize'
                report = {'ok': True, 'demo_rows': len(state['visible']),
                          'window_width': app.winfo_width(), 'window_height': app.winfo_height(),
                          'tabs': len(notebook.tabs()), 'network_calls': 0, 'profit_calculator': True}
            except Exception as exc:
                smoke_status['code'] = 1
                report = {'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)}
            finally:
                if report_path:
                    try:
                        Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                    except OSError:
                        smoke_status['code'] = 1
                app.destroy()
        app.after(250, smoke)
    app.mainloop()
    return smoke_status['code']

if __name__=='__main__':
    import sys
    testing = len(sys.argv) > 1 and sys.argv[1] == '--self-test'
    raise SystemExit(main(testing, sys.argv[2] if testing and len(sys.argv) > 2 else None))
